# quickshell config

Quickshell configuration: `shell.qml` wires the top bar, popouts,
notification server, and shared state. UI components live in `widgets/`,
helpers in `scripts/`, the worker bridge in `services/agent-orchestrator`,
tests in `tests/`, agent policy in `.pi/`, and shared tokens in `theme/Theme.qml`.
The governing principles live in `docs/manifesto.md`.

Run directly with Quickshell (no build step for the shell itself):

```sh
quickshell -c /home/franzs/.config/quickshell
```

## Settings

Per-machine settings live in `settings.json` (git-ignored; copy
`settings.example.json` to get started):

```json
{"logseqGraph": "/home/user/Nextcloud/Documents/Notes"}
```

The Logseq graph is resolved as `--graph` > `LOGSEQ_GRAPH` >
`logseqGraph` in `settings.json` (see
`scripts/quickshell_settings.py`). `QUICKSHELL_SETTINGS` may point at an
alternate settings file. There is no hardwired default path.

Optional `memory` block (see `docs/work-memory.md` for the full example
and defaults) enables the background work→memory scheduler: local
deterministic work-log drafts (`scripts/work_log.py`, auto-generated
per closed session, no network) with exact-preview + Confirm filing to
the project page, plus a "Captured" section listing new session
captures (opt-in `sessionCapture`, "Scan now" via
`memory_tick.py run --job scan`, Accept to the project page with
exact-preview Confirm, Dismiss) plus an evening review / morning plan
(`scripts/daily_review.py`, Review card with Evening/Morning tabs,
deterministic top-3 with Add to tomorrow via the existing agenda
select, Save to journal with exact preview, Refresh; scheduled once per
day after `reviewTime`/`morningTime`). Values are
validated and clamped (`tickSeconds` 10..3600, `minSessionMs`
0..86400000); malformed values fail closed to defaults.
Closed work sessions are also exposed as a local ledger
(`scripts/sessions.py`; sidecar `session_meta`/`session_links`;
`inbox` filter over sessions with pending content; read-only Pi tool
`session_search`; filing still goes through `work_log.py`;
see `docs/sessions.md`). The palette
adds unified `session:` search (content ∪ activity, `inbox:` deleted —
needs-attention is the default sort), `seen:` resource search, and
`todo:` quick-add, with Resume project / Open history / Copy resource /
Open session row actions on sessions and Open / Ask Pi / Copy /
Add TODO here on seen rows. The Daily
planner (CalendarPopout and ProjectPlanner Daily tab) shows a
`Sessions` card for closed sessions with exact-preview filing
to the project page through `work_log.py`. The project overview popup
keeps the change recap plus an explicit Resume button (bounded
`scripts/desktop_resume.py plan` preview, Confirm execute, planner
handoff — never automatic). The bar's
current-project capsule shows a count of today's sessions needing
attention, refreshed on its own slower 60 s cadence while the project state itself arrives on the resident stream.
Restart the shell after changing `memory.enabled`/`tickSeconds` — the
resident scheduler reads them at startup. The `memory` block is
settings-file-only by design — there is no in-shell settings UI; edit
`settings.json` and restart the shell, and a settings surface remains a
roadmap candidate.

## Planner/journal/palette workers

Project planner, journal, and command-palette workers all run through the
resident Rust bridge, `services/agent-orchestrator` (binary
`qs-agent-orchestrator`), driven by `widgets/ScopedAgent.qml`. The
palette uses `--mode palette`: direct `pi --mode rpc --approve` with no
scoped wrapper, scope vars cleared (`QS_PROJECT_PATH`,
`QS_PROJECT_SESSION_SCOPE`, `QS_JOURNAL_MODE`,
`QS_JOURNAL_SESSION_SCOPE`), and its general tool scope intact.
`LOGSEQ_GRAPH`/`PI_CODING_AGENT_SESSION_DIR` are inherited (helpers fall
back to `settings.json` when `LOGSEQ_GRAPH` is unset), and scoped
session dirs are pinned by `QS_*_SESSION_SCOPE` handshakes. The palette
also has `seen:` / `session:` / `todo:` sources (debounced bounded
`desktop_projects.py seen` over the local activity store, unified
`sessions.py search`/`list`, and project-page/journal quick-add with
exact-preview Confirm) with Open / Ask Pi / Copy / Add TODO here and
Resume project / Open history / Copy resource / Open session row
actions. Scoped
project/journal wrappers keep their page/journal scopes. Rust
owns Pi lifecycle/parsing, request correlation, and the approval
protocol; the TypeScript extension and Python helpers still own graph
safety and tools. New op: `requestStats` (palette `stats` via
`request('get_session_stats')`). `ai:` free-form prompts, `/skills`,
image prompts, session commands (`new`, `switch`, `rename`, `model`,
`compact`, `stats`, `stop`), and approvals are covered. Closing the
palette leaves its worker running (reopen shows pending approvals);
closing the planner pauses idle scoped workers via `stopIdle`.
Shared controls are `widgets/WidgetButton.qml` and `widgets/WidgetIconButton.qml`
with `widgets/icons/*.svg` assets.
The planner tasks panel collapses to an overlay below 600px width and
the transcript uses `conversationStore` + `syncConversation` for stable scroll.

