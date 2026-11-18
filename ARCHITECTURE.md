# Playground: architecture and module contracts

Playground is Luma's agentic development environment. This file is the
**engineering contract**: every module below has one owner, a fixed interface,
and tests.

App id `org.projectluma.Playground`. The code is Python 3.11+ (Luma 44 ships
3.14), PyGObject, GTK 4, libadwaita and `luma_appkit` / LumaUI. Previews use
WebKitGTK 6 on Luma. No third-party Python packages are needed at runtime,
except `python3-cryptography` for the vault (an RPM `Requires`). Everything
else is the standard library.

## Layout

```
src/luma-playground/
  luma-app.toml
  data/                      desktop, metainfo, service, launcher, icon, playground.css, symbolic icons
  bin/playground-mcp         launcher for the MCP server (agents spawn this)
  luma_playground/
    __main__.py              python3 -m luma_playground
    core/                    NO GTK IMPORTS. Pure Python, unit-tested.
      paths.py               projects root, project dir helpers
      model.py               dataclasses: Project, Goal, Story, Check, Decision, Activity, Place, Member, Connection
      project.py             create/open/list projects; folder scaffolding; AGENTS.md template
      store.py               read/write .playground/* (goals, decisions, knowledge, activity, threads, brief)
      events.py              in-process event bus (subscribe/publish), thread-safe; UI marshals to GLib
      watch.py               polling watcher for .playground/ changes made by agents (no GTK; UI may use Gio)
      preview.py             detect how to run the app (package.json scripts, etc.), start/stop dev server, find port
      checks.py              run a goal's checks (commands + preview probes) and record Proof
      places.py              places (this computer, ssh servers), roles, connection tests
      security.py            device key, project SSH CA, short-lived certs, vault (age-style, via python3-cryptography)
      usage.py               per-connection usage tracking, caps, headroom snapshots
    agents/                  NO GTK IMPORTS.
      acp.py                 Agent Client Protocol client (JSON-RPC 2.0 over stdio, newline-delimited)
      registry.py            ACP registry fetch/cache (cdn.agentclientprotocol.com/registry/v1/latest/registry.json)
      connections.py         Connection profiles (kind, owner, sharing, where), per-account config homes
      ari.py                 Ari: Luma's own agent loop over an OpenAI-compatible Chat Completions endpoint
      pi.py                  Pi, driven in-process over Pi's own --mode rpc JSONL protocol (no ACP shim)
      models.py              early model lists: finds an available_models hook on the connection's agent module
      model_cache.py         shared per-account cache for those lists (12-hour TTL)
      runner.py              GoalRunner: drives one goal's conversation with a chosen connection; emits Card events
      handoff.py             builds the Handoff brief when switching agent/account
    mcp/
      server.py              MCP server (stdio). Tools write via core.store, so the app sees changes
    app/                     GTK. Imports core/agents, never the reverse.
      application.py         Adw.Application, actions, startup
      window.py              PlaygroundWindow(AppWindow): rail | conversation | stage
      rail.py, conversation.py, composer.py, stage.py
      cards/                 one module per card type (brief, recipe, plan, step, decision, proof, headsup, review, handoff, cost, message)
      sheets/                picker.py, usage.py, places.py, people.py, book.py, newproject.py, review.py, code.py
      snapshot.py            dev tool: PLAYGROUND_SNAPSHOT=<png> renders the window to a PNG and quits
  tests/                     unittest. `python3 -m unittest discover -s tests` must pass with no display.
```

## The project folder (the source of truth)

`~/Documents/Playground Projects/<Name>/` contains:

```
AGENTS.md                 rules for any agent (template in core/project.py)
CLAUDE.md                 "@AGENTS.md"
.mcp.json                 {"mcpServers":{"playground":{"command":"<abs path to playground-mcp>","args":["--project","<abs project dir>"]}}}
.gitignore                includes .playground/local/
.playground/
  project.toml            [project] name, id (uuid4), created, home ("this-computer" | place id), recipe summary
                          [[places]] id, name, role ("development"|"production"|"preview"), kind ("local"|"ssh"), host, user, port
                          [[members]] id, name, role ("owner"|"builder"|"reviewer"|"viewer"), connect_id
                          No secrets ever.
  brief.md                front matter (title, audience, platforms) + body
  goals/<goal-id>.md      front matter: id, title, status, owner, connection, created, updated, size, cost_estimate
                          body sections: ## Outcome, ## Stories (each story "- [ ] ..." with nested "  - check: ..." lines), ## Notes
  decisions.md            append-only list of "### <title>" blocks with date, who, chosen, why
  knowledge/<slug>.md     free markdown, front matter: title, added_by, updated
  activity.jsonl          one JSON per line: {ts, actor, kind, goal?, summary, data?}
  threads/<goal-id>.jsonl Playground's own conversation record: {ts, role, kind, card?, text?, data?}
  pending/<id>.json       a decision an agent is waiting on (see MCP decision.ask)
  answers/<id>.json       the person's answer
  local/                  git-ignored: caches, preview logs, snapshots, recordings
```

