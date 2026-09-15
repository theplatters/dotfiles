//! SQLite append-only activity history.
//!
//! Schema (versioned):
//! - `schema_version(version INTEGER PRIMARY KEY)` holding [`SCHEMA_VERSION`]
//!   as a singleton row (currently 2).
//! - v1: `activity(id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at_ms
//!   INTEGER, kind TEXT, source TEXT, snapshot_json TEXT)` with index
//!   `idx_activity_time(observed_at_ms, id)`.
//! - v2: same plus `project_id TEXT` (nullable, immutable historical
//!   association extracted from `snapshot_json $.project.id` at append
//!   time) with index `idx_activity_project(project_id, observed_at_ms, id)`.
//!   Fresh databases are created at v2; v1 files migrate transactionally on
//!   writable open (rows preserved, `project_id` backfilled from stored
//!   JSON, never re-resolved). Read-only opens accept v1 or v2 without
//!   writes; future/foreign schemas are rejected untouched.
//!
//! The snapshot is stored as JSON alongside timestamp/kind/source so future
//! workers can evolve the generic context without migrations.
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
//!   (`project_id = ?`, indexed in v2, `json_extract` fallback in v1
//!   read-only).
//! - `activity_in_range_for_project(id, start, end, limit)`: range + project
//!   filter, oldest-first.
//! - `recently_used_resources_for_project(id, limit)`: unique resources for
//!   one project, newest-first by latest observation. Dedup key is the
//!   priority location key: `file` if present else `url` else `cwd` else
//!   `page`; `git_branch`/`title`/`adapter`/`git_root`/`git_remote` never
//!   affect uniqueness, so the same file with a different cwd/branch still
//!   yields one entry carrying the latest metadata. Rows stream from the
//!   indexed project range and stop as soon as `limit` distinct keys are
//!   found (memory bounded to `limit`); when every row maps to the same key
//!   the scan may still visit the full project history.
//!
//! Security contract (local, reasonably scoped):
//! - Database files are created `0600` from the outset; existing files must
//!   be regular files owned by the current user with private permissions and
//!   must not be symlinks. Sidecars (`-wal`, `-shm`, `-journal`) follow the
//!   same rule when present.
//! - Schema is validated authoritatively before any mutation. Foreign, newer,
//!   or multi-row `schema_version` content is rejected with no modifications.
//! - Initial setup and v1->v2 migration are transactional. History queries
//!   use the read-only constructor, which never creates files, schemas, or
//!   journals.

use crate::desktop_context::{DesktopContext, ResourceContext};
use rusqlite::{params, Connection, OpenFlags};
use std::path::Path;

/// Current schema version. v2 adds indexed `project_id`.
pub const SCHEMA_VERSION: i64 = 2;
/// Last schema version that read-only opens accept without migration.
/// v1 files stay readable (json_extract fallback) and are never mutated.
pub const MIN_READABLE_SCHEMA_VERSION: i64 = 1;
/// Upper bound for `limit` in read queries (keeps responses bounded).
pub const MAX_QUERY_LIMIT: i64 = 1000;
/// Upper bound for stored snapshot JSON bytes (fail-closed append).
pub const MAX_SNAPSHOT_JSON_BYTES: usize = 64 * 1024;
/// Upper bound for kind/source strings.
pub const MAX_TAG_CHARS: usize = 64;

/// One persisted activity row.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ActivityRecord {
    pub id: i64,
    pub observed_at_ms: i64,
    pub kind: String,
    pub source: String,
    pub snapshot_json: String,
}

impl ActivityRecord {
    /// Parse the stored snapshot back into a generic context.
    pub fn snapshot(&self) -> Result<DesktopContext, StoreError> {
        serde_json::from_str(&self.snapshot_json)
            .map_err(|e| StoreError::InvalidArgument(format!("bad snapshot_json: {e}")))
    }

