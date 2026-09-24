# Desktop project context (deterministic attribution)

Goal: deterministically attribute the existing desktop snapshot to an
existing project mapping. No new model, no inference, no mapping
mutation (history rows are still appended by the collector).

## Source of truth

- Canonical registry: `scripts/projects.py` UUID registry
  (`list_projects` / `projects.toml`). Location precedence:
  explicit `--projects-file` > `QUICKSHELL_PROJECTS_FILE` env >
  `<repo>/projects.toml`. The Rust resolver never parses `projects.toml`
  directly and never duplicates registry validation; it projects/caches
  the validated `list` JSON output only.
- Overlay: `DesktopContext.project` is `{id, name, matched_by}` only
  (`src/desktop_context.rs`). `matched_by` is one of
  `zotero_collection` / `file` / `cwd` / `git_root` / `logseq_page` /
  `git_remote`.
  Missing `project` in older JSON reads as `None`. Semantic equality
  includes `id`/`name`/`matched_by` but never the registry `revision`, so
  identical mappings at different revisions do not flap history.
- Unknown stays unknown: ambiguity or no match yields no association.
  There is no UI-selected project fallback.

## Match precedence (explicit collection, then paths, then remotes)

Explicit collection precedence: `zotero_collection > file > cwd > git_root
> logseq_page > git_remote` (`src/project_context.rs`, strict projection of
the authoritative `scripts/projects.py` `zotero_collection` field):

- Registry shape (optional, `null`/omitted when unlinked): `{server_id,
  library_type ("user"|"group"), library_id (digit string; user `"0"` is the
  server-bound personal-library alias), collection_key (8 uppercase alnum),
  include_subcollections (bool, default true)}`. Unknown sub-fields are
  rejected; TOML stores a linked value as an inline table.
- Reader identity (`ResourceContext.zotero`, metadata only): qualified
  `server_id` + `library_type`/`library_id` plus the stable parent
  `item_key` / opened `attachment_key`, current direct `collections` +
  `ancestor_collections`, `version` (freshness only), and a stable
  `zotero://select/...` URI. Page turns, titles, versions, and URIs never
  affect matching or session identity.
- Server/library must agree exactly, except registry `user/"0"` matches any
  user library on the same server. Then the entry's `collection_key` must be
  in the reader's direct `collections` — or, when `include_subcollections`
  is true, in `ancestor_collections` (descendants included via the published
  ancestors; `false` matches direct membership only).
- Exactly one distinct claiming project wins (`matched_by:
  "zotero_collection"`). Two distinct claimants are ambiguous with **no file
  fallback**; zero claims fall through to existing file/cwd/git matching.
  Collection reads touch no filesystem and spawn no subprocess.
- Collector binding for the reader itself is verified and fail-closed (see
  `services/agent-orchestrator/integrations/zotero/README.md`): the Zotero
  plugin cannot observe native window handles (local `/api/` exposes library
  data only, `Zotero.Reader` exposes no OS handle), so a tiny bridge samples
  `hyprctl activewindow` in the same tick and writes the verified
  `QS_ZOTERO_CONTEXT_FILE` (`window_id` + `pid`, 30 s freshness, 60 s
  last-good; closed/stale/mismatched readers evict; no title fallback, no
  SQLite/cloud/content reads).

Path precedence: `file > cwd > git_root > logseq_page > git_remote`,
resolved lazily in order (`src/project_context.rs`):

- Each candidate is tilde-expanded, then canonicalized with
  symlink-aware `..` semantics (the OS resolves `..` after symlinks).
  A nonexistent trailing suffix keeps the resolved existing prefix.
- Descendant matching honors component boundaries (`/a/b` does not match
  `/a/bc/d`). Longest mapped folder wins at each level.
- A tie between different projects at the same strength is ambiguous:
  no association, and no fallback to a weaker level.
