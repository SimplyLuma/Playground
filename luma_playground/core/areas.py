# SPDX-License-Identifier: Apache-2.0
"""The project's areas as agents see them: the short AGENTS.md, the guide, and the indexes.

Playground is a harness. Every agent that works in a project (any program, any
account, any person's computer) keeps the same few areas up to date, as plain
files in ``.playground/``: tasks, bugs (``issues/``), knowledge, history
(``activity.jsonl``) and the stack. People edit the same files through the app.

What an agent reads, cheapest first:

- ``AGENTS.md`` (and ``CLAUDE.md`` / ``GEMINI.md``, which point at it) is loaded
  on every run, so it stays short: what Playground is, the standing rules, and
  what to keep up to date. Rules people keep in Knowledge are copied in here,
  between the ``playground:rules`` markers, because rules must always apply.
- Each area's ``index.md`` is one line per item, written by Playground. Agents
  read these at the start of a conversation instead of every file. That is how
  a bug a person files by hand reaches every agent without waking any of them.
- ``.playground/guide.md`` holds the formats and fields. Agents open it only
  when they need to write something.

Long instruction files make agents slower and costlier (they explore more), so
nothing here describes the codebase; that belongs in knowledge, read on demand.
"""
from __future__ import annotations

import json

import re
import secrets
from pathlib import Path
from typing import Iterable

from .model import (KNOWLEDGE_KIND_WORDS, KNOWLEDGE_KINDS, knowledge_current, knowledge_paths, TASK_STATUS_WORDS, KnowledgeNote, Task,
                    knowledge_kept, knowledge_kind, now_iso, ts_sort_key)

RULES_START = "<!-- playground:rules (kept rules from Knowledge; Playground rewrites this block) -->"
RULES_END = "<!-- /playground:rules -->"
INDEX_NOTE = "Made by Playground from the files in this folder; don't edit it. It can lag a moment: the files are the truth."
TASK_ID_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"   # Crockford base32: no i, l, o, u
CLAIM_HOURS = 2
DONE_IN_INDEX = 10


def render_agents_md(name: str, path: Path | str, summary: str = "", parent: str = "",
                     rules: Iterable[str] = ()) -> str:
    """The start-here file. Loaded on every run by every agent, so every line has to earn its place.
    A sub-project's (found from the folders above it; ``parent`` is kept for callers) names where it sits and
    says it's a focus, not a fence."""
    from . import family
    about = f"\n{summary.strip()}\n" if summary.strip() else ""
    up = family.parent_of(path) if str(path) not in ("", "/p") else None
    above, stay, elsewhere = "", "Stay inside this folder.", ""
    whole = "the whole project"
    if up is not None:
        top = family.root_of(path)
        where = f"**{family.name_of(up)}** (`{family.rel(path, up)}`)"
        if top != up:
            where += f", inside **{family.name_of(top)}** (`{family.rel(path, top)}`)"
        above = (f"This is a sub-project of {where}: a focus, not a fence. This part's own tasks, bugs, knowledge\n"
                 f"and conversations are here, so start from them; read and change anything in {family.name_of(top)} "
                 f"when the\nwork needs it. If an AGENTS.md above this folder loads too, its rules hold here; its "
                 f"Start here doesn't.\n\n")
        stay = f"Stay inside {family.name_of(top)} (`{family.rel(path, top)}`)."
        whole = f"this part of {family.name_of(top)}"
        elsewhere = (f"- **Where it belongs:** a bug in a shared part, a rule for all of {family.name_of(top)}, a task "
                     f"for another part: file it\n  there (`project` on the playground tools takes its name), not "
                     f"here.\n")
    elif parent:
        above = ("This is a sub-project of the project in `../`: a focus, not a fence. Read and change anything there"
                 " when the work needs it.\n\n")
    return f"""# {name} · a Playground project
{about}
This folder is {whole}, kept by Playground so any agent can carry on where the last one stopped:
what it's for, what's happening, who's on what, what was decided and learned. No app is needed to use it.

## Start here
{above}1. Read `.playground/now.md`: what's underway and by whom, what's waiting, what's ready, how to run and check.
2. Take a task from `.playground/tasks/` and read the knowledge it touches (`.playground/knowledge/index.md`;
   parts list their paths). The story of every turn so far is `.playground/journal.md`, for when you need why.
3. Formats for every file: `.playground/guide.md`, read only when you need one.

## Standing rules
- {stay} Never put passwords, keys or tokens in any file here.
- Prove a change works before calling it done (the checks in now.md). Never change production directly.
- Ask before deciding what the app does, costs or does with data: `playground_decision_ask` (the person
  gets buttons), or, without Playground, ask in your reply and stop.
- Install nothing on this computer yourself (no sudo, dnf, rpm-ostree): builds and packages go in the
  project's box (`playground_box_run`); what the computer must have, `playground_install_ask`. No passwords.
- Scratch files and test data go in `.playground/local/`, never loose in the project.
- Speak plainly: many people here don't write code. Act on these instructions; don't recite them.
{rules_block(rules)}
## Keep it true, as you go
With Playground's `playground_*` tools, use them; without, edit the files as the guide says.
- **Tasks:** claim one before you start, listing the paths you'll change (`reserves`). Done: its status,
  a one-line outcome, and a `## Handoff` (what's done, what's next, how to check it).
- **Bugs:** file what you find; mark it fixed when it is. Trouble with Playground itself: say so instead.
- **Knowledge:** a decision, rule, lesson or mistake the next agent needs: one short note. Update a note,
  or replace it (`superseded_by`), rather than repeat it. Keep part notes true when you change their code.
- **Team channel:** read `.playground/channel.md` before a big change; post when you start, finish, or
  must warn others. If your work would clash with someone's, ask the person first.
- **Run and check:** keep `.playground/run.toml` true: `[app]` runs it (Playground previews it and restarts
  it on changes), `[check]` lists the commands that prove it works.
- **Stack and analytics:** an outside service added, changed or dropped: `.playground/stack.toml`. For an
  app people use, offer (ask first) to count visits and errors into `.playground/analytics.jsonl`.
{elsewhere}Never edit `now.md`, `journal.md` or the `index.md` files: Playground writes them from the rest.
"""


