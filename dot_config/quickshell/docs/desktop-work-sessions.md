# Desktop work sessions (deterministic, Phase 4)

Deterministic clustering of the append-only desktop activity log into
**work sessions**. No LLM, no embeddings, no semantic search, no
screenshots, no sync. Collect + local query only:
no QML wiring and no `ScopedAgent` bridge changes (the existing Pi extension
does gain additive read-only query tools). The deterministic Resume backend
consumes these sessions read-only (plan/execution contract:
`docs/desktop-resume.md`; command-palette and read-only Pi plan integration are
implemented there).

Related docs: `docs/desktop-project-context.md` (attribution contract),
`services/agent-orchestrator/README.md` (collector build/run reference),
`docs/project-planner.md` (planner-facing summary).

Source files: `services/agent-orchestrator/src/desktop_session.rs`
(pure config/identity/rules), `services/agent-orchestrator/src/desktop_store.rs`
(schema v4; v3 content migrates transactionally to v4 — full retrieval
contract: `docs/desktop-history-search.md`),
`services/agent-orchestrator/src/bin/qs-desktop-context.rs` (CLI),
`scripts/desktop_projects.py` (Python backend),
`.pi/extensions/desktop-agent.ts` (7 coherent read-only Pi history tools;
see `docs/desktop-history-search.md`).

## Schema v3 (migratable to v4 only)

- Fresh databases are created at v4 (`SCHEMA_VERSION = 4`); exact coherent
  v3 databases migrate transactionally to v4 on writable open (FTS
  backfill; full contract: `docs/desktop-history-search.md`).
  Activity rows carry stable 128-bit hex `event_id` (random, UNIQUE-indexed,
  collision-retried), durable local `device_id`, and exactly one
  `session_id`. Sessions are materialized rows (`sessions` table:
  project UUID/name, `start_ms`/`end_ms` bounds, first/last activity IDs,
  event count, persisted `open`/`closed` status plus `ended_reason`,
  per-session `gap_ms`/`interruption_ms`, device provenance,
  sorted distinct-application list). `session_resources` holds one deduped
  aggregate per resource key (see below). `device_info` is a singleton
  holding the durable generated local device identity; it scopes appends
  and `current-session`. Well-formed foreign-device events/sessions may
  coexist as synchronized history as long as each event agrees with its
  own session's device — the writer never closes, extends, repairs, or
  reports foreign-device opens as current.
- Raw activity is retained: snapshot JSON bytes and append-time project
  assignments are preserved verbatim. Session rows aggregate without
  copying full snapshots.
- The store accepts exact coherent v4 (writable or read-only) and
  migrates exact coherent v3 to v4 only when the restarted collector holds
  the per-database lock (read-only v3 reports migration-required). Any other
  schema version or shape —
  including pre-v3 (v1/v2) layouts, which remain unsupported with no
  migration and no read-only compatibility — is rejected untouched.
  Malformed or future/foreign v3/v4 content
  (missing v3 tables, missing UNIQUE `idx_activity_event_id`, bad
  `device_info` shape, non-singleton, null provenance columns,
  noncanonical provenance IDs, mismatched event/session devices, orphaned
  session IDs, inexact session projections (`start_ms`/`end_ms` min/max
  timestamps, `first_activity_id`/`last_activity_id` min/max ids),
  orphan sessions, corrupt thresholds, unknown tables/objects,
  newer/multi-row `schema_version`, marker-only layouts with extra
  schema objects)
  is likewise rejected untouched (`IncompatibleSchema`, no modifications).
  Missing non-unique indexes are the only completion (writable recreates
  them); the UNIQUE event index is never recreated.

## Defaults and config

| Setting | Default | Env | Collect flag |
| --- | --- | --- | --- |
| Inactivity gap | 1,800,000 ms (30 min) | `QS_DESKTOP_SESSION_GAP_MS` | `--session-gap-ms MS` |
| Interruption grace | 120,000 ms (2 min) | `QS_DESKTOP_SESSION_INTERRUPTION_MS` | `--session-interruption-ms MS` |

- Precedence: CLI > env > defaults, resolved independently per field; a
  CLI override bypasses its env var entirely (even a malformed one).
  Present-but-malformed env fails closed. Only the final pair is
  bounds-validated. Thresholds are collect-only: query subcommands reject
  `--session-gap-ms`/`--session-interruption-ms`.
- Bounds: gap 1 ms (`MIN_SESSION_GAP_MS`) .. 24 h (`MAX_SESSION_GAP_MS`);
  grace 0 .. gap. Grace `0` means any second unresolved/return event
  splits. Validation happens before any filesystem/DB mutation, so bad
  thresholds fail fast with nothing created.
- Per-session values frozen: each session row stores the `gap_ms` /
  `interruption_ms` in effect at creation. Later config changes never
  rewrite historical boundaries; split decisions always use the open
  session's stored thresholds.

