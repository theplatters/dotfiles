# Desktop history search (session-centric retrieval, Phase 5)

Phase 5 retrieval over the append-only desktop activity log. Session-centric:
search returns compact session summaries with resources; raw events are
absent by default and fetched only through an explicit session detail
drill-down. No LLM, no embeddings, no semantic search, no screenshots, no
auto classification, no restore, no sync. Lexical/token search only
(FTS5 `MATCH` over indexed text columns).

Related docs: `docs/desktop-work-sessions.md` (Phase 4 sessionization),
`docs/desktop-project-context.md` (attribution contract),
`services/agent-orchestrator/README.md` (collector build/run reference),
`docs/project-planner.md` (planner-facing summary).

Source files: `services/agent-orchestrator/src/desktop_store.rs`
(`SessionSearchQuery`, `search_sessions`, `session_detail`, schema v4),
`services/agent-orchestrator/src/bin/qs-desktop-context.rs` (`search`,
`session-detail`), `scripts/desktop_projects.py` (`current-context`,
`search-activity`, `get-session`, `project-activity`),
`.pi/extensions/desktop-agent.ts` (7 coherent read-only Pi tools).

## Retrieval API (`SessionSearchQuery`)

`ActivityStore::search_sessions(&SessionSearchQuery)` in
`src/desktop_store.rs`. All filters are optional and combined in SQL (no
broad in-memory history load).

| Field | Type | Semantics |
| --- | --- | --- |
| `project_id` | optional UUID | Normalized to lowercase for indexed lookup (`normalize_project_id`). |
| `application` | optional text | Trimmed, `1..=256` chars (`MAX_SEARCH_TEXT_CHARS`); column-scoped literal FTS on `application`. |
| `resource` | optional text | Trimmed, `1..=256` chars; column-scoped literal FTS on `resource`. |
| `device_id` | optional 32-hex | Normalized to canonical lowercase (`normalize_hex_id`); normal SQL predicate on `sessions.device_id`. |
| `query` | optional free text | Trimmed, `1..=256` chars; general literal FTS across indexed columns. |
| `start_ms` / `end_ms` | paired optional `i64` | Both `Some` or both `None`; `0 <= from <= to`; start-inclusive / end-exclusive UTC epoch-ms (`[from, to)`). Exactly one present is an error. `from == to` matches nothing (returns empty). |
| `limit` | `i64` | Bounds the number of *sessions* returned, `1..=1000` (`MAX_QUERY_LIMIT`). |

- FTS expressions are escaped/literal: user text is split on whitespace
  and each token is double-quote-quoted (`"` doubled), joined with `AND`
  (`application : "tok" AND …` for app/resource, `"tok" AND …` for free
  text). User FTS syntax cannot inject operators.
- Order carries `matched_at_ms` (`Some(MAX)` newest matching observation)
  for FTS/range searches: newest matching observation first (then session
  end/start/id), so `limit = 1` answers "last matching observation",
  including multi-device overlap. Project/device-only and no-filter searches
  carry `matched_at_ms = None` with session-recency order.
- No filter at all means recent sessions: `recent_sessions(limit)` plus
  newest resources per session.
- Resource-matched prioritization: per-session resources favor
  actually-matched keys ranked by newest matching activity timestamp/id
  (deterministic `GROUP BY key ORDER BY max_ts DESC, max_id DESC, key ASC`,
  newest 8 — correct past 32 matches), then fill with other recent
  resources, capped at `MAX_SEARCH_RESOURCES_PER_SESSION` (8). When nothing
  matched, the newest session resources are returned. Summaries keep stable
  session/device/project IDs, start/end, event count, status/effective
  status, `matched_at_ms`, applications, and resources; they omit
  thresholds/bound IDs and carry no snapshots/raw events.
- Session discovery traverses FTS matches ONCE: a single grouped
  `activity ⨝ activity_fts` query (exactly one `MATCH`, event-level range,
  `GROUP BY session_id`) joined to `sessions` for project/device/time —
  never a correlated `MATCH` per candidate session. Structured no-FTS
  queries stay indexed/bounded on `sessions`.
- Range + text interaction: with a range, text matches must occur on an
  activity row *inside* the range (`activity.observed_at_ms` in
  `[start, end)` joined to its FTS row), not merely on another event in an
  overlapping session. Session overlap (`end >= start AND start < end`) is
  only an index-use prefilter implied by the event-in-range check.