def rules_block(rules: Iterable[str]) -> str:
    lines = [f"- {_one_line(rule)}" for rule in rules if _one_line(rule)]
    return RULES_START + "\n" + "".join(line + "\n" for line in lines) + RULES_END + "\n"


def replace_rules(text: str, rules: Iterable[str]) -> str | None:
    """AGENTS.md with its rules block refreshed, or None when the file has no block (left alone)."""
    start, end = text.find(RULES_START), text.find(RULES_END)
    if start < 0 or end < start:
        return None
    return text[:start] + rules_block(rules).rstrip("\n") + text[end + len(RULES_END):]


def kept_rules(notes: Iterable[KnowledgeNote]) -> list[str]:
    """The rules every agent must always see: kept rule notes, oldest first, each as one line."""
    rules = [n for n in notes if knowledge_kind(n) == "rule" and knowledge_current(n)]
    rules.sort(key=lambda n: ts_sort_key(n.extra.get("created") or n.updated))
    return [n.title if not n.body.strip() or n.body.strip() == n.title else f"{n.title}. {_first_line(n.body)}"
            for n in rules]


GUIDE_MD = """# How to keep a Playground project up to date

Only read the part you need. Times are ISO 8601 in UTC (`2026-10-12T21:40:00Z`).
`by` and `created_by` name who did it: `$PLAYGROUND_ACTOR`, the id of the agent working here
(also in `PLAYGROUND_ACTOR`), or `person:<name>`. Change only the fields you mean to; keep everything else as it is.
If Playground's `playground_*` tools are available, prefer them: they fill the id in for you.

## Tasks: `.playground/tasks/<id>.md`

New task: a new file. The id is `t-` and five characters from `0-9a-z` without i, l, o, u
(`t-7f3k2`); pick one that isn't taken.

    ---
    id: t-7f3k2
    title: Let parents offer rides to a game
    status: todo            # todo, doing, done or dropped
    priority: normal        # high, normal or low
    assignee: ""            # a person, if one owns it
    created_by: $PLAYGROUND_ACTOR
    created_at: 2026-10-12T21:40:00Z
    updated_by: $PLAYGROUND_ACTOR
    updated_at: 2026-10-12T21:40:00Z
    claimed_by: ""
    claimed_until: ""
    labels: [rides]
    blocked_by: []          # task ids that must be done first
    reserves: []            # the paths you'll change while you hold it: src/rides/, server/rides.js
    ---
    What it's for, in the person's words.

    ## Checks
    - [ ] A parent can offer seats on the game page

Before you work on a task: if `claimed_until` is in the future and `claimed_by` isn't you, leave it.
Otherwise set `status: doing`, `claimed_by: <you>`, `claimed_until` two hours ahead, and `reserves`.
Another task's `reserves` overlapping yours: post on the team channel before you touch those paths.
Done: `status: done`, clear both claim fields and `reserves`, tick the checks you proved, add
`## Outcome` (one to three lines) and a `## Handoff`: what's done, what's next, and the command that
checks it. Stopping before it's done: the same `## Handoff`, and release the claim.

## Bugs: `.playground/issues/<number>.md`

The project's own bugs. A problem with Playground itself isn't one: say it in your reply.
New bug: the next number after the highest file there.

    ---
    title: A reserved seat is lost after refreshing
    severity: high          # urgent, high, medium or low
    status: open            # open, fixing, fixed or wont-fix
    reporter: $PLAYGROUND_ACTOR
    created: 2026-10-12T21:40:00Z
    updated: 2026-10-12T21:40:00Z
    place: live             # where it happened: live, or stage (the preview)
    ---
    ## What happened
    ## Expected
    ## Steps
    ## Evidence

Fixed: `status: fixed`, and add `## Fix` with one line on what changed.

## Knowledge: `.playground/knowledge/<short-name>.md`

One idea per note, a few lines at most, its first sentence saying what it is (the index shows it).
Update an existing note instead of adding a near copy. Never delete one that stopped being true:
replace it with a new note, and set the old one's `status: superseded`, `superseded_by: <new-slug>`.

    ---
    title: Only parents can offer rides
    kind: decision          # purpose, decision, rule, part, lesson, mistake or note
    added_by: $PLAYGROUND_ACTOR
    created: 2026-10-12T21:40:00Z
    updated: 2026-10-12T21:40:00Z
    verified: 2026-10-12    # when someone last checked it's still true
    ---
    Why, in a line or two.

A decision, in a few lines each: `## Context` (what forced it), `## Decision`, `## Alternatives` (what
else was weighed, and why not), `## Consequences` (what it means for the work).

- **decision:** something that was settled, and why.
- **rule:** something every agent must always do or never do ("Never publish internal notes to
  GitHub"). A rule you write needs `status: suggested`; it applies once a person keeps it, and
  then Playground adds it to AGENTS.md. A rule a person tells you to keep: write it with
  `status: kept`.
- **part:** how one part of the code works: what it does, how data goes in and out, what it relies
  on, and how to change it safely. Add `paths: [src/auth/, server/session.js]` and `uses: [postgres]`.
  Read a part's note before changing that part; update it when you change how the part works.
- **lesson:** what to do when something happens again. **mistake:** what went wrong and how to
  avoid it.

## Team channel: `.playground/channel.jsonl`

How agents (and people) working at the same time stay out of each other's way. `channel.md` is
Playground's short digest of the last two days; read that, not the whole log. Add one JSON line
to `channel.jsonl`, never change or remove lines:

    {"ts": "2026-10-12T21:40:00Z", "from": "$PLAYGROUND_ACTOR", "kind": "start", "text": "Moving sign-in to email codes", "task": "t-7f3k2", "files": ["src/auth/"]}

Kinds: `start` (what you're about to change, and roughly which files), `done` (what changed),
`heads-up` (something others must know: a renamed file, a broken build, a changed rule),
`question` (to another agent or a person; use `"to"`), `answer`. One or two short sentences.
Playground posts when a person asks an agent for something, so you'll see those too.

## History: `.playground/activity.jsonl`

Add one JSON line at the end. Never change or remove lines.

    {"ts": "2026-10-12T21:40:00Z", "actor": "$PLAYGROUND_ACTOR", "kind": "note", "summary": "Fixed the lost seat (#14)"}

Kinds: `note`, `code`, `bug`, `task`, `release`, `decision`.

## Stack: `.playground/stack.toml`

Each outside service is a `[[services]]` table: `id`, `name`, `kind` (email, database, storage,
hosting, sign-in, payments, texts…), `purpose`, `secrets` (names only, never values), `console`.
Playground's Stack area shows each one, what it costs and how it's doing; list them all, so the person
sees what the app depends on.

## Analytics: `.playground/analytics.jsonl`

Playground's Analytics area charts how the live app is used. Once an app has real users, offer to
measure it (ask first: it's about their users' data). Prefer the least: counting page views and errors
on the app's own server, no third-party tracking unless the person wants it. Record a period at a time,
one JSON line each, appended (never rewrite old lines):

    {"ts": "2026-10-20T09:00:00Z", "period": "2026-10-19", "visitors": 41, "sessions": 57, "errors": 1, "top_pages": [{"path": "/", "views": 48}], "source": "server logs"}

A small script the app's server runs (or a check you run) can append these; say where it lives in Stack.

## Leave these alone

`threads/`, `pending/`, `answers/` and `goals/` are Playground's record of conversations.
`local/` belongs to this computer. The `index.md` files are made by Playground.

## Preview: `.playground/run.toml`

Playground runs the app and shows it in its Preview, where the person can use it at phone, tablet and
desktop sizes. Nothing is installed on the computer or added to its dock. Write one `[app]` table:

    [app]                              # a website or web app
    kind = "web"
    command = "npm run dev -- --port {port}"     # {port}: Playground picks a free one
    url_path = "/"

    [app]                              # a GTK app (Luma, Linux): shown inside Playground through GTK Broadway
    kind = "gtk"
    command = "python3 src/main.py"   # or a built binary: "build/sticky-notes"
    toolkit = "gtk4"                  # or "gtk3"

    [app]                              # any screen over VNC: a VM, another machine, a headless display
    kind = "vnc"
    label = "Windows 11 test machine"
    command = "qemu-system-x86_64 -m 4G -drive file=.playground/local/win11.qcow2 -vnc 127.0.0.1:1"  # optional
    host = "127.0.0.1"
    screen_port = 5901                # VNC port: 5900 + display number
    password_env = "WIN_VNC_PASSWORD" # optional: the variable that holds the password, never the password

How to choose:
- A website: `web`.
- A GTK or LibAdwaita app: `gtk`. It runs with its own settings folder inside the project and its own
  session bus, so it can't touch the person's desktop. Make it adapt to narrow widths; Preview has phone sizes.
  At those it runs as Luma's phone shell runs it: no window buttons, web views with a phone's user agent,
  Luma's device variables (`LUMA_DEVICE_CLASS=handheld`, `LUMA_FORM_FACTOR=phone`, so LumaUI drops its
  title bar), and `PLAYGROUND_PREVIEW_DEVICE` (phone, tablet, desktop or app) set.
- Another toolkit on Linux (Qt, Electron, SDL): run it on a headless compositor with a VNC output
  (`weston --backend=vnc`, `wayvnc`), and use `vnc`.
- Windows, macOS or another OS: a VM (keep its disk in `.playground/local/`, and say so in Knowledge) or
  one of the person's machines with screen sharing on. Use `vnc`. Ask the person before starting a VM:
  it needs a lot of memory and disk.
- Missing a tool (qemu, weston)? Install it in the project's box, or ask with `playground_install_ask`.
- An app (for computers, phones or TVs): LumaUI on GTK 4, packaged as a Flatpak, unless the person chose
  otherwise. A website is a website. Luma's guides start at https://simplyluma.com/llms.txt (LumaUI, its
  action bar and phone/tablet/TV: https://simplyluma.com/developer/docs/kit): read the
  live copies, they change often.

`playground_preview_status` and `playground_preview_screenshot` show you what the person sees.

And the commands that prove it works, in the same file (now.md lists them, so the next agent runs them first):

    [check]
    commands = ["python3 -m pytest -q", "python3 tests/gtk_smoke.py"]

## Sub-projects

Any folder inside a project with its own `.playground/` is a sub-project (Luma's calculator in
`src/apps/calculator/`): its own tasks, bugs, knowledge and conversations, so work on it stays focused. It
isn't a fence: agents there read and change anything in the whole project. now.md says where each one sits
(its rules from above, what the projects above know about its folder; or, from above, every part and how
busy it is). Put things where they belong: a rule for every part in the top project, a part's own lesson in
the part. The playground tools take `project` (a name, or a folder like `..`) to file in another one. A
part big enough for its own focus: `playground_subproject_create` (ask first); its folder may already
hold its code.

## Made by Playground: read them, never edit them

`now.md` (where things stand), `journal.md` (every turn: asked, done, files, decisions), the `index.md`
files and `channel.md`. They're rewritten from the files above whenever those change.
"""

