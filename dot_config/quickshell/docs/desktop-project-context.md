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
  `file` / `cwd` / `git_root` / `git_remote`. Missing `project` in older
  JSON reads as `None`. Semantic equality includes `id`/`name`/
  `matched_by` but never the registry `revision`, so identical mappings
  at different revisions do not flap history.
- Unknown stays unknown: ambiguity or no match yields no association.
  There is no UI-selected project fallback.

## Match precedence (paths, then remotes)

Path precedence: `file > cwd > git_root > git_remote`, resolved lazily
in order (`src/project_context.rs`):

- Each candidate is tilde-expanded, then canonicalized with
  symlink-aware `..` semantics (the OS resolves `..` after symlinks).
  A nonexistent trailing suffix keeps the resolved existing prefix.
- Descendant matching honors component boundaries (`/a/b` does not match
  `/a/bc/d`). Longest mapped folder wins at each level.
- A tie between different projects at the same strength is ambiguous:
  no association, and no fallback to a weaker level.
- Titles are never used for matching.

Remote fallback (only when every path level missed):

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

## Store: schema v2, history is append-time

- Fresh databases are created at v2; schema is `activity(...,
  project_id TEXT)` + `idx_activity_project(project_id,
  observed_at_ms, id)`. `project_id` is extracted from
  `snapshot_json $.project.id` at append time (lowercase-normalized) and
  stored alongside the JSON.
- Writable open migrates v1 → v2 transactionally (rows preserved,
  `project_id` backfilled from stored JSON, never re-resolved).
  Read-only open accepts v1 or v2 without writes (v1 uses a
  `json_extract` fallback); foreign/newer schemas are rejected untouched.
- No retroactive reassignment: later registry edits, removes, or renames
  never rewrite stored rows. Historical associations do not remap;
  queries reflect the mapping at append time.
- Resource identity for `resources`: dedup key is the priority location
  key `file` else `url` else `cwd` else `page`; `git_branch` / `title` /
  `adapter` / `git_root` / `git_remote` never affect uniqueness. Rows
  stream newest-first and stop at `limit` distinct keys.

## CLI (`qs-desktop-context`)

Build (manual; nothing auto-installs, auto-builds, or auto-starts):

```sh
cargo build --locked --release --manifest-path services/agent-orchestrator/Cargo.toml
# services/agent-orchestrator/target/release/qs-desktop-context
```

```text
usage: qs-desktop-context [--db PATH] [collect]
       qs-desktop-context [--db PATH] history [--project UUID] [--limit N] [--from START_MS --to END_MS [--limit N]]
       qs-desktop-context current
       qs-desktop-context current-project
       qs-desktop-context [--db PATH] last-activity --project UUID
       qs-desktop-context [--db PATH] resources --project UUID [--limit N]
```

- `current`: fresh on-demand snapshot with default enrichment
  (app + project) and a focus recheck after enrichment (opaque id +
  application + title + client PID + workspace must agree, else
  unavailable). Never collector IPC, never DB, never a latest-row
  fallback.
- `current-project`: same fresh path, prints the `ProjectContext`
  object or `null`.
- `history --project UUID` / `last-activity --project UUID` /
  `resources --project UUID [--limit N]`: read-only indexed queries for
  one UUID. `--db` overrides the default DB; `current` ignores `--db`
  (warns).

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

## Pi tools (additive, 5)

`desktop_current_project`, `desktop_project_todos`,
`desktop_project_logseq_context`, `desktop_project_activity`,
`desktop_project_resources` (`.pi/extensions/desktop-agent.ts`). Each
call runs one bounded `scripts/desktop_projects.py` subprocess. The
fresh `current` identity is captured once per call only when no explicit
`--project` UUID is given; an explicit UUID skips the compositor query
and resolves history directly. Available in every scope (palette, project,
journal); they never touch `QS_PROJECT_PATH`, never mutate, never
infer. Additive read-tool allowlist entries only; scoped mutation rules
are unchanged.

## What this is not

No LLM, no embeddings, no screenshots, no session management, no
Resume integration. Titles/app names/URLs/paths are stored verbatim
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

## Next-phase constraints

A future search/session consumer must treat `resource` as best-effort
and stale-tolerant, re-validate before acting, and respect the existing
resource-id identity and ordering (`observed_at_ms` + `id`) and
retention behavior. No new semantic layer is introduced here.
