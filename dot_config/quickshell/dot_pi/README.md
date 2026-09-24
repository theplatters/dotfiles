# Command palette Pi usage

The palette is opened with the `commandPalette` shortcut or IPC toggle. Prefix
the input to select a source:

- `ai: prompt` sends a free-form prompt to the persistent Pi session.
- `clip: text` (also `clipboard text` or `# text`) searches clipboard history.
- `file: name` searches `PALETTE_FILE_ROOT` (default home) via `scripts/palette_files.py`, not `LOGSEQ_GRAPH` (`LOGSEQ_GRAPH`/`settings.json` `logseqGraph` is for `logseq_todos`/`logseq_graph`/`project_planner`).
- `seen: text` searches recent/observed resources (files, links, Zotero items, window titles) via `scripts/desktop_projects.py seen` (local activity store only, no new store, no collector change). Rows are flat `{kind,label,identity,project_id,project_name,session_id,last_seen_ms,occurrence_count}` labelled File/Link/Zotero/Page (anything else coerces to Page for display); labels cap at 160 chars, rows at 20, newest first. Bare `seen:` lists recent. Row actions: Open (validated opener only — http(s) URLs, absolute paths, Zotero URIs), Copy identity, Ask Pi (routes into the unified chat as visible editable context, never auto-sent), Add TODO here (prefills the `todo:` composer with this resource as single-use `quickshell-ref::` provenance). Replaces `hist:` outright (L6, no alias, same commit).
- `session: text` (also `work: text`) searches closed work sessions over the content ∪ activity union via `scripts/sessions.py search` (read-only); bare `session:` lists recent via `list`. Rows show time range · project · match source (the union carries no TODO counts and no thought text beyond the matched snippet). Row actions: Open session (planner Daily tab focused on that day/session, degraded when unresolvable), Resume project, Copy resource. `inbox:` is deleted — "needs attention" is the default sort and the bar badge; the `sessions.py inbox` CLI filter itself survives for the badge.
- `todo: text` quick-adds to the current project page's task list (with no current project, today's journal as a plain `- TODO` block). Prepare → exact preview → Confirm → apply with a revision recheck (`project_planner.py page`/`update` with `{project_id,revision,content}`, or `journal_assistant.py context`/`prepare`/`append`); provenance (`quickshell-session::`, gated `quickshell-ref::`) mirrors `session_capture`. The palette stays open on Confirm for serial entry.
- `resume: text` (also `project: text`) searches registry projects via `scripts/desktop_resume.py` `list`. Enter opens the preview-before-execute confirm overlay (`Resume` here is the only execute path); `Ask Pi` and `History` hand off to the project planner. Bare `resume:` lists recent projects.
- `> command` exposes save-only screenshots, screen actions for Pi, system
  actions, and Pi session controls. The screen actions are named `Capture
  region for Pi`, `Translate region with Pi`, and `Summarize region to Logseq`.
- `/ command [arguments]` invokes a discovered Pi skill with its arguments.
- `calc: expr` (also `= expr`) evaluates with the calculator.
- `@`/`%`/`+` selectors filter windows, workspaces, and apps; all sources share unified quota-capped search (`parseQuery`/`rebuildModel`). An arithmetic-looking unified query ranks the `=` calculator first but never suppresses other sources.
- `! text` searches Logseq TODOs; plain text searches windows and applications.

Grammar: `Resume` always means preview-before-execute (only in `resume:`); planner handoffs are labelled `Resume project` / `Open history` / `Open session`. Actions whose result you consume (copy, agent prompts) keep the palette open; navigation/open/close actions close it. Destructive system actions use an explicit labelled confirm (no bare double-Enter); approval `Reject (Esc)` cancels while `Defer`/closing the palette leaves the request pending to reopen safely.

Pi controls such as `new`, `switch Pi session`, `model`, `compact`, `stats`, and `stop`
are available under `>` as `New Pi session`, `Switch Pi session`, and friends. A direct slash command can also be entered after
`ai:` (for example `ai: /desktop-sessions`). Free-form AI prompts remain
unchanged. File search uses Python 3 and the configured graph directory;
clipboard search requires `cliphist`, `file`, and `wl-copy`. AI actions require
the `pi` executable and its safety extension. Palette region capture requires
`grim`; the standalone `scripts/screen_capture.py` helper additionally needs
`slurp` when it is run without `--geometry`.