CHANNEL_KINDS = ("start", "done", "heads-up", "question", "answer", "note")
CHANNEL_DIGEST_HOURS = 48
CHANNEL_DIGEST_LINES = 25


def render_channel_digest(entries: list[dict], now: str | None = None) -> str:
    """The last two days of the team channel, at most 25 lines, oldest first."""
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc).timestamp() - CHANNEL_DIGEST_HOURS * 3600 if now is None else \
        ts_sort_key(now) - CHANNEL_DIGEST_HOURS * 3600
    recent = [e for e in entries if ts_sort_key(e.get("ts")) >= cutoff][-CHANNEL_DIGEST_LINES:]
    lines = ["# Team channel", "", "The last two days, oldest first. Made by Playground from channel.jsonl; "
             "add to that file, not this one.", ""]
    for entry in recent:
        when = str(entry.get("ts") or "")[:16].replace("T", " ")
        bits = [when, str(entry.get("from") or "someone"), str(entry.get("kind") or "note")]
        if entry.get("to"):
            bits[-1] += f" to {entry['to']}"
        text = _one_line(entry.get("text") or "")
        extra = []
        if entry.get("task"):
            extra.append(str(entry["task"]))
        files = entry.get("files")
        if isinstance(files, list) and files:
            extra.append("files: " + ", ".join(str(f) for f in files[:5]))
        lines.append("- " + " · ".join(bits) + f": {text}" + (f" ({'; '.join(extra)})" if extra else ""))
    if not recent:
        lines.append("Quiet: nobody has posted in the last two days.")
    return "\n".join(lines) + "\n"


