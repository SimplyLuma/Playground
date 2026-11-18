# SPDX-License-Identifier: Apache-2.0
"""Keeping agents inside the project.

Two jobs live here, and both are small on purpose so they can be read and
trusted:

1. **Paths.** ``resolve_in_project`` turns whatever path an agent hands us into
   a real path inside the project folder, or refuses. It catches ``..``,
   absolute paths elsewhere, symlinks that point out, NUL bytes, and writes to
   the few files an agent must never rewrite (its own permissions, the
   project's sharing settings, the person's answers).

2. **Commands.** ``Terminal`` runs one command for an agent. On Linux, when
   bubblewrap works, the command sees a read-only system, a private home and
   ``/tmp``, and a read/write project. On macOS, when ``sandbox-exec`` works, a
   Seatbelt profile allows writes only to the project, temp and caches, and
   hides credential folders. Otherwise the command still runs in the project
   folder with a cleaned environment, and the log says it was unconfined.
   Network is allowed and every command is logged with that fact.

3. **Agent programs.** ``confine_agent`` wraps a whole ACP agent (Claude,
   Codex, ...) in the same kind of fence, so the work it does with its own
   tools stays inside the project too. See the section at the end.

No GTK imports. No network calls.
"""
from __future__ import annotations

import codecs
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

__all__ = [
    "PathEscape", "ProtectedPath", "resolve_in_project", "relative_to_project",
    "read_text_file", "write_text_file", "SandboxPolicy", "Terminal",
    "TerminalManager", "run_command", "clean_env", "backend", "wrap_argv",
    "confine_agent", "AgentConfinement", "SandboxUnavailable", "agent_sandbox_wanted", "project_is_shared",
    "resolve_npx", "SANDBOX_SETTINGS",
]

# ── Paths ──────────────────────────────────────────────────────────────────

MAX_READ_BYTES = 8 * 1024 * 1024
MAX_WRITE_BYTES = 8 * 1024 * 1024

# Project-relative paths an agent may read but never write. Directories end in "/".
PROTECTED_WRITES = (
    ".git/hooks/",
    ".git/config",
    ".mcp.json",
    ".claude/",
    ".codex/",
    ".playground/project.toml",
    ".playground/sharing.toml",
    ".playground/answers/",
)


class PathEscape(PermissionError):
    """The path is outside the project, or isn't a usable path at all."""


class ProtectedPath(PermissionError):
    """The path is inside the project but agents may not change it."""


def _real_root(root: str | os.PathLike) -> Path:
    return Path(os.path.realpath(os.fspath(root)))


def resolve_in_project(root: str | os.PathLike, path: str | os.PathLike, *, write: bool = False) -> Path:
    """Return the real absolute path of ``path`` inside ``root``, or raise.

    Relative paths are taken from the project folder. Symlinks are followed
    for every part that exists, so a link that points out of the project is
    refused. With ``write=True`` the protected list is also enforced.
    """
    raw = os.fspath(path) if path is not None else ""
    if not isinstance(raw, str) or not raw.strip():
        raise PathEscape("No file path was given.")
    if "\x00" in raw:
        raise PathEscape("That file path has a hidden character in it, so it was refused.")
    base = _real_root(root)
    candidate = Path(os.path.expanduser(raw)) if raw.startswith("~") else Path(raw)
    if not candidate.is_absolute():
        candidate = base / candidate
    # Lexical clean-up first ("a/../b"), then follow whatever links exist.
    real = Path(os.path.realpath(os.path.normpath(candidate)))
    if real != base and base not in real.parents:
        raise PathEscape(f"{raw} is outside this project, so it was refused.")
    if write:
        if real == base:
            raise PathEscape("That is the project folder itself, not a file.")
        rel = real.relative_to(base).as_posix()
        for protected in PROTECTED_WRITES:
            if protected.endswith("/"):
                if rel == protected[:-1] or rel.startswith(protected):
                    raise ProtectedPath(f"{rel} is looked after by Playground, so agents can't change it.")
            elif rel == protected:
                raise ProtectedPath(f"{rel} is looked after by Playground, so agents can't change it.")
    return real


