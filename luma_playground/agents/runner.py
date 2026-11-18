# SPDX-License-Identifier: Apache-2.0
"""GoalRunner: one goal's conversation with one connection.

The UI calls ``send``, ``answer_decision``, ``switch`` and ``stop``. Each
returns at once; the work happens on the runner's worker thread, and
everything the agent does becomes a **card** in ``threads/<goal>.jsonl``
(through ``core.store``), plus events on the bus:

- agent text is coalesced into one ``message`` card per message;
- tool calls are grouped into a ``step`` card (title, files touched, state,
  elapsed time, spend if it costs money). A step card is appended when it
  starts and again, with the same ``id``, when it finishes: **readers keep the
  last entry for an id**;
- the agent's plan becomes a ``plan`` card (same rule: one id per turn, the
  last entry wins);
- a permission request becomes a ``decision`` card, backed by
  ``pending/<id>.json`` so it shows up like any other question;
- a switch of agent or account writes a ``handoff`` card with the brief.

Live progress that isn't worth a line in the thread (streaming text, a step's
tool calls as they happen, plan ticks) goes out as ``agent.state`` events:
``bus.publish("agent.state", project=..., goal_id=..., state=..., **extra)``.

States: ``starting``, ``working``, ``waiting`` (for the person), ``idle``,
``needs-sign-in`` (with ``methods``), ``handoff`` (with ``brief``), ``stopped``,
``error`` (with ``error``, plain words). ``working`` may carry
``partial_text``, ``step`` or ``plan`` for live display.

Threads: the worker thread does all blocking I/O; the ACP reader and
dispatcher threads deliver updates; bus callbacks run on whichever thread
published. The GTK layer marshals with ``GLib.idle_add``. Nothing here
blocks the caller.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import base64
import json
import logging
import re
import mimetypes
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any, Callable

from . import acp, sandbox
from .connections import Connection, ConnectionRuleError, LaunchError, check_may_run, launch_spec
from .handoff import Handoff, build_brief
from ..core import events
from ..core.model import GoalStatus, PendingDecision, Option, Answer, ThreadEntry, now_iso
from ..core.paths import new_id, is_valid_id
from ..core.store import AlreadyAnswered, ProjectStore, StoreError, atomic_write_json, read_json

log = logging.getLogger(__name__)

PERMISSION_TIMEOUT_S = 30 * 60
STREAM_THROTTLE_S = 0.12
TURN_END = ("(Playground: before you end this turn, keep the project true: your task's status, outcome and "
            "Handoff, and a knowledge note for any decision, rule, lesson or mistake the next agent will need. "
            "Skip what didn't change.)")
LIVE_PULL_S = 30            # an agent on another computer: its work comes back this often during a turn
AUTO_ALLOW_KINDS = {"read", "search", "think", "fetch", "edit", "move", "switch_mode"}
POLICIES = ("auto-in-project", "ask", "allow-all")


# ── Session update → cards ─────────────────────────────────────────────────

def _status_word(status: str | None) -> str:
    return {"completed": "done", "failed": "failed"}.get(status or "", "running")


def _is_bookkeeping(name: str, title: str) -> bool:
    """Calls to Playground's own tools make their own cards; they aren't steps."""
    text = f"{name} {title}".lower()
    return "playground_" in text or "mcp__playground" in text


def _is_step_report(name: str, title: str) -> bool:
    return "step_report" in f"{name} {title}".lower()


def _report_args(raw: Any) -> dict:
    """The arguments of a ``playground_step_report`` call, whatever the agent wraps them in
    (Claude passes them as is; Codex as ``{"server", "tool", "arguments"}``)."""
    if not isinstance(raw, dict):
        return {}
    for key in ("arguments", "args", "input", "params"):
        inner = raw.get(key)
        if isinstance(inner, dict) and "title" in inner:
            return inner
        if isinstance(inner, str) and inner.strip().startswith("{"):
            try:
                parsed = json.loads(inner)
            except ValueError:
                continue
            if isinstance(parsed, dict) and "title" in parsed:
                return parsed
    return raw if "title" in raw else {}


class CardTranslator:
    """Turns ``session/update`` notifications into thread cards. Pure logic:
    ``emit(entry)`` appends a ThreadEntry; ``live(**state)`` publishes progress.
    """

    def __init__(self, project_root: str, emit: Callable[[ThreadEntry], Any], *, by: str = "",
                 live: Callable[..., None] | None = None, show_money: bool = False,
                 clock: Callable[[], float] = time.monotonic, cost_start: float | None = None,
                 find_report: Callable[[str, set], str | None] | None = None) -> None:
        self.root = os.path.realpath(project_root)
        self.emit = emit
        self.by = by
        self.live = live or (lambda **_: None)
        self.show_money = show_money
        self.clock = clock
        self.cost_start = cost_start      # the session's running cost when this turn began
        self.cost_total: float | None = cost_start
        self.currency = "USD"
        self.context_used: int | None = None
        self.context_size: int | None = None
        self.replaying = False
        self.files_touched: list[str] = []
        self._msg: list[str] = []
        self._msg_id: str | None = None
        self._step: dict | None = None
        self._plan_id: str | None = None
        self._plan_entries: list[dict] = []
        self._plan_written: list[dict] | None = None
        self._last_stream = 0.0
        self._skipped: set[str] = set()
        # Step folding: the agent's own step_report joins the automatic card for the same work.
        self.find_report = find_report      # (title, ids written here) -> the id of the agent's own card
        self._reports: dict[str, dict] = {}  # step_report call id -> {"args", "step", "done"}
        self._last_closed: dict | None = None
        self._own_ids: set[str] = set()
        self._unfolded: list[dict] = []     # reports whose card wasn't found yet
        self._lock = threading.RLock()

    # entry points

    def handle(self, update: dict) -> None:
        if self.replaying:
            return
        kind = update.get("sessionUpdate")
        with self._lock:
            if kind == "agent_message_chunk":
                self._text(update)
            elif kind == "tool_call":
                self._tool_call(update, new=True)
            elif kind == "tool_call_update":
                self._tool_call(update, new=False)
            elif kind == "plan":
                self._plan(update)
            elif kind == "usage_update":
                self._usage(update)
            elif kind == "agent_thought_chunk":
                self.live(state="working", thinking=True)

    def flush_text(self) -> None:
        """Write the text so far, so a card that follows (a question) comes after it."""
        with self._lock:
            self._flush_message()

    @property
    def turn_cost(self) -> float | None:
        if self.cost_total is None:
            return None
        return max(0.0, self.cost_total - (self.cost_start or 0.0))

    def discard_text(self) -> None:
        """Drop text not yet written (an agent's own "Failed to authenticate" before the sign-in card)."""
        with self._lock:
            self._msg = []
            self._msg_id = None

    def finish(self, stop_reason: str = "end_turn") -> None:
        """The turn ended: write what's still open."""
        with self._lock:
            self._flush_message()
            if self._step is not None:
                crashed = stop_reason == "error"
                self._close_step(failed=crashed or (stop_reason not in ("end_turn", "cancelled") and self._last_failed()),
                                 stopped=crashed)
            for report in list(self._unfolded):
                self._fold_card(report)
            if self._plan_id and self._plan_entries and self._plan_entries != self._plan_written:
                self._write_plan()
            self._plan_id = None
            self._plan_entries = []
            self._plan_written = None

    # messages

    def _text(self, update: dict) -> None:
        content = update.get("content") or {}
        text = content.get("text") if isinstance(content, dict) and content.get("type") == "text" else None
        if not text:
            return
        if self._step is not None and self._step_settled():
            self._close_step(failed=self._last_failed())
        message_id = update.get("messageId")
        if message_id and self._msg_id and message_id != self._msg_id and self._msg:
            self._flush_message()
        self._msg_id = message_id or self._msg_id
        self._msg.append(text)
        now = self.clock()
        if now - self._last_stream >= STREAM_THROTTLE_S:
            self._last_stream = now
            self.live(state="working", partial_text="".join(self._msg))

    def _flush_message(self) -> None:
        text = "".join(self._msg).strip()
        self._msg = []
        self._msg_id = None
        if text:
            self.emit(ThreadEntry(kind="message", role="agent", text=text, by=self.by))

    # steps

    def _rel(self, path: Any) -> str | None:
        if not isinstance(path, str) or not path:
            return None
        rel = sandbox.relative_to_project(self.root, path)
        return rel if rel and rel != "." else None

    def _tool_call(self, update: dict, new: bool) -> None:
        call_id = str(update.get("toolCallId") or "")
        if not call_id:
            return
        name = str(update.get("name") or "")
        title = str(update.get("title") or "")
        step = self._step
        known = step is not None and call_id in step["calls"]
        if not known:
            if call_id in self._reports:
                self._report_update(call_id, update)
                return
            if call_id in self._skipped:
                return
            if _is_bookkeeping(name, title):
                if _is_step_report(name, title):
                    target = step if step is not None and step["calls"] else self._last_closed
                    if target is not None and target.get("reported_by"):
                        target = None  # that work already has the agent's words
                    self._reports[call_id] = {"args": {}, "step": target, "done": False}
                    self._report_update(call_id, update)
                    return
                self._skipped.add(call_id)
                return
            if not new and not title:
                return  # an update for a call we never saw; nothing useful to show
            self._flush_message()
            if step is None:
                step = self._open_step()
            step["calls"][call_id] = {"title": title or "Working", "kind": update.get("kind") or "other",
                                      "status": "running"}
        call = step["calls"][call_id]
        if title:
            call["title"] = _clean_title(title, self.root)
        if update.get("kind"):
            call["kind"] = update["kind"]
        if update.get("status"):
            call["status"] = _status_word(update.get("status"))
        for loc in update.get("locations") or []:
            self._touch(step, loc.get("path") if isinstance(loc, dict) else None)
        for item in update.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "diff":
                self._touch(step, item.get("path"))
        step["title"] = _clean_title(call["title"], self.root)
        self.live(state="working", step=self._step_data(step, final=False))

    def _touch(self, step: dict, path: Any) -> None:
        rel = self._rel(path)
        if rel and rel not in step["files"]:
            step["files"].append(rel)
        if rel and rel not in self.files_touched:
            self.files_touched.append(rel)

    # folding the agent's own step report into the automatic card

    def _report_update(self, call_id: str, update: dict) -> None:
        report = self._reports[call_id]
        args = _report_args(update.get("rawInput"))
        if args:
            report["args"] = args
        if report["done"] or _status_word(update.get("status")) != "done" or update.get("status") is None:
            return
        report["done"] = True
        step = report["step"]
        title = " ".join(str(report["args"].get("title") or "").split())[:160]
        if step is None or not title:
            return  # nothing automatic to join: the agent's own card stands alone
        step["agent_title"] = title
        step["reported_by"] = call_id
        for path in report["args"].get("files") or []:
            self._touch(step, os.path.join(self.root, path) if isinstance(path, str) and not os.path.isabs(path)
                        else path)
        if step is self._step:
            self.live(state="working", step=self._step_data(step, final=False))
        else:  # already written as finished: write it again with the agent's words
            self.emit(ThreadEntry(kind="step", role="agent", by=self.by, id=step["id"],
                                  data=self._step_data(step, final=True)))
        report["title"] = title
        self._fold_card(report)

    def _fold_card(self, report: dict) -> None:
        """Mark the agent's own card (written by the MCP server) as folded into ours."""
        step = report.get("step")
        if step is None or self.find_report is None or report.get("folded"):
            return
        try:
            other = self.find_report(report.get("title") or "", set(self._own_ids))
        except Exception:  # noqa: BLE001
            other = None
        if not other:
            if report not in self._unfolded:
                self._unfolded.append(report)
            return
        if report in self._unfolded:
            self._unfolded.remove(report)
        report["folded"] = True
        data = {"title": report.get("title") or step.get("agent_title") or "", "state": "done",
                "files": list(step["files"]), "folded_into": step["id"]}
        self.emit(ThreadEntry(kind="step", role="agent", by=self.by, id=other, data=data))

    def _open_step(self) -> dict:
        self._step = {"id": new_id("step", 8), "title": "Working", "files": [], "calls": {},
                      "started": self.clock(), "cost_start": self.cost_total}
        self._own_ids.add(self._step["id"])
        self.emit(ThreadEntry(kind="step", role="agent", by=self.by, id=self._step["id"],
                              data=self._step_data(self._step, final=False)))
        return self._step

    def _step_settled(self) -> bool:
        return all(c["status"] != "running" for c in self._step["calls"].values()) if self._step else True

    def _last_failed(self) -> bool:
        if not self._step or not self._step["calls"]:
            return False
        return list(self._step["calls"].values())[-1]["status"] == "failed"

    def _close_step(self, failed: bool = False, stopped: bool = False) -> None:
        step = self._step
        self._step = None
        if step is None:
            return
        self._last_closed = step
        for call in step["calls"].values():
            if call["status"] == "running":
                call["status"] = "failed" if failed else "done"
        step["state"] = "failed" if failed else "done"
        if stopped:
            step["stopped"] = True
        step["cost_end"] = self.cost_total
        self.emit(ThreadEntry(kind="step", role="agent", by=self.by, id=step["id"],
                              data=self._step_data(step, final=True)))

    def _step_data(self, step: dict, final: bool) -> dict:
        calls = list(step["calls"].values())
        auto = summarize_step(calls, step["files"]) if final else step["title"]
        title = step.get("agent_title") or auto
        if final and "elapsed_final" not in step:
            step["elapsed_final"] = round(self.clock() - step["started"], 1)
        elapsed = step["elapsed_final"] if final else round(self.clock() - step["started"], 1)
        data = {"title": title, "state": step.get("state", "running") if final else "running",
                "files": list(step["files"]), "elapsed_s": elapsed,
                "spend_text": "", "tool_calls": [dict(c) for c in calls]}
        if step.get("agent_title"):
            data["reported"] = True          # the title is the agent's own words
            data["auto_title"] = auto
        if step.get("stopped"):
            data["stopped"] = True           # the agent stopped partway (a crash or a lost connection)
        cost_now = step.get("cost_end", self.cost_total) if final else self.cost_total
        if self.show_money and cost_now is not None and step.get("cost_start") is not None:
            spent = cost_now - step["cost_start"]
            if spent > 0:
                data["spend_text"] = money(spent, self.currency)
        return data

    # plan

    def _plan(self, update: dict) -> None:
        entries = [e for e in update.get("entries") or [] if isinstance(e, dict) and e.get("content")]
        if not entries:
            return
        changed_contents = [e.get("content") for e in entries] != [e.get("content") for e in self._plan_entries]
        self._plan_entries = [{"content": str(e.get("content")), "status": str(e.get("status") or "pending"),
                               "priority": str(e.get("priority") or "medium")} for e in entries]
        if self._plan_id is None:
            self._plan_id = new_id("plan", 8)
        if changed_contents:
            self._flush_message()
            self._write_plan()
        self.live(state="working", plan=self._plan_data())

    def _plan_data(self) -> dict:
        count = len(self._plan_entries)
        size = "small" if count <= 2 else "medium" if count <= 6 else "large"
        return {"stories": [{"title": e["content"], "checks": []} for e in self._plan_entries], "size": size,
                "estimate": {}, "entries": [dict(e) for e in self._plan_entries]}

    def _write_plan(self) -> None:
        self.emit(ThreadEntry(kind="plan", role="agent", by=self.by, id=self._plan_id, data=self._plan_data()))
        self._plan_written = [dict(e) for e in self._plan_entries]

    # usage

    def _usage(self, update: dict) -> None:
        cost = update.get("cost")
        if isinstance(cost, dict) and isinstance(cost.get("amount"), (int, float)):
            self.cost_total = float(cost["amount"])
            self.currency = str(cost.get("currency") or "USD")
        if isinstance(update.get("used"), int):
            self.context_used = update["used"]
        if isinstance(update.get("size"), int):
            self.context_size = update["size"]


def money(amount: float, currency: str = "USD") -> str:
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, "")
    if amount < 0.01:
        return f"under 1 cent" if symbol == "$" else f"under {symbol}0.01"
    return f"{symbol}{amount:,.2f}" if symbol else f"{amount:,.2f} {currency}"


