#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""A scripted ``pi --mode rpc`` subprocess for tests. Stdlib only, no network.

Speaks the real LF-framed JSONL protocol: commands in on stdin, a ``response``
per command plus a configurable stream of session events on stdout. Commands
are recorded to ``$FAKE_PI_STATE/calls.jsonl`` and each emitted event type gets
a marker file under ``$FAKE_PI_STATE/events/`` so tests can wait for a precise
point in the script instead of sleeping.

Environment:
  FAKE_PI_STATE   folder for the call log, markers and argv record
  FAKE_PI_SCRIPT  path to a JSON file: {"events": [...]} (defaults to a turn)
"""
import json
import os
import sys
import threading
import time

STATE = os.environ.get("FAKE_PI_STATE", "")
SCRIPT = os.environ.get("FAKE_PI_SCRIPT", "")
_write_lock = threading.Lock()
_trigger = threading.Event()
_abort_seen = threading.Event()

DEFAULT_EVENTS = [
    {"type": "message_start", "message": {"role": "assistant", "content": []}},
    {"type": "message_update", "usage": {"input": 10, "output": 2, "totalTokens": 12,
                                         "cost": {"total": 0.001}},
     "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "Hello "}},
    {"type": "message_update", "usage": {"input": 10, "output": 3, "totalTokens": 13,
                                         "cost": {"total": 0.0015}},
     "assistantMessageEvent": {"type": "thinking_delta", "contentIndex": 1, "delta": "thinking"}},
    {"type": "message_update", "usage": {"input": 10, "output": 4, "totalTokens": 14,
                                         "cost": {"total": 0.002}},
     "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "world"}},
    {"type": "message_end", "message": {"role": "assistant", "content": []}},
    {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash",
     "args": {"command": "echo hi"}},
    {"__sleep__": 0.05},
    {"type": "extension_ui_request", "id": "u1", "method": "confirm", "title": "Allow?",
     "message": "A user extension asked something."},
    {"__sleep__": 0.25},
    {"type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash",
     "result": {"content": [{"type": "text", "text": "hi"}], "details": {}}, "isError": False},
    {"type": "agent_end", "messages": [], "willRetry": False},
    {"type": "agent_settled", "aborted": False},
]
CANCEL_EVENTS = [
    {"type": "message_update", "usage": {"totalTokens": 5},
     "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "working"}},
    {"__marker__": "waiting-abort"},
    {"__wait__": "abort"},
    {"type": "agent_settled", "aborted": True},
]
HANDLED_EVENTS = []


def record(event):
    if not STATE:
        return
    with open(os.path.join(STATE, "calls.jsonl"), "a") as handle:
        handle.write(json.dumps(event) + "\n")


def marker(name):
    if not STATE:
        return
    folder = os.path.join(STATE, "events")
    os.makedirs(folder, exist_ok=True)
    open(os.path.join(folder, name), "w").close()


def send(record_):
    data = json.dumps(record_, ensure_ascii=False) + "\n"
    with _write_lock:
        sys.stdout.write(data)
        sys.stdout.flush()


def reply(rid, data=None, success=True, error=None):
    out = {"id": rid, "type": "response", "command": "", "success": success}
    if error is not None:
        out["error"] = error
    else:
        out["data"] = data if data is not None else {}
    send(out)


def load_script(message):
    if SCRIPT:
        with open(SCRIPT) as handle:
            doc = json.load(handle)
        return doc.get("events", [])
    if "cancel-me" in message:
        return CANCEL_EVENTS
    if "handled" in message:
        return HANDLED_EVENTS
    return DEFAULT_EVENTS


def emit(events):
    for item in events:
        if not isinstance(item, dict):
            continue
        if "__sleep__" in item:
            time.sleep(float(item["__sleep__"]))
            continue
        if "__marker__" in item:
            marker(str(item["__marker__"]))
            continue
        if "__wait__" in item:
            what = item["__wait__"]
            if what == "abort":
                _abort_seen.wait(10)
            else:
                _trigger.clear()
                _trigger.wait(10)
            continue
        kind = str(item.get("type") or "")
        send(item)
        if kind:
            marker(kind)


MODELS = [
    {"id": "claude-sonnet-4", "name": "Claude Sonnet 4", "provider": "anthropic",
     "reasoning": True, "contextWindow": 200000},
    {"id": "gpt-5", "name": "GPT-5", "provider": "openai", "reasoning": True,
     "contextWindow": 128000},
]


def handle(record_):
    rid = record_.get("id")
    kind = str(record_.get("type") or "")
    record({"command": kind, "id": rid, "record": record_})
    if kind == "get_state":
        reply(rid, {"model": MODELS[0], "thinkingLevel": "medium", "isStreaming": False,
                    "sessionFile": "/tmp/fake-session.jsonl", "sessionId": "fake-session",
                    "messageCount": 0})
    elif kind == "get_available_models":
        reply(rid, {"models": MODELS})
    elif kind == "get_available_thinking_levels":
        reply(rid, {"levels": ["off", "low", "medium", "high"]})
    elif kind == "get_messages":
        reply(rid, {"messages": []})
    elif kind == "get_session_stats":
        reply(rid, {"contextUsage": {"tokens": 10, "contextWindow": 200000, "percent": 0}})
    elif kind == "set_model":
        reply(rid, {"id": record_.get("modelId"), "provider": record_.get("provider", "")})
    elif kind == "set_thinking_level":
        reply(rid, {})
    elif kind == "abort":
        _abort_seen.set()
        reply(rid, {})
    elif kind == "prompt":
        disposition = "handled" if "handled" in str(record_.get("message") or "") else "started"
        reply(rid, {"disposition": disposition})
        if "crash" in str(record_.get("message") or ""):
            sys.stdout.flush()
            os._exit(3)
        events = load_script(str(record_.get("message") or ""))
        threading.Thread(target=emit, args=(events,), daemon=True).start()
    else:
        reply(rid, {})


def main():
    if STATE:
        os.makedirs(STATE, exist_ok=True)
        with open(os.path.join(STATE, "argv.json"), "w") as argv_file:
            json.dump(sys.argv, argv_file)
    for raw in sys.stdin:
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        try:
            record_ = json.loads(line)
        except ValueError:
            continue
        if record_.get("type") == "extension_ui_response":
            record({"command": "extension_ui_response", "id": record_.get("id"),
                    "cancelled": record_.get("cancelled"), "record": record_})
        elif record_.get("type") == "__release__":
            _trigger.set()  # explicit release for a scripted __wait__
            reply(record_.get("id"), {})
        elif isinstance(record_, dict):
            handle(record_)


if __name__ == "__main__":
    main()