Projects tab lists the TOML registry (`scripts/projects.py`, default
`<repo>/projects.toml`, override via `QUICKSHELL_PROJECTS_FILE` or
`--projects-file`): `version = 1` plus `[[projects]]` entries with stable
`id`, required `name`, and optional `logseq_path`/`local_folder`/
`github_url` (Python 3.11+ for stdlib `tomllib`). The planner's **New /
Edit / Delete** manage registry entries only — notes, folders, and repos
are never created or deleted. Palette agents can create projects and pages through the approved tools (`create_project`, `create_logseq_page`): planner New/Edit/Delete and these tools touch registry entries only — never repositories; folder creation is explicit and home-only, and page creation is template-driven and never overwrites. Note-less projects are selectable but show
no note-based TODOs, while the project agent and linked-folder text
file tools work with or without a note. `import-logseq` is the explicit
idempotent graph import (`status:: project` or `type:: project`); the
registered `local_folder` is authoritative for the linked-note file tools,
with legacy `file::` fallback for unregistered notes. The tray
active-project popup shows the current project with its open TODOs, a
bounded recent work-session total (approximate activity, not a precise
timer or billable time), an automatic change recap of the last work session
(repository changes only, never app/window switching; session-start HEAD
baseline plus latest observed changes including dirty state as evidence,
cached per project/work-session, honestly unavailable for old sessions
without a baseline or no repo, latest observed state rather than an exact
end-of-session snapshot; generated by a separate ephemeral tool-disabled
model call with no project conversation history, so recap turns never
pollute the persistent project chat; new capture needs a manual Rust
rebuild and collector restart), and an
agent textbox using the same project agent as the full planner with its
own draft, plus a
full-planner link and approval route, and an explicit **Resume** button
(bounded `scripts/desktop_resume.py plan` preview including unavailable
operations, Confirm, then `execute` and planner handoff — never
automatic); with no current project it falls
back to a plain project list. A linked `local_folder` allows scoped text
file list/read/create/update with revision checks and approval and no
arbitrary shell. Details in
`docs/project-planner.md`.

Rust/Cargo is a build prerequisite. The release binary is git-ignored
and never committed — rebuild it manually after every pull before
launching Quickshell. QML launches only the built binary with no
fallback; a missing binary reports a build error.

```sh
cargo build --locked --release --manifest-path services/agent-orchestrator/Cargo.toml
```

Docs:

- `docs/manifesto.md` — the five governing principles (project workflow,
  AI service, action routing, unified feel, speed) and what they mean in
  practice.
- `services/agent-orchestrator/README.md` — build, CLI, stdin/update
  protocol, bounds, tests.
- `docs/project-planner.md` — planner/journal behavior, project registry,
  overview "Session log" card and Resume button.
- `docs/work-memory.md` — work→memory plumbing + work-log drafts (sidecar schema,
  scheduler/CLI contracts, draft/save/polish flow, gating/caps, privacy,
  failure behavior).
- `docs/sessions.md` — sessions (`scripts/sessions.py` CLI, sidecar `session_meta`/`session_links`,
  planner Session card, palette `seen:`/`session:`/`todo:`
  modes (`hist:` removed, `inbox:` prefix deleted while the `inbox` CLI
  filter survives for the badge), bar badge (sessions needing attention on
  the current-project capsule), inbox as a filter over the same list
  (sessions with pending content), session links
  (`capture`/`draft`/`agent_session`/`continued_from`),
  Pi `session_search` tool, explicit user-run `prune-legacy` (never
  automatic)).
- `docs/roadmap.md` — unscheduled ideas (Jev background notes plus a
  reopened, opt-in Jev advisory batch — query pre-filter/context
  loading, routing, capture screening, review scoring, notification
  triage, approval annotation, Zotero screening — the Phase 2a
  integration direction stays removed;
  desktop surfaces: clipboard, stats, screenshot OCR, agent
  activity/approvals, polkit agent; integration/polish/productivity
  batch).
- `docs/streamlining.md` — active coherence workstream: the
  report → fix loop, the finding ledger, the decision log.
- `.pi/README.md` — palette, skills, and scoped-tool policy.

## Tests

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```