Goal ids are short slugs plus a 4-character suffix (`add-waitlist-7f3a`). Statuses
are `working`, `needs-you`, `ready`, `live`, `paused` or `done`. All writes are atomic
(write a temp file, then `os.replace`). activity.jsonl and threads are
append-only (open with `"a"`, then flush and fsync).

## core/events.py

```python
bus = EventBus()
token = bus.subscribe(topic: str | None, callback)   # None = all topics
bus.publish(topic: str, **payload)
bus.unsubscribe(token)
```

Topics:
- `project.changed`
- `goal.changed` (goal_id)
- `thread.appended` (goal_id, entry)
- `decision.pending` (project, decision)
- `decision.answered`
- `activity.appended`
- `preview.state` (state, url, error)
- `agent.state` (goal_id, state)
- `usage.changed`
- `places.changed`

Callbacks run on the publisher's thread. The GTK layer wraps them with `GLib.idle_add`.

## Cards (thread entries)

Every agent-visible output becomes a **card entry** in `threads/<goal>.jsonl`:
`{"ts":..., "role":"agent"|"person"|"system", "kind":"<card>", "text":..., "data":{...}}`

| kind | data fields |
|---|---|
| `message` | text (markdown-lite) |
| `brief` | title, audience, platforms[], summary, good_looks_like[] |
| `recipe` | summary, parts:[{label, choice, why}], runs_on |
| `plan` | stories:[{title, checks:[str]}], size ("small"/"medium"/"large"), estimate ({connection, amount_text}) |
| `step` | title, state ("running"/"done"/"failed"), files:[path], elapsed_s, spend_text, tool_calls:[{title, kind, status}] |
| `decision` | id, question, options:[{id, label, consequence}], recommended, default, answered? |
| `proof` | shots:[relpath], recording?, checks:[{text, status ("pass"/"fail"/"pending"), evidence}] |
| `headsup` | text, files:[path], undo:{kind, ref} |
| `review` | changes_for_people[], data_changes[], checks summary, destination place id, ready (bool) |
| `handoff` | from_connection, to_connection, brief (text) |
| `cost` | text |

## agents/acp.py

Client side of the Agent Client Protocol, version 1. It launches an agent subprocess, then runs a reader thread.

```python
class AcpClient:
    def __init__(self, command: list[str], env: dict, cwd: str, client_info=("Playground", VERSION),
                 handlers: AcpHandlers): ...
    def start(self) -> InitializeResult          # initialize; advertises fs.read/write, terminal, auth.terminal=True
    def auth_methods(self) -> list[dict]
    def authenticate(self, method_id: str)
    def new_session(self, cwd: str, mcp_servers: list[dict]) -> str
    def load_session(self, session_id, cwd, mcp_servers) / resume_session(...)   # when capabilities allow
    def list_sessions(self, cwd=None) -> list[dict]
    def prompt(self, session_id, blocks: list[dict]) -> StopReason   # blocks until the turn ends; updates stream via handlers
    def cancel(self, session_id)
    def set_mode(self, session_id, mode_id)
    def close()
class AcpHandlers(Protocol):
    on_update(session_id, update: dict)           # session/update notifications
    request_permission(session_id, tool_call, options) -> dict   # returns {"outcome":{"outcome":"selected","optionId":...}} or cancelled
    read_text_file(path, line, limit) -> str      # constrained to the project dir
    write_text_file(path, content)                # constrained to the project dir
    terminal_*                                    # create/output/wait_for_exit/kill/release, sandboxed to the project dir
```

Terminal auth: when the agent needs auth and offers `type: "terminal"`, Playground opens
the agent's own login in a terminal (Luma Terminal; on the Mac dev harness, a
subprocess with a pty). It never sees a token.

## agents/connections.py

