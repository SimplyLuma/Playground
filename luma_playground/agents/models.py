# SPDX-License-Identifier: Apache-2.0
"""Model lists for connections, found through a provider-agnostic hook.

An agent module can offer ``available_models(connection, *, offline=False,
refresh=False)`` returning ACP-shaped rows ``{value, name, description}``.
``available()`` resolves that module from the connection's provider (or its
registry agent id) and calls the hook when it exists. A provider without a hook
returns an empty list here, and still exposes its models over ACP once the agent
is running, through ``AcpClient.config_options`` and the runner's option settle.

``offline=True`` never starts a program, which makes it safe to call on the UI
thread. The cache itself lives in ``model_cache`` and is shared by all hooks.
"""
from __future__ import annotations

import importlib
import importlib.util
import re


def _hook(connection):
    """The ``available_models`` function of the first agent module that offers one."""
    names: list[str] = []
    for value in (getattr(connection, "provider", ""), getattr(connection, "agent", "")):
        name = str(value or "")
        if re.fullmatch(r"[a-z][a-z0-9_]*", name) and name not in names:
            names.append(name)
    for name in names:
        module_name = f"{__package__}.{name}"
        try:
            if importlib.util.find_spec(module_name) is None:
                continue
            module = importlib.import_module(module_name)
        except (ImportError, ValueError):
            continue
        hook = getattr(module, "available_models", None)
        if callable(hook):
            return hook
    return None


def available(connection, *, offline: bool = False, refresh: bool = False) -> list[dict]:
    """ACP-shaped rows for one connection, or an empty list when no early list is known."""
    hook = _hook(connection)
    if hook is None:
        return []
    rows = hook(connection, offline=offline, refresh=refresh) or []
    return [dict(row) for row in rows if isinstance(row, dict) and row.get("value")]
