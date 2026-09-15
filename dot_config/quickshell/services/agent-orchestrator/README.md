# qs-agent-orchestrator

Resident Rust bridge behind `widgets/ScopedAgent.qml` for planner/journal
scoped workers and the command-palette worker. Each worker owns one resident bridge process; the
bridge owns the Pi child lifecycle, Pi JSONL parsing, RPC request
correlation, and the approval protocol. QML only projects snapshots,
tracks per-UI ack gates, and forwards signals — no Pi RPC parsing lives
in QML.

Ownership split:

- Rust owns: bridge stdin command handling, Pi child spawn/stop/reap,
  Pi stdout/stderr parsing, RPC id correlation (`qs-<serial>`),
  scoped session/history refresh state machine, streaming
  normalization, approval queue/expiry, bounded emit, and the `update`
  envelope.
- TypeScript (`.pi/extensions/desktop-agent.ts`) still owns graph
  safety and tool policy: scoped allow-lists (`logseq_project_read` /
  `logseq_project_update`, `logseq_journal_context` /
  `logseq_journal_append`), blocking protected paths, and the
  prepare/preview/confirm/append flow.
- Python (`scripts/project_planner.py`, `scripts/project_sessions.py`,
  `scripts/journal_sessions.py`, `scripts/journal_assistant.py`) still
  owns graph reads/writes, revision checks, and the advisory graph lock.
  The lock serializes cooperating writers only; editors that ignore it
  can still race. No stronger guarantee is offered.

The command palette (`widgets/CommandPalette.qml`) uses the same bridge via
`ScopedAgent { paletteMode: true }` (`--mode palette`): direct
`pi --mode rpc --approve` with scope vars cleared and general tool scope
intact, never the scoped wrappers. `ScopedAgent` + bridge is the only worker path.

## Build prerequisite

Rust/Cargo is required. The release binary is git-ignored
(`services/agent-orchestrator/.gitignore` lists `/target`) and is never
committed — rebuild it manually after every pull, before launching
Quickshell. QML launches only the built binary; there is no fallback to
legacy QML orchestration. A missing/unbuilt binary surfaces the exact
build command instead of hanging:

```text
Agent bridge missing — run: cargo build --locked --release --manifest-path services/agent-orchestrator/Cargo.toml
```

From the repo root:

```sh
cargo build --locked --release --manifest-path services/agent-orchestrator/Cargo.toml
```

Expected binary:

```text
services/agent-orchestrator/target/release/qs-agent-orchestrator
```

This is the exact path `ScopedAgent.bridgePath()` launches with
`--root <shell-root> --mode project|journal|palette …`.

## Runtime wiring

One bridge per worker, launched lazily and kept resident across
Pi idle pauses:

- Project worker (`widgets/ProjectPlanner.qml`): `ScopedAgent {}` with
  `projectPath` set to the selected graph-relative page.
- Journal worker (`widgets/JournalAssistant.qml`):
  `ScopedAgent { journalMode: true }`.
- Palette worker (`widgets/CommandPalette.qml`):
  `ScopedAgent { paletteMode: true }` (unscoped general session; closing
  the palette leaves the worker running, unlike scoped `stopIdle` pauses).
- Nothing auto-starts: `Component.onCompleted` never spawns the bridge.
  Only an explicit `start()` launches it (refreshing the spawn identity
  from the latest authoritative session first, then freezing command
  args for that process). `sendOp` never launches — late
  prompt/respond/history calls while the bridge is down simply fail.
- The bridge itself spawns the Pi child only on the `start` op;
  `requestMessages` without a live child is rejected without launching
  one (and QML additionally guards it: live, desired, not
  stopping/refreshing/retryable).
- `stopIdle` pauses an idle Pi child while preserving `sessionFile` for
  a later `start`; the bridge process itself stays resident.
  `shutdown` (sent on QML destruction) runs a supervised teardown.
- QML functions return the UI id (`ui-<serial>`) for completion
  tracking, or a falsy value when locally rejected / not sent.

Pi child commands (see `src/pi_child.rs`, working directory = `--root`):

```sh
# project mode
python3 scripts/project_sessions.py --project <PAGE> [--session FILE] [--pending-name NAME] [--new-session]
# journal mode
python3 scripts/journal_sessions.py [--session FILE] [--pending-name NAME] [--new-session]
```

Environment follows `ScopedAgent`: palette mode clears all four scope vars;
project mode sets `QS_PROJECT_PATH` and clears `QS_JOURNAL_MODE`/
`QS_JOURNAL_SESSION_SCOPE`; journal mode sets `QS_JOURNAL_MODE=1` and an empty
`QS_PROJECT_PATH`, clearing `QS_PROJECT_SESSION_SCOPE`. Each scoped wrapper
then sets its own `QS_*_SESSION_SCOPE`.
`LOGSEQ_GRAPH`/`PI_CODING_AGENT_SESSION_DIR` are inherited (when
`LOGSEQ_GRAPH` is unset, helpers fall back to `logseqGraph` in
`settings.json`).

