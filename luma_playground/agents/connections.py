# SPDX-License-Identifier: Apache-2.0
"""Connections: the agents and accounts a person has added on this computer.

A connection says which agent program runs, whose account it uses, who may
use it and where it runs. Connections are per device, in
``~/.config/playground/connections.toml``, never in a project, and they never
hold a secret: an API key stays in the keyring and only its keyring name is
written here.

The picker rules are enforced in ``validate`` and
``check_may_run``, not just in the UI:

- A personal plan (Claude Pro/Max, ChatGPT Plus/Pro, and Ari on "Sign in with
  ChatGPT") is just for its owner. ``sharing = "team"`` is refused. A teammate's
  prompt never runs on it, whatever machine it runs on.
- Instead it can "work on team tasks": it picks up tasks given to its owner,
  on the owner's account, up to a capped share of the owner's week.
- A personal plan may run on a server only as the owner's private seat.
- API keys, team keys, local models and Ari credits can be shared with the
  team; money-spending ones need a spend cap.
- Switching account is always the person's choice. Nothing here picks another
  connection when one runs out.

Per-account homes: the first Claude (or Codex) personal plan uses the
program's normal sign-in folder, so an existing ``claude`` login just works.
Every other one gets its own sealed folder under
``~/.local/share/playground/accounts/<id>/``, passed as ``CLAUDE_CONFIG_DIR``
or ``CODEX_HOME``. That is how two Claude accounts live side by side.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields as dc_fields
import glob
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import urllib.request
from typing import Any, Callable

from ..core import paths
from ..core.store import atomic_write_text, dump_toml

KINDS = ("personal-plan", "api-key", "local", "ari", "team-key")
SHARING = ("just-me", "team")
THIS_COMPUTER = "this-computer"
ARI_ENDPOINTS = ("ollama", "openai-compatible", "chatgpt", "luma-credits")

TEAM_TASK_CAP_DEFAULT = 20   # percent of the owner's week
TEAM_TASK_CAP_MIN = 1
TEAM_TASK_CAP_MAX = 50

# Environment variables that name the account folder for each program.
HOME_VARS = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "opencode": "OPENCODE_DATA_DIR"}
# Environment variables an API-key connection fills from the keyring.
KEY_VARS = {
    "claude": ("ANTHROPIC_API_KEY",),
    "codex": ("CODEX_API_KEY", "OPENAI_API_KEY"),
    "gemini": ("GEMINI_API_KEY",),
    "opencode": ("OPENCODE_API_KEY",),
    "goose": (),
}
# Keys that must not leak into a personal-plan launch, or the program bills
# them instead of the plan.
_PLAN_STRIP = {
    "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"),
    "codex": ("OPENAI_API_KEY", "CODEX_API_KEY"),
    "gemini": (),
}
# Variables set when Playground itself runs under another agent host (a
# developer running it from Claude Code). They'd make the child think it is
# part of that host's session.
_HOST_SESSION = re.compile(
    r"^(CLAUDECODE|CLAUDE_PID|CLAUDE_EFFORT|CLAUDE_AGENT_SDK_.*|CLAUDE_PREVIEW_.*|"
    r"CLAUDE_CODE_(ENTRYPOINT|SESSION.*|CHILD_SESSION|HOST_.*|MESSAGING_.*|SDK_.*|DESKTOP_.*|EXECPATH|"
    r"OAUTH_SCOPES|ACCOUNT_UUID|ORGANIZATION_UUID|USER_EMAIL|SSE_PORT|EAGER_FLUSH|REPORT_FINDINGS|"
    r"EMIT_TOOL_USE_SUMMARIES|TERMINAL_MCP_TOOLS|ENABLE_SDK_FILE_CHECKPOINTING|SIMPLE|DISABLE_CRON|"
    r"ENABLE_ASK_USER_QUESTION_TOOL|DISABLE_TERMINAL_TITLE))$")


class ConnectionRuleError(ValueError):
    """A connection setting that breaks a picker rule. The message is for people."""


class LaunchError(RuntimeError):
    """The connection can't be started. The message is for people."""