def _clean_title(title: str, root: str) -> str:
    text = " ".join(str(title).replace("`", "").split())
    if root and root in text:
        text = text.replace(root + os.sep, "").replace(root, ".")
    return text[:140] or "Working"


def summarize_step(calls: list[dict], files: list[str]) -> str:
    """A plain one-line title for a finished step: 'Changed hello.txt and ran a command'."""
    if len(calls) == 1:
        return calls[0]["title"]
    edits = [c for c in calls if c.get("kind") in ("edit", "delete", "move")]
    runs = [c for c in calls if c.get("kind") == "execute"]
    reads = [c for c in calls if c.get("kind") in ("read", "search", "fetch")]
    parts = []
    if edits:
        changed = len(files) or len(edits)
        parts.append(f"changed {files[0]}" if changed == 1 and files else f"changed {changed} files")
    if runs:
        parts.append("ran a command" if len(runs) == 1 else f"ran {len(runs)} commands")
    if reads and not parts:
        parts.append("looked through the project" if len(reads) > 1 else reads[0]["title"])
    if not parts:
        parts.append(f"did {len(calls)} things")
    text = " and ".join(parts) if len(parts) <= 2 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return text[0].upper() + text[1:]


# ── Permissions as decisions ──────────────────────────────────────────────

_CONSEQUENCE = {
    "allow_once": "Just this time.",
    "allow_always": "Don't ask again for this.",
    "reject_once": "The agent will try another way.",
    "reject_always": "Never for this; the agent won't ask again.",
}
_QUESTION = {
    "execute": "{agent} wants to run a command",
    "edit": "{agent} wants to change a file",
    "delete": "{agent} wants to delete something",
    "move": "{agent} wants to move or rename files",
    "fetch": "{agent} wants to fetch something from the internet",
}