- Session detail drill-down: `session_detail(session_id, resource_limit,
  include_events, event_limit)` returns `{session, resources,
  events_included, events}`. `session_id` is 32-hex (canonical lowercase);
  limits are `1..=1000`; `event_limit` is only validated/used when
  `include-events` is given. Raw events are never fetched unless
  explicitly requested; without `include-events` the response has no
  `events` key. Unknown session IDs return `session: null` with empty
  resources.

## Schema v4

- Activity/session/resource model is unchanged from v3: same `activity`
  columns (incl. `project_id`/`event_id`/`device_id`/`session_id`), same
  `sessions`, `session_resources`, and singleton `device_info` shapes.
- Addition 1: application-maintained content-bearing FTS5 table
  `activity_fts`, exactly one event row per activity row
  (`rowid = activity.id`):
  `session_id UNINDEXED, resource_key UNINDEXED, project, application,
  window_title, workspace, resource, metadata` with
  `tokenize='unicode61 remove_diacritics 2'`. No embeddings, no triggers,
  no prefix indexes.
- Searchable field groups: `project` = project name; `application` =
  focused application; `window_title` = focused window title; `workspace` =
  workspace name/id text; `resource` = adapter/file/cwd/git_root/
  git_branch/git_remote/url/page/title joined; `metadata` = kind + source.
  `session_id`/`resource_key` are stored but `UNINDEXED` (not searchable).
- Writes are atomic: every raw append plus its session
  assignment/update/resource aggregation plus its FTS row is one SQLite
  transaction; any step failing rolls back the raw insert (collector FIFO
  retry semantics stay true — including a real mid-transaction FTS failure,
  proven by test). The writer maintains FTS rows explicitly;
  there are no triggers. Writable opens additionally enforce the
  one-to-one `activity.id ↔ activity_fts.rowid` relation; read-only opens
  use the lightweight structural contract below and never scan full history.
- Addition 2: normal index `idx_sessions_device_time(device_id, end_ms,
  start_ms, session_id)` for structured device+time filtering.
- Fresh databases are created at v4 (`SCHEMA_VERSION = 4`).
- v3 → v4 lock-gated transactional preservation/backfill: only the
  lock-capability open (`open_with_session_config_and_lock`, guard for
  exactly `lock_path_for(db)`) migrates an exact coherent v3 database to v4
  in one `BEGIN IMMEDIATE` transaction — strict v3 prevalidation runs
  *inside* the same transaction (no writer can race validation/backfill/
  version bump), then the device+time index and FTS table are created, every
  stored `DesktopContext` is parsed and backfilled into FTS, and
  `schema_version` is bumped 3 → 4. Ordinary opens reject v3 with a clear
  collector lock/restart error (stop the old collector, let its ~2 s drain
  finish, restart; the restart holds the per-DB lock for life and passes it
  to the store). Any malformed snapshot or FTS failure rolls back untouched
  (version stays 3, no FTS, no partial index). A v3 trigger that would fire
  on the migration's own `schema_version` update is rejected before any
  mutation and never fires.
- Read-only v4 behavior (query-performance contract): read-only opens accept
  exact coherent v4 with strict structural validation (objects/tables/
  shapes, exact FTS declaration, required index shapes) inside a single read
  snapshot plus cheap singleton/version/device metadata checks only — never
  full-history data-coherence scans — so every `search`/`history`/`detail`
  invocation stays fast. Rows actually touched still validate through the
  row parsers. Writable opens keep the deep exact validation (inexact
  projections/provenance still reject there). A v3 database gets a clear
  migration-required error: stop/drain the old collector and restart the new
  collector so it migrates while holding the per-database lock. Ordinary
  writable opens never migrate, and v3 is not silently read as v4.
- FTS declaration is exact: precisely `session_id`/`resource_key`
  `UNINDEXED` (any other column `UNINDEXED` rejects), tokenizer exactly
  `unicode61 remove_diacritics 2` (only whitespace/case normalized), no
  `prefix`/`detail`/`content`/`columnsize`/etc options; shadow tables match
  exact expected DDL. SQLite-owned names match the literal `sqlite_*`
  prefix (`GLOB`), never `LIKE 'sqlite_%'`.
- v1/v2 remain unsupported: no migration, no read-only compatibility.
  Schema v3 is migratable to v4 only; never imply v1/v2 support. Any other
  version/shape (pre-v3 layouts, futures, foreign objects, multi-row
  `schema_version`) is rejected untouched (`IncompatibleSchema`).

## CLI (`qs-desktop-context`)

Diagnostics to stderr; stdout carries ONLY JSON. Query modes are strictly
read-only; missing DB reads as empty (exit 0) with a not-run-yet note; bad
limit/range fails closed (exit 1) even when the DB is missing.