The palette forwards `--session-dir`/`--session`/`--name`; scoped modes forward
`--session`/`--pending-name`/`--new-session`.

## Protocol (version 1)

CLI (see `src/cli.rs`):

```text
usage: qs-agent-orchestrator --root PATH --mode project|journal|palette [--project PAGE] [--session FILE] [--pending-name NAME] [--new-session]
```

`--project` is required in project mode and rejected in journal/palette
modes. Unknown flags, a missing `--root`/`--mode`, or a non-`project|journal|palette` mode exits non-zero. Palette spawns direct
`pi --mode rpc --approve` (no scoped wrapper, no `--continue`); scope
vars are cleared so its general tool scope stays intact.

Stdin: newline-delimited JSON commands, one object per line, at most
4 MiB per line (larger lines are rejected without writes; a best-effort
`id` hint is recovered so QML pending gates still clear):

```json
{"version": 1, "id": "ui-1", "op": "start", "args": {}}
```

`version` must be `1` or the command is rejected. Supported `op` values
(see `src/main.rs::process_ui`):

| op | args | notes |
| --- | --- | --- |
| `start` | `{}` | spawn/resume Pi child; accepted while an idle stop settles |
| `stopIdle` | `{}` | pause idle Pi child only; rejected while busy, awaiting approval, compacting, stopping, switching, refreshing, or with ack gates pending |
| `prompt` | `{"message": "…", "images": [...]}` | empty message or not-ready agent rejected; `followUp` streaming while busy |
| `abort` | `{}` | cancel queued approvals plus `clear_queue` + `abort` to Pi |
| `newSession` | `{}` | `new_session` RPC + scoped refresh gate |
| `switchSession` | `{}` | prompts Pi with `/desktop-sessions`, then gates on the scoped picker |
| `rename` | `{"name": "…"}` | trimmed, bounded to 120 chars; `set_session_name` RPC |
| `requestMessages` | `{}` | `get_messages` RPC; rejected without a live child, never launches one |
| `requestStats` | `{}` | `get_session_stats` RPC; read-only, palette `request('get_session_stats')` maps here; rejected without a live ready child, never launches one |
| `respond` | `{"requestId": "…", "fields": {...}}` | unknown/stale ids rejected without side effects; approved fields forwarded as `extension_ui_response` |
| `compact` | `{}` | `compact` RPC |
| `chooseModel` | `{"item": {"provider": "…", "modelId": "…"}}` | `id` accepted as alias for `modelId` |
| `shutdown` | `{}` | supervised teardown of the Pi group, ack, then exit |

Stdout: newline-delimited `update` envelopes (see `src/proto.rs`):

```json
{
  "version": 1,
  "type": "update",
  "state": {
    "sessionFile": "",
    "sessionName": "",
    "ready": false,
    "busy": false,
    "status": "Press Retry to start the agent…",
    "answer": "",
    "messages": [],
    "messagesTruncated": false,
    "pendingApproval": null,
    "retryable": true
  },
  "events": [{"name": "textDelta", "args": ["…"]}],
  "ack": "ui-1",
  "accepted": true
}
```

`ack`/`accepted` are present only for direct command replies;
asynchronous Pi updates omit them. `state` is the full snapshot (see
`AgentState::snapshot`: session identity, `ready`/`busy`/`compacting`/
`controlPending`/`sessionSwitching`/`sessionRefreshPending`/
`sessionRefreshFailed`, `pendingApproval`/`pendingRequests`, process
flags, `retryable`, `diagnostic` tail, `statsText`, plus the flags
below). QML maps `events` by name: `textDelta`, `finished`, `failed`,
`uiRequest`, `stateUpdated`, `statsChanged`, `historyFailed`,
`historyLoaded`.

Snapshot truncation/terminal flags (see `src/transport.rs`):

- `messagesTruncated` (snapshot): state-level history hit a display
  budget, so the `messages` array is a partial view, never the full
  session.
- `displayTruncated` (emitted copy only): answer/messages/stats were
  trimmed to fit the emit budget. Authoritative state is untouched.
- `fatal` (emitted copy only): terminal fail-closed signal. The frame
  still carries the full lifecycle/identity snapshot, but the bridge
  latches unusable, tears down, and exits right after emitting, so QML
  takes its dead/retry path (`bridgeDead`) instead of hanging on a
  half-dead session. Triggered by an over-bound ack id or a snapshot
  that cannot fit even after display trims and cancelling approvals.