def relative_to_project(root: str | os.PathLike, path: str | os.PathLike) -> str | None:
    """A project-relative POSIX path, or None when ``path`` is outside."""
    try:
        real = resolve_in_project(root, path)
    except PermissionError:
        return None
    base = _real_root(root)
    return "." if real == base else real.relative_to(base).as_posix()


def read_text_file(root: str | os.PathLike, path: str, line: int | None = None, limit: int | None = None) -> str:
    """Read a text file inside the project. ``line`` is 1-based."""
    real = resolve_in_project(root, path)
    if real.is_dir():
        raise IsADirectoryError(f"{path} is a folder, not a file.")
    size = real.stat().st_size  # FileNotFoundError propagates with its own meaning
    if size > MAX_READ_BYTES and not (line or limit):
        raise ValueError(f"{path} is too big to read in one go ({size // 1024 // 1024} MB). Ask for a range of lines.")
    with open(real, "rb") as handle:
        head = handle.read(8192)
        if b"\x00" in head:
            raise ValueError(f"{path} isn't a text file.")
        handle.seek(0)
        if line or limit:
            start = max(int(line or 1), 1)
            count = int(limit) if limit else None
            out: list[str] = []
            for number, raw in enumerate(handle, start=1):
                if number < start:
                    continue
                if count is not None and len(out) >= count:
                    break
                out.append(raw.decode("utf-8", errors="replace"))
            return "".join(out)
        return handle.read().decode("utf-8", errors="replace")


def write_text_file(root: str | os.PathLike, path: str, content: str) -> Path:
    """Write a text file inside the project atomically, creating folders as needed."""
    if not isinstance(content, str):
        raise ValueError("File content must be text.")
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        raise ValueError("That file is too big to write in one go.")
    real = resolve_in_project(root, path, write=True)
    if real.is_dir():
        raise IsADirectoryError(f"{path} is a folder, not a file.")
    real.parent.mkdir(parents=True, exist_ok=True)
    # Re-check the parent after creating it: a link swapped in meanwhile is refused.
    resolve_in_project(root, real.parent)
    mode = None
    try:
        mode = real.stat().st_mode & 0o7777
    except FileNotFoundError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=".pg-", suffix=".tmp", dir=str(real.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, real)  # replaces a link itself, never its target
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return real


# ── Environment ────────────────────────────────────────────────────────────

_SECRET_NAME = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|APIKEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIAL)", re.I)
_DROP_ALWAYS = {
    "CLAUDE_CONFIG_DIR", "CODEX_HOME", "SSH_AUTH_SOCK", "GPG_AGENT_INFO",
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT",
}


