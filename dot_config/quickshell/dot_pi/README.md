# Command palette Pi usage

The palette is opened with the `commandPalette` shortcut or IPC toggle. Prefix
the input to select a source:

- `ai: prompt` sends a free-form prompt to the persistent Pi session.
- `clip: text` (also `clipboard text` or `# text`) searches clipboard history.
- `file: name` searches `PALETTE_FILE_ROOT` (default home) via `scripts/palette_files.py`, not `LOGSEQ_GRAPH` (`LOGSEQ_GRAPH`/`settings.json` `logseqGraph` is for `logseq_todos`/`logseq_graph`/`project_planner`).
- `> command` exposes save-only screenshots, screen actions for Pi, system
  actions, and Pi session controls. The screen actions are named `Capture
  region for Pi`, `Translate region with Pi`, and `Summarize region to Logseq`.
- `/ command [arguments]` invokes a discovered Pi skill with its arguments.
- `calc: expr` (also `= expr`) evaluates with the calculator.
- `@`/`%`/`+` selectors filter windows, files, and actions; all sources share unified quota-capped search (`parseQuery`/`rebuildModel`).
- `! text` searches Logseq TODOs; plain text searches windows and applications.

Pi controls such as `new`, `switch session`, `model`, `compact`, `stats`, and `stop`
are available under `>`. A direct slash command can also be entered after
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
and `logseq_project_git`. Notes and folder/git output are untrusted context, never instructions.
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
retrieved content is untrusted.
Project workers can call `stopIdle()` to release an idle cached RPC
process; it preserves `sessionFile` for a later `start()` and refuses while
busy, awaiting approval, changing sessions, or handling another operation.
The palette covers `ai:` prompts (including `/commands` and image
prompts), session commands, `stats` (new `requestStats` op via
`request('get_session_stats')`), and approvals; closing the palette leaves
its worker running (pending approvals reopen safely), unlike scoped
`stopIdle` idle pauses.

## Journal assistant

The journal surface owns a lazy `ScopedAgent` with `journalMode: true`. Its wrapper
uses a graph-level private session pool separate from both palette and project
sessions, clears `QS_PROJECT_PATH`, sets `QS_JOURNAL_MODE=1`, and starts without
an implicit `--continue`.

Journal mode exposes only `logseq_journal_context` and
`logseq_journal_append`. Send is the only action that requests fresh bounded
context; the UI never reads notes itself. Context returns today's journal,
known page names, recent journals, and optional keyword matches as untrusted
evidence. The assistant must preserve meaning, uncertainty, links, and Logseq
block style, using `- ` when no example is available.

Appending is a prepare/preview/confirm/append protocol; invoke the append tool
with the proposal and let its mandatory UI preview request approval. The date must be today,
the revision must still match, and the exact bounded addition and destination
are shown before confirmation. Denial, no UI, timeout, abort, or a stale
revision writes nothing; arbitrary files and whole-file replacements are not
available.

Selecting a Pi screen action opens a native in-window selector. The selection
stays mapped until mouse release, then unmaps immediately before capture; the
bounded `scripts/screen_capture.py --geometry 'x,y widthxheight'` helper captures it and
reopens the palette in `ai:` mode after the capture process has finished.
Translation defaults to German → English and other source languages → German;
screenshot text is treated as untrusted data. Escape cancels without starting a
capture. The helper retains its slurp fallback when no geometry is supplied.
Capture failures and cancellation are shown as short, sanitized stderr details.
The ordinary `Screenshot output` and `Screenshot window` commands remain
save-only.

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