Completion: QML emits `opFinished(id, op, accepted, message)` per ack —
`accepted` with the `failed`-event message on rejection, and
`opFinished(false)` for every pending op on bridge death (queued
commands are dropped, never replayed), plus `bridgeDead()`. Planner and
journal keep the submitted draft until the accepted ack arrives and only
clear it when still unchanged for the originating worker/project;
rejected acks and bridge death preserve the draft (rename reopens its
dialog on rejection).

## Bounded transport and fail-closed behavior

All bounds live in `src/transport.rs`, `src/state.rs`, `src/main.rs`:

- Framing: 4 MiB per JSONL line in every direction (UI stdin, Pi
  stdout/stderr, Pi stdin, UI stdout), with an 8 KiB stderr line bound.
  Oversize UI lines are rejected without writes; malformed or oversize
  Pi lines terminate the session fail-closed (`…; session terminated
  without replay`) — never a bare `failed` against a live-looking
  session, never a replay.
- First transport failure latches: no further Pi writes, `ready` drops,
  start/control ops fail explicitly (`Pi transport failed; retry`), the
  group is torn down, the failure update is emitted, and the bridge
  exits so gates surface instead of hanging.
- Queues: one shared main-event queue (bound 64). UI senders wait
  finitely (~1 s) then latch termination; Pi protocol frames wait ~1 s
  then latch terminal failure — protocol frames are never silently
  dropped. Only Pi stderr diagnostics may shed under backpressure, and
  the drop count surfaces as a throttled (≥5 s) `failed` notice.
- Writers use nonblocking fds with short deadlines (~300 ms for Pi
  stdin/UI stdout), so Stop, approval expiry, and EOF stay responsive.
- Authoritative state caps: answer 512 KiB; history ≤1000 messages,
  per-message text 64 KiB, aggregate history text 512 KiB
  (oldest-first, sets `messagesTruncated`); diagnostic 2048 chars;
  stats 64 KiB; untrusted metadata budgets (session file 4096 B,
  session name 1024 B, model 16 KiB, commands/models 256 items and
  256 KiB each).
- Emitted-copy budget: 3 MiB target under the 4 MiB framing bound.
  Display trims apply to the copy only (answer 64 KiB tail, messages
  512 KiB oldest-first, stats 16 KiB) and set `displayTruncated`.
  String event args over 64 KiB get a char-safe `… [truncated]` form;
  nested object/array args over 64 KiB serialized become
  `{"truncated": true, "bytes": N}`. `uiRequest` is never truncated:
  exactly one event carrying the final `pendingApproval` verbatim, or
  none.
- Approvals: queue holds at most 32 entries — further and duplicate ids
  are ignored (first wins, so a reused id can never swap content under
  a visible dialog). Expiry is `min(timeout, 120 s)`, serviced every
  second. Pi-provided queue ids over 256 B, and approvals that still
  cannot fit, are cancelled fail-closed (authoritative removal + Pi
  cancel write + `failed` event), never truncated. Ack ids over 256 B
  go terminal (`fatal`).

## Supervised shutdown

- Signal handlers only set a termination flag; the main loop polls it
  every ~50 ms tick and runs shutdown there — nothing calls `_exit`
  from a handler.
- Teardown is TERM → finite wait (3 s grace) → KILL → reap over a
  tracked process group that stays supervised until `group_alive` is
  false, so a resistant descendant is escalated even after the direct
  child (leader) already exited. Spawning refuses to overlap a live
  supervised group (`Previous session group did not exit; retry`).
- This is best-effort supervision, not a guarantee: a SIGKILL still
  cannot cover uninterruptible states, zombies awaiting init, or
  theoretical pgid reuse (noted on the `Drop` fallback, which signals
  TERM+KILL without blocking). Docs promise teardown attempts with
  finite waits, not leak-proofing.

## Tests

Rust bridge tests (no Pi/model calls; protocol and teardown tests build
a debug binary and cover framing bounds, fail-closed saturation,
render budgets, and group teardown):

```sh
cargo test --locked --manifest-path services/agent-orchestrator/Cargo.toml
```

Python graph/tool tests stay under the repo `tests/` unittest suite and
are unaffected by this bridge:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

## Desktop-activity collector (`qs-desktop-context`): Phase 1 base + Phase 2 enrichment

