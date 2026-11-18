# SPDX-License-Identifier: Apache-2.0
"""The conversation: one goal's thread, where the agent's work arrives as cards.

The thread is read from `.playground/threads/<goal>.jsonl` (the project folder is the
source of truth) and grows as entries are appended: new cards settle in with a short
rise, and the view follows them down unless you've scrolled up to read.

With no project yet, the same island holds the welcome: "What do you want to make?"
"""
from __future__ import annotations

import json
from pathlib import Path


import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, GLib, GObject, Gtk  # noqa: E402

from . import widgets as w  # noqa: E402
from .cards import CardContext, build_card  # noqa: E402
from .cards import message as message_card  # noqa: E402

THREAD_WIDTH = 780
EXAMPLES = (
    ("leaf", "A tool to track my plant watering"),
    ("gamepad-2", "A game my little brother can play on his phone"),
    ("calendar", "A sign-up page for my pottery classes"),
)


def _key(entry: dict, index: int) -> str:
    return str(entry.get("id") or f"#{index}")


def collapse(entries: list[dict]) -> list[tuple[str, dict]]:
    """One card per id, where it first appeared, as it last was: a step or plan is written again
    with the same id as it moves on, and readers keep the last entry for an id (agents/runner.py)."""
    order: list[str] = []
    latest: dict[str, dict] = {}
    for index, entry in enumerate(entries):
        key = _key(entry, index)
        if key not in latest:
            order.append(key)
        latest[key] = entry
    return [(key, latest[key]) for key in order]


