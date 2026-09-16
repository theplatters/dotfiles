//! SQLite append-only activity history plus deterministic work sessions
//! plus session-centric searchable history (Phase 5).
//!
//! Schema (versioned):
//! - `schema_version(version INTEGER PRIMARY KEY)` holding [`SCHEMA_VERSION`]
//!   as a singleton row (exactly 4).
//! - v4: all v3 tables/columns unchanged (`activity`, `sessions`,
//!   `session_resources`, `device_info` with identical column bytes and
//!   nullability) plus:
//!   - application-maintained content-bearing FTS5 table `activity_fts`
//!     (one row per activity, `rowid = activity.id`):
//!     `session_id UNINDEXED, resource_key UNINDEXED, project, application,
//!     window_title, workspace, resource, metadata`
//!     with `tokenize='unicode61 remove_diacritics 2'` (no embeddings).
//!     Search text includes the project name, focused app/title, workspace
//!     name, resource adapter/file/cwd/git_root/git_branch/git_remote/
//!     url/page/title, and kind/source metadata. No triggers; the writer
//!     maintains FTS rows explicitly inside the append transaction.
//!   - normal index
//!     `idx_sessions_device_time(device_id, end_ms, start_ms, session_id)`
//!     for structured device+time filtering.
//!   Fresh databases are created at v4. Writable opens migrate an exact
//!   coherent v3 database to v4 in one transaction, but ONLY with proof of
//!   ownership of the per-DB [`crate::desktop_lock::DesktopLock`] (the
//!   collector passes its already-held guard, so no old v3 writer can still
//!   be appending after migration); ordinary opens reject v3 with a clear
//!   collector lock/restart error. Any malformed snapshot/FTS failure rolls
//!   back untouched. Read-only opens accept exact coherent v4 only and
//!   report a clear incompatible-schema error for v3. There is no v1/v2
//!   migration and no read-only compatibility for older schemas.
//!
//! The snapshot is stored as JSON alongside timestamp/kind/source so future
//! workers can evolve the generic context without migrations. Sessions never
//! copy full snapshots: they aggregate project/bounds/counts/status/config
//! plus a distinct-application list and compact per-resource aggregates.
//!
//! Query contract (UTC epoch milliseconds):
//! - `recent_activity(limit)`: newest first, `ORDER BY observed_at_ms DESC,
//!   id DESC`, bounded by `limit`.
//! - `activity_in_range(start_ms, end_ms, limit)`: `start_ms` inclusive,
//!   `end_ms` exclusive (`observed_at_ms >= start AND observed_at_ms < end`),
//!   `ORDER BY observed_at_ms ASC, id ASC`, bounded by `limit`. Equal
//!   timestamps order deterministically by `id`.
//! - `recent_activity_for_project(id, limit)` / `last_activity_for_project(id)`:
//!   same ordering as `recent_activity` but filtered to one project UUID
//!   (`project_id = ?`, indexed).
//! - `activity_in_range_for_project(id, start, end, limit)`: range + project
//!   filter, oldest-first.
//! - `recently_used_resources_for_project(id, limit)`: unique resources for
//!   one project, newest-first by latest observation (legacy priority key,
//!   unchanged for compatibility).
//! - Sessions: `current_session(now_ms)` (newest open only when
//!   `now_ms < end_ms + stored_gap`, else `None` without mutating),
//!   `recent_sessions` / `recent_sessions_for_project` (newest-first by
//!   end/start/id), `sessions_in_range` / `sessions_in_range_for_project`
//!   (overlap `end >= start AND start < end`, oldest-first),
//!   `last_session_for_project`, `session_resources` (newest-first by
//!   last_seen), `session_events` (oldest-first by observed/id).
//! - Search (Phase 5): `search_sessions(&SessionSearchQuery)` is
//!   session-centric over the FTS index. Optional paired UTC epoch-ms
//!   `start_ms`/`end_ms` (start inclusive, end exclusive), optional project
//!   UUID / device 32-hex (normal SQL predicates), optional bounded
//!   application / resource / free-text query (escaped/literal FTS5 `MATCH`,
//!   column-scoped for app/resource, general for free text). FTS/range
//!   session discovery traverses FTS matches ONCE (single grouped
//!   `activity ⨝ activity_fts` scan with one `MATCH`, event-level range,
//!   grouped by session); structured no-FTS queries stay indexed/bounded.
//!   Text matches inside a range come from activity rows in that range, not
//!   merely another event in an overlapping session. FTS/range hits carry
//!   `matched_at_ms` (newest matching observation) and order newest matching
//!   observation first (then session end/start/id), so `limit = 1` answers
//!   "last matching observation" across devices; project/device-only and
//!   no-filter searches carry `matched_at_ms = None` with session-recency
//!   order. Per-session resources favor actually-matched keys ranked by
//!   newest matching activity (deterministic) and are capped at
//!   [`MAX_SEARCH_RESOURCES_PER_SESSION`]. No raw events are returned.
//! - Session detail (Phase 5): `session_detail(...)` returns the session
//!   header plus bounded resources and optionally bounded raw events (only
//!   fetched when explicitly requested).
//!
//! Read-only performance contract: read-only opens validate structure
//! (objects, tables, shapes, FTS declaration, required index shapes) plus
//! cheap singleton/version/local-device metadata only — never the
//! full-history data-coherence scans. Every `search`/`history`/`detail`
//! process invocation therefore stays fast on large histories; rows actually
//! touched still validate through the row parsers. Writable opens and
//! migration keep the deep exact validation (session projections,
//! provenance, FTS one-to-one).
//!
//! Security contract (local, reasonably scoped):
//! - Database files are created `0600` from the outset; existing files must
//!   be regular files owned by the current user with private permissions and
//!   must not be symlinks. Sidecars (`-wal`, `-shm`, `-journal`) follow the
//!   same rule when present.
//! - Schema is validated authoritatively before any mutation. Foreign, newer,
//!   or multi-row `schema_version` content is rejected with no modifications.
//!   Unique/PK autoindexes are allowlisted by exact name/shape only (FTS5
//!   shadow tables accounted explicitly). SQLite-owned names use literal
//!   `sqlite_*` prefix matching (`GLOB`), never `LIKE 'sqlite_%'`.
//! - Initial setup and lock-gated v3→v4 migration are transactional
//!   (`BEGIN IMMEDIATE`, prevalidation inside the transaction). History
//!   queries use the read-only constructor, which never creates files,
//!   schemas, or journals and validates inside a single read snapshot.
//! - Every raw append plus its session assignment/update/resource aggregation
//!   plus its FTS row is one SQLite transaction: if any step fails the raw
//!   insert rolls back so collector FIFO retry semantics stay true.
//! - Collector operation: stop the old collector and let its bounded (~2 s)
//!   final drain finish before restarting to migrate. The migrating restart
//!   holds the per-DB lock for life and passes it to the store, proving no
//!   old v3 writer remains.

use crate::desktop_context::{DesktopContext, ResourceContext};
use crate::desktop_lock::DesktopLock;
use crate::desktop_paths::lock_path_for;
use crate::desktop_session::SessionConfig;
use rusqlite::{params, Connection, OpenFlags, OptionalExtension};
use std::path::Path;

/// Current schema version. Fresh databases are created at v4. Only the
/// lock-capability writable open migrates an exact coherent v3 to v4;
/// ordinary writable opens reject v3 (collector lock/restart required).
/// Read-only opens accept exact coherent v4 only (v3 gets a clear
/// migration-required error).
pub const SCHEMA_VERSION: i64 = 4;
/// Last lock-migratable schema version (exact coherent v3 migrates to v4 on
/// the lock-capability writable open only; read-only never migrates).
pub const MIGRATABLE_V3_VERSION: i64 = 3;
/// Upper bound for `limit` in read queries (keeps responses bounded).
pub const MAX_QUERY_LIMIT: i64 = 1000;
/// Upper bound for stored snapshot JSON bytes (fail-closed append).
pub const MAX_SNAPSHOT_JSON_BYTES: usize = 64 * 1024;
/// Upper bound for kind/source strings.
pub const MAX_TAG_CHARS: usize = 64;
/// Upper bound for search text fields (application/resource/free-text),
/// in chars. Keeps FTS expressions bounded.
pub const MAX_SEARCH_TEXT_CHARS: usize = 256;
/// Cap for per-session resources returned by session-centric search.
pub const MAX_SEARCH_RESOURCES_PER_SESSION: i64 = 8;

/// One persisted activity row.
///
/// `event_id`/`device_id`/`session_id` are Phase 4 provenance: always
/// populated by code for v3 rows (nullable in DDL only for compatibility
/// with the already-created v3 database).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ActivityRecord {
    pub id: i64,
    pub observed_at_ms: i64,
    pub kind: String,
    pub source: String,
    pub snapshot_json: String,
    pub event_id: Option<String>,
    pub device_id: Option<String>,
    pub session_id: Option<String>,
}

impl ActivityRecord {
    /// Parse the stored snapshot back into a generic context.
    pub fn snapshot(&self) -> Result<DesktopContext, StoreError> {
        serde_json::from_str(&self.snapshot_json)
            .map_err(|e| StoreError::InvalidArgument(format!("bad snapshot_json: {e}")))
    }

    /// Immutable historical project association for this row.
    ///
    /// This helper parses the snapshot and returns `snapshot.project.id`
    /// (empty/absent as `None`). It never re-resolves against the current
    /// registry: historical associations do not remap.
    pub fn project_id(&self) -> Option<String> {
        let ctx: DesktopContext = serde_json::from_str(&self.snapshot_json).ok()?;
        ctx.project.map(|p| p.id).filter(|s| !s.is_empty())
    }
}

/// One deduped recently-used resource for a project.
///
/// `resource` is the latest stored [`ResourceContext`] for one location key,
/// `observed_at_ms`/`activity_id` locate the newest row that carried it.
/// Ordering is newest-first (`observed_at_ms DESC, activity_id DESC`).
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct ProjectResource {
    pub resource: ResourceContext,
    pub observed_at_ms: i64,
    pub activity_id: i64,
}

/// One materialized work session.
///
/// `project_id`/`project_name` are the session's stable project association
/// (`None` for unresolved-only sessions). `start_ms`/`end_ms` bound the
/// associated activity timestamps (`min`/`max`, so clock rollback never
/// corrupts ranges). `first_activity_id`/`last_activity_id` bound row ids in
/// arrival order, `event_count` counts associated events. `status` is the
/// persisted `open`/`closed` state, `ended_reason` is
/// `inactivity`/`project_switch`/`interruption` (or `None` while open).
/// `unresolved_start_ms` is the persisted start of the currently absorbed
/// unresolved run, if any. `gap_ms`/`interruption_ms` are the thresholds
/// applied at creation so historical boundaries never change. `applications`
/// is the sorted distinct application list derived from associated snapshots.
///
/// Query-time liveness never mutates: use [`SessionRecord::is_active_at`] /
/// [`SessionRecord::effective_status_at`] with one captured `now` per
/// response.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct SessionRecord {
    pub session_id: String,
    pub device_id: String,
    pub project_id: Option<String>,
    pub project_name: Option<String>,
    pub start_ms: i64,
    pub end_ms: i64,
    pub first_activity_id: i64,
    pub last_activity_id: i64,
    pub event_count: i64,
    pub status: String,
    pub ended_reason: Option<String>,
    pub unresolved_start_ms: Option<i64>,
    pub gap_ms: i64,
    pub interruption_ms: i64,
    pub applications: Vec<String>,
}

impl SessionRecord {
    /// Query-time effective status of an open session at `now_ms`:
    /// `active`, `interrupted` (an absorbed unresolved run has reached the
    /// stored grace: `now - unresolved_start >= interruption`, so grace `0`
    /// deactivates at the first unresolved event), or `stale` (the stored
    /// inactivity gap expired: `now >= end + gap`, mirroring the writer's
    /// gap-first precedence). Persisted `closed` sessions report `closed`.
    /// Never mutates; callers capture one `now` per response.
    pub fn effective_status_at(&self, now_ms: i64) -> &'static str {
        if self.status != "open" {
            return "closed";
        }
        if now_ms >= self.end_ms.saturating_add(self.gap_ms) {
            return "stale";
        }
        if let Some(start) = self.unresolved_start_ms {
            if now_ms.saturating_sub(start) >= self.interruption_ms {
                return "interrupted";
            }
        }
        "active"
    }

    /// True exactly when [`SessionRecord::effective_status_at`] is `active`.
    pub fn is_active_at(&self, now_ms: i64) -> bool {
        self.effective_status_at(now_ms) == "active"
    }
}

/// One compact aggregated session resource.
///
/// `resource_key` is the stable dedup key (`portable:<typed>` or
/// `local:<typed>`); exactly one of `portable_identity`/`local_identity` is
/// `Some`, keeping the families distinct. `resource` is the latest compact
/// [`ResourceContext`] for the key. Counts increment per associated event
/// carrying the identity.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct SessionResourceRecord {
    pub session_id: String,
    pub resource_key: String,
    pub kind: String,
    pub portable_identity: Option<String>,
    pub local_identity: Option<String>,
    pub resource: ResourceContext,
    pub occurrence_count: i64,
    pub first_seen_ms: i64,
    pub last_seen_ms: i64,
    pub first_activity_id: i64,
    pub last_activity_id: i64,
}

/// Session-centric search query (Phase 5).
///
/// - `start_ms`/`end_ms` must pair (both `Some` or both `None`); start is
///   inclusive, end is exclusive, UTC epoch-ms, `start <= end`.
/// - `project_id` is a UUID (normalized to lowercase for indexed lookup).
/// - `device_id` is a 32-hex identity (normalized to lowercase).
/// - `application` / `resource` / `query` are bounded nonempty texts
///   (trimmed, `1..=MAX_SEARCH_TEXT_CHARS` chars). They compile to
///   escaped/literal FTS5 expressions (column-scoped for
///   application/resource, general for free text) so user FTS syntax cannot
///   inject operators.
/// - `limit` bounds the number of *sessions* returned (`1..=MAX_QUERY_LIMIT`).
/// - No filter at all means recent sessions (newest-first).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SessionSearchQuery {
    pub start_ms: Option<i64>,
    pub end_ms: Option<i64>,
    pub project_id: Option<String>,
    pub application: Option<String>,
    pub resource: Option<String>,
    pub device_id: Option<String>,
    pub query: Option<String>,
    pub limit: i64,
}

/// One session-centric search hit: the session header, the newest matching
/// activity observation for that session (when the search involved FTS text
/// and/or a time range), plus compact meaningful resources
/// (actually-matched keys favored, capped at
/// [`MAX_SEARCH_RESOURCES_PER_SESSION`]). No raw events are returned.
///
/// `matched_at_ms` is `Some(MAX(activity.observed_at_ms))` over the matching
/// activities when FTS text and/or a range selected the sessions, and `None`
/// for project/device-only or no-filter searches (which keep session-recency
/// ordering).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SessionSearchResult {
    pub session: SessionRecord,
    pub matched_at_ms: Option<i64>,
    pub resources: Vec<SessionResourceRecord>,
}

/// Session detail: header plus bounded resources and optionally bounded raw
/// events. Raw events are only fetched when `events_included` is true.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SessionDetail {
    pub session: Option<SessionRecord>,
    pub resources: Vec<SessionResourceRecord>,
    pub events_included: bool,
    pub events: Vec<ActivityRecord>,
}

/// Validate a project id as the existing UUID identity.
///
/// Accepts canonical hyphenated UUIDs (`8-4-4-4-12` hex, any case, no
/// surrounding whitespace) and returns the lowercase canonical form for
/// indexed lookup. Anything else is [`StoreError::InvalidArgument`].
/// Historical rows keep their stored bytes; only the query is normalized
/// (lookup is exact on the normalized form, case-insensitive via
/// normalization since the registry stores lowercase).
pub fn normalize_project_id(id: &str) -> Result<String, StoreError> {
    if id.is_empty() {
        return Err(StoreError::InvalidArgument(
            "project id must be a UUID (got empty)".to_string(),
        ));
    }
    if id.trim() != id {
        return Err(StoreError::InvalidArgument(
            "project id must not have leading/trailing whitespace".to_string(),
        ));
    }
    if id.len() != 36 {
        return Err(StoreError::InvalidArgument(format!(
            "project id must be a UUID (36 chars, got {} chars)",
            id.len()
        )));
    }
    let b = id.as_bytes();
    for &pos in &[8usize, 13, 18, 23] {
        if b[pos] != b'-' {
            return Err(StoreError::InvalidArgument(format!(
                "project id must be a UUID (hyphen expected at position {pos})"
            )));
        }
    }
    for (i, &c) in b.iter().enumerate() {
        if matches!(i, 8 | 13 | 18 | 23) {
            continue;
        }
        if !c.is_ascii_hexdigit() {
            return Err(StoreError::InvalidArgument(
                "project id must be a UUID ([0-9a-fA-F-] only)".to_string(),
            ));
        }
    }
    Ok(id.to_lowercase())
}

#[derive(Debug)]
pub enum StoreError {
    InvalidArgument(String),
    Sqlite(String),
    /// Database file does not exist (history CLI reports empty, not fake rows).
    NotFound(String),
    /// Filesystem permission denied (distinct from missing).
    PermissionDenied(String),
    /// Foreign, newer, or otherwise incompatible schema. No modifications
    /// were made when this is returned from open.
    IncompatibleSchema(String),
    /// Symlink, non-regular, foreign-owner, or overly broad file/sidecar.
    UnsafePath(String),
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StoreError::InvalidArgument(m) => write!(f, "invalid argument: {m}"),
            StoreError::Sqlite(m) => write!(f, "sqlite error: {m}"),
            StoreError::NotFound(m) => write!(f, "not found: {m}"),
            StoreError::PermissionDenied(m) => write!(f, "permission denied: {m}"),
            StoreError::IncompatibleSchema(m) => write!(f, "incompatible schema: {m}"),
            StoreError::UnsafePath(m) => write!(f, "unsafe path: {m}"),
        }
    }
}

impl std::error::Error for StoreError {}

impl From<rusqlite::Error> for StoreError {
    fn from(e: rusqlite::Error) -> Self {
        StoreError::Sqlite(e.to_string())
    }
}

/// RAII immediate-transaction guard over a shared (`&`) [`Connection`].
///
/// `rusqlite::Connection::transaction()` requires `&mut`, which [`ActivityStore::append`]
/// cannot take (the collector holds a shared store), so this guard issues
/// `BEGIN IMMEDIATE` directly. It fails fast when the database is locked
/// (nothing persisted) and rolls back on drop unless [`ImmediateTx::commit`]
/// succeeded. A failed `COMMIT` is followed by an explicit `ROLLBACK` so a
/// half-open transaction is never left behind.
struct ImmediateTx<'a> {
    conn: &'a Connection,
    committed: bool,
}

impl<'a> ImmediateTx<'a> {
    fn begin(conn: &'a Connection) -> Result<Self, StoreError> {
        conn.execute_batch("BEGIN IMMEDIATE")
            .map_err(|e| StoreError::Sqlite(format!("begin append transaction: {e}")))?;
        Ok(Self {
            conn,
            committed: false,
        })
    }

    fn commit(mut self) -> Result<(), StoreError> {
        let res = self
            .conn
            .execute_batch("COMMIT")
            .map_err(|e| StoreError::Sqlite(format!("commit append: {e}")));
        // Suppress the drop-time rollback either way; on commit failure the
        // transaction may still be open, so roll back explicitly here.
        self.committed = true;
        if res.is_err() {
            let _ = self.conn.execute_batch("ROLLBACK");
        }
        // Prevent `Drop` from running after the explicit cleanup above.
        std::mem::forget(self);
        res
    }
}

impl Drop for ImmediateTx<'_> {
    fn drop(&mut self) {
        if !self.committed {
            let _ = self.conn.execute_batch("ROLLBACK");
        }
    }
}

/// Append-only SQLite activity store (synchronous, bounded) with sessions.
pub struct ActivityStore {
    conn: Connection,
    session_config: SessionConfig,
}

/// v3 DDL for fresh databases (transactional). Nullable `event_id` /
/// `device_id` / `session_id` on activity stay nullable for compatibility
/// with the already-created v3 database; code always populates them.
/// FTS5 DDL for the application-maintained content-bearing table.
/// One row per activity (`rowid = activity.id`). `session_id` and
/// `resource_key` are stored but UNINDEXED (not searchable); the six indexed
/// columns carry project name, focused application, window title, workspace
/// name, resource text, and kind/source metadata. Unicode tokenizer with
/// diacritics removal; no embeddings, no triggers, no prefix indexes.
fn activity_fts_ddl() -> &'static str {
    "CREATE VIRTUAL TABLE activity_fts USING fts5(session_id UNINDEXED, resource_key UNINDEXED, project, application, window_title, workspace, resource, metadata, tokenize='unicode61 remove_diacritics 2')"
}

/// v4 DDL for fresh databases (transactional). All v3 tables/columns keep
/// identical bytes/nullability; v4 adds the device+time index and the FTS5
/// table above. Nullable `event_id` / `device_id` / `session_id` on activity
/// stay nullable for compatibility with the already-created v3 database;
/// code always populates them.
fn v4_ddl() -> &'static str {
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
     CREATE TABLE IF NOT EXISTS activity (
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       observed_at_ms INTEGER NOT NULL,
       kind TEXT NOT NULL,
       source TEXT NOT NULL,
       snapshot_json TEXT NOT NULL,
       project_id TEXT,
       event_id TEXT,
       device_id TEXT,
       session_id TEXT
     );
     CREATE TABLE IF NOT EXISTS device_info (device_id TEXT PRIMARY KEY);
     CREATE TABLE IF NOT EXISTS sessions (
       session_id TEXT PRIMARY KEY,
       project_id TEXT,
       project_name TEXT,
       start_ms INTEGER NOT NULL,
       end_ms INTEGER NOT NULL,
       first_activity_id INTEGER NOT NULL,
       last_activity_id INTEGER NOT NULL,
       event_count INTEGER NOT NULL,
       status TEXT NOT NULL,
       ended_reason TEXT,
       gap_ms INTEGER NOT NULL,
       interruption_ms INTEGER NOT NULL,
       device_id TEXT NOT NULL,
       unresolved_start_ms INTEGER,
       applications_json TEXT NOT NULL
     );
     CREATE TABLE IF NOT EXISTS session_resources (
       session_id TEXT NOT NULL,
       resource_key TEXT NOT NULL,
       kind TEXT NOT NULL,
       portable_identity TEXT,
       local_identity TEXT,
       latest_resource_json TEXT NOT NULL,
       occurrence_count INTEGER NOT NULL,
       first_seen_ms INTEGER NOT NULL,
       last_seen_ms INTEGER NOT NULL,
       first_activity_id INTEGER NOT NULL,
       last_activity_id INTEGER NOT NULL,
       PRIMARY KEY (session_id, resource_key)
     );"
}

/// v4 indexes (transactional, `IF NOT EXISTS`). The seven v3 indexes keep
/// identical shapes plus the new device+time index for structured
/// device+time filtering.
fn v4_indexes_ddl() -> &'static str {
    "CREATE INDEX IF NOT EXISTS idx_activity_time ON activity (observed_at_ms, id);
     CREATE INDEX IF NOT EXISTS idx_activity_project ON activity (project_id, observed_at_ms, id);
     CREATE UNIQUE INDEX IF NOT EXISTS idx_activity_event_id ON activity (event_id);
     CREATE INDEX IF NOT EXISTS idx_activity_session ON activity (session_id, observed_at_ms, id);
     CREATE INDEX IF NOT EXISTS idx_sessions_time ON sessions (end_ms, start_ms, session_id);
     CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions (project_id, end_ms, start_ms, session_id);
     CREATE INDEX IF NOT EXISTS idx_sessions_device_time ON sessions (device_id, end_ms, start_ms, session_id);
     CREATE INDEX IF NOT EXISTS idx_session_resources_seen ON session_resources (session_id, last_seen_ms, resource_key);"
}

