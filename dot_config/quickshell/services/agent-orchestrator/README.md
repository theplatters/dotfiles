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
`LOGSEQ_GRAPH`/`PI_CODING_AGENT_SESSION_DIR` are inherited.

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