class Thread(Gtk.Box):
    """The column of cards for one goal, with its heading."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.add_css_class("pg-thread")
        self.header = w.box("v", 6, "pg-thread-head")
        self.append(self.header)
        self.items = w.box("v", 14, "pg-thread-items")
        self.items.set_accessible_role(Gtk.AccessibleRole.LOG)
        self.append(self.items)
        self.reset()

    def reset(self) -> None:
        w.clear(self.header)
        w.clear(self.items)
        # Another conversation: its own "is working" line, decided afresh.
        if getattr(self, "_thinking_timer", 0):
            GLib.source_remove(self._thinking_timer)
        self._thinking_timer, self._thinking_shown, self._thinking_wanted = 0, None, None
        self._thinking_requested = None
        self.thinking = None
        self.widgets: dict[str, Gtk.Widget] = {}
        self.entries: dict[str, dict] = {}
        self.seen: dict[str, str] = {}
        self.last_day: str | None = None
        self.thinking: Gtk.Widget | None = None
        self.local: list[Gtk.Widget] = []

    @property
    def count(self) -> int:
        return len(self.widgets)

    def set_heading(self, title: str, meta: Gtk.Widget | None, more: Gtk.Widget | None) -> None:
        w.clear(self.header)
        row = w.box("h", 12)
        column = w.box("v", 6, hexpand=True)
        heading = w.label(title, "pg-thread-title", wrap=True)
        heading.set_accessible_role(Gtk.AccessibleRole.HEADING)
        column.append(heading)
        self.meta_slot = w.box("v", 0)
        if meta is not None:
            self.meta_slot.append(meta)
        column.append(self.meta_slot)
        row.append(column)
        if more is not None:
            more.set_valign(Gtk.Align.START)
            row.append(more)
        self.header.append(row)

    def replace_meta(self, meta: Gtk.Widget) -> None:
        if getattr(self, "meta_slot", None) is not None:
            w.clear(self.meta_slot)
            self.meta_slot.append(meta)

    def show(self, entries: list[dict], ctx: CardContext, *, animate: bool) -> bool:
        """Bring the column up to date. Returns False when it needs a full redraw (cards went away)."""
        collapsed = collapse(entries)
        keys = [key for key, _entry in collapsed]
        if any(key not in keys for key in self.widgets):
            return False
        self.set_thinking(None)
        for key, entry in collapsed:
            self.entries[key] = entry
            fingerprint = json.dumps(entry, sort_keys=True, default=str)
            if key in self.widgets:
                if self.seen.get(key) != fingerprint:
                    old = self.widgets[key]
                    widget = build_card(entry, ctx)
                    self.items.insert_child_after(widget, old)
                    self.items.remove(old)
                    self.widgets[key] = widget
                    self.seen[key] = fingerprint
                continue
            moment = w.parse_ts(entry.get("ts"))
            if moment is not None:
                day = w.day_words(moment)
                if day != self.last_day:
                    self.last_day = day
                    self.items.append(message_card.day(day))
            widget = build_card(entry, ctx)
            for local in self.local:
                # Words that weren't saved stay at the foot, under what's new.
                if local.get_parent() is self.items:
                    self.items.remove(local)
            self.items.append(widget)
            for local in self.local:
                self.items.append(local)
            self.widgets[key] = widget
            self.seen[key] = fingerprint
            if animate:
                w.arrive(widget)
        return True

    THINKING_SETTLE_MS = 900

    def set_thinking(self, who: str | None, what: str = "thinking", *, now: bool = False) -> None:
        """The "is working" line under the last card. It changes only once the agent's state has held for a
        moment, so steps starting and ending quickly don't make it blink; the same words are never redrawn."""
        wanted = (who, what) if who else None
        self._thinking_wanted = wanted
        pending = getattr(self, "_thinking_timer", 0)
        if wanted == getattr(self, "_thinking_requested", None):
            if pending:
                return                      # the same state again: don't push the settle timer back
            if wanted == getattr(self, "_thinking_shown", None):
                self._move_thinking_to_bottom()
                return
        self._thinking_requested = wanted
        if pending:
            GLib.source_remove(pending)
            self._thinking_timer = 0
        if now:
            self._apply_thinking()
            return
        self._thinking_timer = GLib.timeout_add(self.THINKING_SETTLE_MS, self._apply_thinking)

    def _move_thinking_to_bottom(self) -> None:
        if self.thinking is not None and self.thinking.get_parent() is self.items \
                and self.items.get_last_child() is not self.thinking:
            self.items.remove(self.thinking)
            self.items.append(self.thinking)

    def _apply_thinking(self) -> bool:
        self._thinking_timer = 0
        wanted = getattr(self, "_thinking_wanted", None)
        shown = getattr(self, "_thinking_shown", None)
        if wanted == shown and (self.thinking is None) == (wanted is None):
            if self.thinking is not None and self.thinking.get_parent() is self.items \
                    and self.items.get_last_child() is not self.thinking:
                self.items.remove(self.thinking)          # stays under the newest card, without arriving again
                self.items.append(self.thinking)
            return False
        if self.thinking is not None:
            if self.thinking.get_parent() is self.items:
                self.items.remove(self.thinking)
            self.thinking = None
        self._thinking_shown = wanted
        if wanted is not None:
            self.thinking = message_card.thinking(*wanted)
            self.items.append(self.thinking)
            if shown is None:
                w.arrive(self.thinking)
        return False