/// FTS text derivation shared by append and v3→v4 backfill.
///
/// Returns `(project, application, window_title, workspace, resource,
/// metadata, resource_key)` where `resource_key` is the stable
/// [`crate::desktop_session::resource_identity`] key for the event (or `""`
/// when no meaningful resource). Search text includes the project name,
/// focused app/title, workspace name, resource
/// adapter/file/cwd/git_root/git_branch/git_remote/url/page/title plus
/// Zotero server/library/item/attachment/collection keys and URI, and
/// kind/source metadata. No content extraction: only the published
/// metadata above is indexed.
fn fts_row_for(
    ctx: &DesktopContext,
    kind: &str,
    source: &str,
    event_project_id: Option<&str>,
) -> (String, String, String, String, String, String, String) {
    let project = ctx
        .project
        .as_ref()
        .map(|p| p.name.clone())
        .filter(|s| !s.is_empty())
        .unwrap_or_default();
    let (application, window_title) = ctx
        .focused_window
        .as_ref()
        .map(|w| (w.application.clone(), w.title.clone()))
        .unwrap_or_default();
    let workspace = ctx
        .workspace
        .as_ref()
        .map(|w| {
            if w.name.is_empty() {
                w.id.clone()
            } else if w.id.is_empty() || w.id == w.name {
                w.name.clone()
            } else {
                format!("{} {}", w.name, w.id)
            }
        })
        .unwrap_or_default();
    let (resource_text, resource_key) = match ctx.resource.as_ref().filter(|r| !r.is_empty()) {
        Some(r) => {
            let mut parts: Vec<&str> = Vec::new();
            if !r.adapter.is_empty() {
                parts.push(r.adapter.as_str());
            }
            for opt in [
                r.file.as_deref(),
                r.cwd.as_deref(),
                r.git_root.as_deref(),
                r.git_branch.as_deref(),
                r.git_remote.as_deref(),
                r.url.as_deref(),
                r.page.as_deref(),
                r.title.as_deref(),
            ] {
                if let Some(v) = opt.filter(|s| !s.is_empty()) {
                    parts.push(v);
                }
            }
            let mut owned: Vec<String> = Vec::new();
            if let Some(z) = r.zotero.as_ref().filter(|x| !x.is_empty()) {
                owned.push(z.server_id.clone());
                owned.push(format!("{}/{}", z.library_type, z.library_id));
                owned.push(z.item_key.clone());
                if let Some(a) = z.attachment_key.as_deref().filter(|s| !s.is_empty()) {
                    owned.push(a.to_string());
                }
                for c in z.collections.iter().chain(z.ancestor_collections.iter()) {
                    owned.push(c.clone());
                }
                if let Some(u) = z.uri.as_deref().filter(|s| !s.is_empty()) {
                    owned.push(u.to_string());
                }
            }
            let mut text = parts.join(" ");
            if !owned.is_empty() {
                if !text.is_empty() {
                    text.push(' ');
                }
                text.push_str(&owned.join(" "));
            }
            let key = crate::desktop_session::resource_identity(r, event_project_id)
                .map(|id| id.key)
                .unwrap_or_default();
            (text, key)
        }
        None => (String::new(), String::new()),
    };
    let metadata = format!("{kind} {source}");
    (
        project,
        application,
        window_title,
        workspace,
        resource_text,
        metadata,
        resource_key,
    )
}

/// Quote one user token as a literal FTS5 phrase (`"` doubled).
fn fts_quote_literal(token: &str) -> String {
    format!("\"{}\"", token.replace('"', "\"\""))
}

/// Validate bounded nonempty search text: trimmed, `1..=MAX_SEARCH_TEXT_CHARS`
/// chars. Returns the trimmed form for FTS compilation.
fn validate_search_text(field: &str, v: &str) -> Result<String, StoreError> {
    let t = v.trim();
    if t.is_empty() {
        return Err(StoreError::InvalidArgument(format!(
            "{field} must be nonempty (non-whitespace)"
        )));
    }
    if t.chars().count() > MAX_SEARCH_TEXT_CHARS {
        return Err(StoreError::InvalidArgument(format!(
            "{field} must be 1..={MAX_SEARCH_TEXT_CHARS} chars, got {} chars",
            t.chars().count()
        )));
    }
    Ok(t.to_string())
}

/// Column-scoped literal AND expression: `col : "tok1" AND col : "tok2"`.
fn fts_column_and(column: &str, text: &str) -> String {
    text.split_whitespace()
        .map(|tok| format!("{column} : {}", fts_quote_literal(tok)))
        .collect::<Vec<_>>()
        .join(" AND ")
}

/// General literal AND expression: `"tok1" AND "tok2"`.
fn fts_general_and(text: &str) -> String {
    text.split_whitespace()
        .map(fts_quote_literal)
        .collect::<Vec<_>>()
        .join(" AND ")
}

impl ActivityStore {
    /// Transactional fresh setup at [`SCHEMA_VERSION`] (v4).
    ///
    /// Caller must have validated that the target is either empty/new or an
    /// otherwise-empty marker-only database holding exactly `schema_version`
    /// with an empty or singleton-v4 marker; this function itself never
    /// decides foreign vs new. The fresh v4 DDL already creates every table,
    /// index, and the FTS table, so no legacy ALTER/creation helper runs here.
    fn init_fresh(conn: &mut Connection) -> Result<(), StoreError> {
        let tx = conn.transaction()?;
        tx.execute_batch(v4_ddl())?;
        tx.execute_batch(v4_indexes_ddl())?;
        tx.execute_batch(activity_fts_ddl())?;
        // Singleton rows after setup.
        let count: i64 = tx.query_row("SELECT COUNT(*) FROM schema_version", [], |r| r.get(0))?;
        if count == 0 {
            tx.execute(
                "INSERT INTO schema_version (version) VALUES (?1)",
                params![SCHEMA_VERSION],
            )?;
        } else if count == 1 {
            let v: i64 = tx.query_row("SELECT version FROM schema_version", [], |r| r.get(0))?;
            if v != SCHEMA_VERSION {
                return Err(StoreError::IncompatibleSchema(format!(
                    "schema version {v} != supported {SCHEMA_VERSION}"
                )));
            }
            // Marker-only completion: v4 DDL above already created every
            // v4 table/index/FTS, so nothing else is backfilled here.
        } else {
            return Err(StoreError::IncompatibleSchema(format!(
                "schema_version holds {count} rows; expected singleton"
            )));
        }
        // Durable device identity: exactly one row.
        let dcount: i64 = tx.query_row("SELECT COUNT(*) FROM device_info", [], |r| r.get(0))?;
        if dcount == 0 {
            let dev = crate::desktop_session::generate_128bit_hex();
            tx.execute(
                "INSERT INTO device_info (device_id) VALUES (?1)",
                params![dev],
            )?;
        } else if dcount != 1 {
            return Err(StoreError::IncompatibleSchema(format!(
                "device_info holds {dcount} rows; expected singleton"
            )));
        } else {
            // Validate stored device shape (32 hex); corrupt => incompatible.
            let dev: String =
                tx.query_row("SELECT device_id FROM device_info", [], |r| r.get(0))?;
            if crate::desktop_session::normalize_hex_id(&dev, "device_id").is_err() {
                return Err(StoreError::IncompatibleSchema(
                    "bad device_info device_id shape".to_string(),
                ));
            }
        }
        tx.commit()?;
        Ok(())
    }

    /// Read-only v3 data-coherence validation (no writes).
    ///
    /// Transactional init cannot legitimately leave a partial v3
    /// database, so an existing version 3 must be fully coherent:
    /// `device_info` is the LOCAL writer identity (exactly one valid row);
    /// every activity row carries valid non-null event/device/session
    /// provenance; every session row carries valid session/device IDs; every
    /// activity session reference resolves AND the activity's device equals
    /// its referenced session's device (foreign-device history stays
    /// self-consistent instead of being forced onto the local identity); a
    /// non-empty activity table implies a non-empty projection; every
    /// session's bounds, references, and counts agree exactly with its
    /// activity rows (`start_ms` is the minimum associated
    /// `observed_at_ms`, `end_ms` the maximum, `first_activity_id` the
    /// minimum associated `id`, `last_activity_id` the maximum, so clock
    /// rollback stays `min`/`max` while ids stay insertion `min`/`max`);
    /// every session has at least one associated activity row; and no device
    /// holds more than one open session. Any incoherence is
    /// [`StoreError::IncompatibleSchema`].
    fn validate_v3_data(conn: &Connection) -> Result<(), StoreError> {
        let bad = |msg: String| StoreError::IncompatibleSchema(format!("bad v3 data: {msg}"));
        let count = |sql: &str| -> Result<i64, StoreError> {
            conn.query_row(sql, [], |r| r.get(0))
                .map_err(|e| bad(format!("count query failed: {e}")))
        };
        let dev: String = conn
            .query_row("SELECT device_id FROM device_info", [], |r| r.get(0))
            .map_err(|e| bad(format!("missing device_info: {e}")))?;
        if crate::desktop_session::normalize_hex_id(&dev, "device_id").is_err() {
            return Err(bad("device_info device_id is not 32-hex".to_string()));
        }
        if dev != dev.to_ascii_lowercase() {
            return Err(bad(
                "device_info device_id is not canonical lowercase hex".to_string()
            ));
        }
        // device_info must be a singleton; it is the local writer identity
        // that scopes appends and current-session, not a constraint on
        // synchronized foreign rows.
        if count("SELECT COUNT(*) FROM device_info")? != 1 {
            return Err(bad("device_info is not a singleton".to_string()));
        }
        if count(
            "SELECT COUNT(*) FROM activity WHERE event_id IS NULL OR device_id IS NULL OR session_id IS NULL",
        )? > 0
        {
            return Err(bad("activity rows lack event/device/session provenance".to_string()));
        }
        // Provenance values must be well-formed 32-hex identities, on both
        // activity rows and session rows.
        for col in ["event_id", "device_id", "session_id"] {
            let sql = format!(
                "SELECT COUNT(*) FROM activity WHERE length({col}) != 32 OR {col} GLOB '*[^0-9a-fA-F]*' OR {col} != lower({col})"
            );
            if count(&sql)? > 0 {
                return Err(bad(format!(
                    "activity.{col} is not canonical lowercase 32-hex"
                )));
            }
        }
        for col in ["session_id", "device_id"] {
            let sql = format!(
                "SELECT COUNT(*) FROM sessions WHERE length({col}) != 32 OR {col} GLOB '*[^0-9a-fA-F]*' OR {col} != lower({col})"
            );
            if count(&sql)? > 0 {
                return Err(bad(format!(
                    "sessions.{col} is not canonical lowercase 32-hex"
                )));
            }
        }
        if count("SELECT COUNT(*) FROM activity WHERE session_id NOT IN (SELECT session_id FROM sessions)")?
            > 0
        {
            return Err(bad("activity session_id has no sessions row".to_string()));
        }
        // Each event must agree with its own session's device. Foreign-device
        // pairs stay intact; a row pointing at another device's session is
        // corruption, not sync.
        if count(
            "SELECT COUNT(*) FROM activity a JOIN sessions s ON a.session_id = s.session_id WHERE a.device_id != s.device_id",
        )? > 0
        {
            return Err(bad("activity device_id mismatches its session device_id".to_string()));
        }
        let n_activity = count("SELECT COUNT(*) FROM activity")?;
        let n_sessions = count("SELECT COUNT(*) FROM sessions")?;
        if n_activity > 0 && n_sessions == 0 {
            return Err(bad(
                "empty session projection for non-empty activity".to_string()
            ));
        }
        // Every session row: valid status/reason/thresholds.
        {
            let mut stmt =
                conn.prepare("SELECT status, ended_reason, gap_ms, interruption_ms FROM sessions")?;
            let mut rows = stmt.query([])?;
            while let Some(row) = rows.next()? {
                let status: String = row.get(0)?;
                let reason: Option<String> = row.get(1)?;
                let gap: i64 = row.get(2)?;
                let intr: i64 = row.get(3)?;
                if status != "open" && status != "closed" {
                    return Err(bad(format!("bad session status {status:?}")));
                }
                if let Some(r) = reason.as_deref() {
                    if !matches!(r, "inactivity" | "project_switch" | "interruption") {
                        return Err(bad(format!("bad session ended_reason {r:?}")));
                    }
                }
                if status == "open" && reason.is_some() {
                    return Err(bad("open session carries ended_reason".to_string()));
                }
                if SessionConfig::new(gap, intr).is_err() {
                    return Err(bad(format!(
                        "bad session thresholds gap={gap} interruption={intr}"
                    )));
                }
            }
        }
        // Every session's projection must agree exactly with its activity
        // rows, local and foreign alike: bound ids resolve to rows of that
        // same session, the stored count matches, `start_ms`/`end_ms` are
        // the min/max associated `observed_at_ms` (clock rollback stays
        // min/max), and `first_activity_id`/`last_activity_id` are the
        // min/max associated `id` (insertion order).
        if count("SELECT COUNT(*) FROM sessions s WHERE s.first_activity_id NOT IN (SELECT id FROM activity)")? > 0
            || count("SELECT COUNT(*) FROM sessions s WHERE s.last_activity_id NOT IN (SELECT id FROM activity)")? > 0
        {
            return Err(bad(
                "session first/last activity id is dangling".to_string()
            ));
        }
        if count(
            "SELECT COUNT(*) FROM sessions s WHERE EXISTS (SELECT 1 FROM activity a WHERE a.id = s.first_activity_id AND a.session_id != s.session_id)",
        )? > 0
            || count(
                "SELECT COUNT(*) FROM sessions s WHERE EXISTS (SELECT 1 FROM activity a WHERE a.id = s.last_activity_id AND a.session_id != s.session_id)",
            )? > 0
        {
            return Err(bad("session bound ids belong to another session".to_string()));
        }
        if count(
            "SELECT COUNT(*) FROM sessions s WHERE s.event_count != (SELECT COUNT(*) FROM activity a WHERE a.session_id = s.session_id)",
        )? > 0
        {
            return Err(bad(
                "session event_count disagrees with activity".to_string()
            ));
        }
        // Every session must have at least one associated activity row.
        if count(
            "SELECT COUNT(*) FROM sessions s WHERE NOT EXISTS (SELECT 1 FROM activity a WHERE a.session_id = s.session_id)",
        )? > 0
        {
            return Err(bad("session has no associated activity rows".to_string()));
        }
        // Exact projection: start=min observed, end=max observed,
        // first=min id, last=max id.
        if count(
            "SELECT COUNT(*) FROM sessions s WHERE s.start_ms != (SELECT MIN(a.observed_at_ms) FROM activity a WHERE a.session_id = s.session_id)",
        )? > 0
        {
            return Err(bad("session start_ms is not min activity observed_at_ms".to_string()));
        }
        if count(
            "SELECT COUNT(*) FROM sessions s WHERE s.end_ms != (SELECT MAX(a.observed_at_ms) FROM activity a WHERE a.session_id = s.session_id)",
        )? > 0
        {
            return Err(bad("session end_ms is not max activity observed_at_ms".to_string()));
        }
        if count(
            "SELECT COUNT(*) FROM sessions s WHERE s.first_activity_id != (SELECT MIN(a.id) FROM activity a WHERE a.session_id = s.session_id)",
        )? > 0
        {
            return Err(bad(
                "session first_activity_id is not min activity id".to_string()
            ));
        }
        if count(
            "SELECT COUNT(*) FROM sessions s WHERE s.last_activity_id != (SELECT MAX(a.id) FROM activity a WHERE a.session_id = s.session_id)",
        )? > 0
        {
            return Err(bad(
                "session last_activity_id is not max activity id".to_string()
            ));
        }
        // At most one open session per device: the writer repairs/extends a
        // single local open, so a second local open is corruption, while a
        // foreign open alongside it is legitimate synchronized history.
        if count(
            "SELECT COUNT(*) FROM (SELECT device_id FROM sessions WHERE status = 'open' GROUP BY device_id HAVING COUNT(*) > 1)",
        )? > 0
        {
            return Err(bad("device holds more than one open session".to_string()));
        }
        Ok(())
    }

    /// Require every v3 table before data validation/completion.
    /// Established v3 holds exactly the five documented tables; a missing
    /// table rejects untouched instead of being recreated or validated
    /// partially.
    fn require_v3_tables(tables: &[String]) -> Result<(), StoreError> {
        for want in [
            "schema_version",
            "activity",
            "device_info",
            "sessions",
            "session_resources",
        ] {
            if !tables.iter().any(|t| t == want) {
                return Err(StoreError::IncompatibleSchema(format!(
                    "missing v3 table {want}; expected all five v3 tables"
                )));
            }
        }
        Ok(())
    }