A `Connection` has:
- id, label, kind (`personal-plan` | `api-key` | `local` | `ari` | `team-key`), provider (`claude` | `codex` | `gemini` | `opencode` | `goose` | `pi` | `ollama` | `ari` | ...), owner
- sharing (`just-me` | `team`); `team` is only allowed when kind != `personal-plan`. Enforce this in code and test it.
- where (`this-computer` | place id)
- work_on_team_tasks (bool, personal plans only) plus a cap
- config_home: a per-account directory under `~/.local/share/playground/accounts/<id>/` that becomes `CLAUDE_CONFIG_DIR` or `CODEX_HOME`. This is how two Claude accounts live side by side. Secrets never live in the project.
- launch(): the ACP command and env, taken from the registry entry or built in (pinned): claude → `@agentclientprotocol/claude-agent-acp`, codex → `@agentclientprotocol/codex-acp`, gemini → `@google/gemini-cli --acp`, opencode, goose and pi → their checked binaries on PATH (pi is launched in `--mode rpc` by `agents/pi.py`). npm agents are installed once into `<data dir>/agents/<id>/<version>/` and started with Node; `agents/runtime.py` uses the computer's Node 22+ or downloads Playground's own pinned, sha256-checked Node into `<data dir>/runtime/`. Nobody installs anything by hand. A newer listed version installs in the background while the installed one keeps running.
- accounts (`agents/accounts.py`): every Claude/Codex account on the computer — the normal folder, `CLAUDE_CONFIG_DIR`/`CODEX_HOME` named in launchers, desktop shortcuts or shell settings, folders like `~/.codex-work`, extra desktop-app profiles, folders already in use — plus "Add an account from a folder" for anything hidden elsewhere. Sign-in files are only checked for existence, never read.

Stored in `~/.config/playground/connections.toml`. Per-device, never in the project.

## agents/ari.py

Ari is an agent loop that speaks OpenAI Chat Completions with tools. Endpoints:
- Ollama (`http://localhost:11434/v1`, context forced to ≥ 64k via `options.num_ctx` / `OLLAMA_CONTEXT_LENGTH`)
- an API key (OpenAI-compatible base URL)
- later, Sign in with ChatGPT

Its tools:
- `read_file`, `write_file`, `list_files`, `run_command` (sandboxed to the project, with a timeout)
- the same `playground.*` tools as the MCP server, called in-process

It exposes the same handler surface as an ACP session, so GoalRunner treats Ari and ACP agents identically.

## agents/pi.py

Pi is launched as a subprocess in `--mode rpc`, speaking one JSON object per line on stdin/stdout (split on LF only; `U+2028`/`U+2029` are legal inside strings). `PiRpcConnection` handles framing, string-id correlation and the stderr tail; `PiAgent` translates Pi's session events (`message_update`, `tool_execution_*`, `agent_settled`) into the same ACP `session/update` dicts the card translator uses. `agent_settled` — not `agent_end` — ends a turn, because retries, compaction and steering can follow. One process serves one goal session; its config dir is `<project>/.playground/local/pi/<actor>/`, where the user's Pi credentials are linked in and this goal's Playground MCP entry is written to `mcp.json` (`exposure: direct`, `--approve` trusts the project). Extension dialogs are answered `cancelled`, since approval UI is deliberately not bridged. Like Ari, `PiAgent` has the duck-typed surface GoalRunner expects.

## agents/runner.py

`GoalRunner(project, goal_id, connection, bus)`:
- `send(text, attachments=[])`
- `answer_decision(id, option)`
- `switch(connection)`: produces a Handoff card and reconnects; resumes natively if it's the same provider and account
- `stop()`

It translates ACP `session/update` (`agent_message_chunk`, `tool_call`, `tool_call_update`, `plan`) into cards: agent text becomes `message`, tool calls are grouped into `step` cards, and the agent's `plan` becomes a `plan` card. It also folds in `playground.*` MCP effects (decisions, proofs) via `store` and `watch`.

## Adding an agent harness

There are two shapes. An ACP agent needs no adapter: add its provider to `registry.PROVIDER_TO_REGISTRY` and `BUILTIN`, and `AcpClient` handles the session. If the harness does not speak ACP, add a module under `agents/` that exposes the duck-typed surface `GoalRunner` uses (session open/reopen/close, `prompt`, `cancel`, `config_options`, `set_config_option`, auth methods where applicable) and emits the same ACP `session/update` dicts as `pi.py`. Give `default_agent_factory` in `runner.py` a branch that builds it, apply `sandbox.confine_agent`, and add the provider to `connections.detect` for sign-in detection and to the app's provider names, glyphs and hues.