Standalone same-crate binary recording Hyprland focus/title/workspace
activity to a private local SQLite DB, plus implemented Phase 2
application-aware enrichment (optional typed `resource` per snapshot;
new `context` kind). Still collect + local query only: no QML wiring,
no `ScopedAgent`/Pi bridge changes, no Pi protocol changes. Per-scope
Pi bridges keep their idle/`stopIdle` behavior; nothing auto-starts or
auto-installs this collector, and no worker/search/session consumer
reads the DB yet (library API in `src/lib.rs`, or read-only `history`
below). Phase 3 (future search/session use) must handle the freshness
notes under Phase 2 below; nothing here promises it.

### Build

Same locked release build as the bridge (repo root); produces both binaries:

```sh
cargo build --locked --release --manifest-path services/agent-orchestrator/Cargo.toml
```

```text
services/agent-orchestrator/target/release/qs-agent-orchestrator
services/agent-orchestrator/target/release/qs-desktop-context
```

### Run (Hyprland session only)

Launch inside the Hyprland environment so `XDG_RUNTIME_DIR` /
`HYPRLAND_INSTANCE_SIGNATURE` are inherited; without them the collector
waits on `no hyprland sockets`. Optional `exec-once` (absolute checkout
path, no auto-start otherwise):

```text
exec-once = /home/franzs/.config/quickshell/services/agent-orchestrator/target/release/qs-desktop-context
```

Discovery is strict (`discover_sockets_with`): explicit signatures must
be safe single segments (`[A-Za-z0-9_.-]`, ≤128 chars), and both
endpoints must be owned non-symlink sockets of the euid (files,
foreign sockets, symlinks rejected). Without a signature, healthy
candidates across modern and legacy bases are liveness-probed (200 ms):
stale loses to live, but multiple *live* instances resolve to `None` —
set `HYPRLAND_INSTANCE_SIGNATURE` explicitly.

Deadlines: connect 2 s (`connect_bounded`); requests 4 s total, each
wait capped by the remaining budget; idle read 1 s; coalesce ~50 ms.

CLI (diagnostics to stderr; stdout carries ONLY history JSON in
`history` mode):

```text
usage: qs-desktop-context [--db PATH] [collect] | history [--limit N] [--from START_MS --to END_MS [--limit N]]
```

- `collect` (default) holds the per-DB lock for life; exit 1 when the
  lock is held or the DB cannot be prepared/opened, exit 2 on usage errors.
- `history` is strictly read-only, never live `current`; missing DB →
  `[]` (exit 0) with a not-run-yet note, bad limit/range → exit 1.
- Only `--db`, `--limit`, `--from`/`--to`, `collect`, `history`,
  `--help`/`-h` exist — no redaction/retention/follow/format flags.

### Database location, strict permissions, per-DB lock

Default `$XDG_STATE_HOME/quickshell/desktop-activity/activity.db`,
else `$HOME/.local/state/…` (errors when neither resolves); `--db PATH`
overrides fully. The lock is a per-database `<dbname>.lock` sibling
(`lock_path_for`; `activity.db` → `activity.db.lock`, so two DBs in one
directory never share a lock; `validate_lock_for_db` /
`canonical_lock_path_for` reject alias escapes). One collector per DB:
`flock(LOCK_EX|LOCK_NB)` held for life; a second exits 1 with
`already locked`. Shutdown is `SIGTERM`/`SIGINT` to a polled flag
(`SIGPIPE` ignored), followed by a bounded ~2 s final drain (deadline
checked per row, ≤100 ms overrun for one in-flight append); clean drain
ends in `shutting down` (exit 0), remainder ends in
`shutdown incomplete: N pending` (exit 1, no durable spool).

Strict, never an arbitrary `chmod`: missing parents are created `0700`
from the outset, existing ancestors untouched; an existing leaf must
already be an euid-owned real directory with no group/other bits and no
symlink components, else startup fails unmodified. New DB/lock files are
`0600`; existing DB, lock, and sidecars (`-wal`, `-shm`, `-journal`)
must be euid-owned private regular files, never symlinks. WAL stays
best-effort.

Custom `--db` example — make the directory private first (a
pre-existing `0755` leaf is rejected, not repaired):

```sh
BIN=services/agent-orchestrator/target/release/qs-desktop-context
install -d -m 0700 /tmp/qs-test
"$BIN" --db /tmp/qs-test/activity.db
"$BIN" --db /tmp/qs-test/activity.db history --limit 5
"$BIN" --db /tmp/qs-test/activity.db history --from 1700000000000 --to 1700003600000 --limit 20
```

### Schema and truly read-only history