    /// Require the documented unique `idx_activity_event_id` to exist with
    /// its exact unique shape. Absence rejects untouched on both opens;
    /// it is never silently recreated (without it uniqueness is
    /// unenforced, so duplicates could already exist).
    fn require_event_id_index(conn: &Connection) -> Result<(), StoreError> {
        let n: i64 = conn.query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_activity_event_id'",
            [],
            |r| r.get(0),
        )?;
        if n == 0 {
            return Err(StoreError::IncompatibleSchema(
                "missing UNIQUE idx_activity_event_id; established v3 requires it".to_string(),
            ));
        }
        // Exact shape is already enforced by `validate_objects_and_indexes`
        // for present indexes, but re-check ownership/uniqueness/columns
        // here so the requirement is self-contained before completion.
        let mut found = false;
        for e in pragma_index_list_full(conn, "activity")? {
            if e.name == "idx_activity_event_id" {
                found = true;
                if e.unique == 0 || e.partial != 0 || e.origin != "c" {
                    return Err(StoreError::IncompatibleSchema(format!(
                        "bad index idx_activity_event_id on activity (unique={} partial={} origin={})",
                        e.unique, e.partial, e.origin
                    )));
                }
            }
        }
        if !found {
            return Err(StoreError::IncompatibleSchema(
                "idx_activity_event_id lives on the wrong table; expected activity".to_string(),
            ));
        }
        let cols = pragma_index_columns(conn, "idx_activity_event_id")?;
        if cols != vec!["event_id".to_string()] {
            return Err(StoreError::IncompatibleSchema(format!(
                "wrong index idx_activity_event_id columns {cols:?}; expected [\"event_id\"]"
            )));
        }
        check_index_sort_and_collation(conn, "idx_activity_event_id")?;
        Ok(())
    }

    /// Require every v4 table before data validation/completion.
    /// Established v4 holds the five base tables plus the FTS virtual table
    /// and its five exact shadow tables; a missing table rejects untouched.
    fn require_v4_tables(tables: &[String]) -> Result<(), StoreError> {
        for want in [
            "schema_version",
            "activity",
            "device_info",
            "sessions",
            "session_resources",
            "activity_fts",
            "activity_fts_data",
            "activity_fts_idx",
            "activity_fts_content",
            "activity_fts_docsize",
            "activity_fts_config",
        ] {
            if !tables.iter().any(|t| t == want) {
                return Err(StoreError::IncompatibleSchema(format!(
                    "missing v4 table {want}; expected all v4 tables"
                )));
            }
        }
        Ok(())
    }

    /// Require the new device+time index to exist with its exact shape when
    /// present-checking is needed. Absence is completable (non-unique), so
    /// this is only used to validate present shapes, not to reject missing.
    fn validate_device_time_index_shape(conn: &Connection) -> Result<(), StoreError> {
        let n: i64 = conn.query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_sessions_device_time'",
            [],
            |r| r.get(0),
        )?;
        if n == 0 {
            return Ok(());
        }
        let cols = pragma_index_columns(conn, "idx_sessions_device_time")?;
        let want = vec![
            "device_id".to_string(),
            "end_ms".to_string(),
            "start_ms".to_string(),
            "session_id".to_string(),
        ];
        if cols != want {
            return Err(StoreError::IncompatibleSchema(format!(
                "wrong index idx_sessions_device_time columns {cols:?}; expected {want:?}"
            )));
        }
        check_index_sort_and_collation(conn, "idx_sessions_device_time")?;
        // Ownership/uniqueness/partial/origin via full index list.
        for e in pragma_index_list_full(conn, "sessions")? {
            if e.name == "idx_sessions_device_time" {
                if e.unique != 0 || e.partial != 0 || e.origin != "c" {
                    return Err(StoreError::IncompatibleSchema(format!(
                        "bad index idx_sessions_device_time on sessions (unique={} partial={} origin={})",
                        e.unique, e.partial, e.origin
                    )));
                }
                return Ok(());
            }
        }
        Err(StoreError::IncompatibleSchema(
            "idx_sessions_device_time lives on the wrong table; expected sessions".to_string(),
        ))
    }

    /// True when a v4 database is missing an expected non-unique index.
    /// The UNIQUE event index and the FTS table are never completed.
    fn needs_v4_completion(conn: &Connection) -> Result<bool, StoreError> {
        for idx in [
            "idx_activity_time",
            "idx_activity_project",
            "idx_activity_session",
            "idx_sessions_time",
            "idx_sessions_project",
            "idx_sessions_device_time",
            "idx_session_resources_seen",
        ] {
            let n: i64 = conn.query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name=?1",
                params![idx],
                |r| r.get(0),
            )?;
            if n == 0 {
                return Ok(true);
            }
        }
        Ok(false)
    }

    /// Complete a v4 database missing expected non-unique indexes.
    fn ensure_v4_complete(conn: &mut Connection) -> Result<(), StoreError> {
        Self::validate_v4_data(conn)?;
        if !Self::needs_v4_completion(conn)? {
            return Ok(());
        }
        let tx = conn.transaction()?;
        tx.execute_batch(v4_indexes_ddl())?;
        tx.commit()?;
        Ok(())
    }

    /// Read-only v4 data-coherence validation (no writes): all v3 invariants
    /// plus a one-to-one `activity.id ↔ activity_fts.rowid` relation.
    /// Content equivalence is NOT re-parsed on every open (expensive);
    /// rowid one-to-one is enforced.
    fn validate_v4_data(conn: &Connection) -> Result<(), StoreError> {
        Self::validate_v3_data(conn)?;
        let bad = |msg: String| StoreError::IncompatibleSchema(format!("bad v4 data: {msg}"));
        let count = |sql: &str| -> Result<i64, StoreError> {
            conn.query_row(sql, [], |r| r.get(0))
                .map_err(|e| bad(format!("count query failed: {e}")))
        };
        let n_activity = count("SELECT COUNT(*) FROM activity")?;
        let n_fts = count("SELECT COUNT(*) FROM activity_fts")?;
        if n_activity != n_fts {
            return Err(bad(format!(
                "activity ({n_activity}) and activity_fts ({n_fts}) row counts differ"
            )));
        }
        if count(
            "SELECT COUNT(*) FROM activity a LEFT JOIN activity_fts f ON f.rowid = a.id WHERE f.rowid IS NULL",
        )? > 0
        {
            return Err(bad(
                "activity rows without activity_fts rows".to_string(),
            ));
        }
        if count(
            "SELECT COUNT(*) FROM activity_fts f LEFT JOIN activity a ON a.id = f.rowid WHERE a.id IS NULL",
        )? > 0
        {
            return Err(bad(
                "activity_fts rows without activity rows".to_string(),
            ));
        }
        Ok(())
    }

    /// Single transactional v3→v4 migration for exact coherent v3 only.
    /// Requires proof of ownership of the per-DB [`DesktopLock`] (see
    /// [`ActivityStore::open_with_session_config_and_lock`]): an old v3
    /// collector still appending after migration would corrupt the FTS
    /// projection, so ordinary opens never migrate.
    ///
    /// The whole migration — strict v3 prevalidation, device+time index and
    /// FTS creation, backfill of every stored `DesktopContext` into FTS
    /// (`rowid = activity.id`), `schema_version` bump — runs inside one
    /// `BEGIN IMMEDIATE` transaction. Prevalidation inside the transaction
    /// means no concurrent writer can race between validation, backfill, and
    /// the version update; `BEGIN IMMEDIATE` fails fast while another writer
    /// holds the database. Any malformed snapshot or FTS failure rolls back
    /// untouched (version stays 3, no FTS, no partial index).
    fn migrate_v3_to_v4(conn: &mut Connection) -> Result<(), StoreError> {
        // BEGIN IMMEDIATE first: reserve the writer lock before observing
        // anything, so validation, backfill, and the version bump are one
        // atomic unit no other writer can interleave with.
        let tx = conn.transaction()?;
        // Strict v3 pre-validation inside the transaction (shapes + objects
        // + UNIQUE + full data coherence). Missing non-unique indexes are
        // tolerated here: this same transaction recreates them.
        let tables = existing_tables(&tx)?;
        validate_objects_and_indexes_v3(&tx)?;
        Self::require_v3_tables(&tables)?;
        let vcols = pragma_table_info(&tx, "schema_version")?;
        validate_schema_version_shape(&vcols)?;
        let acols = pragma_table_info(&tx, "activity")?;
        validate_activity_shape(&acols)?;
        for tbl in ["sessions", "session_resources", "device_info"] {
            let cols = pragma_table_info(&tx, tbl)?;
            match tbl {
                "sessions" => validate_sessions_shape(&cols)?,
                "session_resources" => validate_session_resources_shape(&cols)?,
                "device_info" => validate_device_info_shape(&cols)?,
                _ => {}
            }
        }
        let versions = Self::read_versions(&tx)?;
        match versions.as_slice() {
            [v] if *v == MIGRATABLE_V3_VERSION => {}
            _ => {
                return Err(StoreError::IncompatibleSchema(format!(
                    "migrate v3→v4 requires singleton version {MIGRATABLE_V3_VERSION}, got {versions:?}"
                )))
            }
        }
        Self::require_event_id_index(&tx)?;
        Self::validate_v3_data(&tx)?;
        // Index + FTS + backfill + version bump, same transaction.
        tx.execute_batch(v4_indexes_ddl())?;
        tx.execute_batch(activity_fts_ddl())?;
        // Backfill every activity row.
        {
            let mut sel = tx.prepare(
                "SELECT id, observed_at_ms, kind, source, snapshot_json, project_id, session_id FROM activity ORDER BY id ASC",
            )?;
            let mut rows = sel.query([])?;
            while let Some(row) = rows.next()? {
                let id: i64 = row.get(0)?;
                let _observed: i64 = row.get(1)?;
                let kind: String = row.get(2)?;
                let source: String = row.get(3)?;
                let snapshot_json: String = row.get(4)?;
                let stored_project: Option<String> = row.get(5)?;
                let session_id: String = row.get(6).map_err(|e| {
                    StoreError::IncompatibleSchema(format!("bad v3 data: null session_id: {e}"))
                })?;
                let ctx: DesktopContext = serde_json::from_str(&snapshot_json).map_err(|e| {
                    StoreError::IncompatibleSchema(format!(
                        "bad v3 snapshot for activity {id}: {e}"
                    ))
                })?;
                let event_project = stored_project
                    .as_deref()
                    .filter(|s| !s.is_empty())
                    .map(|s| s.to_lowercase())
                    .or_else(|| {
                        ctx.project
                            .as_ref()
                            .map(|p| p.id.to_lowercase())
                            .filter(|s| !s.is_empty())
                    });
                let (fts_project, fts_app, fts_title, fts_ws, fts_res, fts_meta, fts_key) =
                    fts_row_for(&ctx, &kind, &source, event_project.as_deref());
                tx.execute(
                    "INSERT INTO activity_fts(rowid, session_id, resource_key, project, application, window_title, workspace, resource, metadata) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)",
                    params![
                        id,
                        session_id,
                        fts_key,
                        fts_project,
                        fts_app,
                        fts_title,
                        fts_ws,
                        fts_res,
                        fts_meta
                    ],
                )
                .map_err(|e| {
                    StoreError::IncompatibleSchema(format!("FTS backfill failed for {id}: {e}"))
                })?;
            }
        }
        let changed = tx.execute(
            "UPDATE schema_version SET version = ?1 WHERE version = ?2",
            params![SCHEMA_VERSION, MIGRATABLE_V3_VERSION],
        )?;
        if changed != 1 {
            return Err(StoreError::IncompatibleSchema(
                "schema_version bump affected != 1 row; rolling back".to_string(),
            ));
        }
        tx.commit()?;
        Ok(())
    }

    /// Authoritative pre-mutation validation + setup for writable opens.
    ///
    /// Strict structural policy (BEFORE any mutation, except the migration
    /// transaction itself which validates inside `BEGIN IMMEDIATE`):
    /// - Fresh empty files and marker-only interrupted setups (exactly
    ///   `schema_version` with an empty or singleton-v4 marker, no other
    ///   objects) complete via [`Self::init_fresh`] at v4.
    /// - Established v4 (exact coherent v4: all base tables with exact
    ///   shapes, exact FTS table/columns/config, all eight documented
    ///   indexes when present, UNIQUE event index required, full
    ///   [`Self::validate_v4_data`] coherence) completes missing non-unique
    ///   indexes via [`Self::ensure_v4_complete`].
    /// - Established exact coherent v3 migrates to v4 in one transaction via
    ///   [`Self::migrate_v3_to_v4`] — but ONLY when the caller proves
    ///   ownership of the per-DB [`DesktopLock`] (`allow_migration`). An old
    ///   v3 collector still running could otherwise append v3 rows after
    ///   migration, silently desynchronizing the FTS projection. Ordinary
    ///   opens reject v3 with a clear collector lock/restart error.
    /// - Any other version, any non-v3/v4 shape (including pre-v3 v1/v2
    ///   layouts), futures, multi-rows, missing tables, or foreign objects
    ///   reject untouched. An activity table without a trustworthy singleton
    ///   version marker rejects untouched.
    fn ensure_writable_schema(
        conn: &mut Connection,
        allow_migration: bool,
    ) -> Result<(), StoreError> {
        let tables = existing_tables(conn)?;
        let has_version = tables.iter().any(|t| t == "schema_version");
        let has_activity = tables.iter().any(|t| t == "activity");
        if !has_version && !has_activity {
            validate_objects_and_indexes_empty(conn)?;
            if tables.is_empty() {
                return Self::init_fresh(conn);
            }
            return Err(StoreError::IncompatibleSchema(
                "foreign database: missing schema_version/activity tables".to_string(),
            ));
        }
        if !has_version || !has_activity {
            // Marker-only path: only an otherwise-empty DB holding exactly
            // `schema_version` with an allowed empty/singleton-v4 marker
            // completes. Any FTS or other v4-named object beside the marker
            // rejects before mutation.
            if has_version {
                let cols = pragma_table_info(conn, "schema_version")?;
                validate_schema_version_shape(&cols)?;
                // Reject any non-marker object before deciding.
                let non_marker: Vec<&String> =
                    tables.iter().filter(|t| *t != "schema_version").collect();
                if !non_marker.is_empty() {
                    return Err(StoreError::IncompatibleSchema(format!(
                        "marker-only schema_version accompanied by extra tables {tables:?}; refusing to complete"
                    )));
                }
                // Any index/trigger/view (including FTS shadows) rejects.
                // NOTE: `GLOB 'sqlite_*'` (not `LIKE 'sqlite_%'`) so the
                // underscore is literal: `sqlitex_…` objects cannot evade.
                let stray: i64 = conn.query_row(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name NOT GLOB 'sqlite_*'",
                    [],
                    |r| r.get(0),
                )?;
                if stray != 0 {
                    return Err(StoreError::IncompatibleSchema(
                        "marker-only schema_version accompanied by indexes; refusing to complete"
                            .to_string(),
                    ));
                }
                let stray_tbl: i64 = conn.query_row(
                    "SELECT COUNT(*) FROM sqlite_master WHERE (type='trigger' OR type='view')",
                    [],
                    |r| r.get(0),
                )?;
                if stray_tbl != 0 {
                    return Err(StoreError::IncompatibleSchema(
                        "marker-only schema_version accompanied by triggers/views; refusing to complete"
                            .to_string(),
                    ));
                }
                // FTS virtual table would appear in `tables`; already covered.
                let versions = Self::read_versions(conn)?;
                match versions.as_slice() {
                    [] => {}
                    [v] if *v == SCHEMA_VERSION => {}
                    [v] => {
                        return Err(StoreError::IncompatibleSchema(format!(
                            "partial schema version {v} != supported {SCHEMA_VERSION}"
                        )))
                    }
                    many => {
                        return Err(StoreError::IncompatibleSchema(format!(
                            "schema_version holds {} rows; expected singleton {SCHEMA_VERSION}",
                            many.len()
                        )))
                    }
                }
            }
            if has_activity {
                return Err(StoreError::IncompatibleSchema(
                    "activity table exists without schema_version; refusing to infer a version"
                        .to_string(),
                ));
            }
            if tables.len() != 1 || tables[0] != "schema_version" {
                return Err(StoreError::IncompatibleSchema(format!(
                    "marker-only schema_version accompanied by extra tables {tables:?}; refusing to complete"
                )));
            }
            return Self::init_fresh(conn);
        }
        // Both tables present: read the singleton version first (read-only),
        // then apply version-specific strict validation. No mutation yet.
        // A malformed version table (e.g. TEXT version) is incompatible, not
        // a SQLite error.
        let versions = Self::read_versions(conn)
            .map_err(|e| StoreError::IncompatibleSchema(format!("bad schema_version rows: {e}")))?;
        match versions.as_slice() {
            [] => Err(StoreError::IncompatibleSchema(
                "schema_version is empty beside an existing activity table".to_string(),
            )),
            [v] if *v == MIGRATABLE_V3_VERSION => {
                // Exact coherent v3 → migrate, but only with lock proof.
                // Without it an old v3 collector could still be appending.
                if allow_migration {
                    Self::migrate_v3_to_v4(conn)
                } else {
                    Err(StoreError::IncompatibleSchema(format!(
                        "schema version {v} requires collector lock/restart: stop the old collector (let it drain) and restart it to migrate v{MIGRATABLE_V3_VERSION}→v{SCHEMA_VERSION}; ordinary opens never migrate"
                    )))
                }
            }
            [v] if *v == SCHEMA_VERSION => {
                // Exact coherent v4 → complete missing non-unique indexes.
                validate_objects_and_indexes_v4(conn)?;
                Self::require_v4_tables(&tables)?;
                let vcols = pragma_table_info(conn, "schema_version")?;
                validate_schema_version_shape(&vcols)?;
                let acols = pragma_table_info(conn, "activity")?;
                validate_activity_shape(&acols)?;
                for tbl in ["sessions", "session_resources", "device_info"] {
                    let cols = pragma_table_info(conn, tbl)?;
                    match tbl {
                        "sessions" => validate_sessions_shape(&cols)?,
                        "session_resources" => validate_session_resources_shape(&cols)?,
                        "device_info" => validate_device_info_shape(&cols)?,
                        _ => {}
                    }
                }
                validate_fts_shape(conn)?;
                Self::require_event_id_index(conn)?;
                Self::validate_device_time_index_shape(conn)?;
                Self::ensure_v4_complete(conn)
            }
            [v] => Err(StoreError::IncompatibleSchema(format!(
                "schema version {v} != supported {SCHEMA_VERSION} (v{MIGRATABLE_V3_VERSION} migrates on writable open)"
            ))),
            many => Err(StoreError::IncompatibleSchema(format!(
                "schema_version holds {} rows ({:?}); expected singleton {SCHEMA_VERSION}",
                many.len(),
                &many[..many.len().min(4)]
            ))),
        }
    }

    /// Strict object/index validation wrapper (read-only, no writes).
    /// Version-specific: v4 allowlist (base + FTS + shadows + eight indexes).
    fn validate_objects_strict_v4(conn: &Connection) -> Result<(), StoreError> {
        validate_objects_and_indexes_v4(conn)
    }

    /// Read all version rows (empty allowed for partial-init completion).
    fn read_versions(conn: &Connection) -> Result<Vec<i64>, StoreError> {
        let mut stmt = conn.prepare("SELECT version FROM schema_version")?;
        let mapped = stmt.query_map([], |r| r.get(0))?;
        let mut out = Vec::new();
        for v in mapped {
            out.push(v?);
        }
        Ok(out)
    }

    /// Validate an already-open read-only handle. No writes of any kind.
    ///
    /// Intentional query-performance contract: read-only opens perform
    /// strict structural validation (objects, tables, shapes, FTS
    /// declaration, required index shapes) plus cheap singleton/version/
    /// local-device metadata checks only. They deliberately do NOT run the
    /// full-history data-coherence scans (`validate_v4_data`: session
    /// projection/provenance/FTS one-to-one counts over all rows), so every
    /// `search`/`history`/`detail` process invocation stays fast on large
    /// histories. Rows actually touched still validate through the existing
    /// row parsers (session status/reason/thresholds, snapshot JSON).
    /// Writable opens and migration keep the deep exact validation.
    ///
    /// A v3 database gets a clear migration-required error (writable open
    /// only). Any other version, any non-v4 shape (including pre-v3 v1/v2
    /// layouts), missing tables, or foreign/newer/malformed/multi-row
    /// content rejects without modification.
    fn validate_read_only(conn: &Connection) -> Result<(), StoreError> {
        // Single read transaction/snapshot: all structural statements below
        // observe one consistent database image, so an atomic concurrent
        // append cannot produce mixed-statement false corruption.
        conn.execute_batch("BEGIN")
            .map_err(|e| StoreError::Sqlite(format!("begin read snapshot: {e}")))?;
        let res = Self::validate_read_only_inner(conn);
        let end = if res.is_ok() { "COMMIT" } else { "ROLLBACK" };
        let _ = conn.execute_batch(end);
        res
    }

    /// Read-only validation body (runs inside the caller's read snapshot).
    fn validate_read_only_inner(conn: &Connection) -> Result<(), StoreError> {
        let tables = existing_tables(conn)?;
        if !(tables.iter().any(|t| t == "schema_version") && tables.iter().any(|t| t == "activity"))
        {
            return Err(StoreError::IncompatibleSchema(
                "foreign database: missing schema_version/activity tables".to_string(),
            ));
        }
        // Peek the version first for a clear v3 message (read-only never
        // migrates). Shape failures below still reject as incompatible.
        let versions_peek: Result<Vec<i64>, rusqlite::Error> = (|| {
            let mut stmt = conn.prepare("SELECT version FROM schema_version")?;
            let mapped = stmt.query_map([], |r| r.get::<_, i64>(0))?;
            let mut out = Vec::new();
            for v in mapped {
                out.push(v?);
            }
            Ok(out)
        })();
        if let Ok(vs) = versions_peek {
            if vs.as_slice() == [MIGRATABLE_V3_VERSION] {
                return Err(StoreError::IncompatibleSchema(format!(
                    "schema version {} requires migration: open writable once to migrate to v{} (read-only accepts v{} only)",
                    MIGRATABLE_V3_VERSION, SCHEMA_VERSION, SCHEMA_VERSION
                )));
            }
        }
        Self::validate_objects_strict_v4(conn)?;
        Self::require_v4_tables(&tables)?;
        let vcols = pragma_table_info(conn, "schema_version").map_err(|e| {
            StoreError::IncompatibleSchema(format!("bad schema_version table: {e}"))
        })?;
        validate_schema_version_shape(&vcols)?;
        let acols = pragma_table_info(conn, "activity")
            .map_err(|e| StoreError::IncompatibleSchema(format!("bad activity table: {e}")))?;
        validate_activity_shape(&acols)?;
        for tbl in ["sessions", "session_resources", "device_info"] {
            let cols = pragma_table_info(conn, tbl)
                .map_err(|e| StoreError::IncompatibleSchema(format!("bad {tbl} table: {e}")))?;
            match tbl {
                "sessions" => validate_sessions_shape(&cols)?,
                "session_resources" => validate_session_resources_shape(&cols)?,
                "device_info" => validate_device_info_shape(&cols)?,
                _ => {}
            }
        }
        validate_fts_shape(conn)
            .map_err(|e| StoreError::IncompatibleSchema(format!("bad activity_fts table: {e}")))?;
        let versions = Self::read_versions(conn)
            .map_err(|e| StoreError::IncompatibleSchema(format!("bad schema_version rows: {e}")))?;
        match versions.as_slice() {
            [v] if *v == SCHEMA_VERSION => {
                Self::require_event_id_index(conn)?;
                Self::validate_device_time_index_shape(conn)?;
                // Lightweight contract: cheap local-writer metadata only —
                // singleton device_info with a canonical 32-hex identity.
                // No full-history scans here; writable opens enforce those.
                Self::validate_read_only_metadata(conn)
            }
            [v] => Err(StoreError::IncompatibleSchema(format!(
                "schema version {v} != supported {SCHEMA_VERSION}"
            ))),
            _ => Err(StoreError::IncompatibleSchema(format!(
                "schema_version holds {} rows; expected singleton {SCHEMA_VERSION}",
                versions.len()
            ))),
        }
    }

    /// Cheap read-only metadata check: `device_info` holds exactly one row
    /// with a canonical lowercase 32-hex device identity (the local writer
    /// identity scoping appends and current-session).
    fn validate_read_only_metadata(conn: &Connection) -> Result<(), StoreError> {
        let bad = |msg: String| StoreError::IncompatibleSchema(format!("bad device_info: {msg}"));
        let n: i64 = conn
            .query_row("SELECT COUNT(*) FROM device_info", [], |r| r.get(0))
            .map_err(|e| bad(format!("count failed: {e}")))?;
        if n != 1 {
            return Err(bad(format!("expected singleton, got {n} rows")));
        }
        let dev: String = conn
            .query_row("SELECT device_id FROM device_info", [], |r| r.get(0))
            .map_err(|e| bad(format!("missing device_info: {e}")))?;
        crate::desktop_session::normalize_hex_id(&dev, "device_id")
            .map_err(|e| bad(format!("device_id is not 32-hex: {e}")))?;
        if dev != dev.to_ascii_lowercase() {
            return Err(bad(
                "device_info device_id is not canonical lowercase hex".to_string()
            ));
        }
        Ok(())
    }

    /// Open (creating) a file database.
    ///
    /// Creates parents `0700` and the database `0600` from the outset.
    /// Existing files/sidecars must be private regular files owned by the
    /// current user (no symlinks); violations fail closed. Foreign/newer
    /// schemas are rejected with no modifications.
    ///
    /// The session config is parsed and validated from the environment
    /// BEFORE any filesystem or database mutation, so a malformed
    /// `QS_DESKTOP_SESSION_*` value fails without creating or touching the
    /// database.
    pub fn open<P: AsRef<Path>>(path: P) -> Result<Self, StoreError> {
        let session_config = SessionConfig::from_env()
            .map_err(|e| StoreError::InvalidArgument(format!("session config: {e}")))?;
        Self::open_impl(path, session_config, None)
    }

    /// Internal file open with an already-validated explicit config. All
    /// filesystem/database mutation happens here, after validation.
    /// `lock` is `Some` only for the lock-capability constructor, which is
    /// the sole path allowed to migrate v3→v4.
    fn open_impl<P: AsRef<Path>>(
        path: P,
        session_config: SessionConfig,
        lock: Option<&DesktopLock>,
    ) -> Result<Self, StoreError> {
        let path = path.as_ref();
        crate::desktop_paths::ensure_parent_for_file(path)
            .map_err(|e| map_parent_error(path, &e))?;
        crate::desktop_paths::validate_sidecars(path).map_err(StoreError::UnsafePath)?;

        match std::fs::symlink_metadata(path) {
            Ok(md) => {
                if md.file_type().is_symlink() {
                    return Err(StoreError::UnsafePath(format!(
                        "unsafe database (symlink): {}",
                        path.display()
                    )));
                }
                if !md.is_file() {
                    return Err(StoreError::UnsafePath(format!(
                        "not a regular file: {}",
                        path.display()
                    )));
                }
                crate::desktop_paths::validate_file_private(path)
                    .map_err(StoreError::UnsafePath)?;
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                crate::desktop_paths::create_private_file(path).map_err(|m| {
                    if m.contains("permission denied") {
                        StoreError::PermissionDenied(m)
                    } else if m.contains("already exists") {
                        StoreError::Sqlite(format!("race creating {}: {m}", path.display()))
                    } else {
                        StoreError::Sqlite(m)
                    }
                })?;
            }
            Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => {
                return Err(StoreError::PermissionDenied(format!(
                    "cannot inspect {}: {e}",
                    path.display()
                )));
            }
            Err(e) => {
                return Err(StoreError::Sqlite(format!(
                    "cannot inspect {}: {e}",
                    path.display()
                )))
            }
        }

        // Read-write without CREATE: the file already exists (we created it
        // 0600 when missing), so SQLite must not create with default perms.
        let mut conn = Connection::open_with_flags(
            path,
            OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(|e| map_sqlite_open_error(path, e))?;
        // Fail fast on locked storage so idle/backoff and shutdown-drain
        // retries stay bounded (no multi-second busy wait per append).
        let _ = conn.busy_timeout(std::time::Duration::from_millis(100));

        // Authoritative validation BEFORE any mutation. Fresh files are
        // initialized at v4, existing v4 files are completed (non-unique
        // indexes only); v3 migrates only with lock proof (see
        // `open_with_session_config_and_lock`); any other version/shape plus
        // foreign/newer/multi-row reject untouched.
        let allow_migration = lock.is_some();
        Self::ensure_writable_schema(&mut conn, allow_migration)?;
        // Best-effort WAL for a private local DB (WAL/SHM stay beside the DB
        // inside the private directory). Ignore errors: plain rollback-journal
        // still works.
        let _ = conn.query_row("PRAGMA journal_mode=WAL", [], |r| r.get::<_, String>(0));
        let _ = conn.execute_batch("PRAGMA synchronous=NORMAL;");
        Ok(Self {
            conn,
            session_config,
        })
    }

    /// Open an existing database strictly read-only.
    ///
    /// Never creates files, parent directories, schemas, or journals.
    /// Rejects foreign/incompatible databases without modification.
    /// Distinguishes [`StoreError::NotFound`] (collector never ran) from
    /// [`StoreError::PermissionDenied`] and [`StoreError::UnsafePath`].
    pub fn open_read_only<P: AsRef<Path>>(path: P) -> Result<Self, StoreError> {
        let path = path.as_ref();
        match std::fs::symlink_metadata(path) {
            Ok(md) => {
                if md.file_type().is_symlink() {
                    return Err(StoreError::UnsafePath(format!(
                        "unsafe database (symlink): {}",
                        path.display()
                    )));
                }
                if !md.is_file() {
                    return Err(StoreError::IncompatibleSchema(format!(
                        "not a regular file: {}",
                        path.display()
                    )));
                }
                crate::desktop_paths::validate_file_private(path)
                    .map_err(StoreError::UnsafePath)?;
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                return Err(StoreError::NotFound(format!(
                    "no database at {} (collector has not run yet)",
                    path.display()
                )));
            }
            Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => {
                return Err(StoreError::PermissionDenied(format!(
                    "cannot inspect {}: {e}",
                    path.display()
                )));
            }
            Err(e) => {
                return Err(StoreError::Sqlite(format!(
                    "cannot inspect {}: {e}",
                    path.display()
                )))
            }
        }
        crate::desktop_paths::validate_sidecars(path).map_err(StoreError::UnsafePath)?;

        let conn = Connection::open_with_flags(
            path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(|e| map_sqlite_open_error(path, e))?;
        // Belt-and-braces: forbid writes through this handle.
        let _ = conn.execute_batch("PRAGMA query_only=ON;");
        Self::validate_read_only(&conn)?;
        Ok(Self {
            conn,
            session_config: SessionConfig::default(),
        })
    }

    /// Open (creating) with an explicit session config (tests/CLI overrides).
    /// Precedence `CLI > env > defaults` is resolved by the caller via
    /// [`SessionConfig::resolve`]; this constructor uses `config` as-is for
    /// new sessions (historical sessions keep their stored thresholds).
    /// It never reads the environment.
    ///
    /// Ordinary opens never migrate: a v3 database is rejected with a clear
    /// collector lock/restart error (stop the old collector, restart to
    /// migrate). Fresh and current-v4 opens are unchanged.
    pub fn open_with_session_config<P: AsRef<Path>>(
        path: P,
        config: SessionConfig,
    ) -> Result<Self, StoreError> {
        Self::open_impl(path, config, None)
    }

    /// Lock-capability writable open: like
    /// [`Self::open_with_session_config`], but the caller proves ownership
    /// of the per-DB [`DesktopLock`] for exactly this database
    /// (`lock.path() == lock_path_for(db)`), allowing a v3→v4 migration.
    /// The collector holds this guard for life and passes it here, so no old
    /// v3 writer can still be appending after migration. A lock for any other
    /// path is rejected without touching the database.
    pub fn open_with_session_config_and_lock<P: AsRef<Path>>(
        path: P,
        config: SessionConfig,
        lock: &DesktopLock,
    ) -> Result<Self, StoreError> {
        let path = path.as_ref();
        if lock.path() != lock_path_for(path) {
            return Err(StoreError::InvalidArgument(format!(
                "lock {} does not correspond exactly to database {} (expected {})",
                lock.path().display(),
                path.display(),
                lock_path_for(path).display()
            )));
        }
        Self::open_impl(path, config, Some(lock))
    }

    /// In-memory database (tests/manual validation). Uses defaults (ignores
    /// env for determinism); use [`Self::open_with_session_config`] with a
    /// file path when custom thresholds are needed, or construct then
    /// overwrite via `with_session_config`.
    pub fn open_in_memory() -> Result<Self, StoreError> {
        let mut conn = Connection::open_in_memory()?;
        let _ = conn.busy_timeout(std::time::Duration::from_millis(100));
        Self::init_fresh(&mut conn)?;
        Ok(Self {
            conn,
            session_config: SessionConfig::default(),
        })
    }

    /// In-memory database with an explicit session config (tests).
    pub fn open_in_memory_with_config(config: SessionConfig) -> Result<Self, StoreError> {
        let mut conn = Connection::open_in_memory()?;
        let _ = conn.busy_timeout(std::time::Duration::from_millis(100));
        Self::init_fresh(&mut conn)?;
        Ok(Self {
            conn,
            session_config: config,
        })
    }

    /// Runtime session thresholds for new sessions.
    pub fn session_config(&self) -> SessionConfig {
        self.session_config
    }

    /// Singleton schema version. Errors on foreign/multi-row content instead
    /// of picking an arbitrary row.
    pub fn schema_version(&self) -> Result<i64, StoreError> {
        let tables = existing_tables(&self.conn)?;
        if !tables.iter().any(|t| t == "schema_version") {
            return Err(StoreError::IncompatibleSchema(
                "missing schema_version table".to_string(),
            ));
        }
        let mut stmt = self.conn.prepare("SELECT version FROM schema_version")?;
        let versions: Vec<i64> = stmt
            .query_map([], |r| r.get(0))?
            .collect::<Result<Vec<_>, _>>()?;
        match versions.as_slice() {
            [] => Ok(0),
            [v] => Ok(*v),
            many => Err(StoreError::IncompatibleSchema(format!(
                "schema_version holds {} rows; expected singleton",
                many.len()
            ))),
        }
    }

    /// Durable device identity for this database. Only exact v4 databases
    /// open, so any other version here is corruption, not a legacy handle.
    pub fn device_id(&self) -> Result<String, StoreError> {
        let v = self.schema_version()?;
        if v != SCHEMA_VERSION {
            return Err(StoreError::InvalidArgument(format!(
                "unexpected schema version {v} (expected v{SCHEMA_VERSION})"
            )));
        }
        let dev: String = self
            .conn
            .query_row("SELECT device_id FROM device_info", [], |r| r.get(0))
            .map_err(|e| StoreError::Sqlite(format!("missing device_info: {e}")))?;
        crate::desktop_session::normalize_hex_id(&dev, "device_id")
            .map_err(StoreError::InvalidArgument)?;
        Ok(dev)
    }

    /// Append one meaningful snapshot. Returns the row id.
    ///
    /// The immutable historical association (`project_id`) is extracted from
    /// `ctx.project.id` at append time and stored alongside the JSON. Later
    /// registry mutations never rewrite it (no retroactive remap).
    ///
    /// Project UUIDs are validated/normalized before storage: a present
    /// non-empty project id must parse as the existing UUID identity;
    /// invalid ids fail closed (`InvalidArgument`, nothing persisted), and
    /// valid ids are projected consistently lowercase in both the stored
    /// snapshot JSON and the indexed column, so an uppercase caller context
    /// cannot create unqueryable rows. Empty/absent projects store NULL.
    ///
    /// Phase 4: the raw insert plus its session assignment/update/resource
    /// aggregation plus its FTS row is ONE SQLite transaction
    /// (`BEGIN IMMEDIATE`). If any step (including the FTS insert) fails the
    /// raw insert rolls back, so an append failure always means the raw event
    /// was NOT persisted (collector FIFO retry stays true). No triggers;
    /// the writer maintains `activity_fts` explicitly.
    ///
    /// Error-aware: invalid inputs or SQLite failures return `Err` and the
    /// caller must treat history as NOT saved (retry / surface, never assume
    /// success).
    pub fn append(
        &self,
        kind: &str,
        source: &str,
        ctx: &DesktopContext,
    ) -> Result<i64, StoreError> {
        validate_tag("kind", kind)?;
        validate_tag("source", source)?;
        if ctx.observed_at_ms < 0 {
            return Err(StoreError::InvalidArgument(
                "observed_at_ms must be >= 0".to_string(),
            ));
        }
        // Normalize/validate the project projection consistently.
        let mut normalized = ctx.clone();
        if let Some(p) = normalized.project.as_mut() {
            if p.id.is_empty() {
                normalized.project = None;
            } else {
                let canon = normalize_project_id(&p.id)?;
                p.id = canon;
            }
        }
        let snapshot_json = serde_json::to_string(&normalized)
            .map_err(|e| StoreError::InvalidArgument(format!("snapshot serialize: {e}")))?;
        if snapshot_json.len() > MAX_SNAPSHOT_JSON_BYTES {
            return Err(StoreError::InvalidArgument(format!(
                "snapshot_json {} bytes exceeds {}",
                snapshot_json.len(),
                MAX_SNAPSHOT_JSON_BYTES
            )));
        }
        let project_id: Option<String> = normalized
            .project
            .as_ref()
            .map(|p| p.id.clone())
            .filter(|s| !s.is_empty());
        let project_name: Option<String> = normalized
            .project
            .as_ref()
            .map(|p| p.name.clone())
            .filter(|s| !s.is_empty());
        // v4 transactional path: one immediate transaction covers the raw
        // insert plus its session assignment/update/resource aggregation
        // plus its FTS row. `rusqlite::Connection::transaction()` needs
        // `&mut`, which `&self` append cannot provide, so the RAII
        // [`ImmediateTx`] guard issues `BEGIN IMMEDIATE` itself: it fails
        // fast when locked (nothing persisted) and rolls back on drop unless
        // committed.
        let tx = ImmediateTx::begin(&self.conn)?;
        let res: Result<i64, StoreError> = (|| {
            // Device provenance (must exist after init).
            let device_id: String = self
                .conn
                .query_row("SELECT device_id FROM device_info", [], |r| r.get(0))
                .map_err(|e| StoreError::Sqlite(format!("missing device_info: {e}")))?;
            crate::desktop_session::normalize_hex_id(&device_id, "device_id")
                .map_err(StoreError::InvalidArgument)?;
            // Unique event identity (retry on the astronomically unlikely
            // collision via the in-DB check inside this transaction).
            let mut event_id = crate::desktop_session::generate_128bit_hex();
            for _ in 0..3 {
                let n: i64 = self.conn.query_row(
                    "SELECT COUNT(*) FROM activity WHERE event_id = ?1",
                    params![event_id],
                    |r| r.get(0),
                )?;
                if n == 0 {
                    break;
                }
                event_id = crate::desktop_session::generate_128bit_hex();
            }
            // Load LOCAL open sessions only: foreign-device opens (e.g. from
            // a merged database file) are cross-device history the writer
            // must neither close nor extend. Repair to one local open when
            // corrupt, inside the same transaction so the invariant is
            // restored atomically.
            let mut opens = self.load_local_open_sessions_inner(&device_id)?;
            // Corrupt thresholds fail closed (rollback, nothing persisted).
            for o in &opens {
                validate_session_thresholds(o.gap_ms, o.interruption_ms)?;
            }
            if opens.len() > 1 {
                // Keep newest (end/start/id), close the rest as inactivity.
                opens.sort_by(|a, b| {
                    (a.end_ms, a.start_ms, a.session_id.clone()).cmp(&(
                        b.end_ms,
                        b.start_ms,
                        b.session_id.clone(),
                    ))
                });
                let keep = opens.pop().expect("non-empty");
                for extra in &opens {
                    self.conn.execute(
                        "UPDATE sessions SET status='closed', ended_reason='inactivity' WHERE session_id=?1 AND status='open' AND device_id=?2",
                        params![extra.session_id, device_id],
                    )?;
                }
                opens = vec![keep];
            }
            let new_proj = project_id.as_deref();
            let (session_id, is_new, ended_old): (String, bool, Option<(String, String)>) =
                match opens.pop() {
                    None => {
                        let sid = crate::desktop_session::session_id_for(&device_id, &event_id);
                        (sid, true, None)
                    }
                    Some(open) => {
                        let view = crate::desktop_session::OpenSessionView {
                            project_id: open.project_id.clone(),
                            end_ms: open.end_ms,
                            unresolved_start_ms: open.unresolved_start_ms,
                            gap_ms: open.gap_ms,
                            interruption_ms: open.interruption_ms,
                        };
                        match crate::desktop_session::decide_session_action(
                            &view,
                            new_proj,
                            normalized.observed_at_ms,
                        ) {
                            crate::desktop_session::SessionAction::Continue { .. } => {
                                (open.session_id.clone(), false, None)
                            }
                            crate::desktop_session::SessionAction::Split { ended_reason } => {
                                let sid =
                                    crate::desktop_session::session_id_for(&device_id, &event_id);
                                (
                                    sid,
                                    true,
                                    Some((
                                        open.session_id.clone(),
                                        ended_reason.as_str().to_string(),
                                    )),
                                )
                            }
                        }
                    }
                };
            // Raw insert (part of the same transaction).
            self.conn.execute(
                "INSERT INTO activity (observed_at_ms, kind, source, snapshot_json, project_id, event_id, device_id, session_id)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                params![
                    normalized.observed_at_ms,
                    kind,
                    source,
                    snapshot_json,
                    project_id,
                    event_id,
                    device_id,
                    session_id
                ],
            )?;
            let row_id = self.conn.last_insert_rowid();
            // Finalize old session first (when splitting).
            if let Some((old_sid, reason)) = ended_old {
                self.conn.execute(
                    "UPDATE sessions SET status='closed', ended_reason=?1 WHERE session_id=?2",
                    params![reason, old_sid],
                )?;
            }
            if is_new {
                // New session row for this event.
                let app = normalized
                    .focused_window
                    .as_ref()
                    .map(|w| w.application.clone())
                    .filter(|s| !s.is_empty());
                let apps_json = match app {
                    Some(a) => serde_json::to_string(&vec![a]).unwrap_or("[]".to_string()),
                    None => "[]".to_string(),
                };
                let cfg = self.session_config;
                self.conn.execute(
                    "INSERT INTO sessions (session_id, project_id, project_name, start_ms, end_ms,
                     first_activity_id, last_activity_id, event_count, status, ended_reason,
                     gap_ms, interruption_ms, device_id, unresolved_start_ms, applications_json)
                     VALUES (?1,?2,?3,?4,?5,?6,?7,1,'open',NULL,?8,?9,?10,NULL,?11)",
                    params![
                        session_id,
                        project_id,
                        project_name,
                        normalized.observed_at_ms,
                        normalized.observed_at_ms,
                        row_id,
                        row_id,
                        cfg.inactivity_gap_ms(),
                        cfg.interruption_grace_ms(),
                        device_id,
                        apps_json
                    ],
                )?;
            } else {
                // Extend the open session: start=min/end=max (clock rollback
                // never corrupts ranges), count+1, last=row (insertion
                // order), unresolved run tracking, distinct apps.
                // Reload current values (we consumed `opens`, so re-query).
                let cur: InnerSession = self
                    .conn
                    .query_row(
                        "SELECT session_id, project_id, start_ms, end_ms, gap_ms, interruption_ms, unresolved_start_ms, applications_json, event_count FROM sessions WHERE session_id=?1",
                        params![session_id],
                        |r| {
                            Ok(InnerSession {
                                session_id: r.get(0)?,
                                project_id: r.get(1)?,
                                start_ms: r.get(2)?,
                                end_ms: r.get(3)?,
                                gap_ms: r.get(4)?,
                                interruption_ms: r.get(5)?,
                                unresolved_start_ms: r.get(6)?,
                                applications_json: r.get(7)?,
                                event_count: r.get(8)?,
                                first_activity_id: 0,
                                last_activity_id: 0,
                                status: String::new(),
                                ended_reason: None,
                                project_name: None,
                                device_id: String::new(),
                            })
                        },
                    )
                    .map_err(|e| StoreError::Sqlite(e.to_string()))?;
                validate_session_thresholds(cur.gap_ms, cur.interruption_ms)?;
                let view = crate::desktop_session::OpenSessionView {
                    project_id: cur.project_id.clone(),
                    end_ms: cur.end_ms,
                    unresolved_start_ms: cur.unresolved_start_ms,
                    gap_ms: cur.gap_ms,
                    interruption_ms: cur.interruption_ms,
                };
                let new_unresolved = match crate::desktop_session::decide_session_action(
                    &view,
                    new_proj,
                    normalized.observed_at_ms,
                ) {
                    crate::desktop_session::SessionAction::Continue {
                        new_unresolved_start,
                    } => new_unresolved_start,
                    crate::desktop_session::SessionAction::Split { .. } => {
                        return Err(StoreError::Sqlite(
                            "session decision changed mid-transaction".to_string(),
                        ))
                    }
                };
                let new_start = cur.start_ms.min(normalized.observed_at_ms);
                let new_end = cur.end_ms.max(normalized.observed_at_ms);
                // Merge distinct applications.
                let mut apps: Vec<String> =
                    serde_json::from_str(&cur.applications_json).unwrap_or_default();
                if let Some(a) = normalized
                    .focused_window
                    .as_ref()
                    .map(|w| w.application.clone())
                    .filter(|s| !s.is_empty())
                {
                    if !apps.contains(&a) {
                        apps.push(a);
                        apps.sort();
                    }
                }
                let apps_json = serde_json::to_string(&apps).unwrap_or("[]".to_string());
                self.conn.execute(
                    "UPDATE sessions SET start_ms=?1, end_ms=?2, last_activity_id=?3, event_count=?4, unresolved_start_ms=?5, applications_json=?6 WHERE session_id=?7",
                    params![new_start, new_end, row_id, cur.event_count + 1, new_unresolved, apps_json, session_id],
                )?;
            }
            // Compact resource aggregation for this event. The identity is
            // anchored to the event's own resolved project (see
            // `resource_identity`); first/last seen track min/max
            // timestamps while first/last activity ids stay in insertion
            // order. The FTS resource_key below reuses the same identity.
            let mut fts_resource_key = String::new();
            if let Some(res) = normalized.resource.as_ref().filter(|r| !r.is_empty()) {
                if let Some(ident) =
                    crate::desktop_session::resource_identity(res, project_id.as_deref())
                {
                    fts_resource_key = ident.key.clone();
                    let latest_json = serde_json::to_string(res).unwrap_or("{}".to_string());
                    let existing: Option<(i64, i64, i64)> = self
                        .conn
                        .query_row(
                            "SELECT occurrence_count, first_seen_ms, first_activity_id FROM session_resources WHERE session_id=?1 AND resource_key=?2",
                            params![session_id, ident.key],
                            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                        )
                        .optional()
                        .map_err(|e| StoreError::Sqlite(e.to_string()))?;
                    match existing {
                        Some((cnt, _first_seen, _first_id)) => {
                            self.conn.execute(
                                "UPDATE session_resources SET occurrence_count=?1, first_seen_ms=min(first_seen_ms,?2), last_seen_ms=max(last_seen_ms,?2), last_activity_id=?3, latest_resource_json=?4 WHERE session_id=?5 AND resource_key=?6",
                                params![cnt + 1, normalized.observed_at_ms, row_id, latest_json, session_id, ident.key],
                            )?;
                        }
                        None => {
                            self.conn.execute(
                                "INSERT INTO session_resources (session_id, resource_key, kind, portable_identity, local_identity, latest_resource_json, occurrence_count, first_seen_ms, last_seen_ms, first_activity_id, last_activity_id)
                                 VALUES (?1,?2,?3,?4,?5,?6,1,?7,?7,?8,?8)",
                                params![
                                    session_id,
                                    ident.key,
                                    ident.kind.as_str(),
                                    ident.portable_identity,
                                    ident.local_identity,
                                    latest_json,
                                    normalized.observed_at_ms,
                                    row_id
                                ],
                            )?;
                        }
                    }
                }
            }
            // Application-maintained FTS row (no triggers): derived from the
            // already-validated normalized context + resource identity above.
            // Any failure rolls back the whole append (raw + sessions +
            // resources + FTS stay atomic).
            {
                let (p, app, title, ws, res_text, meta, _key) =
                    fts_row_for(&normalized, kind, source, project_id.as_deref());
                // `_key` must equal the aggregation key above (recomputed for
                // clarity); prefer the aggregation value when present.
                let key = if fts_resource_key.is_empty() {
                    _key
                } else {
                    fts_resource_key
                };
                self.conn
                    .execute(
                        "INSERT INTO activity_fts(rowid, session_id, resource_key, project, application, window_title, workspace, resource, metadata) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)",
                        params![
                            row_id, session_id, key, p, app, title, ws, res_text, meta
                        ],
                    )
                    .map_err(|e| StoreError::Sqlite(format!("FTS insert failed: {e}")))?;
            }
            Ok(row_id)
        })();
        match res {
            // The guard rolls back on drop for `Err`; an explicit rollback
            // on COMMIT failure covers a commit that leaves the transaction
            // open (see `ImmediateTx::commit`).
            Ok(id) => tx.commit().map(|()| id),
            Err(e) => Err(e),
        }
    }

    fn checked_limit(limit: i64) -> Result<i64, StoreError> {
        if limit < 1 || limit > MAX_QUERY_LIMIT {
            return Err(StoreError::InvalidArgument(format!(
                "limit must be 1..={MAX_QUERY_LIMIT}, got {limit}"
            )));
        }
        Ok(limit)
    }

    fn checked_range(start_ms: i64, end_ms: i64) -> Result<(), StoreError> {
        if start_ms < 0 || end_ms < 0 {
            return Err(StoreError::InvalidArgument(
                "range bounds must be >= 0 (UTC epoch-ms)".to_string(),
            ));
        }
        if start_ms > end_ms {
            return Err(StoreError::InvalidArgument(format!(
                "invalid range: start_ms ({start_ms}) > end_ms ({end_ms})"
            )));
        }
        Ok(())
    }

    /// Columns of the v3 `activity` row (the only supported shape).
    const ACTIVITY_COLS: &'static str =
        "id, observed_at_ms, kind, source, snapshot_json, event_id, device_id, session_id";

    /// Newest-first bounded query. Deterministic: `(observed_at_ms DESC,
    /// id DESC)` so equal timestamps order by row id.
    pub fn recent_activity(&self, limit: i64) -> Result<Vec<ActivityRecord>, StoreError> {
        let limit = Self::checked_limit(limit)?;
        let sql = format!(
            "SELECT {} FROM activity ORDER BY observed_at_ms DESC, id DESC LIMIT ?1",
            Self::ACTIVITY_COLS
        );
        let mut stmt = self.conn.prepare(&sql)?;
        let rows = stmt.query_map(params![limit], row_to_record_v3)?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r?);
        }
        Ok(out)
    }

    /// Range query: `start_ms` inclusive, `end_ms` exclusive, UTC epoch-ms.
    /// Oldest-first deterministic: `(observed_at_ms ASC, id ASC)`, bounded.
    pub fn activity_in_range(
        &self,
        start_ms: i64,
        end_ms: i64,
        limit: i64,
    ) -> Result<Vec<ActivityRecord>, StoreError> {
        let limit = Self::checked_limit(limit)?;
        Self::checked_range(start_ms, end_ms)?;
        let sql = format!(
            "SELECT {} FROM activity WHERE observed_at_ms >= ?1 AND observed_at_ms < ?2
             ORDER BY observed_at_ms ASC, id ASC LIMIT ?3",
            Self::ACTIVITY_COLS
        );
        let mut stmt = self.conn.prepare(&sql)?;
        let rows = stmt.query_map(params![start_ms, end_ms, limit], row_to_record_v3)?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r?);
        }
        Ok(out)
    }

    /// Newest-first bounded query for one project UUID.
    ///
    /// Validates the id as the existing UUID identity (normalized to
    /// lowercase) and `limit` as `1..=MAX_QUERY_LIMIT`. Uses the indexed
    /// `project_id` column. Ordering is `(observed_at_ms DESC, id DESC)`.
    pub fn recent_activity_for_project(
        &self,
        project_id: &str,
        limit: i64,
    ) -> Result<Vec<ActivityRecord>, StoreError> {
        let pid = normalize_project_id(project_id)?;
        let limit = Self::checked_limit(limit)?;
        let sql = format!(
            "SELECT {} FROM activity WHERE project_id = ?1
             ORDER BY observed_at_ms DESC, id DESC LIMIT ?2",
            Self::ACTIVITY_COLS
        );
        let mut stmt = self.conn.prepare(&sql)?;
        let rows = stmt.query_map(params![pid, limit], row_to_record_v3)?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r?);
        }
        Ok(out)
    }

    /// Range + project filter: `start_ms` inclusive, `end_ms` exclusive,
    /// oldest-first `(observed_at_ms ASC, id ASC)`, bounded. Lets the CLI
    /// combine `--project` with `--from/--to` instead of silently ignoring
    /// one filter.
    pub fn activity_in_range_for_project(
        &self,
        project_id: &str,
        start_ms: i64,
        end_ms: i64,
        limit: i64,
    ) -> Result<Vec<ActivityRecord>, StoreError> {
        let pid = normalize_project_id(project_id)?;
        let limit = Self::checked_limit(limit)?;
        Self::checked_range(start_ms, end_ms)?;
        let sql = format!(
            "SELECT {} FROM activity WHERE project_id = ?1 AND observed_at_ms >= ?2 AND observed_at_ms < ?3
             ORDER BY observed_at_ms ASC, id ASC LIMIT ?4",
            Self::ACTIVITY_COLS
        );
        let mut stmt = self.conn.prepare(&sql)?;
        let rows = stmt.query_map(params![pid, start_ms, end_ms, limit], row_to_record_v3)?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r?);
        }
        Ok(out)
    }

    /// Latest row for one project UUID, or `None` when the project has no
    /// history. Validates the UUID even when empty (unknown projects return
    /// `None`, malformed ids error).
    pub fn last_activity_for_project(
        &self,
        project_id: &str,
    ) -> Result<Option<ActivityRecord>, StoreError> {
        let rows = self.recent_activity_for_project(project_id, 1)?;
        Ok(rows.into_iter().next())
    }

    /// Recently-used resources for one project UUID, newest-first.
    ///
    /// Dedup key is the priority location key file>url>cwd>page.
    /// See module docs.
    pub fn recently_used_resources_for_project(
        &self,
        project_id: &str,
        limit: i64,
    ) -> Result<Vec<ProjectResource>, StoreError> {
        let pid = normalize_project_id(project_id)?;
        let limit = Self::checked_limit(limit)?;
        let sql = format!(
            "SELECT {} FROM activity WHERE project_id = ?1 ORDER BY observed_at_ms DESC, id DESC",
            Self::ACTIVITY_COLS
        );
        let mut stmt = self.conn.prepare(&sql)?;
        let mut rows = stmt.query(params![pid])?;
        let mut seen: std::collections::HashSet<(char, String)> = std::collections::HashSet::new();
        let mut out: Vec<ProjectResource> = Vec::new();
        while let Some(row) = rows.next()? {
            let record = row_to_record_v3(row)?;
            let Ok(ctx) = record.snapshot() else { continue };
            let Some(res) = ctx.resource else { continue };
            if res.is_empty() {
                continue;
            }
            let Some(key) = resource_location_key(&res) else {
                continue;
            };
            if !seen.insert(key) {
                continue;
            }
            out.push(ProjectResource {
                resource: res,
                observed_at_ms: record.observed_at_ms,
                activity_id: record.id,
            });
            if out.len() as i64 >= limit {
                break;
            }
        }
        Ok(out)
    }

    // ---- Session queries (v4 store; migrated v3 history is preserved) ----

    /// Open sessions belonging to the local durable device only.
    /// Foreign-device opens are cross-device history: listed by session
    /// queries but never continued, repaired, or reported as current.
    fn load_local_open_sessions_inner(
        &self,
        device_id: &str,
    ) -> Result<Vec<InnerSession>, StoreError> {
        let mut stmt = self.conn.prepare(
            "SELECT session_id, project_id, project_name, start_ms, end_ms,
             first_activity_id, last_activity_id, event_count, status, ended_reason,
             gap_ms, interruption_ms, device_id, unresolved_start_ms, applications_json
             FROM sessions WHERE status='open' AND device_id=?1",
        )?;
        let rows = stmt.query_map(params![device_id], row_to_inner_session)?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r?);
        }
        Ok(out)
    }

    /// Read-only local current session: the newest open session of this
    /// database's durable device, returned only while query-time effective
    /// status is `active` at `now_ms` — i.e. the stored inactivity gap has
    /// not expired (`now >= end + gap` is stale) and no absorbed unresolved
    /// run has reached the stored grace (`now - unresolved_start >= grace`
    /// is interrupted; grace `0` deactivates at the first unresolved event).
    /// Otherwise `None` until the next writer finalizes it; never mutates a
    /// read-only DB.
    pub fn current_session(&self, now_ms: i64) -> Result<Option<SessionRecord>, StoreError> {
        if now_ms < 0 {
            return Err(StoreError::InvalidArgument(
                "now_ms must be >= 0 (UTC epoch-ms)".to_string(),
            ));
        }
        let device_id: String = self
            .conn
            .query_row("SELECT device_id FROM device_info", [], |r| r.get(0))
            .map_err(|e| StoreError::Sqlite(format!("missing device_info: {e}")))?;
        let rec = self
            .conn
            .query_row(
                "SELECT session_id, project_id, project_name, start_ms, end_ms,
             first_activity_id, last_activity_id, event_count, status, ended_reason,
             gap_ms, interruption_ms, device_id, unresolved_start_ms, applications_json
             FROM sessions WHERE status='open' AND device_id=?1
             ORDER BY end_ms DESC, start_ms DESC, session_id DESC LIMIT 1",
                params![device_id],
                row_to_inner_session,
            )
            .optional()
            .map_err(|e| StoreError::Sqlite(e.to_string()))?;
        let Some(inner) = rec else { return Ok(None) };
        validate_session_thresholds(inner.gap_ms, inner.interruption_ms).map_err(|e| {
            StoreError::IncompatibleSchema(format!("corrupt session thresholds: {e}"))
        })?;
        let record = inner.into_record()?;
        if record.is_active_at(now_ms) {
            Ok(Some(record))
        } else {
            Ok(None)
        }
    }

    /// Newest-first bounded sessions. Deterministic by
    /// `(end_ms DESC, start_ms DESC, session_id DESC)`.
    pub fn recent_sessions(&self, limit: i64) -> Result<Vec<SessionRecord>, StoreError> {
        let limit = Self::checked_limit(limit)?;
        let mut stmt = self.conn.prepare(
            "SELECT session_id, project_id, project_name, start_ms, end_ms,
             first_activity_id, last_activity_id, event_count, status, ended_reason,
             gap_ms, interruption_ms, device_id, unresolved_start_ms, applications_json
             FROM sessions ORDER BY end_ms DESC, start_ms DESC, session_id DESC LIMIT ?1",
        )?;
        let rows = stmt.query_map(params![limit], row_to_inner_session)?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r?.into_record()?);
        }
        Ok(out)
    }

    /// Newest-first bounded sessions for one project UUID.
    pub fn recent_sessions_for_project(
        &self,
        project_id: &str,
        limit: i64,
    ) -> Result<Vec<SessionRecord>, StoreError> {
        let pid = normalize_project_id(project_id)?;
        let limit = Self::checked_limit(limit)?;
        let mut stmt = self.conn.prepare(
            "SELECT session_id, project_id, project_name, start_ms, end_ms,
             first_activity_id, last_activity_id, event_count, status, ended_reason,
             gap_ms, interruption_ms, device_id, unresolved_start_ms, applications_json
             FROM sessions WHERE project_id = ?1
             ORDER BY end_ms DESC, start_ms DESC, session_id DESC LIMIT ?2",
        )?;
        let rows = stmt.query_map(params![pid, limit], row_to_inner_session)?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r?.into_record()?);
        }
        Ok(out)
    }

    /// Overlap range: `session end >= range start AND session start < range
    /// end` (start inclusive, end exclusive), oldest-first by
    /// `(start_ms ASC, end_ms ASC, session_id ASC)`.
    pub fn sessions_in_range(
        &self,
        start_ms: i64,
        end_ms: i64,
        limit: i64,
    ) -> Result<Vec<SessionRecord>, StoreError> {
        let limit = Self::checked_limit(limit)?;
        Self::checked_range(start_ms, end_ms)?;
        if start_ms == end_ms {
            return Ok(Vec::new());
        }
        let mut stmt = self.conn.prepare(
            "SELECT session_id, project_id, project_name, start_ms, end_ms,
             first_activity_id, last_activity_id, event_count, status, ended_reason,
             gap_ms, interruption_ms, device_id, unresolved_start_ms, applications_json
             FROM sessions WHERE end_ms >= ?1 AND start_ms < ?2
             ORDER BY start_ms ASC, end_ms ASC, session_id ASC LIMIT ?3",
        )?;
        let rows = stmt.query_map(params![start_ms, end_ms, limit], row_to_inner_session)?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r?.into_record()?);
        }
        Ok(out)
    }

    /// Overlap range + project filter, oldest-first.
    pub fn sessions_in_range_for_project(
        &self,
        project_id: &str,
        start_ms: i64,
        end_ms: i64,
        limit: i64,
    ) -> Result<Vec<SessionRecord>, StoreError> {
        let pid = normalize_project_id(project_id)?;
        let limit = Self::checked_limit(limit)?;
        Self::checked_range(start_ms, end_ms)?;
        if start_ms == end_ms {
            return Ok(Vec::new());
        }
        let mut stmt = self.conn.prepare(
            "SELECT session_id, project_id, project_name, start_ms, end_ms,
             first_activity_id, last_activity_id, event_count, status, ended_reason,
             gap_ms, interruption_ms, device_id, unresolved_start_ms, applications_json
             FROM sessions WHERE project_id = ?1 AND end_ms >= ?2 AND start_ms < ?3
             ORDER BY start_ms ASC, end_ms ASC, session_id ASC LIMIT ?4",
        )?;
        let rows = stmt.query_map(params![pid, start_ms, end_ms, limit], row_to_inner_session)?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r?.into_record()?);
        }
        Ok(out)
    }

    /// Latest session for one project UUID (newest by end/start/id), or
    /// `None` when the project has no sessions.
    pub fn last_session_for_project(
        &self,
        project_id: &str,
    ) -> Result<Option<SessionRecord>, StoreError> {
        let rows = self.recent_sessions_for_project(project_id, 1)?;
        Ok(rows.into_iter().next())
    }

    /// One session by stable ID, or `None` (validates the 32-hex shape even
    /// when empty so malformed IDs error before missing-DB fast paths in the
    /// CLI layer; here the DB is already open).
    pub fn session_by_id(&self, session_id: &str) -> Result<Option<SessionRecord>, StoreError> {
        let sid = normalize_session_id(session_id)?;
        let rec = self
            .conn
            .query_row(
                "SELECT session_id, project_id, project_name, start_ms, end_ms,
             first_activity_id, last_activity_id, event_count, status, ended_reason,
             gap_ms, interruption_ms, device_id, unresolved_start_ms, applications_json
             FROM sessions WHERE session_id = ?1",
                params![sid],
                row_to_inner_session,
            )
            .optional()
            .map_err(|e| StoreError::Sqlite(e.to_string()))?;
        match rec {
            Some(inner) => Ok(Some(inner.into_record()?)),
            None => Ok(None),
        }
    }

    /// Compact session resources, newest-first by `last_seen_ms`
    /// (`last_activity_id` tie-break, deterministic).
    pub fn session_resources(
        &self,
        session_id: &str,
        limit: i64,
    ) -> Result<Vec<SessionResourceRecord>, StoreError> {
        let sid = normalize_session_id(session_id)?;
        let limit = Self::checked_limit(limit)?;
        let mut stmt = self.conn.prepare(
            "SELECT session_id, resource_key, kind, portable_identity, local_identity,
             latest_resource_json, occurrence_count, first_seen_ms, last_seen_ms,
             first_activity_id, last_activity_id
             FROM session_resources WHERE session_id = ?1
             ORDER BY last_seen_ms DESC, last_activity_id DESC, resource_key ASC LIMIT ?2",
        )?;
        let rows = stmt.query_map(params![sid, limit], row_to_session_resource)?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r?);
        }
        Ok(out)
    }

    /// Session events, oldest-first by `(observed_at_ms ASC, id ASC)`.
    pub fn session_events(
        &self,
        session_id: &str,
        limit: i64,
    ) -> Result<Vec<ActivityRecord>, StoreError> {
        let sid = normalize_session_id(session_id)?;
        let limit = Self::checked_limit(limit)?;
        let mut stmt = self.conn.prepare(
            "SELECT id, observed_at_ms, kind, source, snapshot_json, event_id, device_id, session_id
             FROM activity WHERE session_id = ?1
             ORDER BY observed_at_ms ASC, id ASC LIMIT ?2",
        )?;
        let rows = stmt.query_map(params![sid, limit], row_to_record_v3)?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r?);
        }
        Ok(out)
    }

    /// Validated search query (normalized project/device, trimmed texts).
    fn validate_search_query(q: &SessionSearchQuery) -> Result<ValidatedSearch, StoreError> {
        let limit = Self::checked_limit(q.limit)?;
        // Paired range.
        let range = match (q.start_ms, q.end_ms) {
            (None, None) => None,
            (Some(s), Some(e)) => {
                Self::checked_range(s, e)?;
                Some((s, e))
            }
            _ => {
                return Err(StoreError::InvalidArgument(
                    "--from and --to must be given together".to_string(),
                ))
            }
        };
        let project = match q.project_id.as_deref() {
            None => None,
            Some(p) => Some(normalize_project_id(p)?),
        };
        let device = match q.device_id.as_deref() {
            None => None,
            Some(d) => Some(
                crate::desktop_session::normalize_hex_id(d, "device_id")
                    .map_err(StoreError::InvalidArgument)?,
            ),
        };
        let application = match q.application.as_deref() {
            None => None,
            Some(t) => Some(validate_search_text("application", t)?),
        };
        let resource = match q.resource.as_deref() {
            None => None,
            Some(t) => Some(validate_search_text("resource", t)?),
        };
        let query = match q.query.as_deref() {
            None => None,
            Some(t) => Some(validate_search_text("query", t)?),
        };
        // Build one literal FTS MATCH expression (AND across filters).
        let mut parts: Vec<String> = Vec::new();
        if let Some(a) = application.as_deref() {
            parts.push(fts_column_and("application", a));
        }
        if let Some(r) = resource.as_deref() {
            parts.push(fts_column_and("resource", r));
        }
        if let Some(f) = query.as_deref() {
            parts.push(fts_general_and(f));
        }
        let fts = if parts.is_empty() {
            None
        } else {
            Some(parts.join(" AND "))
        };
        Ok(ValidatedSearch {
            range,
            project,
            device,
            fts,
            limit,
        })
    }

    /// Session-centric search over FTS + structured filters.
    ///
    /// - Project/device/range are normal SQL predicates; app/resource/query
    ///   are escaped/literal FTS5 `MATCH` (column-scoped/general).
    /// - FTS/range session discovery traverses the FTS matches ONCE: a
    ///   single grouped `activity ⨝ activity_fts` query (one `MATCH`,
    ///   event-level range restriction, grouped by session with
    ///   `MAX(observed_at_ms)`) joined to `sessions` for project/device
    ///   filtering — never a correlated `MATCH` per candidate session.
    ///   Structured no-FTS queries stay indexed/bounded on `sessions`.
    /// - Text matches inside a range come from activity rows in that range
    ///   (`activity.observed_at_ms` in `[start, end)` joined to the FTS row),
    ///   not merely another event in an overlapping session.
    /// - FTS/range hits carry `matched_at_ms` (newest matching observation)
    ///   and order newest matching observation first (then session
    ///   end/start/id), so `limit = 1` answers "last matching observation",
    ///   including multi-device overlap. Project/device-only and no-filter
    ///   searches carry `matched_at_ms = None` with session-recency order.
    /// - Per-session resources favor actually-matched keys ranked by newest
    ///   matching activity (deterministic), capped at
    ///   [`MAX_SEARCH_RESOURCES_PER_SESSION`]. No raw events are returned.
    /// - No filter means recent sessions.
    pub fn search_sessions(
        &self,
        q: &SessionSearchQuery,
    ) -> Result<Vec<SessionSearchResult>, StoreError> {
        let v = Self::validate_search_query(q)?;
        // Empty range (start == end) matches no activity; return early.
        if let Some((s, e)) = v.range {
            if s == e {
                return Ok(Vec::new());
            }
        }
        let has_structured = v.project.is_some() || v.device.is_some() || v.range.is_some();
        if v.fts.is_none() && !has_structured {
            // No filter: recent sessions + newest resources.
            let sessions = self.recent_sessions(v.limit)?;
            let mut out = Vec::new();
            for s in sessions {
                let res =
                    self.session_resources(&s.session_id, MAX_SEARCH_RESOURCES_PER_SESSION)?;
                out.push(SessionSearchResult {
                    session: s,
                    matched_at_ms: None,
                    resources: res,
                });
            }
            return Ok(out);
        }
        if v.fts.is_none() && v.range.is_none() {
            // Project/device-only: indexed sessions-table queries, no FTS
            // traversal, session-recency order, no matched timestamps.
            let sessions = self.structured_sessions_no_fts(
                v.project.as_deref(),
                v.device.as_deref(),
                v.limit,
            )?;
            let mut out = Vec::new();
            for s in sessions {
                let res =
                    self.session_resources(&s.session_id, MAX_SEARCH_RESOURCES_PER_SESSION)?;
                out.push(SessionSearchResult {
                    session: s,
                    matched_at_ms: None,
                    resources: res,
                });
            }
            return Ok(out);
        }
        // FTS and/or range: one grouped matched-session traversal.
        let matched = self.matched_sessions_single_scan(&v)?;
        if matched.is_empty() {
            return Ok(Vec::new());
        }
        // Fetch full session rows for the ranked ids, preserving rank order.
        let ids: Vec<String> = matched.iter().map(|m| m.session_id.clone()).collect();
        let by_id = self.sessions_by_ids(&ids)?;
        // Matched resource keys for all returned sessions in ONE batched FTS
        // traversal (fix: no per-session MATCH loop for discovery).
        let ranked_keys = if v.fts.is_some() {
            self.batched_ranked_resource_keys(&ids, v.fts.as_deref().unwrap(), v.range)?
        } else {
            std::collections::HashMap::new()
        };
        let mut out = Vec::new();
        for m in &matched {
            let Some(session) = by_id.get(&m.session_id) else {
                continue;
            };
            let resources = match ranked_keys.get(&m.session_id) {
                Some(keys) => self.resources_for_ranked_keys(&m.session_id, keys)?,
                None => self.session_resources(&m.session_id, MAX_SEARCH_RESOURCES_PER_SESSION)?,
            };
            out.push(SessionSearchResult {
                session: session.clone(),
                matched_at_ms: Some(m.matched_at_ms),
                resources,
            });
        }
        Ok(out)
    }

    /// Indexed project/device-only session listing (no FTS, no range):
    /// newest-first, bounded. At least one of project/device is `Some`.
    fn structured_sessions_no_fts(
        &self,
        project: Option<&str>,
        device: Option<&str>,
        limit: i64,
    ) -> Result<Vec<SessionRecord>, StoreError> {
        let limit = Self::checked_limit(limit)?;
        const COLS: &str = "session_id, project_id, project_name, start_ms, end_ms, first_activity_id, last_activity_id, event_count, status, ended_reason, gap_ms, interruption_ms, device_id, unresolved_start_ms, applications_json";
        let (sql, args): (String, Vec<rusqlite::types::Value>) = match (project, device) {
            (Some(p), Some(d)) => (
                format!("SELECT {COLS} FROM sessions WHERE project_id = ?1 AND device_id = ?2 ORDER BY end_ms DESC, start_ms DESC, session_id DESC LIMIT ?3"),
                vec![
                    rusqlite::types::Value::Text(p.to_string()),
                    rusqlite::types::Value::Text(d.to_string()),
                    rusqlite::types::Value::Integer(limit),
                ],
            ),
            (Some(p), None) => {
                return self.recent_sessions_for_project(p, limit);
            }
            (None, Some(d)) => (
                format!("SELECT {COLS} FROM sessions WHERE device_id = ?1 ORDER BY end_ms DESC, start_ms DESC, session_id DESC LIMIT ?2"),
                vec![
                    rusqlite::types::Value::Text(d.to_string()),
                    rusqlite::types::Value::Integer(limit),
                ],
            ),
            (None, None) => return self.recent_sessions(limit),
        };
        let mut stmt = self.conn.prepare(&sql)?;
        let pref: Vec<&dyn rusqlite::ToSql> =
            args.iter().map(|a| a as &dyn rusqlite::ToSql).collect();
        let rows = stmt.query_map(pref.as_slice(), row_to_inner_session)?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r?.into_record()?);
        }
        Ok(out)
    }

    /// Single-traversal matched-session discovery for FTS and/or range
    /// searches: one `activity ⨝ activity_fts` scan (exactly one `MATCH`
    /// when FTS text is present, zero otherwise), event-level range
    /// restriction, session join for project/device/time predicates, grouped
    /// by session with the newest matching observation. Ordered newest
    /// matching observation first (then session end/start/id), bounded.
    fn matched_sessions_single_scan(
        &self,
        v: &ValidatedSearch,
    ) -> Result<Vec<MatchedSession>, StoreError> {
        let (sql, args) = build_matched_session_query(v);
        let mut stmt = self.conn.prepare(&sql)?;
        let pref: Vec<&dyn rusqlite::ToSql> =
            args.iter().map(|a| a as &dyn rusqlite::ToSql).collect();
        let mut rows = stmt.query(pref.as_slice())?;
        let mut out = Vec::new();
        while let Some(row) = rows.next()? {
            out.push(MatchedSession {
                session_id: row.get(0)?,
                matched_at_ms: row.get(1)?,
            });
        }
        Ok(out)
    }

    /// Fetch full session rows for the given ids (one bounded `IN` query).
    fn sessions_by_ids(
        &self,
        ids: &[String],
    ) -> Result<std::collections::HashMap<String, SessionRecord>, StoreError> {
        let mut out = std::collections::HashMap::new();
        if ids.is_empty() {
            return Ok(out);
        }
        let placeholders = ids.iter().map(|_| "?").collect::<Vec<_>>().join(",");
        let sql = format!(
            "SELECT session_id, project_id, project_name, start_ms, end_ms, first_activity_id, last_activity_id, event_count, status, ended_reason, gap_ms, interruption_ms, device_id, unresolved_start_ms, applications_json FROM sessions WHERE session_id IN ({placeholders})"
        );
        let mut stmt = self.conn.prepare(&sql)?;
        let args: Vec<rusqlite::types::Value> = ids
            .iter()
            .map(|s| rusqlite::types::Value::Text(s.clone()))
            .collect();
        let pref: Vec<&dyn rusqlite::ToSql> =
            args.iter().map(|a| a as &dyn rusqlite::ToSql).collect();
        let rows = stmt.query_map(pref.as_slice(), row_to_inner_session)?;
        for r in rows {
            let rec = r?.into_record()?;
            out.insert(rec.session_id.clone(), rec);
        }
        Ok(out)
    }

    /// Batched ranked matched-resource keys for the returned sessions: ONE
    /// FTS traversal (`session_id IN (…)` + single `MATCH`), grouped by
    /// `(session, key)` with newest matching activity first. Returns at most
    /// [`MAX_SEARCH_RESOURCES_PER_SESSION`] keys per session, deterministically
    /// ordered (matching timestamp DESC, activity id DESC, key ASC).
    fn batched_ranked_resource_keys(
        &self,
        session_ids: &[String],
        fts_expr: &str,
        range: Option<(i64, i64)>,
    ) -> Result<std::collections::HashMap<String, Vec<String>>, StoreError> {
        let mut out: std::collections::HashMap<String, Vec<String>> =
            std::collections::HashMap::new();
        if session_ids.is_empty() {
            return Ok(out);
        }
        let placeholders = session_ids
            .iter()
            .map(|_| "?")
            .collect::<Vec<_>>()
            .join(",");
        // NOTE: FTS5 MATCH must name the table (`activity_fts`), not an alias.
        let mut sql = format!(
            "SELECT a.session_id AS sid, activity_fts.resource_key AS k, MAX(a.observed_at_ms) AS m, MAX(a.id) AS mi FROM activity a JOIN activity_fts ON activity_fts.rowid = a.id WHERE a.session_id IN ({placeholders}) AND activity_fts MATCH ?"
        );
        let mut args: Vec<rusqlite::types::Value> = session_ids
            .iter()
            .map(|s| rusqlite::types::Value::Text(s.clone()))
            .collect();
        args.push(rusqlite::types::Value::Text(fts_expr.to_string()));
        if let Some((s, e)) = range {
            sql.push_str(" AND a.observed_at_ms >= ? AND a.observed_at_ms < ?");
            args.push(rusqlite::types::Value::Integer(s));
            args.push(rusqlite::types::Value::Integer(e));
        }
        sql.push_str(" GROUP BY sid, k HAVING k != '' ORDER BY sid ASC, m DESC, mi DESC, k ASC");
        let mut stmt = self.conn.prepare(&sql)?;
        let pref: Vec<&dyn rusqlite::ToSql> =
            args.iter().map(|a| a as &dyn rusqlite::ToSql).collect();
        let mut rows = stmt.query(pref.as_slice())?;
        while let Some(row) = rows.next()? {
            let sid: String = row.get(0)?;
            let key: String = row.get(1)?;
            let entry = out.entry(sid).or_default();
            if (entry.len() as i64) < MAX_SEARCH_RESOURCES_PER_SESSION {
                entry.push(key);
            }
        }
        Ok(out)
    }

    /// Resources for one hit given deterministically ranked matched keys:
    /// ranked matches first (in rank order), then other recent resources to
    /// fill up to [`MAX_SEARCH_RESOURCES_PER_SESSION`].
    fn resources_for_ranked_keys(
        &self,
        session_id: &str,
        ranked_keys: &[String],
    ) -> Result<Vec<SessionResourceRecord>, StoreError> {
        let sid = normalize_session_id(session_id)?;
        if ranked_keys.is_empty() {
            return self.session_resources(&sid, MAX_SEARCH_RESOURCES_PER_SESSION);
        }
        let placeholders = ranked_keys
            .iter()
            .map(|_| "?")
            .collect::<Vec<_>>()
            .join(",");
        let sql = format!(
            "SELECT session_id, resource_key, kind, portable_identity, local_identity, latest_resource_json, occurrence_count, first_seen_ms, last_seen_ms, first_activity_id, last_activity_id FROM session_resources WHERE session_id = ?1 AND resource_key IN ({placeholders})"
        );
        let mut stmt = self.conn.prepare(&sql)?;
        let mut args: Vec<rusqlite::types::Value> = vec![rusqlite::types::Value::Text(sid.clone())];
        for k in ranked_keys {
            args.push(rusqlite::types::Value::Text(k.clone()));
        }
        let pref: Vec<&dyn rusqlite::ToSql> =
            args.iter().map(|a| a as &dyn rusqlite::ToSql).collect();
        let rows = stmt.query_map(pref.as_slice(), row_to_session_resource)?;
        let mut by_key = std::collections::HashMap::new();
        for r in rows {
            let rec = r?;
            by_key.insert(rec.resource_key.clone(), rec);
        }
        let mut out = Vec::new();
        for k in ranked_keys {
            if let Some(rec) = by_key.remove(k) {
                out.push(rec);
            }
            if out.len() as i64 >= MAX_SEARCH_RESOURCES_PER_SESSION {
                return Ok(out);
            }
        }
        // Fill with other recent resources (excluding matched).
        let recent = self.session_resources(&sid, 32)?;
        let mut seen: std::collections::HashSet<String> =
            out.iter().map(|r| r.resource_key.clone()).collect();
        for r in recent {
            if out.len() as i64 >= MAX_SEARCH_RESOURCES_PER_SESSION {
                break;
            }
            if seen.insert(r.resource_key.clone()) {
                out.push(r);
            }
        }
        Ok(out)
    }

    /// Session detail: header + bounded resources + optionally bounded raw
    /// events. Raw events are only fetched when `include_events` is true.
    pub fn session_detail(
        &self,
        session_id: &str,
        resource_limit: i64,
        include_events: bool,
        event_limit: i64,
    ) -> Result<SessionDetail, StoreError> {
        let sid = normalize_session_id(session_id)?;
        let resource_limit = Self::checked_limit(resource_limit)?;
        if include_events {
            Self::checked_limit(event_limit)?;
        }
        let session = self.session_by_id(&sid)?;
        match session {
            None => Ok(SessionDetail {
                session: None,
                resources: Vec::new(),
                events_included: include_events,
                events: Vec::new(),
            }),
            Some(s) => {
                let resources = self.session_resources(&sid, resource_limit)?;
                let events = if include_events {
                    self.session_events(&sid, event_limit)?
                } else {
                    Vec::new()
                };
                Ok(SessionDetail {
                    session: Some(s),
                    resources,
                    events_included: include_events,
                    events,
                })
            }
        }
    }
}