BRIEF_LINES = 10


def brief_since(store, since: str | None, *, conversation: str = "", actor: str = "", now: str | None = None) -> str:
    """What changed in the project since an agent's last turn, for the top of its next message: other
    conversations' channel posts, tasks and bugs that are new or changed, and rules people kept.
    At most ten lines; "" when nothing changed. With no ``since`` (a first turn), the last day counts."""
    # Times are to the second, so a change in the same second as the last turn counts as new: seeing one twice
    # is harmless, missing one isn't.
    cutoff = ts_sort_key(since) if since else ts_sort_key(now or now_iso()) - 24 * 3600
    lines: list[tuple[float, str]] = []
    for entry in store.read_channel(60):
        stamp = ts_sort_key(entry.get("ts"))
        if stamp < cutoff or (conversation and entry.get("conversation") == conversation) or \
                (actor and entry.get("from") == actor):
            continue
        extra = f" ({entry['task']})" if entry.get("task") else ""
        lines.append((stamp, f"- {entry.get('from') or 'someone'} · {entry.get('kind') or 'note'}: "
                             f"{_one_line(entry.get('text') or '')}{extra}"))
    for task in store.list_tasks():
        stamp = ts_sort_key(task.updated_at)
        if stamp < cutoff or (actor and task.updated_by == actor):
            continue
        holder = task.claim_holder()
        word = "new task" if task.created_at == task.updated_at else f"task now {TASK_STATUS_WORDS[task.status].lower()}"
        lines.append((stamp, f"- {word}: {task.id} {task.title}" + (f" (claimed by {holder})" if holder else "")
                             + (f", by {task.updated_by}" if task.updated_by else "")))
    for issue in store.list_issues():
        stamp = ts_sort_key(issue.updated)
        if stamp < cutoff:
            continue
        word = "new bug" if issue.created == issue.updated else f"bug now {issue.status}"
        lines.append((stamp, f"- {word}: #{issue.number} {issue.title} ({issue.severity})"))
    preview = _preview_change(store, cutoff)
    if preview:
        lines.append(preview)
    for note in store.list_knowledge():
        if knowledge_kind(note) == "rule" and knowledge_current(note) and ts_sort_key(note.updated) >= cutoff:
            lines.append((ts_sort_key(note.updated), f"- new rule, follow it: {note.title}"))
    from . import family
    from .store import ProjectStore
    for above in family.ancestors(store.root):           # a rule made for the whole family holds in its parts
        for note in ProjectStore(above).list_knowledge():
            if knowledge_kind(note) == "rule" and knowledge_current(note) and ts_sort_key(note.updated) >= cutoff:
                lines.append((ts_sort_key(note.updated),
                              f"- new rule in {family.name_of(above)}, follow it here too: {note.title}"))
    if not lines:
        return ""
    lines.sort()
    shown = [text for _stamp, text in lines[-BRIEF_LINES:]]
    more = len(lines) - len(shown)
    head = "(Playground: since your last turn in this project" + (f", {more} older changes not shown" if more else "")
    return head + ":\n" + "\n".join(shown) + "\nCheck these against your work; if something clashes, say so first.)"