Current store is schema v2 (`SCHEMA_VERSION = 2`): singleton
`schema_version(version INTEGER PRIMARY KEY)` + `activity(id
AUTOINCREMENT, observed_at_ms, kind, source, snapshot_json,
project_id TEXT NULL)` + `idx_activity_time(observed_at_ms, id)` +
`idx_activity_project(project_id, observed_at_ms, id)`.
Phase 1 base was schema v1 (same table without `project_id` and only
the time index); v1 files migrate transactionally on writable open and
stay readable read-only (see project attribution below). Opens validate authoritatively
before any mutation (transactional setup): foreign tables, newer
versions, and multi-row `schema_version` are rejected
(`IncompatibleSchema`) without modification.

`history` uses `open_read_only`: creates nothing, takes no lock,
`PRAGMA query_only=ON`. Missing DB → `[]` (exit 0); permission,
unsafe-path, schema errors are distinct exit-1 failures. Queries (UTC
epoch-ms, `limit` 1..=1000): recent newest-first (`DESC, DESC`); range
start-inclusive/end-exclusive, oldest-first, ties by `id`.

### Snapshots, dedup, bounded backlog

Each snapshot runs four bounded queries per attempt
(`j/activewindow` + `j/activeworkspace`, reread pair) for up to 3
attempts: rereads must agree SEMANTICALLY (parsed focus/workspace fields
+ embedded workspace id, ignoring JSON formatting, geometry, counts, and
other irrelevant metadata) and the window's embedded workspace must
match the activeworkspace (special workspaces exempt); torn reads fail
with `Timeout` instead of persisting an impossible mix, while I/O and
shape errors (non-JSON/wrong-typed JSON) fail fast without retry.
`{}` is valid empty; title commas preserved. Only whitelisted
meaningful events trigger one reconciliation (paired bursts coalesced;
EOF mid-coalesce ends disconnected with no snapshot on the dead
subscription); malformed/oversize/cosmetic lines are ignored. Subscribe
to `.socket2.sock` first, then snapshot — nothing missed, no restored
`current`.

`Tracker` is live state only (`current_context()` starts `None`, never
backfilled). The durable path is `enqueue` with queue-tail dedup (so
`A → B → A` across failures keeps `B`) into a bounded FIFO (128) with
ORIGINAL timestamps, flushed in order on idle/backoff — never via a
compositor query (no desktop polling). Overflow is an explicit gap with
finite memory: the new observation is DROPPED with an
`observation gap` line on stderr (no reserved slot, no lossless claim);
the pre-existing backlog is retained and the session ends
(`BacklogFull`). While the backlog stays full the outer loop enters an
explicit storage-blocked state: ONLY persistence retries + backoff, NO
discovery/snapshot queries until capacity recovers. While no subscription
is held live `current` reads unavailable via local-only marking
(`mark_current_unavailable_local`), never stale available. Honest gap:
the backlog is in-memory, so crash-before-flush loses rows and
overflow/storage-blocked events are dropped with explicit gaps — on
stderr, never claimed as saved.

Shutdown performs a bounded final drain with a fixed deadline
(`SHUTDOWN_DRAIN_DEADLINE = 2 s`), checked between individual appends so
slow-but-successful rows cannot push the whole drain past it; at most one
in-flight SQLite busy-wait (≤100 ms) may overrun. Any remainder is
reported as `shutdown incomplete` with its pending count and exits
nonzero (1, no durable spool — in-memory backlog is lost).

Kinds: `focus`, `title`, `workspace`, `context` (Phase 2: same focused
window/workspace, only the application `resource` changed),
`availability`, `snapshot`. A focused-window `Some ↔ None` change is
`focus` (focus loss/gain proves no lifecycle); `window_open`/
`window_close` are reserved and never emitted. Legacy compatibility:
`SCHEMA_VERSION` stays `1` with the same `activity` table — the
snapshot JSON just gains an additive optional `resource` object, so
Phase 1 rows without `resource` (and without the focused-window
`process_id`) still deserialize to `None` and round-trip; the stored
`kind` string `"resource"` reads back as `context`. `process_id`
(Hyprland `pid`, lenient `None` when absent) is correlation metadata
only and never affects semantic equality/classification on its own.
Key names: `Tracker::{new, current_context,
enqueue, pending_front, pop_pending_front, mark_persisted,
mark_current_unavailable_local, backlog_is_full}`,
`flush_pending`, `final_drain`, `ActivityStore::{open, open_read_only,
open_in_memory, append, recent_activity, activity_in_range,
schema_version}`, `fetch_snapshot[_at]`, `connect_bounded`,
`run_collector_once`/`run_collector_forever`
(`run_collector_once_with_enrich`/`run_collector_forever_with_enrich`
with `AppRefreshConfig`), `acquire_lock`,
`default_db_path`, `lock_path_for`, `ensure_parent_dir`
(`BACKOFF_INITIAL`/`BACKOFF_MAX`, `SHUTDOWN_DRAIN_DEADLINE`,
`MAX_PENDING_OBSERVATIONS = 128`, `SNAPSHOT_MAX_ATTEMPTS = 3`).
Enrichment API: `ResourceContext`, `enrich_desktop_context`,
`enrich_with_env`, `EnrichmentEnv`, `ResourceProvider`,
`APP_REFRESH_INTERVAL`, `RECORD_FRESHNESS_MS`, `LAST_GOOD_TTL_MS`.

