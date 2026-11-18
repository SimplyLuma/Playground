# SPDX-License-Identifier: Apache-2.0
"""OpenCode's model list, read from its own CLI and kept on disk.

Playground normally learns an agent's settings from ACP config options, but that
needs the agent running — too late to let someone choose a model before the first
message. OpenCode can print its models with ``opencode models`` without starting
a session, so we keep that list for the next start. The ACP list, once the agent
is up, is authoritative and replaces this one.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from ..core import paths
from ..core.store import atomic_write_json, read_json

CACHE_TTL_S = 12 * 60 * 60        # models change rarely; refresh a couple of times a day
OPTION_ID = "model"               # OpenCode's ACP config option id for the model
COMMAND = ("opencode", "models")  # the subcommand is plural in current OpenCode

Run = Callable[[list[str], dict[str, str]], subprocess.CompletedProcess | None]


def _cache_path() -> Path:
    return paths.config_dir() / "opencode-models.json"


def _account(connection) -> str:
    try:
        return "|".join(str(part) for part in connection.account_key())
    except Exception:  # noqa: BLE001 - a plain label is enough to key a cache
        return str(getattr(connection, "id", "") or "opencode")


def _environment(connection) -> tuple[str, dict[str, str]]:
    """The program and environment for one connection's account (no secrets read)."""
    from . import connections
    env = connections.base_env()
    exe = shutil.which(COMMAND[0], path=env.get("PATH")) or COMMAND[0]
    home = getattr(connection, "config_home", "")
    if home:
        var = connections.HOME_VARS.get("opencode")
        if var:
            env[var] = str(home)
    return exe, env


def _run(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=15, env=env,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None


def cached_values(connection, *, now: float | None = None) -> list[str]:
    """The last known model ids for this account, if the cache is still fresh."""
    moment = time.time() if now is None else now
    data = read_json(_cache_path()) or {}
    entry = data.get(_account(connection)) if isinstance(data, dict) else None
    if not isinstance(entry, dict) or moment - float(entry.get("at") or 0) > CACHE_TTL_S:
        return []
    return [str(v) for v in entry.get("values") or [] if isinstance(v, str) and v]


def model_option(connection, *, current: Any = None, env: dict[str, str] | None = None,
                 run: Run | None = None, now: float | None = None, refresh: bool = False,
                 cached_only: bool = False) -> dict | None:
    """The model setting in ACP's shape: ``{id, name, category, currentValue,
    options: [{value, name}]}``. Fresh models come from ``opencode models``; a
    stale cache is used when the program is missing or the call fails. ``None``
    means there's no list at all yet. ``cached_only`` never starts the program —
    handy where blocking (the UI thread) isn't allowed.
    """
    moment = time.time() if now is None else now
    values = [] if refresh else cached_values(connection, now=moment)
    if not values and not cached_only:
        exe, base = _environment(connection)
        argv = [exe, COMMAND[1]]
        result = (run or _run)(argv, dict(env if env is not None else base))
        output = result.stdout if result is not None and result.returncode == 0 else ""
        values = list(dict.fromkeys(line.strip() for line in (output or "").splitlines() if line.strip()))
        if values:
            _remember(connection, values, now=moment)
        else:
            values = _stale_values(connection)
    if not values:
        return None
    return {"id": OPTION_ID, "name": "Model", "category": "model", "type": "select",
            "currentValue": current if current in values else None,
            "options": [{"value": value, "name": value} for value in values]}


def _stale_values(connection) -> list[str]:
    data = read_json(_cache_path()) or {}
    entry = data.get(_account(connection)) if isinstance(data, dict) else None
    return [str(v) for v in (entry or {}).get("values") or [] if isinstance(v, str) and v] if isinstance(entry, dict) else []


def _remember(connection, values: list[str], *, now: float) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = read_json(path) or {}
        if not isinstance(data, dict):
            data = {}
        data[_account(connection)] = {"at": now, "values": list(values)}
        atomic_write_json(path, data)
    except OSError:
        pass