def _preview_change(store, cutoff: float) -> tuple[float, str] | None:
    """The preview started or stopped since the last turn: worth knowing before an agent decides it can't look."""
    try:
        state = json.loads(store.paths.preview_state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    stamp = ts_sort_key(state.get("updated"))
    if not isinstance(state, dict) or stamp < cutoff:
        return None
    what = state.get("state") or ""
    if what == "running":
        return stamp, (f"- the preview is running ({_one_line(state.get('label') or 'the app')}): look at it with "
                       "playground_preview_screenshot")
    if what in ("failed", "stopped") and state.get("error"):
        return stamp, f"- the preview stopped: {_one_line(state.get('error'))[:160]}"
    return None


CLAUDE_MD = "@AGENTS.md\n"
GEMINI_MD = "@AGENTS.md\n"


# ── tasks/<id>.md ──

def new_task_id(taken: Iterable[str]) -> str:
    taken = set(taken)
    while True:
        candidate = "t-" + "".join(secrets.choice(TASK_ID_ALPHABET) for _ in range(5))
        if candidate not in taken:
            return candidate


TASK_FIELDS = ("id", "title", "status", "priority", "assignee", "created_by", "created_at", "updated_by",
               "updated_at", "claimed_by", "claimed_until", "conversation", "labels")


def parse_task(task_id: str, text: str) -> Task:
    from .store import parse_front_matter
    fields, body = parse_front_matter(text)
    labels = fields.get("labels") or []
    if isinstance(labels, str):
        labels = [part.strip() for part in labels.split(",") if part.strip()]
    status = str(fields.get("status") or "todo").lower()
    return Task(id=task_id, title=_one_line(str(fields.get("title") or task_id)),
                status=status if status in TASK_STATUS_WORDS else "todo",
                priority=str(fields.get("priority") or "normal").lower(),
                assignee=str(fields.get("assignee") or ""), created_by=str(fields.get("created_by") or ""),
                created_at=str(fields.get("created_at") or ""), updated_by=str(fields.get("updated_by") or ""),
                updated_at=str(fields.get("updated_at") or ""), claimed_by=str(fields.get("claimed_by") or ""),
                claimed_until=str(fields.get("claimed_until") or ""),
                conversation=str(fields.get("conversation") or ""), labels=[str(x) for x in labels],
                body=body.strip(), extra={k: v for k, v in fields.items() if k not in TASK_FIELDS})


def format_task(task: Task) -> str:
    from .store import dump_front_matter
    fields = {"id": task.id, "title": task.title, "status": task.status, "priority": task.priority,
              "assignee": task.assignee, "created_by": task.created_by, "created_at": task.created_at,
              "updated_by": task.updated_by, "updated_at": task.updated_at, "claimed_by": task.claimed_by,
              "claimed_until": task.claimed_until}
    if task.conversation:
        fields["conversation"] = task.conversation
    fields["labels"] = list(task.labels)
    fields.update({k: v for k, v in task.extra.items() if k not in fields})
    return dump_front_matter(fields) + ("\n" + task.body.strip() + "\n" if task.body.strip() else "")


# ── index.md, one line per item ──

def task_list_field(task: Task, name: str) -> list[str]:
    """A list field kept in a task's front matter (blocked_by, reserves), however it was written."""
    value = task.extra.get(name) or []
    if isinstance(value, str):
        value = [v.strip() for v in value.strip("[]").split(",")]
    return [str(v).strip().strip("'\"") for v in value if str(v).strip()]


def task_blockers(task: Task, tasks: list[Task]) -> list[str]:
    """The tasks it waits on that aren't done yet."""
    open_ids = {t.id for t in tasks if t.is_open}
    return [b for b in task_list_field(task, "blocked_by") if b in open_ids]


def ready_tasks(tasks: list[Task], now: str | None = None) -> list[Task]:
    """To do, waiting on nothing, and nobody holds it: what an agent can pick up now, most important first."""
    rank = {"high": 0, "normal": 1, "low": 2}
    return sorted((t for t in tasks if t.status == "todo" and not task_blockers(t, tasks) and not t.claim_holder(now)),
                  key=lambda t: (rank.get(t.priority, 1), ts_sort_key(t.created_at)))


def render_task_index(tasks: list[Task], now: str | None = None) -> str:
    now = now or now_iso()
    rank = {"high": 0, "normal": 1, "low": 2}
    doing = sorted((t for t in tasks if t.status == "doing"), key=lambda t: rank.get(t.priority, 1))
    ready = ready_tasks(tasks, now)
    waiting = [t for t in tasks if t.is_open and t not in doing and t not in ready]
    done = sorted((t for t in tasks if not t.is_open), key=lambda t: -ts_sort_key(t.updated_at))[:DONE_IN_INDEX]
    lines = ["# Tasks", "", INDEX_NOTE, ""]

    def line(task: Task, *more: str) -> str:
        bits = [task.id, task.priority, task.title, *[m for m in more if m]]
        if task.assignee:
            bits.append(f"for {task.assignee}")
        return "- " + " · ".join(bits)
    lines += [f"## Underway ({len(doing)})", ""]
    for task in doing:
        holder = task.claim_holder(now)
        reserves = task_list_field(task, "reserves")
        lines.append(line(task, f"claimed by {holder} until {task.claimed_until}" if holder else "claim ran out",
                          f"reserves {', '.join(reserves)}" if reserves else ""))
    if not doing:
        lines.append("Nothing underway.")
    lines += ["", f"## Ready to pick up ({len(ready)})", ""]
    lines += [line(t) for t in ready] or ["Nothing ready."]
    if waiting:
        lines += ["", f"## Waiting ({len(waiting)})", ""]
        for task in waiting:
            blockers = task_blockers(task, tasks)
            holder = task.claim_holder(now)
            lines.append(line(task, f"blocked by {', '.join(blockers)}" if blockers else "",
                              f"claimed by {holder}" if holder else ""))
    if done:
        lines += ["", f"## Recently finished (last {len(done)})", ""]
        lines += [f"- {t.id} · {TASK_STATUS_WORDS[t.status].lower()} · {t.title}" for t in done]
    return "\n".join(lines) + "\n"


def render_issue_index(issues: list) -> str:
    rank = {"high": 0, "medium": 1, "low": 2}
    open_ = sorted((i for i in issues if not i.is_closed), key=lambda i: (rank.get(i.severity, 1), -i.number))
    closed = sorted((i for i in issues if i.is_closed), key=lambda i: -ts_sort_key(i.updated))[:DONE_IN_INDEX]
    lines = ["# Bugs", "", INDEX_NOTE, "", f"## Open ({len(open_)})", ""]
    lines += [f"- #{i.number} · {i.status} · {i.severity} · {i.title}" for i in open_] or ["Nothing open."]
    if closed:
        lines += ["", f"## Recently closed (last {len(closed)})", ""]
        lines += [f"- #{i.number} · {i.status} · {i.title}" for i in closed]
    return "\n".join(lines) + "\n"


def render_knowledge_index(notes: list[KnowledgeNote]) -> str:
    """One line a note: its file, its title, what it says in a few words (so an agent can tell whether to open
    it), and where in the code it applies. Only current notes: replaced ones stay in the folder, unlisted."""
    lines = ["# Knowledge", "", INDEX_NOTE, "Open a note only when it matters to the work.", ""]
    listed = [n for n in notes if knowledge_current(n) or not knowledge_kept(n)]
    for kind in KNOWLEDGE_KINDS:
        group = [n for n in listed if knowledge_kind(n) == kind]
        if not group:
            continue
        lines += [f"## {KNOWLEDGE_KIND_WORDS[kind]}", ""]
        for note in sorted(group, key=lambda n: n.title.lower()):
            waiting = "" if knowledge_kept(note) else " · suggested, not kept yet"
            where = ", ".join(knowledge_paths(note))
            gist = _gist(note.body)
            lines.append(f"- {note.slug}.md · {note.title}{f': {gist}' if gist else ''}"
                         f"{f' · {where}' if where else ''}{waiting}")
        lines.append("")
    if len(lines) == 5:
        lines.append("Nothing yet.")
    gone = len(notes) - len(listed)
    if gone:
        lines += ["", f"({gone} older note{'s' if gone != 1 else ''} replaced by newer ones: in the folder, not listed.)"]
    return "\n".join(lines).rstrip("\n") + "\n"


def _gist(body: str, limit: int = 90) -> str:
    """The note's first sentence, short: enough to decide whether to open it."""
    text = _one_line(re.sub(r"^#+ .*$", "", body or "", flags=re.M))
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0] if text else ""
    return first if len(first) <= limit else first[:limit - 1].rstrip() + "…"


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _first_line(text: str) -> str:
    return _one_line(text.strip().splitlines()[0]) if text.strip() else ""