@dataclass
class Connection:
    id: str
    label: str
    kind: str
    provider: str
    owner: str = ""
    sharing: str = "just-me"
    where: str = THIS_COMPUTER
    private_seat: bool = False          # a personal plan on a server: the owner's own seat
    work_on_team_tasks: bool = False    # personal plans only
    team_task_cap: int = TEAM_TASK_CAP_DEFAULT
    spend_cap: str = ""                 # shared money-spending kinds: e.g. "$50 a month"
    config_home: str = ""               # "" = the program's normal sign-in folder
    agent: str = ""                     # a registry id, when not the provider's usual agent
    command: list[str] = field(default_factory=list)  # a custom launch command, rarely needed
    model: str = ""
    base_url: str = ""                  # Ari and local models
    endpoint: str = ""                  # Ari: one of ARI_ENDPOINTS
    key_ref: str = ""                   # the keyring item's name; never the key
    key_env: str = ""                   # or an environment variable that holds the key
    sandbox: str = ""                   # fence the agent program in: "on", "off", "" = on when the project is shared
    created: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    # ── what kind of thing it is ──

    @property
    def is_personal(self) -> bool:
        """A consumer plan that belongs to one person."""
        return self.kind == "personal-plan" or (self.kind == "ari" and self.endpoint == "chatgpt")

    @property
    def is_acp(self) -> bool:
        return self.kind != "ari" and self.provider not in ("ari", "ollama")

    @property
    def spends_money(self) -> bool:
        if self.kind in ("api-key", "team-key"):
            return True
        return self.kind == "ari" and self.endpoint in ("openai-compatible", "luma-credits")

    @property
    def uses_default_home(self) -> bool:
        return not self.config_home

    def account_key(self) -> tuple[str, str]:
        """Two connections with the same key are the same account of the same program."""
        return (self.provider, os.path.realpath(self.config_home) if self.config_home else "default")

    def run_label(self, owner_name: str | None = None) -> str:
        """'Runs on Priya's Claude', 'Runs on the team key': shown on every run."""
        who = owner_name or self.owner
        if self.kind == "team-key":
            return "Runs on the team key"
        if self.kind == "local":
            return f"Runs on {possessive(who)} local model" if who else "Runs on a local model"
        name = PROVIDER_NAMES.get(self.provider, self.provider.capitalize())
        return f"Runs on {possessive(who)} {name}" if who else f"Runs on {name}"

    # ── rules ──

    def validate(self) -> None:
        if not paths.is_valid_id(self.id):
            raise ConnectionRuleError("A connection's id can use only small letters, digits and dashes.")
        if self.kind not in KINDS:
            raise ConnectionRuleError(f"Playground doesn't know the kind {self.kind!r}.")
        if not self.provider:
            raise ConnectionRuleError("Say which agent this connection is for.")
        if self.sharing not in SHARING:
            raise ConnectionRuleError("Sharing can be 'just me' or 'team'.")
        if not self.label.strip():
            raise ConnectionRuleError("Give the connection a name.")
        if self.where != THIS_COMPUTER and not paths.is_valid_id(self.where):
            raise ConnectionRuleError("Where it runs must be this computer or one of the project's places.")
        if self.kind == "ari" and self.endpoint and self.endpoint not in ARI_ENDPOINTS:
            raise ConnectionRuleError(f"Ari can't use {self.endpoint!r} yet.")
        if self.is_personal:
            if self.sharing == "team":
                raise ConnectionRuleError(
                    "A personal plan is just for you. The provider's rules don't allow sharing it, "
                    "so turn on 'Work on team tasks' instead, or share an API key.")
            if self.where != THIS_COMPUTER and not self.private_seat:
                raise ConnectionRuleError(
                    "A personal plan can run on a server only as your private seat, doing only your tasks.")
        else:
            if self.private_seat:
                raise ConnectionRuleError("Only a personal plan has a private seat.")
        if self.work_on_team_tasks:
            if not self.is_personal:
                raise ConnectionRuleError("'Work on team tasks' is for personal plans. Share this one instead.")
            if not isinstance(self.team_task_cap, int) or isinstance(self.team_task_cap, bool) or not (
                    TEAM_TASK_CAP_MIN <= self.team_task_cap <= TEAM_TASK_CAP_MAX):
                raise ConnectionRuleError(
                    f"Team tasks need a cap between {TEAM_TASK_CAP_MIN}% and {TEAM_TASK_CAP_MAX}% of your week.")
        if self.sharing == "team" and self.spends_money and not self.spend_cap.strip():
            raise ConnectionRuleError("Set a spending cap before sharing this with the team.")
        if self.kind == "team-key" and self.sharing != "team":
            raise ConnectionRuleError("A team key is shared with the team by definition.")
        if self.config_home and not os.path.isabs(self.config_home):
            raise ConnectionRuleError("The account folder must be a full path.")
        if self.sandbox not in ("", "on", "off"):
            raise ConnectionRuleError("Keeping the agent inside the project is on, off, or decided by the project.")

    # ── the fence ──

    def sandbox_for(self, shared_project: bool) -> bool:
        """Whether this connection's agent program runs fenced in for a project.
        On by default for shared projects; optional, and recommended, when you work alone."""
        if not self.is_acp:
            return False   # Ari runs inside Playground; its commands are always fenced in
        from .sandbox import agent_sandbox_wanted
        return agent_sandbox_wanted(self.sandbox, shared_project=shared_project)

    @property
    def sandbox_recommended(self) -> bool:
        """The picker shows "Keep it inside the project (recommended)" when it's not forced on."""
        return self.is_acp and self.sandbox != "on"

    # ── storage ──

    def to_dict(self) -> dict:
        data: dict[str, Any] = {}
        for f in dc_fields(self):
            if f.name == "extra":
                continue
            value = getattr(self, f.name)
            if value in ("", [], None) and f.name not in ("id", "label", "kind", "provider"):
                continue
            if f.name == "work_on_team_tasks" and not value:
                continue
            if f.name == "private_seat" and not value:
                continue
            if f.name == "team_task_cap" and not self.work_on_team_tasks:
                continue
            data[f.name] = value
        data.update({k: v for k, v in self.extra.items() if k not in data})
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Connection":
        known = {f.name for f in dc_fields(cls)} - {"extra"}
        kwargs = {k: data[k] for k in known if k in data}
        if "command" in kwargs:
            kwargs["command"] = [str(c) for c in kwargs["command"] or []]
        if "team_task_cap" in kwargs:
            try:
                kwargs["team_task_cap"] = int(kwargs["team_task_cap"])
            except (TypeError, ValueError):
                kwargs["team_task_cap"] = TEAM_TASK_CAP_DEFAULT
        for flag in ("private_seat", "work_on_team_tasks"):
            if flag in kwargs:
                kwargs[flag] = bool(kwargs[flag])
        for key in ("id", "label", "kind", "provider"):
            kwargs[key] = str(kwargs.get(key) or "")
        conn = cls(**kwargs)
        conn.extra = {k: v for k, v in data.items() if k not in known}
        return conn