```text
usage: qs-desktop-context [--db PATH] [collect [--session-gap-ms MS --session-interruption-ms MS]]
       qs-desktop-context [--db PATH] history [--project UUID] [--limit N] [--from START_MS --to END_MS [--limit N]]
       qs-desktop-context current
       qs-desktop-context current-project
       qs-desktop-context [--db PATH] last-activity --project UUID
       qs-desktop-context [--db PATH] resources --project UUID [--limit N]
       qs-desktop-context [--db PATH] current-session
       qs-desktop-context [--db PATH] sessions [--project UUID] [--limit N] [--from START_MS --to END_MS]
       qs-desktop-context [--db PATH] last-session --project UUID
       qs-desktop-context [--db PATH] session-resources --session SESSION_ID [--limit N]
       qs-desktop-context [--db PATH] session-events --session SESSION_ID [--limit N]
       qs-desktop-context [--db PATH] search [--project UUID] [--application TEXT] [--resource TEXT] [--device 32HEX] [--query TEXT] [--from START_MS --to END_MS] [--limit N]
       qs-desktop-context [--db PATH] session-detail --session SESSION_ID [--resource-limit N] [--include-events [--event-limit N]]
```

- `search` prints `{query: {project, application, resource, device, query,
  from_ms, to_ms, range_semantics, limit}, sessions: [compact summaries],
  count}`. `range_semantics` is `"start inclusive, end exclusive, UTC
  epoch-ms"` when a range is given, else `null`. `--from`/`--to` must pair;
  `--application`/`--resource`/`--device`/`--query` are search-only;
  `--session` is rejected for `search`.
- `session-detail` prints `{session: full | null, resources: [],
  events_included: bool, events?: []}` and never fetches/outputs events
  unless `--include-events`. `--event-limit` requires `--include-events`.
  `--project`/`--from`/`--to`/`--limit`/search texts are rejected for
  `session-detail`. Defaults: resource/event limits 20.
- Legacy APIs (`history`, `last-activity`, `resources`,
  `current-session`, `sessions`, `last-session`, `session-resources`,
  `session-events`) are unchanged and reject the new search/detail flags.

Examples (scratch DB first; a pre-existing non-private leaf is rejected,
not repaired):

```sh
BIN=services/agent-orchestrator/target/release/qs-desktop-context
install -d -m 0700 /tmp/qs-test
"$BIN" --db /tmp/qs-test/activity.db search --limit 5
"$BIN" --db /tmp/qs-test/activity.db search --query "nvim" --limit 5
"$BIN" --db /tmp/qs-test/activity.db search --application "kitty" --from 1700000000000 --to 1700086400000 --limit 20
"$BIN" --db /tmp/qs-test/activity.db search --project "$UUID" --resource "search_and_matching" --limit 5
SID='<32-hex-session-id>'
"$BIN" --db /tmp/qs-test/activity.db session-detail --session "$SID"
"$BIN" --db /tmp/qs-test/activity.db session-detail --session "$SID" --resource-limit 20 --include-events --event-limit 20
```

## Python backend (`scripts/desktop_projects.py`)

Read-only views; all JSON to stdout, single-line `error:` on stderr, exit
1. Overrides: `--projects-file`, `--graph` (todos/logseq-context only),
`--db`, `--desktop-bin` (plus `QUICKSHELL_PROJECTS_FILE`, `LOGSEQ_GRAPH`,
`QS_DESKTOP_DB`/`QS_DESKTOP_CONTEXT_DB`, `QS_DESKTOP_CONTEXT_BIN`).

```sh
python3 scripts/desktop_projects.py current-context
python3 scripts/desktop_projects.py search-activity [--project UUID] [--application TEXT] [--resource TEXT] [--device 32HEX] [--query TEXT] [--from START_MS --to END_MS] [--limit N]
python3 scripts/desktop_projects.py get-session --session 32HEX [--resource-limit N] [--include-events [--event-limit N]]
python3 scripts/desktop_projects.py project-activity [--project UUID] [--application TEXT] [--resource TEXT] [--device 32HEX] [--query TEXT] [--from START_MS --to END_MS] [--limit N]
```

- `current-context`: alias of `current-project` (same full context plus
  project/registry linkage). Kept as a separate command so the coherent
  history surface does not reuse the legacy name in Pi tools.
- `search-activity`: the Rust `search` object verbatim
  (`{query, sessions, count}`). Direct DB query; no compositor/registry
  call. Empty filters allowed (recent sessions, newest-first). Project is
  an optional UUID; application/resource/query are trimmed `1..256` chars
  (explicit empty is an error, not a dropped filter); device is 32-hex
  canonical lowercase; `--from`/`--to` must pair (nonnegative,
  `from <= to`); limit `1..1000` (default 20). Compact summaries carry
  resources but no snapshots/raw event arrays.