## Sessionization rules

Online, no lookahead: each new event is assigned using only the persisted
local open session plus the new event. Gap is checked first
(gap-first precedence).

- Gap `>=` the open session's stored gap finalizes it as `inactivity`
  — even when the new event also switches projects.
- Resolved known project `A →` known project `B` (`B != A`,
  case-insensitive UUID compare) finalizes immediately as
  `project_switch`. Same-project app/window/workspace/resource changes
  never split.
- A known-project session absorbs a brief unresolved/unavailable run: the
  first unresolved event records `unresolved_start` and continues. While
  the run is open, a switch to another known project still splits
  immediately as `project_switch`. When a later unresolved event — or a
  return to the same project — proves `new_ts - unresolved_start >=
  grace`, the open session finalizes as `interruption` and the later
  event starts the new session. Earlier unresolved events remain in the
  prior session (stable IDs never shift retroactively).
- Brief compositor focus gaps do not even open an unresolved run: a
  focusless snapshot (`available`, no focused window — e.g. a Quickshell
  layer surface takes keyboard focus while the project planner/popup is
  open) retains the previous snapshot's `resource`/`project` for up to the
  session's stored interruption grace, anchored at the first observed
  focusless time (`focusless_since_ms` in the snapshot JSON;
  `retained_since_ms` still means retention was applied; schema v4 is
  unchanged). The persisted row stays honestly `focused_window: null`, and
  repeated focusless events never extend the window (both anchors carry
  over). Expiry is anchored on the streak clock, so an expired,
  honestly-cleared focusless row stays expired instead of re-anchoring
  on an older signal row. A real window event or a workspace change ends
  retention immediately; past the grace the honest cleared context is persisted and
  the normal unresolved/interruption rules above apply. The live
  `current`/`current-project` reads apply the same rule read-only over a
  bounded lookback (up to 64 newest rows) to find the newest row that
  actually carries a project/resource — skipping un-enriched same-window
  rows, and stopping unchanged on an unavailable row, a workspace
  mismatch, or an intervening unknown-project row — so opening the
  planner/topbar does not blank the current project indicator.
- Unresolved session `→` resolved project starts a new project session
  (`project_switch`); unresolved `→` unresolved continues unless gapped.
- Timestamps drive gaps; equal timestamps order by activity ID upstream
  (gap `0` continues). Clock rollback (`new_ts < end`) never splits and
  never corrupts ranges: the writer extends bounds as
  `start=min`/`end=max`, and gap math is rollback-safe
  (`saturating_sub`, so negative gaps never split).
- Shutdown never splits: SIGTERM/SIGINT runs only the bounded (~2 s)
  final drain; no session is finalized on shutdown. Restart continues the
  most recent persisted local open session (nearest by end/start/id) —
  subject to the normal gap rule, so a long downtime ends as `inactivity`
  at the next event. The in-memory tracker is live-only and never
  backfilled from rows.
- Appends are transactional: the raw insert plus its session
  assignment/update/resource aggregation commit as one SQLite transaction;
  sessionization failure rolls back the raw insert so collector FIFO retry
  semantics stay true. Multiple corrupt local opens repair to one
  (newest kept, rest closed as `inactivity`) inside the same transaction.

## Persisted vs effective status; current-session

- Persisted `status` is `open` or `closed`; `ended_reason` is
  `inactivity` / `project_switch` / `interruption` (or `NULL` while open).