/// Validated search (normalized, FTS-compiled).
struct ValidatedSearch {
    range: Option<(i64, i64)>,
    project: Option<String>,
    device: Option<String>,
    fts: Option<String>,
    limit: i64,
}

/// One grouped FTS/range match: session id plus the newest matching activity
/// observation (`MAX(observed_at_ms)`).
struct MatchedSession {
    session_id: String,
    matched_at_ms: i64,
}

/// Build the single-traversal matched-session query for FTS and/or range
/// searches: one `activity ⨝ activity_fts` scan (exactly one `MATCH` when
/// FTS text is present), event-level range restriction, session join for
/// project/device/time predicates, grouped by session. Ordered newest
/// matching observation first (then session end/start/id), bounded.
///
/// Callers fetch full session rows afterwards by id; the FTS index is
/// traversed exactly once here (resource keys use one more batched
/// traversal, never per-session MATCH loops for discovery).
fn build_matched_session_query(v: &ValidatedSearch) -> (String, Vec<rusqlite::types::Value>) {
    let mut sql = String::from(
        "SELECT a.session_id AS sid, MAX(a.observed_at_ms) AS m, MAX(s.end_ms) AS se, MAX(s.start_ms) AS ss FROM activity a",
    );
    let mut args: Vec<rusqlite::types::Value> = Vec::new();
    if v.fts.is_some() {
        sql.push_str(" JOIN activity_fts ON activity_fts.rowid = a.id");
    }
    sql.push_str(" JOIN sessions s ON s.session_id = a.session_id WHERE 1=1");
    if let Some(expr) = v.fts.as_deref() {
        sql.push_str(" AND activity_fts MATCH ?");
        args.push(rusqlite::types::Value::Text(expr.to_string()));
    }
    if let Some((s, e)) = v.range {
        sql.push_str(" AND a.observed_at_ms >= ? AND a.observed_at_ms < ?");
        args.push(rusqlite::types::Value::Integer(s));
        args.push(rusqlite::types::Value::Integer(e));
    }
    if let Some(p) = v.project.as_deref() {
        sql.push_str(" AND s.project_id = ?");
        args.push(rusqlite::types::Value::Text(p.to_string()));
    }
    if let Some(d) = v.device.as_deref() {
        sql.push_str(" AND s.device_id = ?");
        args.push(rusqlite::types::Value::Text(d.to_string()));
    }
    if v.range.is_some() {
        // Session-overlap prefilter for index use (implied by the
        // event-level range above, so no result change).
        sql.push_str(" AND s.end_ms >= ? AND s.start_ms < ?");
        // Re-push the same range bounds (separate placeholders).
        let (s, e) = v.range.unwrap();
        args.push(rusqlite::types::Value::Integer(s));
        args.push(rusqlite::types::Value::Integer(e));
    }
    sql.push_str(" GROUP BY sid ORDER BY m DESC, se DESC, ss DESC, sid DESC LIMIT ?");
    args.push(rusqlite::types::Value::Integer(v.limit));
    (sql, args)
}

