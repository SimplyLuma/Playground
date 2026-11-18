# SPDX-License-Identifier: Apache-2.0
"""Playground's window: the rail, the conversation, and the Stage (spec §5, simulator v73).

    ┌ Playground ▾ │ Kiln  Project book 📖 ─────────────── People · Places ● · Usage ▬ ┐
    │ Projects   + │  Add a waitlist                       │ ▢ ▭ ▣ ▤      Stage Live ⤢ │
    │ Kiln         │  ● Needs you · Started Tuesday · Ari  │                          │
    │  Talk to Ari │  [cards …]                            │      [ the product ]     │
    │  ● Waitlist  │                                       │                          │
    │  + New goal  │  ( Ari ▾ | Tell Ari what to change… ↑)│   Point · Record · Open  │
    └──────────────┴───────────────────────────────────────┴──────────────────────────┘

From 1180 px everything shows. Narrower, the Stage folds away behind the title row's Stage
button and floats in over the conversation. At phone width (under 640) the conversation is
the page: the rail is the title island's menu, the Stage is a sheet one tap away, and the
composer floats above the gesture chin.

The project folder is the source of truth. The window reads it through core.store, keeps a
core.watch watcher on it (so an agent writing through MCP shows up as it happens), listens
to the bus, runs the Stage's preview, and answers the requests only the open app can: a
screenshot of the Stage, and a run of the checker.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from luma_appkit import (ActionCenter, AppWindow, Command, CommandGroup, CommandRegistry, Island,  # noqa: E402
                         IslandOverlaySplitView, TitleIsland, ToastHost, add_style_sheet,
                         command_popover)

from .. import APP_ID  # noqa: E402
from ..core import events  # noqa: E402
from ..core.model import GENERAL_CHAT_ID, Answer, GoalStatus, ThreadEntry, conversation_title  # noqa: E402
from ..core.store import AlreadyAnswered, NotFound, ProjectStore, StoreError, parse_front_matter, read_jsonl  # noqa: E402
from . import widgets as w  # noqa: E402
from .cards import CardContext  # noqa: E402
from .cards.message import note  # noqa: E402
from .composer import Composer  # noqa: E402
from .conversation import Conversation, goal_meta  # noqa: E402
from .sidebar import AREA_KEYS, AREAS, Sidebar  # noqa: E402
from .stage import Stage  # noqa: E402

try:
    from . import sync_ui  # noqa: E402
except Exception as _error:  # noqa: BLE001 - sharing is optional; the window works without it
    logging.getLogger(__name__).warning("sharing isn't available: %s", _error)
    sync_ui = None

log = logging.getLogger(__name__)

WIDE = 1160      # all three parts (a 1180 window, less its frame)
PHONE = 640      # the conversation is the page
HEARTBEAT_S = 10
STATUS_ORDER = {"needs-you": 0, "working": 1, "waiting": 1, "ready": 2, "live": 3, "done": 4, "paused": 5}
ARI = {"id": "ari", "label": "Ari", "provider": "ari", "kind": "ari", "where": "this-computer"}
PROVIDER_NAMES = {"claude": "Claude", "codex": "Codex", "gemini": "Gemini", "opencode": "OpenCode",
                  "goose": "goose", "ollama": "Local model", "ari": "Ari"}
BUSY_STATES = ("starting", "working", "thinking", "running")
RAIL_WIDTH = 224


def _optional(module: str):
    """A core or agents module, if it has landed; None while it hasn't."""
    import importlib
    try:
        return importlib.import_module(f"..{module}", __package__)
    except Exception as error:  # noqa: BLE001 - absence is a state, not a crash
        if not isinstance(error, ModuleNotFoundError):
            log.warning("could not load %s: %s", module, error)
        return None


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    return dict(getattr(value, "__dict__", {}))


def _data_dir(name: str) -> Path:
    from ..core.paths import app_data
    return app_data(name)


def _connection_field(connection, name: str, default=""):
    """Read a field from a stored connection dict or a Connection object."""
    obj = connection.get("_object") if isinstance(connection, dict) else None
    if obj is not None:
        return getattr(obj, name, default)
    if isinstance(connection, dict):
        return connection.get(name, default)
    return getattr(connection, name, default)


def _ari_placeholder(connection) -> bool:
    """The stand-in row load_connections() adds when nobody has connected Ari."""
    return isinstance(connection, dict) and connection.get("id") == "ari" and connection.get("_object") is None


# ── one project, through the store ───────────────────────────────────────────

class ProjectData:
    """One open project, read through core.store (the folder is the source of truth)."""

    def __init__(self, path: Path, bus, connections: list[dict] | None = None) -> None:
        self.path = Path(path)
        self.store = ProjectStore(self.path, bus=bus)
        self.model = None
        self.set_connections(connections or [])
        self.reload()

    def set_connections(self, connections: list[dict]) -> None:
        """Take the window's snapshot of this computer's connections. One cached default
        serves every row, so drawing the list never reopens connections.toml."""
        self.connections = list(connections)
        self.default_id = default_connection_id(self.connections)
        self.connection_ids = {str(_connection_field(c, "id")) for c in self.connections}
        self._connection_labels = {str(_connection_field(c, "id")): _connection_field(c, "label")
                                   for c in self.connections}

    def reload(self) -> None:
        self._you = None
        self.forget()
        try:
            self.model = self.store.read_project()
        except StoreError as error:
            log.warning("could not read %s: %s", self.path, error)
            self.model = None

    @property
    def name(self) -> str:
        return str(getattr(self.model, "name", "") or self.path.name)

    @property
    def what(self) -> str:
        if self.model is None:
            return ""
        return str(self.model.summary or getattr(self.model.recipe, "summary", "") or "")

    @property
    def places(self) -> list[dict]:
        return [p.to_dict() for p in getattr(self.model, "places", [])]

    @property
    def members(self) -> list[dict]:
        return [m.to_dict() for m in getattr(self.model, "members", [])]

    def place(self, place_id: str) -> dict | None:
        return next((p for p in self.places if p.get("id") == place_id), None)

    def member(self, member_id: str) -> dict | None:
        return next((m for m in self.members if m.get("id") == member_id), None)

    def you(self) -> str:
        """Which member is the person at this computer."""
        if getattr(self, "_you", None):
            return self._you
        self._you = self._find_you()
        return self._you

    def _find_you(self) -> str:
        project = _optional("core.project")
        mine = ""
        if project is not None:
            try:
                mine = project.default_owner().id
            except Exception:  # noqa: BLE001
                mine = ""
        ids = [m.get("id") for m in self.members]
        if mine in ids:
            return mine
        owner = next((m for m in self.members if m.get("role") == "owner"), None)
        return str((owner or {}).get("id") or mine or "me")

    # What's read is kept for a moment: one redraw asks for the goals a dozen times (the rail, the heading,
    # the composer, the island), and each read is every goal file plus every pending request.
    CACHE_S = 0.5

    def forget(self) -> None:
        """Something changed on disk: the next read goes to the folder again."""
        self._goals_cache: tuple[float, list[dict]] | None = None
        self._attention_cache: list | None = None

    def goals(self) -> list[dict]:
        import time
        cached = getattr(self, "_goals_cache", None)
        if cached is not None and time.monotonic() - cached[0] < self.CACHE_S:
            return [dict(g) for g in cached[1]]
        goals = self._read_goals()
        self._goals_cache = (time.monotonic(), goals)
        return [dict(g) for g in goals]

    def _read_goals(self) -> list[dict]:
        try:
            listed = self.store.list_goals()
        except StoreError:
            return []
        pending = self.pending_counts()
        here = self.connection_ids
        goals = []
        for goal in listed:
            data = goal.to_dict()
            data["is_chat"] = goal.is_chat
            if goal.is_chat:
                try:
                    data["empty"] = self.store.paths.thread(goal.id).stat().st_size == 0
                except OSError:
                    data["empty"] = True
                if not data["empty"] and len(listed) > 1:
                    # Older projects wrote their beginning (the idea, the brief, the recipe) into a chat of its
                    # own beside the first conversation. With no replies in it, it's not a conversation: hide it.
                    entries = self.thread(goal.id)
                    if all(e.get("role") == "person" or e.get("kind") in ("brief", "recipe") for e in entries):
                        data["empty"] = True
                if not data["empty"]:
                    first = next((e.get("text") for e in self.thread(goal.id)
                                  if e.get("role") == "person" and e.get("text")), "")
                    data["display_title"] = conversation_title(first) if first else "Earlier conversation"
            data["pending"] = pending.get(goal.id, 0)
            data["agent_name"] = self.agent_words(goal.connection)
            if data["pending"] and data["status"] == "working":
                data["status"] = "needs-you"
                data.pop("status_label", None)
            elif data["status"] == "working" and not goal.is_chat and self.nobody_to_work(goal, here):
                # Said honestly: no agent on this computer can pick it up yet ("Working · Ari" would be a fib).
                data["status"] = "waiting"
                data["status_label"] = w.state_label("waiting")
            goals.append(data)
        chat = [g for g in goals if g["is_chat"]]
        rest = [g for g in goals if not g["is_chat"]]
        rest.sort(key=lambda g: (STATUS_ORDER.get(g["status"], 6), -_stamp(g.get("updated") or g.get("created"))))
        return chat + rest

    def goal(self, goal_id: str) -> dict | None:
        return next((g for g in self.goals() if g["id"] == goal_id), None)

    def nobody_to_work(self, goal, here: set[str] | None) -> bool:
        """A goal of yours whose agent isn't connected on this computer. A teammate's goal runs on
        their computer, so it never counts; nor does anything when the connections can't be read."""
        if here is None:
            return False
        you = self.you()
        owner = str(getattr(goal, "owner", "") or "")
        if owner and owner != you and self.member(owner) is not None:
            return False
        key = str(goal.connection or self.default_id)
        for member in self.members:
            member_id = str(member.get("id") or "")
            if member_id and member_id != you and key.startswith(member_id + "-"):
                return False
        return key not in here

    def agent_words(self, connection_id: str) -> str:
        """'Priya’s agent' for a teammate's own plan, else the connection's name."""
        key = str(connection_id or self.default_id)
        for member in self.members:
            member_id = str(member.get("id") or "")
            if member_id and key.startswith(member_id + "-") and member_id != self.you():
                return f"{str(member.get('name') or member_id).split()[0]}’s agent"
        if key == "ari":
            return "Ari"
        label = self._connection_labels.get(key)
        if label:
            return str(label)                            # "Codex 2", as the person named it
        provider = next((p for p in key.split("-") if p in PROVIDER_NAMES), "")
        return PROVIDER_NAMES.get(provider) or key.replace("-", " ").capitalize()

    def ensure_chat(self) -> None:
        try:
            self.store.ensure_general_chat()
        except (StoreError, OSError) as error:
            log.warning("could not make the general chat: %s", error)

    # the Project space: services, bugs and what needs a look
    def services(self) -> list[dict]:
        """`.playground/stack.toml` [[services]] (through the store when it reads them)."""
        reader = getattr(self.store, "read_stack", None)
        if callable(reader):
            try:
                stack = reader()
                services = getattr(stack, "services", None) if not isinstance(stack, dict) else stack.get("services")
                return [_as_dict(s) for s in services or []]
            except Exception:  # noqa: BLE001
                log.exception("store.read_stack failed")
        try:
            with open(self.path / ".playground" / "stack.toml", "rb") as handle:
                import tomllib
                document = tomllib.load(handle)
        except (OSError, ValueError):
            return []
        return [s for s in document.get("services") or [] if isinstance(s, dict) and s.get("id")]

    def issues(self) -> list[dict]:
        """`.playground/issues/<n>.md` front matter, newest first."""
        reader = getattr(self.store, "list_issues", None)
        if callable(reader):
            try:
                return [_as_dict(i) for i in reader()]
            except Exception:  # noqa: BLE001
                log.exception("store.list_issues failed")
        out = []
        for file in (self.path / ".playground" / "issues").glob("*.md"):
            try:
                fields, _body = parse_front_matter(file.read_text(encoding="utf-8"))
            except OSError:
                continue
            fields.setdefault("number", file.stem)
            out.append(fields)
        out.sort(key=lambda i: -_int(i.get("number")))
        return out

    def month_cost(self) -> dict[str, float]:
        """This month's cost per service from `usage/services.jsonl` (the latest snapshot per period)."""
        from datetime import datetime
        month = datetime.now().strftime("%Y-%m")
        latest: dict[tuple[str, str], dict] = {}
        for row in read_jsonl(self.path / ".playground" / "usage" / "services.jsonl"):
            if not str(row.get("ts") or "").startswith(month) or row.get("cost") is None:
                continue
            latest[(str(row.get("service_id")), str(row.get("period") or ""))] = row
        totals: dict[str, float] = {}
        for (service_id, _period), row in latest.items():
            try:
                totals[service_id] = totals.get(service_id, 0.0) + float(row["cost"])
            except (TypeError, ValueError):
                continue
        return totals

    def attention(self) -> list[tuple[str, str, Any]]:
        """Quiet notes for the rail: ("2 open bugs", "bugs", None), ("Resend near budget", "stack", "resend").
        Kept until bugs, services or money change (thread and goal changes don't touch it)."""
        if getattr(self, "_attention_cache", None) is None:
            self._attention_cache = self._read_attention()
        return list(self._attention_cache)

    def forget_attention(self) -> None:
        self._attention_cache = None

    def _read_attention(self) -> list[tuple[str, str, Any]]:
        notes: list[tuple[str, str, Any]] = []
        open_bugs = [i for i in self.issues() if str(i.get("status") or "open") in ("open", "fixing")]
        services = _optional("core.services")
        money = None
        if services is not None:
            try:
                money = services.money_summary(self.path)
            except Exception:  # noqa: BLE001
                log.exception("services.money_summary failed")
        if open_bugs:
            urgent = [i for i in open_bugs if str(i.get("severity")) in ("high", "urgent")]
            words = f"{len(open_bugs)} open bug{'s' if len(open_bugs) != 1 else ''}"
            notes.append((words, "bugs", _int(urgent[0].get("number")) if len(open_bugs) == 1 and urgent else None))
        if money is not None:
            for row in money.get("services") or money.get("rows") or []:
                used = row.get("budget_used")
                if used is not None and used >= 0.8:
                    notes.append((f"{row.get('name')} {'over' if row.get('over_budget') else 'near'} budget",
                                  "stack", row.get("id")))
            return notes
        costs = self.month_cost()
        for service in self.services():
            budget = _money_value(service.get("monthly_budget"))
            spent = costs.get(str(service.get("id")))
            if budget and spent is not None and spent >= 0.8 * budget:
                over = spent >= budget
                notes.append((f"{service.get('name') or service.get('provider')} {'over' if over else 'near'} budget",
                              "stack", service.get("id")))
        return notes

    # threads
    def thread(self, goal_id: str) -> list[dict]:
        try:
            entries = [e.to_dict() for e in self.store.read_thread(goal_id, fold=True)]
        except (StoreError, ValueError):
            entries = []
        # A decision someone is waiting on shows even before its card is written.
        shown = {str((e.get("data") or {}).get("id")) for e in entries if e.get("kind") == "decision"}
        for request in self.pending():
            if request.get("goal") == goal_id and str(request.get("id")) not in shown:
                entries.append({"ts": request.get("asked_at"), "id": f"pending-{request['id']}", "role": "agent",
                                "kind": "decision", "by": request.get("asked_by") or "", "data": dict(request)})
        return entries

    def append(self, goal_id: str, entry: ThreadEntry) -> bool:
        try:
            self.store.append_thread(goal_id, entry)
            return True
        except (StoreError, OSError, ValueError):
            log.exception("could not write to the thread")
            return False

    # decisions and requests
    def pending(self, kind: str = "decision") -> list[dict]:
        try:
            return [_as_dict(p) for p in self.store.list_pending(kind=kind)]
        except (StoreError, OSError):
            return []

    def pending_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for request in self.pending():
            goal = str(request.get("goal") or "")
            if goal:
                counts[goal] = counts.get(goal, 0) + 1
        return counts

    def answer(self, decision_id: str, option: str, by: str) -> bool:
        self.forget()
        try:
            self.store.answer_decision(decision_id, option, by=by)
            return True
        except AlreadyAnswered:
            return True
        except NotFound:
            # Nobody is waiting through pending/ (an old card): record the answer as is.
            try:
                self.store.write_answer(Answer(id=decision_id, option=option, answered_by=by))
                return True
            except AlreadyAnswered:
                return True
            except (StoreError, OSError, ValueError):
                return False
        except (StoreError, OSError, ValueError):
            log.exception("could not record the answer")
            return False

    def create_goal(self, title: str, owner: str) -> str | None:
        self.forget()
        try:
            return self.store.create_goal(title, owner=owner, connection=self.default_id).id
        except (StoreError, OSError, ValueError):
            log.exception("could not create the goal")
            return None

    def set_status(self, goal_id: str, status: str) -> bool:
        self.forget()
        try:
            self.store.update_goal(goal_id, lambda g: setattr(g, "status", GoalStatus.parse(status)))
            return True
        except (StoreError, OSError, ValueError):
            return False