- Query-time `effective_status` (one captured `now` per response, never
  mutating) derives from a persisted `open` session:
  - `active` — within gap and grace;
  - `interrupted` — an absorbed unresolved run reached the stored grace
    (`now - unresolved_start >= interruption`; grace `0` deactivates at
    the first unresolved event);
  - `stale` — the stored inactivity gap expired
    (`now >= end + gap`, mirroring the writer's gap-first precedence).
  - Persisted `closed` sessions always report `closed`. `active` is also
    exposed as a boolean.
- `current-session` returns the newest local-device `open` session only
  while its effective status is `active` at query time, else `null`
  (until the next writer finalizes it). It is scoped to this database's
  durable `device_info` identity: foreign-device opens are listed by
  session queries but never continued, repaired, or reported as current.

## Session resources

- One aggregate per stable identity key with `occurrence_count`,
  `first_seen_ms`/`last_seen_ms` (min/max timestamps),
  first/last activity IDs (insertion order), and the latest compact
  `ResourceContext` as metadata. Counts increment per associated event
  carrying the identity.
- Identity priority: (1) file/cwd under `git_root` (component boundaries;
  root itself is `"."`; `file` wins over `cwd`), (2) `url`, then `page`,
  (3) otherwise machine-local absolute `file`, then `cwd`.
- Portable vs local: a git-root-relative file/cwd anchored by the event's
  project UUID (`portable:proj:<uuid>:…`) or by the normalized GitHub
  remote (`portable:remote:<remote>:…`) is portable; a URL/page is
  portable. Without a project UUID or remote anchor the location is
  explicitly machine-local with the git root embedded
  (`local:…@<root>`), so the same relative path in different repositories
  never collides — even inside one absorbed unresolved run. Typed keys
  (`file:` vs `url:` vs `cwd:` vs `page:`) never collide across types.
- Adapter/title/branch never affect identity, so repeated branch, title,
  or adapter changes on the same location dedup into one aggregate. Empty
  locations aggregate nothing. Raw events always retain the full
  `ResourceContext`; only this compact identity plus latest metadata is
  aggregated per session.

## Query / API / CLI

Rust store (`ActivityStore`): `current_session(now_ms)`,
`recent_sessions` / `recent_sessions_for_project` (newest-first by
end/start/id), `sessions_in_range` / `sessions_in_range_for_project`
(overlap `end >= start AND start < end`, oldest-first; empty range
returns empty), `last_session_for_project`, `session_by_id`,
`session_resources` (newest-first by last-seen), `session_events`
(oldest-first by observed/id). Limits are 1..=1000; ranges require
`from`/`to` together with `0 <= from <= to`. Existing raw commands
(`history`, `last-activity`, `resources`) are unchanged.

Exact CLI usage (`qs-desktop-context`; diagnostics to stderr, stdout
carries ONLY JSON):

```text
usage: qs-desktop-context [--db PATH] [collect [--session-gap-ms MS --session-interruption-ms MS]]
       qs-desktop-context [--db PATH] history [--project UUID] [--limit N] [--from START_MS --to END_MS [--limit N]]
       qs-desktop-context [--db PATH] current
       qs-desktop-context [--db PATH] current-project
       qs-desktop-context [--db PATH] last-activity --project UUID
       qs-desktop-context [--db PATH] resources --project UUID [--limit N]
       qs-desktop-context [--db PATH] current-session
       qs-desktop-context [--db PATH] sessions [--project UUID] [--limit N] [--from START_MS --to END_MS]
       qs-desktop-context [--db PATH] last-session --project UUID
       qs-desktop-context [--db PATH] session-resources --session SESSION_ID [--limit N]
       qs-desktop-context [--db PATH] session-events --session SESSION_ID [--limit N]
```

- `current-session`: persisted current work session object or `null`.
  No compositor call; freshness is query-time `now` computed by Rust.
- `sessions`: optional `--project` scope, optional paired `--from/--to`
  overlap window, `--limit` (default 20). No-project lists all sessions.
- `last-session --project UUID`: latest session for one project (or `null`).
- `session-resources --session 32HEX [--limit N]`: per-session resource
  rollups. `session-events --session 32HEX [--limit N]`: activity rows
  for that session (history shape plus `event_id`/`device_id`/
  `session_id` provenance).
- Session JSON shape: `session_id`, `device_id`, `project:{id,name}|null`,
  `start_ms`/`end_ms`, `first_activity_id`/`last_activity_id`,
  `event_count`, `status`, `ended_reason`, `unresolved_start_ms`,
  `active`, `effective_status`, `applications`, `gap_ms`,
  `interruption_ms`.

Examples (scratch DB first; a pre-existing non-private leaf is rejected,
not repaired):

```sh
BIN=services/agent-orchestrator/target/release/qs-desktop-context
install -d -m 0700 /tmp/qs-test
"$BIN" --db /tmp/qs-test/activity.db current-session
"$BIN" --db /tmp/qs-test/activity.db sessions --limit 5
UUID='existing-project-uuid'
"$BIN" --db /tmp/qs-test/activity.db sessions --project "$UUID" --limit 5
"$BIN" --db /tmp/qs-test/activity.db sessions --from 1700000000000 --to 1700003600000 --limit 20
"$BIN" --db /tmp/qs-test/activity.db last-session --project "$UUID"
SID='<32-hex-session-id>'
"$BIN" --db /tmp/qs-test/activity.db session-resources --session "$SID" --limit 20
"$BIN" --db /tmp/qs-test/activity.db session-events --session "$SID" --limit 20
# collect with explicit thresholds (CLI > env > defaults):
QS_DESKTOP_SESSION_GAP_MS=1800000 QS_DESKTOP_SESSION_INTERRUPTION_MS=120000 \
  "$BIN" --db /tmp/qs-test/activity.db collect --session-gap-ms 1800000 --session-interruption-ms 120000
```

## Python backend (`scripts/desktop_projects.py`)

Read-only views; all JSON to stdout, single-line `error:` on stderr,
exit 1. Overrides: `--projects-file`, `--graph` (todos/logseq-context
only), `--db`, `--desktop-bin` (plus `QUICKSHELL_PROJECTS_FILE`,
`LOGSEQ_GRAPH`, `QS_DESKTOP_DB`/`QS_DESKTOP_CONTEXT_DB`,
`QS_DESKTOP_CONTEXT_BIN`). Old commands unchanged: `current-project`,
`todos`, `logseq-context`, `recent-activity`, `last-activity`,
`resources`. New work-session commands:

```sh
python3 scripts/desktop_projects.py current-session
python3 scripts/desktop_projects.py sessions [--project UUID] [--limit N] [--from START_MS --to END_MS]
python3 scripts/desktop_projects.py last-session --project UUID
python3 scripts/desktop_projects.py session-resources --session 32HEX [--limit N]
python3 scripts/desktop_projects.py session-events --session 32HEX [--limit N]
```

- `current-session` (`{session,reason}`): direct persisted-DB query; no
  compositor call, no registry requirement.
- `sessions` (`{project,registry,requested_project_id,sessions,count,
  reason}`): no-project lists all sessions directly (no
  compositor/registry call); explicit project is UUID-validated with
  registry metadata resolved for display (unknown/deleted UUIDs still
  query). Paired nonnegative `--from/--to` validated here and passed to
  Rust; limit stays 1..1000.
- `last-session` requires explicit `--project UUID`
  (`{project,registry,requested_project_id,session,reason}`);
  unknown/deleted UUIDs still query. `session-resources` /
  `session-events` require `--session 32HEX` (canonical lowercase) and
  make no compositor/registry call (`{session_id,resources,count}` /
  `{session_id,events,count}`).
- Flag gates mirror Rust: `current-session` rejects
  `--project/--limit/--session/--from/--to`; `sessions` rejects
  `--session`; `last-session` rejects `--limit/--session/--from/--to`;
  raw commands reject `--session` and `--from/--to`.

## Pi tools (7 coherent history tools; see `docs/desktop-history-search.md`)

`.pi/extensions/desktop-agent.ts`, every scope (palette, project,
journal). Each call runs one bounded `scripts/desktop_projects.py`
subprocess; scoped mutation rules unchanged. The coherent surface is
`desktop_current_context`, `desktop_current_session`,
`desktop_search_activity`, `desktop_get_session`,
`desktop_project_activity` plus the separate Logseq views
`desktop_project_todos`, `desktop_project_logseq_context` (legacy
per-session/per-project query commands such as `sessions`,
`session-resources`, and `session-events` remain available via the
CLI/Python backend for compatibility).

Work sessions vs Pi agent chat sessions (do not confuse): work sessions
are deterministic DB-derived activity clusters (`session_id` = 32
lowercase hex) describing what the desktop did; they never list, switch,
resume, or mutate Pi sessions. Pi agent sessions
(`SessionManager`/`session_before_switch`/`desktop-sessions` picker in
the same extension) are conversational agent transcripts managed by Pi
itself.

## Limitations and no-goals

- No LLM summaries, embeddings, semantic search, screenshots, or sync.
  Deterministic Resume is a separate consumer/action layer with its own
  typed, non-destructive execution contract: `docs/desktop-resume.md`.
- Event-driven collection only: there is no keyboard/mouse idle detector
  and no heartbeat. A session ends at its last meaningful persisted
  event; current activity is inferred from thresholds
  (`effective_status`), not measured. `stale`/`interrupted` are query-time
  inferences — the persisted row stays `open` until the next writer
  finalizes it.
- URLs, titles, and raw absolute paths are retained verbatim in the
  private local DB (no redaction, no retention controls); skip the
  collector or use a throwaway `--db` if sensitive.
- `session_resources` in `desktop_store.rs` (legacy per-project
  recently-used list) is unchanged for compatibility; per-session
  rollups are the separate `session_resources` table / `session-resources`
  command.

## Phase 5 retrieval (implemented)

Implemented; authoritative guide: `docs/desktop-history-search.md`.

- Consume sessions as the primary unit via session-centric search, but keep raw `session-events`
  for audit: aggregates never copy full snapshots.
- Use stable IDs and provenance: join on `session_id`/`event_id`
  (32-hex) and honor per-event/per-session `device_id`.
- Resolve portable resource identity against the current registry before
  acting (Resume or otherwise); never trust stale absolute paths, window
  IDs, PIDs, or workspace IDs — they are correlation metadata, and
  resources are best-effort/stale-tolerant (5 s refresh cadence, 30 s
  record freshness, 60 s last-good window, focus-bound invalidation).
- Sync (if ever added) must preserve per-device current/open state and
  must not synchronize or overwrite the local `device_info` identity;
  conflict handling for late/out-of-order foreign events must not rewrite
  stable IDs.
- LLM summaries, if later added, are derived and versioned artifacts —
  never session boundaries.