fn existing_tables(conn: &Connection) -> Result<Vec<String>, StoreError> {
    // NOTE: `GLOB 'sqlite_*'` so `_` is literal (`LIKE 'sqlite_%'` would let
    // `sqlitex_…` evade as a false "internal").
    let mut stmt = conn.prepare(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'",
    )?;
    let names = stmt.query_map([], |r| r.get(0))?;
    let mut out = Vec::new();
    for n in names {
        out.push(n?);
    }
    Ok(out)
}

/// One `PRAGMA table_info` row (structural, not SQL-text matching).
struct ColInfo {
    name: String,
    coltype: String,
    notnull: i64,
    dflt: Option<String>,
    pk: i64,
}

fn pragma_table_info(conn: &Connection, table: &str) -> Result<Vec<ColInfo>, StoreError> {
    // Table name is caller-controlled (fixed allowlist), so format is safe.
    let sql = format!("PRAGMA table_info({table})");
    let mut stmt = conn.prepare(&sql)?;
    let mut rows = stmt.query([])?;
    let mut out = Vec::new();
    while let Some(row) = rows.next()? {
        let name: String = row.get(1)?;
        let coltype: String = row.get(2).unwrap_or_default();
        let notnull: i64 = row.get(3).unwrap_or(0);
        let dflt: Option<String> = row.get(4)?;
        let pk: i64 = row.get(5).unwrap_or(0);
        out.push(ColInfo {
            name,
            coltype,
            notnull,
            dflt,
            pk,
        });
    }
    Ok(out)
}