    /// Immutable historical project association for this row.
    ///
    /// Prefers no JSON parsing: the `project_id` column (v2) is
    /// authoritative when present in newer rows, but the snapshot itself is
    /// the ground truth for v1 rows. This helper parses the snapshot and
    /// returns `snapshot.project.id` (empty/absent as `None`). It never
    /// re-resolves against the current registry: historical associations do
    /// not remap.
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

/// Append-only SQLite activity store (synchronous, bounded).
pub struct ActivityStore {
    conn: Connection,
}

impl ActivityStore {
    /// Transactional fresh setup at [`SCHEMA_VERSION`] (v2).
    ///
    /// Caller must have validated that the target is either empty/new or a
    /// matching singleton that merely needs completion; this function itself
    /// never decides foreign vs new.
    fn init_fresh(conn: &mut Connection) -> Result<(), StoreError> {
        let tx = conn.transaction()?;
        tx.execute_batch(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
             CREATE TABLE IF NOT EXISTS activity (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               observed_at_ms INTEGER NOT NULL,
               kind TEXT NOT NULL,
               source TEXT NOT NULL,
               snapshot_json TEXT NOT NULL,
               project_id TEXT
             );
             CREATE INDEX IF NOT EXISTS idx_activity_time ON activity (observed_at_ms, id);
             CREATE INDEX IF NOT EXISTS idx_activity_project ON activity (project_id, observed_at_ms, id);",
        )?;
        // Singleton row: exactly one version row after setup.
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
            // Completing a partial v2 database: ensure the column/index exist.
            Self::ensure_v2_objects_tx(&tx)?;
        } else {
            return Err(StoreError::IncompatibleSchema(format!(
                "schema_version holds {count} rows; expected singleton"
            )));
        }
        tx.commit()?;
        Ok(())
    }

    /// Ensure v2 objects exist inside a writable transaction (idempotent
    /// completion for partial v2 databases). Adds `project_id` when
    /// structurally missing and (re)creates both indexes. Callers must have
    /// run the strict pre-mutation validation first.
    fn ensure_v2_objects_tx(tx: &rusqlite::Transaction<'_>) -> Result<(), StoreError> {
        let has_col: bool = {
            let mut stmt = tx.prepare("PRAGMA table_info(activity)")?;
            let mut rows = stmt.query([])?;
            let mut found = false;
            while let Some(row) = rows.next()? {
                let name: String = row.get(1)?;
                if name == "project_id" {
                    found = true;
                    break;
                }
            }
            found
        };
        if !has_col {
            tx.execute_batch("ALTER TABLE activity ADD COLUMN project_id TEXT;")?;
        }
        tx.execute_batch(
            "CREATE INDEX IF NOT EXISTS idx_activity_time ON activity (observed_at_ms, id);
             CREATE INDEX IF NOT EXISTS idx_activity_project ON activity (project_id, observed_at_ms, id);",
        )?;
        Ok(())
    }

