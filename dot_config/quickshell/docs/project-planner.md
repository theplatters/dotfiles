# Project planner

Open **Project planner** from the command palette, or use the `projectPlanner`
IPC target/global shortcut. It is manual only: opening it lists existing
markdown pages below `pages/` and selecting a page reads that page, including
open and completed TODOs.

## Project registry (`projects.toml`)

The **Projects** tab lists the TOML registry via `scripts/projects.py`, not
the graph scan. The old all-page scan (`scripts/project_planner.py list`)
still exists and remains the source for the daily agenda
(`scripts/daily_agenda.py`); the planner UI no longer lists it.

Registry location (first non-empty wins):

1. explicit `--projects-file` CLI flag,
2. `QUICKSHELL_PROJECTS_FILE` environment variable,
3. `<repo-root>/projects.toml` (repo root is the parent of `scripts/`).

`list`/`create`/`update`/`remove` need no graph, so project management works
with no graph configured. Only `import-logseq` needs `--graph` (or
`LOGSEQ_GRAPH`/`settings.json`).

Schema (`version = 1`; unknown fields are rejected, never silently
preserved):

```toml
version = 1

[[projects]]
id = "<uuid>"                 # required, stable, unique (canonical lowercase)
name = "..."                  # required, non-blank (max 512 chars)
logseq_path = "pages/X.md"    # optional, omitted when empty
local_folder = "/abs/path"    # optional, absolute or ~/ only, omitted when empty
github_url = "https://github.com/owner/repo"  # optional HTTPS repo URL, omitted when empty
```

A name-only project (all optionals empty) is valid. Empty values are stored
as omitted keys and read back as `""`. Duplicate `id` values and duplicate
non-empty `logseq_path` values are rejected. Parsing uses stdlib `tomllib`,
so Python 3.11+ is required.

CRUD (`scripts/projects.py`):

```sh
python3 scripts/projects.py list
echo '{"name":"Demo"}' | python3 scripts/projects.py create
echo '{"id":"<uuid>","revision":"<rev>","name":"Demo","logseq_path":"","local_folder":"","github_url":""}' | python3 scripts/projects.py update
echo '{"id":"<uuid>","revision":"<rev>"}' | python3 scripts/projects.py remove
python3 scripts/projects.py --graph PATH import-logseq
```

- `create` stdin: `name` plus optional `logseq_path`/`local_folder`/
  `github_url` (unknown keys rejected); the `id` is server-assigned.
- `update` stdin: full replacement — `id`, `revision`, `name`, plus
  optionals; missing optionals clear to `""`. A stale `revision` is
  rejected (reload and retry).
- `remove` stdin: `id` plus `revision`. A stale `revision` is rejected.
- Every list/mutation response carries `projects` (sorted by
  lowercased name), `revision` (SHA-256 of the registry file bytes, or of
  empty input when the file is missing), and `file` (absolute registry
  path). The planner sends the listed `revision` back on update/remove.

Planner **New project** / **Edit details** / **Delete** touch only registry
entries: they never create or delete notes, folders, or repositories (the
Delete dialog states this explicitly). Name is required; note, folder, and
GitHub are optional and independent. Failures preserve every typed field;
only the authoritative backend response mutates the list. A write is never
cancelled on timeout or planner close — closing is blocked while a write is
busy.

Projects without a linked note are fully selectable with metadata visible,
but note-based chat and tasks are disabled (`No linked note for this
project…`); selecting one never launches an agent or reads the graph. Chat,
tasks, the per-note Pi session, and the folder/GitHub tools require a linked
`logseq_path`.

`import-logseq` is the only explicit graph import. It is idempotent: it
scans graph pages via the existing `project_planner` safe readers, adds only
notes not already registered, never updates existing entries, and never
writes to the graph. A note counts as a project when its leading
page-property block declares `status:: project` **or** `type:: project`
(plain or `[[project]]`, case-insensitive); only the leading block counts,
so body/nested `- file::` lines and fenced samples never match. The real
graph marks projects with `type:: project`, so the seed import yields 28
pages including non-active ones. `file::`/`github::`/`url::` values are
unwrapped with the same conservative helper; a malformed folder such as
`file://~Arbeit/...` is skipped (folder left empty) with a `warnings` entry
instead of inventing a location, as are duplicate/relative/non-local
targets. The command reports `imported` plus `warnings`.