The model chooser is opt-in and needs no central registry. If the harness can list models before a session starts, add a module-level hook to its agent module:

`available_models(connection, *, offline=False, refresh=False) -> list[dict]`

Return ACP-shaped rows `{value, name, description}`. `models.available()` finds the hook by the connection's provider (or registry agent id), so any registered agent can opt in without editing `models.py`. `offline=True` must never start a program, because the Usage sheet calls it on the UI thread; use `agents/model_cache.py` for the per-account, 12-hour TTL cache. An agent with no hook still gets its model list from ACP `configOptions` once the session is running, which `GoalRunner._settle_options()` applies.

Test a custom adapter with a scripted fake RPC or ACP server, as in `tests/fake_pi_rpc.py`. No network calls in tests.

## mcp/server.py

MCP over stdio, newline-delimited JSON-RPC. It supports both the 2025-11-25 handshake (`initialize`) and the stateless 2026-07-28 style (`server/discover`). Tools are named `playground_<area>_<verb>`, because dots are not allowed in some clients. The 2026 surface is listed in the concept §12; minimum set:

- `goal_list`, `goal_get`, `goal_update` (status, stories, checks), `goal_create`
- `decision_ask` (question, options, recommended): writes `pending/<id>.json`, then **blocks** (polling `answers/<id>.json`, up to 30 minutes, keeping alive with progress notifications) and returns the chosen option
- `decision_record`
- `activity_log`
- `knowledge_add` / `knowledge_search`
- `brief_get` / `brief_update`
- `preview_status` (url, state), `preview_screenshot` (asks the app via `pending/` request files; returns the path)
- `checks_run` (runs the goal's checks via core.checks)
- `proof_attach` (shots, checks)
- `release_propose` (creates a review card; never touches production)
- `places_list` (no secrets)
- `cost_report`

Every tool call is also appended to `activity.jsonl`.

## Rules for every module
- Never read or write outside the project directory, except the per-device config and state dirs above.
- Never store tokens or keys in the project.
- No network calls in tests. Tests use a fake ACP agent (`tests/fake_acp_agent.py`) and a fake OpenAI server (`http.server` on localhost).
- Copy shown to people is plain and warm: no AI-speak, no em dashes.

---

## Addendum (9 Oct 2026): the all-encompassing Project space

Everything about a project lives in one place: its goals, knowledge base and bug reports, and the stack it
runs on (email, storage, databases, hosting) with each service's usage.

**UI decision.** The build window stays focused (goals rail, conversation, Stage). The "Project book" becomes a full **Project space**: a sheet or page over the window with its own section sidebar. Sections:
- Overview (health, what's live, what needs you, spend, recent)
- Goals
- **Bugs**
- **Stack & services**
- **Usage & money**
- **Analytics** (the live app)
- Knowledge
- Decisions
- Places
- People
- History
- Code

Each section is a rich, real view: Helm's breadth, refined.

### New files in `.playground/`

**`stack.toml`: services and dependencies**
- One `[[services]]` table per service, with fields:
  - `id`, `name`, `kind`
  - `provider` (e.g. "Resend", "PostgreSQL", "Cloudflare R2", "AWS", "Stripe", "Luma Connect")
  - `purpose`: plain words, e.g. "Sends booking confirmations and reminders"
  - `used_by`: journeys or parts
  - `environments`: `{ stage = "...", live = "..." }` (descriptions or hostnames, never secrets)
  - `console_url`, `docs_url`, `plan`, `monthly_budget`
  - `secrets`: vault secret names only
  - `usage_source`: adapter id, or "manual"
  - `added_by`, `added`
- `kind` is one of: email, database, storage, hosting, auth, payments, analytics, dns, ai, messaging, other.
- Plus `[[dependencies]]` (name, version, why). These are parsed from `package.json` / `requirements` and refreshed.

**`issues/<n>.md`: bug reports**
- Front matter: `number`, `title`, `severity` (low/medium/high/urgent), `status` (open/fixing/fixed/wont-fix), `reporter`, `goal`, `created`, `updated`, `place` (stage/live).
- Body sections: `## What happened`, `## Expected`, `## Steps`, `## Evidence`.
- Numbers are sequential per project.

**`usage/services.jsonl`: usage snapshots per service**
- Fields: `{ts, service_id, period, metrics: {...}, cost, currency, source}`.
- Numbers only.

**`analytics.jsonl`: live-app analytics snapshots**
- Fields: `{ts, period, visitors, sessions, top_pages[], errors, source}`.

### New MCP tools
- `stack_list`, `stack_add_service`, `stack_update_service`, `stack_remove_service`
- `issue_create`, `issue_update`, `issue_list`
- `usage_record` (service usage numbers)
- `analytics_record`

### Platform
`core/services.py`: read-only usage adapters. Credentials come from the vault, and the adapters never write to providers.
- `cloudflare`: R2 storage and operations via GraphQL
- `aws`: Cost Explorer via the aws CLI if present
- `resend`: emails sent via the API
- `postgres`: database size via `psql`
- `http`: a status-check adapter
- `manual`

Each adapter returns a usage snapshot. A service with no adapter still shows the plan, budget and agent-reported numbers.

### Sample
Kiln's `stack.toml` lists:
- Resend (emails)
- PostgreSQL (bookings and waitlist)
- Cloudflare R2 (studio photos)
- AWS Lightsail (Production)
- Luma Connect (sign-in)
- Twilio (texts; the failing reminder texts link to bug #13)

It has realistic usage and cost snapshots, 4 or 5 bugs (one fixing, linked to the "Fix reminder texts" goal), and a few weeks of analytics.

## Contract additions accepted (9 Oct 2026)
- `.playground/proof/` holds shared proof files. It is tracked, unlike `local/`, which is git-ignored.
- `.playground/local/app.json` is the app heartbeat. While a project is open, the app calls `ProjectStore.write_app_heartbeat()` about every 10 seconds.
- New event topics: `request.pending` (agent asks the app for a screenshot or a checks run; the app replies with `ProjectStore.write_answer`) and `files.changed`.
- Goal front matter gains `kind: chat` (the "Talk to Ari" goal) and `status_note`.
- `ProjectStore.read_thread(goal_id, fold=True)` folds entries that share an id; the last one wins.
- The app's run file is `.playground/run.toml` with an `[app]` table: `command` (may contain `{port}`), `cwd`, `port` (0 means pick a free port), `url_path`, `kind`, `env`, `ready_timeout_s`.
- `GoalRunner.switch(conn)` runs in the background. The handoff arrives as a card and an `agent.state` event.

---

## Addendum (9 Oct 2026): releases to production

The rule: agents never write to production, and only a reviewed release reaches it.
`core/places.py` now does the release itself. Review calls it, History calls the rollback, and
nothing else can.

### `[deploy]` in `.playground/run.toml`

`[deploy]` is the default recipe for every **production** place. `[deploy.places.<place id>]`
overrides it for one place; that is also the only way to give a development or preview place a
recipe. "This computer" never gets one by default.

```toml
[deploy]
kind = "ssh"                   # "ssh" (default for SSH places) | "command" | "vercel" | "netlify" | "cloudflare-pages"
build = "npm run build"        # optional; runs here first, in the project, with a scrubbed environment (no keys, no SSH agent)
source = "dist"                # what to ship, relative to the project (default "."); must stay inside it
path = "/srv/kiln"             # ssh: the folder on the server (default: the place's `path`)
exclude = ["uploads"]          # ssh: never copied, never deleted there. Default: node_modules, .env, .env.*, __pycache__, .DS_Store
                               #      .git, .playground and .mcp.json are always left out
delete = true                  # ssh: remove files on the server that aren't in the release (rsync --delete)
restart = "sudo systemctl reload kiln"   # ssh: run on the server, in `path`, after copying
command = "./scripts/deploy.sh"          # kind "command": run here, in the project
site = "kiln"                  # netlify: the site; cloudflare-pages: the project name (required)
branch = "main"                # cloudflare-pages
health = "/health"             # where to check it's up: a full URL, or a path on the place's `url` (default: the place's `url`,
                               # else the address a hosting CLI printed)
expect_status = 200            # default: any 2xx or 3xx
expect_text = "Kiln"           # optional: must appear in the page
health_timeout_s = 60
timeout_s = 900                # per command
auth = "auto"                  # ssh: "auto" | "certificate" | "agent" (see below)
snapshot = "pg_dump --format=custom kiln"                  # optional: saves the data before every release; stdout is kept
restore_data = "pg_restore --clean --if-exists --dbname=kiln"   # optional: puts a snapshot back (it arrives on stdin)
data_where = "server"          # run snapshot/restore_data on the server over SSH (default) or "local"
env = { NODE_ENV = "production" }   # plain values only; anything that looks like a key is refused
env_secrets = ["VERCEL_TOKEN"]      # vault secret names, given to the deploy/CLI/data commands' environment only

[deploy.places.preview-box]   # an override for one place
kind = "command"
command = "./scripts/preview.sh"
```

Rules the reader enforces, in plain words: `path` must be a folder of its own (never `/`, `~` or a
top-level folder like `/srv`, because `--delete` empties what isn't in the release); letters, digits
and `_ . / -` only; no `..`; `~/x` means `x` in the login's home. Commands are one line.

Kind `command` gets `PLAYGROUND_RELEASE_DIR` (the folder to ship), `PLAYGROUND_COMMIT`,
`PLAYGROUND_RESTORE_POINT`, `PLAYGROUND_LIVE_URL`, `PLAYGROUND_PLACE_{ID,NAME,HOST,USER,PORT,PATH}` and, for
SSH places, `PLAYGROUND_SSH` (a script that runs `ssh` with Playground's options). Hosting CLIs run as
`vercel deploy <source> --prod --yes`, `netlify deploy --prod --dir <source> [--site]` and
`wrangler pages deploy <source> --project-name <site> --branch <branch>`; they use their own sign-in.

**Agreeing to a recipe.** run.toml is in the project, so an agent can edit it. The first release
with a recipe, and the first after anything in it changes (including the contents of a `command`
script inside the project), shows the person what will run and where, and needs their yes. The yes
is a fingerprint kept per device in `<data>/deploy-trust/<project id>.json`, outside the project,
where an agent can't write.

**SSH sign-in (`auth`).** `auto`: this device's Playground key, with the project certificate; when
this device holds the project CA (the owner) and the certificate is missing or about to run out, one
is issued on the spot. Otherwise the person's own keys and SSH agent still work. `certificate`: a
current certificate is required. `agent`: Playground's key and certificate are left out; only the
person's keys and agent are used. Always BatchMode, no passwords, host keys as in SECURITY.md.

### API (`core.places`, appended; nothing existing changed)

```python
release_plan(project_dir, place_id=None) -> {place, recipe, trusted, commands[(where, what)], problem, live_url, kind_words}
release(project_dir, place_id, ticket, goal_id, *, approved_by="", title="", dry_run=False,
        trust_recipe=False, on_step=None, bus=None) -> {ok, restore_point, commit, place, url, snapshot, health, steps, log}
authorize_rollback(project_dir, restore_n, approved_by, place_id=None) -> ReleaseTicket
rollback_plan(project_dir, restore_n, place_id=None) -> {ok, problem, point, place, data, data_saved, trusted, commands}
rollback(project_dir, restore_n, ticket, *, place_id=None, approved_by="", restore_data=False, dry_run=False,
         trust_recipe=False, on_step=None, bus=None) -> {...}
restore_points(project_dir) -> [{number, commit, ts, summary, goal, place, live, code ("copy"|"tag"|""), data}]
read_deploy_recipe(project_dir, place_id=None) -> DeployRecipe | None;  recipe_trusted / trust_recipe
save_restore_point(project_dir, title, store=None) -> {number, commit, tag, reused}
release_lock(project_dir, what) (context manager);  check_health(url, ...) -> {ok, status, message, seconds}
Errors: ReleaseError(step, detail, restore_point, retry) ⊃ ReleaseBusy, RecipeNotTrusted(commands)
```

`release` in order: the recipe is read and agreed (no side effects yet) → the **release lock**
(`.playground/local/release.lock`, flock plus an in-process lock; a second release is refused at
once, never queued, and its ticket isn't spent) → `guard_write` spends the ticket → **"Saving a
restore point"**: `git add -A`, commit "Restore point N: before releasing …", tag
`playground/restore-N` (the same number again when the code hasn't changed since the newest tag,
which is what a retry does; `.playground/` records don't count as code), then the `snapshot` →
**"Copying to <place>"**: build, then rsync (`-rlptz --checksum --delete`, excludes, `-e` Playground's
ssh), then `restart`; a copy of exactly what went out is kept in
`.playground/local/releases/<place>/restore-N/` (the last three) → **"Checking it's up"**: GET the
health address until it answers well or `health_timeout_s` runs out → History, the goal set Live,
a system message in its thread, `release.finished`. Production with no recipe refuses in plain
words. A place that isn't production needs no ticket (this computer: restore point and Live only).
`dry_run` touches nothing: the plan, and for ssh `rsync -n`'s list of what would change.

`rollback(N)` uses the kept copy of N, else exports the `playground/restore-N` tag with
`git archive` into a temporary folder and builds it there (borrowing `node_modules`); the working
copy and git are never touched. Before copying, the data as it is now is saved, so going back can
itself be undone. Data goes back only with `restore_data=True` (the person ticked it).

**Which data belongs to a version.** The snapshot taken at a release (or before going back) is the
data as it was while the *previous* version was live. So going back to N offers the newest snapshot
taken while N was live, that is, the data from the moment N was replaced. `restore_points()` works
this out from History.

### Records

- `activity.jsonl`, kind `release`: `{restore_point, commit, place, state: "live"|"failed", step?, snapshot?, url?, detail}`.
  Kind `restore` (going back): `{went_back_to, commit, place, state, snapshot?, data_restored, detail}`.
  Entries without `state` (older ones, the sample's) count as live.
- `.playground/local/snapshots/*.snapshot` (0600, the newest twelve), `.playground/local/releases/`,
  `.playground/local/logs/release-*.log` (0600, scrubbed of vault values and key-shaped text, the
  newest ten). All local: data copies never sync.
- Bus topics: `release.step` (`project, kind ("release"|"rollback"), place, step, state
  ("running"|"done"|"failed"|"skipped"), label, detail`) and `release.finished` (`project, kind,
  place, ok, restore_point, url?, step?, error?, goal_id?`).

### Known limits

- The lock is per computer. Two teammates releasing from two computers at the same moment aren't
  stopped yet; a lock on the server (or through the Hub) would do it.
- Data copies live on the computer that released. A teammate sees "no copy of the data" for those
  restore points.
- `store.write_run_config` rewrites run.toml with `[app]` only, which would drop `[deploy]`. Nothing
  calls it on an existing project today; it should keep the other tables.

## Addendum (9 Oct 2026, round two): sharing between teammates' computers

Every teammate runs the full app on their own computer, never a reduced web interface. The full design,
in plain words, is `SHARING.md`; this is the contract.

### Module
`core/sync.py` (no GTK). `app/sync_ui.py` (indicator, start/stop). `app/sheets/people.py` (sharing,
invites, approvals, roles, presence, joining).

```python
engine = SyncEngine(project_dir, bus=None, *, interval=4.0, data_dir=None, auto_checkpoint=True)
engine.start() / stop() / sync_now(wait=True) -> status
engine.status() -> {state, text, detail, last_sync, behind, ahead, branch, mode, home, member, role, warning?}
#   state: not-shared | stopped | up-to-date | syncing | offline | access | removed | error | conflict
#   text:  "Up to date", "Syncing", "Offline, will catch up", … ; detail: plain words
engine.set_goal(goal_id)                      # presence
engine.begin_goal(goal_id, switch=True) -> {branch, switched, reason}
engine.checkpoint(message=None, goal_id=None) -> commit | None
engine.release_goal(goal_id, title) -> {commit, branch, pushed}     # raises SyncConflict (plain words)
engine.bring_in_changes() / engine.stage_place() -> place for core.preview
share_project(project_dir, mode="server"|"this-computer"|"folder", place_id=…) -> {mode, home, warning, safety, setup}
home_setup_instructions(project_dir, mode=…, place_id=…) -> {script, command, explanation, steps?}
check_this_computer(project_dir) -> {sshd, trusted, addresses, ready, words}
create_invite(project_dir, role=, member_id=, name=, for_whom=) -> Signed   # .code, .file_text, .file_name, .safety
read_invite(text) / request_to_join(invite, name) / read_join_request(project_dir, text)
approve_join(project_dir, request, role=None) -> Signed (welcome; .extra pushed/note/vault_note)
accept_approval(text, dest_root=None) -> Path (the new clone) / pending_joins() / forget_join(id)
remove_member_access(project_dir, member_id) / set_member_role(project_dir, member_id, role)
sharing_info(project_dir) / read_presence(project_dir) / merge_file(path, base, ours, theirs)
engine_for(project_dir) / start_engine(project_dir) / stop_engine(project_dir) / stop_all()
```

New event topics: `sync.state` (project, state, text, detail, last_sync, behind, ahead, branch, mode,
home, member, role, warning?, changed[]) and `presence.changed` (project, people[]). Both constants
live in core/sync.py (`SYNC_STATE`, `PRESENCE_CHANGED`); core/events.py may adopt them.

### The project folder, when shared
- `.playground/`, `AGENTS.md` and `.mcp.json` are untracked from the code and git-ignored (a commit
  "Share <name>: …" does it). Each computer writes its own `AGENTS.md` and `.mcp.json`.
- `.playground/.gitignore` (local/, presence/, *.tmp) and `.playground/.gitattributes` (merge intent
  for plain git) are part of the records.
- `.playground/presence/<member>.json`: heartbeats `{member, name, device, goal, goal_title, state
  ("here"|"left"), since, ts}`. Never in the records' history; `here` = fresh (150 s) and not left.
- `.playground/local/sync.json`: `{project_id, member}` for this computer.
- `project.toml` gains `[sharing]` (mode, home name, since, by, ca_fingerprint) for display only, and
  members gain `extra.devices` (fingerprints) and `extra.joined`. Nothing in the folder decides where
  a device connects: that is `<data>/sync/<project>/pin.json` (per device, 0600) plus the signed
  access list.

### Git layout on the home and locally
Bare `repo.git` with `refs/heads/main`, `refs/heads/goal/<id>`, `refs/playground/state`,
`refs/playground/presence/<member>` (blobs), `refs/playground/access` (signed), `refs/playground/certs`.
Locally: the same `refs/playground/state|access|certs`, plus `refs/playground/mirror/*` (the home at
the last fetch) and `refs/remotes/playground/*`. Fetch is one wildcard pair; tags don't sync.

### Security (additions to SECURITY.md)
- Sync certificate: principal `pg-<project>-sync`, `-O clear` (no pty, no forwarding),
  `force-command=<home>/gate <member> <device-fingerprint>`, 7 days, renewed by the owner's
  Playground. Issued by `sync.issue_sync_certificate(ca, …)`; it belongs on `ProjectCA`.
- The gate verifies the access list with `ssh-keygen -Y verify` against the root-owned
  `/etc/ssh/playground/projects/<id>/ca.pub`, then runs only `git upload-pack|receive-pack` on this
  project's repository. The pre-receive hook enforces roles per ref.
- Owner-computer homes trust only the sync principal (`home_setup_instructions(mode="this-computer")`
  rewrites core.security's setup script for that), so teammates never get a shell there.
- Invites, requests and welcomes are SSHSIG-signed (CA, device, CA). Host keys are pinned from the
  invite. ssh runs with `-F none`, `IdentitiesOnly`, no agent, strict host keys.
- Paths arriving from the home are validated (no `..`, `.git`, absolute paths, symlinks, local/ or
  presence/), and writes stay inside `.playground/`.

## Addendum (9 Oct 2026, round three): sharing hardening API

Details and reasons are in `SHARING.md` and `SECURITY.md`. This section only lists what other modules may now call.

**Access list.** The format is now `playground-access/2`. Each list expires 72 hours after it is signed. Lists from round two are rebuilt from the CA's records on the computer that shared the project.

**`core.sync`, new or changed:**
- `share_project(..., trust=)`: `trust` is `"live"` (a root-owned sign-in check, which needs sudo) or `"account"` (checks run as the project account, without sudo).
- `home_setup_instructions(..., trust=, home=)`: the result gains `trust` and `words`.
- `remove_device(project_dir, member_id, fingerprint)` and `member_devices(project_dir)`: let a person have a second computer, and remove one.
- `request_renewal` → `read_renewal_request` → `answer_renewal` (on any owner's computer) → `accept_renewal`: the `PGR1`/`PGP1` round trip for a fresh pass.
- `verify_access(ca, project_id, files)`: returns `("ok" | "expired" | "bad", body)`.
- `restore_tag_name(project_dir, number)`: names are `playground/restore/<member>/<n>-<device>`, so teammates' points never clash.
- `expiry_words(moment)`.
- `sharing_info` adds `can_invite`, `trust`, `trust_words`, `pass_expires`, `access_expires` and `issues_passes`.
- The status adds `pass_expires`, `access_expires`, `renew_due` and `renew_words`.

**`core.security`, new:**
- `ProjectCA.issue_sync` and `ProjectCA.revoke_key` (the KRL).
- `sign_certificate`, `shell_options`, `sync_options`, `principal_prefix` and `sync_principal`.
- `Vault.remove_device`.

**Restore points.** These take a restore id (`"<member>/<n>-<device>"`) or, for older entries, a number:
- `places.rollback_plan`
- `places.authorize_rollback`
- `places.rollback`

`restore_points()` gives each point an `id`. The History sheet keys points by `id` and falls back to the number for release entries written before ids existed.

**Still true:** invites and welcomes come only from the computer that shared the project.