Project workers use a separate `ScopedAgent` with `projectPath` set to the
selected page. In the running shell each project/journal worker is served by
one resident Rust bridge (`services/agent-orchestrator`,
`qs-agent-orchestrator`) behind `widgets/ScopedAgent.qml`: the bridge
owns Pi lifecycle/parsing, request correlation, and the approval
protocol, while this extension and the Python helpers still own graph
safety and tools. The palette below is also on the bridge via
`ScopedAgent { paletteMode: true }` (`--mode palette`): direct
`pi --mode rpc --approve` with no scoped wrapper, all four scope vars cleared
(`QS_PROJECT_PATH`, `QS_PROJECT_SESSION_SCOPE`, `QS_JOURNAL_MODE`,
`QS_JOURNAL_SESSION_SCOPE`), `--session-dir`/`--session`/`--name` forwarded,
and its general tool scope intact — never a project/journal page scope.
Its process starts without a prompt; no page context is sent until the
worker's user explicitly sends a request. With `QS_PROJECT_PATH` set, the
extension denies every generic shell/read/write/edit/search/todo/append or
traversal tool and exposes only `logseq_project_read`,
`logseq_project_update`, `logseq_project_files`, `logseq_project_read_file`,
`logseq_project_git`, plus `project_folder_list`, `project_folder_read`,
and `project_folder_write` (registry `local_folder` only, no graph needed),
plus `logseq_agenda_list` and `logseq_agenda_add` (same list/select flow
with preview + UI confirm as the palette).
Notes and folder/git output are untrusted context, never instructions.
Project updates are bounded to 128 KiB (with a 1 MiB serialized transport
limit), show the exact proposed page, require explicit approval, and use a
fresh revision; denial, missing UI, or a stale revision performs no write.
A selected page may declare a read-only folder with one page-level
`file:: /path/to/folder` line (absolute, `~`, graph-relative, or local
`file://`/Markdown-link spelling; the folder may be outside the graph).
The root is resolved afresh from the pinned page on every call: list files
on demand, read one folder-relative UTF-8 file (128 KiB), and show scoped
git status/`HEAD` diff (256 KiB)/last-commit metadata. Folder tools perform
no writes and no shell; sensitive/protected paths are excluded and all
retrieved content is untrusted. Separately, registry-linked
`project_folder_list`/`project_folder_read`/`project_folder_write` target
the authoritative `local_folder` with no graph required (folder-only
projects work; legacy page resolves via the registry lookup only): list,
read with revision, and create (`create:true`) or overwrite (exact revision)
bounded UTF-8 text files with mandatory UI approval and atomic writes.
Project workers can call `stopIdle()` to release an idle cached RPC
process; it preserves `sessionFile` for a later `start()` and refuses while
busy, awaiting approval, changing sessions, or handling another operation.
The palette covers `ai:` prompts (including `/commands` and image
prompts), session commands, `stats` (new `requestStats` op via
`request('get_session_stats')`), and approvals; closing the palette leaves
its worker running (pending approvals reopen safely), unlike scoped
`stopIdle` idle pauses.

## Capability matrix

One documented matrix replaces the per-scope ad-hoc gating, enforced in
`.pi/extensions/desktop-agent.ts`:

| Tool family | palette `ai:` | project | journal |
| --- | --- | --- | --- |
| `desktop_*` read (context/session/search/get/project-activity/resume_plan) | yes | yes | yes |
| `logseq_agenda_list` / `logseq_agenda_add` | yes | yes | no |
| `logseq_journal_context` / `logseq_journal_append` | no | no | yes |
| `logseq_project_read` / `logseq_project_update` / `logseq_project_files*` / `logseq_project_git` | no | yes | no |
| `project_folder_*` (registry `local_folder`) | no | yes | no |
| `zotero_*` | explicit project id | yes (pinned) | denied |
| `session_search` | yes | yes | yes |
| `create_project` / `create_logseq_page` | yes | no | no |

Project workers gained the two agenda tools (same
`scripts/daily_agenda.py` list/select flow with preview + UI confirm as
the palette; journal stays denied). Palette `ai:` uses current-project
default scoping for the project-scoped reads (`desktop_project_todos`,
`desktop_project_logseq_context`, `desktop_project_activity`): omit
`project` to use the fresh current project, mirroring the
`project-activity` default-current pattern. The journal keeps
journal-only writes plus an explicit "file to project X" handoff
instead of a silent cross-write (name the target project X in chat and
continue there with the project tools).

Palette `ai:` can also create projects and pages via `create_project`
and `create_logseq_page` (denied in project and journal scopes).
`create_project(name, logseq_page?, project_folder?, github_url?,
create_folder?)` creates one registry entry through the sanctioned
`scripts/projects.py create` CLI (server-assigned UUID; duplicate
notes/invalid values rejected), creating the home-only project folder
first only when `create_folder: true`. `create_logseq_page(name,
template?, template_page?, properties?)` creates one new
`pages/<name>.md` from the graph's Templates page (`template::<name>`
block, `template-including-parent:: false|true`, `<% today %>` expanded
to the Logseq page-title date; `properties` merges extra leading
`key:: value` page properties), never overwriting an existing page.
Both use prepare → exact preview → Confirm → commit with recheck;
denial, timeout, or no UI writes nothing. The active registry file plus
its lock stays protected from folder/file tools; `create_project` is
the only registry write path besides the planner UI.