Linked-note file tools (`files-list`/`files-read`/`files-git` in
`scripts/project_planner.py`): when the selected note is registered with a
non-empty `local_folder`, that registry folder is resolved strictly with the
full `project_files` safety checks and wins. A corrupt registry or an
unusable registered folder fails closed instead of silently falling back to
a possibly stale `file::` folder. Unregistered notes (or registered entries
without a folder) keep the legacy fallback to the page-level `file::`
property. The helper-side registry override is the
`QUICKSHELL_PROJECTS_FILE` environment variable.

## Desktop project context (deterministic attribution)

Full contract: `docs/desktop-project-context.md`. Work sessions (Phase 4):
`docs/desktop-work-sessions.md`. Session-centric retrieval (Phase 5):
`docs/desktop-history-search.md`.

The live desktop snapshot is attributed to the existing registry only —
no new model, no inference, no writes, no UI-selected fallback. The
overlay is `DesktopContext.project {id, name, matched_by}` with
precedence `file > cwd > git_root > git_remote` (longest
component-boundary folder wins; same-strength ties and conflicting
mapped remotes stay unknown). GitHub remotes accept canonical
HTTPS/SSH/scp (credentials stripped, never persisted); unmapped
upstreams are ignored; discovery is local `git config` only (linked
worktrees supported). The registry subprocess runs only on metadata
change (folder projection TTL 5 s, remotes 30 s + config
  invalidation). History store is schema v4 (schema v3 is migratable to
  v4 only; no v1/v2 migration, no read-only compatibility for older schemas,
  no retroactive reassignment) plus materialized deterministic work sessions
  and session-centric retrieval (`search`, `session-detail`).

- CLI: `current`, `current-project`, `history --project UUID`,
  `last-activity --project UUID`, `resources --project UUID [--limit]`,
  plus work-session queries `current-session`, `sessions [--project
  UUID] [--limit] [--from/--to]`, `last-session --project UUID`,
  `session-resources --session ID [--limit]`, `session-events --session
  ID [--limit]`, plus session-centric retrieval `search` and
  `session-detail` (full contract: `docs/desktop-history-search.md`)
  (`current` is a fresh snapshot + focus recheck, not collector IPC).
- Python: `scripts/desktop_projects.py current-project | todos |
  logseq-context | recent-activity | last-activity | resources |
  current-session | sessions | last-session | session-resources |
  session-events | current-context | search-activity | get-session |
  project-activity`
  (overrides `--projects-file --graph --db --desktop-bin`; reuses the
  `read_page` task parser; a name-only project is valid, has history,
  but returns explicit no-Logseq-linkage).
- Eight coherent read-only desktop tools (`desktop_current_context`,
  `desktop_current_session`, `desktop_search_activity`,
  `desktop_get_session`, `desktop_project_activity` plus the separate
  Logseq views `desktop_project_todos`,
  `desktop_project_logseq_context` and deterministic preview
  `desktop_resume_plan`); existing scopes
  unaffected. Work sessions are deterministic DB-derived activity
  clusters — never Pi chat sessions (the `SessionManager`/
  `desktop-sessions` picker is separate). Build with
  `cargo build --locked --release --manifest-path
  services/agent-orchestrator/Cargo.toml`; nothing auto-installs or
  auto-starts. No LLM/embeddings/screenshots/sync (Resume is a separate
  read-only backend and typed execution: `docs/desktop-resume.md`; integrated
  through the command palette and the read-only Pi plan tool). Retrieval
  consumes sessions as the primary unit with raw events only via explicit
  `session-detail`/`get-session` drill-down, resolves portable resource
  identity against the current registry before acting (never stale absolute
  paths/window IDs/PIDs/workspace IDs), and treats any later LLM summaries
  as derived/versioned — never boundaries — with no new semantic layer.

## Journal tab

The header has a separate **Journal** tab. It is usable even when the graph has
no project pages: selecting it lazily starts one independent, graph-scoped
journal Pi session. The original **Projects** tab remains the default, and its
project selection, workers, drafts, and history are not shared with Journal.
An initial launch or an idle resume may start the worker; a failed worker stays
stopped until **Retry agent** is pressed.
Switching tabs or closing the planner pauses an idle journal worker; a busy,
compacting, stopping, control, session-refresh, session-switch, or approval
state must be stopped or settled before leaving. **Stop** remains available
for cancellation.
Scoped journal history failures offer **Retry history** only for the current
session identity; stale responses do not replace or clear current history
errors, and an accepted empty history still counts as a successful load.

