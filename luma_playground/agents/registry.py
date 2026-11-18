# SPDX-License-Identifier: Apache-2.0
"""The ACP agent registry: which agents exist and how to launch them.

The registry is one JSON file on a CDN. Playground keeps a copy for 24 hours
in the per-device cache, falls back to a stale copy when offline, and falls
back again to built-in launch commands for the agents Playground knows best
(Claude, Codex, Gemini, OpenCode, Goose). Offline mode
(``PLAYGROUND_OFFLINE=1`` or ``offline=True``) never touches the network,
which is what the tests use.

Each entry describes one of three distributions:

- ``npx``: ``{"package": "@scope/name@1.2.3", "args": [...], "env": {...}}``.
  Playground doesn't run these through ``npx``: it installs the package once
  with npm into its own folder and starts it with Node (Playground's own Node,
  see ``runtime``, when the computer has none), so nobody installs Node.js.
- ``uvx``: the same shape, launched with ``uvx``
- ``binary``: per-platform archives (``darwin-aarch64``, ``linux-x86_64``...)
  with ``archive``, ``cmd``, ``args`` and usually ``sha256``.

``launch_command`` turns an entry into an argv. For a binary it prefers a copy
already on the PATH, then one Playground installed earlier. ``install`` sets
up whatever an entry needs (``install_binary`` for archives, ``install_package``
for npm packages) into ``~/.local/share/playground/agents/<id>/<version>/``.
When a newer version is listed but not yet installed, the newest installed one
keeps working (``update_available``) while the new one installs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

REGISTRY_URL = "https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json"
CACHE_TTL_S = 24 * 3600
FETCH_TIMEOUT_S = 10.0

# Playground's own names for the agents it knows, and their registry ids.
PROVIDER_TO_REGISTRY = {
    "claude": "claude-acp",
    "codex": "codex-acp",
    "gemini": "gemini",
    "opencode": "opencode",
    "goose": "goose",
    "pi": "pi",
}

# Used when the registry can't be reached and nothing is cached, and to name
# the agents even when it can. Pinned, so the offline fallback is the same everywhere.
BUILTIN: dict[str, dict] = {
    "claude-acp": {
        "id": "claude-acp", "name": "Claude Agent", "provider": "claude",
        "description": "Claude, through the ACP adapter Anthropic co-authors",
        "version": "0.89.1",
        "distribution": {"npx": {"package": "@agentclientprotocol/claude-agent-acp@0.89.1"}},
    },
    "codex-acp": {
        "id": "codex-acp", "name": "Codex", "provider": "codex",
        "description": "Codex, through the ACP adapter OpenAI co-authors",
        "version": "2.2.2",
        "distribution": {"npx": {"package": "@agentclientprotocol/codex-acp@2.2.2"}},
    },
    "gemini": {
        "id": "gemini", "name": "Gemini CLI", "provider": "gemini",
        "description": "Google's Gemini CLI (API key accounts)", "version": "0.63.0",
        "distribution": {"npx": {"package": "@google/gemini-cli@0.63.0", "args": ["--acp"]}},
    },
    "opencode": {
        "id": "opencode", "name": "OpenCode", "provider": "opencode",
        "description": "OpenCode, the open source coding agent",
        "distribution": {"path": {"cmd": "opencode", "args": ["acp"]}},
    },
    "goose": {
        "id": "goose", "name": "goose", "provider": "goose",
        "description": "goose, the open source agent from Block",
        "distribution": {"path": {"cmd": "goose", "args": ["acp"]}},
    },
    "pi": {
        "id": "pi", "name": "Pi", "provider": "pi",
        "description": "a minimal, extensible agent harness from Earendil",
        "distribution": {"path": {"cmd": "pi"}},
    },
}


class RegistryError(RuntimeError):
    pass


def _xdg(var: str, default: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value and os.path.isabs(value) else Path.home() / default


def cache_path() -> Path:
    """Per-device cache: $PLAYGROUND_CACHE_DIR, else <data dir>/cache when
    PLAYGROUND_DATA_DIR is set (tests), else $XDG_CACHE_HOME/playground."""
    override = os.environ.get("PLAYGROUND_CACHE_DIR")
    if override:
        return Path(override).expanduser() / "acp-registry.json"
    if os.environ.get("PLAYGROUND_DATA_DIR"):
        from ..core import paths
        return paths.data_dir() / "cache" / "acp-registry.json"
    return _xdg("XDG_CACHE_HOME", ".cache") / "playground" / "acp-registry.json"


def agents_dir() -> Path:
    from ..core import paths
    return paths.data_dir() / "agents"


def platform_key() -> str:
    system = {"darwin": "darwin", "linux": "linux", "win32": "windows"}.get(sys.platform, sys.platform)
    machine = platform.machine().lower()
    arch = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}.get(machine, machine)
    return f"{system}-{arch}"


@dataclass
class AgentEntry:
    id: str
    name: str
    version: str = ""
    description: str = ""
    authors: list[str] = field(default_factory=list)
    license: str = ""
    repository: str = ""
    icon: str = ""
    distribution: dict = field(default_factory=dict)
    builtin: bool = False

    @classmethod
    def from_json(cls, data: dict, builtin: bool = False) -> "AgentEntry":
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or data.get("id") or ""),
            version=str(data.get("version") or ""),
            description=str(data.get("description") or ""),
            authors=[str(a) for a in data.get("authors") or []],
            license=str(data.get("license") or ""),
            repository=str(data.get("repository") or ""),
            icon=str(data.get("icon") or ""),
            distribution=dict(data.get("distribution") or {}),
            builtin=builtin,
        )

    @property
    def kinds(self) -> list[str]:
        return [k for k in ("npx", "uvx", "binary", "path") if k in self.distribution]


@dataclass
class LaunchCommand:
    argv: list[str]
    env: dict[str, str]
    kind: str                       # "npm" | "uvx" | "binary" | "path"
    needs_install: bool = False     # Playground can set it up itself (``install``)
    archive_url: str = ""
    sha256: str = ""
    install_dir: str = ""
    missing: str = ""               # a program the person has to install themselves ("uvx", "goose"...)
    package: str = ""               # npm: "name@version"
    path_prepend: str = ""          # put first on PATH when starting it (Playground's own Node)
    update_available: bool = False  # running an older installed version; ``install`` brings the listed one


@dataclass
class Registry:
    agents: dict[str, AgentEntry]
    source: str                     # "network" | "cache" | "stale-cache" | "builtin"
    fetched_at: float = 0.0
    version: str = ""

    def get(self, agent_id: str) -> AgentEntry | None:
        return self.agents.get(agent_id)

    def for_provider(self, provider: str) -> AgentEntry | None:
        agent_id = PROVIDER_TO_REGISTRY.get(provider, provider)
        return self.agents.get(agent_id) or (AgentEntry.from_json(BUILTIN[agent_id], True)
                                             if agent_id in BUILTIN else None)

    def list(self) -> list[AgentEntry]:
        return sorted(self.agents.values(), key=lambda a: a.name.lower())


def parse(document: dict | list) -> dict[str, AgentEntry]:
    """Parse a registry document. Bad entries are skipped, not fatal."""
    items = document.get("agents") if isinstance(document, dict) else document
    if not isinstance(items, list):
        raise RegistryError("The agent list isn't in a form Playground understands.")
    out: dict[str, AgentEntry] = {}
    for item in items:
        if not isinstance(item, dict) or not item.get("id") or not isinstance(item.get("distribution"), dict):
            continue
        entry = AgentEntry.from_json(item)
        if entry.kinds:
            out[entry.id] = entry
    return out


def builtin_registry() -> Registry:
    return Registry({k: AgentEntry.from_json(v, True) for k, v in BUILTIN.items()}, "builtin")


_fetch_lock = threading.Lock()


def offline_default() -> bool:
    return os.environ.get("PLAYGROUND_OFFLINE", "") not in ("", "0", "false", "no")


def load(*, offline: bool | None = None, force: bool = False, url: str = REGISTRY_URL,
         cache: Path | None = None, timeout: float = FETCH_TIMEOUT_S) -> Registry:
    """The registry, from cache when fresh, the network when not, or built in.

    Built-in entries are always merged in, so Claude and Codex are there even
    when the network and the cache both fail.
    """
    offline = offline_default() if offline is None else offline
    cache = cache or cache_path()
    with _fetch_lock:
        cached = _read_cache(cache)
        fresh = cached is not None and time.time() - cached[0] < CACHE_TTL_S
        if cached is not None and (fresh and not force or offline):
            return _merge(parse(cached[1]), "cache" if fresh else "stale-cache", cached[0], cached[1])
        if not offline:
            try:
                document = _fetch(url, timeout)
                agents = parse(document)
                _write_cache(cache, document)
                return _merge(agents, "network", time.time(), document)
            except (OSError, ValueError, RegistryError, urllib.error.URLError):
                pass
        if cached is not None:
            return _merge(parse(cached[1]), "stale-cache", cached[0], cached[1])
        return builtin_registry()


def _merge(agents: dict[str, AgentEntry], source: str, fetched_at: float, document: dict) -> Registry:
    merged = {k: AgentEntry.from_json(v, True) for k, v in BUILTIN.items()}
    merged.update(agents)
    version = str(document.get("version") or "") if isinstance(document, dict) else ""
    return Registry(merged, source, fetched_at, version)


def _fetch(url: str, timeout: float) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "Luma-Playground", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URL
        if response.status != 200:
            raise RegistryError(f"HTTP {response.status}")
        body = response.read(8 * 1024 * 1024 + 1)
    if len(body) > 8 * 1024 * 1024:
        raise RegistryError("registry too large")
    return json.loads(body.decode("utf-8"))


def _read_cache(path: Path) -> tuple[float, dict] | None:
    try:
        with open(path, encoding="utf-8") as handle:
            wrapper = json.load(handle)
        return float(wrapper["fetched_at"]), wrapper["document"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_cache(path: Path, document: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".registry-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"fetched_at": time.time(), "document": document}, handle)
        os.replace(tmp, path)
    except OSError:
        pass


# ── Launch commands ────────────────────────────────────────────────────────

def launch_command(entry: AgentEntry, *, plat: str | None = None, path_env: str | None = None,
                   prefer_path: bool = True) -> LaunchCommand:
    """How to start this agent on this computer.

    Raises RegistryError when the entry has no distribution for this platform.
    """
    dist = entry.distribution
    path_env = path_env if path_env is not None else os.environ.get("PATH", "")
    if "npx" in dist:
        spec = dist["npx"] or {}
        name, version = split_package(str(spec.get("package") or ""))
        if not name:
            raise RegistryError(f"{entry.name} has no package to run.")
        version = version or entry.version
        args = [str(a) for a in spec.get("args") or []]
        package = f"{name}@{version}" if version else name
        install_dir = agents_dir() / _safe_name(entry.id) / _safe_name(version or "latest")
        script = npm_bin(install_dir, name)
        older = None if script else _newest_installed(entry.id, name, exclude=install_dir)
        from . import runtime
        node = runtime.find_node(path_env)
        if node is not None and (script or older):
            run_dir, run_script = (install_dir, script) if script else older
            return LaunchCommand([node.node, str(run_script), *args], _env(spec), "npm", package=package,
                                 install_dir=str(run_dir), path_prepend=node.bin_dir,
                                 update_available=script is None)
        return LaunchCommand(["node", f"{package}", *args], _env(spec), "npm", needs_install=True,
                             package=package, install_dir=str(install_dir))
    if "uvx" in dist:
        spec = dist["uvx"] or {}
        package = str(spec.get("package") or "")
        if not package:
            raise RegistryError(f"{entry.name} has no package to run.")
        argv = ["uvx", package, *[str(a) for a in spec.get("args") or []]]
        missing = "" if shutil.which("uvx", path=path_env) else "uvx"
        return LaunchCommand(argv, _env(spec), "uvx", missing=missing)
    if "path" in dist:
        spec = dist["path"] or {}
        cmd = str(spec.get("cmd") or "")
        found = shutil.which(cmd, path=path_env)
        return LaunchCommand([found or cmd, *[str(a) for a in spec.get("args") or []]], _env(spec), "path",
                             missing="" if found else cmd)
    if "binary" in dist:
        plat = plat or platform_key()
        targets = dist["binary"] or {}
        spec = targets.get(plat)
        if not isinstance(spec, dict):
            raise RegistryError(f"{entry.name} isn't available for this kind of computer yet.")
        cmd = str(spec.get("cmd") or "")
        args = [str(a) for a in spec.get("args") or []]
        if not cmd:
            raise RegistryError(f"{entry.name} has no program to run.")
        program = Path(cmd.replace("\\", "/")).name
        if prefer_path:
            found = shutil.which(program, path=path_env)
            if found:
                return LaunchCommand([found, *args], _env(spec), "binary")
        install_dir = agents_dir() / _safe_name(entry.id) / _safe_name(entry.version or "current")
        target = _safe_join(install_dir, cmd)
        installed = target.is_file() and os.access(target, os.X_OK)
        return LaunchCommand([str(target), *args], _env(spec), "binary", needs_install=not installed,
                             archive_url=str(spec.get("archive") or ""), sha256=str(spec.get("sha256") or ""),
                             install_dir=str(install_dir))
    raise RegistryError(f"Playground doesn't know how to start {entry.name}.")


def split_package(spec: str) -> tuple[str, str]:
    """"@scope/name@1.2.3" -> ("@scope/name", "1.2.3"); no version -> ("@scope/name", "")."""
    spec = spec.strip()
    at = spec.rfind("@")
    if at > 0:
        return spec[:at], spec[at + 1:]
    return spec, ""


def npm_bin(install_dir: Path, name: str) -> Path | None:
    """The script an installed npm package runs, from its package.json ``bin``."""
    package_dir = install_dir / "node_modules" / Path(*name.split("/"))
    try:
        meta = json.loads((package_dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    bins = meta.get("bin")
    if isinstance(bins, dict):
        short = name.rsplit("/", 1)[-1]
        rel = bins.get(short) or (next(iter(bins.values())) if len(bins) == 1 else None)
    else:
        rel = bins
    if not isinstance(rel, str):
        return None
    script = (package_dir / rel).resolve()
    if not str(script).startswith(str(package_dir.resolve()) + os.sep) or not script.is_file():
        return None
    return script


def _newest_installed(agent_id: str, name: str, *, exclude: Path) -> tuple[Path, Path] | None:
    base = agents_dir() / _safe_name(agent_id)
    found = []
    for child in base.glob("*"):
        if child == exclude or child.name.startswith("."):
            continue
        script = npm_bin(child, name)
        if script is not None:
            found.append((child.stat().st_mtime, child, script))
    if not found:
        return None
    _mtime, child, script = max(found)
    return child, script


def _env(spec: dict) -> dict[str, str]:
    env = spec.get("env") or {}
    return {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}


def _safe_name(value: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-._" else "-" for c in value).strip(".")
    return cleaned or "x"


def _safe_join(base: Path, relative: str) -> Path:
    rel = Path(relative.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts:
        raise RegistryError("The agent's program path isn't safe.")
    return base / rel


def install_binary(entry: AgentEntry, *, plat: str | None = None, timeout: float = 120.0,
                   require_checksum: bool = False, opener=urllib.request.urlopen) -> LaunchCommand:
    """Download and unpack a binary agent, checking its sha256 when given.

    Unpacking refuses absolute paths, ``..`` and links that leave the folder.
    """
    command = launch_command(entry, plat=plat, prefer_path=False)
    if command.kind != "binary":
        return command
    if not command.needs_install:
        return command
    if not command.archive_url.startswith(("https://", "file://")):
        raise RegistryError("The download address for this agent isn't secure.")
    if require_checksum and not command.sha256:
        raise RegistryError("This agent's download has no checksum, so Playground won't install it.")
    install_dir = Path(command.install_dir)
    install_dir.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".install-", dir=str(install_dir.parent)))
    try:
        archive = work / "download"
        digest = hashlib.sha256()
        with opener(urllib.request.Request(command.archive_url, headers={"User-Agent": "Luma-Playground"}),
                    timeout=timeout) as response, open(archive, "wb") as out:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
                out.write(chunk)
        if command.sha256 and digest.hexdigest().lower() != command.sha256.lower():
            raise RegistryError("The download didn't match its checksum, so Playground threw it away.")
        unpacked = work / "unpacked"
        unpacked.mkdir()
        _extract(archive, command.archive_url, unpacked)
        if install_dir.exists():
            shutil.rmtree(install_dir)
        os.replace(unpacked, install_dir)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    final = launch_command(entry, plat=plat, prefer_path=False)
    if final.needs_install:
        raise RegistryError(f"{entry.name} was downloaded but its program wasn't where expected.")
    return final


NPM_TIMEOUT_S = 900.0
_package_lock = threading.Lock()


def install_package(entry: AgentEntry, *, progress=None, timeout: float = NPM_TIMEOUT_S,
                    runner=subprocess.run) -> LaunchCommand:
    """Install an npm agent into Playground's own folder with npm, using the
    computer's Node or Playground's own (downloaded first when needed).

    The install goes into a temporary folder and is moved into place only when
    it works, so a failed or interrupted one never leaves a broken agent.
    Older versions of the same agent are removed afterwards.
    """
    from . import runtime
    command = launch_command(entry)
    if command.kind != "npm":
        return command
    if not command.needs_install and not command.update_available:
        return command
    try:
        node = runtime.ensure_node(progress=(lambda words, part: progress(f"{words} Node.js", part))
                                   if progress else None)
    except runtime.SetupError as error:
        raise RegistryError(str(error)) from error
    install_dir = agents_dir() / _safe_name(entry.id) / _safe_name(split_package(command.package)[1] or "latest")
    install_dir.parent.mkdir(parents=True, exist_ok=True)
    with _package_lock:
        if npm_bin(install_dir, split_package(command.package)[0]) is None:
            if progress:
                progress(f"Installing {entry.name}", None)
            work = Path(tempfile.mkdtemp(prefix=".install-", dir=str(install_dir.parent)))
            try:
                _npm_install(node, command.package, work, timeout, runner)
                if npm_bin(work, split_package(command.package)[0]) is None:
                    raise RegistryError(f"{entry.name} installed, but its program wasn’t where expected.")
                if install_dir.exists():
                    shutil.rmtree(install_dir)
                os.replace(work, install_dir)
            finally:
                shutil.rmtree(work, ignore_errors=True)
        for child in install_dir.parent.iterdir():
            if child != install_dir and not child.name.startswith("."):
                shutil.rmtree(child, ignore_errors=True)
    final = launch_command(entry)
    if final.needs_install:
        raise RegistryError(f"{entry.name} was installed but won’t start on this computer.")
    return final


def _npm_install(node, package: str, prefix: Path, timeout: float, runner) -> None:
    from ..core import paths
    npm_home = paths.data_dir() / "runtime" / "npm"
    npm_home.mkdir(parents=True, exist_ok=True)
    user_rc, global_rc = npm_home / "npmrc", npm_home / "npmrc-global"   # npm refuses one file for both
    for rc in (user_rc, global_rc):
        if not rc.exists():
            rc.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = node.bin_dir + os.pathsep + env.get("PATH", "")
    # The person's own npm settings (a private registry, a global prefix) must not change what installs here.
    env.update({"npm_config_userconfig": str(user_rc), "npm_config_globalconfig": str(global_rc),
                "npm_config_cache": str(npm_home / "cache"), "npm_config_update_notifier": "false",
                "npm_config_fund": "false", "npm_config_audit": "false", "npm_config_loglevel": "error",
                "npm_config_registry": "https://registry.npmjs.org/"})
    argv = [node.node, node.npm_cli, "install", "--prefix", str(prefix), "--omit=dev", "--no-package-lock",
            "--ignore-scripts", package]
    try:
        result = runner(argv, capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as error:
        raise RegistryError("Setting up took too long. Check the internet connection and try again.") from error
    except OSError as error:
        raise RegistryError(f"Node.js wouldn’t start ({error}).") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        offline = any(word in (result.stderr or "") for word in ("ENOTFOUND", "EAI_AGAIN", "ETIMEDOUT", "ECONNRE"))
        raise RegistryError("Playground couldn’t download what it needs. Check the internet connection and try "
                            "again." if offline else
                            f"Setting up didn’t work: {detail[-1][:200] if detail else 'npm stopped'}")


def install(entry: AgentEntry, *, progress=None) -> LaunchCommand:
    """Set up whatever this agent needs on this computer. Raises RegistryError in plain words."""
    command = launch_command(entry, prefer_path=False)
    if command.kind == "npm":
        return install_package(entry, progress=progress)
    if command.kind == "binary":
        if progress:
            progress(f"Downloading {entry.name}", None)
        return install_binary(entry, require_checksum=True)
    if command.missing:
        raise RegistryError(f"{entry.name} needs {command.missing}, which Playground can’t install for you.")
    return command


def can_install(entry: AgentEntry) -> bool:
    """Whether Playground can set this agent up by itself on this computer."""
    try:
        command = launch_command(entry, prefer_path=False)
    except RegistryError:
        return False
    if command.kind == "npm":
        from . import runtime
        return platform_key() in runtime.NODE_BUILDS or runtime.find_node() is not None
    return command.kind == "binary" and bool(command.sha256)


def _extract(archive: Path, url: str, dest: Path) -> None:
    dest_real = os.path.realpath(dest)

    def check(name: str) -> Path:
        target = Path(os.path.realpath(dest / name))
        if str(target) != dest_real and not str(target).startswith(dest_real + os.sep):
            raise RegistryError("The download tried to put files outside its folder.")
        return target

    lower = url.lower()
    if lower.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                check(info.filename)
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise RegistryError("The download contains a link, so Playground won't unpack it.")
            zf.extractall(dest)
            for info in zf.infolist():
                perms = (info.external_attr >> 16) & 0o777
                if perms and not info.is_dir():
                    os.chmod(dest / info.filename, perms)
        return
    with tarfile.open(archive, "r:*") as tf:
        members = tf.getmembers()
        for member in members:
            check(member.name)
            if member.islnk() or member.issym():
                check(os.path.join(os.path.dirname(member.name), member.linkname))
            if member.isdev():
                raise RegistryError("The download contains a device file, so Playground won't unpack it.")
        try:
            tf.extractall(dest, filter="data")  # Python 3.12+
        except TypeError:
            tf.extractall(dest)  # noqa: S202 - members were checked above