@dataclass
class _Wait:
    decision_id: str
    session_id: str
    options: list[dict]
    event: threading.Event = field(default_factory=threading.Event)
    choice: str | None = None


# ── The runner ─────────────────────────────────────────────────────────────

AgentFactory = Callable[..., Any]


def default_agent_factory(connection: Connection, root: str, handlers: acp.ProjectHandlers, *, goal_id: str,
                          bus: events.EventBus, log_path: str, registry=None) -> Any:
    """An AcpClient for ACP connections, an AriAgent for Ari and local models.

    The ACP agent's whole program runs fenced in (``sandbox.confine_agent``)
    when the connection says so, or by default when the project is shared. Pi
    is an in-process adapter over Pi's own RPC protocol, but its program is
    launched and fenced the same way."""
    if connection.provider == "pi":
        from .pi import PiAgent
        argv, env = launch_spec(connection, registry)
        env["PLAYGROUND_ACTOR"] = connection.id
        env["PLAYGROUND_GOAL"] = goal_id
        servers = mcp_servers_for(root, goal_id, connection.id)
        confinement = None
        if sandbox.agent_sandbox_wanted(connection.sandbox, shared_project=sandbox.project_is_shared(root)):
            readable = [os.path.dirname(s["command"]) for s in servers
                        if os.path.isabs(s["command"])]
            try:
                confinement = sandbox.confine_agent(
                    argv, env, project_root=root, provider=connection.provider,
                    config_home=connection.config_home, extra_readable=readable)
            except sandbox.SandboxUnavailable as error:
                why = ("this connection is set to always run fenced in" if connection.sandbox == "on"
                       else "this project is shared with other people")
                raise LaunchError(f"{error} {connection.label} won't start without it, because {why}. "
                                  "To run it anyway, turn off \"Keep it inside the project\" for this connection."
                                  ) from error
        agent = PiAgent(root, handlers, argv=confinement.argv if confinement else argv,
                        env=confinement.env if confinement else env, goal_id=goal_id,
                        actor=connection.id, mcp_servers=servers, log_path=log_path,
                        on_close=confinement.cleanup if confinement else None)
        agent.confinement = confinement
        return agent
    if connection.is_acp:
        from . import computers
        computer = computers.for_connection(connection)
        if computer is not None:
            # On another computer: its own copy of the project there, its own files and terminal (computers.py).
            try:
                project_id = ProjectStore(root).read_project().id
            except Exception:  # noqa: BLE001
                project_id = ""
            argv, env, remote_cwd = computers.remote_launch(connection, computer, root, registry,
                                                            project_id=project_id)
            client = acp.AcpClient(argv, env=env, cwd=root, handlers=handlers, stderr_log=log_path,
                                   name=connection.id, login_argv=argv, login_env=env, remote_cwd=remote_cwd)
            client.confinement = None
            client.computer = computer
            return client
        argv, env = launch_spec(connection, registry)
        env["PLAYGROUND_ACTOR"] = connection.id
        env["PLAYGROUND_GOAL"] = goal_id
        confinement = None
        if sandbox.agent_sandbox_wanted(connection.sandbox, shared_project=sandbox.project_is_shared(root)):
            readable = [os.path.dirname(s["command"]) for s in mcp_servers_for(root, goal_id, connection.id)
                        if os.path.isabs(s["command"])]
            # A sub-project is a focus, not a fence: its agents may change anything in the whole family.
            from ..core import family
            top = family.root_of(root)
            family_wide = [str(top)] if os.path.realpath(top) != os.path.realpath(root) else []
            try:
                confinement = sandbox.confine_agent(argv, env, project_root=root, provider=connection.provider,
                                                    config_home=connection.config_home, extra_readable=readable,
                                                    extra_writable=family_wide)
            except sandbox.SandboxUnavailable as error:
                why = ("this connection is set to always run fenced in" if connection.sandbox == "on"
                       else "this project is shared with other people")
                raise LaunchError(f"{error} {connection.label} won't start without it, because {why}. "
                                  "To run it anyway, turn off \"Keep it inside the project\" for this connection."
                                  ) from error
        client = acp.AcpClient(confinement.argv if confinement else argv, env=confinement.env if confinement else env,
                               cwd=root, handlers=handlers, stderr_log=log_path, name=connection.id,
                               login_argv=argv, login_env=env,
                               on_close=confinement.cleanup if confinement else None)
        client.confinement = confinement
        return client
    from .ari import AriAgent, endpoint_for
    return AriAgent(root, handlers, endpoint=endpoint_for(connection), bus=bus, goal_id=goal_id,
                    actor=connection.id)


