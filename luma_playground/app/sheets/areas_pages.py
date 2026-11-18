# SPDX-License-Identifier: Apache-2.0
"""The areas' own pages: Tasks and Knowledge, and the action bar each area gets in the window.

Mixed into the Project space (book.ProjectSpace), so they read the same Space and redraw when
the store says something changed. In the window the bar is LumaUI's ActionCenter at the foot
of the area island: one key action at rest (New task, Report a problem, Add to knowledge),
growing in place into the field or editor it needs (Tasks' quick field, Knowledge's editor),
never a dialog.

Everything a person adds here is written as "person:<name>", so agents can tell a person's
change from an agent's. Nothing here wakes an agent: they see it in the area's index the next
time they start work.
"""
from __future__ import annotations

import re
from typing import Any

from gi.repository import GLib, GObject, Gtk

from . import common as c
from .common import Row, Tag, callout, text

KIND_WORDS_FOR_FORM = {"email": "Email", "database": "Database", "storage": "Storage", "hosting": "Hosting",
                       "auth": "Sign-in", "payments": "Payments", "analytics": "Analytics", "dns": "Domain", "ai": "AI",
                       "messaging": "Texts", "other": "Something else"}

TASK_FILTERS = [("active", "Active"), ("done", "Done"), ("all", "All")]
TASK_VIEWS = [("list", "List"), ("board", "Board")]
PRIORITY_TONES = {"high": "amber", "low": None, "normal": None}
CHECK = re.compile(r"^\s*[-*] \[( |x|X)\] ", re.M)


def check_progress(body: str) -> tuple[int, int]:
    marks = CHECK.findall(body or "")
    return sum(1 for m in marks if m.lower() == "x"), len(marks)


def who_words(actor: str, members: dict[str, str]) -> str:
    """person:sam → Sam; agent:codex@sams-laptop → Codex; anything else as written."""
    actor = str(actor or "")
    if actor.startswith("person:"):
        key = actor.split(":", 1)[1]
        return members.get(key, key.capitalize())
    if actor.startswith("agent:"):
        program = actor.split(":", 1)[1].split("@", 1)[0]
        return {"codex": "Codex", "claude": "Claude", "gemini": "Gemini", "ari": "Ari", "opencode": "OpenCode",
                "goose": "goose", "pi": "Pi"}.get(program.lower(), program.capitalize())
    if actor in members:
        return members[actor]
    return actor.replace("-", " ").capitalize() if actor and actor.islower() else actor