# Playground's own spans of AGENTS.md: (from this line, up to and including the line that starts with that).
OWN_SPANS = (("## Standing rules", RULES_START), ("## Keep it true, as you go", "Never edit `now.md`"),
             ("## Start here", "3. Formats for every file"))


OLD_LAYOUT = "## Keep the project up to date, as you go"


def refresh_agents_md(text: str, current: str) -> str:
    """``text`` (a project's AGENTS.md) with Playground's own sections replaced by ``current``'s. A span a
    person has taken out, or whose ends they changed, is left alone. A file in the earlier layout (before
    "Start here" and now.md) becomes the current one, keeping what a person wrote after it."""
    if OLD_LAYOUT in text and "## Start here" not in text:
        lines = text.splitlines(keepends=True)
        last = next((i for i, line in enumerate(lines) if line.startswith("Change only the item")), None)
        added = "".join(lines[last + 1:]).strip() if last is not None else ""
        return current + (f"\n{added}\n" if added else "")
    for start, end in OWN_SPANS:
        old, new = _span(text, start, end), _span(current, start, end)
        if old and new and old != new:
            text = text.replace(old, new, 1)
    return text


def _span(text: str, start: str, end: str) -> str:
    lines = text.splitlines(keepends=True)
    first = next((i for i, line in enumerate(lines) if line.startswith(start)), None)
    if first is None:
        return ""
    last = next((i for i in range(first + 1, len(lines)) if lines[i].lstrip().startswith(end)), None)
    if last is None:
        return ""
    # The kept-rules block's opening line belongs to the rules, not to the span before it.
    stop = last if end == RULES_START else last + 1
    return "".join(lines[first:stop])


# ── journal.md: the project's story, for whoever arrives next ──

JOURNAL_ASK = 160
JOURNAL_DID = 240


