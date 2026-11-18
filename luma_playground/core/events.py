# SPDX-License-Identifier: Apache-2.0
"""The in-process event bus.

Everything that changes a project publishes a topic here: the store when it
writes, the watcher when an agent writes, the preview and the runners when
their state moves. The window subscribes and redraws.

Callbacks run on the publisher's thread. The GTK layer wraps its callbacks in
``GLib.idle_add`` so widgets are only touched on the main loop. A callback that
raises is logged and never stops the others, or the publisher.

Topics (payloads are keyword arguments; store and watcher events always carry
``project``, the project folder as a string):

    project.changed     what ("project" | "brief" | "decisions" | "knowledge" | "run")
    goal.changed        goal_id, removed (bool, only when a goal file went away)
    thread.appended     goal_id, entry (a ThreadEntry dict)
    decision.pending    decision (a pending request dict)
    decision.answered   id, answer (an Answer dict)
    request.pending     request (a pending non-decision request: screenshot, checks)
    activity.appended   entry (an ActivityEntry dict)
    files.changed       paths (project-relative strings)
    preview.state       state, url, error
    agent.state         goal_id, state
    usage.changed       (project usage) service_id, entry; (per-connection usage, from usage.py) its own payload
    places.changed
    connections.changed the connections on this computer were added, changed or removed
    stack.changed       service_id (when one service changed; absent when the whole stack was rewritten)
    issue.changed       number, removed (bool, only when the file went away)
    analytics.changed   entry
"""
from __future__ import annotations

from dataclasses import dataclass
import itertools
import logging
import threading
from typing import Any, Callable

log = logging.getLogger(__name__)

Callback = Callable[..., Any]

PROJECT_CHANGED = "project.changed"
GOAL_CHANGED = "goal.changed"
THREAD_APPENDED = "thread.appended"
DECISION_PENDING = "decision.pending"
DECISION_ANSWERED = "decision.answered"
REQUEST_PENDING = "request.pending"
ACTIVITY_APPENDED = "activity.appended"
FILES_CHANGED = "files.changed"
PREVIEW_STATE = "preview.state"
AGENT_STATE = "agent.state"
AGENT_OPTIONS = "agent.options"   # an agent's settings for a conversation: model, effort, mode
USAGE_CHANGED = "usage.changed"
PLACES_CHANGED = "places.changed"
CONNECTIONS_CHANGED = "connections.changed"   # a connection was added, changed or removed on this computer
STACK_CHANGED = "stack.changed"
ISSUE_CHANGED = "issue.changed"
TASK_CHANGED = "task.changed"
CHANNEL_POSTED = "channel.posted"
ANALYTICS_CHANGED = "analytics.changed"


@dataclass(frozen=True)
class _Subscription:
    token: int
    topic: str | None
    callback: Callback


class EventBus:
    """A small thread-safe publish/subscribe hub.

    ``subscribe(None, cb)`` receives every topic; the callback is then called
    as ``cb(topic, **payload)``. A topic subscriber is called as
    ``cb(**payload)``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscriptions: dict[int, _Subscription] = {}
        self._tokens = itertools.count(1)

    def subscribe(self, topic: str | None, callback: Callback) -> int:
        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._lock:
            token = next(self._tokens)
            self._subscriptions[token] = _Subscription(token, topic, callback)
        return token

    def unsubscribe(self, token: int) -> None:
        with self._lock:
            self._subscriptions.pop(token, None)

    def publish(self, topic: str, **payload: Any) -> None:
        # Snapshot under the lock, call outside it: a callback may subscribe,
        # unsubscribe or publish without deadlocking.
        with self._lock:
            targets = [s for s in self._subscriptions.values() if s.topic is None or s.topic == topic]
        for subscription in targets:
            try:
                if subscription.topic is None:
                    subscription.callback(topic, **payload)
                else:
                    subscription.callback(**payload)
            except Exception:  # noqa: BLE001 - one bad listener must not break the rest
                log.exception("event listener for %s failed", topic)

    def clear(self) -> None:
        """Drop every subscription (tests, and app shutdown)."""
        with self._lock:
            self._subscriptions.clear()


bus = EventBus()
