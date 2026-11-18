# SPDX-License-Identifier: Apache-2.0
"""Usage: agents, models and money (v73 shot 06).

- **Your connections**: how much each has left and who can use it. A row opens
  its own page: Who can use it, Work on team tasks (with a cap), Where it runs
  (Move to the server), and the spending cap for anything shared.
- **The team's capacity**: members who chose to show how much they have left.
  They share the number, never the account. Yours is a switch here.
- **This month**: what the project spent, from the runs Playground recorded.
  Personal plans aren't money here; their owners pay for them.
- **Recent runs**: every run says whose model it used.

Numbers come from core.usage (it never guesses one it wasn't given) and
settings from agents.connections, whose rules (a personal plan is never shared)
are enforced there as well as here.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from gi.repository import Gtk

from . import common as c
from . import picker
from .common import Row, Tag, callout, get, section, text

RULE = ("Personal plans are for one person, so they’re never shared. Your agent can still <b>work on team "
        "tasks</b>, on your plan, if you choose.")


def present(window: Gtk.Window, **options) -> c.Sheet:
    return UsageSheet(window, options).sheet


def month_start() -> datetime:
    now = datetime.now().astimezone()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def month_lines(project_id: str) -> dict:
    """This month's spending on one project, per connection, from recorded runs."""
    usage = c.module("core.usage")
    connections = {conn.id: conn for conn in picker.load_mine()}
    if usage is None:
        return {"lines": [], "total": 0.0, "runs": []}
    records = [r for r in usage.runs(since=month_start()) if not project_id or r.get("project") == project_id]
    spent: dict[str, float] = {}
    count: dict[str, int] = {}
    for record in records:
        key = record.get("connection", "")
        count[key] = count.get(key, 0) + 1
        if "cost" in record:
            spent[key] = spent.get(key, 0.0) + float(record["cost"])
    lines = []
    for key, amount in sorted(spent.items(), key=lambda pair: -pair[1]):
        conn = connections.get(key)
        if conn is not None and conn.is_personal:
            continue
        label = conn.label if conn else key
        note = {"team-key": "Shared, capped", "api-key": "Your key", "ari": "Ari credits",
                "local": "Free"}.get(conn.kind if conn else "", "")
        cap = conn.spend_cap if conn else ""
        lines.append({"label": label, "amount": round(amount, 2), "amount_text": c.money(amount),
                      "note": f"{note}{' · cap ' + cap if cap else ''}" if note or cap else
                      c.shared.plural(count.get(key, 0), "run"), "connection": key})
    total = round(sum(line["amount"] for line in lines), 2)
    return {"lines": lines, "total": total, "runs": list(reversed(records))[:8]}