PROVIDER_NAMES = {"claude": "Claude", "codex": "Codex", "gemini": "Gemini", "opencode": "OpenCode",
                  "goose": "goose", "pi": "Pi", "ollama": "local model", "ari": "Ari"}


def possessive(name: str) -> str:
    if not name:
        return ""
    if name.lower() in ("me", "you", "yours"):
        return "your"
    return f"{name}'" if name.endswith("s") else f"{name}'s"


def check_may_run(connection: Connection, person: str, *, goal_owner: str = "") -> None:
    """Raise ConnectionRuleError unless ``person`` may run a turn on ``connection``.

    ``person`` is whoever is prompting. ``goal_owner`` is who the goal is given
    to: a personal plan works only on its owner's goals, even with "work on
    team tasks" (taking the task makes it the owner's first).
    """
    owner = connection.owner
    if connection.is_personal:
        if owner and person and person != owner:
            raise ConnectionRuleError(
                f"This runs on {possessive(owner)} own plan, so only they can use it. "
                "Give the task to them, or pick a connection that's shared with the team.")
        if owner and goal_owner and goal_owner != owner:
            raise ConnectionRuleError(
                f"This goal is {possessive(goal_owner)}. A personal plan works only on its owner's goals, "
                "so take the goal first or use a shared connection.")
        return
    if connection.sharing == "just-me" and owner and person and person != owner:
        raise ConnectionRuleError(f"{possessive(owner).capitalize()} connection isn't shared with the team.")


# ── The connections file ───────────────────────────────────────────────────

def connections_path() -> Path:
    return paths.config_dir() / "connections.toml"