def render_journal(store, *, names: dict | None = None) -> str:
    """Every conversation's turns, oldest first: what the person asked, what the agent said it did (the first
    lines of its last reply before the next ask), the files that changed, and the decisions made. Made from
    the threads, so it's complete even when an agent forgot to write anything down."""
    names = dict(names or {})
    lines = ["# Journal", "",
             "How this project got here, oldest first. Made by Playground from the conversations; don't edit it.",
             "New here? Read the last entries, then the knowledge index.", ""]
    entries: list[tuple[str, list[str]]] = []
    try:
        goals = store.list_goals()
    except Exception:  # noqa: BLE001 - a journal is help, never a reason to fail
        goals = []
    for goal in goals:
        try:
            thread = store.read_thread(goal.id)
        except Exception:  # noqa: BLE001
            continue
        turn: dict | None = None

        def close(item: dict | None) -> None:
            if item is None:
                return
            stamp = item["ts"]
            head = f"- {stamp[:10]} {stamp[11:16]} · {goal.title or 'Untitled'}"
            body = [head, f"  - Asked ({item['who']}): {item['ask']}"]
            if item["did"]:
                body.append(f"  - {item['agent'] or 'The agent'}: {item['did']}")
            if item["files"]:
                shown = item["files"][:8]
                body.append("  - Changed: " + ", ".join(shown) + (f" and {len(item['files']) - 8} more"
                                                                    if len(item["files"]) > 8 else ""))
            for decided in item["decided"]:
                body.append(f"  - Decided: {decided}")
            entries.append((stamp, body))

        for entry in thread:
            data = entry.data or {}
            if entry.kind == "message" and entry.role == "person" and (entry.text or "").strip():
                close(turn)
                who = names.get(entry.by, (entry.by or "the person").capitalize())
                turn = {"ts": entry.ts or "", "who": who, "ask": _clip(entry.text, JOURNAL_ASK), "did": "",
                        "agent": "", "files": [], "decided": []}
            elif turn is None:
                continue
            elif entry.kind == "message" and entry.role == "agent" and (entry.text or "").strip():
                turn["did"] = _clip(entry.text, JOURNAL_DID)
                turn["agent"] = names.get(entry.by, entry.by.replace("-", " ").capitalize() if entry.by else "")
            elif entry.kind == "step":
                for item in data.get("files") or []:
                    path = str(item.get("path") if isinstance(item, dict) else item)
                    if path and path not in turn["files"] and not path.startswith(".playground/"):
                        turn["files"].append(path)
            elif entry.kind == "decision" and isinstance(data.get("answered"), dict):
                chosen = data["answered"].get("label") or data["answered"].get("option") or ""
                if chosen:
                    turn["decided"].append(f"{_one_line(data.get('question') or '')} → {chosen}")
        close(turn)
    if not entries:
        return "\n".join(lines + ["Nothing yet."]) + "\n"
    entries.sort(key=lambda item: ts_sort_key(item[0]))
    for _stamp, body in entries:
        lines += body
    return "\n".join(lines) + "\n"


def _clip(text: str, limit: int) -> str:
    words = _one_line(text)
    return words if len(words) <= limit else words[:limit - 1].rstrip() + "…"


# ── now.md: where the project stands, for an agent arriving cold ──

NOW_LIST = 5


def render_now(store, *, now: str | None = None) -> str:
    """The first thing a new agent reads: what the project is for, what's underway (and who has it), what's
    blocked or waiting on a person, what's ready next, the open bugs, what happened lately and what was
    decided, and how to run and check it. Made from the files (never a model's summary), short, and rewritten
    whenever they change, so it's never stale for long."""
    now = now or now_iso()
    lines = ["# Now", "", f"Where this project stands, made by Playground from its files ({now[:16].replace('T', ' ')}). "
             "The files are the truth; this is the map.", ""]

    def safe(read, default):
        try:
            return read()
        except Exception:  # noqa: BLE001 - one unreadable area never hides the rest
            return default
    project = safe(store.read_project, None)
    purpose = [n for n in safe(store.list_knowledge, []) if knowledge_kind(n) == "purpose" and knowledge_current(n)]
    lines += ["## What it's for", ""]
    summary = _one_line(getattr(project, "summary", "") or "")
    lines.append(f"- {summary}" if summary else "- (Not written down yet: a purpose note in knowledge says it.)")
    lines += [f"- {n.title}" for n in purpose[:2]]
    lines += safe(lambda: _part_of(store), [])
    from . import luma
    lines += safe(lambda: luma.now_lines(store.root), [])

    tasks = safe(store.list_tasks, [])
    doing = [t for t in tasks if t.status == "doing"]
    lines += ["", "## Underway", ""]
    for task in doing[:NOW_LIST]:
        holder = task.claim_holder(now)
        reserves = task_list_field(task, "reserves")
        lines.append(f"- {task.id} {task.title} · " + (f"{holder}, until {task.claimed_until[:16].replace('T', ' ')}"
                                                       if holder else "nobody holds it now")
                     + (f" · reserves {', '.join(reserves)}" if reserves else ""))
    if not doing:
        lines.append("- Nothing.")

    pending = [p for p in safe(lambda: store.list_pending(kind="decision"), [])]
    blocked = [t for t in tasks if t.is_open and task_blockers(t, tasks)]
    if pending or blocked:
        lines += ["", "## Waiting", ""]
        lines += [f"- On the person: {_one_line(getattr(p, 'question', ''))}" for p in pending[:NOW_LIST]]
        lines += [f"- {t.id} {t.title} · blocked by {', '.join(task_blockers(t, tasks))}" for t in blocked[:NOW_LIST]]

    ready = ready_tasks(tasks, now)
    lines += ["", "## Ready next", ""]
    lines += [f"- {t.id} {t.title}{' · ' + t.priority if t.priority != 'normal' else ''}" for t in ready[:NOW_LIST]]
    if not ready:
        lines.append("- Nothing ready: ask the person what's next.")

    rank = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
    bugs = sorted((i for i in safe(store.list_issues, []) if not i.is_closed), key=lambda i: rank.get(i.severity, 2))
    if bugs:
        lines += ["", f"## Open bugs ({len(bugs)})", ""]
        lines += [f"- #{i.number} {i.title} · {i.severity}" for i in bugs[:NOW_LIST]]

    lines += safe(lambda: _sub_projects(store, now), [])

    recent = _journal_tail(store, NOW_LIST)
    if recent:
        lines += ["", "## Lately (from journal.md)", ""] + recent

    decided = sorted((n for n in safe(store.list_knowledge, []) if knowledge_kind(n) in ("decision", "lesson", "mistake")
                      and knowledge_current(n)), key=lambda n: -ts_sort_key(n.updated))[:NOW_LIST]
    if decided:
        lines += ["", "## Decided and learned lately", ""]
        lines += [f"- {knowledge_kind(n)}: {n.title} ({n.slug}.md)" for n in decided]

    lines += ["", "## Run and check it", ""] + _run_lines(store)
    return "\n".join(lines) + "\n"