### Phase 2 application enrichment (implemented, opt-in per app)

Additive optional `resource` object on `DesktopContext`
(`adapter`, `file`, `cwd`, `git_root`, `git_branch`, `url`, `page`,
`title`; adapter-only shells normalize to `None`; bounds: adapter 64,
paths 4096, branch 256, url 2048, page 1024 chars). Same table, same
schema version — old rows keep working, new `context` rows appear only
when the resource changes under an unchanged focus/workspace. The
shipped `qs-desktop-context` binary already runs this path
(`run_collector_forever` → default `AppRefreshConfig` →
environment-driven `enrich_desktop_context`); each provider stays
disabled until its own opt-in variables/files exist, and a disabled or
failing provider never fails the base snapshot.

How it runs (async, best-effort, no desktop polling): every compositor
snapshot persists its BASE immediately on the event thread — merged with
the live overlay for the same exact window (opaque id + application +
client PID), because a raw fetch carries `resource: None`, which is not
confirmed absence; without the merge every compositor event would
oscillate clear/restore rows. A bounded background worker (one thread
per session, one in flight, latest request wins, panics caught to base)
then enriches a resource-free copy (the merged overlay is for immediate
persistence only, so confirmed absence clears on the event path while
transient gaps stay masked by bounded last-good); a process-wide hard cap
of 16 concurrent workers
bounds threads no matter how fast sessions reconnect — a denied session
(or a spawn failure, which releases its slot) runs base-only instead of
stacking threads. Pending compositor bytes always win over harvest:
after every idle flush the fd is re-probed, and worker results are
accepted only when it reads idle; a relevant event invalidates the
generation immediately (before coalescing), so the expected shape is
base-first then a `context` row. Terminal paths
(EOF/read errors) never drain enrichment: the subscription is dead, so
in-flight results are discarded and the session marks unavailable
immediately. Enriched rows carry completion time (`now_ms` at persist),
so idle resource changes advance history time. Enriched overflow
propagates `BacklogFull` with local-unavailable marking exactly like
snapshot overflow. Worker threads always stop with their session (held
slot count observable via `enrich_workers_live`); sessions are sequential
so at most one worker is live in production. Separately, a low-frequency application-only
refresh (`APP_REFRESH_INTERVAL = 5 s`) re-submits the live base with a
fresh timestamp for re-enrichment during idle reads — no
`fetch_snapshot`, no `discover`, no storage-blocked bypass. Corrected
polling claim: no *desktop* polling (base stays event-triggered);
application refresh only. The storage-blocked loop stays
persistence-only (no discovery, no snapshot, and no application
refresh) until capacity recovers.