    /// Transactional v1 -> v2 migration: adds indexed `project_id` and
    /// backfills it from the immutable stored snapshot JSON
    /// (`$.project.id` when text, else NULL). Rows are preserved; historical
    /// associations are never re-resolved against the live registry.
    fn migrate_v1_to_v2(conn: &mut Connection) -> Result<(), StoreError> {
        let tx = conn.transaction()?;
        // Caller validated strict v1 shape (no project_id); re-check
        // structurally inside the transaction for TOCTOU safety.
        let has_col: bool = {
            let mut stmt = tx.prepare("PRAGMA table_info(activity)")?;
            let mut rows = stmt.query([])?;
            let mut found = false;
            while let Some(row) = rows.next()? {
                let name: String = row.get(1)?;
                if name == "project_id" {
                    found = true;
                    break;
                }
            }
            found
        };
        if !has_col {
            tx.execute_batch("ALTER TABLE activity ADD COLUMN project_id TEXT;")?;
        }
        // Backfill only NULL slots from stored JSON (idempotent; never
        // overwrites an existing association => no retroactive remap).
        // Projection is normalized lowercase for lookup consistency; stored
        // snapshot JSON bytes are preserved untouched.
        tx.execute_batch(
            "UPDATE activity SET project_id = (
               CASE WHEN json_type(snapshot_json, '$.project.id') = 'text'
                    THEN NULLIF(lower(json_extract(snapshot_json, '$.project.id')), '')
                    ELSE NULL END
             ) WHERE project_id IS NULL;",
        )?;
        tx.execute_batch(
            "CREATE INDEX IF NOT EXISTS idx_activity_time ON activity (observed_at_ms, id);
             CREATE INDEX IF NOT EXISTS idx_activity_project ON activity (project_id, observed_at_ms, id);
             UPDATE schema_version SET version = 2 WHERE version = 1;",
        )?;
        // Exactly one row must remain at v2.
        let count: i64 = tx.query_row("SELECT COUNT(*) FROM schema_version", [], |r| r.get(0))?;
        if count != 1 {
            return Err(StoreError::IncompatibleSchema(format!(
                "schema_version holds {count} rows; expected singleton"
            )));
        }
        let v: i64 = tx.query_row("SELECT version FROM schema_version", [], |r| r.get(0))?;
        if v != SCHEMA_VERSION {
            return Err(StoreError::IncompatibleSchema(format!(
                "migration failed: schema version {v} != supported {SCHEMA_VERSION}"
            )));
        }
        tx.commit()?;
        Ok(())
    }

    /// Complete a v2 database whose column/index may be missing (e.g.
    /// interrupted migration): add column when absent, backfill NULL slots
    /// from stored JSON, recreate indexes. Never overwrites existing
    /// associations. No-op (no transaction, no writes) when already
    /// complete, so contended opens under an exclusive lock do not fail.
    fn ensure_v2_complete(conn: &mut Connection) -> Result<(), StoreError> {
        if !Self::needs_v2_completion(conn)? {
            return Ok(());
        }
        let tx = conn.transaction()?;
        Self::ensure_v2_objects_tx(&tx)?;
        // Backfill only rows whose stored JSON actually carries a project id;
        // pure-NULL rows (no project) are left untouched so empty databases
        // stay write-free. Projection normalized lowercase; JSON untouched.
        tx.execute_batch(
            "UPDATE activity SET project_id = lower(json_extract(snapshot_json, '$.project.id'))
             WHERE project_id IS NULL
               AND json_type(snapshot_json, '$.project.id') = 'text'
               AND NULLIF(lower(json_extract(snapshot_json, '$.project.id')), '') IS NOT NULL;",
        )?;
        tx.commit()?;
        Ok(())
    }

    /// True when a v2 database needs completion writes (missing column,
    /// missing project index, or NULL slots whose JSON carries a project).
    /// Read-only checks only.
    fn needs_v2_completion(conn: &Connection) -> Result<bool, StoreError> {
        if !has_project_column(conn) {
            return Ok(true);
        }
        let idx: i64 = conn.query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_activity_project'",
            [],
            |r| r.get(0),
        )?;
        if idx == 0 {
            return Ok(true);
        }
        let pending: i64 = conn.query_row(
            "SELECT COUNT(*) FROM activity WHERE project_id IS NULL
             AND json_type(snapshot_json, '$.project.id') = 'text'
             AND NULLIF(lower(json_extract(snapshot_json, '$.project.id')), '') IS NOT NULL",
            [],
            |r| r.get(0),
        )?;
        Ok(pending > 0)
    }

    /// Authoritative pre-mutation validation + setup for writable opens.
    ///
    /// Strict structural policy (BEFORE any mutation, Phase 1 consistent):
    /// - Object whitelist: only `schema_version`/`activity` tables (plus
    ///   `sqlite_%` internals) and `idx_activity_time`/`idx_activity_project`
    ///   indexes; extra tables/triggers/views/indexes reject untouched.
    /// - Present tables are shape-checked via `PRAGMA table_info`
    ///   (columns/types/PK/NOT NULL/defaults); present allowlisted indexes are
    ///   order-checked via `PRAGMA index_info`. Missing indexes are OK
    ///   (migration/init recreates); wrong indexes reject unchanged.
    /// - `schema_version` singleton must be `[1]` with v1 shape or `[2]` with
    ///   v2 shape; mismatches, futures, and multi-rows reject untouched.
    /// - Fresh/partial-ours databases complete via [`Self::init_fresh`];
    ///   v1 → [`Self::migrate_v1_to_v2`]; v2 → [`Self::ensure_v2_complete`].
    fn ensure_writable_schema(conn: &mut Connection) -> Result<(), StoreError> {
        let tables = existing_tables(conn)?;
        let has_version = tables.iter().any(|t| t == "schema_version");
        let has_activity = tables.iter().any(|t| t == "activity");
        if !has_version && !has_activity {
            // Truly empty (only sqlite internals) may init; anything else
            // (foreign tables/views/triggers/indexes) rejects.
            Self::validate_objects_strict(conn)?;
            if tables.is_empty() {
                return Self::init_fresh(conn);
            }
            return Err(StoreError::IncompatibleSchema(
                "foreign database: missing schema_version/activity tables".to_string(),
            ));
        }
        if !has_version || !has_activity {
            // Partial: whitelist + shape-check present tables, then complete.
            Self::validate_objects_strict(conn)?;
            if has_version {
                let cols = pragma_table_info(conn, "schema_version")?;
                validate_schema_version_shape(&cols)?;
                // Partial version content must be empty or current; v1-alone
                // without activity is malformed (init would version-mismatch).
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
                let cols = pragma_table_info(conn, "activity")?;
                // Must be a supported shape (v1 or v2); malformed rejects.
                let _ = validate_activity_shape(&cols)?;
            }
            return Self::init_fresh(conn);
        }
        // Both tables present: strict checks BEFORE any mutation.
        Self::validate_objects_strict(conn)?;
        let vcols = pragma_table_info(conn, "schema_version")?;
        validate_schema_version_shape(&vcols)?;
        let acols = pragma_table_info(conn, "activity")?;
        let shape = validate_activity_shape(&acols)?;
        let versions = Self::read_versions(conn)?;
        match versions.as_slice() {
            [] => Self::init_fresh(conn),
            [1] => match shape {
                ActivityShape::V1 => Self::migrate_v1_to_v2(conn),
                ActivityShape::V2 => Err(StoreError::IncompatibleSchema(
                    "version 1 with v2 activity shape (version/shape mismatch)".to_string(),
                )),
            },
            [v] if *v == SCHEMA_VERSION => match shape {
                ActivityShape::V2 => Self::ensure_v2_complete(conn),
                ActivityShape::V1 => Err(StoreError::IncompatibleSchema(
                    "version 2 with v1 activity shape (version/shape mismatch)".to_string(),
                )),
            },
            [v] => Err(StoreError::IncompatibleSchema(format!(
                "schema version {v} != supported {SCHEMA_VERSION}"
            ))),
            many => Err(StoreError::IncompatibleSchema(format!(
                "schema_version holds {} rows ({:?}); expected singleton {SCHEMA_VERSION}",
                many.len(),
                &many[..many.len().min(4)]
            ))),
        }
    }

    /// Strict object/index validation wrapper (read-only, no writes).
    fn validate_objects_strict(conn: &Connection) -> Result<(), StoreError> {
        validate_objects_and_indexes(conn)
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

    /// Legacy helper retained for compatibility: `true` when fresh setup is
    /// required (empty/new or v1-needs-migration handled by the caller),
    /// `false` when already at the current version. Prefer
    /// [`Self::ensure_writable_schema`] for opens.
    #[allow(dead_code)]
    fn needs_init(conn: &Connection) -> Result<bool, StoreError> {
        let tables = existing_tables(conn)?;
        let has_version = tables.iter().any(|t| t == "schema_version");
        let has_activity = tables.iter().any(|t| t == "activity");
        if !has_version && !has_activity {
            if tables.is_empty() {
                return Ok(true);
            }
            return Err(StoreError::IncompatibleSchema(
                "foreign database: missing schema_version/activity tables".to_string(),
            ));
        }
        if !has_version || !has_activity {
            let foreign: Vec<&String> = tables
                .iter()
                .filter(|t| *t != "schema_version" && *t != "activity" && !t.starts_with("sqlite_"))
                .collect();
            if !foreign.is_empty() {
                return Err(StoreError::IncompatibleSchema(format!(
                    "foreign database: unexpected tables {foreign:?}, missing ours"
                )));
            }
            return Ok(true);
        }
        let mut stmt = conn.prepare("SELECT version FROM schema_version")?;
        let versions: Vec<i64> = stmt
            .query_map([], |r| r.get(0))?
            .collect::<Result<Vec<_>, _>>()?;
        match versions.as_slice() {
            [] => Ok(true),
            [v] if *v == SCHEMA_VERSION => Ok(false),
            [1] => Ok(true),
            [v] => Err(StoreError::IncompatibleSchema(format!(
                "schema version {v} != supported {SCHEMA_VERSION}"
            ))),
            many => Err(StoreError::IncompatibleSchema(format!(
                "schema_version holds {} rows ({:?}); expected singleton {SCHEMA_VERSION}",
                many.len(),
                &many[..many.len().min(4)]
            ))),
        }
    }

    /// Validate an already-open read-only handle. No writes of any kind.
    /// Accepts v1 (json_extract fallback) and v2 with strict shape checks;
    /// rejects foreign/newer/malformed/multi-row without modification.
    fn validate_read_only(conn: &Connection) -> Result<(), StoreError> {
        let tables = existing_tables(conn)?;
        if !(tables.iter().any(|t| t == "schema_version")
            && tables.iter().any(|t| t == "activity"))
        {
            return Err(StoreError::IncompatibleSchema(
                "foreign database: missing schema_version/activity tables".to_string(),
            ));
        }
        // Strict whitelist + shapes, still no writes.
        Self::validate_objects_strict(conn)?;
        let vcols = pragma_table_info(conn, "schema_version").map_err(|e| {
            StoreError::IncompatibleSchema(format!("bad schema_version table: {e}"))
        })?;
        validate_schema_version_shape(&vcols)?;
        let acols = pragma_table_info(conn, "activity").map_err(|e| {
            StoreError::IncompatibleSchema(format!("bad activity table: {e}"))
        })?;
        let shape = validate_activity_shape(&acols)?;
        let versions = Self::read_versions(conn).map_err(|e| {
            StoreError::IncompatibleSchema(format!("bad schema_version rows: {e}"))
        })?;
        match versions.as_slice() {
            [1] => match shape {
                ActivityShape::V1 => Ok(()),
                ActivityShape::V2 => Err(StoreError::IncompatibleSchema(
                    "version 1 with v2 activity shape".to_string(),
                )),
            },
            [v] if *v == SCHEMA_VERSION => match shape {
                ActivityShape::V2 => Ok(()),
                ActivityShape::V1 => Err(StoreError::IncompatibleSchema(
                    "version 2 with v1 activity shape".to_string(),
                )),
            },
            [v] => Err(StoreError::IncompatibleSchema(format!(
                "schema version {v} != supported {SCHEMA_VERSION} (readable: {MIN_READABLE_SCHEMA_VERSION}..={SCHEMA_VERSION})"
            ))),
            _ => Err(StoreError::IncompatibleSchema(format!(
                "schema_version holds {} rows; expected singleton {SCHEMA_VERSION}",
                versions.len()
            ))),
        }
    }

    /// Open (creating) a file database.
    ///
    /// Creates parents `0700` and the database `0600` from the outset.
    /// Existing files/sidecars must be private regular files owned by the
    /// current user (no symlinks); violations fail closed. Foreign/newer
    /// schemas are rejected with no modifications.
    pub fn open<P: AsRef<Path>>(path: P) -> Result<Self, StoreError> {
        let path = path.as_ref();
        crate::desktop_paths::ensure_parent_for_file(path)
            .map_err(|e| map_parent_error(path, &e))?;
        crate::desktop_paths::validate_sidecars(path)
            .map_err(StoreError::UnsafePath)?;

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
            Err(e) => return Err(StoreError::Sqlite(format!("cannot inspect {}: {e}", path.display()))),
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
        // initialized at v2, v1 files migrate transactionally, v2 files are
        // completed; foreign/newer/multi-row reject untouched.
        Self::ensure_writable_schema(&mut conn)?;
        // Best-effort WAL for a private local DB (WAL/SHM stay beside the DB
        // inside the private directory). Ignore errors: plain rollback-journal
        // still works.
        let _ = conn.query_row("PRAGMA journal_mode=WAL", [], |r| r.get::<_, String>(0));
        let _ = conn.execute_batch("PRAGMA synchronous=NORMAL;");
        Ok(Self { conn })
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
            Err(e) => return Err(StoreError::Sqlite(format!("cannot inspect {}: {e}", path.display()))),
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
        Ok(Self { conn })
    }

    /// In-memory database (tests/manual validation).
    pub fn open_in_memory() -> Result<Self, StoreError> {
        let mut conn = Connection::open_in_memory()?;
        let _ = conn.busy_timeout(std::time::Duration::from_millis(100));
        Self::init_fresh(&mut conn)?;
        Ok(Self { conn })
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

    /// True when the `activity.project_id` column exists (v2). v1
    /// read-only databases lack it and use a `json_extract` fallback.
    fn has_project_column(&self) -> bool {
        has_project_column(&self.conn)
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
        let project_id: Option<&str> = normalized
            .project
            .as_ref()
            .map(|p| p.id.as_str())
            .filter(|s| !s.is_empty());
        if self.has_project_column() {
            self.conn.execute(
                "INSERT INTO activity (observed_at_ms, kind, source, snapshot_json, project_id) VALUES (?1, ?2, ?3, ?4, ?5)",
                params![normalized.observed_at_ms, kind, source, snapshot_json, project_id],
            )?;
        } else {
            // v1 read-only handles never append (query_only=ON fails here);
            // writable handles always have the column after ensure. Fallback
            // keeps the JSON authoritative when the column is absent.
            self.conn.execute(
                "INSERT INTO activity (observed_at_ms, kind, source, snapshot_json) VALUES (?1, ?2, ?3, ?4)",
                params![normalized.observed_at_ms, kind, source, snapshot_json],
            )?;
        }
        Ok(self.conn.last_insert_rowid())
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

    /// Newest-first bounded query. Deterministic: `(observed_at_ms DESC,
    /// id DESC)` so equal timestamps order by row id.
    pub fn recent_activity(&self, limit: i64) -> Result<Vec<ActivityRecord>, StoreError> {
        let limit = Self::checked_limit(limit)?;
        let mut stmt = self.conn.prepare(
            "SELECT id, observed_at_ms, kind, source, snapshot_json FROM activity
             ORDER BY observed_at_ms DESC, id DESC LIMIT ?1",
        )?;
        let rows = stmt.query_map(params![limit], row_to_record)?;
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
        let mut stmt = self.conn.prepare(
            "SELECT id, observed_at_ms, kind, source, snapshot_json FROM activity
             WHERE observed_at_ms >= ?1 AND observed_at_ms < ?2
             ORDER BY observed_at_ms ASC, id ASC LIMIT ?3",
        )?;
        let rows = stmt.query_map(params![start_ms, end_ms, limit], row_to_record)?;
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
    /// `project_id` column on v2; v1 read-only databases fall back to
    /// `json_extract(snapshot_json,'$.project.id')` (same semantics, no
    /// index). Ordering is `(observed_at_ms DESC, id DESC)`.
    pub fn recent_activity_for_project(
        &self,
        project_id: &str,
        limit: i64,
    ) -> Result<Vec<ActivityRecord>, StoreError> {
        let pid = normalize_project_id(project_id)?;
        let limit = Self::checked_limit(limit)?;
        if self.has_project_column() {
            let mut stmt = self.conn.prepare(
                "SELECT id, observed_at_ms, kind, source, snapshot_json FROM activity
                 WHERE project_id = ?1
                 ORDER BY observed_at_ms DESC, id DESC LIMIT ?2",
            )?;
            let rows = stmt.query_map(params![pid, limit], row_to_record)?;
            let mut out = Vec::new();
            for r in rows {
                out.push(r?);
            }
            Ok(out)
        } else {
            let mut stmt = self.conn.prepare(
                "SELECT id, observed_at_ms, kind, source, snapshot_json FROM activity
                 WHERE json_extract(snapshot_json, '$.project.id') = ?1
                 ORDER BY observed_at_ms DESC, id DESC LIMIT ?2",
            )?;
            let rows = stmt.query_map(params![pid, limit], row_to_record)?;
            let mut out = Vec::new();
            for r in rows {
                out.push(r?);
            }
            Ok(out)
        }
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
        if self.has_project_column() {
            let mut stmt = self.conn.prepare(
                "SELECT id, observed_at_ms, kind, source, snapshot_json FROM activity
                 WHERE project_id = ?1 AND observed_at_ms >= ?2 AND observed_at_ms < ?3
                 ORDER BY observed_at_ms ASC, id ASC LIMIT ?4",
            )?;
            let rows =
                stmt.query_map(params![pid, start_ms, end_ms, limit], row_to_record)?;
            let mut out = Vec::new();
            for r in rows {
                out.push(r?);
            }
            Ok(out)
        } else {
            let mut stmt = self.conn.prepare(
                "SELECT id, observed_at_ms, kind, source, snapshot_json FROM activity
                 WHERE json_extract(snapshot_json, '$.project.id') = ?1
                   AND observed_at_ms >= ?2 AND observed_at_ms < ?3
                 ORDER BY observed_at_ms ASC, id ASC LIMIT ?4",
            )?;
            let rows =
                stmt.query_map(params![pid, start_ms, end_ms, limit], row_to_record)?;
            let mut out = Vec::new();
            for r in rows {
                out.push(r?);
            }
            Ok(out)
        }
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
    /// Streams that project's rows newest-first (`observed_at_ms DESC, id
    /// DESC`, indexed in v2) and dedups by priority location key: `file` if
    /// present else `url` else `cwd` else `page`. `git_branch`/`title`/
    /// `adapter`/`git_root`/`git_remote` never affect uniqueness, so the same
    /// file with a different cwd/branch yields one entry carrying the latest
    /// metadata. Rows without a non-empty resource (or without any of
    /// file/url/cwd/page) are skipped. Stops as soon as `limit` distinct keys
    /// are found, so memory stays bounded to `limit` (`1..=MAX_QUERY_LIMIT`);
    /// when all rows share one key the scan may still visit the full project
    /// history.
    pub fn recently_used_resources_for_project(
        &self,
        project_id: &str,
        limit: i64,
    ) -> Result<Vec<ProjectResource>, StoreError> {
        let pid = normalize_project_id(project_id)?;
        let limit = Self::checked_limit(limit)?;
        let sql = if self.has_project_column() {
            "SELECT id, observed_at_ms, kind, source, snapshot_json FROM activity
             WHERE project_id = ?1
             ORDER BY observed_at_ms DESC, id DESC"
        } else {
            "SELECT id, observed_at_ms, kind, source, snapshot_json FROM activity
             WHERE json_extract(snapshot_json, '$.project.id') = ?1
             ORDER BY observed_at_ms DESC, id DESC"
        };
        let mut stmt = self.conn.prepare(sql)?;
        let mut rows = stmt.query(params![pid])?;
        let mut seen: std::collections::HashSet<(char, String)> =
            std::collections::HashSet::new();
        let mut out: Vec<ProjectResource> = Vec::new();
        while let Some(row) = rows.next()? {
            let record = row_to_record(row)?;
            let Ok(ctx) = record.snapshot() else { continue };
            let Some(res) = ctx.resource else { continue };
            if res.is_empty() {
                continue;
            }
            let Some(key) = resource_location_key(&res) else { continue };
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
}

fn existing_tables(conn: &Connection) -> Result<Vec<String>, StoreError> {
    let mut stmt = conn.prepare(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
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

fn pragma_table_info(
    conn: &Connection,
    table: &str,
) -> Result<Vec<ColInfo>, StoreError> {
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

/// Supported `activity` shapes (structural).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ActivityShape {
    V1,
    V2,
}

/// Strict `activity` shape check. v1 is exactly the 5 documented columns;
/// v2 adds nullable `project_id TEXT` (no default) at the end. Types, PK,
/// `NOT NULL`, and defaults are enforced; anything else (missing/extra/
/// reordered columns, wrong types, wrong nullability, defaults, or PK) is
/// malformed. Detection is structural via `PRAGMA table_info`, never
/// `sql.contains("project_id")` (comments or other identifiers must not
/// fool it).
fn validate_activity_shape(cols: &[ColInfo]) -> Result<ActivityShape, StoreError> {
    let bad = |msg: String| StoreError::IncompatibleSchema(format!("bad activity shape: {msg}"));
    let expect = |c: &ColInfo,
                  name: &str,
                  typ: &str,
                  notnull: i64,
                  pk: i64,
                  what: &str| {
        if c.name != name || !coltype_is(c, typ) || c.notnull != notnull || c.pk != pk || c.dflt.is_some() {
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
            expect(&cols[1], "observed_at_ms", "INTEGER", 1, 0, "observed_at_ms")?;
            expect(&cols[2], "kind", "TEXT", 1, 0, "kind")?;
            expect(&cols[3], "source", "TEXT", 1, 0, "source")?;
            expect(&cols[4], "snapshot_json", "TEXT", 1, 0, "snapshot_json")?;
            Ok(ActivityShape::V1)
        }
        6 => {
            expect(&cols[0], "id", "INTEGER", 0, 1, "id")?;
            expect(&cols[1], "observed_at_ms", "INTEGER", 1, 0, "observed_at_ms")?;
            expect(&cols[2], "kind", "TEXT", 1, 0, "kind")?;
            expect(&cols[3], "source", "TEXT", 1, 0, "source")?;
            expect(&cols[4], "snapshot_json", "TEXT", 1, 0, "snapshot_json")?;
            expect(&cols[5], "project_id", "TEXT", 0, 0, "project_id")?;
            Ok(ActivityShape::V2)
        }
        n => Err(bad(format!("expected 5 (v1) or 6 (v2) columns, got {n}"))),
    }
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

/// Object whitelist + index-shape policy (read-only checks, no writes).
///
/// - Tables (excluding `sqlite_%` internals like `sqlite_sequence`) must be a
///   subset of `{schema_version, activity}`; any other table rejects.
/// - Triggers/views (excluding `sqlite_%`) reject in any count.
/// - Indexes (excluding `sqlite_autoindex_*` internals) must be a subset of
///   `{idx_activity_time, idx_activity_project}`; any other index rejects.
/// - Hidden constraint indexes are rejected via full `PRAGMA index_list`
///   (including `sqlite_%` autoindexes): any entry with `unique != 0`
///   (e.g. `kind TEXT NOT NULL UNIQUE`, which `table_info` alone cannot see
///   and which would make legitimate repeat appends fail), `partial != 0`
///   (e.g. `... WHERE kind='focus'`, which `index_info` column order alone
///   cannot see and which would silently degrade project queries to full
///   scans), `origin != 'c'`, or any `sqlite_autoindex_*` entry rejects.
///   Our schema uses plain rowid `INTEGER PRIMARY KEY`s, so no autoindex is
///   ever expected.
/// - Present allowlisted indexes must have the exact documented column order
///   (`index_info`), `ASC`/`BINARY` on every key column (`index_xinfo`
///   `desc == 0`, `coll == BINARY`); `DESC` or alternate collations reject
///   unchanged. Missing indexes are OK (v1 legitimately lacks the project
///   index; migration/init recreates them).
/// - Table SQL is scanned for unsupported constraint tokens (`CHECK`,
///   `UNIQUE`, `FOREIGN KEY`, `REFERENCES`) after stripping comments and
///   quoted strings/literals; any hit rejects. `table_info` cannot see
///   `CHECK` at all, and `UNIQUE` is double-covered here plus the
///   autoindex rule above.
fn validate_objects_and_indexes(conn: &Connection) -> Result<(), StoreError> {
    let mut stmt = conn.prepare(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'",
    )?;
    let mut rows = stmt.query([])?;
    let mut extra: Vec<String> = Vec::new();
    let mut present_indexes: Vec<String> = Vec::new();
    while let Some(row) = rows.next()? {
        let typ: String = row.get(0)?;
        let name: String = row.get(1)?;
        match typ.as_str() {
            "table" if name == "schema_version" || name == "activity" => {}
            "index" if name == "idx_activity_time" || name == "idx_activity_project" => {
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
        return Err(StoreError::IncompatibleSchema(format!(
            "foreign database objects {extra:?}; expected only schema_version/activity + idx_activity_time/idx_activity_project"
        )));
    }
    // Hidden-constraint sweep over ALL index_list entries (including
    // sqlite_autoindex_*): uniqueness, partial predicates, non-CREATE origin.
    for tbl in ["activity", "schema_version"] {
        for e in pragma_index_list_full(conn, tbl)? {
            if e.name.starts_with("sqlite_autoindex") {
                return Err(StoreError::IncompatibleSchema(format!(
                    "unsupported constraint-created index {} on {tbl} (UNIQUE constraint)",
                    e.name
                )));
            }
            if e.unique != 0 {
                return Err(StoreError::IncompatibleSchema(format!(
                    "unsupported UNIQUE index {} on {tbl}",
                    e.name
                )));
            }
            if e.partial != 0 {
                return Err(StoreError::IncompatibleSchema(format!(
                    "unsupported partial index {} on {tbl}",
                    e.name
                )));
            }
            if e.origin != "c" {
                return Err(StoreError::IncompatibleSchema(format!(
                    "unsupported index {} on {tbl} (origin {})",
                    e.name, e.origin
                )));
            }
        }
    }
    // Unsupported table-constraint tokens (CHECK invisible to table_info;
    // UNIQUE double-covered with the autoindex rule above).
    for tbl in ["schema_version", "activity"] {
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
        let ok = match idx.as_str() {
            "idx_activity_time" => cols == vec!["observed_at_ms".to_string(), "id".to_string()],
            "idx_activity_project" => {
                cols
                    == vec![
                        "project_id".to_string(),
                        "observed_at_ms".to_string(),
                        "id".to_string(),
                    ]
            }
            _ => false,
        };
        if !ok {
            return Err(StoreError::IncompatibleSchema(format!(
                "wrong index {idx} columns {cols:?}; expected documented order"
            )));
        }
        // Sort/collation must match the documented ordering: ASC + BINARY on
        // every key column. A DESC or NOCASE index with the right names would
        // otherwise be accepted by column order alone.
        for kc in pragma_index_key_columns(conn, &idx)? {
            if kc.desc != 0 {
                return Err(StoreError::IncompatibleSchema(format!(
                    "wrong index {idx} sort order on {} (DESC)",
                    kc.name
                )));
            }
            if !kc.coll.eq_ignore_ascii_case("BINARY") {
                return Err(StoreError::IncompatibleSchema(format!(
                    "wrong index {idx} collation on {} ({})",
                    kc.name, kc.coll
                )));
            }
        }
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
    let mut stmt = conn.prepare(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?1",
    )?;
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

/// True when `activity.project_id` exists structurally via `PRAGMA
/// table_info` (no writes, works on read-only handles; never SQL-text
/// matching, so comments or other identifiers cannot fool it).
fn has_project_column(conn: &Connection) -> bool {
    match pragma_table_info(conn, "activity") {
        Ok(cols) => cols.iter().any(|c| c.name == "project_id"),
        Err(_) => false,
    }
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
    } else if s.contains("unable to open") && s.contains("not found")
        || s.contains("no such file")
    {
        StoreError::NotFound(format!("no database at {}: {e}", path.display()))
    } else {
        StoreError::Sqlite(format!("cannot open {}: {e}", path.display()))
    }
}

fn row_to_record(row: &rusqlite::Row<'_>) -> rusqlite::Result<ActivityRecord> {
    Ok(ActivityRecord {
        id: row.get(0)?,
        observed_at_ms: row.get(1)?,
        kind: row.get(2)?,
        source: row.get(3)?,
        snapshot_json: row.get(4)?,
    })
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
            conn.execute_batch("CREATE TABLE other (id INTEGER PRIMARY KEY); INSERT INTO other VALUES (1);")
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
        // v2 fresh DB holds singleton [2]; inserting a distinct version (1)
        // creates multi-row [2,1] which must be rejected (not silently
        // picked). Preserves the original v1-era intent after the v2 bump.
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
}