def _is_speed(option: dict) -> bool:
    words = f"{option.get('id', '')} {option.get('name', '')}".lower()
    return any(w in words for w in ("fast", "speed", "tier"))


def _action_part(value) -> str:
    """Make an agent-supplied id safe for a GAction name.

    The toolkit turns a command's id into a menu action (`menu.opt-…`). An id with
    a `:` (OpenCode model ids like `ollama-cloud/deepseek-v4-pro:0813`) makes an
    invalid *detailed* action name and crashes GTK when the menu is built. Only
    keep letters, digits, dash and dot; the caller adds an index so two choices
    that sanitise alike still get distinct actions.
    """
    text = re.sub(r"[^A-Za-z0-9.\-]", "_", str(value if value is not None else ""))
    return text or "_"


def _option_rank(option: dict) -> tuple:
    """Model, effort and speed first (what people change); then the rest as the agent listed them."""
    category = option.get("category") or ""
    order = {"model": 0, "thought_level": 1}.get(category, 2 if _is_speed(option) else 3)
    return (order,)


def _as_switch(choices: list[dict]) -> tuple | None:
    """Two choices that read as off and on (Off/On, Default/Plan) are one tick in the menu."""
    if len(choices) != 2:
        return None
    first = str(choices[0].get("name") or choices[0].get("value") or "").lower()
    if first in ("off", "default", "no", "disabled", "normal"):
        return choices[0].get("value"), choices[1].get("value")
    return None


def _int(value) -> int:
    try:
        return int(str(value).lstrip("#"))
    except (TypeError, ValueError):
        return 0


def _money_value(value) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group(0)) if match else None


def _stamp(value) -> float:
    moment = w.parse_ts(value)
    return moment.timestamp() if moment else 0.0


def projects_root() -> Path:
    paths = _optional("core.paths")
    if paths is not None:
        return Path(paths.projects_root())
    return Path.home() / "Documents" / "Playground Projects"


def list_projects(bus, connections: list[dict] | None = None) -> list[ProjectData]:
    project = _optional("core.project")
    folders: list[Path] = []
    if project is not None:
        try:
            folders = [Path(p.path) for p in project.list_projects(projects_root()) if p.path]
        except Exception:  # noqa: BLE001
            log.exception("could not list projects")
    projects = [ProjectData(folder, bus, connections) for folder in folders]
    projects.sort(key=lambda p: p.name.lower())
    return projects


def find_project_dir(path: str | Path) -> Path | None:
    current = Path(path).expanduser().resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".playground" / "project.toml").is_file():
            return candidate
    return None


# ── connections: who works ──────────────────────────────────────────────────

def local_connection_ids() -> set[str] | None:
    """The ids of the connections on this computer (None when they can't be read)."""
    module = _optional("agents.connections")
    if module is None:
        return None
    try:
        return {str(c.id) for c in module.ConnectionStore().list()}
    except Exception:  # noqa: BLE001
        log.debug("could not read the connections", exc_info=True)
        return None


def load_connections() -> list[dict]:
    module = _optional("agents.connections")
    found: list[dict] = []
    if module is not None:
        try:
            found = [dict(_as_dict(c), _object=c) for c in module.ConnectionStore().list()]
        except Exception:  # noqa: BLE001
            log.exception("could not read the connections")
    if not any(c.get("kind") == "ari" or c.get("id") == "ari" for c in found):
        found.insert(0, dict(ARI))
    return found


def _local_agent(connection) -> bool:
    """A connection that's an agent program here: not Ari or a bare local model, and not on another computer."""
    provider = str(_connection_field(connection, "provider", "") or "").lower()
    where = str(_connection_field(connection, "where", "this-computer") or "this-computer")
    return (_connection_field(connection, "kind", "") != "ari" and provider not in ("ari", "ollama", "")
            and where in ("this-computer", "local"))


def default_connection_id(found: list | None = None) -> str:
    """Where a new conversation starts: a real Ari connection first, else the first agent
    set up on this computer, else the Ari placeholder until someone connects one.

    Callers that already hold the connections (the window, its project rows) pass
    them in; only a one-off call reads the file. Ari's placeholder is included in
    the window's list, so it is handled here like any other connection."""
    if found is None:
        module = _optional("agents.connections")
        if module is None:
            return "ari"
        try:
            found = list(module.ConnectionStore().list())
        except Exception:  # noqa: BLE001 - no connections file means Ari, as before
            return "ari"
    ari = next((c for c in found if not _ari_placeholder(c) and (
                _connection_field(c, "kind") == "ari" or _connection_field(c, "id") == "ari")), None)
    if ari is not None:
        return str(_connection_field(ari, "id")) if _connection_field(ari, "id") != "ari" else "ari"
    agent = next((c for c in found if _local_agent(c)), None)
    return str(_connection_field(agent, "id")) if agent is not None else "ari"


def connection_subtitle(connection: dict) -> str:
    """What the composer's chip says under the agent's name: what's left, or what it runs on."""
    usage = _optional("core.usage")
    if usage is not None and connection.get("id"):
        try:
            head = usage.latest_headroom(connection["id"])
        except Exception:  # noqa: BLE001
            head = None
        if head and head.get("percent_left") is not None:
            return f"{float(head['percent_left']):.0f}% of the week left"
    kind = connection.get("kind")
    if kind == "ari" and connection.get("_object") is None and not connection.get("endpoint"):
        # Playground's own stand-in: nobody has connected Ari to anything on this computer yet.
        return "Not connected yet"
    if kind == "ari" and connection.get("endpoint") == "chatgpt":
        chatgpt = _optional("agents.chatgpt")
        state = chatgpt.status(str(connection.get("id"))) if chatgpt is not None else {}
        return {"signed-in": state.get("label") or "Using ChatGPT plan", "coming-soon": "ChatGPT plan, coming soon",
                "plan-off": "ChatGPT plan isn’t allowed"}.get(state.get("state"), "Sign in with ChatGPT")
    if kind == "ari":
        endpoint = connection.get("endpoint") or "luma-credits"
        return {"chatgpt": "Your ChatGPT plan", "ollama": "On this computer"}.get(endpoint, "Luma credits")
    if connection.get("plan"):
        return str(connection["plan"])
    return {"personal-plan": "Your plan", "api-key": "API key", "team-key": "Team key",
            "local": "On this computer"}.get(str(kind), "")


def plan_usage_url(connection: dict) -> str | None:
    """OpenAI's usage page while Ari runs on the person's ChatGPT plan (signed in), else None."""
    if connection.get("kind") != "ari" or connection.get("endpoint") != "chatgpt":
        return None
    chatgpt = _optional("agents.chatgpt")
    if chatgpt is None:
        return None
    try:
        state = chatgpt.status(str(connection.get("id")))
    except Exception:  # noqa: BLE001
        return None
    return state.get("manage_usage_url") if state.get("state") == "signed-in" else None


# ── the window ───────────────────────────────────────────────────────────────