class AreaPages:
    """Methods ProjectSpace gains: the Tasks and Knowledge sections and the bar."""

    task_filter = "active"
    task_view = "list"
    task_open: str | None = None
    knowledge_filter = "all"
    bug_filter = "open"
    note_open: str | None = None
    _bar_frozen = False             # redrawing the page under a search field that's being typed in
    knowledge_query = ""
    adding_task = False
    task_draft = ""

    # ── who is doing this, as the files say it ──

    def _members(self) -> dict[str, str]:
        """Who's who by id: the project's people, and the agents on this computer by their names."""
        project = self.data.project if self.data else None
        names = {m.id: m.name for m in getattr(project, "members", []) or []}
        connections = c.module("agents.connections")
        if connections is not None:
            try:
                names.update({conn.id: conn.label for conn in connections.ConnectionStore().list()})
            except Exception:  # noqa: BLE001
                pass
        return names

    def _me(self) -> str:
        project = getattr(self.window, "project", None)
        you = project.you() if project is not None and hasattr(project, "you") else ""
        return f"person:{you or 'me'}"

    # ── the bar at the foot of the area ──

    @property
    def center(self):
        return getattr(self.sheet, "center", None)

    def update_bar(self) -> None:
        center = self.center
        if center is None or self._bar_frozen:
            return
        kit = c.kit
        if kit is None:
            return
        if self.at == "tasks":
            if self.adding_task:
                return
            center.show_bar([kit.BarAction("plus", "New task", primary=True, on_activate=self.start_task)])
        elif self.at == "bugs":
            issue = next((i for i in (self.data.issues if self.data else [])
                          if int(i.get("number") or 0) == self.issue_open), None) if self.issue_open else None
            if issue is None:
                center.show_bar([kit.BarAction("plus", "Report a bug", primary=True, on_activate=self.start_bug)])
                return
            status = str(issue.get("status") or "open")
            items = []
            if issue.get("goal"):
                items.append(kit.BarAction("message-square", "Open the conversation", primary=True,
                                           on_activate=lambda: self.window.open_conversation(str(issue["goal"]))))
            elif status in ("open", "fixing"):
                items.append(kit.BarAction("sparkles", "Defer to agent", primary=True,
                                           on_activate=lambda: self.fix_with_agent(issue)))
            if status in ("open", "fixing"):
                items.append(kit.BarAction("check", "Mark fixed",
                                           on_activate=lambda: self.set_issue(issue, status="fixed")))
            else:
                items.append(kit.BarAction("rotate-ccw", "Open it again",
                                           on_activate=lambda: self.set_issue(issue, status="open")))
            center.show_bar(items)
        elif self.at == "knowledge":
            note = self._open_note()
            if note is not None:
                model = c.module("core.model")
                items = [kit.BarAction("pencil", "Edit", primary=True, on_activate=lambda: self.start_note(note))]
                if model.knowledge_kind(note) in ("lesson", "mistake", "note", "decision"):
                    items.append(kit.BarAction("shield-check", "Make it a rule",
                                               on_activate=lambda: self.make_rule(note)))
                items.append(kit.BarAction("trash-2", None, tooltip="Forget it", danger=True,
                                           on_activate=lambda: self.forget_note(note)))
                center.show_bar(items)
                return
            if center.searching:
                return
            search = center.search(self._knowledge_search, collapsed=True, placeholder="Search knowledge",
                                   text=self.knowledge_query, on_close=lambda: self._knowledge_search(""))
            studying = self.data is not None and self._study(self.data) is not None
            center.show_bar([kit.BarAction("plus", "Add to knowledge", primary=True, on_activate=self.start_note),
                             kit.BarAction("sparkles", "Study the project", on_activate=self.study,
                                           sensitive=not studying),
                             search])
        elif self.at == "people":
            center.show_bar([kit.BarAction("users", "Manage people", primary=True,
                                           on_activate=lambda: c.open_sheet(self.window, "people", **self.ctx.options)),
                             kit.BarAction("sparkles", "Add an agent",
                                           on_activate=lambda: c.open_sheet(self.window, "picker",
                                                                            **self.ctx.options))])
        elif self.at == "repository":
            self.repo_bar()
        elif self.at == "search":
            self.search_bar()
        elif self.at == "stack":
            center.show_bar([kit.BarAction("plus", "Add a service", primary=True, on_activate=self.start_service),
                             kit.BarAction("refresh-cw", "Check now", on_activate=self.refresh_services)])
        else:
            center.hide_bar()

    # ── Tasks ──

    def section_tasks(self, data) -> Gtk.Widget:
        if self.task_open:
            task = next((t for t in data.tasks if t.id == self.task_open), None)
            if task is not None:
                return self.task_detail(task, data)
            self.task_open = None
        open_ = [t for t in data.tasks if t.is_open]
        done = [t for t in data.tasks if not t.is_open]
        lede = f"{len(open_)} to do or underway · {len(done)} done"
        filters = c.segmented(TASK_FILTERS, self.task_filter, label="Show",
                              on_change=lambda key: self._set_task("task_filter", key))
        views = c.segmented(TASK_VIEWS, self.task_view, label="View",
                            on_change=lambda key: self._set_task("task_view", key))
        box = c.vbox(self.heading("Tasks", lede, filters, views))
        if not data.tasks:
            box.append(c.ListGroup(Row("Nothing to do yet", "Add a task below, or ask an agent. Agents add the "
                                       "work you ask for and claim a task before they start it.",
                                       lead="list-todo"), css="pgs-box"))
            return box
        shown = {"active": open_, "done": done, "all": data.tasks}[self.task_filter]
        if self.task_view == "board" and not self.sheet.phone:
            box.append(self.task_board(data.tasks if self.task_filter == "all" else
                                       (open_ + done if self.task_filter == "active" else done), data))
            return box
        groups = (("Underway", [t for t in shown if t.status == "doing"]),
                  ("To do", [t for t in shown if t.status == "todo"]),
                  ("Done", [t for t in shown if t.status == "done"]),
                  ("Dropped", [t for t in shown if t.status == "dropped"]))
        rank = {"high": 0, "normal": 1, "low": 2}
        any_rows = False
        for title, tasks in groups:
            if not tasks:
                continue
            any_rows = True
            tasks.sort(key=lambda t: (rank.get(t.priority, 1), t.title.lower()) if t.is_open else
                       (-c.module("core.model").ts_sort_key(t.updated_at),))
            box.append(self.subhead(title, str(len(tasks))))
            box.append(c.ListGroup(*[self.task_row(t, data) for t in tasks], css="pgs-box"))
        if not any_rows:
            box.append(c.ListGroup(Row("Nothing here", "Switch to All to see every task.", lead="list-todo"),
                                   css="pgs-box"))
        return box

    def _set_task(self, name: str, value: str) -> None:
        setattr(self, name, value)
        self.render(keep_scroll=True)

    def _task_meta(self, task, data) -> tuple[list[Gtk.Widget], str]:
        members = self._members()
        tags: list[Gtk.Widget] = []
        holder = task.claim_holder()
        if holder and task.is_open:
            tags.append(Tag(f"{who_words(holder, members)} is on it", "run"))
        if task.priority == "high" and task.is_open:
            tags.append(Tag("High", "amber"))
        bits = []
        ticked, total = check_progress(task.body)
        if total:
            bits.append(f"{ticked}/{total} checks")
        if task.assignee:
            bits.append(f"for {members.get(task.assignee, task.assignee)}")
        if task.created_by:
            bits.append(who_words(task.created_by, members))
        return tags, " · ".join(bits)

    def task_row(self, task, data) -> Gtk.Widget:
        tags, sub = self._task_meta(task, data)
        if self.sheet.phone and tags:
            # On a phone the title gets the row's width; the tags' words go on the line under it.
            sub = " · ".join(filter(None, [" · ".join(t.get_label() for t in tags), sub]))
            tags = []
        tick = self._tick(task)
        return Row(task.title, sub or None, lead=tick, tags=tags, end=c.glyph("chevron-right", 15, "pgs-chevron"),
                   on_activate=lambda t=task: self.open_task(t.id), dim=not task.is_open)

    def _tick(self, task) -> Gtk.Widget:
        done = task.status == "done"
        tick = Gtk.Button(valign=Gtk.Align.CENTER)
        tick.add_css_class("pgs-task-tick")
        if done:
            tick.add_css_class("done")
        if task.status == "doing":
            tick.add_css_class("doing")
        tick.set_child(c.glyph("check", 14) if done else Gtk.Box())
        tick.set_tooltip_text("Mark as not done" if done else "Mark as done")
        tick.update_property([Gtk.AccessibleProperty.LABEL], [f"{task.title}: {'done' if done else 'not done'}"])
        tick.connect("clicked", lambda _b, t=task: self.set_task(t.id, status="todo" if t.status == "done" else "done"))
        return tick

    def task_board(self, tasks: list, data) -> Gtk.Widget:
        columns = Gtk.Box(spacing=12, homogeneous=True)
        for status, words in (("todo", "To do"), ("doing", "Underway"), ("done", "Done")):
            items = [t for t in tasks if t.status == status]
            column = c.vbox(spacing=8, css="pgs-board-column")
            column.append(c.hbox(text(words, "pgs-sub-title", wrap=False),
                                 text(str(len(items)), "pgs-nav-count", wrap=False), spacing=8))
            for task in items:
                tags, sub = self._task_meta(task, data)
                card = Gtk.Button()
                card.add_css_class("pgs-board-card")
                if not task.is_open:
                    card.add_css_class("dim")
                inner = c.vbox(spacing=6)
                inner.append(c.hbox(self._tick(task), text(task.title, "pgs-board-title", hexpand=True),
                                    spacing=10))
                if tags:
                    inner.append(c.hbox(*tags, spacing=6))
                if sub:
                    inner.append(text(sub, "pgs-row-sub"))
                card.set_child(inner)
                card.connect("clicked", lambda _b, t=task: self.open_task(t.id))
                column.append(card)
            columns.append(column)
        return columns

    def open_task(self, task_id: str) -> None:
        self.at = "tasks"
        self.task_open = task_id
        self.render()
        self.scroll.get_vadjustment().set_value(0)

    def task_detail(self, task, data) -> Gtk.Widget:
        members = self._members()
        back = c.button("Tasks", icon="chevron-left", style="plain", small=True,
                        on_click=lambda: (setattr(self, "task_open", None), self.render()))
        back.set_halign(Gtk.Align.START)
        linked = bool(task.conversation and getattr(self.window, "project", None) is not None
                      and self.window.project.goal(task.conversation))
        work = c.button("Open the conversation" if linked else "Work on this", icon="message-square", style="key",
                        small=True, on_click=lambda: self.work_on(task))
        box = c.vbox(back, self.heading(task.title, f"{task.id} · added by {who_words(task.created_by, members)}"
                                        f"{' · ' + c.shared.day_words(c.shared.parse_ts(task.created_at)) if task.created_at else ''}",
                                        work), spacing=0)
        holder = task.claim_holder()
        if holder and task.is_open:
            box.append(callout(f"<b>{c.escape(who_words(holder, members))}</b> is working on this until "
                               f"{c.escape(_clock(task.claimed_until))}. Other agents leave it alone until then.",
                               icon="lock", tone="info"))
        status = c.segmented([("todo", "To do"), ("doing", "Underway"), ("done", "Done"), ("dropped", "Dropped")],
                             task.status, label="Status", on_change=lambda key: self.set_task(task.id, status=key))
        priority = c.segmented([("high", "High"), ("normal", "Normal"), ("low", "Low")], task.priority,
                               label="Priority", on_change=lambda key: self.set_task(task.id, priority=key))
        status.set_halign(Gtk.Align.START)
        priority.set_halign(Gtk.Align.START)
        box.append(c.vbox(c.hbox(text("Status", "pgs-field-label", wrap=False), status, spacing=12),
                          c.hbox(text("Priority", "pgs-field-label", wrap=False), priority, spacing=12),
                          spacing=10, css="pgs-form-card"))
        box.append(self.subhead("Details", "Markdown. Checks are lines like “- [ ] A parent can offer seats”."))
        body = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False, top_margin=10, bottom_margin=10,
                            left_margin=12, right_margin=12)
        body.add_css_class("pgs-task-body")
        body.get_buffer().set_text(task.body or "")
        body.update_property([Gtk.AccessibleProperty.LABEL], ["Task details"])
        body.set_size_request(-1, 160)
        save = c.button("Save details", style="fill", small=True,
                        on_click=lambda: self.set_task(task.id, body=_buffer(body)))
        save.set_halign(Gtk.Align.END)
        box.append(c.vbox(body, css="pgs-field"))
        save.set_margin_top(8)
        box.append(save)
        when = c.shared.parse_ts(task.updated_at)
        if task.updated_by:
            box.append(text(f"Last changed by {who_words(task.updated_by, members)}"
                            f"{' · ' + c.shared.day_words(when) if when else ''}", "pgs-row-sub"))
        return box

    def set_task(self, task_id: str, **changes) -> None:
        store = c.module("core.store").ProjectStore(self.ctx.project_dir)
        me = self._me()

        def change(task) -> None:
            for key, value in changes.items():
                setattr(task, key, value)
            if changes.get("status") in ("todo", "done", "dropped") and task.claimed_by:
                task.claimed_by = task.claimed_until = ""

        c.run_async(lambda: store.update_task(task_id, change, by=me), lambda _r: self.load(),
                    lambda error: c.toast(self.sheet.card, str(error), kind="error"))

    def work_on(self, task) -> None:
        """A conversation about this task, claimed for its agent, with the ask written for the person to send."""
        window = self.window
        if task.conversation and callable(getattr(window, "open_conversation", None)) and \
                window.project is not None and window.project.goal(task.conversation):
            window.open_conversation(task.conversation)
            return
        starter = getattr(window, "start_conversation", None)
        if callable(starter):
            starter(task.title, f"Work on task {task.id}: {task.title}. It's claimed for you; mark it done "
                                f"with a one-line outcome when it's proved.", task_id=task.id)

    def start_task(self) -> None:
        center, kit = self.center, c.kit
        if center is None or kit is None:
            return
        self.adding_task = True

        def submit(words: str) -> None:
            words = (words or "").strip()
            if not words:
                return
            store = c.module("core.store").ProjectStore(self.ctx.project_dir)
            me = self._me()
            field.clear()
            self.task_draft = ""
            c.run_async(lambda: store.create_task(words, by=me), lambda _t: self.load(),
                        lambda error: c.toast(self.sheet.card, str(error), kind="error"))

        def close() -> None:
            self.adding_task = False
            self.update_bar()

        field = kit.BarEntry("quick", icon="plus", text=self.task_draft,
                             placeholder="Add a task, like “Let parents offer rides”",
                             on_change=lambda value: setattr(self, "task_draft", value), on_submit=submit,
                             on_close=close, close_label="Done")
        center.show_bar([field])
        field.focus()

    # ── Bugs: report one from the bar, hand one to an agent ──

    def start_bug(self) -> None:
        center, kit = self.center, c.kit
        if center is None or kit is None:
            self.toggle_form("bug")
            return
        title = Gtk.Entry(placeholder_text="The Join waitlist button does nothing on a phone", hexpand=True)
        title.update_property([Gtk.AccessibleProperty.LABEL], ["What went wrong"])
        choice = {"severity": "medium", "place": "live"}
        how_bad = c.segmented([("low", "Low"), ("medium", "Medium"), ("high", "High"), ("urgent", "Urgent")],
                              "medium", label="How bad", on_change=lambda key: choice.update(severity=key))
        where = c.segmented([("live", "Live"), ("stage", "Preview")], "live", label="Where",
                            on_change=lambda key: choice.update(place=key))
        for switch in (how_bad, where):
            switch.set_halign(Gtk.Align.START)

        def save() -> None:
            words = title.get_text().strip()
            if not words:
                title.grab_focus()
                return
            details = (editor.draft or "").strip()
            project = self.data.project if self.data else None
            reporter = project.owner.name if project is not None and project.owner else ""
            editor.clear()
            title.set_text("")
            center.fold()

            def done(number: int) -> None:
                self.issue_open = number
                self.load()
            c.run_async(lambda: self.create_issue(words, choice["severity"], choice["place"],
                                                  {"what": details}, reporter), done,
                        lambda error: c.toast(self.sheet.card, str(error), kind="error"))

        editor = kit.ActionEditor("Report a bug", "circle-alert", summary="agents see it when they start work",
                                  fields=[("What", title), ("How bad", how_bad), ("Where", where)],
                                  placeholder="What happened, what you expected, and how to make it happen again",
                                  primary=kit.BarAction("check", "Report it", on_activate=save))
        center.set_editor(editor)
        center.grow()
        GLib.idle_add(lambda: (title.grab_focus(), False)[1])

    def fix_with_agent(self, issue: dict) -> None:
        """A conversation for the fix, with the agent you use, linked to the bug both ways."""
        starter = getattr(self.window, "start_conversation", None)
        if not callable(starter):
            self.fix_bug(issue)
            return
        number = issue.get("number")
        sections = issue.get("sections") or {}
        ask = " ".join(filter(None, [
            f"Fix bug #{number}: {issue.get('title')}.",
            f"What happened: {sections.get('what happened', '').strip()}" if sections.get("what happened") else "",
            f"Expected: {sections.get('expected', '').strip()}" if sections.get("expected") else "",
            "Prove it with a check that fails before the fix and passes after, then mark the bug fixed."]))
        goal_id = starter(f"Fix: {issue.get('title')}", ask, send=True)
        if goal_id:
            store = c.module("core.store").ProjectStore(self.ctx.project_dir)
            c.run_async(lambda: self._write_issue(store, issue, goal=goal_id, status="fixing"),
                        lambda _r: self.load(), lambda error: c.toast(self.sheet.card, str(error), kind="error"))

    # ── Stack: add a service from the bar ──

    def start_service(self) -> None:
        center, kit = self.center, c.kit
        if center is None or kit is None:
            self.toggle_form("service")
            return
        state = {"mode": "agent"}
        name = Gtk.Entry(placeholder_text="Resend", hexpand=True)
        name.update_property([Gtk.AccessibleProperty.LABEL], ["Name"])
        console = Gtk.Entry(placeholder_text="https://resend.com/emails (optional)", hexpand=True)
        console.update_property([Gtk.AccessibleProperty.LABEL], ["Console address"])
        kinds = list(KIND_WORDS_FOR_FORM)
        kind = Gtk.DropDown.new_from_strings([KIND_WORDS_FOR_FORM[k] for k in kinds])
        kind.set_halign(Gtk.Align.START)
        kind.set_valign(Gtk.Align.CENTER)
        kind.add_css_class("flat")
        kind.update_property([Gtk.AccessibleProperty.LABEL], ["Kind of service"])
        placeholders = {"agent": "We send emails with Resend, and photos live in Cloudflare R2",
                        "form": "What it does, in plain words"}

        def mode(key: str) -> None:
            state["mode"] = key
            for widget in (name, console, kind):
                (widget.get_parent() or widget).set_visible(key == "form")      # the field's whole row
            editor.set_summary("an agent adds it, and asks for any key straight into the vault" if key == "agent"
                               else "you fill it in; keys go in the vault, never here")
            hint = getattr(editor, "_placeholder", None)
            if hint is not None:
                hint.set_label(placeholders[key])

        def save() -> None:
            words = (editor.draft or "").strip()
            if state["mode"] == "agent":
                if not words:
                    editor.focus_content()
                    return
                editor.clear()
                center.fold()
                starter = getattr(self.window, "start_conversation", None)
                if callable(starter):
                    starter("Add to the stack", f"Add this to the project's stack: {words} Ask me for any key "
                                                "straight into the vault, never into a file.", send=True)
                return
            if not name.get_text().strip():
                name.grab_focus()
                return
            console_url = console.get_text().strip()
            if console_url and not console_url.startswith("https://"):
                c.toast(self.sheet.card, "The console address starts with https://", kind="error")
                return
            values = {"name": name.get_text().strip(), "provider": "", "purpose": words, "console_url": console_url,
                      "plan": "", "monthly_budget": "", "secrets": "", "stage": "", "live": "",
                      "kind": kinds[kind.get_selected()]}
            editor.clear()
            name.set_text("")
            console.set_text("")
            center.fold()
            c.run_async(lambda: self.add_service(values), lambda _r: self.load(),
                        lambda error: c.toast(self.sheet.card, str(error), kind="error"))

        editor = kit.ActionEditor("Add a service", "layers", modes=[("agent", "Tell an agent", "sparkles"),
                                                                    ("form", "Fill it in", "pencil")],
                                  mode="agent", on_mode=mode,
                                  fields=[("Name", name), ("Kind", kind), ("Console", console)],
                                  placeholder=placeholders["agent"],
                                  primary=kit.BarAction("check", "Add", on_activate=save))
        center.set_editor(editor)
        center.grow()
        mode("agent")
        GLib.idle_add(lambda: (editor.focus_content(), False)[1])

    # ── the team channel, at the top of Activity ──

    def channel_digest(self, data) -> Gtk.Widget | None:
        """What agents and people said to each other lately: what they started, finished, warned about."""
        posts = list(data.channel)[-8:]
        if not posts:
            return None
        members = self._members()
        tones = {"start": "run", "done": "good", "heads-up": "amber", "question": "violet", "answer": None, "note": None}
        words = {"start": "Starting", "done": "Done", "heads-up": "Heads-up", "question": "Question", "answer": "Answer",
                 "note": "Note"}
        rows = []
        for post in reversed(posts):
            kind = str(post.get("kind") or "note")
            when = c.shared.parse_ts(post.get("ts"))
            sub = " · ".join(filter(None, [who_words(str(post.get("from") or ""), members),
                                           c.shared.day_words(when) + when.astimezone().strftime(", %-I:%M %p")
                                           if when else "", str(post.get("task") or "")]))
            rows.append(Row(str(post.get("text") or ""), sub, tags=[Tag(words.get(kind, kind.capitalize()),
                                                                         tones.get(kind))]))
        box = c.vbox(self.subhead("Team channel", "How agents working at the same time stay out of each other’s way"),
                     c.ListGroup(*rows, css="pgs-box"))
        box.set_margin_bottom(8)
        return box

    # ── Knowledge ──

    def section_knowledge(self, data) -> Gtk.Widget:
        model = c.module("core.model")
        notes = list(data.knowledge)
        if self.note_open:
            note = self._open_note()
            if note is not None:
                return self.note_detail(note)
            self.note_open = None
        finder = c.module("core.search")
        wanted = finder.terms(self.knowledge_query) if finder is not None else []
        if wanted:
            notes = [n for n in notes if all(t in finder.fold(f"{n.title}\n{n.body}") for t in wanted)]
        waiting = [n for n in notes if not model.knowledge_kept(n)]
        kept = [n for n in notes if model.knowledge_current(n)]         # replaced notes stay in the folder, unlisted
        by_kind = {kind: sorted((n for n in kept if model.knowledge_kind(n) == kind), key=lambda n: n.title.lower())
                   for kind in model.KNOWLEDGE_KINDS}
        decisions = [] if wanted else list(data.decisions)
        members = self._members()
        study = self._study(data)
        lede = ("What agents and the team know about this project. Rules are in front of every agent, always; "
                "the rest is read when the work touches it.")
        box = c.vbox(self.heading("Knowledge", lede))
        for note in waiting:
            box.append(self.suggestion_card(note))
        if wanted or self.knowledge_filter != "all":
            return self._knowledge_list(box, by_kind, decisions, members, wanted)

        # The shape of what's known, at a glance; each tile opens its kind in full.
        lessons = len(by_kind["lesson"]) + len(by_kind["mistake"])
        from .book import stat_tile
        box.append(self.columns(
            stat_tile("Rules", str(len(by_kind["rule"])), "always in force",
                      on_click=lambda: self._set_task("knowledge_filter", "rule")),
            stat_tile("Parts", str(len(by_kind["part"])), "of the code",
                      on_click=lambda: self._set_task("knowledge_filter", "part")),
            stat_tile("Decisions", str(len(by_kind["decision"]) + len(decisions)), "with why",
                      on_click=lambda: self._set_task("knowledge_filter", "decision")),
            stat_tile("Lessons", str(lessons), "and mistakes",
                      on_click=lambda: self._set_task("knowledge_filter", "lesson")),
            wide=4, phone=2))
        if study is not None:
            box.append(self.study_row(study))
        elif not by_kind["part"]:
            box.append(self.study_card())

        purpose = by_kind["purpose"]
        if purpose:
            card = c.vbox(text("What it’s for", "pgs-kcard-label"), text(purpose[0].title, "pgs-kpurpose-title"),
                          text(_plain_note(purpose[0].body)[:400], "pgs-row-sub") if purpose[0].body.strip()
                          else Gtk.Box(), spacing=4, css="pgs-kpurpose")
            box.append(card)

        box.append(self.subhead("Rules", "Every agent always has these in front of it"))
        if by_kind["rule"]:
            rows = [Row(n.title, " ".join(_plain_note(n.body).split())[:160] or None,
                        lead=text(str(i), "pgs-rule-number", wrap=False, xalign=0.5), chevron=True,
                        on_activate=lambda n=n: self.open_note(n.slug))
                    for i, n in enumerate(by_kind["rule"], 1)]
            box.append(c.ListGroup(*rows, css="pgs-box"))
        else:
            box.append(c.ListGroup(Row("No rules yet", "Add how things must always be done here, like “Never "
                                       "publish internal notes to GitHub”. Every agent follows them.",
                                       lead="shield-check"), css="pgs-box"))

        if by_kind["part"]:
            box.append(self.subhead("Parts", f"{len(by_kind['part'])} · how the code is built; agents read a "
                                    "part’s note before changing it"))
            box.append(self.columns(*[self.part_card(n) for n in by_kind["part"][:12]], wide=2))
            if len(by_kind["part"]) > 12:
                more = c.button(f"All {len(by_kind['part'])} parts", style="plain", small=True,
                                on_click=lambda: self._set_task("knowledge_filter", "part"))
                more.set_halign(Gtk.Align.START)
                box.append(more)

        cards = []
        decision_rows = [self.note_row(n, members) for n in by_kind["decision"]] + \
            [self.decision_row(d) for d in decisions]
        for kind, title, rows in (("decision", "Decisions", decision_rows),
                                  ("lesson", "Lessons and mistakes",
                                   [self.note_row(n, members) for n in by_kind["lesson"] + by_kind["mistake"]]),
                                  ("note", "Notes", [self.note_row(n, members) for n in by_kind["note"]])):
            if rows:
                cards.append(self.kind_card(kind, title, rows))
        if cards:
            box.append(self.subhead("Everything else", "read when the work touches it"))
            box.append(self.columns(*cards, wide=2))
        return box

    def _knowledge_list(self, box, by_kind: dict, decisions: list, members: dict, wanted: list) -> Gtk.Widget:
        """One kind in full (a tile was chosen), or everything that matches the search."""
        model = c.module("core.model")
        words = model.KNOWLEDGE_KIND_WORDS
        if not wanted:
            back = c.button("All knowledge", icon="chevron-left", style="plain", small=True,
                            on_click=lambda: self._set_task("knowledge_filter", "all"))
            back.set_halign(Gtk.Align.START)
            box.append(back)
        kinds = list(model.KNOWLEDGE_KINDS) if wanted else \
            (["lesson", "mistake"] if self.knowledge_filter == "lesson" else [self.knowledge_filter])
        shown = 0
        for kind in kinds:
            rows = [self.note_row(n, members) for n in by_kind.get(kind, [])]
            if kind == "decision":
                rows += [self.decision_row(d) for d in decisions]
            if not rows:
                continue
            shown += len(rows)
            box.append(self.subhead(words[kind], str(len(rows))))
            box.append(c.ListGroup(*rows, css="pgs-box"))
        if not shown:
            box.append(c.ListGroup(Row(f"Nothing matches “{self.knowledge_query.strip()}”" if wanted else
                                       "Nothing here yet", "Try fewer words, or a different one." if wanted else
                                       "Agents add these as they work, or Study the project fills them in.",
                                       lead="search" if wanted else "book-open"), css="pgs-box"))
        return box

    def part_card(self, note) -> Gtk.Widget:
        model = c.module("core.model")
        paths = model.knowledge_paths(note)
        uses = note.extra.get("uses") or []
        uses = [str(u) for u in (uses if isinstance(uses, list) else str(uses).strip("[]").split(","))
                if str(u).strip()]
        first = " ".join(_plain_note(note.body).split())
        inner = c.vbox(c.hbox(c.glyph("layers", 16), text(note.title, "pgs-kpart-title", wrap=False, ellipsize=True,
                                                         hexpand=True), spacing=8),
                       text(first[:140] + ("…" if len(first) > 140 else ""), "pgs-row-sub") if first else Gtk.Box(),
                       spacing=6)
        chips = c.Adw.WrapBox(child_spacing=6, line_spacing=6)
        for path in paths[:3]:
            chip = text(path, "pgs-kpath", wrap=False, ellipsize=True)
            chip.set_max_width_chars(28)
            chips.append(chip)
        for use in uses[:3]:
            chips.append(Tag(use.strip().strip("'\""), None))
        if paths or uses:
            inner.append(chips)
        button = Gtk.Button(child=inner, hexpand=True)
        button.add_css_class("pgs-kpart")
        button.update_property([Gtk.AccessibleProperty.LABEL], [f"{note.title}: {first[:120]}"])
        button.connect("clicked", lambda _b: self.open_note(note.slug))
        return button

    def kind_card(self, kind: str, title: str, rows: list) -> Gtk.Widget:
        head = c.hbox(text(title, "pgs-kcard-title", wrap=False, hexpand=True),
                      text(str(len(rows)), "pgs-sub-aside", wrap=False), spacing=8)
        card = c.vbox(head, c.ListGroup(*rows[:5], css="pgs-box"), spacing=8, css="pgs-kcard")
        if len(rows) > 5:
            more = c.button(f"All {len(rows)}", style="plain", small=True,
                            on_click=lambda: self._set_task("knowledge_filter", kind))
            more.set_halign(Gtk.Align.START)
            card.append(more)
        return card

    # ── studying the project: the knowledge base, built from the code by an agent ──

    def _study(self, data):
        """The last Study the project, while its conversation is still going (else None)."""
        entry = next((e for e in data.activity if getattr(e, "kind", "") == "study"), None)
        if entry is None or not entry.goal:
            return None
        goal = next((g for g in data.goals if g.id == entry.goal), None)
        if goal is None or goal.status.value not in ("working", "needs-you", "draft"):
            return None
        return goal

    def study_card(self) -> Gtk.Widget:
        start = c.button("Study the project", icon="sparkles", style="key", small=True, on_click=self.study)
        start.set_halign(Gtk.Align.START)
        card = c.vbox(c.hbox(c.glyph("book-open", 18), text("Build the knowledge base from the code", "pgs-kcard-title",
                                                           wrap=False), spacing=10),
                      text("An agent reads the whole project, part by part, and writes down how each part works, "
                           "what it relies on, the stack, the conventions it finds and the decisions in its "
                           "history. Rules it finds wait for you to keep them. It changes no code. On a big "
                           "project it takes a while and uses your plan.", "pgs-row-sub"),
                      start, spacing=10, css="pgs-kstudy")
        return card

    def study_row(self, goal) -> Gtk.Widget:
        opener = getattr(self.window, "open_conversation", None)
        return c.ListGroup(Row("Studying the project", f"In “{goal.title}”. New notes appear here as they’re "
                               "written.", lead=c.shared.Spinner(18), chevron=True,
                               on_activate=lambda: opener(goal.id) if callable(opener) else None), css="pgs-box")

    def study(self) -> None:
        goal_id = self.defer_to_agent("Study the project", STUDY_ASK)
        if goal_id:
            store = c.module("core.store").ProjectStore(self.ctx.project_dir)
            c.run_async(lambda: store.log("Asked an agent to study the project and build the knowledge base",
                                          kind="study", actor=self._me(), goal=goal_id),
                        lambda _r: self.load(), lambda _e: None)

    def note_row(self, note, members: dict) -> Gtk.Widget:
        body = " ".join(_plain_note(note.body).replace("• ", "").split())
        meta = " · ".join(filter(None, [who_words(note.added_by, members),
                                        c.shared.day_words(c.shared.parse_ts(note.updated)) if note.updated else ""]))
        sub = "\n".join(filter(None, [body[:240] + ("…" if len(body) > 240 else ""), meta]))
        return Row(note.title, sub or None, chevron=True, on_activate=lambda n=note: self.open_note(n.slug))

    # ── one note: read it, edit it in the bar, make a lesson a rule ──

    def _open_note(self):
        if not self.note_open or self.data is None:
            return None
        return next((n for n in self.data.knowledge if n.slug == self.note_open), None)

    def open_note(self, slug: str) -> None:
        self.note_open = slug
        self.render()
        self.scroll.get_vadjustment().set_value(0)

    def note_detail(self, note) -> Gtk.Widget:
        model = c.module("core.model")
        kind = model.knowledge_kind(note)
        members = self._members()
        back = c.button("Knowledge", icon="chevron-left", style="plain", small=True,
                        on_click=lambda: (setattr(self, "note_open", None), self.render()))
        back.set_halign(Gtk.Align.START)
        when = c.shared.parse_ts(note.updated) if note.updated else None
        meta = " · ".join(filter(None, [model.KNOWLEDGE_KIND_WORDS[kind].rstrip("s") if kind != "purpose" else
                                        "Purpose", f"added by {who_words(note.added_by, members)}"
                                        if note.added_by else "", c.shared.day_words(when) if when else ""]))
        box = c.vbox(back, self.heading(note.title, meta), spacing=0)
        if kind == "rule":
            box.append(callout("Every agent has this in front of it, always.", icon="shield-check", tone="info"))
        card = c.vbox(text(_plain_note(note.body) or "No more to it than the title.", "pgs-note-body",
                           selectable=True), css="pgs-box")
        card.set_margin_top(12)
        box.append(card)
        return box

    def make_rule(self, note) -> None:
        store = c.module("core.store").ProjectStore(self.ctx.project_dir)

        def work() -> None:
            note.extra["kind"] = "rule"
            note.extra["status"] = "kept"
            note.updated = ""
            store.write_knowledge(note)
        c.run_async(work, lambda _r: (self.load(), c.toast(self.sheet.card, "Every agent follows it now")),
                    lambda error: c.toast(self.sheet.card, str(error), kind="error"))

    def _knowledge_search(self, query: str) -> None:
        if query == self.knowledge_query:
            return
        self.knowledge_query = query
        self._bar_frozen = True
        try:
            self.render(keep_scroll=False)
        finally:
            self._bar_frozen = False

    def decision_row(self, decision) -> Gtk.Widget:
        who = getattr(decision, "who", "") or ""
        why = getattr(decision, "why", "") or ""
        chosen = getattr(decision, "chosen", "") or getattr(decision, "title", "")
        sub = " · ".join(filter(None, [why, who]))
        return Row(str(getattr(decision, "title", "") or chosen), sub or None)

    def suggestion_card(self, note) -> Gtk.Widget:
        members = self._members()
        who = who_words(note.added_by, members) or "An agent"
        keep = c.button("Keep", style="key", small=True, on_click=lambda: self.keep_note(note, True))
        drop = c.button("Drop", style="plain", small=True, on_click=lambda: self.keep_note(note, False))
        later = text("Agents won’t follow it until someone keeps it", "pgs-row-sub", wrap=False)
        later.set_visible(not self.sheet.phone)

        body = " ".join(note.body.split())
        more = None
        if len(body) > 300:
            # The collapsed view keeps roughly the old length, but ends on a word and an ellipsis.
            short = body[:299].rsplit(" ", 1)[0] + "…"
            body_label = text(short, "pgs-row-sub")
            more = c.button("Show more", style="plain", small=True)
            expanded = [False]

            def set_label(label: str) -> None:
                widget_label = getattr(more, "label_widget", None)
                if widget_label is not None:
                    widget_label.set_label(label)
                else:
                    more.set_label(label)
                more.update_property([Gtk.AccessibleProperty.LABEL], [label])

            def flip(_button) -> None:
                expanded[0] = not expanded[0]
                body_label.set_label(body if expanded[0] else short)
                more.update_state([Gtk.AccessibleState.EXPANDED], [GObject.Value(GObject.TYPE_INT, int(expanded[0]))])
                set_label("Show less" if expanded[0] else "Show more")

            more.connect("clicked", flip)
        else:
            body_label = text(body, "pgs-row-sub") if body else Gtk.Box()

        card = c.vbox(c.hbox(c.glyph("sparkles", 16), text(f"{who} suggests a rule", "pgs-sub-title", wrap=False,
                                                            hexpand=True, ellipsize=True), later, spacing=8),
                      text(note.title, "pgs-suggest-title"),
                      body_label,
                      c.hbox(keep, drop, more, spacing=8), spacing=8, css="pgs-suggest-card")
        card.set_margin_bottom(14)
        return card

    def keep_note(self, note, keep: bool) -> None:
        store = c.module("core.store").ProjectStore(self.ctx.project_dir)

        def work() -> None:
            if keep:
                note.extra["status"] = "kept"
                note.updated = ""
                store.write_knowledge(note)
            else:
                store.delete_knowledge(note.slug)

        c.run_async(work, lambda _r: (self.load(), c.toast(self.sheet.card, "Every agent follows it now" if keep
                                                            else "Dropped")),
                    lambda error: c.toast(self.sheet.card, str(error), kind="error"))

    def forget_note(self, note) -> None:
        store = c.module("core.store").ProjectStore(self.ctx.project_dir)
        self.note_open = None
        c.run_async(lambda: store.delete_knowledge(note.slug), lambda _r: self.load(),
                    lambda error: c.toast(self.sheet.card, str(error), kind="error"))

    def start_note(self, editing=None) -> None:
        center, kit = self.center, c.kit
        model = c.module("core.model")
        if center is None or kit is None:
            self.toggle_form("note")
            return
        state = {"kind": model.knowledge_kind(editing) if editing is not None else "rule"}
        title = Gtk.Entry(placeholder_text="Never publish internal notes to GitHub", hexpand=True,
                          text=editing.title if editing is not None else "")
        title.update_property([Gtk.AccessibleProperty.LABEL], ["Title"])
        icons = {"purpose": "goal", "decision": "hand", "rule": "shield-check", "part": "layers", "lesson": "lightbulb",
                 "mistake": "circle-alert", "note": "file-text"}
        modes = [(k, model.KNOWLEDGE_KIND_WORDS[k].rstrip("s") if k != "purpose" else "Purpose", icons[k])
                 for k in model.KNOWLEDGE_KINDS]

        def save() -> None:
            words = title.get_text().strip()
            if not words:
                title.grab_focus()
                return
            body = editor.draft or ""
            store = c.module("core.store").ProjectStore(self.ctx.project_dir)
            if editing is not None:
                note = editing
                note.title, note.body, note.updated = words, body.strip(), ""
                note.extra.update(kind=state["kind"], status="kept", edited_by=self._me())
            else:
                note = model.KnowledgeNote(slug="", title=words, body=body.strip(), added_by=self._me(),
                                           extra={"kind": state["kind"], "status": "kept",
                                                  "created": model.now_iso()})
            editor.clear()
            title.set_text("")
            center.fold()
            c.run_async(lambda: store.write_knowledge(note), lambda _r: self.load(),
                        lambda error: c.toast(self.sheet.card, str(error), kind="error"))

        first = dict((k, (w, i)) for k, w, i in modes)[state["kind"]]
        editor = kit.ActionEditor(first[0], first[1], summary=_kind_summary(state["kind"]), modes=modes,
                                  mode=state["kind"], on_mode=lambda key: (state.update(kind=key),
                                                                          editor.set_summary(_kind_summary(key))),
                                  fields=[("Title", title)], placeholder="Why, or how, in a line or two (optional)",
                                  primary=kit.BarAction("check", "Save", on_activate=save))
        if editing is not None and editing.body.strip() and editor.text_view is not None:
            editor.text_view.get_buffer().set_text(editing.body.strip())
        center.set_editor(editor)
        center.grow()
        GLib.idle_add(lambda: (title.grab_focus(), False)[1])