def clean_env(extra: dict[str, str] | None = None, base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a sandboxed command gets: no credentials, no agent homes."""
    source = dict(os.environ if base is None else base)
    env = {k: v for k, v in source.items() if k not in _DROP_ALWAYS and not _SECRET_NAME.search(k)}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["PLAYGROUND_SANDBOX"] = "1"
    for key, value in (extra or {}).items():
        if isinstance(key, str) and key and "=" not in key and "\x00" not in key and isinstance(value, str):
            env[key] = value
    return env


# ── Sandbox backends ───────────────────────────────────────────────────────

@dataclass
class SandboxPolicy:
    """What a command may touch. Built per project."""
    project_root: str
    network: bool = True
    hide_home: bool = True
    backend: str = "auto"            # "auto" | "bwrap" | "seatbelt" | "none"
    log_path: str | None = None      # one line per command; defaults to .playground/local/logs/sandbox.log
    extra_writable: list[str] = field(default_factory=list)
    timeout_s: float = 30 * 60       # hard ceiling per command

    def __post_init__(self) -> None:
        self.project_root = os.path.realpath(self.project_root)
        if self.log_path is None:
            self.log_path = os.path.join(self.project_root, ".playground", "local", "logs", "sandbox.log")


_probe_lock = threading.Lock()
_probe_cache: dict[str, bool] = {}

# Toolchains that live in a home folder; visible read-only inside bubblewrap.
_HOME_TOOLCHAINS = (".nvm", ".cargo", ".rustup", ".local/bin", ".local/lib", ".local/share/pnpm",
                    ".bun", ".deno", ".pyenv", ".volta", ".sdkman", "go", ".gitconfig", ".npmrc")
# Credential folders that Seatbelt hides from commands on macOS.
_HOME_SECRETS = (".ssh", ".claude", ".codex", ".gnupg", ".aws", ".config/gh", ".config/playground",
                 ".local/share/playground/accounts", "Library/Keychains", ".netrc", ".docker/config.json")


def _probe(name: str, argv: list[str]) -> bool:
    with _probe_lock:
        if name in _probe_cache:
            return _probe_cache[name]
        ok = False
        try:
            ok = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=5).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        _probe_cache[name] = ok
        return ok


def bwrap_available() -> bool:
    exe = shutil.which("bwrap")
    if not exe or not sys.platform.startswith("linux"):
        return False
    return _probe("bwrap", [exe, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                            "--unshare-pid", "--die-with-parent", "true"])


def seatbelt_available() -> bool:
    exe = "/usr/bin/sandbox-exec"
    if sys.platform != "darwin" or not os.path.exists(exe):
        return False
    return _probe("seatbelt", [exe, "-p", "(version 1)(allow default)", "/usr/bin/true"])


def backend(policy: SandboxPolicy) -> str:
    """The backend that will actually be used for this policy."""
    choice = policy.backend
    if choice in ("auto", "bwrap") and bwrap_available():
        return "bwrap"
    if choice in ("auto", "seatbelt") and seatbelt_available():
        return "seatbelt"
    return "none"


def _bwrap_argv(argv: list[str], cwd: str, policy: SandboxPolicy) -> list[str]:
    home = os.path.realpath(os.path.expanduser("~"))
    out = [shutil.which("bwrap") or "bwrap",
           "--ro-bind", "/", "/",
           "--dev", "/dev",
           "--proc", "/proc",
           "--tmpfs", "/tmp",
           "--tmpfs", "/var/tmp"]
    if policy.hide_home and home not in ("/", ""):
        out += ["--tmpfs", home]
        for item in _HOME_TOOLCHAINS:
            out += ["--ro-bind-try", os.path.join(home, item), os.path.join(home, item)]
    for extra in policy.extra_writable:
        out += ["--bind-try", extra, extra]
    # Last, so it sits on top of any tmpfs above (a project under /tmp or ~ still shows).
    out += ["--bind", policy.project_root, policy.project_root]
    out += ["--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try"]
    if not policy.network:
        out += ["--unshare-net"]
    out += ["--die-with-parent", "--new-session", "--setenv", "HOME", home, "--chdir", cwd, "--"]
    return out + argv


_SEATBELT = """(version 1)
(allow default)
(deny file-write*)
(allow file-write*
  (subpath (param "PROJECT"))
  (subpath "/private/tmp")
  (subpath "/private/var/folders")
  (subpath "/dev")
  (subpath (string-append (param "HOME") "/Library/Caches"))
  (subpath (string-append (param "HOME") "/.npm"))
  (subpath (string-append (param "HOME") "/.cache")){extra})
(deny file-read* file-write*{secrets})
{network}"""


def _seatbelt_argv(argv: list[str], policy: SandboxPolicy) -> list[str]:
    home = os.path.realpath(os.path.expanduser("~"))
    extra = "".join(f'\n  (subpath "{_sb_quote(os.path.realpath(p))}")' for p in policy.extra_writable)
    secrets = "".join(f'\n  (subpath "{_sb_quote(os.path.join(home, s))}")' for s in _HOME_SECRETS)
    network = "" if policy.network else "(deny network*)"
    profile = _SEATBELT.format(extra=extra, secrets=secrets, network=network)
    return ["/usr/bin/sandbox-exec", "-p", profile, "-D", f"PROJECT={policy.project_root}",
            "-D", f"HOME={home}", "--"] + argv


def _sb_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def wrap_argv(argv: list[str], cwd: str, policy: SandboxPolicy) -> tuple[list[str], str]:
    """Wrap ``argv`` for the policy's backend. Returns (argv, backend name)."""
    which = backend(policy)
    if which == "bwrap":
        return _bwrap_argv(argv, cwd, policy), which
    if which == "seatbelt":
        return _seatbelt_argv(argv, policy), which
    return list(argv), "none"


def _log(policy: SandboxPolicy, line: str) -> None:
    if not policy.log_path:
        return
    try:
        os.makedirs(os.path.dirname(policy.log_path), exist_ok=True)
        with open(policy.log_path, "a", encoding="utf-8") as handle:
            handle.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + line + "\n")
    except OSError:
        pass