Type rough thoughts into Journal and press **Send** (or Ctrl+Enter). Opening
the planner, changing tabs, restoring a session, and creating a session never
send a prompt or fetch context. Only an explicit Send tells the agent to first
call `logseq_journal_context` with relevant query terms. That read is bounded
and returns the current journal, page names/recent examples, and relevant
matches; the agent uses those results to follow existing links and conventions.
The journal read/write is capped at 128 KiB and the JSON transport at 1 MiB;
recent examples and relevant matches are capped as well rather than exposing
the graph wholesale.
The assistant then proposes normalized blocks and a destination by invoking
`logseq_journal_append` with its date, revision, and text. That tool prepares
the destination and asks for approval on the shared surface using the exact
preview before writing; the assistant must not ask for a separate verbal
approval in chat. The revision is stale-safe, so a changed journal is rejected
instead of silently merged. The displayed “Today’s journal” date is local UI
guidance; the tool is the authority for the append date, including a date
rollover while the tab is open.

Journal thoughts and tool requests are treated as untrusted data in the UI:
chat user text is plain text, assistant replies retain Markdown rendering, and
approval previews are bounded selectable plain text. The journal session has
its own New, Rename, and Restore controls and persistent graph-level scope.

Each project has its own lazily-created Pi session. Selecting a project starts
that session without sending a prompt. A prompt is sent only after the user
presses **Send**; the planner fetches a fresh page first. That fresh page
context is included only with the first user message of the conversation or
session and is JSON-delimited from the explicit request as untrusted data.
Follow-up sends submit only the typed request. The decision derives from the
authoritative current `worker.messages` user history (any `user` role counts,
including older wrapped prompts) once a correlated successful history load
for the current `(sessionFile, messagesGeneration)` is known
(`ScopedAgent.historyLoadedValid`, per worker so startup and background
workers gate independently) plus a transient accepted-turn boundary for the
prompt-to-history race — never a global ever-sent flag — so a new empty
session includes context, a resumed nonempty session does not, and a
rejected first send can retry with context. A Pi-level prompt failure
(bridge ack positive on write, Pi `success:false` later) clears the transient
boundary when history is still authoritatively empty so the retry wraps
again; unrelated failures never drop valid loaded history. While session
history is unknown, loading, or failed the send is blocked with a
history-loading notice instead of guessing empty. A restored empty cache
after `stateUpdated` is therefore not treated as fresh until its queued
`get_messages` succeeds; a delayed or failed history fetch keeps the send
blocked with a retryable history error. Typed drafts and agent history
remain separate for each project while the shell runs. The transcript keeps
the full raw submitted prompt in its model but user rows show only the
original request (malformed or lookalike text is left untouched; the decoder
requires the canonical exact wrapper shape — exact keys only, recomposed
equality — so extra or duplicate keys never decode); wrapped user rows offer
an inline **Inspect prompt** expansion showing the full submitted prompt as
selectable plain text. History stores text only with no provenance, so a
fully canonical user-pasted wrapper is indistinguishable from a generated
one and decodes the same way; this heuristic display limitation is accepted
rather than solved with extra storage. Assistant rendering is unchanged.

## Session controls

A single ~48px session/model toolbar belongs to the selected project. The
elided session name keeps storage status as tooltip/accessibility rather than
a second text line, and a **Session…** button opens a menu with **New
session**, **Rename**, and **Restore session**. It calls only that project's
`ScopedAgent` worker: session controls do not compose a prompt, append page
content, or make a model call. They are disabled (and guarded in the handlers)
while the page is loading or writing, a request/approval is active, or the
worker is busy, compacting, stopping, switching sessions, processing another
control, or is not ready. The menu snapshots owner/path/session at open and
revalidates fail-closed, and closes on project/agent/planner close.

The active name is shown when available; an unnamed active session is shown as
**Unnamed session**, and a project with no active worker is shown as **New
session**. Rename uses a bounded, accessible in-layer editor. Cancel closes
the editor without changing the existing name or the project's unsent draft.
New sessions likewise do not clear the per-project draft. Restore uses Pi's
existing scoped session picker and preserves its select/keyboard/cancel flow.
The picker title comes from the request, and its restore view does not expose
the page context as approval content.

A clickable **model** button in the same toolbar shows the selected project's
authoritative model as `provider/id` (both `id` and `modelId` shapes count;
**Loading models…**, **No model selected**, **Models unavailable**, and **No
model** cover loading/empty/missing states, with the full label as tooltip).
The dropdown lists only valid
available models with the current one marked, scrolls long lists, and stays
keyboard accessible. It reuses the session readiness gates, forwards only the
selected worker's listed entry via `chooseModel`, and revalidates the
project/session owner at activation so a stale popup cannot retarget. It
never sends a prompt, clears drafts/context/history, or optimistically
rewrites the current model; immediate and async rejections surface through
the existing notice/failed path.