class PlaygroundWindow(AppWindow):
    def __init__(self, application: Adw.Application) -> None:
        self.app = application
        super().__init__(application=application, app_id=APP_ID, title="Playground", icon_name=APP_ID,
                         commands=self._commands(), default_width=1400, default_height=860,
                         minimum_width=360, minimum_height=480, size_class="wide")
        self.add_css_class("pg-window")
        self._load_sheets()
        self.bus = getattr(application, "bus", None) or events.bus
        self.projects: list[ProjectData] = []
        self.project: ProjectData | None = None
        self.goal_id: str | None = None
        self.connections: list[dict] = load_connections()
        self._default_connection_id = default_connection_id(self.connections)
        self.runners: dict[tuple[str, str], Any] = {}
        self.agent_states: dict[str, str] = {}
        self._tokens: list[int] = []
        self._watcher = None
        self._heartbeat = 0
        self._sync_source = 0
        self._phone = False
        self._compact = False
        self._page: Gtk.Widget | None = None
        self._page_back = None
        self._answering: set[str] = set()
        self._unread: set[str] = set()      # conversations with news the person hasn't opened

        self._build_title()
        self._build_body()
        self._build_breakpoints()
        self._subscribe()
        self.connect("close-request", self._closing)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)

        self.reload_projects()
        self._reap_previews()
        start = self.recall("project")
        target = next((p for p in self.projects if str(p.path) == start), None) if start else None
        target = target or (self.projects[0] if self.projects else None)
        if target is not None:
            self._show_project(target, self.recall("goal"))
        else:
            self._show_welcome()
        GLib.timeout_add(600, self._offer_setup)

    def _offer_setup(self) -> bool:
        """Nothing gets built without an agent: the first time there's none, Get Playground ready says how to
        connect one (sheets/setup.py). PLAYGROUND_SETUP=1 shows it anyway."""
        from .sheets import setup
        if os.environ.get("PLAYGROUND_SETUP") == "1" or setup.needed():
            self.open_sheet("setup")
        return GLib.SOURCE_REMOVE

    # ── style ─────────────────────────────────────────────────────────────
    def _load_sheets(self) -> None:
        """Playground's sheets, re-read whenever the treatment changes so every @luma_* colour follows it.
        (Loaded here too because the kit's colours exist only once AppWindow has installed them.)"""
        display = Gdk.Display.get_default()
        if display is None or getattr(display, "_pg_sheets", False):
            return
        for name in ("playground.css", "cards.css", "sheets.css"):
            path = _data_dir(name)
            if path.is_file():
                add_style_sheet(str(path))
        icons = _data_dir("icons")
        if icons.is_dir():
            theme = Gtk.IconTheme.get_for_display(display)
            if str(icons) not in (theme.get_search_path() or []):
                theme.add_search_path(str(icons))
        display._pg_sheets = True

    # ── commands: the identity menu and the shortcuts ─────────────────────
    def _commands(self) -> CommandRegistry:
        has_project = lambda: getattr(self, "project", None) is not None  # noqa: E731
        return CommandRegistry((
            CommandGroup("Project", (
                Command("goal.new", "New goal", lambda: self.new_goal(), icon="pg-goal-symbolic",
                        shortcut=("Ctrl", "N"), enabled=has_project),
                Command("project.search", "Search", lambda: self.show_search(), icon="pg-search-symbolic",
                        shortcut=("Ctrl", "K"), enabled=has_project),
                Command("project.new", "New project", lambda: self.new_project(), icon="pg-plus-symbolic"),
                Command("project.book", "Project space", lambda: self.open_sheet("book"), icon="pg-book-open-symbolic",
                        shortcut=("Ctrl", "B"), enabled=has_project),
                Command("project.files", "Show in Filer", lambda: self.show_in_files(), icon="pg-folder-symbolic",
                        enabled=has_project),
            )),
            CommandGroup("Team", (
                Command("team.people", "People", lambda: self.open_sheet("people"), icon="pg-users-symbolic",
                        enabled=has_project),
                Command("team.places", "Places", lambda: self.open_sheet("places"), icon="pg-server-symbolic",
                        enabled=has_project),
                Command("team.usage", "Usage and money", lambda: self.open_sheet("usage"), icon="pg-gauge-symbolic",
                        enabled=has_project),
            )),
            CommandGroup("Stage", (
                Command("stage.toggle", "Show the Stage", lambda: self.toggle_stage(), icon="pg-smartphone-symbolic",
                        shortcut=("Ctrl", "Shift", "S"), enabled=has_project),
                Command("stage.point", "Point at something", lambda: self.point(),
                        icon="pg-mouse-pointer-click-symbolic", shortcut=("Ctrl", "Shift", "P"), enabled=has_project),
                Command("stage.reload", "Reload the Stage", lambda: self.stage.reload(), icon="pg-refresh-cw-symbolic",
                        shortcut=("Ctrl", "R"), enabled=has_project),
            )),
        ))

    # ── the title row ─────────────────────────────────────────────────────
    def _build_title(self) -> None:
        content = w.box("h", 0, "pg-title-content", valign=Gtk.Align.CENTER)
        divider = Gtk.Box(valign=Gtk.Align.CENTER)
        divider.add_css_class("pg-title-divider")
        content.append(divider)
        self.project_button = Gtk.Button(valign=Gtk.Align.CENTER)
        self.project_button.add_css_class("pg-project-button")
        self.project_button.set_tooltip_text("Open the Project space")
        self.project_button.connect("clicked", lambda _b: self.open_sheet("book"))
        line = w.box("h", 8)
        self.project_mark_slot = w.box("h", 0, valign=Gtk.Align.CENTER)
        line.append(self.project_mark_slot)
        self.project_name = w.label("", "pg-project-name")
        line.append(self.project_name)
        self.project_book_words = w.label("Project space", "pg-project-book")
        line.append(self.project_book_words)
        line.append(w.icon("book-open", 15, "pg-project-book-icon"))
        self.project_button.set_child(line)
        content.append(self.project_button)
        self.title_content = content
        self.set_title_content(content)
        # The sidebar's picker names the project; the title bar stays clear.
        content.set_visible(False)

        trailing = w.box("h", 4, "pg-title-trailing", valign=Gtk.Align.CENTER)
        # Shared projects: "Up to date" / "Syncing" / "Offline, will catch up" (hides itself otherwise).
        self.sync_indicator = sync_ui.SyncIndicator(self) if sync_ui is not None else None
        if self.sync_indicator is not None:
            trailing.append(self.sync_indicator)
        self.people_button = Gtk.Button()
        self.people_button.add_css_class("pg-tb")
        self.people_button.add_css_class("pg-people")
        self.people_button.connect("clicked", lambda _b: self.open_sheet("people"))
        self.people_words = None
        trailing.append(self.people_button)
        self.places_button = self._title_button("server", "Places", lambda: self.open_sheet("places"))
        self.places_dot = w.dot("good")
        self.places_button.get_child().append(self.places_dot)
        trailing.append(self.places_button)
        self.usage_button = self._title_button("gauge", "Usage", lambda: self.open_sheet("usage"))
        self.usage_meter = w.Meter(0.0, width=26, height=4, accessible="Usage this week")
        self.usage_meter.add_css_class("pg-mini")
        self.usage_button.get_child().append(self.usage_meter)
        trailing.append(self.usage_button)
        self.stage_button = Gtk.ToggleButton()
        self.stage_button.add_css_class("pg-tb")
        stage_line = w.box("h", 7)
        stage_line.append(w.icon("message-square", 16))
        self.stage_words = w.label("Conversation", "pg-tb-label")
        stage_line.append(self.stage_words)
        self.stage_dot = w.dot("good")
        self.stage_dot.set_visible(False)
        stage_line.append(self.stage_dot)
        self.stage_button.set_child(stage_line)
        self.stage_button.set_tooltip_text("Show the conversation")
        self.stage_button.update_property([Gtk.AccessibleProperty.LABEL], ["Conversation"])
        self.stage_button.set_visible(False)
        self.stage_button.connect("toggled", lambda b: self._stage_toggled(b.get_active()))
        trailing.append(self.stage_button)
        self.trailing = trailing
        self.set_trailing(trailing)

    def _title_button(self, glyph: str, text: str, callback) -> Gtk.Button:
        button = Gtk.Button()
        button.add_css_class("pg-tb")
        line = w.box("h", 7)
        line.append(w.icon(glyph, 16))
        line.label_widget = w.label(text, "pg-tb-label")
        line.append(line.label_widget)
        button.set_child(line)
        button.set_tooltip_text(text)
        button.update_property([Gtk.AccessibleProperty.LABEL], [text])
        button.connect("clicked", lambda _b: callback())
        return button

    def _fill_people(self) -> None:
        self._sync_faces()
        line = w.box("h", 8)
        you = self.project.you() if self.project else ""
        others = [m for m in (self.project.members if self.project else []) if m.get("id") != you]
        if others:
            faces = w.box("h", 0, "pg-faces", valign=Gtk.Align.CENTER)
            for member in others[:3]:
                faces.append(w.Presence(str(member.get("name") or "?"), 24, here=bool(member.get("here"))))
            line.append(faces)
        else:
            line.append(w.icon("users", 16))
        self.people_words = w.label("People", "pg-tb-label")
        self.people_words.set_visible(not self._compact)
        line.append(self.people_words)
        self.people_button.set_child(line)
        names = ", ".join(str(m.get("name")).split()[0] for m in others[:3])
        self.people_button.set_tooltip_text(f"People: you, {names}" if names else "People")
        self.people_button.update_property([Gtk.AccessibleProperty.LABEL], ["People"])

    def _sync_faces(self) -> None:
        """Members' photos for every face drawn by name: the rail, people chips, banners, cards, sheets."""
        photos: dict[str, str] = {}
        if self.project is not None:
            members = self.project.members
            firsts = [str(m.get("name") or "").split()[0].lower() for m in members if str(m.get("name") or "").split()]
            for member in members:
                path = self.project.store.member_photo(member)
                if path is None:
                    continue
                name = str(member.get("name") or "")
                keys = [name, member.get("id")]
                if name.split() and firsts.count(name.split()[0].lower()) == 1:
                    keys.append(name.split()[0])   # cards that say just "Priya"
                for key in keys:
                    if key:
                        photos[str(key)] = str(path)
        w.set_faces(photos)

    def _fill_places(self) -> None:
        """The title row's Places dot: green when every server answered its last check, amber when one didn't,
        and no dot while nothing has been checked (it never claims what it doesn't know)."""
        tests = {}
        if self.project is not None:
            try:
                import json
                tests = json.loads((self.project.path / ".playground" / "local" / "places-status.json")
                                   .read_text(encoding="utf-8"))
            except (OSError, ValueError):
                tests = {}
        servers = [p for p in (self.project.places if self.project else []) if p.get("kind") == "ssh"]
        results = [tests.get(p.get("id")) for p in servers if isinstance(tests, dict)]
        checked = [r for r in results if isinstance(r, dict)]
        for kind in ("good", "warn"):
            self.places_dot.remove_css_class(kind)
        if any(not r.get("ok") for r in checked):
            self.places_dot.add_css_class("warn")
            words = "Places · a server didn’t answer"
        elif checked and len(checked) == len(servers):
            self.places_dot.add_css_class("good")
            words = "Places · every server answered"
        else:
            words = "Places · not checked yet" if servers else "Places"
        self.places_dot.set_visible(bool(checked))
        self.places_button.set_tooltip_text(words)

    def _fill_usage(self) -> None:
        """The title row's little meter: how much of this goal's connection is used this week."""
        usage = _optional("core.usage")
        fraction = None
        agent = self._agent_for(self.goal_id) if self.project is not None else None
        if usage is not None and agent and agent.get("id"):
            try:
                head = usage.latest_headroom(agent["id"])
                if head and head.get("percent_left") is not None:
                    fraction = 1.0 - float(head["percent_left"]) / 100.0
                else:
                    totals = usage.week_totals(agent["id"])
                    cap = usage.get_cap(agent["id"]) or {}
                    if cap.get("weekly_cost") and totals.get("cost_known"):
                        fraction = float(totals["cost"]) / float(cap["weekly_cost"])
            except Exception:  # noqa: BLE001
                fraction = None
        tooltip = "Usage"
        services = _optional("core.services")
        if fraction is None and services is not None and self.project is not None:
            # No reading for the agent: the project's month against its budgets instead.
            try:
                total = services.money_summary(self.project.path).get("total") or {}
                if total.get("budget"):
                    fraction = float(total["cost"]) / float(total["budget"])
                tooltip = f"Usage · {total.get('summary')}" if total.get("summary") else tooltip
            except Exception:  # noqa: BLE001
                fraction = None
        self.usage_meter.set_visible(fraction is not None)
        if fraction is not None:
            self.usage_meter.set_fraction(fraction)
            (self.usage_meter.add_css_class if fraction >= 0.85 else self.usage_meter.remove_css_class)("warn")
        self.usage_button.set_tooltip_text(tooltip)
        self.rail.set_usage(fraction, tooltip)

    # ── the body: rail | conversation | Stage ─────────────────────────────
    def _build_body(self) -> None:
        # The sidebar: the project picker, the areas, and the conversations (sidebar.py).
        self.rail = Sidebar()
        remembered = self.recall("area")
        remembered = self.AREA_FOR.get(remembered, remembered)
        self.rail.area = remembered if remembered in AREA_KEYS else "tasks"
        self.rail.connect("area-chosen", lambda _r, key: self.show_area(key))
        self.rail.connect("search", lambda _r: self.show_search())
        self.rail.connect("usage", lambda _r: self.open_sheet("usage"))
        self.rail.connect("conversation-chosen", lambda _r, goal_id: self.open_conversation(goal_id))
        self.rail.connect("new-conversation", lambda _r: self.new_goal())
        self.rail.connect("project-chosen", lambda _r, path: self.open_project(path))
        self.rail.connect("new-project", lambda _r: self.new_project())
        self.rail.connect("new-subproject", lambda _r: self.new_subproject())
        self.rail.connect("open-folder", lambda _r: self.open_folder())
        self.rail.connect("project-menu", lambda _r, button: self._project_menu(button))
        self.rail.connect("conversation-menu", lambda _r, goal_id, row, x, y: self._conversation_menu(goal_id, row, x, y))

        # The middle: the area you're in. It stays put while conversations come and go on the right.
        self.area_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.area_island = Island()
        self.area_island.add_css_class("pg-area-island")
        self.area_island.append(self.area_holder)
        self.area_host = ToastHost(self.area_island)
        self.area_center = ActionCenter().attach(self.area_host)
        self.space = None          # the Project space's sections, shown one at a time (sheets/book.py)

        self.conversation = Conversation(_data_dir("org.projectluma.Playground.svg"))
        self.conversation.welcome.connect("start", lambda _w, text: self._start_new_project(text))
        self.conversation.welcome.connect("join", lambda _w: self.open_sheet("people"))
        self.composer = Composer()
        self.composer.connect("send", self._send)
        self.composer.connect("stop", lambda _c: self.stop_conversation())
        self.composer.connect("pick-agent", lambda c: self.open_sheet("picker", goal_id=self.goal_id,
                                                                       anchor=None if self._phone else c.agent))
        self.composer.connect("pick-options", lambda c: self._options_menu())
        self.conversation.set_composer(self.composer)
        self.conversation_island = Island()
        self.conversation_island.add_css_class("pg-conversation-island")
        self.conversation_island.append(self.conversation)

        self.stage = Stage()
        self.stage.connect("point", lambda _s, about: self._pointed(about))
        self.stage.connect("recorded", lambda _s, folder, words: self.composer.add_attachment(folder, words, "recording"))
        self.stage.connect("expand", lambda _s, wide: self._stage_wide(wide))
        self.stage.connect("close", lambda _s: self.hide_stage())
        self.stage.connect("open-sheet", lambda _s, name: self.open_sheet(name))
        self.stage.connect("restart", lambda _s: self.restart_preview())
        self.stage.connect("stop", lambda _s: self.stop_preview())
        self.stage.connect("refill", lambda _s: self.restart_preview())
        self.stage.connect("report", lambda _s, error: self.report_preview(error))
        self.stage_island = Island()
        self.stage_island.add_css_class("pg-stage-island")
        self.stage_island.append(self.stage)

        # Product is the Stage (the live app); every other area is the Project space's section.
        self.middle = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                                transition_duration=0 if w.reduced_motion() else 120, hexpand=True, vexpand=True)
        self.middle.add_named(self.area_host, "area")
        self.middle.add_named(self.stage_island, "preview")

        # The conversation stands on the right, beside the area (Helm's chat column).
        self.split = IslandOverlaySplitView(sidebar_position=Gtk.PackType.END, min_sidebar_width=380,
                                            max_sidebar_width=460, sidebar_width_fraction=0.36,
                                            enable_show_gesture=False, enable_hide_gesture=True)
        self.split.add_css_class("pg-split")
        self.split.set_content(self.middle)
        self.split.set_sidebar(self.conversation_island)
        self.split.connect("notify::collapsed", lambda *_: self._split_changed())
        self.split.connect("notify::show-sidebar", lambda *_: self._split_changed())

        # A page (the Project book, the code, a review) takes the room the conversation and Stage share.
        # The page brings its own island (sheets/common.Sheet), so this is only where it stands.
        self.page_island = Gtk.Box(hexpand=True, vexpand=True)
        self.page_island.add_css_class("pg-page-host")
        self.work = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                              transition_duration=0 if w.reduced_motion() else 150, hexpand=True, vexpand=True)
        self.work.add_named(self.split, "main")
        self.work.add_named(self.page_island, "page")

        self.sheet = Adw.BottomSheet(show_drag_handle=True, modal=True, full_width=True, can_open=False)
        self.sheet.add_css_class("pg-stage-sheet")
        self.sheet.set_content(self.work)
        self.sheet.connect("notify::open", lambda *_: self._sheet_moved())

        body = w.box("h", 0, "pg-body", hexpand=True, vexpand=True)
        # The sidebar is as wide as it must be and no wider: long conversation titles (cut short with …) and
        # "Working · Codex 2" mustn't make it grow each time the list is drawn again.
        rail_width = Adw.Clamp(orientation=Gtk.Orientation.HORIZONTAL, maximum_size=RAIL_WIDTH,
                               tightening_threshold=RAIL_WIDTH, child=self.rail, hexpand=False)
        self.rail_clamp = rail_width
        body.append(rail_width)
        body.append(self.sheet)
        self.set_body(body)

        # On a phone the rail is the title island's menu (v71 lNavTtl): ☰ | goal, project · state.
        self.island = TitleIsland("Playground", "", lead="menu", grow=self.rail.sidebar, grows="menu",
                                  phone_only=False)
        self.island.set_visible(False)
        self.island.float_over(self.layer_host)
        self.island.connect("grown", lambda _i, grown: self.phone_stage.set_visible(
            self._phone and not grown and self.project is not None and self._page is None))
        self.connect("notify::default-height", lambda *_: self._size_sheet())
        self.phone_stage = Gtk.Button(halign=Gtk.Align.END, valign=Gtk.Align.START, visible=False)
        self.phone_stage.add_css_class("pg-phone-stage")
        line = w.box("h", 8)
        line.append(w.icon("message-square", 18))
        line.append(w.label("Chat"))
        self.phone_stage_dot = w.dot("good")
        self.phone_stage_dot.set_visible(False)
        line.append(self.phone_stage_dot)
        self.phone_stage.set_child(line)
        self.phone_stage.update_property([Gtk.AccessibleProperty.LABEL], ["Show the conversation"])
        self.phone_stage.connect("clicked", lambda _b: self.show_conversation())
        self.layer_host.add_overlay(self.phone_stage)
        self.layer_host.set_measure_overlay(self.phone_stage, False)

    def _build_breakpoints(self) -> None:
        compact = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(f"max-width: {WIDE - 1}px"))
        compact.add_setter(self.split, "collapsed", True)
        compact.connect("apply", lambda _b: self._set_compact(True))
        compact.connect("unapply", lambda _b: self._set_compact(False))
        self.add_breakpoint(compact)
        phone = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(f"max-width: {PHONE - 1}px"))
        phone.add_setter(self.split, "collapsed", True)
        phone.add_setter(self.trailing, "visible", False)
        phone.add_setter(self.rail, "visible", False)
        phone.add_setter(self.split, "min-sidebar-width", 0)
        phone.add_setter(self.island, "visible", True)
        phone.connect("apply", lambda _b: self._set_phone(True))
        phone.connect("unapply", lambda _b: self._set_phone(False))
        self.add_breakpoint(phone)

    def _set_compact(self, compact: bool) -> None:
        self._compact = compact
        (self.add_css_class if compact else self.remove_css_class)("pg-compact")
        for label in (self.places_button.get_child().label_widget, self.usage_button.get_child().label_widget,
                      self.stage_words, self.people_words, self.project_book_words):
            if label is not None:
                label.set_visible(not compact)
        # Narrower than 1180 the conversation floats in over the area; the title's button brings it.
        self.stage_button.set_visible(compact and self.project is not None and not self._phone)
        self.split.set_show_sidebar(not compact)
        if not compact:
            self.stage_button.set_active(False)
        self._split_changed()
        self._size_area()

    def _set_phone(self, phone: bool) -> None:
        self._phone = phone
        (self.add_css_class if phone else self.remove_css_class)("pg-phone")
        self.composer.set_phone(phone)
        self.rail.set_phone(phone)
        self.stage.set_narrow(phone)
        self.phone_stage.set_visible(phone and self.project is not None and self._page is None)
        self.stage_button.set_visible(self._compact and not phone and self.project is not None)
        if phone:
            # On a phone the area is the page; the conversation is a sheet one tap away.
            if self.split.get_sidebar() is self.conversation_island:
                self.split.set_sidebar(None)
            self.sheet.set_sheet(self.conversation_island)
            self.sheet.set_can_open(True)
        else:
            self.sheet.set_open(False)
            self.sheet.set_sheet(None)
            self.sheet.set_can_open(False)
            if self.split.get_sidebar() is None:
                self.split.set_sidebar(self.conversation_island)
        self._size_area()
        self._sync_island()

    def _size_area(self) -> None:
        host = getattr(self, "space_host", None)
        if host is not None:
            host.set_sizes(phone=self._phone, compact=self._compact)

    def _sheet_moved(self) -> None:
        """While the conversation sheet is up it is the screen: the title island and its button step aside."""
        up = self.sheet.get_open()
        self._sync_island()
        self.island.set_visible(self._phone and not up)
        self.phone_stage.set_visible(self._phone and not up and self.project is not None and self._page is None)

    def _size_sheet(self) -> None:
        return

    def _split_changed(self) -> None:
        floating = self.split.get_collapsed() and not self._phone
        (self.conversation_island.add_css_class if floating else self.conversation_island.remove_css_class)("floating")
        if floating and self.stage_button.get_active() != self.split.get_show_sidebar():
            self.stage_button.set_active(self.split.get_show_sidebar())

    # ── the conversation, shown beside the area (or over it when narrow) ──
    def show_conversation(self) -> None:
        if self._page is not None:
            self.hide_page(self._page)
        if self._phone:
            self.sheet.set_open(True)
        elif self._compact:
            self.split.set_show_sidebar(True)

    def hide_conversation(self) -> None:
        if self._phone:
            self.sheet.set_open(False)
        elif self._compact:
            self.split.set_show_sidebar(False)

    # ── the Stage is the Product area ─────────────────────────────────────
    def show_stage(self) -> None:
        self.show_area("preview")
        if self._phone:
            self.sheet.set_open(False)
        elif self._compact:
            self.split.set_show_sidebar(False)

    def hide_stage(self) -> None:
        if self.rail.area == "preview":
            self.show_area(getattr(self, "_last_area", None) or "tasks")

    def toggle_stage(self) -> None:
        if self.rail.area == "preview":
            self.hide_stage()
        else:
            self.show_stage()

    def _stage_toggled(self, active: bool) -> None:
        # The title's button: the conversation, when it floats.
        if self._compact and self.split.get_show_sidebar() != active:
            self.split.set_show_sidebar(active)

    def _stage_wide(self, wide: bool) -> None:
        return            # the Stage already fills the middle

    def point(self) -> None:
        self.show_stage()
        self.stage.set_pointing(not self.stage.pointing)

    def _pointed(self, about: str) -> None:
        self.composer.set_about(about)
        self.show_conversation()

    # ── areas: the middle of the window ───────────────────────────────────
    SECTION_FOR = {"tasks": "tasks", "bugs": "bugs", "knowledge": "knowledge", "stack": "stack",
                   "analytics": "analytics", "activity": "history", "team": "people", "repository": "repository"}
    AREA_FOR = {"history": "activity", "people": "team", "issues": "bugs", "goals": "tasks", "decisions": "knowledge",
                "brief": "knowledge", "places": "stack", "money": "stack", "overview": "tasks", "code": "repository",
                "product": "preview", "stage": "preview"}

    def show_area(self, key: str, item=None) -> None:
        """Open an area in the middle of the window; the conversation on the right stays as it is."""
        key = key if key in AREA_KEYS else self.AREA_FOR.get(key, "tasks")
        if key == "files" and self.project is not None:
            # The project's files: the code page, for the whole project rather than one conversation's change.
            self.open_sheet("code", goal="", back_label=self.project.name, terminal="closed")
            self.rail.select_area("files")
            return
        if self._page is not None:
            self.hide_page(self._page)
        if key != "preview":
            self._last_area = key
        self.rail.select_area(key)
        self.remember("area", key)
        if key == "preview":
            self.area_center.hide_bar()
            self.middle.set_visible_child_name("preview")
        else:
            self.middle.set_visible_child_name("area")
            space = self._ensure_space()
            if space is not None:
                space.go(self.SECTION_FOR.get(key, key), item=item)
        if self._phone:
            self.sheet.set_open(False)
            if self.island.grown:
                self.island.fold()
        self._sync_island()

    def show_search(self) -> None:
        """Search everywhere: the area's bar becomes the field, the area shows what matches."""
        if self.project is None:
            return
        if self._page is not None:
            self.hide_page(self._page)
        self.middle.set_visible_child_name("area")
        space = self._ensure_space()
        if space is None:
            return
        self.rail.select_area(None)
        space.go("search")
        if self._phone:
            self.sheet.set_open(False)
        self._sync_island()

    def _ensure_space(self):
        """The Project space for the open project, standing in the area island (one per project)."""
        if self.project is None:
            return None
        if self.space is not None and getattr(self.space, "project_path", None) == self.project.path:
            return self.space
        from .sheets import book
        if self.space is not None:
            self.space_host.drop()
        self.space_host = book.AreaHost(self, self.area_holder)
        self.space_host.center = self.area_center
        self.space_host.set_sizes(phone=self._phone, compact=self._compact)
        self.space = book.ProjectSpace(self, {"project_dir": self.project.path,
                                              "section": self.SECTION_FOR.get(self.rail.area, "tasks")},
                                       host=self.space_host)
        self.space.project_path = self.project.path
        return self.space

    def _area_counts(self) -> dict:
        """The sidebar's badges: open tasks, open bugs (red when one is high), rules waiting to be kept."""
        project = self.project
        if project is None:
            return {}
        try:
            tasks = [t for t in project.store.list_tasks(include_closed=False)]
        except (StoreError, OSError):
            tasks = []
        bugs = [i for i in project.issues() if i.get("status", "open") not in ("fixed", "wont-fix")]
        try:
            from ..core.model import knowledge_kept
            waiting = [n for n in project.store.list_knowledge() if not knowledge_kept(n)]
        except (StoreError, OSError):
            waiting = []
        return {"tasks": (len(tasks), any(t.status == "doing" for t in tasks)),
                "bugs": (len(bugs), any(i.get("severity") in ("high", "urgent") for i in bugs)),
                "knowledge": (len(waiting), bool(waiting))}

    def open_conversation(self, goal_id: str) -> None:
        self.select_goal(goal_id)
        self.show_conversation()

    # ── pages: the Project book, the code, a review (sheets/* put them here) ─
    def show_page(self, widget: Gtk.Widget, on_back=None) -> None:
        if self._page is not None and self._page is not widget:
            self.page_island.remove(self._page)
        self._page, self._page_back = widget, on_back
        if widget.get_parent() is None:
            widget.set_hexpand(True)
            widget.set_vexpand(True)
            self.page_island.append(widget)
        self.work.set_visible_child_name("page")
        self.rail.select(None)
        self.phone_stage.set_visible(False)
        # On a phone the title island says where you are now: the page, not the goal behind it.
        GLib.idle_add(self._title_page, priority=GLib.PRIORITY_DEFAULT)

    def _title_page(self) -> bool:
        from .sheets import common
        sheet = common.showing(self)
        words = {"book": "Project space", "code": "Code", "review": "Review and release"}.get(
            getattr(sheet, "name", ""), "")
        if words and self.project is not None and self._page is not None:
            self.island.set_title(words, self.project.name)
        return GLib.SOURCE_REMOVE
        if self.island.grown:
            self.island.fold()

    def hide_page(self, widget: Gtk.Widget | None = None) -> None:
        if widget is not None and widget is not self._page:
            return
        if self._page is not None and self._page.get_parent() is self.page_island:
            self.page_island.remove(self._page)
        self._page, self._page_back = None, None
        self.work.set_visible_child_name("main")
        self.rail.select(self.goal_id)
        self.phone_stage.set_visible(self._phone and self.project is not None)
        self._sync_island()

    # ── projects ──────────────────────────────────────────────────────────
    def reload_projects(self) -> None:
        self.projects = list_projects(self.bus, self.connections)

    def default_connection_id(self) -> str:
        """The cached choice for new conversations and goals with no agent set."""
        return self._default_connection_id

    def refresh_connections(self) -> None:
        """Reload the connections file once and share the result with every project row."""
        self.connections = load_connections()
        self._default_connection_id = default_connection_id(self.connections)
        for project in self.projects:
            project.set_connections(self.connections)
        if self.project is not None:
            self._schedule_sync()

    def _on_connections_changed(self, **payload) -> None:
        self.refresh_connections()

    def _reap_previews(self) -> None:
        """A Playground that was killed can't stop its Stage previews: stop what it left behind (core.preview
        records which processes it started). On a thread, before this run starts its own."""
        preview = _optional("core.preview")
        reap = getattr(preview, "reap_stale", None)
        if reap is None:
            return
        folders = [p.path for p in self.projects]

        def work() -> None:
            for folder in folders:
                try:
                    reap(folder)
                except Exception:  # noqa: BLE001
                    log.exception("couldn't tidy up old previews in %s", folder)
        thread = threading.Thread(target=work, daemon=True, name="preview-reap")
        thread.start()
        self._reaping = thread

    def open_project(self, path: str) -> None:
        """Open a project folder, or the project a file inside it belongs to (app.open)."""
        folder = find_project_dir(path)
        if folder is None:
            self._toast("That isn’t a Playground project")
            return
        self.reload_projects()
        project = next((p for p in self.projects if p.path.resolve() == folder), None)
        if project is None:
            project = ProjectData(folder, self.bus, self.connections)
            self.projects.append(project)
        self._show_project(project, None)

    def _show_project(self, project: ProjectData, goal_id: str | None) -> None:
        if self.project is not None and self.project.path != project.path:
            self._leave_project()
        self.project = project
        self.composer.attach_dir = Path(project.path) / ".playground" / "local" / "attachments"
        # However it was opened (the sidebar, or reopened at start), its files follow this Playground: AGENTS.md's
        # own sections, the guide, the indexes and the journal. Cheap, and off the main loop.
        core_project = _optional("core.project")
        working = {goal for (path, goal), runner in self.runners.items()
                   if path == str(project.path) and getattr(runner, "busy", False)}

        def bring_up_to_date() -> None:
            if core_project is not None:
                _quietly(core_project.upgrade_layout, project.path)
            # Steps a closed Playground left "running": this computer's agents', where nothing runs now.
            mine = {str(_connection_field(c, "id")) for c in self.connections}
            for goal in project.store.list_goals():
                if goal.id not in working:
                    _quietly(project.store.close_stale_steps, goal.id, mine)
        threading.Thread(target=bring_up_to_date, daemon=True, name="upgrade-layout").start()
        self.middle.set_visible(True)
        self.split.set_max_sidebar_width(460)
        self.split.set_sidebar_width_fraction(0.36)
        project.ensure_chat()
        self.remember("project", str(project.path))
        self.set_identity_subtitle(project.name)
        w.clear(self.project_mark_slot)
        self.project_mark_slot.append(w.project_mark(project.name, project.path, 20))
        self.project_name.set_label(project.name)
        # The sidebar's picker names the project and its areas replace the Project space and Places buttons.
        self.title_content.set_visible(False)
        # People live in the Team area and usage at the sidebar's foot: the title bar stays clear.
        for button in (self.people_button, self.usage_button, self.places_button):
            button.set_visible(False)
        self.stage_button.set_visible(self._compact and not self._phone)
        self.phone_stage.set_visible(self._phone)
        self._fill_people()
        self._fill_places()
        goals = project.goals()
        ids = {g["id"] for g in goals}
        if goal_id not in ids:
            goal_id = next((g["id"] for g in goals if g["status"] == "needs-you" and not g["is_chat"]), None) \
                or next((g["id"] for g in goals if g["status"] in ("working", "waiting", "ready") and not g["is_chat"]), None) \
                or (goals[0]["id"] if goals else GENERAL_CHAT_ID)
        self.rail.current_goal = goal_id
        self.rail.show(self.projects, project, goals, self._agent_for(GENERAL_CHAT_ID), counts=self._area_counts(),
                       status=self._project_status(project), live=bool(self._live_url()))
        self.stage.set_project(str(project.path), project.name, live_url=self._live_url(),
                               where_text=self._stage_where())
        self.stage.set_live_services(self._live_services())
        self._start_watching(project)
        if sync_ui is not None:
            try:
                sync_ui.start_sync(self, project.path)
                if self.sync_indicator is not None:
                    self.sync_indicator.set_project(project.path)
            except Exception:  # noqa: BLE001 - sharing never stops a project opening
                log.exception("sharing didn't start")
        self._start_preview(project)
        self.select_goal(goal_id)
        self.show_area(self.rail.area)

    def _live_url(self) -> str:
        preview = _optional("core.preview")
        if preview is None or self.project is None:
            return ""
        try:
            live = preview.stage_and_live(self.project.path).get("live")
        except Exception:  # noqa: BLE001
            live = None
        return str((live or {}).get("url") or "")

    def _live_services(self) -> list[str]:
        """What Live runs on, for the Stage's line: AWS Lightsail · PostgreSQL."""
        if self.project is None:
            return []
        names = []
        for service in self.project.services():
            environments = service.get("environments") or {}
            if isinstance(environments, dict) and environments.get("live") and \
                    service.get("kind") in ("hosting", "database", "storage"):
                names.append(str(service.get("name") or service.get("provider")))
        return names[:3]

    def _project_status(self, project) -> str:
        """Under the project's name in the picker: Live, or where it is, and who's working right now."""
        working = [g for g in project.goals() if not g.get("is_chat") and
                   self.agent_states.get(g["id"], "") in BUSY_STATES]
        where = "Live" if self._live_url() else "On this computer"
        if working:
            return f"{where} · {len(working)} working"
        return where

    def new_subproject(self) -> None:
        """A project inside this one (the notes app inside Luma): its own areas, the parent's a read away."""
        if self.project is None:
            return
        parent = self.project
        dialog = Adw.AlertDialog(heading=f"New sub-project in {parent.name}",
                                 body="A part of the project with its own tasks, bugs and conversations, so work on "
                                      "it stays focused. Agents there can still read and change the whole project.")
        entry = Gtk.Entry(placeholder_text="Calculator", activates_default=True)
        entry.update_property([Gtk.AccessibleProperty.LABEL], ["Sub-project name"])
        where = Gtk.Entry(placeholder_text="Folder, if not its name: src/apps/calculator", activates_default=True)
        where.update_property([Gtk.AccessibleProperty.LABEL], ["Its folder inside the project"])
        fields = w.box("v", 8)
        fields.append(entry)
        fields.append(where)
        dialog.set_extra_child(fields)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("make", "Make it")
        dialog.set_response_appearance("make", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("make")

        def chosen(_d, response: str) -> None:
            name = entry.get_text().strip()
            if response != "make" or not name:
                return
            from ..core.paths import safe_project_name
            from ..core.project import adopt_folder
            # Its folder: a new one, or one that already holds the part's code (that stays as it is).
            folder = (parent.path / (where.get_text().strip().strip("/") or safe_project_name(name))).resolve()
            try:
                folder.relative_to(parent.path.resolve())
                if folder == parent.path.resolve():
                    raise ValueError("Give the part a folder of its own inside the project.")
                folder.mkdir(parents=True, exist_ok=True)
                made = adopt_folder(folder, name, bus=self.bus)
            except ValueError:
                self._toast("That folder isn’t inside the project.")
                return
            except Exception as error:  # noqa: BLE001 - said plainly, never a crash
                self._toast(str(error))
                return
            self.reload_projects()
            self.open_project(str(made.path))

        dialog.connect("response", chosen)
        dialog.present(self)

    def open_folder(self) -> None:
        """A folder that already holds work (a repository, an app inside one) becomes a project where it is.
        Nothing in it is replaced; inside another project, it's a sub-project of it."""
        dialog = Gtk.FileDialog(title="Open a folder as a project", modal=True)

        def chosen(dlg, result) -> None:
            try:
                picked = dlg.select_folder_finish(result)
            except GLib.Error:
                return                                   # dismissed
            path = picked.get_path() if picked is not None else None
            if not path:
                return
            from ..core.project import adopt_folder
            try:
                made = adopt_folder(path, bus=self.bus)
            except Exception as error:  # noqa: BLE001 - said plainly, never a crash
                self._toast(str(error))
                return
            self.reload_projects()
            self.open_project(str(made.path))
        dialog.select_folder(self, None, chosen)

    def _stage_where(self) -> str:
        return "on this computer · only you can see it"

    def _show_welcome(self) -> None:
        self._leave_project()
        self.project = None
        self.goal_id = None
        # No project yet: "What do you want to make?" is the whole window, not a column beside an empty area.
        self.middle.set_visible(False)
        self.split.set_max_sidebar_width(100000)
        self.split.set_sidebar_width_fraction(1.0)
        self.area_center.hide_bar()
        self.set_identity_subtitle("")
        self.title_content.set_visible(False)
        for button in (self.people_button, self.places_button, self.usage_button, self.stage_button):
            button.set_visible(False)
        self.phone_stage.set_visible(False)
        self.rail.show(self.projects, None, [], dict(ARI), drafting="Describe what you want to make")
        self.conversation.show_welcome()
        self.stage.set_project(None, "")
        self._sync_island()
        GLib.idle_add(lambda: (self.conversation.welcome.focus(), False)[1])

    def _leave_project(self) -> None:
        """Stop watching and running what belonged to the project we're leaving."""
        self._watch_token = None
        if sync_ui is not None:
            try:
                sync_ui.stop_sync(self)
            except Exception:  # noqa: BLE001
                log.exception("sharing didn't stop cleanly")
        if self._watcher is not None:
            watcher, self._watcher = self._watcher, None
            threading.Thread(target=watcher.stop, daemon=True).start()
        if self._heartbeat:
            GLib.source_remove(self._heartbeat)
            self._heartbeat = 0
        if self.project is not None:
            try:
                self.project.store.clear_app_heartbeat()
            except OSError:
                pass
            # Its Stage stops too: one running preview per open project, never a pile of them.
            preview = _optional("core.preview")
            if preview is not None:
                folder = self.project.path
                if getattr(self, "_quitting", False):
                    preview.stop_preview(folder)      # Playground is going: wait for it, so nothing is left
                else:
                    threading.Thread(target=lambda: preview.stop_preview(folder), daemon=True,
                                     name="preview-stop").start()
        if self._page is not None:
            self.hide_page(self._page)

    def _start_watching(self, project: ProjectData) -> None:
        # Opening the project that's already open again must not leave a second watcher and heartbeat behind
        # (each would publish every change twice, and the thread would redraw twice).
        if self._watcher is not None:
            watcher, self._watcher = self._watcher, None
            threading.Thread(target=watcher.stop, daemon=True).start()
        if self._heartbeat:
            GLib.source_remove(self._heartbeat)
            self._heartbeat = 0
        watch = _optional("core.watch")
        if watch is not None:
            # The watcher's first look at the folder walks all of it, so it starts off the main loop.
            token = object()
            self._watch_token = token

            def work() -> None:
                try:
                    watcher = watch.watch(project.path, bus=self.bus)
                except Exception:  # noqa: BLE001
                    log.exception("could not watch %s", project.path)
                    return

                def adopt() -> bool:
                    if getattr(self, "_watch_token", None) is token and self.project is project:
                        self._watcher = watcher
                    else:   # the project closed (or opened again) meanwhile
                        threading.Thread(target=watcher.stop, daemon=True).start()
                    return GLib.SOURCE_REMOVE
                GLib.idle_add(adopt, priority=GLib.PRIORITY_DEFAULT)
            threading.Thread(target=work, daemon=True, name="watch-start").start()
        # The heartbeat tells the MCP server there's a Stage and a checker to ask.
        self._beat()
        self._heartbeat = GLib.timeout_add_seconds(HEARTBEAT_S, self._beat)
        GLib.idle_add(self._answer_requests)

    def _beat(self) -> bool:
        if self.project is None:
            return GLib.SOURCE_REMOVE
        try:
            self.project.store.write_app_heartbeat()
            self.project.store.expire_decisions()
        except (OSError, StoreError):
            pass
        return GLib.SOURCE_CONTINUE

    def _start_preview(self, project: ProjectData) -> None:
        preview = _optional("core.preview")
        if preview is None:
            self.stage.set_preview("none")
            return
        if getattr(self, "_preview_stopped", "") == str(project.path):
            self.stage.set_preview("stopped")              # stopped by the person: it waits for Run
            return
        running = preview.current(project.path)
        if running is not None:
            status = running.status()
            self.stage.set_preview(status.get("state", ""), status.get("url", ""), status.get("error", ""),
                                   status.get("label", ""), status.get("target", ""), status.get("device", ""))
            return
        plan = preview.detect(project.path)
        if plan is None:
            self.stage.set_preview("none")
            return
        self.stage.set_preview("starting", label=getattr(plan, "label", ""), target=getattr(plan, "target", ""))

        # Light or dark as Playground is now: the preview's shell tells the app (core/preview_shell.py).
        appearance = "dark" if Adw.StyleManager.get_default().get_dark() else "light"

        def work() -> None:
            reaping = getattr(self, "_reaping", None)
            if reaping is not None and reaping is not threading.current_thread():
                reaping.join(timeout=10)   # an old run's server may still hold the port
            try:
                # A desktop app fills a device frame; in the App window view it's a window on a small desktop.
                fill = "0" if self.stage.device == "app" else "1"
                preview.start_preview(project.path, bus=self.bus, extra_env={"PLAYGROUND_PREVIEW_FILL": fill,
                                                                             "PLAYGROUND_PREVIEW_DEVICE": self.stage.device,
                                                                             "PLAYGROUND_PREVIEW_APPEARANCE": appearance})
            except Exception as error:  # noqa: BLE001
                log.exception("the preview didn't start")
                GLib.idle_add(self.stage.set_preview, "failed", "", str(error), priority=GLib.PRIORITY_DEFAULT)
        threading.Thread(target=work, daemon=True, name="preview-start").start()

    def report_preview(self, error: str) -> None:
        """A stopped preview, filed as a bug in one click: what it said and the last lines it printed, so the
        next agent starts from the evidence (agents see new bugs in their next brief)."""
        project, preview = self.project, _optional("core.preview")
        if project is None:
            return
        tail = ""
        if preview is not None:
            tail = preview.read_log_tail(project.path / ".playground" / "local" / "preview.log", lines=40)
        first = (error or "The preview stopped").strip().splitlines()[0]
        try:
            issue = project.store.create_issue(
                f"The preview stops: {first[:70]}", severity="high", place="stage", reporter=project.you(),
                what_happened=error.strip() or "The preview stopped.", expected="The app keeps running in the preview.",
                steps="Open the project; the preview starts and then stops.",
                evidence=("The last lines it printed:\n\n```\n" + tail.strip()[-3000:] + "\n```") if tail.strip() else "")
        except (StoreError, OSError, ValueError) as error_:
            self._toast(str(error_))
            return
        self._toast(f"Filed bug #{issue.number}")
        self.show_area("bugs", item=issue.number)

    def stop_conversation(self, goal_id: str | None = None) -> None:
        """Stop: the agent ends what it's doing in this conversation; the next message carries on."""
        runner = self._runner(goal_id or self.goal_id or "", create=False)
        if runner is None or not runner.cancel():
            return
        name = self._agent_for(goal_id or self.goal_id or "").get("label") or "The agent"
        self._toast(f"Stopping {name}…")

    def restart_preview(self) -> None:
        """Stop the project's preview and start it again (after a change it didn't pick up, or a crash).
        Asks that come while one restart is under way (clicking through the sizes) become one more, after it."""
        project, preview = self.project, _optional("core.preview")
        if project is None or preview is None:
            return
        if getattr(self, "_restarting", False):
            self._restart_again = True
            return
        self._preview_stopped = ""
        self._restarting, self._restart_again = True, False
        self.stage.set_preview("starting")

        def work() -> None:
            try:
                preview.stop_preview(project.path)
            except Exception:  # noqa: BLE001 - it may already be gone
                log.exception("stopping the preview")

            def start() -> bool:
                self._restarting = False
                if self._restart_again:
                    self.restart_preview()
                elif self.project is project:
                    self._start_preview(project)
                return False
            GLib.timeout_add(250, start)
        threading.Thread(target=work, daemon=True, name="preview-restart").start()

    def stop_preview(self) -> None:
        """Stop: the app stops, and stays stopped (changes don't start it again) until Run."""
        project, preview = self.project, _optional("core.preview")
        if project is None or preview is None:
            return
        self._preview_stopped = str(project.path)
        if getattr(self, "_files_changed", 0):
            GLib.source_remove(self._files_changed)
            self._files_changed = 0
        self.stage.set_preview("stopped")

        def work() -> None:
            try:
                preview.stop_preview(project.path)
            except Exception:  # noqa: BLE001 - it may already be gone
                log.exception("stopping the preview")
        threading.Thread(target=work, daemon=True, name="preview-stop").start()

    def _held_stopped(self) -> bool:
        return self.project is not None and getattr(self, "_preview_stopped", "") == str(self.project.path)

    def new_project(self) -> None:
        """v73: a new project starts from the welcome, in the conversation, with a New project row in the rail."""
        if self._phone:
            self.open_sheet("newproject")
            return
        self._show_welcome()

    def _start_new_project(self, text: str) -> None:
        if not text.strip():
            self.conversation.welcome.focus()
            return
        # The questions, brief and recipe come in their own sheet: a step finished before the project exists, not
        # part of its conversation.
        self.open_sheet("newproject", text=text)

    def _project_menu(self, button: Gtk.Widget) -> None:
        registry = CommandRegistry((CommandGroup(None, (
            Command("menu.book", "Project space", lambda: self.open_sheet("book"), icon="pg-book-open-symbolic"),
            Command("menu.files", "Show in Filer", lambda: self.show_in_files(), icon="pg-folder-symbolic"),
            Command("menu.places", "Places", lambda: self.open_sheet("places"), icon="pg-server-symbolic"),
            Command("menu.people", "People", lambda: self.open_sheet("people"), icon="pg-users-symbolic"),
        )),))
        command_popover(registry).present_for(button)

    def show_in_files(self, path: str | None = None) -> None:
        if self.project is None:
            return
        target = Gio.File.new_for_path(path or str(self.project.path))
        Gtk.FileLauncher.new(target).launch(self, None, None)

    # ── goals ─────────────────────────────────────────────────────────────
    def select_goal(self, goal_id: str | None) -> None:
        project = self.project
        if project is None or goal_id is None:
            return
        if self._page is not None:
            self.hide_page(self._page)
        self.goal_id = goal_id
        if goal_id in self._unread:
            self._unread.discard(goal_id)
            self.rail.set_unread(self._unread)
        app = self.get_application()
        if app is not None:
            app.withdraw_notification(f"conversation-{goal_id}")
        if sync_ui is not None:
            sync_ui.set_goal(self, goal_id)
        self.remember("goal", goal_id)
        self.rail.select(goal_id)
        if self.island.grown:
            self.island.fold()
        goal = project.goal(goal_id) or {"id": goal_id, "title": goal_id, "status": "working", "is_chat": False}
        agent = self._agent_for(goal_id)
        name = agent.get("label") or "Ari"
        if goal.get("is_chat") and not project.thread(goal_id):
            title = "New conversation"
            meta = None
            more = None
            empty = ("What do you want to do?", f"Say it here. The first message names the conversation, and "
                                                f"{name} starts on it.")
            placeholder = f"Ask {name} anything, or say what to build…"
        elif goal.get("is_chat"):
            title = goal.get("display_title") or "General"
            meta = goal_meta("", "", agent, "", chat=True)
            more = None
            empty = (f"Ask {name} anything about {project.name}",
                     "Where things stand, or what to do next.")
            placeholder = f"Ask {name} about {project.name}…"
        else:
            title = str(goal.get("title") or "Untitled conversation")
            meta = goal_meta(goal.get("status", "working"), self._started_words(goal), agent,
                             self._where_words(agent.get("where")) if agent.get("where")
                             not in (None, "", "this-computer") else "this computer",
                             on_state=self.conversation.show_open_question)
            more = w.icon_button("ellipsis", "More for this conversation")
            more.connect("clicked", lambda b: self._goal_menu(b, goal))
            empty = ("Say what you want", f"{name} makes a plan you can see before any work starts.")
            placeholder = f"Tell {name} what to change…"
        self.conversation.show_goal(goal_id, title, meta, more, project.thread(goal_id),
                                    self._card_context(goal, agent), empty=empty)
        self.composer.set_agent(agent, connection_subtitle(agent))
        self._composer_agent = (agent.get("id"), agent.get("label"))
        known = self._runner(goal_id, create=False)
        self.composer.set_options(known.options() if known is not None else [], can_choose=self._agent_runs_here())
        self.composer.set_plan_link(plan_usage_url(agent))
        self._banner(goal, agent)
        state = self.agent_states.get(goal_id, "")
        busy = state in BUSY_STATES
        self.composer.set_placeholder(f"{name} is working. You can still add something…" if busy else placeholder)
        self.conversation.set_thinking(name if busy and not self._step_running() else None)
        self._fill_usage()
        self._sync_island()

    def _banner(self, goal: dict, agent: dict) -> None:
        """Someone else's goal, or a paused one: the composer says so, with what you can do."""
        project = self.project
        you = project.you() if project else ""
        owner = str(goal.get("owner") or "")
        if goal.get("is_chat") or project is None:
            self.composer.set_banner(None)
            return
        if goal.get("status") == "paused":
            glyph = w.StateGlyph("paused", 18)
            glyph.set_margin_start(8)
            note_ = str(goal.get("status_note") or "").strip().rstrip(".")
            words = (f"<b>Paused: {GLib.markup_escape_text(note_)}.</b>" if note_ else "<b>Paused.</b>") + \
                " Nothing runs until you resume it."
            self.composer.set_banner(glyph, words, (
                w.button("Resume", icon_name_="play", style="key", on_click=lambda: self.resume_goal(goal["id"])),))
            return
        member = project.member(owner) if owner and owner != you else None
        if member is not None and goal.get("status") == "working" and agent.get("owner") == owner:
            first = str(member.get("name") or owner).split()[0]
            their = {"her": "her", "him": "his"}.get(str(member.get("pronouns") or "").split("/")[-1].strip().lower(),
                                                     "their")
            words = (f"<b>This is {GLib.markup_escape_text(first)}’s conversation.</b> {their.capitalize()} agent is working "
                     f"on it right now, on {their} computer.")
            self.composer.set_banner(w.person_face(str(member.get("name") or owner), 30), words, (
                w.button(f"Message {first}", icon_name_="message-square",
                         on_click=lambda: self.open_sheet("people", member=owner)),
                w.button("Pick it up", icon_name_="handoff", style="key",
                         on_click=lambda: self.open_sheet("picker", goal_id=goal["id"])),))
            return
        self.composer.set_banner(None)

    def _step_running(self) -> bool:
        if self.project is None or self.goal_id is None:
            return False
        entries = self.project.thread(self.goal_id)
        return bool(entries) and entries[-1].get("kind") == "step" and \
            (entries[-1].get("data") or {}).get("state") == "running"

    def _started_words(self, goal: dict) -> str:
        moment = w.parse_ts(goal.get("created"))
        owner = str(goal.get("owner") or "")
        you = self.project.you() if self.project else ""
        if not owner or owner == you:
            who = "you"
        else:
            member = self.project.member(owner) if self.project else None
            who = str((member or {}).get("name") or owner).split()[0]
            return f"{who}’s conversation · started {w.when_words(moment)}" if moment else f"{who}’s conversation"
        return f"Started by {who} {w.when_words(moment)}" if moment else f"Started by {who}"

    def _conversation_menu(self, goal_id: str, row: Gtk.Widget, x: float = -1, y: float = -1) -> None:
        """A conversation's menu: rename, details, pin, fork, archive, delete (and pause, who works on it)."""
        if self.project is None:
            return
        goal = self.project.goal(goal_id) or {"id": goal_id, "title": goal_id}
        extra = goal.get("extra") or {}
        pinned, archived, paused = bool(extra.get("pinned")), bool(extra.get("archived")), goal.get("status") == "paused"
        working = self.agent_states.get(goal_id, "") in BUSY_STATES
        registry = CommandRegistry((
            CommandGroup(None, (
                Command("conv.stop", "Stop", lambda: self.stop_conversation(goal_id), icon="pg-stop-symbolic",
                        description="End what the agent is doing now", visible=lambda: working),
            )),
            CommandGroup(None, (
                Command("conv.rename", "Rename…", lambda: self._rename_conversation(goal), icon="pg-pencil-symbolic"),
                Command("conv.details", "Details", lambda: self._conversation_details(goal), icon="pg-info-symbolic"),
                Command("conv.pin", "Unpin" if pinned else "Pin", lambda: self._set_extra(goal_id, pinned=not pinned),
                        icon="pg-pin-symbolic"),
                Command("conv.fork", "Fork", lambda: self._fork_conversation(goal), icon="pg-copy-symbolic",
                        description="A copy of the conversation so far, to try another way"),
                Command("conv.agent", "Who works on this", lambda: self.open_sheet("picker", goal_id=goal_id),
                        icon="pg-sparkles-symbolic"),
                Command("conv.pause", "Resume" if paused else "Pause",
                        lambda: self.resume_goal(goal_id) if paused else self._set_status(goal_id, "paused"),
                        icon="pg-play-symbolic" if paused else "pg-pause-symbolic", visible=lambda: not goal.get("is_chat")),
            )),
            CommandGroup(None, (
                Command("conv.archive", "Unarchive" if archived else "Archive",
                        lambda: self._set_extra(goal_id, archived=not archived), icon="pg-archive-symbolic"),
                Command("conv.delete", "Delete…", lambda: self._delete_conversation(goal), icon="pg-trash-2-symbolic",
                        destructive=True),
            )),
        ))
        menu = command_popover(registry)
        if x >= 0 and hasattr(menu, "present_at_pointer"):
            menu.present_at_pointer(row, x, y)
        else:
            menu.present_for(row)

    def _set_extra(self, goal_id: str, **changes) -> None:
        if self.project is None:
            return

        def change(goal) -> None:
            for key, value in changes.items():
                if value:
                    goal.extra[key] = value
                else:
                    goal.extra.pop(key, None)
        try:
            self.project.store.update_goal(goal_id, change)
        except (StoreError, OSError) as error:
            self._toast(str(error))
            return
        if changes.get("archived") and goal_id == self.goal_id:
            others = [g for g in self.project.goals() if g["id"] != goal_id and not (g.get("extra") or {}).get("archived")]
            if others:
                self.select_goal(others[0]["id"])
        self._sync_now()

    def _rename_conversation(self, goal: dict) -> None:
        dialog = Adw.AlertDialog(heading="Rename conversation")
        entry = Gtk.Entry(text=str(goal.get("title") or ""), activates_default=True)
        entry.update_property([Gtk.AccessibleProperty.LABEL], ["Name"])
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("rename", "Rename")
        dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("rename")
        dialog.set_close_response("cancel")

        def respond(_d, response: str) -> None:
            title = entry.get_text().strip()
            if response == "rename" and title and self.project is not None:
                try:
                    self.project.store.update_goal(goal["id"], lambda g: setattr(g, "title", title))
                except (StoreError, OSError, ValueError) as error:
                    self._toast(str(error))
                    return
                self._sync_now()
                if goal["id"] == self.goal_id:
                    self.select_goal(goal["id"])
        dialog.connect("response", respond)
        dialog.present(self)

    def _conversation_details(self, goal: dict) -> None:
        project = self.project
        if project is None:
            return
        agent = self._agent_for(goal["id"])
        entries = project.thread(goal["id"])
        owner = project.member(goal.get("owner") or "") or {}
        started = w.parse_ts(goal.get("created"))
        lines = [("Started", f"{w.day_words(started)} at {started.astimezone().strftime('%-I:%M %p')}" if started else ""),
                 ("By", str(owner.get("name") or goal.get("owner") or "")),
                 ("Agent", f"{agent.get('label') or 'Ari'} · {connection_subtitle(agent)}"),
                 ("State", w.state_label(goal.get("status") or "working")),
                 ("Messages and cards", str(len(entries))),
                 ("Forked from", str((goal.get("extra") or {}).get("forked_from") or "")),
                 ("Saved in", f".playground/threads/{goal['id']}.jsonl")]
        body = "\n".join(f"{k}: {v}" for k, v in lines if v)
        dialog = Adw.AlertDialog(heading=str(goal.get("title") or "Conversation"), body=body)
        dialog.add_response("close", "Close")
        dialog.present(self)

    def _fork_conversation(self, goal: dict) -> None:
        """A new conversation that starts with this one's history, to try something another way."""
        project = self.project
        if project is None:
            return
        try:
            store = project.store
            source = store.read_goal(goal["id"])
            made = store.create_goal(f"{source.title} (fork)", owner=project.you(), connection=source.connection,
                                     kind=source.kind)
            made.extra["forked_from"] = source.id
            store.write_goal(made)
            history = store.paths.thread(source.id)
            if history.exists():
                store.paths.thread(made.id).write_bytes(history.read_bytes())
        except (StoreError, OSError, ValueError) as error:
            self._toast(str(error))
            return
        self._sync_now()
        self.select_goal(made.id)
        self.show_conversation()

    def _delete_conversation(self, goal: dict) -> None:
        dialog = Adw.AlertDialog(heading=f"Delete “{goal.get('title') or 'this conversation'}”?",
                                 body="It leaves the sidebar for everyone on the project. A copy is kept on this "
                                      "computer in .playground/local/deleted, in case you change your mind.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def respond(_d, response: str) -> None:
            if response != "delete" or self.project is None:
                return
            store = self.project.store
            keep = store.paths.local_dir / "deleted" / f"{goal['id']}"
            try:
                keep.mkdir(parents=True, exist_ok=True)
                for path in (store.paths.goal(goal["id"]), store.paths.thread(goal["id"])):
                    if path.exists():
                        path.replace(keep / path.name)
            except OSError as error:
                self._toast(str(error))
                return
            if goal["id"] == self.goal_id:
                self.goal_id = None
                self.project._goals_cache = None
            self._sync_now()
            remaining = [g for g in self.project.goals() if not (g.get("extra") or {}).get("archived")]
            if self.goal_id is None and remaining:
                self.select_goal(remaining[0]["id"])
        dialog.connect("response", respond)
        dialog.present(self)

    def _goal_menu(self, button: Gtk.Widget, goal: dict) -> None:
        self._conversation_menu(goal["id"], button)

    RESUME_WORDS = "Carry on where you left off."

    def resume_goal(self, goal_id: str) -> None:
        """Resume: the conversation is working again and its agent carries on from where it stopped (a person's
        Stop, or a turn cut off when Playground closed), as if they'd said so in the composer."""
        if self.project is None:
            return

        def working(goal) -> None:
            goal.status, goal.status_note = GoalStatus.WORKING, ""
        try:
            self.project.store.update_goal(goal_id, working)
        except (StoreError, OSError, ValueError) as error:
            self._toast(str(error))
            return
        self.project.forget()
        if goal_id != self.goal_id:
            self.select_goal(goal_id)
        self.composer.set_banner(None)
        self._send(None, self.RESUME_WORDS, [], "")
        self._schedule_sync()

    def _set_status(self, goal_id: str, status: str) -> None:
        if self.project is not None and self.project.set_status(goal_id, status):
            self._schedule_sync()

    def new_goal(self) -> None:
        """A blank conversation on the right: type, and the first message names it."""
        if self.project is None:
            self.new_project()
            return
        if not self.project.thread(GENERAL_CHAT_ID):
            self.select_goal(GENERAL_CHAT_ID)
            self.show_conversation()
            self.composer.focus()
            return
        dialog = Adw.AlertDialog(heading="New conversation", body="What it’s about, in a few words. Every agent "
                                 "you talk to here works in the same project and sees the others’ work.")
        entry = Gtk.Entry(placeholder_text="Like “Let people pay when they book”", activates_default=True)
        entry.update_property([Gtk.AccessibleProperty.LABEL], ["What it’s about"])
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("create", "Start")
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("create")
        dialog.set_close_response("cancel")

        def respond(_dialog, response: str) -> None:
            title = entry.get_text().strip()
            if response != "create" or not title or self.project is None:
                return
            goal_id = self.project.create_goal(title, self.project.you())
            if goal_id:
                self._sync_now()
                self.select_goal(goal_id)
                self.show_conversation()
                self.composer.focus()
        dialog.connect("response", respond)
        dialog.present(self)

    def _conversation_from(self, words: str) -> str | None:
        project = self.project
        if project is None:
            return None
        title = conversation_title(words)
        goal_id = project.create_goal(title, project.you())
        if not goal_id:
            self._toast("The conversation couldn’t be made")
            return None
        chosen = (project.goal(GENERAL_CHAT_ID) or {}).get("connection") or ""
        if chosen:
            try:
                project.store.update_goal(goal_id, lambda g: setattr(g, "connection", chosen))
            except (StoreError, OSError):
                pass
        project._goals_cache = None
        self.goal_id = goal_id
        self._sync_now()
        self.select_goal(goal_id)
        return goal_id

    def start_conversation(self, title: str, ask: str = "", task_id: str = "", *, send: bool = False) -> str | None:
        """A new conversation (from a task's Work on this): made, opened on the right, the ask written in the
        composer for the person to send or change first. With ``task_id``, the task is claimed for this
        conversation's agent and linked both ways, so "Codex is on it" is true from the first moment."""
        if self.project is None:
            return None
        goal_id = self.project.create_goal(title, self.project.you())
        if not goal_id:
            self._toast("The conversation couldn’t be made")
            return None
        if task_id:
            agent = self._agent_for(goal_id)
            who = str(agent.get("id") or "ari")

            def link(task) -> None:
                task.conversation = goal_id
                task.status = "doing" if task.status == "todo" else task.status
            try:
                self.project.store.claim_task(task_id, who)
                self.project.store.update_task(task_id, link, by=f"person:{self.project.you()}")
                self.project.store.update_goal(goal_id, lambda g: g.extra.__setitem__("task", task_id))
            except (StoreError, OSError, ValueError) as error:
                self._toast(str(error))
        self._sync_now()
        self.select_goal(goal_id)
        self.show_conversation()
        if ask:
            self.composer.set_text(ask)
            if send:
                # Deferred to the agent: it starts now, and the person sees it on the right.
                self.composer.submit()
                return goal_id
        self.composer.focus()
        return goal_id

    # ── who works where ───────────────────────────────────────────────────
    def connection(self, key: str | None) -> dict:
        """A connection by id: one on this computer, or a teammate's described from the project."""
        key = str(key or "")
        if not key:
            return self.connection(self.default_connection_id())   # a goal with no chosen agent
        for connection in self.connections:
            if key in (connection.get("id"), connection.get("label")):
                return connection
        if key == "ari":
            return dict(ARI)
        provider = key.split("-")[-1] if "-" in key else key
        owner = key[: -len(provider) - 1] if "-" in key else ""
        name = PROVIDER_NAMES.get(provider, provider.capitalize())
        member = self.project.member(owner) if self.project and owner else None
        if member:
            first = str(member.get("name") or owner).split()[0]
            return {"id": key, "label": f"{first}’s {name}", "provider": provider, "kind": "personal-plan",
                    "owner": owner, "where": f"{owner}-computer"}
        return {"id": key, "label": name, "provider": provider, "kind": "personal-plan", "where": "this-computer"}

    def _agent_for(self, goal_id: str | None) -> dict:
        if self.project is None or not goal_id:
            return self.connection(self.default_connection_id())
        goal = self.project.goal(goal_id) or {}
        return self.connection(goal.get("connection") or "")

    def _where_words(self, where: str | None) -> str:
        if not where or where in ("this-computer", "local"):
            return "This computer"
        computers = _optional("agents.computers")
        computer = computers.ComputerStore().get(where) if computers is not None else None
        if computer is not None:
            return computer.name
        place = self.project.place(where) if self.project else None
        if place is not None:
            return str(place.get("name") or where)
        match = re.fullmatch(r"([a-z0-9]+)-computer", str(where))
        if match and self.project is not None:
            member = self.project.member(match.group(1))
            if member:
                return f"{str(member.get('name')).split()[0]}’s computer"
        return str(where).replace("-", " ").capitalize()

    def _place_name(self, place_id: str) -> str:
        place = self.project.place(place_id) if self.project else None
        if place is None:
            return place_id.replace("-", " ").capitalize()
        host = place.get("url") or place.get("host")
        if host and "://" in str(host):
            from urllib.parse import urlparse
            host = urlparse(str(host)).netloc or host     # "kiln.example", not "https://kiln.example"
        return f"{place.get('name')}, {host}" if host and place.get("role") == "production" else str(place.get("name"))

    def _card_context(self, goal: dict | None, agent: dict) -> CardContext:
        project = self.project
        you = project.you() if project else "me"

        def person_name(member_id: str) -> str:
            member = project.member(member_id) if project else None
            return str((member or {}).get("name") or member_id)

        ctx = CardContext(
            project_name=project.name if project else "",
            project_dir=str(project.path) if project else "",
            goal_id=self.goal_id or "",
            goal_title=str((goal or {}).get("title") or ""),
            goal_status=str((goal or {}).get("status") or ""),
            agent_name=agent.get("label") or "Ari",
            connection=agent,
            connection_for=self.connection,
            where_words=self._where_words,
            open_sheet=self.open_sheet,
            answer_decision=self._answer,
            undo=self._undo,
            send=lambda text: self._send(None, text, [], ""),
            focus_composer=lambda text: (self.composer.set_text(text) if text else self.composer.focus()),
            show_stage=lambda: (self.stage.set_env("stage"), self.show_stage()),
            show_image=self._show_image,
            continue_handoff=self._continue_handoff,
            show_in_files=self.show_in_files,
            goal_for=lambda goal_id: project.goal(goal_id) if project else None,
            open_goal=self.select_goal,
            place_name=self._place_name,
            services=project.services() if project else [],
            open_section=self.open_section,
            you=you,
            person_name=person_name,
            retry=self._retry,
            sign_in=self._sign_in,
            signed_in=self._signed_in,
            open_url=self.open_url,
        )
        return ctx

    # ── when an agent stopped: the failure card's buttons ──────────────────
    def _retry(self) -> bool:
        goal_id = self.goal_id or ""
        runner = self._runner(goal_id, create=False)
        if runner is not None and callable(getattr(runner, "retry", None)):
            try:
                if runner.retry():
                    self.agent_states[goal_id] = "starting"
                    self.conversation.set_thinking(self._agent_for(goal_id).get("label") or "Ari", "starting")
                    return True
            except Exception:  # noqa: BLE001
                log.exception("try again failed")
        # Playground was closed since (the runner is new) or there's nothing waiting: say how to carry on.
        self._toast("Send your last message again to carry on")
        self.composer.focus()
        return False

    def _sign_in(self, method: dict) -> None:
        goal_id = self.goal_id or ""
        runner = self._runner(goal_id, create=True)
        if runner is None:
            self.open_sheet("picker", goal_id=goal_id)
            return
        if method.get("type") == "agent":
            # The agent runs its own sign-in (it opens the browser), then carries on by itself.
            try:
                runner.sign_in_with_agent(str(method.get("id") or ""))
            except Exception as error:  # noqa: BLE001
                self._toast(str(error) or "That sign-in didn’t start")
            return
        from .sheets import picker
        picker.sign_in_again(self, runner, method, self._agent_for(goal_id).get("label") or "The agent")

    def _signed_in(self) -> None:
        goal_id = self.goal_id or ""
        runner = self._runner(goal_id, create=True)
        if runner is None:
            return
        try:
            runner.signed_in()
            self.agent_states[goal_id] = "starting"
            self.conversation.set_thinking(self._agent_for(goal_id).get("label") or "Ari", "starting")
        except Exception:  # noqa: BLE001
            log.exception("carrying on after sign-in failed")

    def open_url(self, url: str) -> None:
        if url.startswith(("https://", "http://")):
            Gtk.UriLauncher.new(url).launch(self, None, None)

    # ── acting on the goal ────────────────────────────────────────────────
    def _connection_object(self, connection: dict):
        if connection.get("_object") is not None:
            return connection["_object"]
        module = _optional("agents.connections")
        if module is None:
            return None
        try:
            return module.ConnectionStore().get(connection.get("id") or "")
        except Exception:  # noqa: BLE001
            return None

    def _runner(self, goal_id: str, *, create: bool):
        if self.project is None or not goal_id:
            return None
        key = (str(self.project.path), goal_id)
        if key in self.runners or not create:
            return self.runners.get(key)
        module = _optional("agents.runner")
        if module is None:
            return None
        connection = self._connection_object(self._agent_for(goal_id))
        if connection is None:
            return None
        try:
            runner = module.GoalRunner(self.project.path, goal_id, connection, self.bus, person=self.project.you())
        except Exception:  # noqa: BLE001
            log.exception("could not start the runner")
            return None
        self.runners[key] = runner
        return runner

    def runner_for(self, goal_id: str, create: bool = True):
        """The goal's GoalRunner (made on first use), or None while nobody is connected to run it."""
        return self._runner(goal_id, create=create)

    def start_goal(self, goal_id: str) -> None:
        """Open a goal that was just made and get its agent going on it (New project's first goal)."""
        self._sync_now()
        self.select_goal(goal_id)
        goal = self.project.goal(goal_id) if self.project else None
        runner = self._runner(goal_id, create=True)
        if goal is None:
            self.composer.focus()
            return
        words = "Make the first version from the brief and recipe above, and get it running in Preview."
        if runner is None:
            # Nobody can run it yet: the first words are kept in the thread, and the note says what to do.
            self._send(None, words, [], "")
            return
        self._begin_goal(goal_id)
        try:
            runner.send(words)
            self.agent_states[goal_id] = "starting"
            self.conversation.set_thinking(self._agent_for(goal_id).get("label") or "Ari", "starting")
        except Exception:  # noqa: BLE001
            log.exception("the first goal didn't start")

    def _send(self, _composer, text: str, attachments: list, about: str) -> None:
        project, goal_id = self.project, self.goal_id
        if project is None or goal_id is None:
            return
        if goal_id == GENERAL_CHAT_ID and not project.thread(goal_id):
            # The blank conversation: the first message makes a real one, named from what was said.
            made = self._conversation_from(text or about or "New conversation")
            if made is None:
                return
            goal_id = made
        words = f"About {about}: {text}" if about and text else text
        attachments = [dict(a, path=self._bring_in(a["path"])) if a.get("path") else a for a in attachments]
        runner = self._runner(goal_id, create=True)
        name = self._agent_for(goal_id).get("label") or "Ari"
        if runner is not None:
            self._begin_goal(goal_id)
            try:
                runner.send(words, attachments=[a.get("path") for a in attachments if a.get("path")])
                self.agent_states[goal_id] = "starting"
                self.conversation.set_thinking(name, now=True)      # the person just sent: say so at once
                self._schedule_sync()
                return
            except Exception as error:  # noqa: BLE001
                log.exception("the runner could not take the message")
                self._toast(str(error) or "That didn’t send")
                return
        # Nobody is connected to run it: keep the words in the project, and say so plainly.
        data: dict = {}
        if about:
            data["about"] = about
        if attachments:
            data["attachments"] = [a.get("path") for a in attachments if a.get("path")]
        if project.append(goal_id, ThreadEntry(kind="message", role="person", text=text, by=project.you(), data=data)):
            self._sync_now()
        hint = w.box("v", 8, "pg-offline", halign=Gtk.Align.CENTER)
        hint.append(note(f"**{name} isn’t connected yet.** Your words are saved in the project, so {name} reads "
                         f"them first once it’s set up.", "plug"))
        button = w.button("Choose who works on this", icon_name_="sparkles", small=True,
                          on_click=lambda: self.open_sheet("picker", goal_id=goal_id))
        button.set_halign(Gtk.Align.CENTER)
        hint.append(button)
        self.conversation.append_local(hint)

    def _begin_goal(self, goal_id: str) -> None:
        """In a shared project, goal work happens on the goal's own branch (core.sync). A couple of git calls,
        so on a thread; if it can't switch safely, the reason shows as a quiet note."""
        engine = sync_ui.engine_for(self) if sync_ui is not None else None
        if engine is None or goal_id == GENERAL_CHAT_ID:
            return

        def work() -> None:
            try:
                result = engine.begin_goal(goal_id) or {}
            except Exception as error:  # noqa: BLE001
                result = {"switched": False, "reason": str(error)}
            if result.get("switched") is False and result.get("reason"):
                GLib.idle_add(lambda: (self.goal_id == goal_id and self.conversation.append_local(
                    note(str(result["reason"]), "info")), False)[1], priority=GLib.PRIORITY_DEFAULT)
        threading.Thread(target=work, daemon=True, name="goal-branch").start()

    def _answer(self, decision_id: str, option: str) -> bool:
        if self.project is None:
            return False
        try:
            asked = self.project.store.read_pending(decision_id)
        except (StoreError, OSError):
            asked = None
        extra = getattr(asked, "extra", None) or {}
        if extra.get("source") == "install" and option == "install":
            self._install_on_computer(self.project, str(getattr(asked, "goal", "") or self.goal_id or ""),
                                      [str(p) for p in extra.get("packages") or []])
        runner = self._runner(self.goal_id or "", create=False)
        if runner is None:
            # An agent's own question, answered after Playground restarted: its conversation's agent takes the
            # answer, so it carries on instead of waiting for a reply it will never get.
            try:
                pending = self.project.store.read_pending(decision_id)
            except (StoreError, OSError):
                pending = None
            source = str((getattr(pending, "extra", None) or {}).get("source") or "")
            if pending is not None and getattr(pending, "goal", "") and source != "permission":
                runner = self._runner(pending.goal, create=True)
        if runner is not None:
            try:
                runner.answer_decision(decision_id, option)
                return True
            except Exception:  # noqa: BLE001
                log.exception("the runner could not take the answer")
        goal_id = self.goal_id
        answered = self.project.answer(decision_id, option, self.project.you())
        if answered and goal_id:
            self._settle(goal_id)
        return answered

    def _settle(self, goal_id: str) -> None:
        """The last open question on a goal was answered and no agent is here to move it on: it stops
        saying "Needs you · one decision". (A running agent's MCP call does this itself.)"""
        project = self.project
        if project is None or project.pending_counts().get(goal_id, 0):
            return

        def clear(goal) -> None:
            if goal.status == GoalStatus.parse("needs-you"):
                goal.status = GoalStatus.parse("working")
                goal.status_note = ""
        try:
            project.store.update_goal(goal_id, clear)
        except (StoreError, OSError, ValueError):
            log.exception("could not update the goal after the answer")
        self._schedule_sync()

    def _undo(self, undo: dict, data: dict) -> bool:
        runner = self._runner(self.goal_id or "", create=False)
        if runner is not None and callable(getattr(runner, "undo", None)):
            try:
                runner.undo(undo)
                return True
            except Exception:  # noqa: BLE001
                log.exception("the runner could not undo")
        kind, ref = str(undo.get("kind") or ""), str(undo.get("ref") or "")
        if kind in ("commit", "git", "git-commit") and ref and self.project is not None and \
                re.fullmatch(r"[0-9a-fA-F]{4,40}|HEAD(~\d+)?", ref):
            path = str(self.project.path)
            title = str(data.get("text") or "the change")

            def work() -> None:
                result = subprocess.run(["git", "-C", path, "revert", "--no-edit", ref], capture_output=True,
                                        text=True, timeout=30)
                GLib.idle_add(self._undone, result.returncode == 0, title, priority=GLib.PRIORITY_DEFAULT)
            threading.Thread(target=work, daemon=True).start()
            return True
        # Otherwise the agent does it: it knows what it changed.
        self._send(None, f"Please undo this: {data.get('text') or 'the change you told me about'}", [], "")
        return True

    def _undone(self, ok: bool, title: str) -> bool:
        if ok and self.project is not None and self.goal_id:
            self.project.append(self.goal_id, ThreadEntry(kind="message", role="system", by="playground",
                                                          text=f"Undone: {title}", data={"icon": "rotate-ccw"}))
        self._toast("Undone" if ok else "That couldn’t be undone automatically. Ask the agent to put it back.")
        return GLib.SOURCE_REMOVE

    def _continue_handoff(self, data: dict, brief: str) -> bool:
        runner = self._runner(self.goal_id or "", create=True)
        if runner is None:
            self._toast("The next agent isn’t connected yet")
            return False
        try:
            runner.continue_handoff(brief)
            return True
        except Exception as error:  # noqa: BLE001
            self._toast(str(error))
            return False

    def switch_connection(self, goal_id: str, connection) -> None:
        """The picker chose someone else for this goal. The runner writes the Handoff card."""
        runner = self._runner(goal_id, create=True)
        if runner is not None:
            try:
                runner.switch(connection)
                return
            except Exception as error:  # noqa: BLE001
                self._toast(str(error))
                return
        if self.project is None:
            return
        handoff = _optional("agents.handoff")
        target = self.connection(getattr(connection, "id", None) or _as_dict(connection).get("id"))
        try:
            before = (self.project.goal(goal_id) or {}).get("connection") or "ari"
            card = handoff.build_brief(self.project.store, goal_id, from_connection=before,
                                       to_connection=connection).card_data() if handoff else {}
            self.project.store.update_goal(goal_id, lambda g: setattr(g, "connection", target.get("id") or ""))
            self.project.append(goal_id, ThreadEntry(kind="handoff", role="system", data=card,
                                                     text=f"Switched to {target.get('label')}"))
        except Exception as error:  # noqa: BLE001
            self._toast(str(error))

    def _show_image(self, path: str, caption: str) -> None:
        dialog = Adw.Dialog(title=caption or "The shot", content_width=420, content_height=820)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        try:
            picture = Gtk.Picture.new_for_paintable(Gdk.Texture.new_from_filename(path))
        except GLib.Error:
            return
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        picture.add_css_class("pg-lightbox")
        for side in ("bottom", "start", "end"):
            getattr(picture, f"set_margin_{side}")(16)
        toolbar.set_content(picture)
        dialog.set_child(toolbar)
        dialog.present(self)

    def _toast(self, message: str) -> bool:
        try:
            from luma_appkit import Toast
            Toast.show(self.conversation, message)
        except Exception:  # noqa: BLE001
            log.info("%s", message)
        return GLib.SOURCE_REMOVE

    # ── sheets (owned by sheets/*, opened through the application) ────────
    def open_sheet(self, name: str, **options) -> None:
        if name == "book" and self.project is not None:
            # The Project space's sections are the window's areas now.
            section = str(options.get("section") or ("bugs" if options.get("issue") else self.rail.area))
            self.show_area(section, item=options.get("issue") or options.get("service") or options.get("item"))
            return
        if name == "people" and self.project is not None and not options:
            self.show_area("team")
            return
        if self.island.grown:
            self.island.fold()
        try:
            self.get_application().open_sheet(name, **options)
        except ModuleNotFoundError:
            self._toast("That part of Playground is still being built")
        except Exception:  # noqa: BLE001
            log.exception("could not open the %s sheet", name)

    def open_section(self, section: str, item=None) -> None:
        """An area at one item in it: a bug, a service."""
        self.show_area(section, item=None if item in (None, "") else item)

    # ── what only the open app can do: a screenshot of the Stage, the checker ─
    def _answer_requests(self) -> bool:
        project = self.project
        if project is None:
            return GLib.SOURCE_REMOVE
        for kind in ("screenshot", "checks", "box"):
            for request in project.pending(kind):
                request_id = str(request.get("id"))
                if request_id in self._answering:
                    continue
                self._answering.add(request_id)
                if kind == "screenshot":
                    self._screenshot(project, request)
                elif kind == "box":
                    self._box(project, request)
                else:
                    self._run_checks(project, request)
        return GLib.SOURCE_REMOVE

    def _reply(self, project: ProjectData, request_id: str, *, text: str = "", data: dict | None = None) -> None:
        try:
            project.store.write_answer(Answer(id=request_id, text=text, data=data or {}, answered_by="playground"))
        except AlreadyAnswered:
            pass
        except (StoreError, OSError) as error:
            log.warning("could not answer %s: %s", request_id, error)
        self._answering.discard(request_id)

    def _box(self, project: ProjectData, request: dict) -> None:
        """An agent's command in the project's box (core.box): made the first time, one command at a time.
        The answer goes back through the agent's waiting call, or, if that gave up, as its next message."""
        box = _optional("core.box")
        request_id, goal_id = str(request.get("id")), str(request.get("goal") or "")
        command = str((request.get("params") or {}).get("command") or "")
        if box is None or not command:
            self._reply(project, request_id, text="The box isn't available here.")
            return
        root, project_id = project.path, getattr(project, "id", "") or ""
        self._toast("Running in the project’s box…")

        def work() -> None:
            name = box.name_for(root, project_id)
            with box.lock_for(name):
                try:
                    box.ensure(root, project_id)
                    words = box.run(root, command, project_id).words(command)
                except box.BoxError as error:
                    words = f"The project's box couldn't run `{command}`: {error}"
            GLib.idle_add(lambda: (self._box_done(project, request_id, goal_id, words), False)[1])
        threading.Thread(target=work, daemon=True, name="box-run").start()

    def _box_done(self, project: ProjectData, request_id: str, goal_id: str, words: str) -> None:
        self._reply(project, request_id, text=words)

        def unclaimed() -> bool:
            # Nobody picked the answer up (the agent's call stopped waiting): it's the agent's next message.
            try:
                still = project.store.read_pending(request_id) is not None
            except (StoreError, OSError):
                still = False
            if still:
                try:
                    project.store.remove_pending(request_id)
                except (StoreError, OSError):
                    pass
                runner = self._runner(goal_id, create=True) if goal_id else None
                if runner is not None:
                    runner.tell(f"(Playground: your command in the project's box finished.) {words}")
            return False
        GLib.timeout_add(2000, unclaimed)

    def _install_on_computer(self, project: ProjectData, goal_id: str, packages: list[str]) -> None:
        """The person said yes on an install card: the system's own password dialog (polkit), then the agent
        hears how it went."""
        box = _optional("core.box")
        if box is None:
            return
        self._toast("Your computer will ask for your password in its own window")

        def work() -> None:
            try:
                result = box.install_on_computer(packages)
                words = (f"(Playground: installed {', '.join(packages)} on this computer.) " if result.code == 0 else
                         f"(Playground: installing {', '.join(packages)} on this computer didn't work, exit code "
                         f"{result.code}: the person may have cancelled the password window.) ") + result.output[-1500:]
            except box.BoxError as error:
                words = f"(Playground: couldn't install {', '.join(packages)}: {error})"

            def tell() -> bool:
                runner = self._runner(goal_id, create=True) if goal_id else None
                if runner is not None:
                    runner.tell(words)
                return False
            GLib.idle_add(tell)
        threading.Thread(target=work, daemon=True, name="install").start()

    def _screenshot(self, project: ProjectData, request: dict) -> None:
        params = request.get("params") or {}
        request_id = str(request["id"])
        # Proof is shared and committed; local/ isn't (ARCHITECTURE.md, contract additions).
        relative = f".playground/proof/{request.get('goal') or 'stage'}/{request_id}.png"

        def done(ok: bool, error: str) -> None:
            if ok:
                self._reply(project, request_id, data={"path": relative, "device": params.get("device") or "phone"})
            else:
                self._reply(project, request_id, text=error or "The Stage couldn’t take a screenshot.")
        self.stage.capture(str(project.path / relative), device=str(params.get("device") or ""),
                           path=str(params.get("path") or "/"), environment=str(params.get("environment") or "stage"),
                           done=done)

    def _run_checks(self, project: ProjectData, request: dict) -> None:
        checks = _optional("core.checks")
        request_id = str(request["id"])
        if checks is None:
            self._reply(project, request_id, text="Playground’s checker isn’t available.")
            return
        goal_id = str(request.get("goal") or "")
        url = self.stage.preview.get("url") or None

        def work() -> None:
            try:
                outcome = checks.run_goal_checks(project.path, goal_id, preview_url=url, store=project.store,
                                                 screenshot=self._screenshot_from_thread)
                result = {"checks": outcome.get("checks") or []}
                GLib.idle_add(lambda: (self._reply(project, request_id, data=result), False)[1],
                              priority=GLib.PRIORITY_DEFAULT)
            except Exception as error:  # noqa: BLE001
                GLib.idle_add(lambda: (self._reply(project, request_id, text=str(error)), False)[1],
                              priority=GLib.PRIORITY_DEFAULT)
        threading.Thread(target=work, daemon=True, name="checks").start()

    def _screenshot_from_thread(self, url: str, path) -> str | None:
        """The checker's screenshot callback: it runs on the checker's thread, the Stage on the main loop."""
        done = threading.Event()
        result: dict = {}

        def finished(ok: bool, error: str) -> None:
            result["ok"], result["error"] = ok, error
            done.set()

        def start() -> bool:
            self.stage.capture(str(path), path=url, done=finished)
            return GLib.SOURCE_REMOVE
        GLib.idle_add(start, priority=GLib.PRIORITY_DEFAULT)
        done.wait(45)
        return str(path) if result.get("ok") else None

    # ── staying current: the bus ──────────────────────────────────────────
    def _subscribe(self) -> None:
        def later(handler):
            # At the main loop's default priority, not idle: while something animates (a spinner, a working
            # step) on a slow renderer, frames are drawn back to back and idle callbacks would wait behind them.
            return lambda **payload: GLib.idle_add(lambda: (handler(**payload), False)[1],
                                                   priority=GLib.PRIORITY_DEFAULT)
        for topic, handler in ((events.THREAD_APPENDED, self._on_change), (events.GOAL_CHANGED, self._on_change),
                               (events.PROJECT_CHANGED, self._on_project), (events.DECISION_PENDING, self._on_change),
                               (events.DECISION_ANSWERED, self._on_change), (events.PREVIEW_STATE, self._on_preview),
                               (events.AGENT_STATE, self._on_agent), (events.USAGE_CHANGED, self._on_usage),
                               (events.PLACES_CHANGED, self._on_project),
                               (events.CONNECTIONS_CHANGED, self._on_connections_changed),
                               (events.REQUEST_PENDING, self._on_request),
                               ("stack.changed", self._on_space), ("issue.changed", self._on_space),
                               (events.AGENT_OPTIONS, self._on_options), (events.FILES_CHANGED, self._on_files),
                               ("analytics.changed", self._on_space)):
            self._tokens.append(self.bus.subscribe(topic, later(handler)))

    # ── the agent's settings: model, effort, mode ─────────────────────────
    def _on_options(self, **payload) -> None:
        if not self._mine(payload) or payload.get("goal_id") != self.goal_id:
            return
        self.composer.set_options(payload.get("options") or [], can_choose=self._agent_runs_here())
        if getattr(self, "_options_waiting", False):
            self._options_waiting = False
            self._options_menu()

    def _agent_runs_here(self) -> bool:
        agent = self._agent_for(self.goal_id)
        return bool(agent) and agent.get("kind") != "ari" and agent.get("provider") not in ("ari", "ollama", "")

    def _options_menu(self) -> None:
        """The agent's settings as one menu: each setting a group, its choices ticked. When the agent hasn't
        said yet, it's started first and the menu opens once it has."""
        if self.goal_id is None:
            return
        runner = self._runner(self.goal_id, create=True)
        if runner is None:
            self._toast("Choose who works on this first")
            return
        options = runner.options()
        if not options:
            self._options_waiting = True
            self.composer.options_words.set_label("Asking…")
            runner.prepare()
            return
        self._present_options(options)

    def _present_options(self, options: list[dict]) -> None:
        main, rest = [], []
        for option in sorted(options, key=_option_rank):
            choices = [c for c in option.get("options") or [] if isinstance(c, dict)]
            if not choices:
                continue
            option_id = option.get("id")
            base = f"opt.{_action_part(option_id)}"
            name = str(option.get("name") or option_id)
            current = option.get("currentValue")
            words = next((str(c.get("name") or c.get("value")) for c in choices if c.get("value") == current), "")
            switch = _as_switch(choices)
            if switch is not None:
                off, on = switch
                command = Command(base, name,
                                  lambda o=option_id, now=current, a=off, b=on:
                                  self._set_option(o, a if now == b else b),
                                  checked=lambda now=current, b=on: now == b)
            else:
                # The id is only an action name: the real value rides in the lambda. An index keeps
                # ids unique even when two models differ only by a character we sanitise away.
                command = Command(base, f"{name} · {words}" if words else name, lambda: None,
                                  children=tuple(
                                      Command(f"{base}.{index}", str(c.get("name") or c.get("value")),
                                              lambda o=option_id, v=c.get("value"): self._set_option(o, v),
                                              description=str(c.get("description") or "")[:80],
                                              checked=lambda now=current, v=c.get("value"): now == v)
                                      for index, c in enumerate(choices)))
            (main if option.get("category") in ("model", "thought_level") or _is_speed(option) else rest).append(command)
        groups = [CommandGroup(None, tuple(main))] if main else []
        if rest:
            groups.append(CommandGroup(None, tuple(rest)))
        if not groups:
            self._toast("This agent doesn’t offer any settings")
            return
        menu = command_popover(CommandRegistry(tuple(groups)))
        menu.set_parent(self.composer.options_button)
        menu.set_position(Gtk.PositionType.TOP)       # the composer sits at the foot: its menus open upward
        menu.popup()

    def _set_option(self, config_id: str, value) -> None:
        runner = self._runner(self.goal_id, create=True) if self.goal_id else None
        if runner is not None:
            runner.set_option(config_id, value)

    def _mine(self, payload: dict) -> bool:
        project = payload.get("project")
        if self.project is None:
            return False
        if project is None:
            return True
        try:
            return Path(str(project)).resolve() == self.project.path.resolve()
        except OSError:
            return False

    def _on_change(self, **payload) -> None:
        if self._mine(payload):
            self._schedule_sync()

    def _on_project(self, **payload) -> None:
        if self._mine(payload) and self.project is not None:
            self.project.reload()
            self._fill_people()
            self._fill_places()
            self.stage.set_project(str(self.project.path), self.project.name, live_url=self._live_url(),
                                   where_text=self._stage_where())
            self._schedule_sync()
            if payload.get("what") == "run":
                # An agent wrote or changed run.toml: the preview starts (again) the way it now says, a moment
                # after the last change, so the agent can look at it and take its screenshot.
                if getattr(self, "_run_changed", 0):
                    GLib.source_remove(self._run_changed)
                self._run_changed = GLib.timeout_add(1500, self._run_config_changed)

    def _bring_in(self, path: str) -> str:
        """An attachment from outside the project (a screenshot in Photos, a file in Downloads) is copied into
        the project's own local folder: agents can only read inside the project, so otherwise it never
        reached them."""
        project = self.project
        if project is None:
            return path
        source = Path(path).expanduser()
        try:
            source.resolve().relative_to(Path(project.path).resolve())
            return str(source)                    # already inside
        except (ValueError, OSError):
            pass
        folder = Path(project.path) / ".playground" / "local" / "attachments"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / source.name
            if target.exists() and target.stat().st_size != source.stat().st_size:
                target = folder / f"{source.stem}-{int(time.time())}{source.suffix}"
            if source.is_file() and source.stat().st_size <= 50 * 1024 * 1024:
                shutil.copy2(source, target)
                return str(target)
        except OSError:
            log.exception("could not bring %s into the project", source)
        return str(source)

    def _on_files(self, **payload) -> None:
        """The app's files changed (an agent's edit, or yours): the preview follows by itself, a moment after the
        last change, so nobody has to restart anything. Dev servers that reload themselves are left alone."""
        if not self._mine(payload) or self.project is None or self._held_stopped():
            return
        if getattr(self, "_files_changed", 0):
            GLib.source_remove(self._files_changed)
        self._files_changed = GLib.timeout_add(2000, self._refresh_preview)

    def _refresh_preview(self) -> bool:
        self._files_changed = 0
        preview = _optional("core.preview")
        if self.project is None or preview is None:
            return GLib.SOURCE_REMOVE
        running = preview.current(self.project.path)
        how = preview.follows_changes(running.plan) if running is not None and running.plan is not None else ""
        if how == "restart":
            self.restart_preview()
        elif how == "reload":
            self.stage.reload()
        return GLib.SOURCE_REMOVE

    def _run_config_changed(self) -> bool:
        self._run_changed = 0
        if self.project is not None and not self._held_stopped():
            self.restart_preview()
        return GLib.SOURCE_REMOVE

    def _on_space(self, **payload) -> None:
        """Services, bugs or analytics moved: the rail's quiet line and the Stage's Live line follow."""
        if self._mine(payload) and self.project is not None:
            self.project.forget_attention()
            self.stage.set_live_services(self._live_services())
            self._schedule_sync()

    def _on_request(self, **payload) -> None:
        if self._mine(payload):
            self._answer_requests()

    def _on_usage(self, **_payload) -> None:
        if self.project is not None:
            self.project.forget_attention()
        self._fill_usage()
        if self.goal_id is not None:
            agent = self._agent_for(self.goal_id)
            self.composer.set_agent(agent, connection_subtitle(agent))
            self.composer.set_plan_link(plan_usage_url(agent))

    def _on_preview(self, **payload) -> None:
        if not self._mine(payload):
            return
        state = str(payload.get("state") or "")
        self.stage.set_preview(state, str(payload.get("url") or ""), str(payload.get("error") or ""),
                               str(payload.get("label") or ""), str(payload.get("target") or ""),
                               str(payload.get("device") or ""))
        good = state == "running"
        for dot in (self.stage_dot, self.phone_stage_dot):
            dot.set_visible(good)

    def _on_agent(self, **payload) -> None:
        if not self._mine(payload):
            return
        goal_id, state = str(payload.get("goal_id") or ""), str(payload.get("state") or "")
        if not state:
            return
        before = self.agent_states.get(goal_id, "")
        self.agent_states[goal_id] = state
        self._notify(goal_id, state, before)
        if goal_id != self.goal_id:
            return
        name = self._agent_for(goal_id).get("label") or "Ari"
        busy = state in BUSY_STATES
        self.conversation.set_thinking(name if busy and not self._step_running() else None,
                                       "starting" if state == "starting" else "working")
        self.composer.set_busy(busy)
        # Sign-in, errors and going offline each come with their own card in the thread (written by the
        # runner, with Try again), so here the composer just says it calmly.
        if state == "offline":
            self.composer.set_placeholder(f"You’re offline. {name} carries on once you’re back and press Try again.")
        elif state == "needs-sign-in":
            self.composer.set_placeholder(f"{name} needs you to sign in. The card above has the way on.")

    def _notify(self, goal_id: str, state: str, before: str) -> None:
        """A desktop notification when an agent needs the person, or finishes, and they aren't looking at that
        conversation. Clicking it opens the conversation."""
        looking = self.is_active() and goal_id == self.goal_id and self.split.get_show_sidebar()
        if looking or self.project is None:
            return
        name = self._agent_for(goal_id).get("label") or "Ari"
        goal = self.project.goal(goal_id) or {}
        title = str(goal.get("display_title") or goal.get("title") or "A conversation")
        if state == "waiting":
            words, body = f"{name} needs you", f"{title}: a question only you can answer."
        elif state == "needs-sign-in":
            words, body = f"{name} needs you to sign in", title
        elif state == "error":
            words, body = f"{name} stopped", f"{title}: something went wrong. Open it to see what and try again."
        elif state == "idle" and before in BUSY_STATES:
            words, body = f"{name} finished", title
        else:
            return
        app = self.get_application()
        if app is None:
            return
        if app.lookup_action("open-conversation") is None:
            action = Gio.SimpleAction.new("open-conversation", GLib.VariantType.new("s"))
            action.connect("activate", lambda _a, value: (self.present(), self.open_conversation(value.get_string())))
            app.add_action(action)
        note = Gio.Notification.new(words)
        note.set_body(body)
        notification_action(note, "app.open-conversation", GLib.Variant.new_string(goal_id))
        if state in ("waiting", "needs-sign-in"):
            note.set_priority(Gio.NotificationPriority.HIGH)
        app.send_notification(f"conversation-{goal_id}", note)
        self._unread.add(goal_id)
        self.rail.set_unread(self._unread)

    def _schedule_sync(self) -> None:
        if not self._sync_source:
            self._sync_source = GLib.timeout_add(100, self._sync_now)

    def _sync_now(self) -> bool:
        self._sync_source = 0
        project = self.project
        if project is None:
            return GLib.SOURCE_REMOVE
        project._goals_cache = None   # this sync is because something changed: read the folder once, freshly
        goals = project.goals()
        attention = project.attention()
        changed = attention != getattr(self, "_attention", None)
        self._attention = attention
        if [g["id"] for g in goals] != self.rail.goal_order() or changed:
            self.rail.show(self.projects, project, goals, self._agent_for(GENERAL_CHAT_ID), counts=self._area_counts(),
                           status=self._project_status(project), live=bool(self._live_url()))
        else:
            for goal in goals:
                self.rail.update_goal(goal)
            self.rail.set_counts(self._area_counts())
        if self.goal_id is not None:
            self.conversation.sync(project.thread(self.goal_id))
            if self.agent_states.get(self.goal_id, "") in BUSY_STATES and not self._step_running():
                self.conversation.set_thinking(self._agent_for(self.goal_id).get("label") or "Ari")
            goal = next((g for g in goals if g["id"] == self.goal_id), None)
            if goal is not None and not goal.get("is_chat"):
                self._refresh_heading(goal)
            if goal is not None:
                # The agent may have changed underneath (a handoff to Codex 2): the composer and its notice follow.
                agent = self._agent_for(self.goal_id)
                key = (agent.get("id"), agent.get("label"))
                if key != getattr(self, "_composer_agent", None):
                    self._composer_agent = key
                    self.composer.set_agent(agent, connection_subtitle(agent))
                    self.composer.set_plan_link(plan_usage_url(agent))
                    self._banner(goal, agent)
        self._sync_island()
        return GLib.SOURCE_REMOVE

    def _refresh_heading(self, goal: dict) -> None:
        agent = self._agent_for(goal["id"])
        where = self._where_words(agent.get("where")) if agent.get("where") not in (None, "", "this-computer") \
            else "this computer"
        self.conversation.replace_meta(goal_meta(goal.get("status", "working"), self._started_words(goal), agent, where,
                                                 on_state=self.conversation.show_open_question))

    def _sync_island(self) -> None:
        """The phone's title island: where you are, and how it's going."""
        if self._page is not None and self.project is not None:
            self._title_page()
            return
        if self.project is None:
            self.island.set_title("New project", "Playground")
            return
        if self._phone and not self.sheet.get_open():
            # On a phone the page is the area: the island names it.
            words = {key: name for key, name, _icon in AREAS}
            searching = self.space is not None and getattr(self.space, "at", "") == "search"
            self.island.set_title("Search" if searching else words.get(self.rail.area, "Tasks"), self.project.name)
            return
        goal = self.project.goal(self.goal_id or "") or {}
        if goal.get("is_chat") or not goal:
            agent = self._agent_for(GENERAL_CHAT_ID)
            self.island.set_title(f"Talk to {agent.get('label') or 'Ari'}", self.project.name)
            return
        status = goal.get("status", "working")
        self.island.set_title(str(goal.get("title") or ""), f"{self.project.name} · {w.state_label(status)}")

    # ── keys and closing ──────────────────────────────────────────────────
    def _key(self, _controller, keyval, _code, state) -> bool:
        if keyval == Gdk.KEY_Escape and self.stage.pointing:
            self.stage.set_pointing(False)
            return True
        if keyval == Gdk.KEY_F6:
            # F6 moves between the rail, the conversation and the Stage.
            focus = self.get_focus()
            order = [self.rail, self.composer, self.stage]
            current = next((i for i, part in enumerate(order) if focus is not None and focus.is_ancestor(part)), -1)
            target = order[(current + (-1 if state & Gdk.ModifierType.SHIFT_MASK else 1)) % len(order)]
            if target is self.rail:
                self.rail.focus_current()
            elif target is self.composer:
                self.composer.focus()
            else:
                self.stage.child_focus(Gtk.DirectionType.TAB_FORWARD)
            return True
        return False

    def _closing(self, *_args) -> bool:
        for token in self._tokens:
            self.bus.unsubscribe(token)
        self._tokens = []
        if self._sync_source:
            GLib.source_remove(self._sync_source)
            self._sync_source = 0
        self._quitting = True
        self._leave_project()
        self.stage.shutdown()
        for runner in self.runners.values():
            close = getattr(runner, "close", None) or getattr(runner, "stop", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    log.exception("a runner didn't stop cleanly")
        preview = _optional("core.preview")
        if preview is not None:
            preview.stop_all()     # (the application's do_shutdown does it again, in case of another window)
        return False


def notification_action(note, action: str, target) -> None:
    """Set a notification's click action across PyGObject versions: some expose the
    C name ``set_default_action_and_target_value``, others only the introspected
    ``set_default_action_and_target`` (which takes the same GVariant target)."""
    setter = getattr(note, "set_default_action_and_target_value", None) or note.set_default_action_and_target
    setter(action, target)


def _quietly(work, *args) -> None:
    try:
        work(*args)
    except Exception:  # noqa: BLE001 - bringing files up to date never stops a project opening
        log.exception("bringing %s up to date", args[0] if args else "")