def _as_options(value) -> dict:
    """A conversation's saved agent settings ({connection: {option: value}}); the goal's front matter keeps
    them as JSON text."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return {str(k): dict(v) for k, v in value.items() if isinstance(v, dict)} if isinstance(value, dict) else {}


def mcp_servers_for(root: str, goal_id: str, actor: str) -> list[dict]:
    """The project's Playground MCP server, in ACP's form, told which goal and actor it serves."""
    config = read_json(Path(root) / ".mcp.json") or {}
    servers = config.get("mcpServers") if isinstance(config, dict) else None
    out = []
    for name, spec in (servers or {}).items():
        if not isinstance(spec, dict) or not spec.get("command"):
            continue  # http servers etc. are left to the agent's own config
        env = [{"name": str(k), "value": str(v)} for k, v in (spec.get("env") or {}).items()]
        if name == "playground":
            env += [{"name": "PLAYGROUND_ACTOR", "value": actor}, {"name": "PLAYGROUND_GOAL", "value": goal_id}]
        out.append({"name": str(name), "command": str(spec["command"]),
                    "args": [str(a) for a in spec.get("args") or []], "env": env})
    return out


class GoalRunner:
    """Drives one goal's conversation with one connection. See the module docstring."""

    def __init__(self, project: Any, goal_id: str, connection: Connection, bus: events.EventBus | None = None, *,
                 person: str = "", agent_factory: AgentFactory | None = None, registry=None,
                 permission_policy: str = "auto-in-project", sandbox_policy: sandbox.SandboxPolicy | None = None,
                 team_task: bool = False, permission_timeout: float = PERMISSION_TIMEOUT_S,
                 usage_module: Any = "default") -> None:
        root = _project_root(project)
        if not is_valid_id(goal_id):
            raise ValueError(f"not a goal id: {goal_id!r}")
        if permission_policy not in POLICIES:
            raise ValueError(f"permission_policy must be one of {POLICIES}")
        self.root = os.path.abspath(root)            # as the app names it (events carry this)
        self.real_root = os.path.realpath(root)      # for path checks
        self.goal_id = goal_id
        self.connection = connection
        self.bus = bus if bus is not None else events.bus
        self.store = ProjectStore(self.root, bus=self.bus)
        self.person = person or connection.owner
        self.agent_factory = agent_factory or default_agent_factory
        self.registry = registry
        self.permission_policy = permission_policy
        self.sandbox_policy = sandbox_policy or sandbox.SandboxPolicy(self.root)
        self.team_task = team_task
        self.permission_timeout = permission_timeout
        if usage_module == "default":
            try:
                from ..core import usage as usage_module
            except Exception:  # noqa: BLE001
                usage_module = None
        self.usage = usage_module

        self._agent: Any = None
        self._session_id: str | None = None
        self._session_note: str = ""          # "new" | "resumed" | "loaded"
        self._needs_context = True            # first prompt of a session gets the goal context
        self._handoff: Handoff | None = None  # brief waiting to go to the next agent
        self._handoff_entry_id: str | None = None
        self._translator: CardTranslator | None = None
        self._session_cost: float | None = None
        self._prompted = False
        self._turn_started = 0.0
        self._busy = threading.Event()
        self._pull_lock = threading.Lock()
        self._answered: list[tuple[str, str]] = []     # (decision id, note) answers not yet seen by the agent
        self._told: list[str] = []                       # results of long work (box, installs) to pass on
        self._stopped_by_person = False
        self._closed = False
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.RLock()
        self._waits: dict[str, _Wait] = {}
        self._last_failed_text: str | None = None
        self._last_failed_attachments: list[str] = []
        self._failure_card: ThreadEntry | None = None   # the card with Try again / Sign in, until it's fixed
        self._resume_note = False                       # the next prompt says the last turn was cut off
        self.sandbox_backend = ""                       # "bwrap" | "seatbelt" | "" (not fenced in)
        self.state = "idle"
        # What we already know before the agent starts: the last session's settings, or
        # OpenCode's cached models. The ACP list replaces these as soon as the agent is up.
        self._options: list[dict] = self._seed_options()
        self._worker = threading.Thread(target=self._work, name=f"goal-{goal_id}", daemon=True)
        self._worker.start()
        self._sub = self.bus.subscribe(events.DECISION_ANSWERED, self._on_answered)

    # ── public API (all return at once) ──

    @property
    def busy(self) -> bool:
        return self._busy.is_set()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until queued work is done (tests and command-line tools; never the UI thread)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        settled = 0
        while settled < 3:
            if self._busy.is_set() or not self._queue.empty() or self._queue.unfinished_tasks:
                settled = 0
            else:
                settled += 1
            if deadline is not None and time.monotonic() > deadline:
                return False
            time.sleep(0.05)
        return True

    def send(self, text: str, attachments: list[str] | None = None) -> ThreadEntry:
        """The person said something. Appends their card now; the agent runs on the worker."""
        text = (text or "").strip()
        if not text and not attachments:
            raise ValueError("Say something first.")
        if self._closed:
            raise RuntimeError("This conversation was closed.")
        data = {"attachments": [sandbox.relative_to_project(self.root, a) or a for a in attachments]} if attachments else {}
        entry = self.store.append_thread(self.goal_id, ThreadEntry(kind="message", role="person", text=text,
                                                                   by=self.person, data=data))
        self._queue.put(("turn", text, list(attachments or [])))
        self._announce(text)
        return entry

    def _announce(self, text: str) -> None:
        """Tell the team channel what the person asked for, so other agents working in the project see it
        before their own work collides with it. Short: the first sentence, at most 140 characters."""
        if not text:
            return
        first = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]
        ask = first if len(first) <= 140 else first[:139].rstrip() + "…"
        who = self.person or "someone"
        agent = getattr(self.connection, "label", "") or "an agent"
        try:
            self.store.post_channel(f"{who} asked {agent}: {ask}", by=f"person:{who}", kind="start",
                                    conversation=self.goal_id)
        except (ValueError, OSError, StoreError) as error:   # a secret-looking ask, or a full disk: never block the turn
            log.info("didn't post to the team channel: %s", error)

    def continue_handoff(self, brief: str | None = None) -> None:
        """Start the next agent on the (possibly edited) handoff brief."""
        if brief is not None:
            self.edit_handoff(brief)
        if self._handoff is None:
            raise RuntimeError("There's no handoff waiting.")
        self._queue.put(("turn", "", []))

    def edit_handoff(self, brief: str) -> None:
        """The person edited the brief on the handoff card before the next agent starts."""
        with self._lock:
            if self._handoff is None:
                raise RuntimeError("There's no handoff waiting.")
            self._handoff.brief = brief.strip()
            self.store.append_thread(self.goal_id, ThreadEntry(kind="handoff", role="system", id=self._handoff_entry_id or "",
                                                               by=self.person, data=self._handoff.card_data()))

    def answer_decision(self, decision_id: str, option: str = "", text: str = "") -> None:
        """The person answered a decision card: an agent's permission question or an MCP decision."""
        with self._lock:
            wait = self._waits.get(decision_id)
        if wait is not None:
            if option and not any(o.get("optionId") == option for o in wait.options):
                raise ValueError(f"{option} isn't one of the options.")
            try:
                self.store.write_answer(Answer(id=decision_id, option=option,
                                               label=_option_label(wait.options, option), text=text,
                                               answered_by=self.person))
            except AlreadyAnswered:
                pass
            self._resolve_wait(decision_id, option)
            return
        pending = self.store.read_pending(decision_id)
        answer = self.store.answer_decision(decision_id, option, text=text, by=self.person)
        # An agent's question (playground_decision_ask): if its call is still waiting, the answer goes back
        # through it. If the agent has moved on (its turn ended, or its tool call gave up), the answer comes to
        # it as the start of its next turn, now if it's idle, so it never sits waiting for a reply it missed.
        question = getattr(pending, "question", "") or ""
        chosen = answer.label or answer.text
        if (getattr(pending, "extra", None) or {}).get("source") == "install" and answer.option == "install":
            return          # the app installs it and tells the agent how it went (tell()), once it's done
        note = (f"(Playground: the person answered your question \"{question}\": \"{chosen}\". Carry on on that "
                "basis; don't ask it again.)")
        with self._lock:
            self._answered.append((decision_id, note))
        if not self.busy:
            self._queue.put(("answered",))

    def switch(self, connection: Connection) -> None:
        """Move this goal to another connection. Checks the picker rules now (raises
        ConnectionRuleError); the rest happens on the worker after any running turn
        is stopped. Same program and account: the session carries on natively.
        Anything else: a ``handoff`` card with an editable brief, and
        ``agent.state`` "handoff"; the next agent starts on ``continue_handoff()``
        or the person's next message."""
        connection.validate()
        check_may_run(connection, self.person, goal_owner=self._goal_owner())
        self._cancel_turn()
        self._queue.put(("switch", connection))

    def is_same_account(self, connection: Connection) -> bool:
        return (self.connection.account_key() == connection.account_key()
                and self.connection.is_acp == connection.is_acp)

    def _has_work_to_hand_off(self) -> bool:
        """Nothing to hand over on a conversation where no agent has done anything yet."""
        try:
            entries = self.store.read_thread(self.goal_id)
        except Exception:  # noqa: BLE001
            return False
        for entry in entries:
            if entry.role != "agent":
                continue
            if entry.kind in ("step", "plan", "decision", "proof"):
                return True
            if entry.kind == "message" and str(entry.text or "").strip():
                return True
        return False

    def _switch(self, connection: Connection) -> Handoff | None:
        old = self.connection
        same = self.is_same_account(connection)
        with self._lock:
            self.connection = connection
            if same:
                self._save_session()
                return None
            try:
                self.store.close_stale_steps(self.goal_id, {old.id})
            except (StoreError, OSError, ValueError):
                log.exception("closing steps the previous agent left running")
            self._shutdown_agent()
            self._session_cost = None
            self._needs_context = True
            self._options = self._seed_options()
            if self._has_work_to_hand_off():
                self._handoff = build_brief(self.store, self.goal_id, from_connection=old, to_connection=connection)
                entry = self.store.append_thread(self.goal_id, ThreadEntry(kind="handoff", role="system",
                                                                           by=self.person,
                                                                           data=self._handoff.card_data()))
                self._handoff_entry_id = entry.id
            self._save_session()
        try:
            self.store.update_goal(self.goal_id, lambda g: setattr(g, "connection", connection.id))
        except StoreError:
            pass
        self.store.log(f"Switched to {connection.label}", kind="agent", actor=self.person, goal=self.goal_id,
                       data={"from": old.id, "to": connection.id})
        if self._handoff is not None:
            self._state("handoff", brief=self._handoff.brief)
        else:
            self._state("idle")
        self._publish_options()
        if not self.options():
            self.warm_options()
        return self._handoff

    def stop(self, grace: float = 8.0) -> None:
        """Stop the agent: cancel the turn, close the program, mark the goal paused.
        If the agent ignores the cancel for ``grace`` seconds, its program is ended."""
        self._cancel_turn()
        self._queue.put(("stop",))
        agent = self._agent
        if agent is not None and self.busy:
            def watchdog() -> None:
                if self._busy.wait(0) and not _wait_clear(self._busy, grace):
                    log.warning("agent ignored cancel; ending it")
                    try:
                        agent.close()
                    except Exception:  # noqa: BLE001
                        pass
            threading.Thread(target=watchdog, name="goal-stop", daemon=True).start()

    def cancel(self, grace: float = 10.0) -> bool:
        """The person pressed Stop: end what the agent is doing now. The conversation stays as it is, and the
        next message carries on from there. An agent that ignores the cancel for ``grace`` seconds has its
        program ended (it starts again, resuming its session, with the next message). False if idle."""
        if not self.busy:
            return False
        self._stopped_by_person = True
        self._cancel_turn()
        agent = self._agent

        def watchdog() -> None:
            if _wait_clear(self._busy, grace) or agent is None:
                return
            log.warning("the agent ignored Stop; ending its program")
            try:
                agent.close()
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=watchdog, name="goal-cancel", daemon=True).start()
        return True

    def close(self) -> None:
        """App shutdown: end the agent without touching the goal's status."""
        if self._closed:
            return
        self._closed = True
        self._cancel_turn()
        self._queue.put(("close",))
        self._worker.join(timeout=10)
        if self._worker.is_alive():  # the agent ignored the cancel: end its program
            agent = self._agent
            if agent is not None:
                try:
                    agent.close()
                except Exception:  # noqa: BLE001
                    pass
            self._worker.join(timeout=5)
        self.bus.unsubscribe(self._sub)

    # sign-in

    # the agent's settings: model, reasoning effort, mode (ACP session config options)

    def options(self) -> list[dict]:
        """The settings the agent offers for this conversation, as last seen. OpenCode's
        cached model list means this can be useful before the agent has started."""
        return list(getattr(self, "_options", []) or [])

    def _connection_model(self) -> str:
        return str(getattr(self.connection, "model", "") or "")

    def _seed_options(self) -> list[dict]:
        """Settings known without starting the agent: last session's, then the model cache."""
        remembered = (self._load_session() or {}).get("options") or []
        options = [dict(o) for o in remembered if isinstance(o, dict) and o.get("id")]
        if not options:
            try:
                from . import models
                rows = models.available(self.connection, offline=True)
            except Exception:  # noqa: BLE001 - the cache is a convenience, never a blocker
                rows = []
            if rows:
                options = [{"id": "model", "name": "Model", "category": "model", "type": "select",
                            "currentValue": self._saved_options().get("model"), "options": rows}]
        saved = self._saved_options()
        default = self._connection_model()
        model_ids = {str(o.get("id")) for o in options if o.get("category") == "model"}
        if default and not any(str(key) in model_ids for key in saved):
            for option in options:
                if option.get("category") != "model":
                    continue
                if any(row.get("value") == default for row in option.get("options") or []):
                    option["currentValue"] = default
                break
        return options

    def warm_options(self) -> None:
        """Fetch a model list from the agent's program when the person opens the menu."""
        if not getattr(self.connection, "is_acp", False) or self.options():
            return

        def work() -> list[dict]:
            try:
                from . import models
                return models.available(self.connection)
            except Exception:  # noqa: BLE001
                return []

        def done(rows: list[dict]) -> None:
            if rows and not self._closed and not self.options():
                self._options = [{"id": "model", "name": "Model", "category": "model", "type": "select",
                                  "currentValue": self._saved_options().get("model"), "options": rows}]
                self._publish_options()

        threading.Thread(target=lambda: done(work()), name=f"models-{self.goal_id}", daemon=True).start()

    def prepare(self) -> None:
        """Start the agent (if it isn't) so its settings can be shown. Off the caller's thread."""
        self._queue.put(("prepare",))

    def set_option(self, config_id: str, value: Any) -> None:
        """Choose a setting. It's kept for this conversation and this agent, and applied every time it starts."""
        def change(goal) -> None:
            saved = _as_options(goal.extra.get("agent_options"))
            saved.setdefault(self.connection.id, {})[str(config_id)] = value
            goal.extra["agent_options"] = saved
        try:
            self.store.update_goal(self.goal_id, change)
        except Exception as error:  # noqa: BLE001 - the choice still applies to the running agent
            log.info("couldn't keep the setting: %s", error)
        self._options = [dict(o, currentValue=value) if o.get("id") == config_id else o for o in self.options()]
        self._publish_options()
        self._queue.put(("option", config_id, value))

    def _saved_options(self) -> dict:
        try:
            goal = self.store.read_goal(self.goal_id)
        except Exception:  # noqa: BLE001
            return {}
        return dict(_as_options(goal.extra.get("agent_options")).get(self.connection.id) or {})

    def _apply_option(self, config_id: str, value: Any) -> None:
        agent = self._agent
        if agent is None or not self._session_id or not hasattr(agent, "set_config_option"):
            return          # applied when the agent next starts
        try:
            self._options = agent.set_config_option(self._session_id, config_id, value)
        except Exception as error:  # noqa: BLE001 - said plainly; the conversation carries on
            log.info("the agent didn't take %s=%s: %s", config_id, value, error)
            self._options = agent.config_options(self._session_id)
        self._publish_options()

    def _settle_options(self) -> None:
        """Right after a session starts: put back the person's choices, then say what's on offer."""
        agent = self._agent
        if agent is None or not self._session_id or not hasattr(agent, "config_options"):
            return
        # A few agents repeat no options on a reopen; keep our cached list (OpenCode's
        # models) in that case so choosing a model stays possible from the first message.
        self._options = agent.config_options(self._session_id) or self.options()
        if not self._options:
            remembered = (self._load_session() or {}).get("options") or []
            if remembered and hasattr(agent, "sessions"):
                # Teach the client what the agent offered before, so setting one goes through it as usual.
                agent.sessions.setdefault(self._session_id, {})["configOptions"] = \
                    [o for o in remembered if not str(o.get("id", "")).startswith("__")]
                self._options = agent.config_options(self._session_id) or list(remembered)
        saved = self._saved_options()
        current = {o.get("id"): o.get("currentValue") for o in self._options}
        for config_id, value in saved.items():
            offered = next((o for o in self._options if o.get("id") == config_id), None)
            if offered is None or current.get(config_id) == value:
                continue
            if not any(c.get("value") == value for c in offered.get("options") or []):
                continue
            try:
                self._options = agent.set_config_option(self._session_id, config_id, value)
            except Exception as error:  # noqa: BLE001
                log.info("couldn't put back %s: %s", config_id, error)
        model_ids = {str(o.get("id")) for o in self._options if o.get("category") == "model"}
        default = self._connection_model()
        if default and not any(str(key) in model_ids for key in saved):
            offered = next((o for o in self._options if o.get("category") == "model"), None)
            current = {o.get("id"): o.get("currentValue") for o in self._options}
            if offered is not None and current.get(offered.get("id")) != default and \
                    any(c.get("value") == default for c in offered.get("options") or []):
                try:
                    self._options = agent.set_config_option(self._session_id, str(offered.get("id")), default)
                except Exception as error:  # noqa: BLE001
                    log.info("couldn't use the connection's model: %s", error)
        self._save_session()
        self._publish_options()

    def _publish_options(self) -> None:
        self.bus.publish(events.AGENT_OPTIONS, project=self.root, goal_id=self.goal_id,
                         connection=self.connection.id, options=self.options())

    def sign_in_methods(self) -> list[dict]:
        agent = self._agent
        return list(agent.auth_methods()) if agent is not None and getattr(agent, "info", None) else []

    def sign_in_command(self, method_id: str) -> tuple[list[str], dict]:
        """(argv, env) for a terminal sign-in. Show it in a terminal (VTE); exit 0
        means signed in; then call ``signed_in()``. Playground never sees the token."""
        if self._agent is None or not hasattr(self._agent, "login_command"):
            raise RuntimeError("Start the agent first.")
        return self._agent.login_command(method_id)

    def sign_in_with_agent(self, method_id: str) -> None:
        """For ``type: "agent"`` methods: the agent runs its own sign-in (often a browser)."""
        self._queue.put(("auth", method_id))

    def signed_in(self, retry: bool = True) -> None:
        """Sign-in finished outside the agent; reconnect and, if asked, retry the last message.
        The goal's conversation with the agent is picked up again (same session) when the agent can."""
        self._queue.put(("reconnect", retry))

    @property
    def can_retry(self) -> bool:
        return self._last_failed_text is not None

    def retry(self) -> bool:
        """"Try again" after the agent stopped, lost its connection or ran into a problem.
        Reopens the same conversation when the agent can, and carries on from there.
        Returns False when there's nothing to try again."""
        if self._last_failed_text is None or self._closed:
            return False
        self._queue.put(("retry",))
        return True

    # ── worker ──

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            kind = item[0]
            try:
                if kind == "turn":
                    self._turn(item[1], item[2])
                elif kind == "answered":
                    if self._answer_notes(peek=True):
                        self._turn("", [])
                elif kind == "switch":
                    self._switch(item[1])
                elif kind == "auth":
                    self._authenticate(item[1])
                elif kind == "reconnect":
                    self._shutdown_agent()
                    if item[1] and self._last_failed_text is not None:
                        text, self._last_failed_text = self._last_failed_text, None
                        self._turn(text, self._last_failed_attachments)
                elif kind == "retry":
                    if self._last_failed_text is not None:
                        text, self._last_failed_text = self._last_failed_text, None
                        if self._agent is not None and not getattr(self._agent, "alive", False):
                            self._shutdown_agent()
                        self._resume_note = True
                        self._turn(text, self._last_failed_attachments)
                elif kind == "stop":
                    self._shutdown_agent()
                    self._set_goal_status(GoalStatus.PAUSED, only_from=(GoalStatus.WORKING, GoalStatus.NEEDS_YOU))
                    self._state("stopped")
                elif kind == "close":
                    self._shutdown_agent()
                    return
                elif kind == "prepare":
                    self._ensure_agent()
                    self._publish_options()
                elif kind == "option":
                    self._apply_option(item[1], item[2])
            except Exception:  # noqa: BLE001 - the worker must survive anything
                log.exception("goal runner %s failed on %s", self.goal_id, kind)
                self._busy.clear()
            finally:
                self._queue.task_done()

    def _state(self, state: str, **extra: Any) -> None:
        self.state = state
        self.bus.publish(events.AGENT_STATE, project=self.root, goal_id=self.goal_id, state=state,
                         connection=self.connection.id, **extra)

    def _system(self, text: str, *, entry_id: str = "", **data: Any) -> ThreadEntry:
        return self.store.append_thread(self.goal_id, ThreadEntry(kind="message", role="system", text=text,
                                                                  data=data, by="playground", id=entry_id))

    def _failure(self, text: str, **data: Any) -> None:
        """A card that offers a way on (Try again, Sign in). It's written again, marked
        ``resolved``, once a turn goes through, so its button goes away."""
        self._failure_card = self._system(text, **data)

    def _resolve_failure(self) -> None:
        card, self._failure_card = self._failure_card, None
        if card is None:
            return
        data = {k: v for k, v in card.data.items() if k not in ("action", "methods")}
        data["resolved"] = True
        try:
            self._system(card.text, entry_id=card.id, **data)
        except (StoreError, OSError):
            pass

    def _turn(self, text: str, attachments: list[str]) -> None:
        if self._closed:
            return
        self._busy.set()
        self._turn_started = time.monotonic()
        self._prompted = False
        stop_reason = "error"
        live_pull = None
        try:
            check_may_run(self.connection, self.person, goal_owner=self._goal_owner())
            self._check_caps()
            self._set_goal_status(GoalStatus.WORKING, only_from=None)
            self._state("starting")
            self._ensure_agent()
            self._close_stale_steps()
            self._mirror("push")
            live_pull = self._live_pull()
            self._state("working", sandbox=self.sandbox_backend)
            blocks = self._blocks(text, attachments)
            self._translator = self._new_translator()
            self._prompted = True
            stop_reason = self._agent.prompt(self._session_id, blocks)
            self._save_session()          # remembers when this conversation was last briefed
            # Delivered: the context and any handoff brief don't go again.
            self._needs_context = False
            self._handoff = None
            self._handoff_entry_id = None
            self._translator.finish(stop_reason)
            self._resume_note = False
            self._resolve_failure()
            self._after_turn(stop_reason)
        except acp.AuthRequired as error:
            if self._translator is not None:
                self._translator.discard_text()   # the agent's own "failed to authenticate" says the same
            self._finish_translator("error")
            self._remember_failed(text, attachments)
            methods = self.sign_in_methods()
            self._resolve_failure()
            reason = getattr(error, "reason", "") or "sign-in"
            if reason == "plan-off":
                plain = str(error)
            elif not self.connection.is_acp:
                plain = (f"{self.connection.label} needs you to sign in with ChatGPT. Your password goes only to "
                         "OpenAI, and the sign-in stays in this computer's keyring. Then Try again picks up right here.")
            else:
                plain = (f"{self.connection.label} needs you to sign in again. Playground opens the agent's own "
                         "sign-in; it never sees your password. When you're done, Try again picks up right here.")
            self._failure(plain, action="sign-in", reason=reason, methods=methods, retry_label="Try again")
            self._set_goal_status(GoalStatus.NEEDS_YOU, note="sign in")
            self._state("needs-sign-in", methods=methods, reason="sign-in", detail=getattr(error, "detail", ""))
        except (ConnectionRuleError, LaunchError) as error:
            self._finish_translator("error")
            self._system(str(error), error=True)
            self._set_goal_status(GoalStatus.NEEDS_YOU, note="can't start")
            self._state("error", message=str(error), error=str(error))
        except acp.AcpError as error:
            if self._stopped_by_person:
                # Stop ended an agent that wouldn't stop: that's the person's choice, not a failure.
                self._finish_translator("cancelled")
                self._shutdown_agent(keep_session=True)
                self._person_stopped()
            else:
                self._agent_failed(error, text, attachments)
        finally:
            if live_pull is not None:
                live_pull.set()
            if self._prompted:
                self._mirror("pull")
            self._record_usage(stop_reason)
            if self._translator is not None and self._translator.cost_total is not None:
                self._session_cost = self._translator.cost_total
            self._translator = None
            self.store.refresh_area("journal")      # the turn, in the project's story for whoever comes next
            self._busy.clear()
            if not self._closed and self._answer_notes(peek=True):
                self._queue.put(("answered",))      # answered while it worked, and its own call didn't get it

    def _mirror(self, direction: str) -> None:
        """For an agent on another computer: the project there before its turn, and its work back after."""
        agent = self._agent
        computer = getattr(agent, "computer", None)
        folder = getattr(agent, "remote_cwd", None)
        if computer is None or not folder:
            return
        from . import computers
        user = str(self.connection.extra.get("remote_user") or "")
        try:
            if direction == "push":
                computers.push(computer, self.root, folder, user=user,
                               user_home=str(self.connection.extra.get("remote_home") or ""))
            else:
                with self._pull_lock:
                    computers.pull(computer, self.root, folder, user=user)
        except computers.ComputerError as error:
            if direction == "push":
                raise LaunchError(str(error)) from error
            self._system(f"{error} The work is still on {computer.name}; it comes back after the next turn.",
                         error=True)

    def _live_pull(self) -> threading.Event | None:
        """During a long turn on another computer, its work comes back every LIVE_PULL_S seconds, so the
        preview and the files here keep up. Quiet: a pull that fails waits for the next one, or the last."""
        agent = self._agent
        computer = getattr(agent, "computer", None)
        folder = getattr(agent, "remote_cwd", None)
        if computer is None or not folder:
            return None
        from . import computers
        stop = threading.Event()
        user = str(self.connection.extra.get("remote_user") or "")

        def loop() -> None:
            while not stop.wait(LIVE_PULL_S):
                if not self._pull_lock.acquire(blocking=False):
                    continue
                try:
                    computers.pull(computer, self.root, folder, user=user)
                except Exception:  # noqa: BLE001 - the turn's last pull reports problems
                    pass
                finally:
                    self._pull_lock.release()
        threading.Thread(target=loop, daemon=True, name="remote-live-pull").start()
        return stop

    def _remember_failed(self, text: str, attachments: list[str]) -> None:
        self._last_failed_text = text
        self._last_failed_attachments = list(attachments or [])

    def _agent_failed(self, error: acp.AcpError, text: str, attachments: list[str]) -> None:
        """The agent stopped partway, lost the internet, hit a limit or failed: a plain card
        with a way on, and the goal waits for the person."""
        partial = self._translator is not None and (self._translator.files_touched or self._prompted)
        self._finish_translator("error")
        self._remember_failed(text, attachments)
        detail = getattr(error, "detail", "") or ""
        agent = self._agent
        tail = agent.stderr_tail(8) if agent is not None and hasattr(agent, "stderr_tail") else ""
        log.warning("agent turn failed: %s %s", error, detail)
        crashed = isinstance(error, acp.AcpClosed) or not getattr(agent, "alive", False)
        reason = getattr(error, "reason", "") or ""
        if reason not in ("usage-limit", "offline") and acp.looks_offline(str(error), detail, tail):
            reason = "offline"
        elif not reason and crashed:
            reason = "crashed"
        can_resume = self._can_reopen()
        if crashed:
            self._shutdown_agent(keep_session=True)
        name = self.connection.label or "The agent"
        data: dict[str, Any] = {"error": True, "reason": reason, "detail": (detail or tail)[-2000:]}
        if reason == "offline":
            words = (f"Playground can't reach the internet, so {name} can't talk to its model. "
                     "Check your connection, then Try again.")
            data.update(action="retry", retry_label="Try again")
            state, note = "offline", "offline"
        elif reason == "usage-limit":
            words = str(error)
            url = (getattr(error, "data", None) or {}).get("manage_usage") if isinstance(getattr(error, "data", None), dict) else None
            data.update(action="manage-usage", url=url or "", secondary="retry", retry_label="Try again")
            state, note = "error", "limit reached"
        elif reason in ("coming-soon", "not-eligible"):
            words = str(error)
            state, note = "error", "can't start"
        elif reason == "crashed":
            words = f"{name} stopped partway through"
            words += (". Its conversation is saved, so Try again picks up where it left off."
                      if can_resume and partial else ". Try again to start it once more.")
            data.update(action="retry", retry_label="Try again", resumable=bool(can_resume))
            state, note = "error", "agent stopped"
        else:
            words = str(error)
            data.update(action="retry", retry_label="Try again")
            state, note = "error", "agent stopped"
        self._resolve_failure()
        self._failure(words, **data)
        self._set_goal_status(GoalStatus.NEEDS_YOU, note=note)
        self._state(state, message=words, error=words, reason=reason, can_retry=True)

    def _can_reopen(self) -> bool:
        agent = self._agent
        info = getattr(agent, "info", None)
        if info is None or not (getattr(info, "can_resume", False) or getattr(info, "can_load", False)):
            return False
        saved = self._load_session()
        return bool(saved and saved.get("session_id"))

    def _finish_translator(self, reason: str) -> None:
        if self._translator is not None:
            try:
                self._translator.finish(reason)
            except Exception:  # noqa: BLE001
                log.exception("finishing cards failed")

    def _after_turn(self, stop_reason: str) -> None:
        pending = [p for p in self.store.list_pending(kind="decision") if getattr(p, "goal", "") == self.goal_id]
        if stop_reason == "cancelled":
            self._person_stopped()
            return
        if stop_reason == "max_tokens":
            self._system("The agent stopped because its answer got too long. Say \"keep going\" to carry on.")
        elif stop_reason == "max_turn_requests":
            self._system("The agent reached its limit of steps for one turn. Say \"keep going\" to carry on.")
        elif stop_reason == "refusal":
            self._system("The agent declined to carry on with that.")
        if pending:
            self._set_goal_status(GoalStatus.NEEDS_YOU, note="one decision" if len(pending) == 1 else f"{len(pending)} decisions")
            self._state("waiting")
        else:
            self._set_goal_status(GoalStatus.READY, only_from=(GoalStatus.WORKING,))
            self._state("idle")

    # ── the agent process ──

    def _new_translator(self) -> CardTranslator:
        def emit(entry: ThreadEntry) -> None:
            self.store.append_thread(self.goal_id, entry)

        def live(**state: Any) -> None:
            self._state(state.pop("state", "working"), **state)

        def find_report(title: str, own: set) -> str | None:
            """The agent's own step card (written by the MCP server) with this title."""
            wanted = " ".join(title.split())
            try:
                entries = self.store.read_thread(self.goal_id, limit=80)
            except (StoreError, OSError, ValueError):
                return None
            latest: dict[str, ThreadEntry] = {}
            for entry in entries:
                if entry.id:
                    latest[entry.id] = entry
            for entry in reversed(list(latest.values())):
                if entry.kind != "step" or entry.id in own or entry.data.get("folded_into"):
                    continue
                if " ".join(str(entry.data.get("title") or "").split()) == wanted:
                    return entry.id
            return None

        return CardTranslator(self.real_root, emit, by=self.connection.id, live=live,
                              show_money=self.connection.spends_money, cost_start=self._session_cost,
                              find_report=find_report)

    def _handlers(self) -> acp.ProjectHandlers:
        runner = self

        class _Handlers(acp.ProjectHandlers):
            def on_update(self, session_id: str, update: dict) -> None:
                translator = runner._translator
                if translator is not None and session_id in ("", runner._session_id):
                    translator.handle(update)

            def request_permission(self, session_id: str, tool_call: dict, options: list[dict]) -> dict:
                return runner._ask_permission(session_id, tool_call, options)

            def permissions_cancelled(self, session_id: str) -> None:
                runner._cancel_waits()

        return _Handlers(self.root, policy=self.sandbox_policy)

    def _ensure_agent(self) -> None:
        agent = self._agent
        if agent is not None and getattr(agent, "alive", False) and self._session_id:
            return
        if agent is not None:
            self._shutdown_agent(keep_session=True)
        log_path = os.path.join(self.root, ".playground", "local", "logs", f"agent-{self.connection.id}.log")
        agent = self.agent_factory(self.connection, self.root, self._handlers(), goal_id=self.goal_id, bus=self.bus,
                                   log_path=log_path, registry=self.registry)
        self._agent = agent
        confinement = getattr(agent, "confinement", None)
        self.sandbox_backend = confinement.backend if confinement is not None and confinement.confined else ""
        agent.start()
        servers = mcp_servers_for(self.root, self.goal_id, self.connection.id) \
            if self.connection.is_acp and getattr(agent, "computer", None) is None else []
        saved = self._load_session()
        self._session_id = None
        if saved and saved.get("session_id"):
            # A load replays the conversation as updates; with no turn running
            # (no translator) they are dropped, since the thread already has them.
            try:
                self._session_note = agent.reopen_session(saved["session_id"], self.root, servers)
                self._session_id = saved["session_id"]
                self._needs_context = False
            except (acp.Unsupported, acp.AcpError) as error:
                log.info("couldn't reopen %s: %s", saved.get("session_id"), error)
                if self._handoff is None and self.store.paths.thread(self.goal_id).exists():
                    # The agent lost the thread; give it the brief so it isn't starting cold.
                    self._handoff = build_brief(self.store, self.goal_id, from_connection=self.connection,
                                                to_connection=self.connection)
        if self._session_id is None:
            self._session_id = agent.new_session(self.root, servers)
            self._session_note = "new"
            self._session_cost = None
            self._needs_context = True
        self._save_session()
        self._settle_options()

    def _shutdown_agent(self, keep_session: bool = True) -> None:
        """End the agent's program. The session id stays in sessions.json, so the
        next turn reopens it natively when the agent can."""
        agent, self._agent = self._agent, None
        self._cancel_waits()
        if agent is not None:
            try:
                agent.close()
            except Exception:  # noqa: BLE001
                log.exception("closing the agent failed")
        self._session_id = None

    def _cancel_turn(self) -> None:
        agent, session_id = self._agent, self._session_id
        self._cancel_waits()
        if agent is not None and session_id and self.busy:
            try:
                agent.cancel(session_id)
            except acp.AcpError:
                pass

    def _authenticate(self, method_id: str) -> None:
        try:
            if self._agent is None:
                self._ensure_agent_started_only()
            self._agent.authenticate(method_id)
        except acp.AcpError as error:
            self._system(f"Signing in didn't work: {error}", error=True)
            self._state("needs-sign-in", methods=self.sign_in_methods())
            return
        self._system("Signed in.")
        self._queue.put(("reconnect", True))

    def _ensure_agent_started_only(self) -> None:
        log_path = os.path.join(self.root, ".playground", "local", "logs", f"agent-{self.connection.id}.log")
        self._agent = self.agent_factory(self.connection, self.root, self._handlers(), goal_id=self.goal_id,
                                         bus=self.bus, log_path=log_path, registry=self.registry)
        self._agent.start()

    # ── prompts ──

    def _blocks(self, text: str, attachments: list[str]) -> list[dict]:
        parts: list[str] = []
        if self._needs_context:
            parts.append(self._goal_context())
        if self._resume_note and self._session_note in ("resumed", "loaded"):
            parts.append("(Playground: your last turn was cut off partway through. Look at what's already done "
                         "and carry on from there; don't redo it.)")
        if self._handoff is not None:
            parts.append(self._handoff.brief)
        parts.extend(self._answer_notes())
        brief = self._brief()
        if brief:
            parts.append(brief)
        if text:
            parts.append(text if not parts else "The person says:\n" + text)
        # Last, where it's read: memory is kept as the turn ends, not left to be remembered (keeping notes
        # decays under load when it's only asked for once, at the start).
        parts.append(TURN_END)
        blocks = [acp.text_block("\n\n".join(p for p in parts if p))]
        info = getattr(self._agent, "info", None)
        for path in attachments:
            try:
                real = sandbox.resolve_in_project(self.root, path)
            except PermissionError:
                continue
            if real.is_file():
                kind = mimetypes.guess_type(real.name)[0] or ""
                if kind.startswith("image/") and info is not None and info.prompt_capabilities.get("image") \
                        and real.stat().st_size < 5_000_000:
                    # A screenshot the person dropped in: the agent sees the picture, not just its name.
                    blocks.append({"type": "image", "mimeType": kind,
                                   "data": base64.b64encode(real.read_bytes()).decode("ascii")})
                    continue
                if info is not None and info.prompt_capabilities.get("embeddedContext") and real.stat().st_size < 200_000:
                    try:
                        content = sandbox.read_text_file(self.root, str(real))
                        blocks.append({"type": "resource", "resource": {"uri": real.as_uri(), "text": content}})
                        continue
                    except (ValueError, OSError):
                        pass
                blocks.append(acp.resource_link_block(str(real)))
        return blocks

    def _person_stopped(self) -> None:
        self._stopped_by_person = False
        self._system("Stopped. Send a message to carry on, or to change course.")
        self._set_goal_status(GoalStatus.PAUSED, note="you stopped it", only_from=(GoalStatus.WORKING,))
        self._state("stopped")

    def _close_stale_steps(self) -> None:
        """A step a cut-off turn left "running" is closed before a new one starts, including
        one left by the agent this conversation switched away from. A teammate's step, whose
        id starts with their member id, is theirs to close on their computer."""
        ids = {self.connection.id}
        try:
            members = self.store.read_project().members
            others = {str(m.id) for m in members if str(m.id) and str(m.id) != self.person}
            for entry in self.store.read_thread(self.goal_id):
                if entry.kind == "step" and entry.by and not any(
                        entry.by.startswith(other + "-") for other in others):
                    ids.add(entry.by)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.store.close_stale_steps(self.goal_id, ids)
        except (StoreError, OSError, ValueError):
            log.exception("closing steps a cut-off turn left open")

    def tell(self, note: str) -> None:
        """Something the agent asked for finished outside its turn (a long command in the project's box, an
        install): it's the start of the agent's next turn, now if it's idle."""
        with self._lock:
            self._told.append(note)
        if not self.busy and not self._closed:
            self._queue.put(("answered",))

    def _answer_notes(self, peek: bool = False) -> list[str]:
        """Answers to the agent's questions it hasn't had yet. One its own tool call picked up (the MCP server
        removes the question when it hands the answer over) is skipped, so nothing is said twice."""
        with self._lock:
            waiting = [(i, n) for i, n in self._answered if self.store.read_pending(i) is not None]
            told = list(self._told)
            if not peek:
                self._answered, self._told = [], []
        if not peek:
            for decision_id, _note in waiting:
                try:
                    self.store.remove_pending(decision_id)
                except (StoreError, OSError):
                    pass
        return [note for _i, note in waiting] + told

    def _brief(self) -> str:
        """What changed elsewhere in the project since this conversation's last turn (core.areas.brief_since).
        Agents are told to look; this makes sure they know."""
        from ..core.areas import brief_since
        saved = self._load_session() or {}
        try:
            text = brief_since(self.store, saved.get("briefed"), conversation=self.goal_id, actor=self.connection.id)
        except Exception as error:  # noqa: BLE001 - a brief is help, never a reason to fail a turn
            log.info("no brief this turn: %s", error)
            text = ""
        self._briefed_at = now_iso()
        return text

    def _goal_context(self) -> str:
        try:
            goal = self.store.read_goal(self.goal_id)
            title, outcome, chat = goal.title, goal.outcome, goal.is_chat
        except StoreError:
            title, outcome, chat = self.goal_id, "", False
        if chat:
            return (f"(Playground: this is the project's general conversation, id {self.goal_id}. "
                    "Follow AGENTS.md in this folder.)")
        text = (f"(Playground: you're working on the goal \"{title}\", id {self.goal_id}. Use that id with the "
                "Playground tools. Follow AGENTS.md in this folder.")
        if outcome.strip():
            text += f" The outcome wanted: {outcome.strip()[:600]}"
        return text + ")"

    # ── permissions ──

    def _ask_permission(self, session_id: str, tool_call: dict, options: list[dict]) -> dict:
        auto = self._auto_permission(tool_call, options)
        if auto is not None:
            return auto
        translator = self._translator
        if translator is not None:
            translator.flush_text()
        agent_name = self.connection.label or "The agent"
        kind = str(tool_call.get("kind") or "")
        question = _QUESTION.get(kind, "{agent} wants to do something that needs your OK").format(agent=agent_name)
        title = _clean_title(str(tool_call.get("title") or ""), self.root)
        decision_id = new_id("allow " + (title or kind or "this"), 28)
        opts = [Option(id=str(o.get("optionId")), label=str(o.get("name") or o.get("optionId")),
                       consequence=_CONSEQUENCE.get(str(o.get("kind")), "")) for o in options if o.get("optionId")]
        recommended = acp.pick_option(options, allow=True) or ""
        default = acp.pick_option(options, allow=False) or ""
        files = [r for r in (sandbox.relative_to_project(self.root, loc.get("path"))
                             for loc in tool_call.get("locations") or [] if isinstance(loc, dict)) if r]
        context = title + (f" ({', '.join(files[:5])})" if files else "")
        wait = _Wait(decision_id, session_id, options)
        with self._lock:
            self._waits[decision_id] = wait
        try:
            self.store.write_pending(PendingDecision(
                id=decision_id, question=question, options=opts, goal=self.goal_id, recommended=recommended,
                default=default, context=context, asked_by=self.connection.id, extra={"source": "permission"}))
            self.store.append_thread(self.goal_id, ThreadEntry(
                kind="decision", role="agent", by=self.connection.id,
                data={"id": decision_id, "question": question, "options": [o.to_dict() for o in opts],
                      "recommended": recommended, "default": default, "context": context, "source": "permission"}))
        except (StoreError, OSError, ValueError):
            log.exception("couldn't record a permission question")
        self._set_goal_status(GoalStatus.NEEDS_YOU, note="one decision")
        self._state("waiting", decision_id=decision_id)
        answered = wait.event.wait(self.permission_timeout)
        with self._lock:
            self._waits.pop(decision_id, None)
        try:
            self.store.remove_pending(decision_id)
        except OSError:
            pass
        self._set_goal_status(GoalStatus.WORKING, only_from=(GoalStatus.NEEDS_YOU,))
        self._state("working")
        if not answered or wait.choice is None:
            return acp.cancelled_outcome()
        return acp.selected_outcome(wait.choice)

    def _auto_permission(self, tool_call: dict, options: list[dict]) -> dict | None:
        if self.permission_policy == "ask":
            return None
        allow = acp.pick_option(options, allow=True)
        if allow is None:
            return None
        if self.permission_policy == "allow-all":
            return acp.selected_outcome(allow)
        kind = str(tool_call.get("kind") or "")
        if kind == "execute" and not self.connection.is_acp:
            # Ari's commands run in Playground's own sandbox; it marks them so.
            meta = tool_call.get("_meta") if isinstance(tool_call.get("_meta"), dict) else {}
            if (meta.get("playground") or {}).get("sandboxed"):
                return acp.selected_outcome(allow)
        if kind not in AUTO_ALLOW_KINDS:
            return None
        locations = [loc.get("path") for loc in tool_call.get("locations") or [] if isinstance(loc, dict)]
        raw = tool_call.get("rawInput") if isinstance(tool_call.get("rawInput"), dict) else {}
        for key in ("path", "file_path", "filePath", "notebook_path"):
            if isinstance(raw.get(key), str):
                locations.append(raw[key])
        if kind in ("edit", "move") and not locations:
            return None  # can't tell where it writes, so ask
        for path in locations:
            try:
                sandbox.resolve_in_project(self.root, path, write=kind in ("edit", "move"))
            except PermissionError:
                return None
        return acp.selected_outcome(allow)

    def _resolve_wait(self, decision_id: str, option: str | None) -> None:
        with self._lock:
            wait = self._waits.get(decision_id)
        if wait is not None:
            wait.choice = option or None
            wait.event.set()

    def _cancel_waits(self) -> None:
        with self._lock:
            waits = list(self._waits.values())
        for wait in waits:
            wait.choice = None
            wait.event.set()

    def _on_answered(self, project: str = "", id: str = "", answer: dict | None = None, **_: Any) -> None:
        """An answer written by someone else (another window, the UI going through the store)."""
        if project and os.path.realpath(project) != self.real_root:
            return
        with self._lock:
            wait = self._waits.get(id)
        if wait is not None and answer:
            self._resolve_wait(id, str(answer.get("option") or "") or None)

    # ── goal, sessions, usage ──

    def _goal_owner(self) -> str:
        try:
            return self.store.read_goal(self.goal_id).owner
        except StoreError:
            return ""

    def _set_goal_status(self, status: GoalStatus, *, only_from: tuple | None = None, note: str = "") -> None:
        def change(goal) -> None:
            if goal.is_chat:
                return
            if only_from is not None and goal.status not in only_from:
                return
            if goal.status in (GoalStatus.LIVE, GoalStatus.DONE) and status == GoalStatus.READY:
                return
            goal.status = status
            goal.status_note = note
        try:
            self.store.update_goal(self.goal_id, change)
        except (StoreError, OSError):
            pass

    def _sessions_path(self) -> Path:
        return Path(self.root, ".playground", "local", "sessions.json")

    def _load_session(self) -> dict | None:
        """The goal's last native session, if it belongs to this program and account."""
        data = read_json(self._sessions_path()) or {}
        entry = data.get(self.goal_id)
        if isinstance(entry, dict) and entry.get("account") == list(self.connection.account_key()):
            return entry
        return None

    def _save_session(self) -> None:
        try:
            with self.store.locked():
                data = read_json(self._sessions_path()) or {}
                before = data.get(self.goal_id) if isinstance(data.get(self.goal_id), dict) else {}
                data[self.goal_id] = {"connection": self.connection.id, "provider": self.connection.provider,
                                      "account": list(self.connection.account_key()),
                                      "session_id": self._session_id or "", "updated": now_iso(),
                                      # What the agent offered last time: some don't repeat it on a reopen.
                                      "options": self.options() or before.get("options") or [],
                                      "briefed": getattr(self, "_briefed_at", "") or before.get("briefed") or ""}
                atomic_write_json(self._sessions_path(), data)
        except (OSError, StoreError):
            log.warning("couldn't save the session map")

    def _check_caps(self) -> None:
        if self.usage is None:
            return
        try:
            if self.team_task:
                ok, why = self.usage.can_run_team_task(self.connection.id)
            else:
                ok, why = self.usage.within_caps(self.connection.id)
        except Exception:  # noqa: BLE001 - usage problems never block work
            return
        if not ok:
            raise ConnectionRuleError(why + " Choose another connection, or wait for it to reset.")

    def _record_usage(self, stop_reason: str) -> None:
        translator = self._translator
        if self.usage is None or not self._prompted or stop_reason == "error":
            return  # a turn that never got an answer (sign-in, crash) isn't a run
        try:
            project_id = ""
            owner_name = ""
            try:
                project = self.store.read_project()
                project_id = project.id
                member = project.member(self.connection.owner)
                owner_name = member.first_name if member else ""
            except StoreError:
                pass
            cost = translator.turn_cost if translator is not None and self.connection.spends_money else None
            self.usage.record_run(self.connection.id, runs_on=self.connection.run_label(owner_name or None),
                                  cost=cost, duration_s=round(time.monotonic() - self._turn_started, 1),
                                  goal_id=self.goal_id, project_id=project_id, team_task=self.team_task,
                                  model=self.connection.model, bus=self.bus)
        except Exception:  # noqa: BLE001
            log.exception("couldn't record usage")


def _wait_clear(flag: threading.Event, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while flag.is_set():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _option_label(options: list[dict], option_id: str) -> str:
    for opt in options:
        if opt.get("optionId") == option_id:
            return str(opt.get("name") or option_id)
    return option_id


def _project_root(project: Any) -> str:
    if isinstance(project, (str, os.PathLike)):
        return os.fspath(project)
    for attr in ("path", "root"):
        value = getattr(project, attr, None)
        if value:
            return os.fspath(value)
    raise ValueError("Give the runner a project folder.")