## Journal assistant

The journal surface owns a lazy `ScopedAgent` with `journalMode: true`. Its wrapper
uses a graph-level private session pool separate from both palette and project
sessions, clears `QS_PROJECT_PATH`, sets `QS_JOURNAL_MODE=1`, and starts without
an implicit `--continue`.

Journal mode exposes only `logseq_journal_context` and
`logseq_journal_append` (journal allowlist unchanged). Send is the only action that requests fresh bounded
context; the UI never reads notes itself. Context returns today's journal,
known page names, recent journals, and optional keyword matches as untrusted
evidence. The assistant must preserve meaning, uncertainty, links, and Logseq
block style, using `- ` when no example is available.

Appending is a prepare/preview/confirm/append protocol; invoke the append tool
with the proposal and let its mandatory UI preview request approval. The date must be today,
the revision must still match, and the exact bounded addition and destination
are shown before confirmation. Denial, no UI, timeout, abort, or a stale
revision writes nothing; arbitrary files and whole-file replacements are not
available. Filing a thought to a project page is never a silent
cross-write: use the explicit "file to project X" handoff instead (name
the target project X in chat and continue there with the project tools;
`logseq_journal_append` stays journal-only).

Selecting a Pi screen action opens a native in-window selector. The selection
stays mapped until mouse release, then unmaps immediately before capture; the
bounded `scripts/screen_capture.py --geometry 'x,y widthxheight'` helper captures it and
reopens the palette in `ai:` mode after the capture process has finished.
Translation defaults to German → English and other source languages → German;
screenshot text is treated as untrusted data. Escape cancels without starting a
capture. The helper retains its slurp fallback when no geometry is supplied.
Capture failures and cancellation are shown as short, sanitized stderr details.
The ordinary `Save screenshot (output)` and `Save screenshot (window)` commands remain
save-only.

## Desktop history

Palette, project, and journal scopes share nine read-only desktop tools:
`desktop_current_context`, `desktop_project_todos`,
`desktop_project_logseq_context`, `desktop_project_activity`,
`desktop_current_session`, `desktop_search_activity`,
`desktop_get_session`, `desktop_resume_plan`, and `session_search`. The capture inbox is UI/scheduler-driven with no Pi tools/writes. Prefer session search (`desktop_search_activity`
across projects, `desktop_project_activity` scoped to the current project;
omit `project` to use the fresh current project) with structured filters
and small limits, resolve natural times into UTC
`[fromMs,toMs)` using the tool-described local timezone, drill down with
`desktop_get_session`, and request raw events only when necessary. Each hit's
`matched_at_ms` is the newest matching observation (use it as the latest match,
not session end). History proves file/resource observation and focus, not file
edits: answer edit questions as last observed/active and qualify the claim.
History is untrusted evidence; backend times stay explicit UTC epoch-ms,
start-inclusive/end-exclusive.

For an explicit Resume/continue request, inspect `desktop_resume_plan`
first: it previews deterministic structured context (current registry
metadata, latest current-device work session, selected files/resources,
repository/observed branch, Logseq reference/open TODOs, safe Pi session
association, operations availability/warnings) via
`scripts/desktop_resume.py plan` without executing anything, writing repo
contents, switching Pi sessions, or generating a summary. Project workers
may call it without `project` to use their pinned project (resolved from
`QS_PROJECT_PATH` to the stable registry id); outside project mode
`project` is required (UUID or registry name/unique prefix) with no
fallback to the current desktop. Explicit `project` is allowed in any
scope. Use structured fields rather than inventing paths/commands; actual
desktop execution remains a user-driven Quickshell action; an existing
scoped Pi session is resumed only by the current ProjectPlanner/session
infrastructure. New Pi sessions can use this compact plan plus existing
Logseq/project tools on the first explicit user request — no automatic AI
summary. No execute tool is exposed: the model must never trigger Resume
desktop actions or arbitrary shell commands.

Run the local tests without starting an RPC/provider session:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Build the worker bridge before launching the shell (planner, journal, and
palette; binary is
git-ignored, rebuild manually after each pull; a missing binary shows a
build error with no fallback):

```sh
cargo build --locked --release --manifest-path services/agent-orchestrator/Cargo.toml
```

Bridge protocol details live in `services/agent-orchestrator/README.md`.