class UsageSheet:
    def __init__(self, window: Gtk.Window, options: dict) -> None:
        self.window = window
        self.ctx = c.context_for(window, options)
        self.data: dict | None = None
        self.message: tuple[str, str] | None = None
        name = self.ctx.name
        self.sheet = c.Sheet(window, "Usage", f"Agents, models and money{' on ' + name if name else ''}",
                             name="usage")
        self.sheet.listener.on("usage.changed", lambda **_: self.load())
        self.sheet.set_body(self.build())
        self.sheet.present()
        self.detail_id = options.get("detail") if isinstance(options.get("detail"), str) else None
        self.load()

    # ── data ──

    def load(self) -> None:
        project_dir = self.ctx.project_dir

        def read() -> dict:
            usage = c.module("core.usage")
            mine = picker.load_mine()
            project = self.ctx.project
            rooms = {conn.id: picker.headroom(conn) for conn in mine}
            overviews = {}
            if usage is not None:
                for conn in mine:
                    try:
                        overviews[conn.id] = usage.overview(conn.id)
                    except Exception:  # noqa: BLE001
                        overviews[conn.id] = {}
            capacity = usage.team_capacity(project_dir) if usage and project_dir else []
            month = month_lines(project.id if project else "") if project else {"lines": [], "total": 0, "runs": []}
            goals = {}
            if project_dir is not None:
                try:
                    goals = {g.id: g.title for g in c.module("core.store").ProjectStore(project_dir).list_goals()}
                except Exception:  # noqa: BLE001
                    goals = {}
            return {"mine": mine, "rooms": rooms, "overviews": overviews, "capacity": capacity, "month": month,
                    "goals": goals, "project": project}

        def done(data: dict) -> None:
            self.data = data
            self.refresh()
            if self.detail_id:
                conn = next((x for x in data["mine"] if x.id == self.detail_id), None)
                self.detail_id = None
                if conn is not None:
                    self.open_detail(conn)

        c.run_async(read, done, lambda error: self.say("error", str(error)))

    def refresh(self) -> None:
        if self.sheet.closed:
            return
        if self.sheet.depth > 1 and getattr(self, "detail", None) is not None:
            fresh = next((x for x in (self.data or {}).get("mine", []) if x.id == self.detail.id), None)
            if fresh is not None:
                self.sheet.replace_top(self.build_detail(fresh))
            return
        self.sheet.replace_top(self.build())

    def say(self, kind: str, words: str) -> None:
        self.message = (kind, words)
        self.refresh()

    # ── the main page ──

    def build(self) -> Gtk.Widget:
        body = c.vbox()
        data = self.data
        if data is None:
            body.append(c.vbox(c.shared.Spinner(18), text("Reading usage…", "pgs-row-sub", xalign=0.5), spacing=10,
                               css="pgs-empty"))
            return body
        if self.message is not None:
            kind, words = self.message
            body.append(callout(c.escape(words), icon="circle-check" if kind == "ok" else "triangle-alert",
                                tone="good" if kind == "ok" else "warn"))
        body.append(section("Your connections", first=self.message is None,
                            action=c.link_button("Add", "plus", lambda: c.open_sheet(
                                self.window, "picker", **dict(self.ctx.options, page="add")))))
        rows = []
        for conn in sorted(data["mine"], key=lambda x: (x.kind != "ari", x.kind == "team-key", x.label.lower())):
            rows.append(self.connection_row(conn, data["rooms"].get(conn.id) or {}))
        if not rows:
            rows.append(Row("No connections yet", "Add Ari, Claude, Codex or a local model.", lead="sparkles",
                            on_activate=lambda: c.open_sheet(self.window, "picker", page="add")))
        body.append(c.ListGroup(*rows))
        rule = callout(RULE, icon="lock", tone="rule")
        rule.set_margin_top(10)
        body.append(rule)
        project = data.get("project")
        if project is not None:
            body.append(self.capacity(project, data))
            body.append(self.month(project, data))
            body.append(self.recent(data))
        return body

    def connection_row(self, conn, room: dict) -> Gtk.Widget:
        words = room.get("text") or room.get("quiet") or "No reading yet"
        lead = c.mark(conn, size=34)
        share_icon, share = picker.sharing_words(conn)
        end = c.hbox(text("Team tasks" if conn.work_on_team_tasks else
                          ("Team" if share == "Shared with the team" else "Just you"), "pgs-row-end", wrap=False),
                     c.glyph("chevron-right", 15, "pgs-chevron"), spacing=6)
        row = Row(conn.label, None, lead=lead, tags=[Tag(picker.kind_words(conn))], end=end,
                  on_activate=lambda x=conn: self.open_detail(x))
        meter_line = c.hbox(spacing=10)
        if room.get("fraction") is not None:
            meter_line.append(c.meter(room["fraction"], width=90, label=f"{conn.label}: {words}"))
        meter_line.append(text(words, "pgs-row-sub", wrap=False, ellipsize=True))
        row.words.append(meter_line)
        return row

    def capacity(self, project, data: dict) -> Gtk.Widget:
        box = c.vbox()
        me = project.owner
        shared = any(m.get("member") == (me.id if me else "") for m in data["capacity"])
        toggle = c.switch(shared, label="Show my headroom to the team",
                          on_toggle=lambda on: self.share_mine(project, on))
        box.append(section("The team’s capacity", note="People choose to show how much they have left. They share "
                                                      "the number, never the account."))
        rows = []
        mine = [x for x in data["mine"] if x.is_personal]
        rows.append(Row("Show mine", "Your personal plans’ headroom, as numbers, for the people on "
                                     f"{project.name}." if mine else "You have no personal plans to show.",
                        lead=c.shared.person_face(me.name if me else "You", 26), end=toggle))
        if not mine:
            toggle.set_sensitive(False)
        for member in data["capacity"]:
            person = project.member(member.get("member", ""))
            name = "You" if me is not None and member.get("member") == me.id else (
                person.first_name if person else member.get("member", ""))
            for item in member.get("connections") or []:
                fraction = item.get("percent_left")
                lines = c.vbox(spacing=4, hexpand=True)
                lines.append(c.hbox(text(f"{name} · {item.get('label')}", "pgs-cap-name", wrap=False, hexpand=True),
                                    text("Team tasks" if item.get("team_tasks") else f"Just {name.lower() if name == 'You' else name}",
                                         "pgs-cap-share", wrap=False)))
                lines.append(c.meter((fraction or 0) / 100, label=f"{name}: {item.get('summary')}"))
                lines.append(text(item.get("summary") or "", "pgs-cap-left"))
                face = c.shared.person_face(person.name if person else name, 26)
                face.set_valign(Gtk.Align.START)
                line = c.hbox(face, lines, spacing=10, css="pgs-row")
                rows.append(line)
        box.append(c.ListGroup(*rows))
        return box

    def share_mine(self, project, on: bool) -> None:
        usage = c.module("core.usage")
        me = project.owner
        if usage is None or me is None or self.ctx.project_dir is None:
            return
        rows = [{"connection_id": x.id, "label": x.label, "provider": x.provider,
                 "team_tasks": x.work_on_team_tasks,
                 "plan": (getattr(x, "extra", None) or {}).get("plan", "") if isinstance(getattr(x, "extra", None), dict) else "",
                 "team_task_percent": x.team_task_cap if x.work_on_team_tasks else None}
                for x in (self.data or {}).get("mine", []) if x.is_personal]
        project_dir = self.ctx.project_dir
        c.run_async(lambda: usage.share_capacity(project_dir, me.id, rows, opt_in=on),
                    lambda _r: self.load(), lambda error: self.say("error", str(error)))

    def month(self, project, data: dict) -> Gtk.Widget:
        box = c.vbox()
        box.append(section(f"This month on {project.name}"))
        month = data["month"]
        rows = []
        for line in month["lines"]:
            conn = next((x for x in data["mine"] if x.id == line["connection"]), None)
            rows.append(Row(line["label"], line["note"] or None, lead=c.mark(conn, size=34) if conn else "wallet",
                            end=text(line["amount_text"], "pgs-row-end", "strong", wrap=False)))
        if rows:
            rows.append(Row("Total so far", "Personal plans aren’t counted here. They’re paid for by their owners.",
                            lead="wallet", end=text(c.money(month["total"]), "pgs-row-end", "strong", "total",
                                                    wrap=False)))
        else:
            rows.append(Row("Nothing spent yet", "Shared keys and Ari credits show here as they’re used. Personal "
                                                 "plans aren’t money here; their owners pay for them.", lead="wallet"))
        box.append(c.ListGroup(*rows))
        return box

    def recent(self, data: dict) -> Gtk.Widget:
        box = c.vbox()
        records = data["month"]["runs"]
        if not records:
            return box
        box.append(section("Recent runs", note="Every run says whose model it used."))
        connections = {x.id: x for x in data["mine"]}
        rows = []
        for record in records[:6]:
            conn = connections.get(record.get("connection", ""))
            title = data["goals"].get(record.get("goal", ""), "") or "A conversation"
            cost = c.money(record["cost"]) if "cost" in record else ""
            tokens = int(record.get("tokens_in", 0)) + int(record.get("tokens_out", 0))
            amount = cost or (f"{tokens // 1000}k tokens" if tokens >= 1000 else (f"{tokens} tokens" if tokens else ""))
            rows.append(Row(title, record.get("runs_on") or (conn.run_label() if conn else ""),
                            lead=c.mark(conn or {"provider": "ari"}, size=34),
                            end=text(amount, "pgs-row-end", wrap=False)))
        box.append(c.ListGroup(*rows))
        return box

    # ── one connection ──

    def open_detail(self, conn) -> None:
        self.detail = conn
        self.sheet.push(self.build_detail(conn), conn.label, " · ".join(filter(None, [
            {"personal-plan": "Personal plan", "api-key": "API key", "team-key": "Team key", "local": "Local model",
             "ari": "Luma’s agent"}.get(conn.kind, ""), (conn.extra or {}).get("plan", "")])))

    def build_detail(self, conn) -> Gtk.Widget:
        data = self.data or {}
        room = data.get("rooms", {}).get(conn.id) or {}
        overview = data.get("overviews", {}).get(conn.id) or {}
        body = c.vbox()
        if self.message is not None:
            kind, words = self.message
            self.message = None
            body.append(callout(c.escape(words), icon="circle-check" if kind == "ok" else "triangle-alert",
                                tone="good" if kind == "ok" else "warn"))
        hero = c.hbox(c.mark(conn, size=44), c.vbox(text(conn.label, "pgs-conn-hero-name", wrap=False),
                                                     text(describe(conn), "pgs-conn-hero-sub"), spacing=2,
                                                     valign=Gtk.Align.CENTER, hexpand=True), spacing=14)
        hero.set_margin_top(4)
        hero.set_margin_bottom(14)
        body.append(hero)
        headline = room.get("text") or room.get("quiet") or "No reading yet"
        lines = c.vbox(text(headline, "pgs-headroom-title"), spacing=6, hexpand=True)
        if room.get("fraction") is not None:
            lines.append(c.meter(room["fraction"], label=headline))
        cap_reason = overview.get("cap_reason")
        if conn.spends_money and cap_reason:
            lines.append(text(cap_reason, "pgs-row-sub"))
        elif room.get("fraction") is None:
            lines.append(text("Playground shows this when the agent or its program reports it. It never guesses.",
                              "pgs-row-sub"))
        body.append(c.ListGroup(c.hbox(c.lead_tile("gauge"), lines, spacing=12, css="pgs-row")))

        body.append(section("Who can use it"))
        options = c.vbox()
        if conn.is_personal:
            owner_rule = {"claude": "Anthropic’s", "codex": "OpenAI’s"}.get(conn.provider, "the provider’s")
            options.append(option("Just you", f"Personal plans can’t be shared with other people. That’s "
                                              f"{owner_rule} rule, and it keeps your account safe.",
                                  c.glyph("lock", 18, "pgs-opt-lock")))
            team = option("Work on team tasks",
                          f"Your agent picks up work given to you, or from the shared queue, on your plan and your "
                          "computer. You see everything it does and can stop it any time.",
                          c.switch(conn.work_on_team_tasks, label="Work on team tasks",
                                   on_toggle=lambda on: self.change(conn, work_on_team_tasks=on)))
            if conn.work_on_team_tasks:
                team.get_first_child().append(self.cap_slider(conn))
            options.append(team)
        else:
            shareable = conn.kind != "team-key"
            share = c.switch(conn.sharing == "team", label="Share with the team", sensitive=shareable,
                             on_toggle=lambda on: self.change(conn, sharing="team" if on else "just-me"))
            words = {"team-key": "A team key is for the team. Anyone on the project can pick it, and every run says "
                                 "it used the team key.",
                     "local": "Local models are yours to share. Teammates’ conversations can use it when it runs on the "
                              "Dev server.",
                     "ari": "Every run is labelled with who started it."}.get(
                conn.kind, "Anyone on the project can pick it, and every run says it used your key.")
            options.append(option("Share with the team", words, share))
            if conn.spends_money:
                entry = Gtk.Entry(text=conn.spend_cap, placeholder_text="$25 a month", width_chars=12,
                                  valign=Gtk.Align.CENTER)
                entry.update_property([Gtk.AccessibleProperty.LABEL], ["Spending cap"])
                entry.connect("activate", lambda e: self.change(conn, spend_cap=e.get_text().strip()))
                field = c.vbox(entry, css="pgs-field", valign=Gtk.Align.CENTER)
                options.append(option("Spending cap", "Agents stop and ask before going over. Shared keys need one.",
                                      field))
            owner = self.member_name(conn.owner)
            options.append(option("Owner", f"{owner}. Only they can change the key or the cap." if owner != "You"
                                  else "You.", None))
        body.append(c.ListGroup(options))

        if getattr(conn, "is_acp", False):
            on, shared = picker.fenced(conn, self.ctx.project_dir)
            sub = ("It can only change files in this project, and can’t read the rest of your home folder. "
                   + ("On for shared projects." if shared and conn.sandbox in ("", "on") else "Recommended."))
            fence = c.switch(on, label="Keep it inside the project",
                             on_toggle=lambda value: picker.set_fence(conn, value, lambda _p: self.load()))
            body.append(section("Safety"))
            body.append(c.ListGroup(option("Keep it inside the project", sub, fence)))

        body.append(section("Where it runs"))
        icon, where = picker.where_words(conn, self.ctx.project)
        if conn.where == "this-computer":
            sub = "Runs here. It pauses when this computer sleeps."
        elif conn.is_personal:
            sub = "On the server it’s still your private seat: your sign-in is encrypted to you, and it only works " \
                  "on your goals."
        else:
            sub = "On the server, so teammates’ conversations can use it while laptops are closed."
        servers = self.servers()
        if conn.kind == "ari" and conn.endpoint in ("luma-credits", "chatgpt"):
            end = None
        elif conn.where != "this-computer":
            end = c.button("Bring it back here", icon="monitor", style="fill", small=True,
                           on_click=lambda: self.change(conn, where="this-computer", private_seat=False))
        elif servers:
            target = servers[0]
            end = c.button("Move to the server", icon="server", style="fill", small=True,
                           tooltip=f"Run it on {target.name}",
                           on_click=lambda: self.change(conn, where=target.id, private_seat=conn.is_personal))
        else:
            end = None
            sub += " Add a development server in Places to move it there."
        body.append(c.ListGroup(option(where, sub, end)))
        if conn.kind == "local":
            note = callout("A model this size does real work in <b>Private mode</b>, with at least a 64k memory for "
                           "the conversation. When a job is too big for it, it hands up to Ari and tells you.",
                           icon="sparkle", tone="info")
            note.set_margin_top(10)
            body.append(note)
        remove = c.button(f"Remove {conn.label}", icon="trash-2", style="plain", small=True,
                          on_click=lambda: self.remove(conn))
        remove.add_css_class("pgs-danger-link")
        remove.set_halign(Gtk.Align.START)
        remove.set_margin_top(18)
        body.append(remove)
        return body

    def cap_slider(self, conn) -> Gtk.Widget:
        connections = c.module("agents.connections")
        low, high = connections.TEAM_TASK_CAP_MIN, connections.TEAM_TASK_CAP_MAX
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, low, high, 1)
        scale.set_value(conn.team_task_cap)
        scale.set_hexpand(True)
        scale.set_draw_value(False)
        scale.add_css_class("pgs-slider")
        scale.update_property([Gtk.AccessibleProperty.LABEL], ["Most of my week for team tasks"])
        value = text(f"{conn.team_task_cap}%", "pgs-slider-value", wrap=False)
        timer = [0]

        def moved(_scale) -> None:
            percent = int(round(scale.get_value()))
            value.set_label(f"{percent}%")
            if timer[0]:
                c.GLib.source_remove(timer[0])
            # Save once the slider rests, not on every step of a drag.
            timer[0] = c.GLib.timeout_add(450, lambda: (timer.__setitem__(0, 0),
                                                         self.change(conn, team_task_cap=percent, quiet=True),
                                                         False)[2])

        scale.connect("value-changed", moved)
        return c.hbox(text("Up to", wrap=False), scale, value, text("of my week", wrap=False), spacing=12,
                      css="pgs-slider-row")

    def servers(self) -> list:
        project = self.ctx.project
        if project is None:
            return []
        return [p for p in project.places if p.kind == "ssh" and p.role == "development"]

    def member_name(self, member_id: str) -> str:
        project = self.ctx.project
        if project is None or not member_id:
            return "You"
        if project.owner is not None and project.owner.id == member_id:
            return "You"
        member = project.member(member_id)
        return member.name if member else member_id

    # ── changing a connection ──

    def change(self, conn, quiet: bool = False, **changes: Any) -> None:
        connections = c.module("agents.connections")
        usage = c.module("core.usage")

        def write():
            store = connections.ConnectionStore()
            current = store.get(conn.id)
            if current is None:
                raise ValueError("That connection isn’t here any more.")
            for key, value in changes.items():
                setattr(current, key, value)
            saved = store.update(current)
            if usage is not None and ("work_on_team_tasks" in changes or "team_task_cap" in changes):
                if saved.work_on_team_tasks:
                    usage.set_cap(saved.id, team_task_percent=saved.team_task_cap)
                else:
                    usage.set_cap(saved.id, clear=("team_task_percent",))
            return saved

        def done(saved) -> None:
            self.detail = saved
            if not quiet:
                c.toast(self.sheet.card, f"Saved {saved.label}", kind="saved")
            self.load()

        def failed(error) -> None:
            # The connection rules speak in plain words (a personal plan is never shared, a shared key needs a cap).
            self.message = ("error", str(error))
            self.load()

        c.run_async(write, done, failed)

    def remove(self, conn) -> None:
        connections = c.module("agents.connections")

        def go(_checked: bool = False) -> None:
            def write() -> None:
                connections.ConnectionStore().remove(conn.id)

            def done(_r) -> None:
                self.sheet.pop()
                c.toast(self.sheet.card, f"Removed {conn.label}", kind="deleted")
                self.load()

            c.run_async(write, done, lambda error: self.say("error", str(error)))

        if c.kit is not None:
            c.kit.DestructiveDialog.ask(self.sheet.card, title=f"Remove {conn.label}?",
                                        body="Conversations that use it ask you to pick another agent. Its sign-in stays in "
                                             "its own program.", action="Remove", on_confirm=go)
        else:
            go()