FAMILY_LIST = 12


def _part_of(store) -> list[str]:
    """A sub-project's place in the family: the chain above it, the rules from above that hold here too, what
    the projects above know about this folder, and how busy its parent is. Only what bears on this part."""
    from . import family
    from .store import ProjectStore
    here = store.root
    chain = family.ancestors(here)
    if not chain:
        return []
    up, top = chain[0], chain[-1]
    names = " › ".join([family.name_of(p) for p in reversed(chain)] + [family.name_of(here)])
    lines = ["", f"## Part of {family.name_of(top)}", "",
             f"- {names}. {family.name_of(up)} (`{family.rel(here, up)}`): {_one_line(family.summary_of(up)) or 'no summary yet'}."]
    rules, known = [], []
    for above in reversed(chain):                       # the top project's rules first
        notes = ProjectStore(above).list_knowledge()
        rules += kept_rules(notes)
        inner = family.rel(above, here)
        for note in notes:
            if knowledge_kind(note) == "rule" or not knowledge_current(note):
                continue
            scopes = [p.strip().lstrip("./").rstrip("*") for p in knowledge_paths(note)]
            if any(s and (inner.startswith(s) or s.startswith(inner)) for s in scopes):
                known.append(f"- {family.name_of(above)} knows: {note.title} "
                             f"(`{family.rel(here, above)}.playground/knowledge/{note.slug}.md`)")
    lines += [f"- Rule from above, it holds here too: {_one_line(rule)}" for rule in rules[:FAMILY_LIST]]
    lines += known[:NOW_LIST]
    parent = ProjectStore(up)
    doing = sum(1 for t in parent.list_tasks() if t.status == "doing")
    bugs = sum(1 for i in parent.list_issues() if not i.is_closed)
    lines.append(f"- In {family.name_of(up)}: {doing} underway, {bugs} open bugs. Its own map: "
                 f"`{family.rel(here, up)}.playground/now.md` (or `playground_now` with project \"{family.name_of(up)}\").")
    return lines


def _sub_projects(store, now: str) -> list[str]:
    """The parts inside this project, one line each, so a conversation about the whole sees every part
    without carrying any of them."""
    from . import family
    from .store import ProjectStore
    children = family.children_of(store.root)
    if not children:
        return []
    lines = ["", f"## Sub-projects ({len(children)})", "",
             "Each keeps its own tasks, bugs and knowledge. To work in one, start from its AGENTS.md; to file "
             "something there, give its name as `project`.", ""]
    for child in children[:FAMILY_LIST]:
        part = ProjectStore(child)
        tasks = part.list_tasks()
        doing = sum(1 for t in tasks if t.status == "doing")
        ready = len(ready_tasks(tasks, now))
        bugs = sum(1 for i in part.list_issues() if not i.is_closed)
        counts = ", ".join(f"{n} {word}" for n, word in ((doing, "underway"), (ready, "ready"), (bugs, "open bugs")) if n)
        summary = _one_line(family.summary_of(child))
        lines.append(f"- **{family.name_of(child)}** (`{family.rel(store.root, child)}`)"
                     + (f": {summary[:90]}" if summary else "") + (f" · {counts}" if counts else ""))
    if len(children) > FAMILY_LIST:
        lines.append(f"- and {len(children) - FAMILY_LIST} more (the project list shows them all).")
    return lines


def _journal_tail(store, count: int) -> list[str]:
    """The last few journal entries, one line each: when, what was asked, what was done."""
    try:
        text = store.paths.journal.read_text(encoding="utf-8")
    except OSError:
        return []
    entries: list[list[str]] = []
    for line in text.splitlines():
        if line.startswith("- 20"):
            entries.append([line[2:18]])
        elif entries and line.startswith("  - Asked"):
            entries[-1].append(line.split(": ", 1)[-1])
        elif entries and line.startswith("  - ") and not line.startswith(("  - Changed", "  - Decided")) \
                and len(entries[-1]) == 2:
            entries[-1].append(line.split(": ", 1)[-1])
    out = []
    for entry in entries[-count:]:
        ask = _clip(entry[1], 80) if len(entry) > 1 else ""
        did = _clip(entry[2], 110) if len(entry) > 2 else ""
        out.append(f"- {entry[0]} · asked: {ask}" + (f" → {did}" if did else ""))
    return out


def _run_lines(store) -> list[str]:
    """How to run it (run.toml's [app]) and how to check it ([check] commands), and what the box holds."""
    import tomllib
    try:
        data = tomllib.loads(store.paths.run_toml.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        data = {}
    out = []
    app = data.get("app") or {}
    if app.get("command"):
        out.append(f"- Run ({app.get('kind') or 'web'}): `{app['command']}`" + (f" in {app['cwd']}" if app.get("cwd") else ""))
    for check in (data.get("check") or {}).get("commands") or []:
        out.append(f"- Check: `{check}`")
    try:
        box = tomllib.loads((store.paths.meta / "box.toml").read_text(encoding="utf-8"))
        if box.get("packages"):
            out.append(f"- Builds need (in the project's box): {', '.join(box['packages'])}")
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return out or ["- Not written down yet: say it in `.playground/run.toml` ([app] command, [check] commands)."]