- `get-session`: the Rust `session-detail` object verbatim
  (`{session, resources, events_included, events?}`). Direct DB query; no
  compositor/registry call. `--session` 32-hex required; events excluded
  by default; `--event-limit` requires `--include-events`; limits
  `1..1000` (defaults 20). `events` is present only when requested.
- `project-activity`: same Rust `search` but defaults to the fresh current
  stable project identity; explicit `--project` skips the compositor.
  Registry is display-only (unknown explicit UUIDs still query). Returns
  `{project, reported_project?, registry, requested_project_id, query,
  sessions, count, reason}`. Unassociated current identity returns empty
  with no DB query. The default-current two-call path shares one
  `REQUEST_TIMEOUT` (8 s) deadline.
- `seen [--query TEXT] [--project UUID] [--limit N]`: thin palette
  mapper for the `seen:` source (Phase 2b §4.1). With `--query` it
  flattens the per-session resources of one Rust `search` (sessions
  ranked by match, resources newest-first); bare it maps `resources`
  output (current project unless `--project`). Returns flat
  `{rows, truncated, total}` with
  `{kind, label, identity, project_id, project_name, session_id,
  last_seen_ms, occurrence_count}` (kinds `file`/`url`/`zotero`/`page`,
  labels capped at 160 chars, rows capped at 20). No new store, no
  collector change. Replaces the palette `hist:` prefix outright (L6);
  the Pi tool rename from `session_ledger_list` to `session_search`
  lives in `docs/sessions.md`.
- Legacy APIs (`current-project`, `todos`, `logseq-context`,
  `recent-activity`, `last-activity`, `resources`, `current-session`,
  `sessions`, `last-session`, `session-resources`, `session-events`)
  remain with unchanged contracts and reject the new search/detail flags.

## Pi tools (7 coherent history tools)

`.pi/extensions/desktop-agent.ts`, registered unconditionally so they are
available in every scope (palette, project, journal). Each call runs one
bounded `scripts/desktop_projects.py` subprocess (1 MiB cap, 10 s
timeout); scoped mutation rules unchanged. History results are untrusted
evidence and never Pi agent chat sessions
(`SessionManager`/`session_before_switch`/`desktop-sessions` picker is a
separate conversational-session system).

| Pi tool | Backend command | Purpose |
| --- | --- | --- |
| `desktop_current_context` | `current-context` | Fresh live context + project/registry linkage. |
| `desktop_current_session` | `current-session` | Persisted current deterministic work session. |
| `desktop_search_activity` | `search-activity` | Cross-project session search; empty filters list recent sessions. |
| `desktop_get_session` | `get-session` | One session detail; events only with `includeEvents`. |
| `desktop_project_activity` | `project-activity` | Session search scoped to the current project by default. |
| `desktop_project_todos` | `todos` | TODOs for the current project (separate Logseq view). |
| `desktop_project_logseq_context` | `logseq-context` | Logseq page content for the current project (separate Logseq view). |

`desktop_project_todos` / `desktop_project_logseq_context` remain separate
Logseq views reusing the existing `read_page` parser; they are not history
retrieval. There is no separate chatbot or QML protocol: the existing
command palette AI path (resident bridge via `widgets/ScopedAgent.qml`,
`--mode palette`) is reused — Pi calls these tools through the same
extension, and no new QML wiring was added.

Phase 2a note: the sessions Pi tool is now `session_search` (see
`docs/sessions.md`); it stays available in every scope (palette,
project, journal). Phase 2c note: it is union-backed — thought and
TODO full text from the derived `content_index` FTS5 table in
`annotations.db` (same tokenizer/declaration discipline as
`activity_fts`, populated per successful write plus `content_index.py
rebuild`, dropping it costs only a rescan) ∪ collector
`search-activity`, deduped by `session_id` with content matches
ranked first. Omitting the text query browses via `sessions.py
list`.

## Natural-language time handling

The backend accepts only concrete UTC epoch-ms `[from, to)` pairs; it
never parses natural language. Pi resolves phrases like "yesterday",
"this week", or "around 14:00" into a concrete `[fromMs, toMs)` window
before calling, using the machine IANA timezone shown in the tool
descriptions (`DESKTOP_LOCAL_TZ` from
`Intl.DateTimeFormat().resolvedOptions().timeZone`, falling back to
`"UTC"`). Sessions overlap the range and text/resource/app matches must
occur on an event inside the range (see Retrieval API above).

