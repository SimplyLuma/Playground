# SPDX-License-Identifier: Apache-2.0
"""Finding every account of an agent program on this computer.

People keep a second (or third) Claude or Codex account in many ways: a
launcher script or a desktop shortcut that sets ``CLAUDE_CONFIG_DIR`` or
``CODEX_HOME``, a shell alias that does the same, a folder named like
``~/.codex-work``, a second profile of the desktop app, or a folder Playground
itself made. ``discover`` looks in all of those and returns one ``Account``
per sign-in folder, the program's normal folder first. It never opens a
sign-in file: it only checks that one exists.

The desktop apps keep their own sign-in to themselves, so an extra profile of
the Claude app becomes an account that signs in once, in a folder of its own.

``search_path`` is the PATH plus the places programs are usually installed
in a home folder (nvm, npm's global folder, Homebrew...), because an app
started from the desktop often doesn't get the PATH a terminal has.
"""
from __future__ import annotations

from dataclasses import dataclass
import glob
import os
from pathlib import Path
import re
import sys
from typing import Iterable

from ..core import paths

HOME_VAR = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "opencode": "OPENCODE_DATA_DIR"}
DEFAULT_FOLDER = {"claude": ".claude", "codex": ".codex", "opencode": ".local/share/opencode"}
NAMES = {"claude": "Claude", "codex": "Codex", "opencode": "OpenCode"}
# The file each program writes when it signs in (only whether it exists is checked).
SIGN_IN_FILE = {"claude": ".credentials.json", "codex": "auth.json", "opencode": "auth.json"}
# Files that show a folder is the program's own, signed in or not.
OWN_FILES = {"claude": (".credentials.json", ".claude.json", "settings.json"),
             "codex": ("auth.json", "config.toml"),
             "opencode": ("auth.json", "account.json", "opencode.json", "opencode.jsonc")}
# Folders people name for extra accounts, relative to the home folder.
FOLDER_PATTERNS = {
    "claude": (".claude-*", ".claude_*", ".config/claude-*", ".local/share/claude-profile-*"),
    "codex": (".codex-*", ".codex_*", ".config/codex-*", ".local/share/codex-profile-*"),
    "opencode": (".local/share/opencode-*", ".local/share/opencode_*", ".config/opencode-*", ".local/state/opencode-*"),
}
# Desktop apps whose extra profiles are extra accounts that need their own sign-in here.
APP_PROFILES = {"claude": "Claude"}
SCRIPT_DIRS = (".local/bin", "bin", ".bin")
SHELL_FILES = (".bashrc", ".bash_aliases", ".bash_profile", ".profile", ".zshrc", ".zprofile", ".zshenv",
               ".config/fish/config.fish")
# Where programs live in a home folder when the desktop's PATH doesn't list them.
HOME_BIN_DIRS = (".local/bin", "bin", ".npm-global/bin", ".npm/bin", ".claude/local", ".volta/bin", ".bun/bin",
                 ".cargo/bin", ".deno/bin", "Library/pnpm", ".local/share/pnpm")
SYSTEM_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/home/linuxbrew/.linuxbrew/bin")
MAX_SCAN_BYTES = 64 * 1024


@dataclass
class Account:
    provider: str
    home: str            # "" = the program's normal folder
    label: str           # "Claude", "Claude 2", "Codex (work)"
    source: str = ""     # where it was found, in words: "your codex-2 launcher", "the Claude 2 app"
    id: str = ""         # the connection id it would get: "claude", "claude-2", "codex-work"

    @property
    def is_default(self) -> bool:
        return not self.home


def _home(env: dict[str, str]) -> str:
    return env.get("HOME") or str(Path.home())


def default_home(provider: str, env: dict[str, str]) -> str:
    return env.get(HOME_VAR[provider]) or os.path.join(_home(env), DEFAULT_FOLDER[provider])


def search_path(env: dict[str, str]) -> str:
    """PATH, then the usual home-folder and system install places, newest nvm Node first."""
    home = _home(env)
    parts = [p for p in (env.get("PATH") or "").split(os.pathsep) if p]
    extra = [os.path.join(home, d) for d in HOME_BIN_DIRS]
    nvm = sorted(glob.glob(os.path.join(home, ".nvm", "versions", "node", "*", "bin")), key=_version_key, reverse=True)
    extra += nvm
    if sys.platform != "win32":
        extra += list(SYSTEM_BIN_DIRS)
    else:
        appdata = env.get("APPDATA") or ""
        if appdata:
            extra.append(os.path.join(appdata, "npm"))
    for part in extra:
        if part not in parts and os.path.isdir(part):
            parts.append(part)
    return os.pathsep.join(parts)


def _version_key(path: str) -> tuple:
    name = Path(path).parent.name.lstrip("v")
    return tuple(int(x) if x.isdigit() else 0 for x in name.split("."))


def signed_in(provider: str, home: str, env: dict[str, str]) -> bool | None:
    """Whether the sign-in file exists in this account's folder. None when it can't be told (the Mac
    keeps Claude's sign-in in the keychain)."""
    folder = home or default_home(provider, env)
    if os.path.isfile(os.path.join(folder, SIGN_IN_FILE[provider])):
        return True
    if provider == "claude" and sys.platform == "darwin":
        return None
    return False