# Study the project: what the agent is asked, once, to build the knowledge base from the code.
STUDY_ASK = (
    "Study this whole project and build its knowledge base in .playground/knowledge/ (formats in "
    ".playground/guide.md). Work part by part, top to bottom; don't stop at the top level. Change no code.\n\n"
    "1. Purpose: if there's no purpose note, write one: what the project is for, and who uses it.\n"
    "2. Parts: one note per meaningful part of the code (kind: part, with paths: and uses:): what it does, how "
    "data goes in and out, what it relies on, and how to change it safely. Small parts can share a note.\n"
    "3. Stack: every outside service in .playground/stack.toml (names of secrets only, never values), and the "
    "main languages and frameworks in a note.\n"
    "4. Rules the code already follows (formatters, linters, CI, CONTRIBUTING, existing AGENTS.md or CLAUDE.md): "
    "kind: rule, status: suggested. I decide which to keep.\n"
    "5. Decisions you can see in docs, ADRs or the commit history: kind: decision, with why.\n"
    "6. Real problems you find (broken tests, TODOs that matter): file them as bugs.\n\n"
    "Read .playground/knowledge/index.md first and update notes rather than add near copies. Short notes, facts "
    "not guesses; write 'unclear' where you aren't sure. Skip dependencies, build output, generated and vendored "
    "files and lockfiles. When you're done, post one line to the team channel and tell me what you covered and "
    "what you skipped.")


def _plain_note(body: str) -> str:
    lines = []
    for line in (body or "").splitlines():
        line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "• ", line)
        lines.append(re.sub(r"[*_`]{1,2}([^*_`]+)[*_`]{1,2}", r"\1", line))
    return "\n".join(lines).strip()


def _kind_summary(kind: str) -> str:
    return {"rule": "every agent always follows it", "decision": "something that was settled, and why",
            "lesson": "what to do when it happens again", "mistake": "what went wrong, so it doesn’t again",
            "purpose": "what the project is for", "note": "anything worth knowing",
            "part": "how one part of the code works"}.get(kind, "")


def _buffer(view: Gtk.TextView) -> str:
    buffer = view.get_buffer()
    return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)


def _clock(stamp: str) -> str:
    moment = c.shared.parse_ts(stamp)
    if moment is None:
        return stamp
    return moment.astimezone().strftime("%-I:%M %p")