- Window titles are never used for matching, with one explicit
  exception: the Logseq page level below, which matches the focused
  Logseq page extracted from the window title exactly against linked
  registry pages (never a fuzzy title search).
- The `file` level is fed by application providers. A focused Okular
  window contributes the currently displayed PDF through Okular's
  per-process D-Bus service (`org.kde.okular-<PID>`, or the
  `org.kde.okular.Instance_*` name under Flatpak; objects `/okular`,
  `/okular2`, ... answering `currentDocument`), so a PDF inside a
  registered folder matches at `file` without any title guessing.
  Multi-tab windows bind only when the window title uniquely matches one
  tab's file stem; ambiguous tabs and the welcome screen bind nothing
  (fail closed, evicting any stale last-good document). No git metadata
  is attached to PDFs.

Logseq page level (between `git_root` and `git_remote`):

- Source is the title-only `logseq-title` extraction (`src/app_context.rs`):
  the `<page> - Logseq` suffix form when present, otherwise the bare
  trimmed title, carried as `ResourceContext.page`.
- The claim matches exactly (case-sensitive) against a linked registry
  row's derived page label (from `logseq_path` exactly like
  `scripts/logseq_common.py page_name`: strip the `pages/` prefix and
  `.md` suffix, percent-decode `%XX`, replace `___` with `/`) OR its
  display name (registry names mirror page labels, so stale `logseq_path`
  rows keep matching after underscore->space renames). The registry JSON
  `page` field is the project name, never page identity, and is ignored.
- Only rows with a non-empty derived page label are eligible (name-only
  rows never match). Exactly one distinct claiming project wins
  (`matched_by: "logseq_page"`); a tie between different projects is
  ambiguous with no remote fallback.

Remote fallback (only when every path and page level missed):

- Canonical GitHub forms only: HTTPS / SSH / scp normalized to
  `https://github.com/owner/repo` (single trailing slash / single `.git`
  stripped, `www.` normalized, owner/repo `[A-Za-z0-9_.-]+`,
  case-insensitive compare). Credentials are stripped and never stored
  or logged.
- Deterministic claim rule: normalize the repo's URLs, then compute the
  distinct *registered* projects claimed. Exactly one distinct claimed
  project wins — even with other unmapped remotes (a fork with an
  unmapped upstream still matches the mapped origin). Zero claims, or
  claims spanning more than one distinct project (conflicting mapped
  remotes), mean unknown.