def discover(provider: str, env: dict[str, str], saved: Iterable = ()) -> list[Account]:
    """Every account of ``provider`` on this computer, the normal one first."""
    if provider not in HOME_VAR:
        return []
    home = _home(env)
    default = os.path.realpath(default_home(provider, env))
    found: dict[str, Account] = {}
    order: list[str] = []

    def add(folder: str, source: str, *, must_exist: bool = True, label: str = "") -> None:
        folder = folder.replace("${HOME}", home).replace("$HOME", home)
        if folder == "~" or folder.startswith("~/"):
            folder = home + folder[1:]
        if not os.path.isabs(folder):
            return
        real = os.path.realpath(folder)
        if real == default or real in found:
            return
        if must_exist and not _looks_like(provider, real):
            return
        found[real] = Account(provider, folder, label, source)
        order.append(real)

    for conn in saved:   # accounts Playground already uses come first, under the names given then
        if getattr(conn, "provider", "") == provider and getattr(conn, "config_home", ""):
            add(conn.config_home, "Playground", must_exist=False, label=getattr(conn, "label", ""))
    for folder, source in _named_in_files(provider, env):
        add(folder, source, must_exist=False)
    for pattern in FOLDER_PATTERNS[provider]:
        for folder in sorted(glob.glob(os.path.join(home, pattern))):
            if os.path.isdir(folder):
                add(folder, "")
    accounts = [Account(provider, "", NAMES[provider], "")] + [found[k] for k in order]
    for name in _app_profiles(provider, env)[1:]:
        if any(a.label == name for a in accounts):
            continue
        slug = paths.slugify(name, 32)
        accounts.append(Account(provider, str(paths.account_home(slug)), name, f"the {name} app"))
    _name(accounts)
    return accounts


def _looks_like(provider: str, folder: str) -> bool:
    return os.path.isdir(folder) and any(os.path.exists(os.path.join(folder, f)) for f in OWN_FILES[provider])


def _named_in_files(provider: str, env: dict[str, str]) -> list[tuple[str, str]]:
    """``CODEX_HOME=/somewhere`` in launcher scripts, desktop shortcuts and shell settings."""
    var = HOME_VAR[provider]
    pattern = re.compile(rf"""\b{var}\s*=\s*(["']?)([^\s"';|&)]+)\1""")
    fish = re.compile(rf"""\bset\s+-\w*x\w*\s+{var}\s+(["']?)([^\s"';]+)\1""")
    home = _home(env)
    out: list[tuple[str, str]] = []
    files: list[tuple[Path, str]] = []
    for d in SCRIPT_DIRS:
        folder = Path(home) / d
        try:
            files += [(p, f"your {p.name} launcher") for p in sorted(folder.iterdir()) if p.is_file()]
        except OSError:
            pass
    for apps in _application_dirs(env):
        try:
            files += [(p, "a desktop shortcut") for p in sorted(apps.glob("*.desktop"))]
        except OSError:
            pass
    files += [(Path(home) / f, "your shell settings") for f in SHELL_FILES]
    for path, source in files:
        text = _read_small(path)
        if not text or var not in text:
            continue
        if path.suffix == ".desktop":
            name = re.search(r"^Name=(.+)$", text, re.M)
            source = f"the {name.group(1).strip()} shortcut" if name else source
        for match in list(pattern.finditer(text)) + list(fish.finditer(text)):
            out.append((match.group(2), source))
    return out


def _read_small(path: Path) -> str:
    try:
        if not path.is_file() or path.stat().st_size > MAX_SCAN_BYTES:
            return ""
        data = path.read_bytes()
    except OSError:
        return ""
    if b"\0" in data[:1024]:
        return ""                     # a compiled program, not a script
    return data.decode("utf-8", errors="replace")


def _application_dirs(env: dict[str, str]) -> list[Path]:
    data_home = env.get("XDG_DATA_HOME") or os.path.join(_home(env), ".local", "share")
    dirs = [data_home, *(env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(os.pathsep)]
    return [Path(d) / "applications" for d in dirs if d]


def _app_profiles(provider: str, env: dict[str, str]) -> list[str]:
    """The desktop app's profiles by name ("Claude", "Claude 2", "Claude (Work)"), main one first."""
    app = APP_PROFILES.get(provider)
    if not app:
        return []
    pattern = re.compile(rf"^Name={re.escape(app)}(?:\s+(?:\d+|\([^)]+\)))?\s*$", re.M)
    names: list[str] = []
    for apps in _application_dirs(env):
        try:
            entries = sorted(apps.glob("*.desktop"))
        except OSError:
            continue
        for entry in entries:
            head = _read_small(entry).split("\n[", 1)[0]   # the main section, not "Uninstall" actions
            match = pattern.search(head)
            if match:
                name = match.group(0)[len("Name="):].strip()
                if name not in names:
                    names.append(name)
    if sys.platform == "darwin" and (os.path.isdir(f"/Applications/{app}.app") or
                                     os.path.isdir(os.path.join(_home(env), "Applications", f"{app}.app"))):
        if app not in names:
            names.insert(0, app)
    names.sort(key=lambda n: (n != app, n))
    return names


def _name(accounts: list[Account]) -> None:
    """Give every account a clear, unique name and id: "Codex", "Codex 2", "Codex (work)"."""
    taken: set[str] = set()
    for index, account in enumerate(accounts, start=1):
        name = NAMES[account.provider]
        if not account.label:
            account.label = _label_from_folder(account.provider, account.home) or f"{name} {index}"
        label, n = account.label, 2
        while label in taken:
            label, n = f"{account.label} {n}", n + 1
        account.label = label
        taken.add(label)
        account.id = account.provider if account.is_default else paths.slugify(label, 32)


def _label_from_folder(provider: str, folder: str) -> str:
    """~/.codex-work → "Codex (work)", ~/.local/share/codex-profile-2 → "Codex 2"."""
    base = Path(folder).name.lower().lstrip(".")
    words = [w for w in re.split(r"[-_. ]+", base) if w and w not in (provider, "profile", "config", "home")]
    if not words:
        return ""
    name = NAMES[provider]
    rest = " ".join(words)
    return f"{name} {rest}" if rest.isdigit() else f"{name} ({rest})"
