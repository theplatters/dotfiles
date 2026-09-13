# Project planner

Open **Project planner** from the command palette, or use the `projectPlanner`
IPC target/global shortcut. It is manual only: opening it lists existing
markdown pages below `pages/` and selecting a page reads that page, including
open and completed TODOs.

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
presses **Send**; the planner fetches a fresh page first and JSON-delimits the
untrusted page context from the explicit request. Typed drafts and agent
history remain separate for each project while the shell runs.

## Session controls

The session strip belongs to the selected project and offers **New session**,
**Rename**, and **Restore session**. It calls only that project's `ScopedAgent`
worker: session controls do not compose a prompt, append page content, or
make a model call. They are disabled (and guarded in the handlers) while the
page is loading or writing, a request/approval is active, or the worker is
busy, compacting, stopping, switching sessions, processing another control,
or is not ready.

The active name is shown when available; an unnamed active session is shown as
**Unnamed session**, and a project with no active worker is shown as **New
session**. Rename uses a bounded, accessible in-layer editor. Cancel closes
the editor without changing the existing name or the project's unsent draft.
New sessions likewise do not clear the per-project draft. Restore uses Pi's
existing scoped session picker and preserves its select/keyboard/cancel flow.
The picker title comes from the request, and its restore view does not expose
the page context as approval content.

Session storage is persistent and isolated by the resolved graph and project
page. The launch wrapper stores projects below
`$XDG_STATE_HOME/quickshell/project-sessions/projects/<sha256(graph\0project)>`;
journals are a sibling `journals/<sha256(graph)>`;
`PI_CODING_AGENT_SESSION_DIR` overrides the base. Restore lists
only sessions for the current graph and project, and startup resumes the
latest saved session for that key by validating that exact saved file and
passing it explicitly rather than relying on an implicit continuation. Sessions
from the old shared pool are not automatically assigned to projects.

The session strip shows **Loading session history…** while identity or history
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

`LOGSEQ_GRAPH` selects the graph when `--graph` is omitted. Project worker
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