fn coltype_is(col: &ColInfo, want: &str) -> bool {
    col.coltype.eq_ignore_ascii_case(want)
}

/// Strict `schema_version` shape: exactly one `version INTEGER PRIMARY KEY`
/// column, no default. `notnull` is ignored (SQLite reports 0 for
/// `INTEGER PRIMARY KEY` unless explicitly `NOT NULL`).
fn validate_schema_version_shape(cols: &[ColInfo]) -> Result<(), StoreError> {
    if cols.len() != 1 {
        return Err(StoreError::IncompatibleSchema(format!(
            "schema_version has {} columns; expected 1 (version)",
            cols.len()
        )));
    }
    let c = &cols[0];
    if c.name != "version" || !coltype_is(c, "INTEGER") || c.pk != 1 || c.dflt.is_some() {
        return Err(StoreError::IncompatibleSchema(format!(
            "bad schema_version shape: expected version INTEGER PRIMARY KEY, got {} {} pk={} default={:?}",
            c.name, c.coltype, c.pk, c.dflt
        )));
    }
    Ok(())
}

/// Strict v3 `activity` shape check (structural, the only accepted shape):
/// exactly the 9 documented columns — `id`, `observed_at_ms`, `kind`,
/// `source`, `snapshot_json`, nullable `project_id`, nullable `event_id`,
/// `device_id`, `session_id` TEXT (no defaults) at the end. Detection is
/// structural via `PRAGMA table_info`. Any other column count or layout
/// (including pre-v3 v1/v2 shapes) rejects as [`StoreError::IncompatibleSchema`].
fn validate_activity_shape(cols: &[ColInfo]) -> Result<(), StoreError> {
    let bad = |msg: String| StoreError::IncompatibleSchema(format!("bad activity shape: {msg}"));
    let expect = |c: &ColInfo, name: &str, typ: &str, notnull: i64, pk: i64, what: &str| {
        if c.name != name
            || !coltype_is(c, typ)
            || c.notnull != notnull
            || c.pk != pk
            || c.dflt.is_some()
        {
            return Err(bad(format!(
                "{what}: expected {name} {typ} notnull={notnull} pk={pk} default=NULL, got {} {} notnull={} pk={} default={:?}",
                c.name, c.coltype, c.notnull, c.pk, c.dflt
            )));
        }
        Ok(())
    };
    match cols.len() {
        5 => {
            expect(&cols[0], "id", "INTEGER", 0, 1, "id")?;
            expect(
                &cols[1],
                "observed_at_ms",
                "INTEGER",
                1,
                0,
                "observed_at_ms",
            )?;
            expect(&cols[2], "kind", "TEXT", 1, 0, "kind")?;
            expect(&cols[3], "source", "TEXT", 1, 0, "source")?;
            expect(&cols[4], "snapshot_json", "TEXT", 1, 0, "snapshot_json")?;
            return Err(bad(format!(
                "expected 9 (v3) columns, got {} (pre-v3 v1 shape rejected)",
                cols.len()
            )));
        }
        6 => {
            return Err(bad(format!(
                "expected 9 (v3) columns, got 6 (pre-v3 v2 shape rejected)"
            )));
        }
        9 => {
            expect(&cols[0], "id", "INTEGER", 0, 1, "id")?;
            expect(
                &cols[1],
                "observed_at_ms",
                "INTEGER",
                1,
                0,
                "observed_at_ms",
            )?;
            expect(&cols[2], "kind", "TEXT", 1, 0, "kind")?;
            expect(&cols[3], "source", "TEXT", 1, 0, "source")?;
            expect(&cols[4], "snapshot_json", "TEXT", 1, 0, "snapshot_json")?;
            expect(&cols[5], "project_id", "TEXT", 0, 0, "project_id")?;
            expect(&cols[6], "event_id", "TEXT", 0, 0, "event_id")?;
            expect(&cols[7], "device_id", "TEXT", 0, 0, "device_id")?;
            expect(&cols[8], "session_id", "TEXT", 0, 0, "session_id")?;
            Ok(())
        }
        n => Err(bad(format!("expected 9 (v3) columns, got {n}"))),
    }
}

/// Strict `sessions` shape (15 columns, see module docs). `notnull` ignored
/// for the TEXT PRIMARY KEY column.
fn validate_sessions_shape(cols: &[ColInfo]) -> Result<(), StoreError> {
    let bad = |msg: String| StoreError::IncompatibleSchema(format!("bad sessions shape: {msg}"));
    if cols.len() != 15 {
        return Err(bad(format!("expected 15 columns, got {}", cols.len())));
    }
    let expect = |c: &ColInfo, name: &str, typ: &str, notnull: i64, pk: i64| {
        if c.name != name || !coltype_is(c, typ) || c.pk != pk || c.dflt.is_some() {
            return Err(bad(format!(
                "{name}: expected {typ} pk={pk} default=NULL, got {} {} notnull={} pk={} default={:?}",
                c.name, c.coltype, c.notnull, c.pk, c.dflt
            )));
        }
        if pk == 0 && c.notnull != notnull {
            return Err(bad(format!(
                "{name}: expected notnull={notnull}, got {}",
                c.notnull
            )));
        }
        Ok(())
    };
    // PK nullability ignored (SQLite reports 0 for TEXT PRIMARY KEY).
    if cols[0].name != "session_id"
        || !coltype_is(&cols[0], "TEXT")
        || cols[0].pk != 1
        || cols[0].dflt.is_some()
    {
        return Err(bad(format!(
            "session_id: expected TEXT PRIMARY KEY, got {} {} pk={} default={:?}",
            cols[0].name, cols[0].coltype, cols[0].pk, cols[0].dflt
        )));
    }
    expect(&cols[1], "project_id", "TEXT", 0, 0)?;
    expect(&cols[2], "project_name", "TEXT", 0, 0)?;
    expect(&cols[3], "start_ms", "INTEGER", 1, 0)?;
    expect(&cols[4], "end_ms", "INTEGER", 1, 0)?;
    expect(&cols[5], "first_activity_id", "INTEGER", 1, 0)?;
    expect(&cols[6], "last_activity_id", "INTEGER", 1, 0)?;
    expect(&cols[7], "event_count", "INTEGER", 1, 0)?;
    expect(&cols[8], "status", "TEXT", 1, 0)?;
    expect(&cols[9], "ended_reason", "TEXT", 0, 0)?;
    expect(&cols[10], "gap_ms", "INTEGER", 1, 0)?;
    expect(&cols[11], "interruption_ms", "INTEGER", 1, 0)?;
    expect(&cols[12], "device_id", "TEXT", 1, 0)?;
    expect(&cols[13], "unresolved_start_ms", "INTEGER", 0, 0)?;
    expect(&cols[14], "applications_json", "TEXT", 1, 0)?;
    Ok(())
}

/// Strict `session_resources` shape (11 columns, composite PK).
fn validate_session_resources_shape(cols: &[ColInfo]) -> Result<(), StoreError> {
    let bad =
        |msg: String| StoreError::IncompatibleSchema(format!("bad session_resources shape: {msg}"));
    if cols.len() != 11 {
        return Err(bad(format!("expected 11 columns, got {}", cols.len())));
    }
    let expect = |c: &ColInfo, name: &str, typ: &str, notnull: i64, pk: i64| {
        if c.name != name || !coltype_is(c, typ) || c.pk != pk || c.dflt.is_some() {
            return Err(bad(format!(
                "{name}: expected {typ} pk={pk} default=NULL, got {} {} notnull={} pk={} default={:?}",
                c.name, c.coltype, c.notnull, c.pk, c.dflt
            )));
        }
        if pk == 0 && c.notnull != notnull {
            return Err(bad(format!(
                "{name}: expected notnull={notnull}, got {}",
                c.notnull
            )));
        }
        Ok(())
    };
    // Composite PK nullability ignored for the two key columns.
    for (i, (name, typ, pk)) in [("session_id", "TEXT", 1), ("resource_key", "TEXT", 2)]
        .iter()
        .enumerate()
    {
        let c = &cols[i];
        if c.name != *name || !coltype_is(c, typ) || c.pk != *pk || c.dflt.is_some() {
            return Err(bad(format!(
                "{name}: expected {typ} pk={pk}, got {} {} pk={} default={:?}",
                c.name, c.coltype, c.pk, c.dflt
            )));
        }
    }
    expect(&cols[2], "kind", "TEXT", 1, 0)?;
    expect(&cols[3], "portable_identity", "TEXT", 0, 0)?;
    expect(&cols[4], "local_identity", "TEXT", 0, 0)?;
    expect(&cols[5], "latest_resource_json", "TEXT", 1, 0)?;
    expect(&cols[6], "occurrence_count", "INTEGER", 1, 0)?;
    expect(&cols[7], "first_seen_ms", "INTEGER", 1, 0)?;
    expect(&cols[8], "last_seen_ms", "INTEGER", 1, 0)?;
    expect(&cols[9], "first_activity_id", "INTEGER", 1, 0)?;
    expect(&cols[10], "last_activity_id", "INTEGER", 1, 0)?;
    Ok(())
}

/// Strict `device_info` shape (singleton TEXT PRIMARY KEY).
fn validate_device_info_shape(cols: &[ColInfo]) -> Result<(), StoreError> {
    if cols.len() != 1 {
        return Err(StoreError::IncompatibleSchema(format!(
            "device_info has {} columns; expected 1 (device_id)",
            cols.len()
        )));
    }
    let c = &cols[0];
    if c.name != "device_id" || !coltype_is(c, "TEXT") || c.pk != 1 || c.dflt.is_some() {
        return Err(StoreError::IncompatibleSchema(format!(
            "bad device_info shape: expected device_id TEXT PRIMARY KEY, got {} {} pk={} default={:?}",
            c.name, c.coltype, c.pk, c.dflt
        )));
    }
    Ok(())
}

/// Columns of an index in order via `PRAGMA index_info` (structural).
fn pragma_index_columns(conn: &Connection, index: &str) -> Result<Vec<String>, StoreError> {
    // Index name comes from our allowlist or sqlite_master; format is safe.
    let sql = format!("PRAGMA index_info({index})");
    let mut stmt = conn.prepare(&sql)?;
    let mut rows = stmt.query([])?;
    let mut ordered: Vec<(i64, String)> = Vec::new();
    while let Some(row) = rows.next()? {
        let seqno: i64 = row.get(0)?;
        let name: String = row.get(2)?;
        ordered.push((seqno, name));
    }
    ordered.sort_by_key(|(seq, _)| *seq);
    Ok(ordered.into_iter().map(|(_, n)| n).collect())
}

/// Expected non-auto indexes: name -> (owning table, required uniqueness,
/// ordered key columns).
const EXPECTED_INDEXES: &[(&str, &str, bool, &[&str])] = &[
    (
        "idx_activity_time",
        "activity",
        false,
        &["observed_at_ms", "id"],
    ),
    (
        "idx_activity_project",
        "activity",
        false,
        &["project_id", "observed_at_ms", "id"],
    ),
    ("idx_activity_event_id", "activity", true, &["event_id"]),
    (
        "idx_activity_session",
        "activity",
        false,
        &["session_id", "observed_at_ms", "id"],
    ),
    (
        "idx_sessions_time",
        "sessions",
        false,
        &["end_ms", "start_ms", "session_id"],
    ),
    (
        "idx_sessions_project",
        "sessions",
        false,
        &["project_id", "end_ms", "start_ms", "session_id"],
    ),
    (
        "idx_sessions_device_time",
        "sessions",
        false,
        &["device_id", "end_ms", "start_ms", "session_id"],
    ),
    (
        "idx_session_resources_seen",
        "session_resources",
        false,
        &["session_id", "last_seen_ms", "resource_key"],
    ),
];

/// Expected PK autoindexes: (owning table, name, ordered key columns).
/// Includes the two FTS5 shadow PK autoindexes (`activity_fts_idx` and
/// `activity_fts_config` each carry a `sqlite_autoindex_*_1` PK).
const EXPECTED_PK_AUTOINDEXES: &[(&str, &str, &[&str])] = &[
    ("sessions", "sqlite_autoindex_sessions_1", &["session_id"]),
    (
        "session_resources",
        "sqlite_autoindex_session_resources_1",
        &["session_id", "resource_key"],
    ),
    (
        "device_info",
        "sqlite_autoindex_device_info_1",
        &["device_id"],
    ),
    (
        "activity_fts_idx",
        "sqlite_autoindex_activity_fts_idx_1",
        &["segid", "term"],
    ),
    (
        "activity_fts_config",
        "sqlite_autoindex_activity_fts_config_1",
        &["k"],
    ),
];

/// Expected FTS declared columns in order.
const EXPECTED_FTS_COLUMNS: &[&str] = &[
    "session_id",
    "resource_key",
    "project",
    "application",
    "window_title",
    "workspace",
    "resource",
    "metadata",
];

/// Check `ASC`/`BINARY` on every key column of `index` via `index_xinfo`.
fn check_index_sort_and_collation(conn: &Connection, index: &str) -> Result<(), StoreError> {
    for kc in pragma_index_key_columns(conn, index)? {
        if kc.desc != 0 {
            return Err(StoreError::IncompatibleSchema(format!(
                "wrong index {index} sort order on {} (DESC)",
                kc.name
            )));
        }
        if !kc.coll.eq_ignore_ascii_case("BINARY") {
            return Err(StoreError::IncompatibleSchema(format!(
                "wrong index {index} collation on {} ({})",
                kc.name, kc.coll
            )));
        }
    }
    Ok(())
}