class ConnectionStore:
    """``connections.toml`` on this computer. Thread-safe within the app."""

    HEADER = ("# Playground connections on this computer. Never secrets: API keys stay in the keyring.\n"
              "# Edit in Playground (Usage, or the agent picker).\n")

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else connections_path()
        self._lock = threading.RLock()

    def list(self) -> list[Connection]:
        with self._lock:
            try:
                text = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return []
            try:
                document = tomllib.loads(text)
            except tomllib.TOMLDecodeError as error:
                raise ConnectionRuleError(f"The connections file can't be read: {error}") from error
            out = []
            for item in document.get("connections") or []:
                if isinstance(item, dict) and item.get("id"):
                    out.append(Connection.from_dict(item))
            return out

    def get(self, connection_id: str) -> Connection | None:
        return next((c for c in self.list() if c.id == connection_id), None)

    def _write(self, items: list[Connection]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = dump_toml({"connections": [c.to_dict() for c in items]}) if items else ""
        atomic_write_text(self.path, self.HEADER + body, mode=0o600)
        from ..core import events
        events.bus.publish(events.CONNECTIONS_CHANGED)

    def add(self, connection: Connection) -> Connection:
        """Validate, give it an account folder if it needs one, and save."""
        with self._lock:
            items = self.list()
            if any(c.id == connection.id for c in items):
                raise ConnectionRuleError("There's already a connection with that name.")
            if not connection.created:
                from ..core.model import now_iso
                connection.created = now_iso()
            assign_config_home(connection, items)
            connection.validate()
            items.append(connection)
            self._write(items)
            return connection

    def update(self, connection: Connection) -> Connection:
        with self._lock:
            connection.validate()
            items = self.list()
            for i, existing in enumerate(items):
                if existing.id == connection.id:
                    if existing.config_home != connection.config_home and existing.config_home:
                        raise ConnectionRuleError("A connection's account folder can't be moved.")
                    items[i] = connection
                    self._write(items)
                    return connection
            raise ConnectionRuleError("That connection isn't on this computer.")

    def remove(self, connection_id: str) -> None:
        """Forget the connection. Its account folder is left alone: signing out
        is the agent program's job, and the folder may hold its history."""
        with self._lock:
            items = [c for c in self.list() if c.id != connection_id]
            self._write(items)


def assign_config_home(connection: Connection, existing: list[Connection]) -> None:
    """The first personal plan of a program keeps its normal sign-in folder;
    every other account of that program gets a sealed folder of its own."""
    if connection.config_home or connection.provider not in HOME_VARS:
        return
    if connection.kind == "personal-plan":
        taken = any(c.provider == connection.provider and c.uses_default_home for c in existing)
        if not taken:
            return
    connection.config_home = str(paths.account_home(connection.id))


def ensure_config_home(connection: Connection) -> str | None:
    if not connection.config_home:
        return None
    home = Path(connection.config_home)
    home.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(home, 0o700)
    except OSError:
        pass
    return str(home)


# ── Launching ──────────────────────────────────────────────────────────────

def base_env(source: dict[str, str] | None = None) -> dict[str, str]:
    """This process's environment without another agent host's session variables."""
    env = dict(os.environ if source is None else source)
    for key in list(env):
        if _HOST_SESSION.match(key):
            del env[key]
    return env


def launch_spec(connection: Connection, registry=None, *, env: dict[str, str] | None = None,
                key_lookup: Callable[[Connection], str | None] | None = None,
                install: bool | None = None) -> tuple[list[str], dict[str, str]]:
    """The argv and environment that start this connection's ACP agent.

    An agent Playground can set up itself (an npm adapter, a checked download)
    is set up here when it's missing, so this can take a while the first time:
    call it off the UI thread. ``install=False`` (or offline mode) never
    downloads. A newer listed version installs in the background while the
    installed one runs.

    Raises LaunchError with a plain reason when it can't be started here.
    """
    if not connection.is_acp:
        raise LaunchError("Ari runs inside Playground; it has no program to start.")
    connection.validate()
    out_env = base_env(env)
    if connection.command:
        argv = list(connection.command)
    else:
        from . import registry as registry_mod
        reg = registry if registry is not None else registry_mod.load()
        entry = reg.get(connection.agent) if connection.agent else reg.for_provider(connection.provider)
        if entry is None:
            raise LaunchError(f"Playground doesn't know how to start {connection.label}.")
        try:
            launch = registry_mod.launch_command(entry, path_env=out_env.get("PATH"))
        except registry_mod.RegistryError as error:
            raise LaunchError(str(error)) from error
        if install is None:
            install = not registry_mod.offline_default()
        if launch.needs_install:
            if not install:
                raise LaunchError(f"{entry.name} isn't set up on this computer yet.")
            try:
                launch = registry_mod.install(entry)
            except registry_mod.RegistryError as error:
                raise LaunchError(str(error)) from error
        elif launch.update_available and install:
            _update_in_background(registry_mod, entry)
        if launch.missing:
            raise LaunchError(f"{entry.name} needs {launch.missing}, which isn't on this computer yet.")
        argv = launch.argv
        out_env.update(launch.env)
        if launch.path_prepend:
            out_env["PATH"] = launch.path_prepend + os.pathsep + out_env.get("PATH", "")
    home_var = HOME_VARS.get(connection.provider)
    if home_var:
        out_env.pop(home_var, None)
        home = ensure_config_home(connection)
        if home:
            out_env[home_var] = home
            if connection.provider == "claude":
                # A long-lived token for the default account would override this folder's sign-in.
                out_env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    if connection.is_personal:
        for key in _PLAN_STRIP.get(connection.provider, ()):
            out_env.pop(key, None)
    elif connection.kind in ("api-key", "team-key"):
        key = (key_lookup or lookup_api_key)(connection)
        if not key:
            raise LaunchError(f"The API key for {connection.label} isn't in the keyring.")
        for var in KEY_VARS.get(connection.provider, ()):
            out_env[var] = key
    return argv, out_env


_updating: set[str] = set()
_updating_lock = threading.Lock()


def _update_in_background(registry_mod, entry) -> None:
    """Install the newer version for next time; the installed one keeps running now."""
    with _updating_lock:
        if entry.id in _updating:
            return
        _updating.add(entry.id)

    def work() -> None:
        try:
            registry_mod.install(entry)
        except Exception:  # noqa: BLE001 - tried again next launch
            pass
        finally:
            with _updating_lock:
                _updating.discard(entry.id)

    threading.Thread(target=work, name=f"playground-update-{entry.id}", daemon=True).start()


def set_up(connection: Connection, *, progress=None, registry=None) -> None:
    """Set up whatever this connection's agent needs (Node.js, its adapter) now.
    Raises LaunchError in plain words."""
    if not connection.is_acp or connection.command:
        return
    from . import registry as registry_mod
    reg = registry if registry is not None else registry_mod.load()
    entry = reg.get(connection.agent) if connection.agent else reg.for_provider(connection.provider)
    if entry is None:
        raise LaunchError(f"Playground doesn't know how to start {connection.label}.")
    try:
        registry_mod.install(entry, progress=progress)
    except registry_mod.RegistryError as error:
        raise LaunchError(str(error)) from error


def agent_sign_in(connection: Connection, *, registry=None, timeout: float = 60.0) -> tuple[str, Any]:
    """How to sign in through the agent's own ACP adapter, when its program isn't on the computer.

    Returns ``("terminal", (argv, env))`` for a sign-in to show in a terminal
    (Claude's ``/login``), ``("agent", method_id)`` when the agent runs its own
    sign-in, usually in the browser (Codex with ChatGPT), or ``("none", None)``.
    """
    from . import acp
    argv, env = launch_spec(connection, registry)
    client = acp.AcpClient(argv, env=env, cwd=str(Path.home()), name=connection.label)
    try:
        client.start(timeout=timeout)
        methods = [m for m in client.auth_methods() if isinstance(m, dict)]
    finally:
        client.close()
    terminal = next((m for m in methods if m.get("type") == "terminal"), None)
    if terminal is not None:
        return "terminal", _terminal_command(argv, env, terminal)
    plan = next((m for m in methods if m.get("type") in (None, "agent") and "key" not in str(m.get("id"))), None)
    if plan is not None:
        return "agent", str(plan.get("id"))
    return "none", None


def _terminal_command(argv: list[str], env: dict[str, str], method: dict) -> tuple[list[str], dict[str, str]]:
    out_env = dict(env)
    for key, value in (method.get("env") or {}).items():
        out_env[str(key)] = str(value)
    return [*argv, *[str(a) for a in method.get("args") or []]], out_env


def agent_authenticate(connection: Connection, method_id: str, *, registry=None, timeout: float = 15 * 60.0) -> None:
    """Run an agent-run sign-in to the end (it may open the browser). Blocks; call it off the UI thread."""
    from . import acp
    argv, env = launch_spec(connection, registry)
    client = acp.AcpClient(argv, env=env, cwd=str(Path.home()), name=connection.label)
    try:
        client.start()
        client.authenticate(method_id, timeout=timeout)
    finally:
        client.close()


# ── Keys: from the keyring, never from a project ──────────────────────────

KEYRING_APP = "org.projectluma.Playground"


def lookup_api_key(connection: Connection, *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str | None:
    """The connection's API key from the OS keyring (libsecret's ``secret-tool``,
    or the macOS keychain), else from ``key_env``. Never from a project file."""
    ref = connection.key_ref or connection.id
    commands = []
    if shutil.which("secret-tool"):
        commands.append(["secret-tool", "lookup", "app", KEYRING_APP, "connection", ref])
    if sys.platform == "darwin" and shutil.which("security"):
        commands.append(["security", "find-generic-password", "-s", KEYRING_APP, "-a", ref, "-w"])
    for argv in commands:
        try:
            result = runner(argv, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        value = (result.stdout or "").strip()
        if result.returncode == 0 and value:
            return value
    if connection.key_env:
        value = os.environ.get(connection.key_env, "").strip()
        if value:
            return value
    return None


def store_api_key_command(connection: Connection) -> list[str] | None:
    """The command the UI runs (with the key on stdin) to save a key in the keyring.
    Playground itself never writes the key anywhere else."""
    ref = connection.key_ref or connection.id
    if shutil.which("secret-tool"):
        return ["secret-tool", "store", "--label", f"Playground: {connection.label}",
                "app", KEYRING_APP, "connection", ref]
    return None


# ── Finding what's already on this computer ───────────────────────────────

@dataclass
class Detected:
    provider: str
    name: str
    program: str = ""
    version: str = ""
    installed: bool = False
    signed_in: bool | None = None   # None: couldn't tell
    method: str = ""                # "subscription", "api key", "ChatGPT", ...
    account: str = ""               # a label only, e.g. the plan name
    note: str = ""
    models: list[str] = field(default_factory=list)
    via: str = ""                   # where the program came from, e.g. "the ChatGPT app"
    id: str = ""                    # the connection id this account gets: "claude", "codex-2"
    home: str = ""                  # this account's sign-in folder; "" = the program's normal one
    found_in: str = ""              # where the account was found: "your codex-2 launcher", "the Claude 2 app"
    ready: bool = False             # Playground can start this agent right now
    can_setup: bool = False         # Playground can set it up by itself (no admin, nothing for the person to do)

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("provider", "name", "program", "version", "installed",
                                               "signed_in", "method", "account", "note", "models", "via",
                                               "ready", "can_setup", "id", "home", "found_in")}


# Desktop apps that carry an agent program inside them. Their copy is used only to
# ask the program itself whether it's signed in and to run its own sign-in.
BUNDLED = {
    "codex": (("the ChatGPT app", ("/usr/lib/chatgpt/resources/codex", "/opt/ChatGPT/resources/codex",
                                   "/opt/chatgpt/resources/codex", "/snap/chatgpt/current/resources/codex",
                                   "/var/lib/flatpak/app/*[Cc]hat[Gg][Pp][Tt]*/current/active/files/*/resources/codex",
                                   "~/.local/share/flatpak/app/*[Cc]hat[Gg][Pp][Tt]*/current/active/files/*/resources/codex",
                                   "/Applications/ChatGPT.app/Contents/Resources/codex",
                                   "~/Applications/ChatGPT.app/Contents/Resources/codex",
                                   "/Applications/Codex.app/Contents/Resources/codex",
                                   "~/AppData/Local/Programs/ChatGPT/resources/codex.exe")),),
}
# Desktop apps that mean the person already has an account, though the agent signs in on its own.
APPS = {"claude": ("Claude", "/Applications/Claude.app"), "codex": ("ChatGPT", "/Applications/ChatGPT.app")}


def _bundled(provider: str) -> tuple[str, str]:
    for via, candidates in BUNDLED.get(provider, ()):
        for candidate in candidates:
            for path in sorted(glob.glob(os.path.expanduser(candidate))):
                if os.path.isfile(path) and os.access(path, os.X_OK):
                    return path, via
    return "", ""


def _has_app(provider: str, env: dict[str, str]) -> bool:
    """Whether the provider's desktop app is installed (a .app, or a desktop entry named for it)."""
    name, mac_app = APPS.get(provider, ("", ""))
    if not name:
        return False
    if os.path.isdir(mac_app) or os.path.isdir(os.path.expanduser("~" + mac_app)):
        return True
    data_home = env.get("XDG_DATA_HOME") or os.path.join(env.get("HOME") or str(Path.home()), ".local", "share")
    dirs = [data_home, *(env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(os.pathsep)]
    for folder in dirs:
        apps = Path(folder) / "applications"
        try:
            entries = list(apps.glob("*.desktop"))
        except OSError:
            continue
        for entry in entries:
            try:
                head = entry.read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError:
                continue
            if re.search(rf"^Name={re.escape(name)}$", head, re.M):
                return True
    return False


def _saved_connections() -> list[Connection]:
    try:
        return ConnectionStore().list()
    except Exception:  # noqa: BLE001 - finding accounts must never fail over a bad connections file
        return []


def _pi_rpc_models(command: list[str], env: dict[str, str], timeout: float) -> list[dict] | None:
    """Ask Pi itself, over RPC, which models are available. Pi only lists models
    whose providers have usable credentials, so this doubles as its auth status.
    Returns None when the probe fails, so the caller can fall back."""
    proc = None
    try:
        proc = subprocess.Popen([*command, "--mode", "rpc", "--no-session"], env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, start_new_session=True)
        proc.stdin.write('{"id":"pg-detect","type":"get_available_models"}\n')
        proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("id") == "pg-detect" and record.get("type") == "response":
                data = record.get("data") if isinstance(record.get("data"), dict) else {}
                return [m for m in data.get("models") or [] if isinstance(m, dict)]
        return None
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if proc is not None:
            for pipe in (proc.stdin, proc.stdout):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)


def _run(argv: list[str], timeout: float, env: dict[str, str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None


def detect(*, env: dict[str, str] | None = None, check_ollama: bool = True, timeout: float = 15.0,
           ollama_host: str | None = None, ollama_opener: Callable | None = None, registry=None) -> list[Detected]:
    """What agents are here, whether Playground can run or set them up, and
    whether they're signed in, without reading any secret.

    Uses ``claude auth status --json`` and ``codex login status`` (their own
    reports, from the program on the PATH or the copy inside its desktop app),
    else whether their sign-in file exists, ``--version`` for the rest, and
    Ollama's local model list.
    """
    run_env = base_env(env)
    path = run_env.get("PATH")
    found: list[Detected] = []

    from . import accounts
    path = accounts.search_path(run_env)
    saved = _saved_connections()

    claude = shutil.which("claude", path=path)
    claude_version = ""
    if claude:
        version = _run([claude, "--version"], timeout, run_env)
        if version and version.returncode == 0:
            claude_version = version.stdout.strip().split(" ")[0]
    has_claude_app = _has_app("claude", run_env)
    for account in accounts.discover("claude", run_env, saved):
        item = _account_item("claude", account, claude, claude_version, "", run_env, timeout)
        if account.is_default and not claude and has_claude_app and not item.signed_in:
            item.note = "You have the Claude app. Claude Code signs in on its own, once."
        found.append(item)

    codex = shutil.which("codex", path=path)
    via = ""
    if not codex:
        codex, via = _bundled("codex")
    codex_version = ""
    if codex:
        version = _run([codex, "--version"], timeout, run_env)
        if version and version.returncode == 0:
            codex_version = version.stdout.strip().split(" ")[-1]
    for account in accounts.discover("codex", run_env, saved):
        found.append(_account_item("codex", account, codex, codex_version, via, run_env, timeout))

    for provider, program, name, args in (("gemini", "gemini", "Gemini CLI", ["--version"]),
                                          ("opencode", "opencode", "OpenCode", ["--version"]),
                                          ("goose", "goose", "goose", ["--version"]),
                                          ("pi", "pi", "Pi", ["--version"])):
        exe = shutil.which(program, path=path)
        item = Detected(provider, name, program=exe or "", installed=bool(exe))
        if exe:
            version = _run([exe, *args], min(timeout, 10), run_env)
            if version and version.returncode == 0:
                item.version = (version.stdout.strip().splitlines() or [""])[0][:40]
            if accounts.SIGN_IN_FILE.get(provider):   # programs that keep a sign-in file of their own
                account = accounts.Account(provider, "", accounts.NAMES.get(provider, name), id=provider)
                item = _account_item(provider, account, exe, item.version, "", run_env, timeout)
            if provider == "pi":
                models = _pi_rpc_models([exe], run_env, min(timeout, 15))
                if models is not None:
                    item.signed_in = bool(models)
                    item.models = [str(m.get("id")) for m in models if m.get("id")]
                    item.note = (f"Signed in to {len({m.get('provider') for m in models})} providers."
                                 if models else "Not signed in yet. Run /login in Pi.")
        found.append(item)

    _readiness(found, registry, path)

    ollama_exe = shutil.which("ollama", path=path)
    item = Detected("ollama", "Ollama", program=ollama_exe or "", installed=bool(ollama_exe))
    if check_ollama:
        from . import ollama
        state = ollama.status(ollama_host or ollama.DEFAULT_HOST, opener=ollama_opener or urllib.request.urlopen,
                              which=lambda name: ollama_exe)
        item.installed = state.installed
        item.models = [m.name for m in state.models]
        item.signed_in = state.running or None
        item.version = state.version
        # "Running, with 2 models." or "Ollama is running, but it has no models yet. Open a terminal and run: ..."
        item.note = " ".join(p for p in (state.note, state.next_step) if p)
    found.append(item)
    return found


def _account_item(provider: str, account, program: str | None, version: str, via: str, env: dict[str, str],
                  timeout: float) -> Detected:
    """One account's row: asks the program itself when it's here, else checks the sign-in file exists."""
    from . import accounts
    item = Detected(provider, account.label, program=program or "", installed=bool(program), version=version,
                    via=via, id=account.id, home=account.home, found_in=account.source)
    account_env = dict(env)
    if account.home:
        account_env[HOME_VARS[provider]] = account.home
    usable = program and (account.is_default or os.path.isdir(account.home))
    if usable and provider == "claude":
        status = _run([program, "auth", "status", "--json"], timeout, account_env)
        if status and status.stdout.strip().startswith("{"):
            try:
                data = json.loads(status.stdout)
            except ValueError:
                data = {}
            item.signed_in = bool(data.get("loggedIn"))
            method = str(data.get("authMethod") or "")
            item.method = {"claude.ai": "subscription", "oauth_token": "subscription",
                           "api_key": "api key", "none": ""}.get(method, method)
            item.account = str(data.get("subscriptionType") or "")
            return item
    elif usable and provider == "codex":
        status = _run([program, "login", "status"], timeout, account_env)
        if status is not None:
            text = (status.stdout + "\n" + status.stderr).lower()  # never kept: it may show a masked key
            if "logged in" in text and "not logged in" not in text:
                item.signed_in = True
                item.method = "ChatGPT" if "chatgpt" in text else ("api key" if "api key" in text else "")
                return item
            if "not logged in" in text:
                item.signed_in = False
                return item
    item.signed_in = accounts.signed_in(provider, account.home, env)
    return item


def check_account(provider: str, home: str = "", *, env: dict[str, str] | None = None,
                  timeout: float = 15.0) -> Detected:
    """Whether one account folder is signed in ("" = the program's normal folder), e.g. right after its sign-in."""
    from . import accounts
    run_env = base_env(env)
    path = accounts.search_path(run_env)
    program = shutil.which(provider, path=path) or ""
    via = ""
    if not program:
        program, via = _bundled(provider)
    account = accounts.Account(provider, home, accounts.NAMES.get(provider, provider), id=provider)
    return _account_item(provider, account, program, "", via, run_env, timeout)


def _readiness(found: list[Detected], registry, path: str | None) -> None:
    """Fill ``ready`` and ``can_setup`` from the agent registry (cached; built in when offline)."""
    from . import registry as registry_mod
    try:
        reg = registry if registry is not None else registry_mod.load()
    except Exception:  # noqa: BLE001 - the built-in list is always there
        reg = registry_mod.builtin_registry()
    for item in found:
        entry = reg.for_provider(item.provider)
        if entry is None:
            continue
        try:
            launch = registry_mod.launch_command(entry, path_env=path)
        except registry_mod.RegistryError:
            continue
        item.ready = not launch.needs_install and not launch.missing
        item.can_setup = item.ready or registry_mod.can_install(entry)


def suggest(detected: list[Detected], owner: str, existing: list[Connection] | None = None) -> list[Connection]:
    """Connections to offer from what ``detect`` found (not saved)."""
    existing = existing or []
    have = {c.account_key() for c in existing}
    out: list[Connection] = []
    for item in detected:
        key = (item.provider, os.path.realpath(item.home) if item.home else "default")
        if not (item.installed or item.ready or item.can_setup) or key in have:
            continue
        if item.provider in ("claude", "codex") and item.signed_in and \
                item.method == {"claude": "subscription", "codex": "ChatGPT"}[item.provider]:
            out.append(Connection(id=item.id or item.provider, label=item.name, kind="personal-plan",
                                  provider=item.provider, owner=owner, config_home=item.home))
        elif item.provider == "ollama" and item.models and "ari-local" not in {c.id for c in existing}:
            out.append(Connection(id="ari-local", label="Ari on this computer", kind="ari", provider="ari",
                                  owner=owner, endpoint="ollama", base_url="http://localhost:11434/v1",
                                  model=item.models[0], extra={"context": 65536}))
    return out