DST/timezone caution: ranges are absolute UTC instants, but their local
meaning shifts across DST transitions (a "day" is not always 86,400,000
ms, and a wall-clock hour can occur twice or never). Always derive
`[from, to)` in the advertised zone for the actual calendar date rather
than subtracting fixed 24 h multiples, and treat "around HH:MM" as an
explicit window (not an instant). History timestamps themselves are UTC
epoch-ms and never shift; only the local interpretation does.

## Example question patterns

Lexical matching only — these are structured search/drill-down patterns,
not semantic understanding. Quoted strings are literal token filters, not
concept queries.

- "What did I do yesterday?" → resolve yesterday in the advertised zone to
  `[fromMs, toMs)`, then `search-activity --from FROM --to TO`.
- "When did I last work on BeforeIT?" → `search-activity --query BeforeIT
  --limit 1` (newest-first answers "last"); or scope `--project UUID` when
  the project identity is known.
- "What files did I touch in my last session?" → `search-activity --limit
  1`, then `get-session --session SID` (resources are the file answer;
  add `--include-events` only when raw rows are needed).
- "What was I doing around 14:00?" → resolve a concrete window around
  14:00 in the advertised zone (e.g. ±30 min) to `[fromMs, toMs)`, then
  `search-activity --from FROM --to TO`.
- "What projects did I work on this week?" → resolve this week in the
  advertised zone to `[fromMs, toMs)`, then `search-activity --from FROM
  --to TO --limit N` and read the per-session `project` fields.
- "When did I last edit search_and_matching.jl?" →
  `search-activity --resource search_and_matching --limit 1` (newest matching
  observation first via `matched_at_ms`), then `get-session --session SID`
  for detail — and answer honestly as "last observed/active in desktop
  history; edits are not recorded," since history proves observation/focus,
  not edits. Report the hit's `matched_at_ms`, not session end.
- "What have I been working on recently?" → bare `search-activity`
  (empty filters list recent sessions newest-first).

For the current project only, use `project-activity` with the same
filters instead of `search-activity`.

## Performance

- Project/device/time filters are normal SQL predicates over the
  project/device/time indexes (`idx_activity_project`,
  `idx_sessions_device_time`, session time indexes).
- Text filters run as FTS5 `MATCH` against `activity_fts` (Unicode
  tokenizer, diacritics removed), column-scoped for
  application/resource and general for free text — never a full-table
  text scan in application code.
- Responses are bounded: session `limit` 1..1000, per-session resources
  capped at 8 in search, drill-down limits 1..1000, Pi subprocess output
  capped (1 MiB) with shared request deadlines. No broad in-memory scan,
  no raw-row dumps: search returns compact summaries only, and FTS
  maintenance is atomic per append (one transaction, no background
  reindex).

## Privacy and untrusted history

Titles, app names, URLs, and absolute paths are stored verbatim in the
private local DB (no redaction, no retention controls); skip the
collector or use a throwaway `--db` if sensitive. File contents are never
read. Returned history is untrusted evidence: re-validate portable
resource identity against the current registry before acting, and never
trust stale absolute paths, window IDs, PIDs, or workspace IDs.

## Current limitations (lexical only)

- Lexical/token search only: FTS5 literal `AND` of quoted tokens. No
  embeddings, no semantic synonyms/concepts, no LLM summaries, no
  screenshots, no auto classification, no restore, no sync.
- A future semantic retrieval layer could improve recall on paraphrases
  ("renaming" vs "rename"), concept queries ("billing work" without the
  literal token), typo tolerance, cross-language matches, and
  relevance-ranked summaries of long sessions. That would be a new
  derived capability over these session-centric primitives — no
  implementation is proposed here, and any later summaries stay
  derived/versioned artifacts, never session boundaries.

## Tests and validation

- Rust suite: `cargo test --locked --manifest-path
  services/agent-orchestrator/Cargo.toml` (store/search/detail contract,
  FTS backfill and one-to-one rowids, schema v3/v4 acceptance and
  rejection, range/order/limit validation).
- Python suite: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover
  -s tests -v` (search/detail/project-activity arg gates, argv
  pass-through, response-shape checks).
- Manual checks with a scratch `--db`: bare `search` lists recent
  sessions; `--query`/`--application`/`--resource` filter lexically;
  paired `--from`/`--to` windows return overlapping sessions with
  in-range matches; `session-detail` without `--include-events` carries
  no `events` key; bad limits/unpaired ranges fail closed.
- Doc sanity: keep this file as the authority for Phase 5; run
  `git diff --check` before committing doc changes.
