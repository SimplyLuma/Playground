# SPDX-License-Identifier: Apache-2.0
"""Pi, driven through its own ``--mode rpc`` JSONL protocol, inside Playground.

Pi is a long-lived subprocess that speaks one JSON object per line on stdin and
stdout. This adapter owns that process directly - there is no ACP shim and no
third-party bridge - and emits the same ACP-shaped ``session/update`` dicts the
``GoalRunner`` already understands from ``AcpClient`` and ``AriAgent``. It has
the same duck-typed surface, so the runner treats all three alike.

A few protocol details matter and are easy to get wrong:

- Frames are split **only on LF** (strip a preceding CR). ``U+2028`` /
  ``U+2029`` are legal inside JSON strings, so a generic line reader loses data.
- Responses carry the command's string ``id`` and may arrive out of order.
  Events have no id and keep arriving after a command response.
- A successful ``prompt`` response means *accepted*, not *done*. ``agent_end``
  closes one low-level run, but retries, compaction and steering can follow;
  ``agent_settled`` is the real end of the turn.
- Extension dialogs block Pi until we answer them. We have no approval UI, so
  dialogs are answered ``cancelled`` and logged; fire-and-forget UI records are
  ignored. Pi's own tools run under the project sandbox, which is the boundary.

One Pi process serves one goal session. Its config dir lives under the
project's ``.playground/local/pi/<actor>/``: the user's Pi credentials and
settings are linked in, and this goal's Playground MCP entry is written to its
``mcp.json``. That keeps two goals in the same project from racing over a
single ``.pi/mcp.json``.
"""
from __future__ import annotations

from collections import deque
import codecs
import itertools
import json
import logging
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time
import uuid
from typing import Any, Callable

from . import acp, sandbox
from ..core.store import atomic_write_json

log = logging.getLogger(__name__)

PI_NAME = "Pi"
REQUEST_TIMEOUT = 60.0
START_TIMEOUT = 60.0
IDLE_TIMEOUT = 30 * 60.0       # no event from Pi for this long ends the turn
GRACE_S = 2.0                  # stdin EOF, then SIGTERM, then SIGKILL
MAX_RESULT = 4000
DEFAULT_CONTEXT = 200_000


class PiError(acp.AcpError):
    """Something went wrong talking to Pi. ``str()`` is for people."""


# ── The JSONL transport ─────────────────────────────────────────────────────

class _Pending:
    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Any = None
        self.error: acp.AcpError | None = None


