//! SQLite append-only activity history.
//!
//! Schema (versioned):
//! - `schema_version(version INTEGER PRIMARY KEY)` holding [`SCHEMA_VERSION`]
//!   as a singleton row.
//! - `activity(id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at_ms INTEGER,
//!   kind TEXT, source TEXT, snapshot_json TEXT)` with index
//!   `idx_activity_time(observed_at_ms, id)`.
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
//!
//! Security contract (local, reasonably scoped):
//! - Database files are created `0600` from the outset; existing files must
//!   be regular files owned by the current user with private permissions and
//!   must not be symlinks. Sidecars (`-wal`, `-shm`, `-journal`) follow the
//!   same rule when present.
//! - Schema is validated authoritatively before any mutation. Foreign, newer,
//!   or multi-row `schema_version` content is rejected with no modifications.
//! - Initial setup is transactional. History queries use the read-only
//!   constructor, which never creates files, schemas, or journals.

use crate::desktop_context::DesktopContext;
use rusqlite::{params, Connection, OpenFlags};
use std::path::Path;

/// Current schema version. Bumped only with a migration.
pub const SCHEMA_VERSION: i64 = 1;
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
    /// Transactional fresh setup. Caller must have validated that the target
    /// is either empty/new or a matching singleton that merely needs
    /// completion; this function itself never decides foreign vs new.
    fn init_fresh(conn: &mut Connection) -> Result<(), StoreError> {
        let tx = conn.transaction()?;
        tx.execute_batch(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
             CREATE TABLE IF NOT EXISTS activity (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               observed_at_ms INTEGER NOT NULL,
               kind TEXT NOT NULL,
               source TEXT NOT NULL,
               snapshot_json TEXT NOT NULL
             );
             CREATE INDEX IF NOT EXISTS idx_activity_time ON activity (observed_at_ms, id);",
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
        } else {
            return Err(StoreError::IncompatibleSchema(format!(
                "schema_version holds {count} rows; expected singleton"
            )));
        }
        tx.commit()?;
        Ok(())
    }

    /// Authoritative pre-mutation validation. Returns `true` when fresh setup
    /// is required (empty/new database), `false` when the existing database
    /// already matches. Errors without modifying anything for foreign/newer/
    /// corrupt content.
    fn needs_init(conn: &Connection) -> Result<bool, StoreError> {
        let tables = existing_tables(conn)?;
        let has_version = tables.iter().any(|t| t == "schema_version");
        let has_activity = tables.iter().any(|t| t == "activity");
        if !has_version && !has_activity {
            if tables.is_empty() {
                return Ok(true);
            }
            // Non-empty foreign database without our tables: reject, no mods.
            return Err(StoreError::IncompatibleSchema(
                "foreign database: missing schema_version/activity tables".to_string(),
            ));
        }
        if !has_version || !has_activity {
            // Partial: one of ours missing. If the database otherwise holds
            // only our tables (or is empty aside from sqlite internals),
            // allow completion via init; otherwise treat as foreign.
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
        // Both tables present: enforce singleton version.
        let mut stmt = conn.prepare("SELECT version FROM schema_version")?;
        let versions: Vec<i64> = stmt
            .query_map([], |r| r.get(0))?
            .collect::<Result<Vec<_>, _>>()?;
        match versions.as_slice() {
            [] => Ok(true),
            [v] if *v == SCHEMA_VERSION => Ok(false),
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
    fn validate_read_only(conn: &Connection) -> Result<(), StoreError> {
        let tables = existing_tables(conn)?;
        if !(tables.iter().any(|t| t == "schema_version")
            && tables.iter().any(|t| t == "activity"))
        {
            return Err(StoreError::IncompatibleSchema(
                "foreign database: missing schema_version/activity tables".to_string(),
            ));
        }
        let mut stmt = conn
            .prepare("SELECT version FROM schema_version")
            .map_err(|e| StoreError::IncompatibleSchema(format!("bad schema_version table: {e}")))?;
        let versions: Vec<i64> = stmt
            .query_map([], |r| r.get(0))
            .map_err(|e| StoreError::IncompatibleSchema(format!("bad schema_version rows: {e}")))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| StoreError::IncompatibleSchema(format!("bad schema_version rows: {e}")))?;
        match versions.as_slice() {
            [v] if *v == SCHEMA_VERSION => Ok(()),
            [v] => Err(StoreError::IncompatibleSchema(format!(
                "schema version {v} != supported {SCHEMA_VERSION}"
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

        // Authoritative validation BEFORE any mutation.
        let fresh = Self::needs_init(&conn)?;
        if fresh {
            Self::init_fresh(&mut conn)?;
        }
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

    /// Append one meaningful snapshot. Returns the row id.
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
        let snapshot_json = serde_json::to_string(ctx)
            .map_err(|e| StoreError::InvalidArgument(format!("snapshot serialize: {e}")))?;
        if snapshot_json.len() > MAX_SNAPSHOT_JSON_BYTES {
            return Err(StoreError::InvalidArgument(format!(
                "snapshot_json {} bytes exceeds {}",
                snapshot_json.len(),
                MAX_SNAPSHOT_JSON_BYTES
            )));
        }
        self.conn.execute(
            "INSERT INTO activity (observed_at_ms, kind, source, snapshot_json) VALUES (?1, ?2, ?3, ?4)",
            params![ctx.observed_at_ms, kind, source, snapshot_json],
        )?;
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
        let store = ActivityStore::open_in_memory().unwrap();
        store
            .conn
            .execute("INSERT INTO schema_version (version) VALUES (2)", [])
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