Session storage is persistent and isolated by the resolved graph and project
page. The launch wrapper stores projects below
`$XDG_STATE_HOME/quickshell/project-sessions/projects/<sha256(graph\0project)>`;
journals are a sibling `journals/<sha256(graph)>`;
`PI_CODING_AGENT_SESSION_DIR` overrides the base. Restore lists
only sessions for the current graph and project, and startup resumes the
latest saved session for that key by validating that exact saved file and
passing it explicitly rather than relying on an implicit continuation. Sessions
from the old shared pool are not automatically assigned to projects.

The session label tooltip shows **Loading session history…** while identity or history
is unresolved. A settled conversation with no messages is marked **not saved
to Restore yet**; this does not disable New session or Rename. Pi does not
persist an empty session name as a durable saved-session record. The pending
name survives intentional idle pauses in the current shell, but an empty
session name is not guaranteed across a full shell restart. Once a conversation
has messages, its saved session and name persist normally.

If a picker is cancelled, the prior history remains associated with the same
authoritative session. If a restore reaches a different authoritative session
but its history fetch fails, the planner never copies the old conversation
into it; it shows a history-load error and offers a scoped retry. If session
identity is still unknown, it likewise refuses to restore any old history.

Checkboxes use the revision-checked `project_planner.py toggle` operation and
update only after the helper confirms the write. A stale page is rejected and
offers **Reload page**. Pi markdown updates use the scoped project extension
tools `logseq_project_read`/`logseq_project_update` with exact-preview plus
revision recheck and in-layer confirm. Stop an agent or finish its approval
before switching projects or closing the planner.

Below `chatRow.width < 600` the tasks panel collapses to the `tasksOverlay`
overlay, and the transcript uses `conversationStore` + `syncConversation` for
stable scroll; session/tab/close controls use `WidgetButton`/`WidgetIconButton`
with `widgets/icons/*.svg` assets.

Page reads have a bounded UI timeout and can be safely retried. Checkbox writes
are never killed by that timeout because the write may already have committed;
the planner waits for the helper response and applies its authoritative page,
or offers a reload when the result cannot be verified. The helper uses a
bounded advisory graph lock and an exact revision check. This serializes
cooperating writers, but does not serialize editors that ignore the lock; it
is not a total guarantee against hostile concurrent directory relocation or
other filesystem races. Detected unsafe paths and stale revisions are
rejected, but uncooperating writers can still race a final replacement.

The project-agent RPC transport bounds total helper output to 1 MiB and page
content to 128 KiB. Project workers are paused when they are idle and the
planner closes; their session history and drafts remain available, while a
failed worker requires an explicit **Retry agent**. Closing the palette
leaves its worker running instead (pending approvals reopen safely).

The helper can also be used directly:

```sh
python3 scripts/project_planner.py --graph PATH list
python3 scripts/project_planner.py --graph PATH page < page.json
python3 scripts/project_planner.py --graph PATH toggle < toggle.json
python3 scripts/project_planner.py --graph PATH update < update.json
python3 scripts/project_planner.py --graph PATH files-list < page.json
python3 scripts/project_planner.py --graph PATH files-read < read.json
python3 scripts/project_planner.py --graph PATH files-git < page.json
```

`LOGSEQ_GRAPH` selects the graph when `--graph` is omitted, falling back to
`logseqGraph` in `settings.json` (see `settings.example.json`). Project worker
agents receive only their selected graph-relative page through `QS_PROJECT_PATH`.

## Project folder inspection (`file::` property)

A selected project page can declare one folder for read-only inspection with
a single page-level property (first column, before any blocks):

```markdown
file:: /home/user/code/demo

- TODO ship the thing
```

Supported values (after trimming, quote stripping, and one Markdown
`[label](target)` / `<target>` / `[[target]]` unwrap when present):

- absolute paths: `/home/user/code/demo`
- `~` expansion: `~/code/demo`
- graph-relative paths: `projects/demo` (resolved below the Logseq graph)
- local `file://` URLs: `file:///home/user/code/demo`