class PiRpcConnection:
    """One ``pi --mode rpc`` subprocess: LF framing, id correlation, events.

    The reader thread is the only reader of stdout. It routes ``response``
    records to waiting ``request`` callers and pushes every other record onto
    an ordered event queue that the turn loop drains. stderr is kept as a
    short tail for error messages and never parsed as protocol data.
    """

    def __init__(self, argv: list[str], env: dict[str, str], cwd: str, *,
                 stderr_log: str | None = None) -> None:
        self.argv = [str(a) for a in argv]
        self.env = dict(env)
        self.cwd = cwd
        self.stderr_log = stderr_log
        self._proc: subprocess.Popen | None = None
        self._write_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._pending: dict[str, _Pending] = {}
        self._events: queue.Queue = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and not self._closed.is_set()

    def start(self, timeout: float = START_TIMEOUT) -> None:
        if self._proc is not None:
            raise RuntimeError("This Pi connection was already started.")
        try:
            self._proc = subprocess.Popen(
                self.argv, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        except OSError as error:
            raise PiError(f"Pi couldn't start: {error.strerror or error}.", detail=repr(error)) from error
        for target, label in ((self._read_stdout, "reader"), (self._pump_stderr, "stderr")):
            thread = threading.Thread(target=target, name=f"pi-{label}", daemon=True)
            thread.start()
            self._threads.append(thread)
        # Confirm the child is really speaking RPC before anyone prompts it.
        self.request({"type": "get_state"}, timeout=timeout)

    def send(self, record: dict) -> bool:
        """Fire-and-forget write (used for abort and extension UI responses)."""
        proc = self._proc
        if proc is None or self._closed.is_set() or proc.poll() is not None or proc.stdin is None:
            return False
        data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

    def request(self, record: dict, timeout: float = REQUEST_TIMEOUT) -> Any:
        rid = "pg-" + str(next(self._ids))
        pending = _Pending()
        with self._lock:
            self._pending[rid] = pending
        if not self.send({**record, "id": rid}):
            with self._lock:
                self._pending.pop(rid, None)
            raise acp.AcpClosed("Pi's program stopped.")
        if not pending.event.wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise acp.AcpTimeout("Pi took too long to answer.", detail=f"{record.get('type')} exceeded {timeout}s")
        if pending.error is not None:
            raise pending.error
        return pending.result

    def next_event(self, timeout: float = 1.0) -> dict | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain_events(self) -> None:
        while True:
            try:
                self._events.get_nowait()
            except queue.Empty:
                return

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:  # pragma: no cover - defensive
            return
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        buffer = ""
        try:
            while True:
                # read1 returns as soon as any bytes are available; read(4096)
                # would block until it had the full 4096 and stall short frames.
                chunk = proc.stdout.read1(4096)
                if not chunk:
                    break
                buffer += decoder.decode(chunk)
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if line.endswith("\r"):
                        line = line[:-1]
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        log.warning("ignored an unreadable Pi record: %s", line[:200])
                        continue
                    if isinstance(record, dict):
                        self._route(record)
            tail = decoder.decode(b"", final=True)
            if tail.strip():
                try:
                    self._route(json.loads(tail))
                except ValueError:
                    pass
        except Exception:  # noqa: BLE001 - a reader thread must not die silently
            log.exception("reading Pi's output failed")
        finally:
            error = acp.AcpClosed("Pi's program stopped.", detail=self.stderr_tail(8))
            self._fail_all(error)
            # Wake a prompt that is waiting for agent_settled.
            self._events.put({"type": "__exit__", "error": str(error)})

    def _route(self, record: dict) -> None:
        if record.get("type") == "response":
            rid = record.get("id")
            with self._lock:
                pending = self._pending.pop(str(rid), None) if rid is not None else None
            if pending is None:
                # A parse failure or a late reply to an abandoned request.
                if not record.get("success", False):
                    log.info("unmatched Pi response: %s", str(record.get("error") or "")[:200])
                return
            if record.get("success"):
                pending.result = record.get("data")
            else:
                pending.error = acp.AcpError(
                    str(record.get("error") or "Pi couldn't do that."),
                    detail=json.dumps(record, ensure_ascii=False)[:500])
            pending.event.set()
            return
        self._events.put(record)

    def _pump_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:  # pragma: no cover - defensive
            return
        handle = None
        if self.stderr_log:
            try:
                Path(self.stderr_log).parent.mkdir(parents=True, exist_ok=True)
                handle = open(self.stderr_log, "a", encoding="utf-8", errors="replace")
            except OSError:
                handle = None
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                self._stderr_tail.append(line)
                if handle is not None:
                    try:
                        handle.write(line + "\n")
                        handle.flush()
                    except OSError:
                        pass
        except (OSError, ValueError):
            pass
        finally:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass

    def _fail_all(self, error: acp.AcpError) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.error = error
            item.event.set()

    def stderr_tail(self, lines: int = 20) -> str:
        return "\n".join(list(self._stderr_tail)[-max(0, lines):])

    def terminate(self, grace: float = GRACE_S) -> None:
        self._closed.set()
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()      # orderly shutdown, per Pi's docs
        except OSError:
            pass
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self._signal(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self._signal(proc, signal.SIGKILL)
                try:
                    proc.wait(timeout=grace)
                except subprocess.TimeoutExpired:  # pragma: no cover - very stubborn child
                    log.warning("Pi didn't exit after SIGKILL")
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        self._fail_all(acp.AcpClosed("Pi was closed."))

    @staticmethod
    def _signal(proc: subprocess.Popen, sig: int) -> None:
        try:
            if os.name == "nt":
                proc.terminate() if sig == signal.SIGTERM else proc.kill()
            else:
                os.killpg(os.getpgid(proc.pid), sig)
        except (OSError, ProcessLookupError):
            pass


# ── Config: a private Pi home per connection ───────────────────────────────

def agent_dir(root: str, actor: str) -> Path:
    """This connection's private Pi config dir, inside the project's local state."""
    return Path(root, ".playground", "local", "pi", actor)


def write_mcp_config(root: str, actor: str, mcp_servers: list[dict] | None) -> Path:
    """Link the person's Pi credentials into a private dir and write this goal's
    Playground MCP server there, in Pi's own ``mcp.json`` shape.

    The private dir avoids two goals in one project overwriting each other's
    ``PLAYGROUND_GOAL``. Nothing in ``~/.pi/agent`` is modified.
    """
    base = agent_dir(root, actor)
    (base / "sessions").mkdir(parents=True, exist_ok=True)
    user = Path.home() / ".pi" / "agent"
    if user.is_dir():
        for entry in user.iterdir():
            if entry.name in ("mcp.json", "sessions"):
                continue  # generated below; sessions stay private to this goal
            target = base / entry.name
            if target.exists() or target.is_symlink():
                continue
            try:
                target.symlink_to(entry, target_is_directory=entry.is_dir())
            except OSError:
                pass                      # a broken link is better than a copy of a secret
    servers: dict[str, dict] = {}
    for spec in mcp_servers or []:
        if not isinstance(spec, dict) or not spec.get("command"):
            continue  # http servers are left to the person's own config
        env = {str(e.get("name")): str(e.get("value")) for e in spec.get("env") or []
               if isinstance(e, dict) and e.get("name")}
        servers[str(spec.get("name") or "playground")] = {
            "command": str(spec["command"]),
            "args": [str(a) for a in spec.get("args") or []],
            "env": env,
            "cwd": os.path.abspath(root),
            "exposure": "direct",
        }
    atomic_write_json(base / "mcp.json", {"mcpServers": servers})
    return base


# ── Translating Pi's world into ACP updates ────────────────────────────────

def _clip(text: str, limit: int = MAX_RESULT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit // 2] + f"\n\n[... {len(text) - limit} characters cut ...]\n\n" + text[-limit // 2:]


def _describe(name: str, args: dict, root: str) -> tuple[str, str, list[dict]]:
    """A plain title, an ACP tool kind, and the files this Pi tool touches."""
    n = name.lower()

    def loc(value: Any) -> list[dict]:
        if not isinstance(value, str) or not value:
            return []
        try:
            return [{"path": str(sandbox.resolve_in_project(root, value))}]
        except PermissionError:
            return []

    path = next((args.get(k) for k in ("path", "file_path", "filePath", "target_file")
                 if isinstance(args.get(k), str)), None)
    if n in ("bash", "shell", "execute", "run_command", "task", "commands"):
        return f"Run {str(args.get('command') or '')[:120]}", "execute", []
    if n in ("write", "edit", "apply_patch", "patch", "replace", "str_replace", "delete", "move", "create"):
        return f"{n.title()} {path or ''}".strip(), "edit", loc(path)
    if n in ("read", "view", "cat", "get_file"):
        return f"Read {path or ''}".strip(), "read", loc(path)
    if n in ("grep", "find", "ls", "list", "search", "glob", "view_directory"):
        query = args.get("query") or args.get("pattern") or path or ""
        return f"Search for {str(query)[:60]}".strip(), "search", []
    if n in ("fetch", "web", "http", "curl", "web_fetch"):
        return f"Fetch {str(args.get('url') or '')[:120]}".strip(), "fetch", []
    if "playground" in n:
        return name.replace("mcp__playground__", "Playground: ").replace("_", " "), "other", []
    return name, "other", []


def _result_content(result: Any) -> list[dict]:
    """Pi tool results -> the ACP content shape ``CardTranslator`` reads."""
    out: list[dict] = []
    result = result if isinstance(result, dict) else {}
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            out.append({"type": "content", "content": {"type": "text", "text": _clip(str(item.get("text") or ""))}})
        elif item.get("type") == "image" and isinstance(item.get("data"), str):
            out.append({"type": "content", "content": {"type": "image", "data": item["data"],
                                                       "mimeType": str(item.get("mimeType") or "image/png")}})
    return out


def _blocks_to_pi(blocks: list[dict]) -> tuple[str, list[dict]]:
    """ACP content blocks -> Pi's ``message`` plus optional ``images``."""
    parts: list[str] = []
    images: list[dict] = []
    for block in blocks or []:
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text") or ""))
        elif kind == "image" and isinstance(block.get("data"), str):
            images.append({"type": "image", "data": block["data"],
                           "mimeType": str(block.get("mimeType") or "image/png")})
        elif kind == "resource":
            res = block.get("resource") or {}
            if res.get("text"):
                parts.append(f"Attached file {res.get('uri', '')}:\n{res['text']}")
        elif kind == "resource_link":
            parts.append(f"(Attached: {block.get('uri', '')})")
    return "\n\n".join(p for p in parts if p), images


def _model_value(model: dict) -> str:
    """The select value for a Pi model: ``provider/id`` so set_model can decode it."""
    provider = str(model.get("provider") or "")
    mid = str(model.get("id") or "")
    return f"{provider}/{mid}" if provider and mid else (mid or provider)


model_value = _model_value


def available_models(connection, *, offline: bool = False, refresh: bool = False) -> list[dict]:
    """This module's hook for the general model chooser in ``agents.models``.

    Pi answers ``get_available_models`` over its own RPC before a session
    starts, so its list can be cached per account like any other hook's.
    """
    from . import connections, model_cache

    moment = time.time()
    rows = [] if refresh else model_cache.cached(connection, now=moment)
    if rows:
        return rows
    if offline:
        return model_cache.stale(connection)
    try:
        argv, env = connections.launch_spec(connection, install=False)
        found = connections._pi_rpc_models(argv, env, timeout=8)
    except Exception:  # noqa: BLE001 - models are a convenience, never a blocker
        found = None
    if not found:
        return model_cache.stale(connection)
    rows = [{"value": _model_value(model),
             "name": str(model.get("name") or model.get("id") or ""),
             "description": str(model.get("provider") or "")}
            for model in found if isinstance(model, dict) and (model.get("id") or model.get("provider"))]
    if rows:
        model_cache.remember(connection, rows, now=moment)
    return rows


# ── The agent ───────────────────────────────────────────────────────────────

class PiAgent:
    """Pi as an ACP-shaped agent, with the surface ``GoalRunner`` expects."""

    def __init__(self, project_root: str | os.PathLike, handlers: acp.ProjectHandlers, *,
                 argv: list[str], env: dict[str, str], goal_id: str = "", actor: str = "pi",
                 mcp_servers: list[dict] | None = None, log_path: str | None = None,
                 name: str = PI_NAME, on_close: Callable[[], None] | None = None) -> None:
        self.root = os.path.realpath(os.fspath(project_root))
        self.handlers = handlers
        self.argv = [str(a) for a in argv]
        self.env = dict(env)
        self.goal_id = goal_id
        self.actor = actor
        self.mcp_servers = list(mcp_servers or [])
        self.log_path = log_path
        self.name = name
        self._on_close = on_close
        self.sessions: dict[str, dict] = {}
        self.confinement = None
        self.info = acp.InitializeResult(
            protocol_version=acp.PROTOCOL_VERSION,
            agent_capabilities={
                "loadSession": False,
                "sessionCapabilities": {"resume": {}, "close": {}},
                "promptCapabilities": {"image": True, "embeddedContext": True},
            },
            agent_info={"name": "pi", "title": PI_NAME},
            # Filled from the providers Pi reports once the process starts.
            auth_methods=[],
        )
        self._conn: PiRpcConnection | None = None
        self._cancel = threading.Event()
        self._context_size = DEFAULT_CONTEXT
        self._config_options: list[dict] = []
        self._models: list[dict] = []
        self._current_model = ""
        self._current_thinking = ""
        self._session_id: str | None = None
        self._msg_id: str | None = None

    # ── lifecycle ──

    @property
    def alive(self) -> bool:
        return self._conn is not None and self._conn.alive

    def start(self, timeout: float = 0) -> acp.InitializeResult:
        """Prepare this connection's Pi config. The process starts with a session."""
        return self.info

    def close(self, grace: float = GRACE_S) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.terminate(grace)
        if self._on_close is not None:
            try:
                self._on_close()
            except Exception:  # noqa: BLE001
                log.exception("closing Pi's sandbox failed")

    def stderr_tail(self, lines: int = 20) -> str:
        return self._conn.stderr_tail(lines) if self._conn is not None else ""

    def _spawn(self, session_id: str) -> PiRpcConnection:
        self.close()
        self._session_id = session_id
        base = write_mcp_config(self.root, self.actor, self.mcp_servers)
        env = dict(self.env)
        env["PI_CODING_AGENT_DIR"] = str(base)
        env["PLAYGROUND_ACTOR"] = self.actor
        env["PLAYGROUND_GOAL"] = self.goal_id
        # --approve trusts project-local Pi config for this run; --session-id
        # makes the session resumable across restarts.
        argv = [*self.argv, "--approve", "--mode", "rpc", "--session-id", session_id]
        conn = PiRpcConnection(argv, env, self.root, stderr_log=self.log_path)
        conn.start()
        self._conn = conn
        try:
            stats = conn.request({"type": "get_session_stats"}, timeout=REQUEST_TIMEOUT) or {}
            usage = stats.get("contextUsage") or {} if isinstance(stats, dict) else {}
            size = usage.get("contextWindow")
            if isinstance(size, int) and size > 0:
                self._context_size = size
            state = conn.request({"type": "get_state"}, timeout=REQUEST_TIMEOUT) or {}
            if isinstance(state, dict):
                model = state.get("model") or {}
                if isinstance(model, dict) and model.get("id"):
                    self._current_model = _model_value(model)
                if isinstance(state.get("thinkingLevel"), str):
                    self._current_thinking = state["thinkingLevel"]
        except acp.AcpError:
            log.info("couldn't read Pi's initial state; carrying on with defaults")
        self._seed_options()
        self.info.auth_methods = [
            {"id": f"terminal:{p}", "name": f"Sign in to {p.replace('-', ' ').title()}",
             "type": "terminal", "description": "Opens Pi's own sign-in in a terminal.",
             "args": ["auth", "login", p]}
            for p in sorted({str(m.get("provider")) for m in self._models if m.get("provider")})
        ]
        return conn

    # ── sessions ──

    def new_session(self, cwd: str, mcp_servers: list[dict] | None = None, timeout: float = 0) -> str:
        if mcp_servers is not None:
            self.mcp_servers = list(mcp_servers)
        session_id = "pg-" + uuid.uuid4().hex[:12]
        self._spawn(session_id)
        self.sessions[session_id] = {}
        return session_id

    def reopen_session(self, session_id: str, cwd: str, mcp_servers: list[dict] | None = None) -> str:
        if mcp_servers is not None:
            self.mcp_servers = list(mcp_servers)
        self._spawn(session_id)
        self.sessions.setdefault(session_id, {})
        try:
            self._conn.request({"type": "get_messages"}, timeout=REQUEST_TIMEOUT)
        except acp.AcpError as error:
            log.info("couldn't replay Pi's earlier messages: %s", error)
        return "resumed"

    def close_session(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)
        if session_id and self._session_id == session_id:
            self.close()

    # ── a turn ──

    def cancel(self, session_id: str) -> None:
        self._cancel.set()
        if self._conn is not None:
            self._conn.send({"type": "abort"})

    def prompt(self, session_id: str, blocks: list[dict], *, idle_timeout: float = IDLE_TIMEOUT,
               timeout: float | None = None) -> str:
        conn = self._conn
        if conn is None or not conn.alive:
            raise acp.AcpClosed("Pi isn't running.")
        self._cancel.clear()
        self._msg_id = None
        conn.drain_events()
        message, images = _blocks_to_pi(blocks)
        record: dict[str, Any] = {"type": "prompt", "message": message}
        if images:
            record["images"] = images
        data = conn.request(record, timeout=timeout or REQUEST_TIMEOUT) or {}
        if isinstance(data, dict) and data.get("disposition") == "handled":
            # An extension command or input handler consumed it; no run started.
            return "end_turn"
        started = time.monotonic()
        while True:
            event = conn.next_event(timeout=1.0)
            if event is None:
                now = time.monotonic()
                if timeout is not None and now - started > timeout:
                    self.cancel(session_id)
                    raise acp.AcpTimeout("Pi took too long, so Playground stopped it.",
                                         detail="prompt timeout")
                if now - started > idle_timeout:
                    self.cancel(session_id)
                    raise acp.AcpTimeout("Pi went quiet, so Playground stopped it.",
                                         detail="prompt idle timeout")
                continue
            etype = event.get("type")
            if etype == "__exit__":
                raise acp.AcpClosed(str(event.get("error") or "Pi stopped partway through."),
                                    detail=self.stderr_tail(8))
            self._update(session_id, event)
            if etype == "agent_settled":
                if self._cancel.is_set() or event.get("aborted"):
                    return "cancelled"
                return "end_turn"

    def _update(self, session_id: str, event: dict) -> None:
        """Translate one Pi event into zero or more ACP session updates."""
        etype = event.get("type")
        if etype == "message_update":
            self._message_update(session_id, event)
        elif etype == "message_start":
            if isinstance(event.get("message"), dict) and event["message"].get("role") == "assistant":
                self._msg_id = None
        elif etype == "message_end":
            if isinstance(event.get("message"), dict) and event["message"].get("role") == "assistant":
                self._msg_id = None
        elif etype == "tool_execution_start":
            self._tool_start(session_id, event)
        elif etype == "tool_execution_end":
            self._tool_end(session_id, event)
        elif etype == "extension_ui_request":
            self._extension_ui(event)
        elif etype in ("agent_end", "compaction_start", "compaction_end",
                       "auto_retry_start", "auto_retry_end", "turn_end", "agent_start"):
            # None of these end the turn: retries, compaction and steering follow.
            log.debug("Pi event during a turn: %s", etype)

    def _emit(self, session_id: str, update: dict) -> None:
        try:
            self.handlers.on_update(session_id, update)
        except Exception:  # noqa: BLE001
            log.exception("Pi update handler failed")

    def _message_update(self, session_id: str, event: dict) -> None:
        usage = event.get("usage")
        if isinstance(usage, dict) and usage:
            update: dict[str, Any] = {"sessionUpdate": "usage_update",
                                      "used": int(usage.get("totalTokens") or 0),
                                      "size": self._context_size}
            cost = usage.get("cost")
            if isinstance(cost, dict) and isinstance(cost.get("total"), (int, float)):
                update["cost"] = {"amount": float(cost["total"]), "currency": "USD"}
            self._emit(session_id, update)
        nested = event.get("assistantMessageEvent") or {}
        if not isinstance(nested, dict):
            return
        kind = nested.get("type")
        if kind in ("text_delta", "thinking_delta"):
            if self._msg_id is None:
                self._msg_id = "pi-" + uuid.uuid4().hex[:8]
            self._emit(session_id, {"sessionUpdate": "agent_message_chunk" if kind == "text_delta"
                                    else "agent_thought_chunk", "messageId": self._msg_id,
                                    "content": {"type": "text", "text": str(nested.get("delta") or "")}})

    def _tool_start(self, session_id: str, event: dict) -> None:
        call_id = str(event.get("toolCallId") or "")
        if not call_id:
            return
        name = str(event.get("toolName") or "")
        args = event.get("args") if isinstance(event.get("args"), dict) else {}
        title, kind, locations = _describe(name, args, self.root)
        self._emit(session_id, {"sessionUpdate": "tool_call", "toolCallId": call_id, "name": name,
                                "title": title, "kind": kind, "status": "in_progress",
                                "locations": locations, "rawInput": args})

    def _tool_end(self, session_id: str, event: dict) -> None:
        call_id = str(event.get("toolCallId") or "")
        if not call_id:
            return
        self._emit(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": call_id,
                                "status": "failed" if event.get("isError") else "completed",
                                "content": _result_content(event.get("result"))})

    def _extension_ui(self, event: dict) -> None:
        method = event.get("method")
        if method in ("select", "confirm", "input", "editor"):
            log.info("auto-cancelling Pi extension dialog %s (no approval UI)", method)
            if self._conn is not None:
                self._conn.send({"type": "extension_ui_response", "id": event.get("id"),
                                 "cancelled": True})
        # notify / setStatus / setWidget / setTitle / set_editor_text need no reply.

    # ── sign-in ──

    def auth_methods(self) -> list[dict]:
        return list(self.info.auth_methods)

    def authenticate(self, method_id: str, timeout: float = 0) -> None:
        raise acp.Unsupported("Pi signs in with a terminal command, not from here.")

    def login_command(self, method_id: str) -> tuple[list[str], dict]:
        method = next((m for m in self.auth_methods() if m.get("id") == method_id), None)
        if method is None or method.get("type") != "terminal":
            raise acp.AcpError("Pi doesn't offer that way to sign in.")
        return [*self.argv, *[str(a) for a in method.get("args") or []]], dict(self.env)

    # ── the agent's settings ──

    def _seed_options(self) -> None:
        conn = self._conn
        if conn is None:
            return
        models: list[dict] = []
        levels: list[str] = []
        try:
            result = conn.request({"type": "get_available_models"}, timeout=REQUEST_TIMEOUT) or {}
            self._models = [m for m in (result.get("models") or []) if isinstance(m, dict) and m.get("id")] \
                if isinstance(result, dict) else []
        except acp.AcpError:
            pass
        try:
            result = conn.request({"type": "get_available_thinking_levels"}, timeout=REQUEST_TIMEOUT) or {}
            levels = [str(x) for x in (result.get("levels") or [])] if isinstance(result, dict) else []
        except acp.AcpError:
            pass
        options: list[dict] = []
        models = self._models
        if models:
            options.append({"id": "model", "name": "Model", "category": "model", "type": "select",
                            "currentValue": self._current_model,
                            "options": [{"value": _model_value(m),
                                         "name": str(m.get("name") or m.get("id")),
                                         "description": str(m.get("provider") or "")}
                                        for m in models]})
        if levels:
            options.append({"id": "thought_level", "name": "Thinking", "category": "other", "type": "select",
                            "currentValue": self._current_thinking,
                            "options": [{"value": level, "name": level.title(), "description": ""}
                                        for level in levels]})
        self._config_options = options

    def config_options(self, session_id: str) -> list[dict]:
        return [dict(option) for option in self._config_options]

    def set_config_option(self, session_id: str, config_id: str, value: Any) -> list[dict]:
        conn = self._conn
        if conn is None or not conn.alive:
            raise acp.AcpClosed("Pi isn't running.")
        if config_id == "model":
            pieces = str(value).split("/", 1)
            provider, model_id = (pieces[0], pieces[1]) if len(pieces) == 2 else ("", pieces[0])
            record: dict[str, Any] = {"type": "set_model", "modelId": model_id}
            if provider:
                record["provider"] = provider
            conn.request(record, timeout=REQUEST_TIMEOUT)
            self._current_model = str(value)
        elif config_id == "thought_level":
            conn.request({"type": "set_thinking_level", "level": str(value)}, timeout=REQUEST_TIMEOUT)
            self._current_thinking = str(value)
        else:
            raise acp.Unsupported("Pi doesn't offer that setting.")
        for option in self._config_options:
            if option.get("id") == config_id:
                option["currentValue"] = value
        return self.config_options(session_id)
