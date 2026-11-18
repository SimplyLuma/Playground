# SPDX-License-Identifier: Apache-2.0
"""Per-account cache for model lists an agent can offer before it runs.

The rows are ACP-shaped option values, ``{value, name, description}``. Agent
modules that implement ``available_models`` can use this helper; the cache key
is the connection's account, so two accounts of the same program don't share a
list.
"""
from __future__ import annotations

import time
from pathlib import Path

from ..core import paths
from ..core.store import atomic_write_json, read_json

TTL_S = 12 * 60 * 60


def cache_path() -> Path:
    return paths.config_dir() / "available-models.json"


def key(connection) -> str:
    try:
        return "|".join(str(part) for part in connection.account_key())
    except Exception:  # noqa: BLE001 - a plain id is enough to key a cache
        return str(getattr(connection, "id", "") or "unknown")


def cached(connection, *, now: float | None = None) -> list[dict]:
    moment = time.time() if now is None else now
    entry = (read_json(cache_path()) or {}).get(key(connection))
    if not isinstance(entry, dict) or moment - float(entry.get("at") or 0) > TTL_S:
        return []
    return [dict(row) for row in entry.get("rows") or [] if isinstance(row, dict) and row.get("value")]


def stale(connection) -> list[dict]:
    entry = (read_json(cache_path()) or {}).get(key(connection))
    return [dict(row) for row in (entry or {}).get("rows") or [] if isinstance(row, dict)] \
        if isinstance(entry, dict) else []


def remember(connection, rows: list[dict], *, now: float) -> None:
    try:
        path = cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = read_json(path) or {}
        if not isinstance(data, dict):
            data = {}
        data[key(connection)] = {"at": now, "rows": rows}
        atomic_write_json(path, data)
    except OSError:
        pass