def option(title: str, sub: str, end: Gtk.Widget | None) -> Gtk.Widget:
    """One setting (v73 .pgopt2): what it is, what it means, and its control."""
    words = c.vbox(text(title, "pgs-opt-title"), text(sub, "pgs-opt-sub"), spacing=2, hexpand=True)
    line = c.hbox(words, end, spacing=12, css="pgs-opt")
    if end is not None:
        end.set_valign(Gtk.Align.CENTER)
    return line


def describe(conn) -> str:
    plan = (conn.extra or {}).get("plan", "")
    name = {"claude": "Claude", "codex": "Codex", "gemini": "Gemini", "opencode": "OpenCode", "goose": "goose",
            "pi": "Pi"}.get(conn.provider, conn.provider)
    if conn.kind == "personal-plan":
        return f"Your {plan or 'plan'}, signed in through {name}’s own program"
    if conn.kind == "team-key":
        return "An API key for the team, kept in the keyring of the computer it runs on"
    if conn.kind == "api-key":
        return "Your API key, kept in this computer’s keyring"
    if conn.kind == "local":
        return f"{conn.model or 'A model'} through Ollama, on this computer"
    return {"luma-credits": "Paid with Luma credits", "chatgpt": "Paid with your ChatGPT plan (Sign in with ChatGPT)",
            "ollama": "On a local model through Ollama", "openai-compatible": "On your API key"}.get(
        conn.endpoint, "Luma’s own agent")