- Discovery is real local git config only:
  `git -C <root> config --local --includes --null --get-regexp '^remote\..*\.url$'`
  (git's own config semantics, so comments/quoting/includes/linked worktrees via
  `commondir` behave correctly). No network, no shell, bounded
  time/output. Ambient Git environment spoofing is sanitized
  (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_CONFIG_*` removed) and remote-count
  overflow fails closed. Credentials are never persisted.

## Caching

- Registry: the bounded `list` subprocess runs only when registry
  metadata changes (path, device, inode, mtime/ctime, length). An
  unchanged mapping never spawns Python per refresh. Failures fail
  closed (no association, never stale mappings) with a brief 5 s retry
  TTL. A missing registry file reads as an empty mapping.
- Folder projection: raw `local_folder` strings are stored; each needed
  mapping is resolved lazily per precedence level with a short 5 s TTL
  (bounded to 256 entries). Symlink retargets converge within the TTL
  without spawning Python.
- Remotes: discovery (including negative/empty) is cached with a 30 s
  TTL plus best-effort git-config/`commondir` metadata invalidation,
  bounded to 64 repos. Pure path matches run no additional Git remote
  subprocess (the first fresh query still loads a present registry via the
  validated Python helper; a missing registry file reads as an empty
  mapping with no subprocess).

## Store: schema v4 (v3 migratable to v4 only), history is append-time

- Fresh databases are created at v4; schema is `activity(...,
  project_id TEXT, event_id TEXT, device_id TEXT, session_id TEXT)` +
  `idx_activity_project(project_id, observed_at_ms, id)` plus the
  deterministic work-session tables `sessions`, `session_resources`,
  the singleton `device_info` (durable local device identity), the
  application-maintained FTS5 `activity_fts` table, and
  `idx_sessions_device_time`. Full Phase 5 contract:
  `docs/desktop-history-search.md`.
  `project_id` is extracted from `snapshot_json $.project.id` at append
  time (lowercase-normalized) and stored alongside the JSON. Full
  session contract: `docs/desktop-work-sessions.md`.
- The store accepts exact coherent v4 and migrates exact coherent v3 only
  when the restarted collector holds the per-database lock (schema v3 is
  migratable to v4 only; ordinary writable/read-only opens do not migrate;
  v1/v2 remain unsupported with no migration or read-only compatibility).
  Foreign/newer schemas are likewise rejected untouched.
- No retroactive reassignment: later registry edits, removes, or renames
  never rewrite stored rows. Historical associations do not remap;
  queries reflect the mapping at append time.
- Resource identity for `resources`/`session_resources`: a bound Zotero reader
  aggregates by its stable document key
  (`portable:zotero:<server>:<type>/<lib>:item:<item>:att:<att>` — titles,
  versions, collections, URIs, and page turns never affect it); otherwise the
  priority location key `file` else `url` else `cwd` else `page`;
  `git_branch` / `title` / `adapter` / `git_root` / `git_remote` never affect
  uniqueness. Rows stream newest-first and stop at `limit` distinct keys.
  FTS search text additionally indexes the Zotero server/library/item/
  attachment/collection keys and URI (metadata only, never content).

## CLI (`qs-desktop-context`)

Build (manual; nothing auto-installs, auto-builds, or auto-starts):

```sh
cargo build --locked --release --manifest-path services/agent-orchestrator/Cargo.toml
# services/agent-orchestrator/target/release/qs-desktop-context
```

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
       qs-desktop-context [--db PATH] search [--project UUID] [--application TEXT] [--resource TEXT] [--device 32HEX] [--query TEXT] [--from START_MS --to END_MS] [--limit N]
       qs-desktop-context [--db PATH] session-detail --session SESSION_ID [--resource-limit N] [--include-events [--event-limit N]]
```

- `current`: fresh on-demand snapshot with default enrichment
  (app + project) and a focus recheck after enrichment (opaque id +
  application + title + client PID + workspace must agree, else
  unavailable). Never collector IPC. When the snapshot is focusless
  (`available`, no focused window — e.g. a Quickshell layer surface holds
  focus while the planner/popup is open), up to 64 newest persisted rows
  are read best-effort (strictly read-only; `--db` or the default DB)
  newest→oldest to find the carry source — the newest row that actually
  carries a `project` or non-empty `resource`, skipping un-enriched rows
  for the same focused window (raw `focus`/`title` rows land before
  their enriched `context` row) — and its `resource`/`project` are
  retained inside the session interruption grace, so a brief focus gap
  never blanks the current project. Expiry is anchored on the durable
  focusless streak clock (`focusless_since_ms`, stamped on every
  available focusless observation; `retained_since_ms` still means
  retention was applied); repeated focusless events never extend the
  window and there is no schema change. The walk returns the fresh
  snapshot unchanged on three hard boundaries — an unavailable row, a
  workspace mismatch with the candidate, or an intervening windowed row
  that carried nothing (unknown project) — as well as on an expired
  streak or an unreadable/missing DB. A popup held past the grace still
  clears by design; while the collector is not running the read-only
  live path cannot persist the streak clock, so a windowed carry source
  re-anchors fresh each poll and the grace effectively does not expire
  until the collector persists a row.
- `current-project`: same path, prints the `ProjectContext`
  object or `null`.
- `history --project UUID` / `last-activity --project UUID` /
  `resources --project UUID [--limit N]`: read-only indexed queries for
  one UUID. `--db` overrides the default DB for every command; for
  `current`/`current-project` it only feeds the focusless retention
  fallback (current modes still work without a database or HOME). Work-session queries (`current-session`, `sessions`,
  `last-session`, `session-resources`, `session-events`) are read-only
  persisted-DB queries returning session objects with query-time
   `effective_status` (`active`/`interrupted`/`stale`/`closed`); full
  contract and examples: `docs/desktop-work-sessions.md`. Session-centric
  retrieval (`search`, `session-detail`) is session-first with no raw
  events by default; full contract and examples:
  `docs/desktop-history-search.md`.

Examples (scratch DB first; a pre-existing non-private leaf is rejected,
not repaired):

```sh
BIN=services/agent-orchestrator/target/release/qs-desktop-context
install -d -m 0700 /tmp/qs-test
"$BIN" --db /tmp/qs-test/activity.db history --limit 5
UUID='existing-project-uuid'
"$BIN" current
"$BIN" current-project
"$BIN" --db /tmp/qs-test/activity.db history --project "$UUID" --limit 5
"$BIN" --db /tmp/qs-test/activity.db last-activity --project "$UUID"
"$BIN" --db /tmp/qs-test/activity.db resources --project "$UUID" --limit 5
"$BIN" --db /tmp/qs-test/activity.db current-session
"$BIN" --db /tmp/qs-test/activity.db sessions --project "$UUID" --limit 5
"$BIN" --db /tmp/qs-test/activity.db last-session --project "$UUID"
```

## Python backend (`scripts/desktop_projects.py`)

Read-only views over the fresh `current` identity + canonical registry
+ `read_page` reuse. No inference, no writes, no selected-project
fallback. Overrides: `--projects-file`, `--graph`, `--db`,
`--desktop-bin` (plus `QUICKSHELL_PROJECTS_FILE`, `LOGSEQ_GRAPH`,
`QS_DESKTOP_DB`/`QS_DESKTOP_CONTEXT_DB`, `QS_DESKTOP_CONTEXT_BIN`).

```sh
python3 scripts/desktop_projects.py current-project
python3 scripts/desktop_projects.py todos
python3 scripts/desktop_projects.py logseq-context
python3 scripts/desktop_projects.py recent-activity
python3 scripts/desktop_projects.py last-activity --project 'existing-project-uuid'
python3 scripts/desktop_projects.py resources --project 'existing-project-uuid' --limit 5
python3 scripts/desktop_projects.py current-session
python3 scripts/desktop_projects.py sessions [--project 'existing-project-uuid'] [--limit 5] [--from START_MS --to END_MS]
python3 scripts/desktop_projects.py last-session --project 'existing-project-uuid'
python3 scripts/desktop_projects.py session-resources --session '<32-hex-session-id>' --limit 5
python3 scripts/desktop_projects.py session-events --session '<32-hex-session-id>' --limit 5
python3 scripts/desktop_projects.py current-context
python3 scripts/desktop_projects.py search-activity [--project UUID] [--application TEXT] [--resource TEXT] [--device 32HEX] [--query TEXT] [--from START_MS --to END_MS] [--limit 5]
python3 scripts/desktop_projects.py get-session --session '<32-hex-session-id>' [--resource-limit 5] [--include-events [--event-limit 5]]
python3 scripts/desktop_projects.py project-activity [--project UUID] [--application TEXT] [--resource TEXT] [--device 32HEX] [--query TEXT] [--from START_MS --to END_MS] [--limit 5]
```

- `current-project` captures `current` once per request and looks the
  reported id up in the fresh registry by stable id; the current
  registry `name`/`logseq_path` wins over any historical name.
- Identity vs linkage: any present registry entry (including name-only)
  is a valid identity for `recent-activity` / `last-activity` /
  `resources`. Only `todos` / `logseq-context` additionally require a
  non-empty `logseq_path` and reuse the existing `read_page` task
  parser. A name-only project (valid, has history) returns explicit
  `name-only-no-linkage` with empty Logseq data and no graph
  requirement.
- Explicit `--project UUID` allows historical queries; unknown/deleted
  UUIDs still query history/resources/last-activity. Unassociated/stale
  defaults return empty with no DB query. `current` failures are
  single-line `error:` on stderr, exit 1. The binary is never built or
  started automatically; a missing binary names its path plus the manual
  cargo build command.

## Pi tools (7 coherent history tools)

`desktop_current_context`, `desktop_project_todos`,
`desktop_project_logseq_context`, plus the session-centric retrieval
tools `desktop_current_session`, `desktop_search_activity`,
`desktop_get_session`, `desktop_project_activity`
(`.pi/extensions/desktop-agent.ts`; authoritative tool contract:
`docs/desktop-history-search.md`). Each
call runs one bounded `scripts/desktop_projects.py` subprocess. The
fresh `current` identity is captured once per call only when no explicit
`--project` UUID is given; an explicit UUID skips the compositor query
  and resolves history directly. Available in every scope (palette, project,
journal); they never touch `QS_PROJECT_PATH`, never mutate, never
infer. The work-session tools serve deterministic DB-derived activity
clusters (32-hex `session_id`) and never list, switch, resume, or mutate
Pi agent chat sessions (the `SessionManager`/`desktop-sessions` picker
is a separate conversational-session system). Additive read-tool
allowlist entries only; scoped mutation rules
are unchanged.

## Deterministic work sessions (Phase 4)

Implemented. Full contract: `docs/desktop-work-sessions.md`.

## What this is not

No LLM, no embeddings, no screenshots, no
sync. Deterministic work sessions exist (query-only activity clusters;
see `docs/desktop-work-sessions.md`) — there is still no session
mutation and no Pi chat-session management. Resume is a separate
read-only backend over these primitives (`docs/desktop-resume.md`;
command-palette and read-only Pi plan integration implemented), not part of
attribution. Titles/app names/URLs/paths are stored verbatim
(no redaction, no retention controls); automatic attribution never reads
working file contents — only an explicitly requested Logseq view reads
the linked page via the existing `read_page` API.

## Limitations

- Collector enrichment is async: the initial base row is bare by design;
  the enriched `context` row arrives later. Focus changes discard
  in-flight enrichment for the old focus.
- Fresh `current` queries spawn the validated helper; its cache is
  process-local (thread-local resolver), so each fresh process reloads
  metadata once.
- `resources` streams newest-first and stops at `limit` distinct keys;
  when every row maps to the same key the scan can visit the full
  project history.
- Historical ids persist: remove/rename never rewrites stored
  `project_id`; deleted UUIDs remain queryable, Logseq views follow the
  current registry mapping only.
- Filesystem `stat`/`canonicalize` are blocking with no timeout, so a
  hung network mount can stall the calling worker thread; exposure is
  limited by lazy per-level matching, not hard-bounded.
- The binary locates the registry helper at a compile-time repo path
  (`<manifest>/../../scripts/projects.py`); relocating the checkout
  needs a rebuild, not merely a registry-path override.

## Retrieval consumer constraints (Phase 5 implemented)

Phase 5 session-centric retrieval is implemented (authoritative guide:
`docs/desktop-history-search.md`). Its consumers already follow these
rules: treat `resource` as best-effort
and stale-tolerant, re-validate before acting, and respect the existing
resource-id identity and ordering (`observed_at_ms` + `id`) and
retention behavior. Session consumers must use sessions as the primary
unit but keep raw `session-events` for audit, join on stable
`session_id`/`event_id`/`device_id`, resolve portable resource identity
against the current registry before acting (never trust stale absolute
paths/window IDs/PIDs/workspace IDs), and treat any later LLM summaries
as derived/versioned artifacts — never session boundaries. No new
semantic layer is introduced here.