class Welcome(Gtk.Box):
    """Start from nothing, well (v73 `.pgnew`): one big question and three examples."""

    __gsignals__ = {"start": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
                    "join": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, icon_path: Path | None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("pg-welcome")
        if icon_path is not None and icon_path.is_file():
            try:
                texture = Gdk.Texture.new_from_filename(str(icon_path))
                mark = Gtk.Image.new_from_paintable(texture)
                mark.set_pixel_size(60)
                mark.set_halign(Gtk.Align.START)
                mark.add_css_class("pg-welcome-mark")
                mark.set_accessible_role(Gtk.AccessibleRole.PRESENTATION)
                self.append(mark)
            except GLib.Error:
                pass
        heading = w.label("What do you want to make?", "pg-welcome-title", wrap=True)
        heading.set_accessible_role(Gtk.AccessibleRole.HEADING)
        self.append(heading)
        self.append(w.label("Say it the way you’d tell a friend. Ari asks a couple of questions, makes a real "
                            "folder for it, and gets a first version on screen.", "pg-welcome-text", wrap=True))
        well = w.box("v", 10, "pg-big")
        self.view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False, top_margin=10,
                                 bottom_margin=4, left_margin=12, right_margin=12)
        self.view.add_css_class("pg-big-text")
        self.view.set_size_request(-1, 92)
        self.view.update_property([Gtk.AccessibleProperty.LABEL], ["What do you want to make?"])
        placeholder = w.label("A little app that…", "pg-big-placeholder", can_target=False,
                              valign=Gtk.Align.START, halign=Gtk.Align.START)
        placeholder.set_margin_top(10)
        placeholder.set_margin_start(14)
        stack = Gtk.Overlay()
        stack.set_child(self.view)
        stack.add_overlay(placeholder)
        buffer = self.view.get_buffer()
        buffer.connect("changed", lambda b: placeholder.set_visible(b.get_char_count() == 0))
        well.append(stack)
        line = w.box("h", 6)
        line.append(w.label("Press Return to start", "pg-sub", hexpand=True))
        self.start_button = w.button("Start", icon_name_="sparkles", style="key", on_click=self._start)
        line.append(self.start_button)
        well.append(line)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.view.add_controller(keys)
        self.append(well)
        examples = w.box("v", 6, "pg-examples")
        examples.append(w.label("Or start from one of these", "pg-examples-title"))
        for glyph, text in EXAMPLES:
            row = Gtk.Button()
            row.add_css_class("pg-example")
            line = w.box("h", 12)
            line.append(w.icon(glyph, 18))
            line.append(w.label(text, hexpand=True, wrap=True))
            arrow = w.icon("arrow-right", 16)
            arrow.add_css_class("pg-example-arrow")
            line.append(arrow)
            row.set_child(line)
            row.connect("clicked", lambda _b, t=text: self.emit("start", t))
            examples.append(row)
        self.append(examples)
        # A teammate's project instead: People is the join page while no project is open.
        join = Gtk.Button(halign=Gtk.Align.START)
        join.add_css_class("pg-join-link")
        line = w.box("h", 8)
        line.append(w.icon("users", 15))
        line.append(w.label("Someone invited you? Join their project"))
        join.set_child(line)
        join.set_tooltip_text("Join a project a teammate shared with you")
        join.connect("clicked", lambda _b: self.emit("join"))
        self.append(join)

    def _text(self) -> str:
        buffer = self.view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False).strip()

    def _start(self) -> None:
        self.emit("start", self._text())

    def _key(self, _controller, keyval, _code, state) -> bool:
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not state & Gdk.ModifierType.SHIFT_MASK:
            self._start()
            return True
        return False

    def focus(self) -> None:
        self.view.grab_focus()