/// Object whitelist + index-shape policy (read-only checks, no writes).
///
/// SQLite-owned names are matched with `GLOB 'sqlite_*'` (literal prefix),
/// never `LIKE 'sqlite_%'` (where `_` is a wildcard that `sqlitex_…`
/// objects could exploit to evade validation).
///
/// - Tables (excluding `sqlite_*` internals) must be a subset of
///   `{schema_version, activity, sessions, session_resources, device_info}`;
///   any other table rejects. Triggers/views reject in any count.
/// - Named indexes (excluding `sqlite_autoindex_*` internals) must be a
///   subset of the eight documented names AND live on their documented
///   owning table; any other index rejects.
/// - Full `PRAGMA index_list` (including autoindexes) must contain only:
///   (a) `origin='c'`, non-partial indexes whose uniqueness matches the
///   documented requirement (the event index MUST be unique; all others
///   MUST be non-unique), (b) the exact PK autoindexes
///   (`origin='pk'`, unique, non-partial, exact table/name/columns,
///   `ASC`/`BINARY`). Anything else (UNIQUE columns, partial/DESC/NOCASE,
///   wrong-table, unexpected autoindexes) rejects.
/// - Present allowlisted indexes must have the exact documented column order
///   (`index_info`); missing non-unique indexes are OK (init/completion
///   recreates them). The UNIQUE `idx_activity_event_id` is never
///   recreated: established v3 requires it via
///   [`ActivityStore::require_event_id_index`].
/// - Table SQL is scanned for unsupported constraint tokens (`CHECK`,
///   `UNIQUE`, `FOREIGN KEY`, `REFERENCES`); our DDL contains none (unique
///   event identities use an explicit `CREATE UNIQUE INDEX`, PKs use
///   `PRIMARY KEY` without the `UNIQUE` word).
///
/// This is the v3 validator (no FTS): v3 databases must not contain
/// `activity_fts` or its shadows. Missing non-unique indexes (including the
/// new device+time index) are tolerated for migration completion.
fn validate_objects_and_indexes(conn: &Connection) -> Result<(), StoreError> {
    const TABLES: &[&str] = &[
        "schema_version",
        "activity",
        "sessions",
        "session_resources",
        "device_info",
    ];
    let mut stmt =
        conn.prepare("SELECT type, name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'")?;
    let mut rows = stmt.query([])?;
    let mut extra: Vec<String> = Vec::new();
    let mut present_indexes: Vec<String> = Vec::new();
    while let Some(row) = rows.next()? {
        let typ: String = row.get(0)?;
        let name: String = row.get(1)?;
        match typ.as_str() {
            "table" if TABLES.contains(&name.as_str()) => {}
            "index"
                if EXPECTED_INDEXES
                    .iter()
                    .any(|(n, _, _, _)| *n == name.as_str()) =>
            {
                present_indexes.push(name);
            }
            "trigger" | "view" => {
                extra.push(format!("{typ}:{name}"));
            }
            _ => {
                extra.push(format!("{typ}:{name}"));
            }
        }
    }
    if !extra.is_empty() {
        let names: Vec<&str> = EXPECTED_INDEXES.iter().map(|(n, _, _, _)| *n).collect();
        return Err(StoreError::IncompatibleSchema(format!(
            "foreign database objects {extra:?}; expected only {TABLES:?} + {names:?}"
        )));
    }
    for tbl in [
        "activity",
        "schema_version",
        "sessions",
        "session_resources",
        "device_info",
    ] {
        for e in pragma_index_list_full(conn, tbl)? {
            if e.name.starts_with("sqlite_autoindex") {
                let Some((_, _, cols)) = EXPECTED_PK_AUTOINDEXES
                    .iter()
                    .find(|(t, n, _)| *t == tbl && *n == e.name)
                else {
                    return Err(StoreError::IncompatibleSchema(format!(
                        "unsupported constraint-created index {} on {tbl} (UNIQUE constraint)",
                        e.name
                    )));
                };
                if e.unique == 0 || e.partial != 0 || e.origin != "pk" {
                    return Err(StoreError::IncompatibleSchema(format!(
                        "bad PK autoindex {} on {tbl} (unique={} partial={} origin={})",
                        e.name, e.unique, e.partial, e.origin
                    )));
                }
                let got = pragma_index_columns(conn, &e.name)?;
                let want: Vec<String> = cols.iter().map(|c| c.to_string()).collect();
                if got != want {
                    return Err(StoreError::IncompatibleSchema(format!(
                        "wrong PK autoindex {} columns {got:?}; expected {want:?}",
                        e.name
                    )));
                }
                check_index_sort_and_collation(conn, &e.name)?;
                continue;
            }
            let Some((_, owner, unique_required, _)) =
                EXPECTED_INDEXES.iter().find(|(n, _, _, _)| *n == e.name)
            else {
                return Err(StoreError::IncompatibleSchema(format!(
                    "unsupported index {} on {tbl}",
                    e.name
                )));
            };
            if *owner != tbl {
                return Err(StoreError::IncompatibleSchema(format!(
                    "index {} lives on {tbl}; expected table {owner}",
                    e.name
                )));
            }
            if (e.unique != 0) != *unique_required {
                return Err(StoreError::IncompatibleSchema(format!(
                    "wrong uniqueness for index {} on {tbl} (unique={}, required {})",
                    e.name, e.unique, unique_required
                )));
            }
            if e.partial != 0 || e.origin != "c" {
                return Err(StoreError::IncompatibleSchema(format!(
                    "bad index {} on {tbl} (partial={} origin={})",
                    e.name, e.partial, e.origin
                )));
            }
        }
    }
    for tbl in TABLES {
        if let Some(sql) = table_sql(conn, tbl)? {
            if sql_has_forbidden_constraint(&sql) {
                return Err(StoreError::IncompatibleSchema(format!(
                    "unsupported table constraint in {tbl} DDL"
                )));
            }
        }
    }
    for idx in present_indexes {
        let cols = pragma_index_columns(conn, &idx)?;
        let (_, _, _, want) = EXPECTED_INDEXES
            .iter()
            .find(|(n, _, _, _)| *n == idx.as_str())
            .expect("present index is allowlisted");
        let want: Vec<String> = want.iter().map(|c| c.to_string()).collect();
        if cols != want {
            return Err(StoreError::IncompatibleSchema(format!(
                "wrong index {idx} columns {cols:?}; expected documented order {want:?}"
            )));
        }
        // Sort/collation must match the documented ordering: ASC + BINARY on
        // every key column. A DESC or NOCASE index with the right names would
        // otherwise be accepted by column order alone.
        check_index_sort_and_collation(conn, &idx)?;
    }
    Ok(())
}

/// v3 object validator alias (strict v3: no FTS objects).
fn validate_objects_and_indexes_v3(conn: &Connection) -> Result<(), StoreError> {
    validate_objects_and_indexes(conn)
}

/// Empty-DB object check (fresh files): no non-`sqlite_*` objects at all.
fn validate_objects_and_indexes_empty(conn: &Connection) -> Result<(), StoreError> {
    let n: i64 = conn.query_row(
        "SELECT COUNT(*) FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'",
        [],
        |r| r.get(0),
    )?;
    if n != 0 {
        return Err(StoreError::IncompatibleSchema(format!(
            "foreign database objects present in supposedly-empty DB ({n} objects)"
        )));
    }
    Ok(())
}

/// Strict FTS shape: exact declared columns in order via `PRAGMA table_info`
/// plus an exact parse of the `USING fts5(...)` declaration: precisely which
/// columns are `UNINDEXED`, the exact `unicode61 remove_diacritics 2`
/// tokenizer, and no other options. Only harmless whitespace/case differences
/// are normalized; anything else rejects. Shadow tables are validated by
/// exact expected `CREATE TABLE` SQL (whitespace-normalized), so altered
/// shadows reject.
fn validate_fts_shape(conn: &Connection) -> Result<(), StoreError> {
    let cols = pragma_table_info(conn, "activity_fts")?;
    if cols.len() != EXPECTED_FTS_COLUMNS.len() {
        return Err(StoreError::IncompatibleSchema(format!(
            "bad activity_fts shape: expected {} columns, got {}",
            EXPECTED_FTS_COLUMNS.len(),
            cols.len()
        )));
    }
    for (i, want) in EXPECTED_FTS_COLUMNS.iter().enumerate() {
        let c = &cols[i];
        if c.name != *want {
            return Err(StoreError::IncompatibleSchema(format!(
                "bad activity_fts column {i}: expected {want}, got {}",
                c.name
            )));
        }
        // FTS columns carry no type/notnull/pk/default.
        if !c.coltype.is_empty() || c.notnull != 0 || c.pk != 0 || c.dflt.is_some() {
            return Err(StoreError::IncompatibleSchema(format!(
                "bad activity_fts column {want}: expected typeless nullable non-pk, got {} notnull={} pk={} default={:?}",
                c.coltype, c.notnull, c.pk, c.dflt
            )));
        }
    }
    let sql = table_sql(conn, "activity_fts")?
        .ok_or_else(|| StoreError::IncompatibleSchema("missing activity_fts DDL".to_string()))?;
    parse_fts5_declaration(&sql)?;
    // Shadows: exact expected CREATE TABLE SQL (whitespace-normalized), so
    // any altered shadow (extra column, changed key, dropped WITHOUT ROWID…)
    // rejects instead of being silently accepted.
    const EXPECTED_SHADOW_SQL: &[(&str, &str)] = &[
        (
            "activity_fts_data",
            "CREATE TABLE 'activity_fts_data'(id INTEGER PRIMARY KEY, block BLOB)",
        ),
        (
            "activity_fts_idx",
            "CREATE TABLE 'activity_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID",
        ),
        (
            "activity_fts_content",
            "CREATE TABLE 'activity_fts_content'(id INTEGER PRIMARY KEY, c0, c1, c2, c3, c4, c5, c6, c7)",
        ),
        (
            "activity_fts_docsize",
            "CREATE TABLE 'activity_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB)",
        ),
        (
            "activity_fts_config",
            "CREATE TABLE 'activity_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID",
        ),
    ];
    for (shadow, want_sql) in EXPECTED_SHADOW_SQL {
        let got = table_sql(conn, shadow)?.ok_or_else(|| {
            StoreError::IncompatibleSchema(format!("missing FTS shadow table {shadow}"))
        })?;
        if normalize_ws(&got) != normalize_ws(want_sql) {
            return Err(StoreError::IncompatibleSchema(format!(
                "bad FTS shadow table {shadow} DDL: got {got:?}"
            )));
        }
    }
    Ok(())
}

/// Collapse all whitespace runs to single spaces (for harmless-spacing
/// normalization in DDL comparisons).
fn normalize_ws(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Parse and exactly validate the `USING fts5(...)` declaration of the
/// `activity_fts` table SQL.
///
/// Accepted declaration (modulo whitespace/case only):
/// `(session_id UNINDEXED, resource_key UNINDEXED, project, application,
/// window_title, workspace, resource, metadata,
/// tokenize='unicode61 remove_diacritics 2')`.
/// Precisely the first two columns are `UNINDEXED`; the tokenizer value must
/// normalize to exactly `unicode61 remove_diacritics 2`; no other option
/// (`prefix`, `detail`, `content`, `columnsize`, `trigram`, …) is allowed.
fn parse_fts5_declaration(sql: &str) -> Result<(), StoreError> {
    let bad =
        |msg: String| StoreError::IncompatibleSchema(format!("bad activity_fts config: {msg}"));
    let lower = sql.to_lowercase();
    // Locate `USING fts5` tolerating arbitrary whitespace/case between the
    // keywords (`USING  FTS5(` is the same declaration).
    let using_pos = lower
        .find("using")
        .ok_or_else(|| bad("missing USING fts5".to_string()))?;
    let fts_rel = lower[using_pos..]
        .find("fts5")
        .ok_or_else(|| bad("missing USING fts5".to_string()))?;
    let fts_pos = using_pos + fts_rel;
    let after = &sql[fts_pos..];
    let open = after
        .find('(')
        .ok_or_else(|| bad("missing fts5 argument list".to_string()))?;
    // The declaration is the final parenthesized group; take the last ')'.
    let close = after
        .rfind(')')
        .ok_or_else(|| bad("unterminated fts5 argument list".to_string()))?;
    if close <= open {
        return Err(bad("unterminated fts5 argument list".to_string()));
    }
    let inner = &after[open + 1..close];
    let parts = split_top_level_commas(inner);
    // Exactly 8 column definitions + tokenize.
    if parts.len() != EXPECTED_FTS_COLUMNS.len() + 1 {
        return Err(bad(format!(
            "expected {} fts5 arguments (8 columns + tokenize), got {}",
            EXPECTED_FTS_COLUMNS.len() + 1,
            parts.len()
        )));
    }
    for (i, want) in EXPECTED_FTS_COLUMNS.iter().enumerate() {
        let tokens: Vec<String> = parts[i]
            .split_whitespace()
            .map(|t| {
                t.trim_matches(|c| c == '"' || c == '\'' || c == '`' || c == '[' || c == ']')
                    .to_string()
            })
            .filter(|t| !t.is_empty())
            .collect();
        if tokens.is_empty() || !tokens[0].eq_ignore_ascii_case(want) {
            return Err(bad(format!(
                "column {i}: expected {want}, got {:?}",
                parts[i].trim()
            )));
        }
        let unindexed = tokens[1..]
            .iter()
            .any(|t| t.eq_ignore_ascii_case("unindexed"));
        if tokens.len() > 2 || (tokens.len() == 2 && !unindexed) {
            return Err(bad(format!(
                "column {want}: unexpected definition {:?} (only UNINDEXED allowed)",
                parts[i].trim()
            )));
        }
        let want_unindexed = i < 2;
        if unindexed != want_unindexed {
            return Err(bad(format!(
                "column {want}: UNINDEXED {}",
                if want_unindexed {
                    "required"
                } else {
                    "forbidden"
                }
            )));
        }
    }
    // Final argument must be exactly the tokenizer.
    let tok = parts[EXPECTED_FTS_COLUMNS.len()].trim();
    let tok_lower = tok.to_lowercase();
    let eq = tok_lower
        .find('=')
        .ok_or_else(|| bad(format!("unexpected fts5 option {tok:?}")))?;
    let (name, value) = tok.split_at(eq);
    if name.trim().to_lowercase() != "tokenize" {
        return Err(bad(format!(
            "unexpected fts5 option {tok:?} (only tokenize allowed)"
        )));
    }
    let value = value[1..].trim().trim_matches('\'').trim_matches('"');
    if normalize_ws(&value.to_lowercase()) != "unicode61 remove_diacritics 2" {
        return Err(bad(format!(
            "tokenizer must be exactly unicode61 remove_diacritics 2, got {value:?}"
        )));
    }
    Ok(())
}

/// Split on top-level commas (commas inside single-quoted strings, e.g. the
/// tokenizer value, do not split).
fn split_top_level_commas(s: &str) -> Vec<String> {
    let mut parts = Vec::new();
    let mut cur = String::new();
    let mut in_single = false;
    let mut chars = s.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\'' {
            // `''` inside a quoted string is an escaped quote.
            if in_single && chars.peek() == Some(&'\'') {
                cur.push('\'');
                cur.push(chars.next().unwrap());
                continue;
            }
            in_single = !in_single;
            cur.push(c);
            continue;
        }
        if c == ',' && !in_single {
            parts.push(cur.trim().to_string());
            cur = String::new();
            continue;
        }
        cur.push(c);
    }
    parts.push(cur.trim().to_string());
    parts
}

/// v4 object whitelist + index-shape policy (read-only, no writes).
///
/// Base tables plus `activity_fts` and its five exact shadows; triggers/views
/// reject; named indexes must be a subset of the eight documented names with
/// exact ownership/uniqueness/columns/ASC/BINARY; PK autoindexes allowlisted
/// by exact table/name/columns (including the two FTS shadow PKs); missing
/// non-unique indexes OK (completion recreates); UNIQUE event index never
/// recreated.
fn validate_objects_and_indexes_v4(conn: &Connection) -> Result<(), StoreError> {
    const TABLES: &[&str] = &[
        "schema_version",
        "activity",
        "sessions",
        "session_resources",
        "device_info",
        "activity_fts",
        "activity_fts_data",
        "activity_fts_idx",
        "activity_fts_content",
        "activity_fts_docsize",
        "activity_fts_config",
    ];
    let mut stmt =
        conn.prepare("SELECT type, name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'")?;
    let mut rows = stmt.query([])?;
    let mut extra: Vec<String> = Vec::new();
    let mut present_indexes: Vec<String> = Vec::new();
    while let Some(row) = rows.next()? {
        let typ: String = row.get(0)?;
        let name: String = row.get(1)?;
        match typ.as_str() {
            "table" if TABLES.contains(&name.as_str()) => {}
            "index"
                if EXPECTED_INDEXES
                    .iter()
                    .any(|(n, _, _, _)| *n == name.as_str()) =>
            {
                present_indexes.push(name);
            }
            "trigger" | "view" => {
                extra.push(format!("{typ}:{name}"));
            }
            _ => {
                extra.push(format!("{typ}:{name}"));
            }
        }
    }
    if !extra.is_empty() {
        let names: Vec<&str> = EXPECTED_INDEXES.iter().map(|(n, _, _, _)| *n).collect();
        return Err(StoreError::IncompatibleSchema(format!(
            "foreign database objects {extra:?}; expected only {TABLES:?} + {names:?}"
        )));
    }
    for tbl in [
        "activity",
        "schema_version",
        "sessions",
        "session_resources",
        "device_info",
        "activity_fts",
        "activity_fts_data",
        "activity_fts_idx",
        "activity_fts_content",
        "activity_fts_docsize",
        "activity_fts_config",
    ] {
        for e in pragma_index_list_full(conn, tbl)? {
            if e.name.starts_with("sqlite_autoindex") {
                let Some((_, _, cols)) = EXPECTED_PK_AUTOINDEXES
                    .iter()
                    .find(|(t, n, _)| *t == tbl && *n == e.name)
                else {
                    return Err(StoreError::IncompatibleSchema(format!(
                        "unsupported constraint-created index {} on {tbl} (UNIQUE constraint)",
                        e.name
                    )));
                };
                if e.unique == 0 || e.partial != 0 || e.origin != "pk" {
                    return Err(StoreError::IncompatibleSchema(format!(
                        "bad PK autoindex {} on {tbl} (unique={} partial={} origin={})",
                        e.name, e.unique, e.partial, e.origin
                    )));
                }
                let got = pragma_index_columns(conn, &e.name)?;
                let want: Vec<String> = cols.iter().map(|c| c.to_string()).collect();
                if got != want {
                    return Err(StoreError::IncompatibleSchema(format!(
                        "wrong PK autoindex {} columns {got:?}; expected {want:?}",
                        e.name
                    )));
                }
                check_index_sort_and_collation(conn, &e.name)?;
                continue;
            }
            let Some((_, owner, unique_required, _)) =
                EXPECTED_INDEXES.iter().find(|(n, _, _, _)| *n == e.name)
            else {
                return Err(StoreError::IncompatibleSchema(format!(
                    "unsupported index {} on {tbl}",
                    e.name
                )));
            };
            if *owner != tbl {
                return Err(StoreError::IncompatibleSchema(format!(
                    "index {} lives on {tbl}; expected table {owner}",
                    e.name
                )));
            }
            if (e.unique != 0) != *unique_required {
                return Err(StoreError::IncompatibleSchema(format!(
                    "wrong uniqueness for index {} on {tbl} (unique={}, required {})",
                    e.name, e.unique, unique_required
                )));
            }
            if e.partial != 0 || e.origin != "c" {
                return Err(StoreError::IncompatibleSchema(format!(
                    "bad index {} on {tbl} (partial={} origin={})",
                    e.name, e.partial, e.origin
                )));
            }
        }
    }
    // FTS virtual table must have no normal indexes of its own.
    // (Covered above: any index on activity_fts not in EXPECTED_INDEXES
    // rejects; EXPECTED_INDEXES owns none on activity_fts, so any is extra.)
    for tbl in [
        "schema_version",
        "activity",
        "sessions",
        "session_resources",
        "device_info",
    ] {
        if let Some(sql) = table_sql(conn, tbl)? {
            if sql_has_forbidden_constraint(&sql) {
                return Err(StoreError::IncompatibleSchema(format!(
                    "unsupported table constraint in {tbl} DDL"
                )));
            }
        }
    }
    for idx in present_indexes {
        let cols = pragma_index_columns(conn, &idx)?;
        let (_, _, _, want) = EXPECTED_INDEXES
            .iter()
            .find(|(n, _, _, _)| *n == idx.as_str())
            .expect("present index is allowlisted");
        let want: Vec<String> = want.iter().map(|c| c.to_string()).collect();
        if cols != want {
            return Err(StoreError::IncompatibleSchema(format!(
                "wrong index {idx} columns {cols:?}; expected documented order {want:?}"
            )));
        }
        check_index_sort_and_collation(conn, &idx)?;
    }
    Ok(())
}

/// One full `PRAGMA index_list` row (includes autoindexes; caller decides).
struct IndexListEntry {
    name: String,
    unique: i64,
    origin: String,
    partial: i64,
}

/// Full `PRAGMA index_list(table)`: all indexes including `sqlite_%`
/// autoindexes (unlike the sqlite_master whitelist query). Missing tables
/// yield an empty list.
fn pragma_index_list_full(
    conn: &Connection,
    table: &str,
) -> Result<Vec<IndexListEntry>, StoreError> {
    let sql = format!("PRAGMA index_list({table})");
    let mut stmt = conn.prepare(&sql)?;
    let mut rows = stmt.query([])?;
    let mut out = Vec::new();
    while let Some(row) = rows.next()? {
        let name: String = row.get(1)?;
        let unique: i64 = row.get(2).unwrap_or(0);
        let origin: String = row.get(3).unwrap_or_default();
        let partial: i64 = row.get(4).unwrap_or(0);
        out.push(IndexListEntry {
            name,
            unique,
            origin,
            partial,
        });
    }
    Ok(out)
}

/// One `PRAGMA index_xinfo` key column.
struct IndexKeyColumn {
    name: String,
    desc: i64,
    coll: String,
}

/// Key columns (`key == 1`) of an index in order via `PRAGMA index_xinfo`.
fn pragma_index_key_columns(
    conn: &Connection,
    index: &str,
) -> Result<Vec<IndexKeyColumn>, StoreError> {
    let safe = index.replace('"', "");
    let sql = format!("PRAGMA index_xinfo(\"{safe}\")");
    let mut stmt = conn.prepare(&sql)?;
    let mut rows = stmt.query([])?;
    let mut ordered: Vec<(i64, IndexKeyColumn)> = Vec::new();
    while let Some(row) = rows.next()? {
        let seqno: i64 = row.get(0)?;
        let name: Option<String> = row.get(2)?;
        let desc: i64 = row.get(3).unwrap_or(0);
        let coll: String = row.get(4).unwrap_or_default();
        let key: i64 = row.get(5).unwrap_or(0);
        if key != 1 {
            continue;
        }
        let name = name.unwrap_or_default();
        ordered.push((seqno, IndexKeyColumn { name, desc, coll }));
    }
    ordered.sort_by_key(|(seq, _)| *seq);
    Ok(ordered.into_iter().map(|(_, kc)| kc).collect())
}

/// Raw `CREATE TABLE` SQL for constraint-token scanning.
fn table_sql(conn: &Connection, table: &str) -> Result<Option<String>, StoreError> {
    let mut stmt = conn.prepare("SELECT sql FROM sqlite_master WHERE type='table' AND name=?1")?;
    let mut rows = stmt.query(params![table])?;
    if let Some(row) = rows.next()? {
        let sql: Option<String> = row.get(0)?;
        Ok(sql)
    } else {
        Ok(None)
    }
}