The folder may live outside the graph. Empty values, duplicate `file::`
lines, and missing/nonexistent/non-directory/sensitive roots are errors.
Only the leading page-property block counts (blank lines and other `key::`
lines may precede it): a `file::` line after the first block, heading,
list, fence, or drawer line — including fenced samples — never grants a
root, and neither do indented or list-item spellings such as
`- file:: /tmp` (block-level properties). A root at or below a secrets
directory (`.ssh`, `.env`/credentials names, …), a `.git` internals
directory, or a protected policy path is refused.

In project mode the agent gets three extra read-only tools, all pinned to
the selected page with the root resolved afresh from the page on every
request (no model-supplied page/root override, no folder writes, no shell):

- `logseq_project_files` lists up to 1000 regular files (`files-list`).
- `logseq_project_read_file` reads one folder-relative UTF-8 text file up
  to 128 KiB, e.g. `{"file": "src/main.py"}` (`files-read`).
- `logseq_project_git` reports scoped status (including untracked names;
  contents via the read tool), `HEAD` diff, and last-commit metadata
  (`files-git`).

Safety and limits: child paths must be normalized folder-relative paths
(no absolute paths, `..`, symlinks, or symlink escapes); the root itself is
acquired by walking its canonical components from `/` with
`O_DIRECTORY`/`O_NOFOLLOW` and revalidating the kernel path of the fd, and
all further traversal is fd-relative from that pinned root fd, so a swapped
ancestor fails instead of redirecting listing or reads. `.git` internals,
`.ssh`/`.gnupg`/`.aws`, `.env`/credentials/secrets names, and repo policy
paths (`.pi` settings at any nesting level, helper scripts,
`ScopedAgent.qml`) are excluded from listing, reads, and diffs. Listings
hold open descriptors only along the current chain, budget directory visits
(4096), and report descriptor exhaustion as explicit truncation, never
silent omission. Status paths are converted from repo-relative to
root-relative, so every reported path works verbatim in the read tool
(rename sources included as `orig`). Git uses fixed argv with no shell, a
sanitized environment (`GIT_CONFIG_NOSYSTEM=1`, global/system config
disabled, `GIT_PAGER=cat`, no terminal prompt, `GIT_NO_LAZY_FETCH=1`),
`--no-optional-locks`, `--no-ext-diff`/`--no-textconv`/`--no-renames`,
submodule recursion off plus `--ignore-submodules=all`, and
`core.fsmonitor=false`; each git child runs in its own process group with
non-blocking pipes under one overall deadline (~8 s inside the 10 s helper
timeout), killing the group on timeout, overflow, or SIGTERM abort (the CLI
then exits nonzero with `operation aborted` instead of orphaning git).
Repository-local config that can execute commands (external diff,
textconv/command drivers including dotted subsections such as
`filter "safe.driver"`, clean/smudge/process filters, merge drivers,
fsmonitor hooks, config includes, custom submodule updates, permissive
`protocol.allow`) refuses the whole request fail-closed before any content
command runs instead of risking execution. The diff is built from an
allowlist of the reported non-excluded paths (literal `:(literal)`
pathspecs, renames as delete+add) plus `:(exclude,literal)` entries for
every changed-but-forbidden path, because a literal pathspec also matches
descendants: without the exclusions, an allowed file replacing a directory
(e.g. `bundle` over `bundle/.env`) would leak the deleted secret. A hostile
writer modifying the repo (including its config) concurrently with a request
is outside the guarantees — same stance as the existing project helper's
graph lock — but every execution vector is additionally neutralized at use
time by flags/environment that outrank repo config, so the worst case is
refusal or stale data, never helper execution.
Output is bounded incrementally (512 KiB per command; diffs byte-budgeted
to 256 KiB including the truncation notice, cut on a UTF-8 boundary) with
explicit truncation notes. Non-repos and unborn repos share the same
response schema (`isRepo`/`unborn` discriminate; `diff` is `""`, `head` is
null). Folder listings, file contents, and diffs are untrusted data: the
QML prompt never embeds the whole folder, the model calls these tools on
demand, and page writes keep their existing exact-preview approval and
fresh-revision semantics.

## Agent orchestrator bridge

Each project worker, the journal worker, and the command palette is a
resident Rust bridge (`services/agent-orchestrator`, binary
`qs-agent-orchestrator`) driven through `widgets/ScopedAgent.qml`. The
bridge owns the Pi child lifecycle, Pi JSONL parsing, RPC request
correlation, and the approval protocol; the TypeScript extension and the
Python helpers still own graph safety and tools. The palette uses
`--mode palette` (direct `pi --mode rpc --approve`, scope vars cleared,
general tool scope intact) versus the scoped project/journal wrappers.