class Conversation(Gtk.Box):
    """The centre island's content: the thread (or the welcome) with the composer floating over it."""

    def __init__(self, icon_path: Path | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.add_css_class("pg-conversation")
        self.overlay = Gtk.Overlay(hexpand=True, vexpand=True)
        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.scroller.add_css_class("pg-thread-scroll")
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                               transition_duration=0 if w.reduced_motion() else 160, vhomogeneous=False)
        self.thread = Thread()
        self.thread_clamp = Adw.Clamp(maximum_size=THREAD_WIDTH, tightening_threshold=THREAD_WIDTH)
        column = w.box("v", 0)
        column.append(self.thread)
        self.safe = Gtk.Box(height_request=132)
        column.append(self.safe)
        self.thread_clamp.set_child(column)
        self.stack.add_named(self.thread_clamp, "thread")
        self.welcome = Welcome(icon_path)
        welcome_clamp = Adw.Clamp(maximum_size=640, tightening_threshold=640)
        welcome_clamp.set_child(self.welcome)
        welcome_clamp.add_css_class("pg-welcome-clamp")
        self.stack.add_named(welcome_clamp, "welcome")
        # Starting a new project happens here, in the conversation, like v73 (sheets/newproject.InlineHost).
        self.flow_clamp = Adw.Clamp(maximum_size=THREAD_WIDTH, tightening_threshold=THREAD_WIDTH)
        self.flow_clamp.add_css_class("pg-flow")
        self.stack.add_named(self.flow_clamp, "flow")
        self.empty = w.box("v", 8, "pg-goal-empty", valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        self.stack.add_named(self.empty, "empty")
        self.scroller.set_child(self.stack)
        self.overlay.set_child(self.scroller)
        # The conversation's title and state stay pinned at the top of its column, above the cards.
        self.thread.remove(self.thread.header)
        self.pinned = w.box("v", 0, "pg-chat-pinned", visible=False)
        self.pinned.append(self.thread.header)
        self.append(self.pinned)
        self.append(self.overlay)
        self.stack.connect("notify::visible-child-name",
                           lambda st, _p: self.pinned.set_visible(st.get_visible_child_name() == "thread"))
        self.composer: Gtk.Widget | None = None
        self.ctx = CardContext()
        self.goal_id: str | None = None
        self._follow = True
        self._scroll_source = 0
        adjustment = self.scroller.get_vadjustment()
        adjustment.connect("value-changed", self._scrolled)
        adjustment.connect("notify::upper", self._grew)

    # ── the composer floats over the foot of the thread ──────────────────
    def set_composer(self, composer: Gtk.Widget) -> None:
        """The composer floats at the foot, as wide as the thread (v73: min(720, 100% − 48))."""
        self.composer = composer
        self.composer_holder = Adw.Clamp(maximum_size=768, tightening_threshold=768, valign=Gtk.Align.END)
        self.composer_holder.add_css_class("pg-composer-holder")
        self.composer_holder.set_child(composer)
        self.overlay.add_overlay(self.composer_holder)

    def _show_composer(self, shown: bool) -> None:
        if self.composer is not None:
            self.composer_holder.set_visible(shown)
        self.safe.set_size_request(-1, 132 if shown else 40)

    # ── what's shown ─────────────────────────────────────────────────────
    def _release_focus(self) -> None:
        """The thread's cards are about to be thrown away: focus mustn't be left on one of them (GTK would
        go on asking a destroyed widget about its ancestors)."""
        root = self.get_root()
        focus = root.get_focus() if root is not None and hasattr(root, "get_focus") else None
        if focus is not None and (focus is self.thread or focus.is_ancestor(self.thread) or
                                  focus.is_ancestor(self.flow_clamp)):
            root.set_focus(None)

    def show_welcome(self) -> None:
        self.goal_id = None
        self._release_focus()
        self.thread.reset()
        self.stack.set_visible_child_name("welcome")
        self._show_composer(False)

    def show_flow(self, widget: Gtk.Widget) -> None:
        """A step-by-step flow in place of the thread (New project): no composer, the same width."""
        self.goal_id = None
        self._release_focus()
        self.thread.reset()
        self.flow_clamp.set_child(widget)
        self.stack.set_visible_child_name("flow")
        self._show_composer(False)

    @property
    def showing_flow(self) -> bool:
        return self.stack.get_visible_child_name() == "flow"

    def show_goal(self, goal_id: str, title: str, meta: Gtk.Widget | None, more: Gtk.Widget | None,
                  entries: list[dict], ctx: CardContext, *, empty: tuple[str, str] | None = None) -> None:
        self.goal_id = goal_id
        self.ctx = ctx
        self._release_focus()
        self.thread.reset()
        self.thread.set_heading(title, meta, more)
        self.pinned.set_visible(True)
        self.thread.show(entries, ctx, animate=False)
        self._show_composer(True)
        self.stack.set_visible_child_name("thread")
        if not entries and empty is not None:
            hint = w.box("v", 6, "pg-thread-hint", halign=Gtk.Align.CENTER)
            hint.append(w.label(empty[0], "pg-thread-hint-title", xalign=0.5, wrap=True))
            hint.append(w.label(empty[1], "pg-sub", xalign=0.5, wrap=True, justify=Gtk.Justification.CENTER))
            self.thread.items.append(hint)
        self._follow = True
        self._to_bottom()

    def sync(self, entries: list[dict]) -> None:
        """Bring the thread up to date with the file: new cards settle in, changed ones update in place."""
        if self.goal_id is None:
            return
        hint = self.thread.items.get_first_child()
        if entries and hint is not None and hint.has_css_class("pg-thread-hint"):
            self.thread.items.remove(hint)
        if not self.thread.show(entries, self.ctx, animate=True):
            header = list(self._heading_widgets())
            meta_slot = getattr(self.thread, "meta_slot", None)
            self._release_focus()
            self.thread.reset()
            for widget in header:
                self.thread.header.append(widget)
            self.thread.meta_slot = meta_slot
            self.thread.show(entries, self.ctx, animate=False)
        if self._follow:
            self._to_bottom()

    def replace_meta(self, meta: Gtk.Widget) -> None:
        self.thread.replace_meta(meta)

    def _heading_widgets(self):
        child = self.thread.header.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.thread.header.remove(child)
            yield child
            child = following

    def set_thinking(self, who: str | None, what: str = "thinking", *, now: bool = False) -> None:
        self.thread.set_thinking(who, what, now=now)
        if who and self._follow:
            self._to_bottom()

    def append_local(self, widget: Gtk.Widget) -> None:
        """A line that isn't in the file (a gentle 'not connected yet'): shown once, not saved."""
        self.thread.items.append(widget)
        self.thread.local.append(widget)
        w.arrive(widget)
        self._to_bottom()

    def show_open_question(self) -> None:
        """Scroll to the latest question card (the one "Needs you" is about)."""
        keys = [k for k, e in self.thread.entries.items() if e.get("kind") == "decision" and k in self.thread.widgets]
        if not keys:
            return
        card = self.thread.widgets[keys[-1]]
        from gi.repository import Graphene
        found, point = card.compute_point(self.stack, Graphene.Point())
        if not found:
            return
        self._follow = False
        self.scroller.get_vadjustment().set_value(max(0.0, point.y - 24))
        card.grab_focus()

    # ── following the thread down ────────────────────────────────────────
    def _scrolled(self, adjustment: Gtk.Adjustment) -> None:
        bottom = adjustment.get_upper() - adjustment.get_page_size()
        self._follow = adjustment.get_value() >= bottom - 48

    def _grew(self, adjustment: Gtk.Adjustment, _pspec) -> None:
        if self._follow:
            self._to_bottom()

    def _to_bottom(self) -> None:
        if self._scroll_source:
            return

        def go() -> bool:
            self._scroll_source = 0
            adjustment = self.scroller.get_vadjustment()
            adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
            return GLib.SOURCE_REMOVE
        self._scroll_source = GLib.idle_add(go)


def goal_meta(status: str, started: str, agent: dict, where: str, *, chat: bool = False,
              chat_text: str = "", on_state=None) -> Gtk.Widget:
    """Under the goal's title: its state, who started it and when, and who works on it where (v73 `.pgmeta`)."""
    line = Adw.WrapBox(child_spacing=8, line_spacing=4)
    line.add_css_class("pg-thread-meta")
    if chat:
        line.append(w.label(chat_text or "Ask anything about the project.", wrap=True))
        return line
    # One quiet line: how it stands, and who started it when. The agent (and its model) is at the composer.
    kind = w.state_kind(status)
    state = w.label(w.state_label(status) if kind != "draft" or status == "waiting" else "New", "pg-meta-state")
    state.add_css_class(kind)
    if status == "needs-you" and on_state is not None:
        # "Needs you" always leads to what needs you: the question, however far up the conversation it is.
        button = Gtk.Button(child=state, tooltip_text="Show the question")
        button.add_css_class("flat")
        button.add_css_class("pg-meta-state-button")
        button.connect("clicked", lambda _b: on_state())
        line.append(button)
        line.append(w.label("·", "pg-sep"))
        line.append(w.label("a question for you"))
    else:
        line.append(state)
    if started:
        line.append(w.label("·", "pg-sep"))
        line.append(w.label(started[:1].lower() + started[1:] if started.startswith("Started") else started))
    if where not in ("", "this computer") and status != "waiting" and "’s conversation" not in started:
        line.append(w.label("·", "pg-sep"))
        line.append(w.label(f"on {where}"))
    return line