/// True when stripped DDL contains an unsupported table-constraint keyword
/// (`CHECK`, `UNIQUE`, `FOREIGN KEY`, `REFERENCES`) as whole words.
/// Comments and quoted strings/literals are stripped first so identifiers in
/// comments or defaults cannot cause false rejections; our own DDL contains
/// none of these tokens.
fn sql_has_forbidden_constraint(sql: &str) -> bool {
    let stripped = strip_sql_comments_and_strings(sql);
    let upper = stripped.to_uppercase();
    let b = upper.as_bytes();
    let is_word = |c: u8| c.is_ascii_alphanumeric() || c == b'_';
    let has_word = |word: &[u8]| {
        if word.is_empty() || b.len() < word.len() {
            return false;
        }
        let mut i = 0;
        while i + word.len() <= b.len() {
            if &b[i..i + word.len()] == word {
                let before = if i == 0 { None } else { Some(b[i - 1]) };
                let after = b.get(i + word.len()).copied();
                let before_ok = before.map(|c| !is_word(c)).unwrap_or(true);
                let after_ok = after.map(|c| !is_word(c)).unwrap_or(true);
                if before_ok && after_ok {
                    return true;
                }
            }
            i += 1;
        }
        false
    };
    if has_word(b"CHECK") || has_word(b"UNIQUE") || has_word(b"REFERENCES") {
        return true;
    }
    // FOREIGN KEY is a two-word token.
    if has_word(b"FOREIGN") && has_word(b"KEY") {
        return true;
    }
    false
}

/// Strip `--` line comments, `/* */` block comments, and `'...'` / `"..."`
/// / `` `...` `` / `[...]` quoted segments (with SQLite escape rules) so
/// keyword scans see only real DDL tokens.
fn strip_sql_comments_and_strings(sql: &str) -> String {
    let b = sql.as_bytes();
    let mut out = String::with_capacity(sql.len());
    let mut i = 0;
    while i < b.len() {
        // Line comment.
        if b[i] == b'-' && i + 1 < b.len() && b[i + 1] == b'-' {
            i += 2;
            while i < b.len() && b[i] != b'\n' {
                i += 1;
            }
            out.push(' ');
            continue;
        }
        // Block comment.
        if b[i] == b'/' && i + 1 < b.len() && b[i + 1] == b'*' {
            i += 2;
            while i + 1 < b.len() && !(b[i] == b'*' && b[i + 1] == b'/') {
                i += 1;
            }
            i = (i + 2).min(b.len());
            out.push(' ');
            continue;
        }
        // Single-quoted string/literal ('' escape).
        if b[i] == b'\'' {
            i += 1;
            while i < b.len() {
                if b[i] == b'\'' {
                    if i + 1 < b.len() && b[i + 1] == b'\'' {
                        i += 2;
                    } else {
                        i += 1;
                        break;
                    }
                } else {
                    i += 1;
                }
            }
            out.push(' ');
            continue;
        }
        // Double-quoted identifier ("" escape).
        if b[i] == b'"' {
            i += 1;
            while i < b.len() {
                if b[i] == b'"' {
                    if i + 1 < b.len() && b[i + 1] == b'"' {
                        i += 2;
                    } else {
                        i += 1;
                        break;
                    }
                } else {
                    i += 1;
                }
            }
            out.push(' ');
            continue;
        }
        // Backtick identifier.
        if b[i] == b'`' {
            i += 1;
            while i < b.len() && b[i] != b'`' {
                i += 1;
            }
            i = (i + 1).min(b.len());
            out.push(' ');
            continue;
        }
        // Bracket identifier.
        if b[i] == b'[' {
            i += 1;
            while i < b.len() && b[i] != b']' {
                i += 1;
            }
            i = (i + 1).min(b.len());
            out.push(' ');
            continue;
        }
        out.push(b[i] as char);
        i += 1;
    }
    out
}

/// Priority location dedup key: `file` if present else `url` else `cwd`
/// else `page`. Branch/title/adapter/git_root/git_remote are correlation
/// metadata and never create distinct entries. Returns `None` when none of
/// file/url/cwd/page is present (caller skips such resources).
fn resource_location_key(r: &ResourceContext) -> Option<(char, String)> {
    if let Some(f) = r.file.as_deref().filter(|s| !s.is_empty()) {
        return Some(('f', f.to_string()));
    }
    if let Some(u) = r.url.as_deref().filter(|s| !s.is_empty()) {
        return Some(('u', u.to_string()));
    }
    if let Some(c) = r.cwd.as_deref().filter(|s| !s.is_empty()) {
        return Some(('c', c.to_string()));
    }
    if let Some(p) = r.page.as_deref().filter(|s| !s.is_empty()) {
        return Some(('p', p.to_string()));
    }
    None
}

fn map_parent_error(path: &Path, msg: &str) -> StoreError {
    if msg.contains("permission denied") {
        StoreError::PermissionDenied(format!("{}: {msg}", path.display()))
    } else if msg.contains("symlink") || msg.contains("not owned") || msg.contains("group/other") {
        StoreError::UnsafePath(format!("{}: {msg}", path.display()))
    } else {
        StoreError::Sqlite(format!("{}: {msg}", path.display()))
    }
}

fn map_sqlite_open_error(path: &Path, e: rusqlite::Error) -> StoreError {
    let s = e.to_string().to_lowercase();
    if s.contains("permission denied") || s.contains("readonly") && s.contains("permission") {
        StoreError::PermissionDenied(format!("cannot open {}: {e}", path.display()))
    } else if s.contains("unable to open") && s.contains("not found") || s.contains("no such file")
    {
        StoreError::NotFound(format!("no database at {}: {e}", path.display()))
    } else {
        StoreError::Sqlite(format!("cannot open {}: {e}", path.display()))
    }
}

fn row_to_record_v3(row: &rusqlite::Row<'_>) -> rusqlite::Result<ActivityRecord> {
    Ok(ActivityRecord {
        id: row.get(0)?,
        observed_at_ms: row.get(1)?,
        kind: row.get(2)?,
        source: row.get(3)?,
        snapshot_json: row.get(4)?,
        event_id: row.get(5)?,
        device_id: row.get(6)?,
        session_id: row.get(7)?,
    })
}

/// Validate a stable session/event/device 128-bit hex identity.
pub fn normalize_session_id(id: &str) -> Result<String, StoreError> {
    crate::desktop_session::normalize_hex_id(id, "session id").map_err(StoreError::InvalidArgument)
}

/// Inner session row (all columns) before public conversion.
#[derive(Clone, Debug)]
struct InnerSession {
    session_id: String,
    project_id: Option<String>,
    project_name: Option<String>,
    start_ms: i64,
    end_ms: i64,
    first_activity_id: i64,
    last_activity_id: i64,
    event_count: i64,
    status: String,
    ended_reason: Option<String>,
    gap_ms: i64,
    interruption_ms: i64,
    device_id: String,
    unresolved_start_ms: Option<i64>,
    applications_json: String,
    // Back-compat: some queries select a subset; missing fields default.
}

impl InnerSession {
    fn into_record(self) -> Result<SessionRecord, StoreError> {
        // Validate status/reason shapes (fail-closed on corrupt rows).
        if self.status != "open" && self.status != "closed" {
            return Err(StoreError::IncompatibleSchema(format!(
                "bad session status {:?}",
                self.status
            )));
        }
        if let Some(r) = self.ended_reason.as_deref() {
            if !matches!(r, "inactivity" | "project_switch" | "interruption") {
                return Err(StoreError::IncompatibleSchema(format!(
                    "bad session ended_reason {r:?}"
                )));
            }
        }
        if self.status == "open" && self.ended_reason.is_some() {
            return Err(StoreError::IncompatibleSchema(
                "open session must not carry ended_reason".to_string(),
            ));
        }
        validate_session_thresholds(self.gap_ms, self.interruption_ms)
            .map_err(|e| StoreError::IncompatibleSchema(format!("bad session thresholds: {e}")))?;
        let applications: Vec<String> =
            serde_json::from_str(&self.applications_json).unwrap_or_default();
        Ok(SessionRecord {
            session_id: self.session_id,
            device_id: self.device_id,
            project_id: self.project_id.filter(|s| !s.is_empty()),
            project_name: self.project_name.filter(|s| !s.is_empty()),
            start_ms: self.start_ms,
            end_ms: self.end_ms,
            first_activity_id: self.first_activity_id,
            last_activity_id: self.last_activity_id,
            event_count: self.event_count,
            status: self.status,
            ended_reason: self.ended_reason,
            unresolved_start_ms: self.unresolved_start_ms,
            gap_ms: self.gap_ms,
            interruption_ms: self.interruption_ms,
            applications,
        })
    }
}

fn row_to_inner_session(row: &rusqlite::Row<'_>) -> rusqlite::Result<InnerSession> {
    Ok(InnerSession {
        session_id: row.get(0)?,
        project_id: row.get(1)?,
        project_name: row.get(2)?,
        start_ms: row.get(3)?,
        end_ms: row.get(4)?,
        first_activity_id: row.get(5)?,
        last_activity_id: row.get(6)?,
        event_count: row.get(7)?,
        status: row.get(8)?,
        ended_reason: row.get(9)?,
        gap_ms: row.get(10)?,
        interruption_ms: row.get(11)?,
        device_id: row.get(12)?,
        unresolved_start_ms: row.get(13)?,
        applications_json: row.get(14)?,
    })
}

fn row_to_session_resource(row: &rusqlite::Row<'_>) -> rusqlite::Result<SessionResourceRecord> {
    let latest_json: String = row.get(5)?;
    let resource: ResourceContext = serde_json::from_str(&latest_json).unwrap_or_default();
    Ok(SessionResourceRecord {
        session_id: row.get(0)?,
        resource_key: row.get(1)?,
        kind: row.get(2)?,
        portable_identity: row.get(3)?,
        local_identity: row.get(4)?,
        resource,
        occurrence_count: row.get(6)?,
        first_seen_ms: row.get(7)?,
        last_seen_ms: row.get(8)?,
        first_activity_id: row.get(9)?,
        last_activity_id: row.get(10)?,
    })
}

/// Validate per-session thresholds (same bounds as [`SessionConfig`]).
fn validate_session_thresholds(gap_ms: i64, interruption_ms: i64) -> Result<(), StoreError> {
    SessionConfig::new(gap_ms, interruption_ms)
        .map(|_| ())
        .map_err(StoreError::InvalidArgument)
}

fn validate_tag(what: &str, v: &str) -> Result<(), StoreError> {
    if v.is_empty() || v.chars().count() > MAX_TAG_CHARS {
        return Err(StoreError::InvalidArgument(format!(
            "{what} must be 1..={MAX_TAG_CHARS} chars"
        )));
    }
    if v.trim() != v {
        return Err(StoreError::InvalidArgument(format!(
            "{what} must not have leading/trailing whitespace"
        )));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::desktop_context::{FocusedWindow, Source, Workspace};

    fn sample(ts: i64) -> DesktopContext {
        DesktopContext::available(
            Source::Hyprland,
            Some(FocusedWindow::new("0x1", "kitty", "shell")),
            Some(Workspace::new("1", "1")),
            ts,
        )
    }

    #[test]
    fn roundtrip_and_reopen() {
        let dir = std::env::temp_dir().join(format!(
            "qs-desktop-test-{}-{}",
            std::process::id(),
            now_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let db = dir.join("activity.db");
        {
            let store = ActivityStore::open(&db).unwrap();
            assert_eq!(store.schema_version().unwrap(), SCHEMA_VERSION);
            let ctx = sample(1000);
            let id = store.append("focus", "hyprland", &ctx).unwrap();
            assert!(id > 0);
            let recent = store.recent_activity(10).unwrap();
            assert_eq!(recent.len(), 1);
            assert_eq!(recent[0].snapshot().unwrap(), ctx);
        }
        // Reopen: rows survive; schema version persists.
        {
            let store = ActivityStore::open(&db).unwrap();
            assert_eq!(store.schema_version().unwrap(), SCHEMA_VERSION);
            let recent = store.recent_activity(10).unwrap();
            assert_eq!(recent.len(), 1);
            assert_eq!(recent[0].observed_at_ms, 1000);
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn range_inclusive_exclusive_and_ordering() {
        let store = ActivityStore::open_in_memory().unwrap();
        for ts in [1000, 2000, 3000] {
            store.append("focus", "hyprland", &sample(ts)).unwrap();
        }
        // start inclusive, end exclusive.
        let rows = store.activity_in_range(1000, 3000, 10).unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].observed_at_ms, 1000);
        assert_eq!(rows[1].observed_at_ms, 2000);
        // Empty range (start == end) is valid, returns nothing.
        assert!(store.activity_in_range(2000, 2000, 10).unwrap().is_empty());
        // Ascending deterministic order.
        let all = store.activity_in_range(0, 9999, 10).unwrap();
        assert_eq!(all.len(), 3);
        // Recent is descending.
        let recent = store.recent_activity(10).unwrap();
        assert_eq!(recent[0].observed_at_ms, 3000);
        assert_eq!(recent[2].observed_at_ms, 1000);
    }

    #[test]
    fn equal_timestamps_order_deterministically() {
        let store = ActivityStore::open_in_memory().unwrap();
        for _ in 0..3 {
            store.append("focus", "hyprland", &sample(5000)).unwrap();
        }
        let asc = store.activity_in_range(0, 9999, 10).unwrap();
        assert_eq!(asc.len(), 3);
        assert!(asc[0].id < asc[1].id && asc[1].id < asc[2].id);
        let desc = store.recent_activity(10).unwrap();
        assert!(desc[0].id > desc[1].id && desc[1].id > desc[2].id);
    }

    #[test]
    fn invalid_limits_and_ranges_rejected() {
        let store = ActivityStore::open_in_memory().unwrap();
        assert!(store.recent_activity(0).is_err());
        assert!(store.recent_activity(-1).is_err());
        assert!(store.recent_activity(MAX_QUERY_LIMIT + 1).is_err());
        assert!(store.activity_in_range(2000, 1000, 10).is_err());
        assert!(store.activity_in_range(-1, 100, 10).is_err());
        assert!(store.activity_in_range(0, -5, 10).is_err());
        assert!(store.activity_in_range(0, 100, 0).is_err());
    }

    #[test]
    fn append_validates_error_aware() {
        let store = ActivityStore::open_in_memory().unwrap();
        let mut bad = sample(-1);
        assert!(store.append("focus", "hyprland", &bad).is_err());
        bad = sample(1);
        assert!(store.append("", "hyprland", &bad).is_err());
        assert!(store.append("focus", "", &bad).is_err());
        // Nothing persisted on failure.
        assert!(store.recent_activity(10).unwrap().is_empty());
    }

    #[cfg(unix)]
    fn tmp_store_dir(tag: &str) -> std::path::PathBuf {
        let p = std::env::temp_dir().join(format!(
            "qs-store-{}-{}-{tag}",
            std::process::id(),
            now_nanos()
        ));
        std::fs::create_dir_all(&p).unwrap();
        p
    }

    #[cfg(unix)]
    #[test]
    fn db_file_created_0600() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tmp_store_dir("mode");
        let db = dir.join("activity.db");
        let _store = ActivityStore::open(&db).unwrap();
        let mode = std::fs::metadata(&db).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600, "db file must be 0600 from creation");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn symlink_db_rejected() {
        let dir = tmp_store_dir("symlink");
        let real = dir.join("real.db");
        let store = ActivityStore::open(&real).unwrap();
        store.append("focus", "hyprland", &sample(1)).unwrap();
        drop(store);
        let link = dir.join("link.db");
        std::os::unix::fs::symlink(&real, &link).unwrap();
        assert!(ActivityStore::open(&link).is_err());
        assert!(ActivityStore::open_read_only(&link).is_err());
        // Target untouched and still readable.
        let ro = ActivityStore::open_read_only(&real).unwrap();
        assert_eq!(ro.recent_activity(10).unwrap().len(), 1);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn read_only_does_not_mutate() {
        let dir = tmp_store_dir("readonly");
        let db = dir.join("activity.db");
        {
            let store = ActivityStore::open(&db).unwrap();
            store.append("focus", "hyprland", &sample(42)).unwrap();
        }
        // Checkpoint so the file is stable, then snapshot bytes.
        {
            let conn = rusqlite::Connection::open(&db).unwrap();
            let _ = conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);");
        }
        let before = std::fs::read(&db).unwrap();
        let ro = ActivityStore::open_read_only(&db).unwrap();
        assert_eq!(ro.recent_activity(10).unwrap().len(), 1);
        // History must not accept writes through the read-only handle.
        assert!(ro.append("focus", "hyprland", &sample(43)).is_err());
        let after = std::fs::read(&db).unwrap();
        assert_eq!(before, after, "read-only open must not mutate the database");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn foreign_db_rejected_without_modification() {
        let dir = tmp_store_dir("foreign");
        let db = dir.join("foreign.db");
        {
            let conn = rusqlite::Connection::open(&db).unwrap();
            conn.execute_batch(
                "CREATE TABLE other (id INTEGER PRIMARY KEY); INSERT INTO other VALUES (1);",
            )
            .unwrap();
        }
        // Tighten to private so rejection is for foreignness, not perms.
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&db, std::fs::Permissions::from_mode(0o600));
        }
        let before = std::fs::read(&db).unwrap();
        match ActivityStore::open(&db) {
            Err(StoreError::IncompatibleSchema(_)) => {}
            Err(e) => panic!("expected IncompatibleSchema, got {e:?}"),
            Ok(_) => panic!("expected IncompatibleSchema, opened foreign DB"),
        }
        match ActivityStore::open_read_only(&db) {
            Err(StoreError::IncompatibleSchema(_)) => {}
            Err(e) => panic!("expected IncompatibleSchema (ro), got {e:?}"),
            Ok(_) => panic!("expected IncompatibleSchema (ro), opened foreign DB"),
        }
        let after = std::fs::read(&db).unwrap();
        assert_eq!(before, after, "foreign DB must be unchanged");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn future_version_rejected_unchanged() {
        let dir = tmp_store_dir("future");
        let db = dir.join("activity.db");
        {
            let store = ActivityStore::open(&db).unwrap();
            store.append("focus", "hyprland", &sample(7)).unwrap();
        }
        {
            let conn = rusqlite::Connection::open(&db).unwrap();
            conn.execute("UPDATE schema_version SET version = 999", [])
                .unwrap();
        }
        let before = std::fs::read(&db).unwrap();
        match ActivityStore::open(&db) {
            Err(StoreError::IncompatibleSchema(_)) => {}
            Err(e) => panic!("expected IncompatibleSchema for v999, got {e:?}"),
            Ok(_) => panic!("expected IncompatibleSchema for v999, opened"),
        }
        match ActivityStore::open_read_only(&db) {
            Err(StoreError::IncompatibleSchema(_)) => {}
            Err(e) => panic!("expected IncompatibleSchema (ro) for v999, got {e:?}"),
            Ok(_) => panic!("expected IncompatibleSchema (ro) for v999, opened"),
        }
        let after = std::fs::read(&db).unwrap();
        assert_eq!(before, after, "future-version DB must be unchanged");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn read_only_missing_is_not_found() {
        let missing = std::env::temp_dir().join(format!(
            "qs-store-missing-{}-{}-nofile.db",
            std::process::id(),
            now_nanos()
        ));
        let _ = std::fs::remove_file(&missing);
        match ActivityStore::open_read_only(&missing) {
            Err(StoreError::NotFound(_)) => {}
            Err(e) => panic!("expected NotFound, got {e:?}"),
            Ok(_) => panic!("expected NotFound, opened missing DB"),
        }
    }

    #[test]
    fn multi_row_schema_version_rejected() {
        // v3 fresh DB holds singleton [3]; inserting a distinct version (1)
        // creates multi-row [3,1] which must be rejected (not silently
        // picked).
        let store = ActivityStore::open_in_memory().unwrap();
        store
            .conn
            .execute("INSERT INTO schema_version (version) VALUES (1)", [])
            .unwrap();
        assert!(store.schema_version().is_err());
    }

    fn now_nanos() -> u128 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    }

    #[test]
    fn immediate_tx_rolls_back_on_drop_and_commits_explicitly() {
        // The RAII guard is the unit under test: an uncommitted guard must
        // leave no trace, a committed one must persist.
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT);")
            .unwrap();
        {
            let _guard = ImmediateTx::begin(&conn).unwrap();
            conn.execute("INSERT INTO t (v) VALUES ('dropped')", [])
                .unwrap();
            // Drop without commit: rollback.
        }
        let n: i64 = conn
            .query_row("SELECT COUNT(*) FROM t", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n, 0, "dropped guard must roll back");
        {
            let guard = ImmediateTx::begin(&conn).unwrap();
            conn.execute("INSERT INTO t (v) VALUES ('kept')", [])
                .unwrap();
            guard.commit().unwrap();
        }
        let n: i64 = conn
            .query_row("SELECT COUNT(*) FROM t", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n, 1, "committed guard must persist");
        // A second BEGIN while a guard holds the transaction fails fast
        // (locked semantics surface as an error, nothing persisted).
        {
            let _outer = ImmediateTx::begin(&conn).unwrap();
            assert!(ImmediateTx::begin(&conn).is_err());
        }
    }

    #[test]
    fn fts_matched_session_plan_traverses_fts_once() {
        // Regression: FTS/range session discovery must traverse the FTS
        // matches once (single grouped scan, one MATCH), never a correlated
        // MATCH per candidate session. Structured no-FTS plans use no MATCH.
        let fts_q = ValidatedSearch {
            range: Some((0, 1000)),
            project: Some("11111111-1111-1111-1111-111111111111".to_string()),
            device: None,
            fts: Some("application : \"zen\"".to_string()),
            limit: 10,
        };
        let (sql, _) = build_matched_session_query(&fts_q);
        assert_eq!(
            sql.to_uppercase().matches("MATCH").count(),
            1,
            "FTS discovery must contain exactly one MATCH: {sql}"
        );
        let range_only = ValidatedSearch {
            range: Some((0, 1000)),
            project: None,
            device: None,
            fts: None,
            limit: 10,
        };
        let (sql, _) = build_matched_session_query(&range_only);
        assert_eq!(
            sql.to_uppercase().matches("MATCH").count(),
            0,
            "range-only discovery must contain no MATCH: {sql}"
        );
        // EXPLAIN QUERY PLAN against a real v4 schema: exactly one plan node
        // touches the FTS virtual table.
        let store = ActivityStore::open_in_memory().unwrap();
        let (sql, args) = build_matched_session_query(&fts_q);
        let explain = format!("EXPLAIN QUERY PLAN {sql}");
        let mut stmt = store.conn.prepare(&explain).unwrap();
        let pref: Vec<&dyn rusqlite::ToSql> =
            args.iter().map(|a| a as &dyn rusqlite::ToSql).collect();
        let mut rows = stmt.query(pref.as_slice()).unwrap();
        let mut fts_nodes = 0;
        while let Some(row) = rows.next().unwrap() {
            let detail: String = row.get(3).unwrap_or_default();
            if detail.contains("activity_fts") {
                fts_nodes += 1;
            }
        }
        assert_eq!(
            fts_nodes, 1,
            "query plan must scan activity_fts exactly once"
        );
    }
}