_SHELL_CHARS = re.compile(r"[\s|&;<>()$`\\\"'*?\[\]#~=%{}!]")


def command_argv(command: str, args: Iterable[str] | None) -> list[str]:
    """ACP gives ``command`` and ``args``. Some agents put a whole shell line in
    ``command`` with no args; that one runs through ``/bin/sh -c``."""
    args = [str(a) for a in (args or [])]
    if not isinstance(command, str) or not command.strip():
        raise ValueError("No command was given.")
    if not args and _SHELL_CHARS.search(command) and not shutil.which(command):
        return ["/bin/sh", "-c", command]
    return [command, *args]


# ── Terminals ──────────────────────────────────────────────────────────────

DEFAULT_OUTPUT_LIMIT = 1024 * 1024


class Terminal:
    """One sandboxed command with captured, size-limited output."""

    def __init__(self, argv: list[str], *, policy: SandboxPolicy, cwd: str | None = None,
                 env: dict[str, str] | None = None, output_byte_limit: int | None = None,
                 timeout_s: float | None = None, on_output: Callable[[str], None] | None = None) -> None:
        self.policy = policy
        self.cwd = str(resolve_in_project(policy.project_root, cwd or policy.project_root))
        if not os.path.isdir(self.cwd):
            raise NotADirectoryError(f"{cwd} isn't a folder in this project.")
        self.limit = max(int(output_byte_limit or DEFAULT_OUTPUT_LIMIT), 1024)
        self.timeout_s = timeout_s if timeout_s is not None else policy.timeout_s
        self._on_output = on_output
        self._buf = bytearray()
        self._truncated = False
        self._lock = threading.Lock()
        self._done = threading.Event()
        self.exit_code: int | None = None
        self.signal: str | None = None
        self.argv_requested = list(argv)
        wrapped, self.backend = wrap_argv(list(argv), self.cwd, policy)
        _log(policy, f"run backend={self.backend} network={'allowed' if policy.network else 'blocked'} "
                     f"cwd={self.cwd} cmd={shlex.join(argv)[:2000]}")
        self.started = time.monotonic()
        self._proc = subprocess.Popen(
            wrapped, cwd=self.cwd, env=clean_env(env), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        self._reader = threading.Thread(target=self._read, name="pg-terminal-out", daemon=True)
        self._reader.start()
        self._watchdog = threading.Thread(target=self._watch, name="pg-terminal-wait", daemon=True)
        self._watchdog.start()

    # The reader owns stdout; the watchdog owns the exit and the time limit.
    def _read(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        stream = self._proc.stdout
        try:
            while True:
                chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
                if not chunk:
                    break
                with self._lock:
                    self._buf += chunk
                    if len(self._buf) > self.limit:
                        cut = len(self._buf) - self.limit
                        # Keep the cut on a UTF-8 character boundary.
                        while cut < len(self._buf) and (self._buf[cut] & 0xC0) == 0x80:
                            cut += 1
                        del self._buf[:cut]
                        self._truncated = True
                if self._on_output:
                    try:
                        self._on_output(decoder.decode(chunk))
                    except Exception:  # noqa: BLE001 - a listener must not stop the reader
                        pass
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _watch(self) -> None:
        try:
            code = self._proc.wait(timeout=self.timeout_s if self.timeout_s else None)
        except subprocess.TimeoutExpired:
            _log(self.policy, f"timeout after {int(self.timeout_s)}s: {shlex.join(self.argv_requested)[:300]}")
            self._signal_group(signal.SIGTERM)
            try:
                code = self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._signal_group(signal.SIGKILL)
                code = self._proc.wait()
        self._reader.join(timeout=5)
        if code is not None and code < 0:
            try:
                self.signal = signal.Signals(-code).name
            except ValueError:
                self.signal = str(-code)
            self.exit_code = None
        else:
            self.exit_code = code
        self._done.set()

    def _signal_group(self, sig: int) -> None:
        try:
            os.killpg(self._proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self._proc.send_signal(sig)
            except OSError:
                pass

    @property
    def running(self) -> bool:
        return not self._done.is_set()

    def output(self) -> dict:
        with self._lock:
            text = bytes(self._buf).decode("utf-8", errors="replace")
            truncated = self._truncated
        result = {"output": text, "truncated": truncated}
        if self._done.is_set():
            result["exitStatus"] = {"exitCode": self.exit_code, "signal": self.signal}
        return result

    def wait(self, timeout: float | None = None) -> dict | None:
        if not self._done.wait(timeout):
            return None
        return {"exitCode": self.exit_code, "signal": self.signal}

    def kill(self) -> None:
        if self._done.is_set():
            return
        self._signal_group(signal.SIGTERM)
        if not self._done.wait(2):
            self._signal_group(signal.SIGKILL)
            self._done.wait(3)

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started


class TerminalManager:
    """The ``terminal/*`` methods for one agent connection."""

    MAX_TERMINALS = 16

    def __init__(self, policy: SandboxPolicy) -> None:
        self.policy = policy
        self._terms: dict[str, Terminal] = {}
        self._lock = threading.Lock()
        self._n = 0

    def create(self, command: str, args: list[str] | None = None, env: list[dict] | dict | None = None,
               cwd: str | None = None, output_byte_limit: int | None = None) -> str:
        with self._lock:
            live = sum(1 for t in self._terms.values() if t.running)
            if live >= self.MAX_TERMINALS:
                raise RuntimeError("Too many commands are running at once. Wait for one to finish.")
        env_map: dict[str, str] = {}
        if isinstance(env, dict):
            env_map = {str(k): str(v) for k, v in env.items()}
        elif isinstance(env, list):
            for item in env:
                if isinstance(item, dict) and "name" in item:
                    env_map[str(item["name"])] = str(item.get("value", ""))
        term = Terminal(command_argv(command, args), policy=self.policy, cwd=cwd, env=env_map,
                        output_byte_limit=output_byte_limit)
        with self._lock:
            self._n += 1
            term_id = f"term-{self._n}"
            self._terms[term_id] = term
        return term_id

    def get(self, terminal_id: str) -> Terminal:
        with self._lock:
            term = self._terms.get(terminal_id)
        if term is None:
            raise KeyError(f"There's no command called {terminal_id}.")
        return term

    def output(self, terminal_id: str) -> dict:
        return self.get(terminal_id).output()

    def wait_for_exit(self, terminal_id: str, timeout: float | None = None) -> dict:
        status = self.get(terminal_id).wait(timeout)
        if status is None:
            raise TimeoutError("The command is still running.")
        return status

    def kill(self, terminal_id: str) -> None:
        self.get(terminal_id).kill()

    def release(self, terminal_id: str) -> None:
        with self._lock:
            term = self._terms.pop(terminal_id, None)
        if term is not None:
            term.kill()

    def release_all(self) -> None:
        with self._lock:
            terms = list(self._terms.values())
            self._terms.clear()
        for term in terms:
            term.kill()


def run_command(command: str, policy: SandboxPolicy, *, cwd: str | None = None, timeout_s: float = 300,
                output_byte_limit: int = 256 * 1024, env: dict[str, str] | None = None) -> dict:
    """Run a shell command to completion in the sandbox (Ari's ``run_command``).

    Returns ``{"output", "truncated", "exit_code", "signal", "timed_out", "backend", "elapsed_s"}``.
    """
    term = Terminal(["/bin/sh", "-c", command], policy=policy, cwd=cwd, env=env,
                    output_byte_limit=output_byte_limit, timeout_s=timeout_s)
    term.wait(timeout_s + 10)
    if term.running:
        term.kill()
    out = term.output()
    timed_out = term.signal is not None and term.elapsed_s >= timeout_s
    return {"output": out["output"], "truncated": out["truncated"], "exit_code": term.exit_code,
            "signal": term.signal, "timed_out": timed_out, "backend": term.backend,
            "elapsed_s": round(term.elapsed_s, 2)}


# ── Whole agent programs ───────────────────────────────────────────────────
#
# Claude and Codex run most of their work with their own tools, so fencing in
# only the commands they hand back to us isn't enough. ``confine_agent`` wraps
# the agent program itself (and so everything it starts: its shell commands,
# its MCP servers, Codex's own inner sandbox) in the same kind of fence:
#
# - the system is read-only;
# - the project folder is read/write;
# - the connection's own sign-in folder is read/write (the agent keeps its
#   sessions and refreshed sign-in there), plus a private temp folder;
# - the rest of the home folder is hidden, except the toolchains it needs to
#   run (node, the downloaded adapter, git's settings), read-only;
# - the network is on, because the agent has to reach its model.
#
# On Linux that's bubblewrap; on macOS it's a Seatbelt profile with the same
# shape. With neither, ``confine_agent`` says so and the caller decides.

SANDBOX_SETTINGS = ("", "on", "off")   # "" = the project decides (on when it's shared)

# Where each program keeps its sign-in and history when it uses its normal folder.
AGENT_HOMES = {
    "claude": (".claude", ".claude.json"),
    "codex": (".codex",),
    "gemini": (".gemini",),
    "opencode": (".local/share/opencode", ".config/opencode", ".local/state/opencode"),
    "goose": (".config/goose", ".local/share/goose", ".local/state/goose"),
    # Pi normally uses ~/.pi/agent, but a Playground connection points it at a
    # per-project .playground/local/pi/<actor>/ dir via PI_CODING_AGENT_DIR.
    "pi": (".pi",),
}
# Caches a program writes besides its home (logs, update checks).
AGENT_CACHES = {
    "claude": ("Library/Caches/claude-cli-nodejs", ".cache/claude-cli-nodejs"),
    "opencode": (".cache/opencode",),
}
# Programs that keep their sign-in in the macOS keychain.
KEYCHAIN_PROVIDERS = ("claude", "codex", "gemini")
_AGENT_READ_EXTRA = (".config/git", ".gitignore_global", ".ssh/known_hosts")


class SandboxUnavailable(RuntimeError):
    """The fence was asked for and this computer can't build it. Plain words."""


def agent_sandbox_wanted(setting: str, *, shared_project: bool) -> bool:
    """Per connection: "on", "off", or "" (on for projects with other members;
    optional, and recommended, when you work alone)."""
    if setting == "on":
        return True
    if setting == "off":
        return False
    return bool(shared_project)


def project_is_shared(project_root: str | os.PathLike) -> bool:
    """True when project.toml lists anyone besides the owner."""
    try:
        import tomllib
        with open(os.path.join(os.fspath(project_root), ".playground", "project.toml"), "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return False
    members = [m for m in data.get("members") or [] if isinstance(m, dict)]
    return any(str(m.get("role") or "") != "owner" for m in members)


def resolve_npx(argv: list[str], *, home: str | None = None, cache: str | None = None) -> tuple[list[str], str | None]:
    """``npx -y <package>`` → ``node <its entry point>`` when npx already has the
    package, so a fenced agent needs only that folder (read-only) instead of
    npm's whole cache (read/write). Returns (argv, package folder or None)."""
    if not argv or os.path.basename(argv[0]) != "npx":
        return list(argv), None
    rest = list(argv[1:])
    while rest and rest[0] in ("-y", "--yes", "--quiet", "-q"):
        rest.pop(0)
    if not rest or rest[0].startswith("-"):
        return list(argv), None
    spec, args = rest[0], rest[1:]
    name = spec.rsplit("@", 1)[0] if spec.count("@") > (1 if spec.startswith("@") else 0) else spec
    root = cache or os.path.join(home or os.path.expanduser("~"), ".npm", "_npx")
    best: tuple[float, str, str] | None = None
    try:
        entries = os.listdir(root)
    except OSError:
        return list(argv), None
    for entry in entries:
        folder = os.path.join(root, entry)
        try:
            with open(os.path.join(folder, "package.json"), encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, ValueError):
            continue
        if spec not in ((manifest.get("_npx") or {}).get("packages") or []):
            continue
        pkg_dir = os.path.join(folder, "node_modules", *name.split("/"))
        try:
            with open(os.path.join(pkg_dir, "package.json"), encoding="utf-8") as handle:
                pkg = json.load(handle)
        except (OSError, ValueError):
            continue
        bins = pkg.get("bin")
        if isinstance(bins, str):
            target = bins
        elif isinstance(bins, dict) and bins:
            target = bins.get(name.split("/")[-1]) or next(iter(bins.values()))
        else:
            continue
        script = os.path.realpath(os.path.join(pkg_dir, str(target)))
        if not os.path.isfile(script):
            continue
        stamp = os.path.getmtime(os.path.join(folder, "package.json"))
        if best is None or stamp > best[0]:
            best = (stamp, script, folder)
    if best is None:
        return list(argv), None
    node = shutil.which("node") or shutil.which("node", path=os.environ.get("PATH"))
    if not node:
        return list(argv), None
    return [node, best[1], *args], best[2]


@dataclass
class AgentConfinement:
    """How one agent program is fenced in. ``argv``/``env`` are what to launch."""
    argv: list[str]
    env: dict[str, str]
    backend: str                       # "bwrap" | "seatbelt" | "none"
    project_root: str
    writable: list[str] = field(default_factory=list)
    readable: list[str] = field(default_factory=list)
    tmp_dir: str = ""
    original_argv: list[str] = field(default_factory=list)
    _own_tmp: bool = False

    @property
    def confined(self) -> bool:
        return self.backend != "none"

    def describe(self) -> str:
        if not self.confined:
            return "The agent runs without a fence: it can reach the whole computer, like any program you start."
        return ("The agent runs fenced in: it can change only this project and its own sign-in folder, "
                "it can't see the rest of your home folder, and it can use the internet.")

    def cleanup(self) -> None:
        if self._own_tmp and self.tmp_dir:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)


def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def _code_roots() -> list[str]:
    """Playground's own code and the Python running it: the MCP server the agent
    starts needs to read them."""
    from ..core.paths import code_root
    roots = [str(code_root())]
    prefix = os.path.realpath(sys.prefix)
    roots.append(prefix)
    roots.append(os.path.dirname(os.path.realpath(sys.executable)))
    return roots


def confine_agent(argv: list[str], env: dict[str, str], *, project_root: str | os.PathLike, provider: str = "",
                  config_home: str = "", extra_writable: Iterable[str] = (), extra_readable: Iterable[str] = (),
                  home: str | None = None, backend: str = "auto", network: bool = True,
                  required: bool = True, state_dir: str | None = None) -> AgentConfinement:
    """Wrap an agent program so it can change only the project and its own
    sign-in folder. Raises SandboxUnavailable when ``required`` and there's no
    working backend on this computer."""
    root = os.path.realpath(os.fspath(project_root))
    home = os.path.realpath(home or os.path.expanduser("~"))
    policy = SandboxPolicy(root, network=network, backend=backend)
    which = globals()["backend"](policy)
    if which == "none":
        if required:
            raise SandboxUnavailable(
                "Playground can't fence in agents on this computer ("
                + ("bubblewrap isn't working" if sys.platform.startswith("linux") else "there's no sandbox it can use")
                + ").")
        return AgentConfinement(list(argv), dict(env), "none", root, original_argv=list(argv))

    launch, adapter = resolve_npx(list(argv), home=home)
    writable: list[str] = [root]
    if config_home:
        writable.append(os.path.realpath(config_home))
    else:
        writable += [os.path.join(home, p) for p in AGENT_HOMES.get(provider, ())]
    writable += [os.path.join(home, p) for p in AGENT_CACHES.get(provider, ())]
    writable += [os.path.realpath(p) for p in extra_writable if p]
    readable: list[str] = [os.path.join(home, p) for p in _HOME_TOOLCHAINS + _AGENT_READ_EXTRA]
    # The adapters and the Node.js that Playground set up itself (registry.install, runtime).
    from ..core import paths
    readable += [os.path.realpath(paths.data_dir() / "agents"), os.path.realpath(paths.data_dir() / "runtime")]
    if adapter:
        readable.append(os.path.realpath(adapter))
    elif launch and os.path.basename(launch[0]) == "npx":
        writable.append(os.path.join(home, ".npm"))   # npx has to fetch it; npm's cache must be writable
    for item in _code_roots() + [os.path.realpath(p) for p in extra_readable if p]:
        readable.append(item)
    for part in (env.get("PATH") or "").split(os.pathsep):
        if part and os.path.isabs(part) and _under(os.path.realpath(part), home):
            readable.append(os.path.realpath(part))
    exe = shutil.which(launch[0], path=env.get("PATH")) if launch else None
    if exe:
        readable.append(os.path.dirname(os.path.realpath(exe)))
    if sys.platform == "darwin" and provider in KEYCHAIN_PROVIDERS:
        writable.append(os.path.join(home, "Library", "Keychains"))
    # Only what's in the home folder needs naming; the rest of the system is readable anyway.
    readable = _dedupe([p for p in readable if _under(p, home)])
    writable = _dedupe(writable)
    for path in writable:
        name = os.path.basename(path)
        if path != root and _under(path, home) and not os.path.exists(path) and not name.endswith(".json") \
                and "Keychains" not in path:
            try:
                os.makedirs(path, mode=0o700, exist_ok=True)
            except OSError:
                pass

    out_env = dict(env)
    out_env["PLAYGROUND_AGENT_SANDBOX"] = which
    out_env["HOME"] = home
    conf = AgentConfinement([], out_env, which, root, writable=writable, readable=readable,
                            original_argv=list(argv))
    if which == "bwrap":
        conf.tmp_dir = "/tmp"
        out_env["TMPDIR"] = "/tmp"
        conf.argv = _bwrap_agent_argv(launch, root, home, writable, readable, network)
    else:
        base = state_dir or tempfile.gettempdir()
        os.makedirs(base, exist_ok=True)
        conf.tmp_dir = os.path.realpath(tempfile.mkdtemp(prefix="pg-agent-", dir=base))
        conf._own_tmp = True
        out_env["TMPDIR"] = conf.tmp_dir + os.sep
        conf.argv = _seatbelt_agent_argv(launch, root, home, writable, readable, conf.tmp_dir, network)
    _log(SandboxPolicy(root), f"agent backend={which} network={'allowed' if network else 'blocked'} "
                              f"cmd={shlex.join(argv)[:500]} writable={writable}")
    return conf


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _bwrap_agent_argv(argv: list[str], root: str, home: str, writable: list[str], readable: list[str],
                      network: bool) -> list[str]:
    out = [shutil.which("bwrap") or "bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
           "--tmpfs", "/tmp", "--tmpfs", "/var/tmp"]
    if home not in ("/", "") and not _under(home, root):
        out += ["--tmpfs", home]
    for path in readable:
        out += ["--ro-bind-try", path, path]
    for path in writable:
        if path != root:
            out += ["--bind-try", path, path]
    out += ["--bind", root, root]
    out += ["--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try"]
    if not network:
        out += ["--unshare-net"]
    out += ["--die-with-parent", "--new-session", "--setenv", "HOME", home, "--setenv", "TMPDIR", "/tmp",
            "--chdir", root, "--"]
    return out + list(argv)


_SEATBELT_AGENT = """(version 1)
(allow default)
(deny file-write*)
(allow file-write*
  (literal "/dev/null") (literal "/dev/zero") (literal "/dev/dtracehelper") (literal "/dev/tty")
  (regex #"^/dev/tty") (regex #"^/dev/fd/")
  (subpath (param "TMP")){writable})
(deny file-read-data file-read-xattr (subpath (param "HOME")))
(allow file-read-data file-read-xattr
  (subpath (param "TMP")){readable}{writable})
{network}"""


def _sb_path(path: str, home: str) -> str:
    quoted = _sb_quote(path)
    if path.endswith(".json") and _under(path, home):
        # A settings file the program rewrites atomically next to itself (".claude.json.tmp.123").
        return f'\n  (regex #"^{re.escape(path)}")'
    return f'\n  (subpath "{quoted}")'


def _seatbelt_agent_argv(argv: list[str], root: str, home: str, writable: list[str], readable: list[str],
                         tmp: str, network: bool) -> list[str]:
    profile = _SEATBELT_AGENT.format(
        writable="".join(_sb_path(p, home) for p in writable),
        readable="".join(_sb_path(p, home) for p in readable),
        network="" if network else "(deny network*)")
    return ["/usr/bin/sandbox-exec", "-p", profile, "-D", f"HOME={home}", "-D", f"TMP={tmp}", "--"] + list(argv)