Oscillation guard: bounded thread-local last-good cache
(`LAST_GOOD_TTL_MS = 60 s`, max 64 focus keys of lowercased app +
opaque window id + client PID, so a reused address never inherits the
previous occupant). Thread-locality means each session worker starts
fresh (reconnects invalidate automatically). Provider `None`
(transiently unavailable) serves the cached resource for the same focus
within TTL instead of flapping; a confirmed mismatch (kitty peer,
explicit binding) evicts instead of serving, never returning foreign
data. Kitty-path serves additionally require the exact verified
pane + editor PID recorded alongside the cached neovim value
(nonsemantic metadata, never persisted): a missing record on a
different pane or for a new editor PID clears, evicts, and falls back
to the verified pane cwd instead of serving old; unresolvable or
ambiguous pane focus is likewise a confirmed mismatch. A fresh result with a transiently-missing `git_branch` is
backfilled from the cache when the file/cwd/url/page anchor is
unchanged — but carried git keeps its ORIGINAL timestamps, so repeated
partial resolutions cannot renew the window past TTL, and a confirmed
non-repo (git's own "not a git repository" text under `LC_ALL=C`)
clears metadata immediately instead of back-filling. Cache health and
timestamps never enter the persisted resource, so last-good alone never
creates activity.

Per-app (all bounded, no shell, no file-content reads; details in
`integrations/*/README.md`):

- Neovim (opt-in Lua `qs-context.lua`): publishes the actual `file` +
  `cwd` per PID (`<pid>.json`, `0600`, atomic rename, `0700`
  euid-owned dir, default
  `$XDG_RUNTIME_DIR/quickshell/nvim-context`). Epoch
  `updated_at_ms` via `gettimeofday` (wall clock, not monotonic);
  collector ignores records older than 30 s. Standalone `nvim`/`neovim`
  class binds record PID == focused client PID; inside kitty it wins
  only as the unique foreground `nvim` with a fresh correlated record
  (matching kitty window id when both present). Relative `file` is
  resolved against `cwd`. The collector (not Lua) attaches
  `git_root`/`git_branch`.
- Kitty (opt-in remote control): `allow_remote_control socket-only`
  only (never `yes`; `socket-only` is a transport restriction — only
  local-socket peers may issue remote commands at all — not a
  read-only flag, so keep the socket private regardless), socket at an
  absolute path under a private runtime parent with a per-instance PID
  suffix, addressed as the exact `unix:` address `kitty @ --to`
  requires (bare paths and `unix:` addresses both normalize; see kitty
  README; no `/tmp` public-parent example). Identity is `SO_PEERCRED`
  peer PID == focused Hyprland client PID over the same bounded
  connector (a present but unequal peer is a confirmed mismatch:
  evicted, never last-good-served), then unique-`is_focused` OS
  window/tab/pane (any ambiguity yields no resource; numeric id==pid
  conflation rejected). Pane cwd is the unanimous foreground-process
  cwd else the focused pane `cwd`. Query bounded (1.5 s, 256 KiB).
- Zen (honest fallback + opt-in explicit): default `zen-title` carries
  only the focused window title, `url` stays `null`. An explicit URL
  comes only from an externally supplied private JSON file
  (`QS_ZEN_CONTEXT_FILE`, `0600`, ≤16 KiB) holding `window_id` + `pid`
  + `url`/`title` + epoch `updated_at_ms` (≤30 s); both the opaque
  compositor window id AND the client PID must match, else title
  fallback (confirmed mismatch) or bounded last-good (transient
  unreadable). No bundled publisher — your helper must sample the
  focused id+pid from the compositor in the same tick as the URL,
  write atomically, and refresh roughly every ~10 s.
- Logseq (title-only, no HTTP): the local `getCurrentPage` API was
  removed — it reports a process-global page with no verifiable
  per-window binding (a configured window id cannot prove the global
  value belongs to the focused window without polling), so any
  attributed page would be deceptive. `logseq-title` always carries the
  full window title plus `page` only via the explicit rule `<page> -
  Logseq` suffix-strip (anything else: `page` stays `null`, never a
  guess, never a URL).
- Git (collector-side, neovim/kitty paths only): bounded
  `git -C <dir> rev-parse` (1 s each, 8 KiB stdout, 2 KiB stderr,
  `LC_ALL=C`) from the file parent else `cwd`. Explicit non-repos
  (git's own "not a git repository") clear metadata immediately;
  timeouts/missing binaries stay transient for last-good fill.
  Detached HEAD yields `detached:<short-sha>`; worktrees resolve to
  their own root/branch; relative/malicious paths rejected. A transient
  branch miss leaves `git_branch` unset for last-good fill (which never
  renews its own TTL) rather than persisting a removal.

Security: full URLs, absolute paths, and window titles are stored
verbatim in the private DB (no redaction, no retention controls);
file *contents* are never read (only published records plus `git
rev-parse` metadata). Skip the collector or use a throwaway `--db` if
sensitive. Private-file rules from Phase 1 extend to enrichment inputs
(euid-owned, no group/other bits, no symlinks, `O_NOFOLLOW`, size
bounds; over-permissive/foreign/malformed inputs ignored).

Setup prerequisites (manual; nothing auto-installs or auto-starts):
Rust/Cargo locked release build, a Hyprland session with
`XDG_RUNTIME_DIR`/`HYPRLAND_INSTANCE_SIGNATURE`, one opt-in at a time
(`QS_KITTY_SOCKET` or `KITTY_LISTEN_ON`; Lua `qs-context.lua` with
`vim.g.qs_nvim_context_enable = true`; `QS_ZEN_CONTEXT_FILE`;
Logseq needs no config — title-only), and
an optional manual `exec-once` for the collector. Phase 3 note: future
search/session consumers must treat `resource` as best-effort and
stale-tolerant (5 s refresh cadence, 30 s record freshness, 60 s
last-good window, focus-bound invalidation) and must re-validate before
acting; no QML/search/session wiring exists yet.

### Desktop project attribution (deterministic, existing mapping only)

Deterministic overlay of the existing snapshot onto the existing
`scripts/projects.py` UUID registry (`QUICKSHELL_PROJECTS_FILE`
precedence; never parses `projects.toml` directly). No new model, no
inference, no writes. Full contract, CLI/Python usage, and limits:
`docs/desktop-project-context.md`.

- `DesktopContext.project` is `{id, name, matched_by}` only
  (`file`/`cwd`/`git_root`/`git_remote`); missing in old JSON reads as
  `None`. Unknown/ambiguous stays unassociated — no UI-selected
  fallback.
- Precedence `file > cwd > git_root > git_remote`, longest
  component-boundary folder wins per level; ties across projects at one
  level mean no association. Remotes are canonical GitHub
  HTTPS/SSH/scp (`https://github.com/owner/repo`, credentials stripped,
  never persisted); exactly one distinct registered claim wins while
  unmapped upstreams are ignored; conflicting mapped remotes mean
  unknown. Discovery is local `git config` only (linked worktrees via
  `commondir` supported), no network.
- Caches: registry subprocess only on metadata change (no repeat Python
  when unchanged); folder projection TTL 5 s; remotes 30 s TTL plus
  git-config invalidation.
- Store is schema v2 with indexed `project_id` (append-time,
  backfilled transactionally from v1 on writable open, read-only v1
  supported, no retroactive reassignment).
- CLI: `current`, `current-project`, `history --project UUID`,
  `last-activity --project UUID`, `resources --project UUID [--limit]`.
  `current` is a fresh on-demand snapshot plus focus recheck — not
  collector IPC/history. Python `scripts/desktop_projects.py` serves
  `current-project` / `todos` / `logseq-context` / `recent-activity` /
  `last-activity` / `resources` (overrides `--projects-file` `--graph`
  `--db` `--desktop-bin`; reuses the `read_page` task parser;
  name-only projects have history but no Logseq). Five additive Pi
  tools only; existing scopes unaffected. Same locked release build as
  above; nothing auto-installs or auto-starts. No
  LLM/embeddings/screenshots/sessions/Resume.
- Limits: async initial base is bare, then a `context` row; fresh-query
  helper caches are process-local; latest-resource streaming is bounded
  but can scan long duplicate runs; historical ids persist across
  remove/rename; graph context follows the current mapping; hung mounts
  are not hard-bounded; the binary's helper path is compile-time (move
  the checkout → rebuild). Next-phase consumers must keep resource-id
  identity/ordering and retention with no new semantic layer.

### Manual checks and tests

With a scratch `--db` (private dir first, as above) inside a Hyprland
session: start collection (expect `collecting to …`); run
`history --limit 20` twice idle — ids/count unchanged (dedup); switch
window/title/workspace and see one new `focus`/`title`/`workspace` row
each; with an opt-in provider set, change only the app resource (e.g.
nvim buffer, kitty cwd, bound Zen URL, Logseq title page) and see one
new `context` row, then repeat unchanged and see dedup (last-good masks
transient blips without new rows); `kill -TERM <pid>` drains pending within ~2 s (deadline checked per
row, ≤100 ms overrun for one in-flight append) and exits 0, or exits
1 with `shutdown incomplete: N pending` when storage stays blocked (no
durable spool); socket loss logs `reconnecting` with backoff, and a
second collector on the same `--db` exits 1 with `already locked`.

```sh
cargo test --locked --manifest-path services/agent-orchestrator/Cargo.toml
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Covers FIFO order/overflow, semantic reread stability (irrelevant
metadata ignored), discovery safety, read-only non-mutation, schema
rejection, fake-socket event→snapshot→disconnect→unavailable,
storage-blocked persistence-only backoff with no extra snapshots plus
ordered recovery, and shutdown drain (recovered vs persistent failure).

### Privacy and limitations

Titles/app names/URLs/paths stored verbatim (title ≤1024, app ≤256 chars; resource paths ≤4096, branch ≤256, url ≤2048, page ≤1024); no
redaction or retention controls — skip the collector or use a throwaway
`--db` if sensitive. File contents are never read. Hyprland-only; event-triggered base with no desktop polling
fallback plus low-frequency application-only refresh (5 s, no compositor
query); one writer per DB; `history` prints pretty JSON rows only;
crash-before-flush loses queued rows, overflow drops the new observation
with an explicit `observation gap`, and shutdown keeps only what the ~2 s
per-row-bounded drain persists (no lossless-recovery claim, no durable spool).
Enrichment is best-effort and focus-bound: the initial base row is bare
by design (enrichment arrives as a later `context` row); a focus change
discards in-flight enrichment for the old focus and EOF never waits for
it, so a slow provider can delay a `context` row but never base rows,
events, shutdown, or disconnect marking. Reused compositor addresses
are safe (PID-keyed cache + overlay merge); Logseq has no page API
(title rule only); Zen has no bundled URL publisher; a `context` row in
flight at SIGTERM is lost while its base is safe.