Build the binary manually after every pull — it is git-ignored and never
committed, and QML launches only the built binary with no fallback. A
missing binary reports a build error instead of hanging:

```sh
cargo build --locked --release --manifest-path services/agent-orchestrator/Cargo.toml
```

The launched binary is
`services/agent-orchestrator/target/release/qs-agent-orchestrator`.
Nothing auto-starts: only an explicit `start()` launches the bridge,
and the bridge spawns the Pi child on the `start` op via
`scripts/project_sessions.py --project PAGE …` (journal:
`scripts/journal_sessions.py …`), forwarding `--session`/`--pending-name`/`--new-session`
alongside `--project` (see `pi_child.rs`, `ScopedAgent.qml`). Per scoped worker the bridge stays
resident across idle pauses, and `stopIdle` pauses an idle child while
preserving `sessionFile`. Submitted drafts are kept until the accepted
ack arrives (`opFinished`); rejections and bridge death preserve them.

Stdin takes version-1 JSONL commands (`start`, `stopIdle`, `prompt`,
`abort`, `newSession`, `switchSession`, `rename`, `requestMessages`,
`requestStats`, `respond`, `compact`, `chooseModel`, `shutdown`); stdout emits `update`
envelopes:

```json
{"version": 1, "id": "ui-1", "op": "prompt", "args": {"message": "summarize the page"}}
{"version": 1, "type": "update", "state": {"ready": true, "busy": true}, "events": [], "ack": "ui-1", "accepted": true}
```

Full ops, envelope fields, bounds, and tests are documented in
`services/agent-orchestrator/README.md`. Rust tests:

```sh
cargo test --locked --manifest-path services/agent-orchestrator/Cargo.toml
```


## Project UUID scope + Zotero collection

Project workers are pinned by stable registry UUID (`QS_PROJECT_ID`), not by a frozen note path, and work without Logseq including Zotero-only projects. The current optional note/folder/collection is resolved per operation from a fresh `scripts/projects.py list` read:

- Registry contract (backend worker owns persistence): optional `zotero_collection` null or `{server_id:string, library_type:'user'|'group', library_id:digit string ('0' allowed user bound server), collection_key:8 uppercase alnum, include_subcollections:bool default true}`. Old projects without the field keep working.
- Project form offers picker/status/open/unlink; unknown/offline links are preserved verbatim in edits. Picker covers both My Library and shared/group libraries (enumerated read-only from the local API via `scripts/zotero.py libraries`) and lists the selected library's collections from `scripts/zotero.py collections` in hierarchical order (subcollections nested by `parentCollection`, siblings sorted by name); selection fills server/library/collection for the next save, with manual key entry kept for offline/unknown links. Picking a shared collection records `library_type:"group"` + group id.
- Helper `python3 scripts/zotero.py <command>` with JSON stdin/stdout and structured nonzero failures: `capabilities {}`, `collections {library_type?,library_id?}`, `search {project_id,query?,limit?,start?}`, `item {project_id,item_key}`, `read-pdf {project_id,attachment_key,query?,start_page?,end_page?}`, `prepare {project_id,operation,params} => {prepared,preview}`, `apply {project_id,prepared}` (backend worker implements; integration tests use a fake helper until then).
- Extension tools `zotero_search/item/read_pdf` plus `zotero_prepare/apply` (prepare/ask/apply, cancellation checks, bounded output). Operations: add-item metadata, add-existing, update-item, add/remove membership, create/update subcollection. Project ID comes from the pinned env (fresh registry); palette takes an explicit project ID; journal is denied unless deliberately project-explicit (no broadened default). Allowlist updated; no keys in output/prompts. Mutations show explicit previews with shared-item edit warnings; no library deletion. Citations are on-demand (metadata vs fulltext, no unsolicited whole-library ingestion).
- Sessions: `scripts/project_sessions.py --project-id UUID [--project pages/X.md]` scopes to `.../projects/by-id/<uuid>`; `--project` alone keeps legacy `.../projects/<sha256>` compatibility. `latest_session` never scans another scope; legacy restores only when the page is explicitly declared. Agent cache is UUID-keyed; no-note prompt/send works without a page read; note tools fail clearly without a note; the generic scope block is preserved. New scope vars are cleared across palette/journal.

Integration assumptions/limits (until the backend exists): `scripts/zotero.py` is absent, so the extension fails closed with `zotero helper exited`; tests cover UUID/no-note sessions, migration, scoped access/approval denial, and QML contracts against a fake helper. Final schema alignment will inspect the backend once available.
