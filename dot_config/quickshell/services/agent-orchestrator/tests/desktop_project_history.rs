//! Project history/query tests (schema v2 + CLI).
//!
//! Covers:
//! - v1 -> v2 transactional migration (rows preserved, project_id backfilled
//!   from stored JSON, never re-resolved; index exists)
//! - project query ordering/limits/range filters + UUID validation
//! - recently-used resources dedup (priority key file>url>cwd>page;
//!   branch/title ignored, incl. same-file-different-cwd dedup)
//! - unknown projects (empty, not error)
//! - old v1 DB read-only compatibility without writes
//! - future/foreign rejection untouched
//! - missing-DB CLI (array/null) with args-validated-first
//! - `current` unavailable without compositor + stable fake-socket snapshot
//!   and focus-race rejection
//! - `history --project` + `last-activity` + `resources` CLI shapes
//!
//! Notes/limitations (also reported):
//! - `recently_used_resources_for_project` streams the indexed project range
//!   newest-first and stops once `limit` distinct keys are found; memory stays
//!   bounded to `limit`. When all rows share one key the scan may still visit
//!   the full project history.
//! - Dedup key is priority `file` else `url` else `cwd` else `page`;
//!   adapter/git_root/git_remote/git_branch/title never affect uniqueness.
//! - `current` is a fresh on-demand snapshot (not collector-tracker state,
//!   not a historical row); PID participates in the race check.

use qs_agent_orchestrator::desktop_context::{
    DesktopContext, FocusedWindow, ProjectContext, ResourceContext, Source, Workspace,
};
use qs_agent_orchestrator::desktop_store::{normalize_project_id, ActivityStore};
use std::path::{Path, PathBuf};
use std::process::Command;

const A_ID: &str = "11111111-1111-1111-1111-111111111111";
const B_ID: &str = "22222222-2222-2222-2222-222222222222";
const UNKNOWN_ID: &str = "99999999-9999-9999-9999-999999999999";

fn tmpdir(tag: &str) -> PathBuf {
    let p = std::env::temp_dir().join(format!(
        "qs-projhist-{tag}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&p).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o700));
    }
    p
}

fn bin_path() -> PathBuf {
    if let Some(p) = option_env!("CARGO_BIN_EXE_qs_desktop_context") {
        return PathBuf::from(p);
    }
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("target")
        .join("debug")
        .join("qs-desktop-context")
}

fn ctx_with(
    ts: i64,
    win: &str,
    title: &str,
    resource: Option<ResourceContext>,
    project: Option<ProjectContext>,
) -> DesktopContext {
    let base = DesktopContext::available(
        Source::Hyprland,
        Some(FocusedWindow::new(win, "kitty", title)),
        Some(Workspace::new("1", "1")),
        ts,
    );
    let with_r = base.with_resource(resource);
    with_r.with_project(project)
}

fn proj(id: &str, name: &str, by: &str) -> ProjectContext {
    ProjectContext::new(id, name, by)
}

fn res_file(file: &str, cwd: &str, branch: Option<&str>, title: Option<&str>) -> ResourceContext {
    let mut r = ResourceContext::new(
        "neovim",
        Some(file),
        Some(cwd),
        None,
        branch,
        None,
        None,
        title,
    );
    // ResourceContext::new takes branch as git_branch (5th) and title (8th);
    // keep adapter neovim.
    let _ = &mut r;
    r
}

// --- Migration ---

fn create_v1_db(path: &Path, rows: Vec<(i64, &str, &str, DesktopContext)>) {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).unwrap();
    }
    let _ = std::fs::remove_file(path);
    let conn = rusqlite::Connection::open(path).unwrap();
    conn.execute_batch(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
         CREATE TABLE activity (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           observed_at_ms INTEGER NOT NULL,
           kind TEXT NOT NULL,
           source TEXT NOT NULL,
           snapshot_json TEXT NOT NULL
         );
         CREATE INDEX idx_activity_time ON activity (observed_at_ms, id);
         INSERT INTO schema_version (version) VALUES (1);",
    )
    .unwrap();
    for (ts, kind, source, ctx) in rows {
        let json = serde_json::to_string(&ctx).unwrap();
        conn.execute(
            "INSERT INTO activity (observed_at_ms, kind, source, snapshot_json) VALUES (?1, ?2, ?3, ?4)",
            rusqlite::params![ts, kind, source, json],
        )
        .unwrap();
    }
    drop(conn);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
}

#[test]
fn migration_v1_preserves_rows_and_backfills() {
    let dir = tmpdir("migrate");
    let db = dir.join("activity.db");
    let a_ctx = ctx_with(
        1000,
        "0x1",
        "a",
        Some(ResourceContext::new(
            "neovim",
            Some("/tmp/a.md"),
            Some("/tmp"),
            None,
            None,
            None,
            None,
            None,
        )),
        Some(proj(A_ID, "Alpha", "file")),
    );
    let b_ctx = ctx_with(2000, "0x2", "b", None, None);
    // Legacy Phase-1 JSON: no resource/project keys at all.
    let legacy: DesktopContext = serde_json::from_str(
        r#"{"focused_window":{"id":"0x3","application":"kitty","title":"old"},"workspace":{"id":"1","name":"1"},"available":true,"source":"hyprland","observed_at_ms":3000}"#,
    )
    .unwrap();
    create_v1_db(
        &db,
        vec![
            (1000, "focus", "hyprland", a_ctx.clone()),
            (2000, "focus", "hyprland", b_ctx.clone()),
            (3000, "focus", "hyprland", legacy.clone()),
        ],
    );
    // Writable open migrates transactionally.
    let store = ActivityStore::open(&db).unwrap();
    assert_eq!(store.schema_version().unwrap(), 2);
    // Rows preserved.
    let all = store.recent_activity(10).unwrap();
    assert_eq!(all.len(), 3);
    assert_eq!(all[0].observed_at_ms, 3000);
    // Backfilled association is immutable historical truth.
    let for_a = store.recent_activity_for_project(A_ID, 10).unwrap();
    assert_eq!(for_a.len(), 1);
    assert_eq!(for_a[0].observed_at_ms, 1000);
    assert_eq!(for_a[0].snapshot().unwrap(), a_ctx);
    assert_eq!(for_a[0].project_id().as_deref(), Some(A_ID));
    // Unknown project: empty.
    assert!(store
        .recent_activity_for_project(UNKNOWN_ID, 10)
        .unwrap()
        .is_empty());
    // Index exists.
    let conn = rusqlite::Connection::open(&db).unwrap();
    let idx: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_activity_project'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(idx, 1, "v2 must have idx_activity_project");
    // Appending after migration stores the column directly.
    let new_ctx = ctx_with(
        4000,
        "0x4",
        "c",
        Some(ResourceContext::new(
            "kitty",
            None,
            Some("/tmp"),
            None,
            None,
            None,
            None,
            None,
        )),
        Some(proj(A_ID, "Alpha", "cwd")),
    );
    store.append("context", "hyprland", &new_ctx).unwrap();
    let for_a2 = store.recent_activity_for_project(A_ID, 10).unwrap();
    assert_eq!(for_a2.len(), 2);
    assert_eq!(for_a2[0].observed_at_ms, 4000);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn project_queries_ordering_limits_filters() {
    let store = ActivityStore::open_in_memory().unwrap();
    // A: ts 1000, 2000, 2000 (tie); B: ts 1500.
    let a1 = ctx_with(1000, "0x1", "a1", None, Some(proj(A_ID, "A", "cwd")));
    let b = ctx_with(1500, "0x2", "b", None, Some(proj(B_ID, "B", "cwd")));
    let a2 = ctx_with(2000, "0x3", "a2", None, Some(proj(A_ID, "A", "cwd")));
    let a3 = ctx_with(2000, "0x4", "a3", None, Some(proj(A_ID, "A", "cwd")));
    for (k, c) in [("focus", a1), ("focus", b), ("focus", a2), ("focus", a3)] {
        store.append(k, "hyprland", &c).unwrap();
    }
    // Newest-first, ties by id DESC.
    let rows = store.recent_activity_for_project(A_ID, 10).unwrap();
    assert_eq!(rows.len(), 3);
    assert!(rows[0].id > rows[1].id);
    assert_eq!(rows[0].observed_at_ms, 2000);
    assert_eq!(rows[1].observed_at_ms, 2000);
    assert_eq!(rows[2].observed_at_ms, 1000);
    // Limit truncates newest-first.
    let lim = store.recent_activity_for_project(A_ID, 2).unwrap();
    assert_eq!(lim.len(), 2);
    assert_eq!(lim[0].id, rows[0].id);
    // Last activity.
    let last = store.last_activity_for_project(A_ID).unwrap().unwrap();
    assert_eq!(last.id, rows[0].id);
    assert!(store
        .last_activity_for_project(UNKNOWN_ID)
        .unwrap()
        .is_none());
    // Range + project (inclusive start, exclusive end, ASC).
    let range = store
        .activity_in_range_for_project(A_ID, 1000, 2000, 10)
        .unwrap();
    assert_eq!(range.len(), 1);
    assert_eq!(range[0].observed_at_ms, 1000);
    let range2 = store
        .activity_in_range_for_project(A_ID, 0, 5000, 1)
        .unwrap();
    assert_eq!(range2.len(), 1, "limit applies to range+project");
    // Validation.
    assert!(store.recent_activity_for_project("not-a-uuid", 10).is_err());
    assert!(store.recent_activity_for_project(A_ID, 0).is_err());
    assert!(store
        .recent_activity_for_project(A_ID, 1001)
        .is_err());
    assert!(store
        .activity_in_range_for_project(A_ID, 2000, 1000, 10)
        .is_err());
    assert!(store
        .activity_in_range_for_project(A_ID, -1, 10, 10)
        .is_err());
    assert!(store.last_activity_for_project("").is_err());
    // UUID normalization: uppercase queries match lowercase storage.
    let upper = A_ID.to_uppercase();
    assert_eq!(
        store
            .recent_activity_for_project(&upper, 10)
            .unwrap()
            .len(),
        3
    );
    assert!(normalize_project_id(&upper).unwrap() == A_ID);
}

#[test]
fn resources_dedup_ignores_branch_title() {
    let store = ActivityStore::open_in_memory().unwrap();
    // Same file, different branch/title -> one entry (latest wins).
    let r1 = res_file("/tmp/a.md", "/tmp", Some("main"), Some("T1"));
    let r2 = res_file("/tmp/a.md", "/tmp", Some("feature"), Some("T2"));
    let c1 = ctx_with(1000, "0x1", "t", Some(r1), Some(proj(A_ID, "A", "file")));
    let c2 = ctx_with(2000, "0x1", "t", Some(r2.clone()), Some(proj(A_ID, "A", "file")));
    // Different file.
    let c3 = ctx_with(
        1500,
        "0x2",
        "t",
        Some(res_file("/tmp/b.md", "/tmp", Some("main"), None)),
        Some(proj(A_ID, "A", "file")),
    );
    // Same URL, different title -> one entry.
    let u1 = ResourceContext::new(
        "zen",
        None,
        None,
        None,
        None,
        Some("https://a.example/"),
        None,
        Some("Title One"),
    );
    let u2 = ResourceContext::new(
        "zen",
        None,
        None,
        None,
        None,
        Some("https://a.example/"),
        None,
        Some("Title Two"),
    );
    let c4 = ctx_with(2500, "0x3", "t", Some(u1), Some(proj(A_ID, "A", "cwd")));
    let c5 = ctx_with(2600, "0x3", "t", Some(u2), Some(proj(A_ID, "A", "cwd")));
    // Cwd-only.
    let c6 = ctx_with(
        2700,
        "0x4",
        "t",
        Some(ResourceContext::new(
            "kitty",
            None,
            Some("/tmp/work"),
            None,
            None,
            None,
            None,
            None,
        )),
        Some(proj(A_ID, "A", "cwd")),
    );
    // No resource: skipped.
    let c7 = ctx_with(2800, "0x5", "t", None, Some(proj(A_ID, "A", "cwd")));
    // Other project: excluded.
    let c8 = ctx_with(
        2900,
        "0x6",
        "t",
        Some(res_file("/tmp/a.md", "/tmp", Some("main"), None)),
        Some(proj(B_ID, "B", "file")),
    );
    for (k, c) in [
        ("focus", c1),
        ("context", c2),
        ("focus", c3),
        ("focus", c4),
        ("context", c5),
        ("focus", c6),
        ("focus", c7),
        ("focus", c8),
    ] {
        store.append(k, "hyprland", &c).unwrap();
    }
    let items = store
        .recently_used_resources_for_project(A_ID, 20)
        .unwrap();
    // Priority keys: f:/tmp/a.md, f:/tmp/b.md, u:https://.., c:/tmp/work = 4.
    assert_eq!(items.len(), 4, "got {items:?}");
    // Newest-first by latest observation.
    assert_eq!(items[0].observed_at_ms, 2700);
    assert_eq!(items[0].resource.cwd.as_deref(), Some("/tmp/work"));
    assert_eq!(items[1].observed_at_ms, 2600);
    assert_eq!(
        items[1].resource.url.as_deref(),
        Some("https://a.example/")
    );
    // Branch-change file entry keeps the LATEST branch/title but counts once.
    let file_entry = items.iter().find(|e| e.resource.file.as_deref() == Some("/tmp/a.md")).unwrap();
    assert_eq!(file_entry.observed_at_ms, 2000);
    assert_eq!(file_entry.resource.git_branch.as_deref(), Some("feature"));
    // Limit truncates output (newest first).
    let lim = store.recently_used_resources_for_project(A_ID, 2).unwrap();
    assert_eq!(lim.len(), 2);
    assert_eq!(lim[0].observed_at_ms, 2700);
    // Unknown project: empty.
    assert!(store
        .recently_used_resources_for_project(UNKNOWN_ID, 10)
        .unwrap()
        .is_empty());
    // Validation.
    assert!(store.recently_used_resources_for_project("bad", 10).is_err());
    assert!(store.recently_used_resources_for_project(A_ID, 0).is_err());
}

#[test]
fn resources_priority_file_wins_over_cwd_and_early_stop() {
    let store = ActivityStore::open_in_memory().unwrap();
    // Same file with different cwd/branch: one entry, latest metadata wins.
    let old = ResourceContext::new(
        "neovim",
        Some("/tmp/a.md"),
        Some("/tmp/old"),
        None,
        Some("main"),
        None,
        None,
        Some("Old title"),
    );
    let new = ResourceContext::new(
        "neovim",
        Some("/tmp/a.md"),
        Some("/tmp/new"),
        None,
        Some("feature"),
        None,
        None,
        Some("New title"),
    );
    store
        .append(
            "focus",
            "hyprland",
            &ctx_with(1000, "0x1", "t", Some(old), Some(proj(A_ID, "A", "file"))),
        )
        .unwrap();
    store
        .append(
            "context",
            "hyprland",
            &ctx_with(2000, "0x1", "t", Some(new.clone()), Some(proj(A_ID, "A", "file"))),
        )
        .unwrap();
    let items = store
        .recently_used_resources_for_project(A_ID, 10)
        .unwrap();
    assert_eq!(items.len(), 1, "same file+cwd change must not duplicate: {items:?}");
    assert_eq!(items[0].observed_at_ms, 2000);
    assert_eq!(items[0].resource.file.as_deref(), Some("/tmp/a.md"));
    // Latest metadata carried (cwd/branch/title from newest row).
    assert_eq!(items[0].resource.cwd.as_deref(), Some("/tmp/new"));
    assert_eq!(items[0].resource.git_branch.as_deref(), Some("feature"));
    assert_eq!(items[0].resource.title.as_deref(), Some("New title"));

    // Early stop: enough distinct newest rows satisfy a small limit without
    // needing the older tail. Newest distinct files first, then a long tail
    // of duplicates of the oldest key.
    let store2 = ActivityStore::open_in_memory().unwrap();
    for (i, ts) in [5000, 4000, 3000].iter().enumerate() {
        let r = ResourceContext::new(
            "neovim",
            Some(&format!("/tmp/new{i}.md")),
            Some("/tmp"),
            None,
            None,
            None,
            None,
            None,
        );
        store2
            .append(
                "focus",
                "hyprland",
                &ctx_with(*ts, &format!("0x{i}"), "t", Some(r), Some(proj(A_ID, "A", "file"))),
            )
            .unwrap();
    }
    // Long older tail all mapping to one already-seen key.
    for ts in [2000, 1900, 1800, 1700, 1600] {
        let r = ResourceContext::new(
            "neovim",
            Some("/tmp/new0.md"),
            Some("/tmp"),
            None,
            Some("main"),
            None,
            None,
            None,
        );
        store2
            .append(
                "context",
                "hyprland",
                &ctx_with(ts, "0x9", "t", Some(r), Some(proj(A_ID, "A", "file"))),
            )
            .unwrap();
    }
    let lim = store2
        .recently_used_resources_for_project(A_ID, 2)
        .unwrap();
    assert_eq!(lim.len(), 2);
    // Newest-first distinct keys; streaming stops once 2 are found.
    assert_eq!(lim[0].observed_at_ms, 5000);
    assert_eq!(lim[0].resource.file.as_deref(), Some("/tmp/new0.md"));
    assert_eq!(lim[1].observed_at_ms, 4000);
    assert_eq!(lim[1].resource.file.as_deref(), Some("/tmp/new1.md"));
}

#[test]
fn old_db_readonly_unchanged_and_compatible() {
    let dir = tmpdir("readonly-v1");
    let db = dir.join("activity.db");
    let a_ctx = ctx_with(
        1000,
        "0x1",
        "a",
        Some(ResourceContext::new(
            "neovim",
            Some("/tmp/a.md"),
            Some("/tmp"),
            None,
            None,
            None,
            None,
            None,
        )),
        Some(proj(A_ID, "Alpha", "file")),
    );
    create_v1_db(&db, vec![(1000, "focus", "hyprland", a_ctx.clone())]);
    // Ensure WAL sidecars from creation are checkpointed away so byte
    // comparison is stable (raw connection may leave journal mode default).
    {
        let conn = rusqlite::Connection::open(&db).unwrap();
        let _ = conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);");
    }
    let before = std::fs::read(&db).unwrap();
    let ro = ActivityStore::open_read_only(&db).unwrap();
    assert_eq!(ro.schema_version().unwrap(), 1);
    // Project queries work via json_extract fallback.
    let rows = ro.recent_activity_for_project(A_ID, 10).unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].snapshot().unwrap(), a_ctx);
    let last = ro.last_activity_for_project(A_ID).unwrap().unwrap();
    assert_eq!(last.id, rows[0].id);
    let res = ro.recently_used_resources_for_project(A_ID, 10).unwrap();
    assert_eq!(res.len(), 1);
    assert_eq!(res[0].resource.file.as_deref(), Some("/tmp/a.md"));
    // Writes through read-only handle fail.
    assert!(ro.append("focus", "hyprland", &a_ctx).is_err());
    let after = std::fs::read(&db).unwrap();
    assert_eq!(before, after, "v1 read-only must not mutate");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn future_and_foreign_rejected_unchanged_v2() {
    let dir = tmpdir("future-v2");
    let db = dir.join("activity.db");
    {
        let store = ActivityStore::open(&db).unwrap();
        assert_eq!(store.schema_version().unwrap(), 2);
        store
            .append(
                "focus",
                "hyprland",
                &ctx_with(7, "0x1", "t", None, None),
            )
            .unwrap();
    }
    {
        let conn = rusqlite::Connection::open(&db).unwrap();
        conn.execute("UPDATE schema_version SET version = 999", [])
            .unwrap();
    }
    let before = std::fs::read(&db).unwrap();
    assert!(matches!(
        ActivityStore::open(&db),
        Err(qs_agent_orchestrator::desktop_store::StoreError::IncompatibleSchema(_))
    ));
    assert!(matches!(
        ActivityStore::open_read_only(&db),
        Err(qs_agent_orchestrator::desktop_store::StoreError::IncompatibleSchema(_))
    ));
    let after = std::fs::read(&db).unwrap();
    assert_eq!(before, after);
    // Foreign DB.
    let fdb = dir.join("foreign.db");
    {
        let conn = rusqlite::Connection::open(&fdb).unwrap();
        conn.execute_batch("CREATE TABLE other (id INTEGER PRIMARY KEY); INSERT INTO other VALUES (1);")
            .unwrap();
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&fdb, std::fs::Permissions::from_mode(0o600));
    }
    let fbefore = std::fs::read(&fdb).unwrap();
    assert!(ActivityStore::open(&fdb).is_err());
    assert!(ActivityStore::open_read_only(&fdb).is_err());
    assert_eq!(fbefore, std::fs::read(&fdb).unwrap());
    let _ = std::fs::remove_dir_all(&dir);
}

// --- Strict structural validation ---

fn chmod_0600(path: &Path) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
}

fn raw_db(path: &Path, setup: &str) {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).unwrap();
    }
    let _ = std::fs::remove_file(path);
    let conn = rusqlite::Connection::open(path).unwrap();
    conn.execute_batch(setup).unwrap();
    drop(conn);
    chmod_0600(path);
}

fn assert_both_reject_unchanged(path: &Path) {
    use qs_agent_orchestrator::desktop_store::StoreError;
    let before = std::fs::read(path).unwrap();
    match ActivityStore::open(path) {
        Err(StoreError::IncompatibleSchema(_)) => {}
        Err(e) => panic!("writable open must reject IncompatibleSchema, got {e:?}"),
        Ok(_) => panic!("writable open must reject foreign/malformed schema"),
    }
    match ActivityStore::open_read_only(path) {
        Err(StoreError::IncompatibleSchema(_)) => {}
        Err(e) => panic!("read-only open must reject IncompatibleSchema, got {e:?}"),
        Ok(_) => panic!("read-only open must reject foreign/malformed schema"),
    }
    let after = std::fs::read(path).unwrap();
    assert_eq!(before, after, "rejected DB must be byte-unchanged");
}

fn legit_v2_db(path: &Path) {
    let store = ActivityStore::open(path).unwrap();
    assert_eq!(store.schema_version().unwrap(), 2);
    store
        .append("focus", "hyprland", &ctx_with(1, "0x1", "t", None, None))
        .unwrap();
    drop(store);
    // Checkpoint WAL so byte comparison is stable.
    {
        let conn = rusqlite::Connection::open(path).unwrap();
        let _ = conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);");
    }
    chmod_0600(path);
}

#[test]
fn strict_rejects_extra_objects_with_expected_names() {
    // Each subcase starts from a legitimate v2 DB plus exactly one foreign
    // object; both opens must reject IncompatibleSchema with no mutation.
    let cases: Vec<(&str, Box<dyn Fn(&Path)>)> = vec![
        ("extra-table", Box::new(|db: &Path| {
            let c = rusqlite::Connection::open(db).unwrap();
            c.execute_batch("CREATE TABLE evil (id INTEGER PRIMARY KEY);").unwrap();
        })),
        ("extra-view", Box::new(|db: &Path| {
            let c = rusqlite::Connection::open(db).unwrap();
            c.execute_batch("CREATE VIEW v AS SELECT 1 AS x;").unwrap();
        })),
        ("extra-trigger", Box::new(|db: &Path| {
            let c = rusqlite::Connection::open(db).unwrap();
            c.execute_batch(
                "CREATE TRIGGER trg AFTER INSERT ON activity BEGIN SELECT 1; END;",
            )
            .unwrap();
        })),
        ("extra-index", Box::new(|db: &Path| {
            let c = rusqlite::Connection::open(db).unwrap();
            c.execute_batch("CREATE INDEX idx_evil ON activity (observed_at_ms);")
                .unwrap();
        })),
        ("expected-plus-unrelated", Box::new(|db: &Path| {
            // Both expected tables present plus an unrelated table: the old
            // name/version check would have migrated; strict must reject.
            let c = rusqlite::Connection::open(db).unwrap();
            c.execute_batch("CREATE TABLE other (id INTEGER PRIMARY KEY); INSERT INTO other VALUES (1);")
                .unwrap();
        })),
    ];
    for (tag, add) in cases {
        let dir = tmpdir(&format!("strict-extra-{tag}"));
        let db = dir.join("activity.db");
        legit_v2_db(&db);
        add(&db);
        chmod_0600(&db);
        assert_both_reject_unchanged(&db);
        let _ = std::fs::remove_dir_all(&dir);
    }
}

#[test]
fn strict_rejects_malformed_expected_tables() {
    // Each DDL keeps expected names/version but breaks structure.
    let v2_good_cols = "id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at_ms INTEGER NOT NULL,
            kind TEXT NOT NULL,
            source TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            project_id TEXT";
    let cases: Vec<(&str, String)> = vec![
        ("kind-nullable", format!("CREATE TABLE schema_version (version INTEGER PRIMARY KEY); CREATE TABLE activity (id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at_ms INTEGER NOT NULL, kind TEXT, source TEXT NOT NULL, snapshot_json TEXT NOT NULL, project_id TEXT); CREATE INDEX idx_activity_time ON activity (observed_at_ms, id); CREATE INDEX idx_activity_project ON activity (project_id, observed_at_ms, id); INSERT INTO schema_version (version) VALUES (2);")),
        ("kind-wrong-type", format!("CREATE TABLE schema_version (version INTEGER PRIMARY KEY); CREATE TABLE activity (id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at_ms INTEGER NOT NULL, kind INTEGER NOT NULL, source TEXT NOT NULL, snapshot_json TEXT NOT NULL, project_id TEXT); CREATE INDEX idx_activity_time ON activity (observed_at_ms, id); CREATE INDEX idx_activity_project ON activity (project_id, observed_at_ms, id); INSERT INTO schema_version (version) VALUES (2);")),
        ("extra-column", format!("CREATE TABLE schema_version (version INTEGER PRIMARY KEY); CREATE TABLE activity ({v2_good_cols}, extra TEXT); CREATE INDEX idx_activity_time ON activity (observed_at_ms, id); CREATE INDEX idx_activity_project ON activity (project_id, observed_at_ms, id); INSERT INTO schema_version (version) VALUES (2);")),
        ("reordered-columns", format!("CREATE TABLE schema_version (version INTEGER PRIMARY KEY); CREATE TABLE activity (observed_at_ms INTEGER NOT NULL, id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, source TEXT NOT NULL, snapshot_json TEXT NOT NULL, project_id TEXT); CREATE INDEX idx_activity_time ON activity (observed_at_ms, id); CREATE INDEX idx_activity_project ON activity (project_id, observed_at_ms, id); INSERT INTO schema_version (version) VALUES (2);")),
        ("project-id-wrong-type", format!("CREATE TABLE schema_version (version INTEGER PRIMARY KEY); CREATE TABLE activity (id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at_ms INTEGER NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL, snapshot_json TEXT NOT NULL, project_id INTEGER); CREATE INDEX idx_activity_time ON activity (observed_at_ms, id); CREATE INDEX idx_activity_project ON activity (project_id, observed_at_ms, id); INSERT INTO schema_version (version) VALUES (2);")),
        ("project-id-not-null", format!("CREATE TABLE schema_version (version INTEGER PRIMARY KEY); CREATE TABLE activity (id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at_ms INTEGER NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL, snapshot_json TEXT NOT NULL, project_id TEXT NOT NULL); CREATE INDEX idx_activity_time ON activity (observed_at_ms, id); CREATE INDEX idx_activity_project ON activity (project_id, observed_at_ms, id); INSERT INTO schema_version (version) VALUES (2);")),
        ("project-id-default", format!("CREATE TABLE schema_version (version INTEGER PRIMARY KEY); CREATE TABLE activity (id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at_ms INTEGER NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL, snapshot_json TEXT NOT NULL, project_id TEXT DEFAULT 'x'); CREATE INDEX idx_activity_time ON activity (observed_at_ms, id); CREATE INDEX idx_activity_project ON activity (project_id, observed_at_ms, id); INSERT INTO schema_version (version) VALUES (2);")),
        ("schema-version-extra-col", "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, extra TEXT); CREATE TABLE activity (id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at_ms INTEGER NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL, snapshot_json TEXT NOT NULL, project_id TEXT); CREATE INDEX idx_activity_time ON activity (observed_at_ms, id); CREATE INDEX idx_activity_project ON activity (project_id, observed_at_ms, id); INSERT INTO schema_version (version, extra) VALUES (2, NULL);".to_string()),
        ("schema-version-wrong-type", "CREATE TABLE schema_version (version TEXT PRIMARY KEY); CREATE TABLE activity (id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at_ms INTEGER NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL, snapshot_json TEXT NOT NULL, project_id TEXT); CREATE INDEX idx_activity_time ON activity (observed_at_ms, id); CREATE INDEX idx_activity_project ON activity (project_id, observed_at_ms, id); INSERT INTO schema_version (version) VALUES ('2');".to_string()),
    ];
    for (tag, setup) in cases {
        let dir = tmpdir(&format!("strict-malformed-{tag}"));
        let db = dir.join("activity.db");
        raw_db(&db, &setup);
        assert_both_reject_unchanged(&db);
        let _ = std::fs::remove_dir_all(&dir);
    }
}

#[test]
fn strict_project_id_detection_is_structural() {
    // SQL comment mentioning project_id must not fool detection: no real
    // column => legitimate v1, migrates to v2 on writable open.
    {
        let dir = tmpdir("strict-comment");
        let db = dir.join("activity.db");
        raw_db(
            &db,
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
             CREATE TABLE activity (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               observed_at_ms INTEGER NOT NULL,
               kind TEXT NOT NULL,
               source TEXT NOT NULL,
               snapshot_json TEXT NOT NULL -- project_id
             );
             CREATE INDEX idx_activity_time ON activity (observed_at_ms, id);
             INSERT INTO schema_version (version) VALUES (1);",
        );
        // Read-only sees v1 (fallback) and accepts.
        let ro = ActivityStore::open_read_only(&db).unwrap();
        assert_eq!(ro.schema_version().unwrap(), 1);
        drop(ro);
        // Writable migrates (adds the real column) and bumps to v2.
        let store = ActivityStore::open(&db).unwrap();
        assert_eq!(store.schema_version().unwrap(), 2);
        let _ = std::fs::remove_dir_all(&dir);
    }
    // Lookalike identifier without the exact column is malformed, not v2.
    {
        let dir = tmpdir("strict-lookalike");
        let db = dir.join("activity.db");
        raw_db(
            &db,
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
             CREATE TABLE activity (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               observed_at_ms INTEGER NOT NULL,
               kind TEXT NOT NULL,
               source TEXT NOT NULL,
               snapshot_json TEXT NOT NULL,
               project_id_fake TEXT
             );
             CREATE INDEX idx_activity_time ON activity (observed_at_ms, id);
             INSERT INTO schema_version (version) VALUES (2);",
        );
        assert_both_reject_unchanged(&db);
        let _ = std::fs::remove_dir_all(&dir);
    }
    // Wrong-typed project_id column rejects even though the name matches.
    {
        let dir = tmpdir("strict-wrongtype-col");
        let db = dir.join("activity.db");
        raw_db(
            &db,
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
             CREATE TABLE activity (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               observed_at_ms INTEGER NOT NULL,
               kind TEXT NOT NULL,
               source TEXT NOT NULL,
               snapshot_json TEXT NOT NULL,
               project_id INTEGER
             );
             CREATE INDEX idx_activity_time ON activity (observed_at_ms, id);
             CREATE INDEX idx_activity_project ON activity (project_id, observed_at_ms, id);
             INSERT INTO schema_version (version) VALUES (2);",
        );
        assert_both_reject_unchanged(&db);
        let _ = std::fs::remove_dir_all(&dir);
    }
}

#[test]
fn strict_rejects_same_name_wrong_index_and_allows_missing() {
    // Same name, wrong columns => reject unchanged (both opens).
    for (tag, idx_sql) in [
        (
            "project-wrong-cols",
            "CREATE INDEX idx_activity_project ON activity (observed_at_ms, id);",
        ),
        (
            "project-wrong-order",
            "CREATE INDEX idx_activity_project ON activity (observed_at_ms, project_id, id);",
        ),
        (
            "time-wrong-cols",
            "CREATE INDEX idx_activity_time ON activity (id);",
        ),
    ] {
        let dir = tmpdir(&format!("strict-idx-{tag}"));
        let db = dir.join("activity.db");
        legit_v2_db(&db);
        {
            let c = rusqlite::Connection::open(&db).unwrap();
            if tag.starts_with("project") {
                c.execute_batch("DROP INDEX idx_activity_project;").unwrap();
            } else {
                c.execute_batch("DROP INDEX idx_activity_time;").unwrap();
            }
            c.execute_batch(idx_sql).unwrap();
        }
        chmod_0600(&db);
        assert_both_reject_unchanged(&db);
        let _ = std::fs::remove_dir_all(&dir);
    }
    // Missing project index on an otherwise legitimate v2 DB: read-only
    // accepts (fallback still correct), writable recreates it.
    {
        let dir = tmpdir("strict-idx-missing-ok");
        let db = dir.join("activity.db");
        legit_v2_db(&db);
        {
            let c = rusqlite::Connection::open(&db).unwrap();
            c.execute_batch("DROP INDEX idx_activity_project;").unwrap();
        }
        chmod_0600(&db);
        let ro = ActivityStore::open_read_only(&db).unwrap();
        assert_eq!(ro.schema_version().unwrap(), 2);
        drop(ro);
        let store = ActivityStore::open(&db).unwrap();
        assert_eq!(store.schema_version().unwrap(), 2);
        let c = rusqlite::Connection::open(&db).unwrap();
        let n: i64 = c
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_activity_project'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(n, 1, "writable open must recreate missing project index");
        let _ = std::fs::remove_dir_all(&dir);
    }
}

#[test]
fn strict_rejects_hidden_index_constraints() {
    // UNIQUE column constraint creates a sqlite_autoindex (invisible to the
    // old name-filtered whitelist/table_info) and would make legitimate
    // repeat appends fail. Partial / UNIQUE / DESC / NOCASE variants of the
    // named indexes pass column-order checks alone but break indexed-query
    // assumptions. All must reject on both opens with no mutation.
    let good_v2 = "CREATE TABLE schema_version (version INTEGER PRIMARY KEY); CREATE TABLE activity (id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at_ms INTEGER NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL, snapshot_json TEXT NOT NULL, project_id TEXT);";
    let good_idx = "CREATE INDEX idx_activity_time ON activity (observed_at_ms, id); CREATE INDEX idx_activity_project ON activity (project_id, observed_at_ms, id);";
    let cases: Vec<(&str, String)> = vec![
        ("unique-kind-column", format!("{good_v2} {good_idx} INSERT INTO schema_version (version) VALUES (2);").replace("kind TEXT NOT NULL,", "kind TEXT NOT NULL UNIQUE,")),
        ("unique-named-index", format!("{good_v2} CREATE UNIQUE INDEX idx_activity_time ON activity (observed_at_ms, id); CREATE INDEX idx_activity_project ON activity (project_id, observed_at_ms, id); INSERT INTO schema_version (version) VALUES (2);")),
        ("partial-project-index", format!("{good_v2} CREATE INDEX idx_activity_time ON activity (observed_at_ms, id); CREATE INDEX idx_activity_project ON activity (project_id, observed_at_ms, id) WHERE kind='focus'; INSERT INTO schema_version (version) VALUES (2);")),
        ("desc-project-index", format!("{good_v2} CREATE INDEX idx_activity_time ON activity (observed_at_ms, id); CREATE INDEX idx_activity_project ON activity (project_id, observed_at_ms, id DESC); INSERT INTO schema_version (version) VALUES (2);")),
        ("nocase-time-index", format!("{good_v2} CREATE INDEX idx_activity_time ON activity (observed_at_ms COLLATE NOCASE, id); CREATE INDEX idx_activity_project ON activity (project_id, observed_at_ms, id); INSERT INTO schema_version (version) VALUES (2);")),
    ];
    for (tag, setup) in cases {
        let dir = tmpdir(&format!("strict-hidden-{tag}"));
        let db = dir.join("activity.db");
        raw_db(&db, &setup);
        assert_both_reject_unchanged(&db);
        let _ = std::fs::remove_dir_all(&dir);
    }
    // Sanity: a UNIQUE violation really would break appends, so rejection
    // protects write availability (demonstrate on a raw handle, not ours).
    {
        let dir = tmpdir("strict-hidden-demo");
        let db = dir.join("activity.db");
        raw_db(
            &db,
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY); CREATE TABLE activity (id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at_ms INTEGER NOT NULL, kind TEXT NOT NULL UNIQUE, source TEXT NOT NULL, snapshot_json TEXT NOT NULL, project_id TEXT); CREATE INDEX idx_activity_time ON activity (observed_at_ms, id); CREATE INDEX idx_activity_project ON activity (project_id, observed_at_ms, id); INSERT INTO schema_version (version) VALUES (2);",
        );
        let c = rusqlite::Connection::open(&db).unwrap();
        c.execute(
            "INSERT INTO activity (observed_at_ms, kind, source, snapshot_json) VALUES (1, 'focus', 'hyprland', '{}')",
            [],
        )
        .unwrap();
        assert!(
            c.execute(
                "INSERT INTO activity (observed_at_ms, kind, source, snapshot_json) VALUES (2, 'focus', 'hyprland', '{}')",
                [],
            )
            .is_err(),
            "UNIQUE(kind) must break legitimate repeats"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}

#[test]
fn strict_rejects_check_constraints() {
    // CHECK is invisible to table_info/index_list; SQL-token scan rejects.
    // Quoted/commented CHECK words must not false-positive (covered below by
    // the legit-DB control passing elsewhere).
    let dir = tmpdir("strict-check");
    let db = dir.join("activity.db");
    raw_db(
        &db,
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY); CREATE TABLE activity (id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at_ms INTEGER NOT NULL, kind TEXT NOT NULL CHECK(length(kind) > 0), source TEXT NOT NULL, snapshot_json TEXT NOT NULL, project_id TEXT); CREATE INDEX idx_activity_time ON activity (observed_at_ms, id); CREATE INDEX idx_activity_project ON activity (project_id, observed_at_ms, id); INSERT INTO schema_version (version) VALUES (2);",
    );
    assert_both_reject_unchanged(&db);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn append_normalizes_project_uuid_consistently() {
    let store = ActivityStore::open_in_memory().unwrap();
    // Uppercase caller context normalizes to canonical lowercase and stays
    // queryable via either case.
    let upper = A_ID.to_uppercase();
    let ctx_upper = ctx_with(
        1000,
        "0x1",
        "t",
        Some(ResourceContext::new(
            "neovim",
            Some("/tmp/a.md"),
            Some("/tmp"),
            None,
            None,
            None,
            None,
            None,
        )),
        Some(proj(&upper, "Alpha", "file")),
    );
    let id = store.append("focus", "hyprland", &ctx_upper).unwrap();
    assert!(id > 0);
    let rows = store.recent_activity_for_project(A_ID, 10).unwrap();
    assert_eq!(rows.len(), 1);
    let snap = rows[0].snapshot().unwrap();
    assert_eq!(
        snap.project.as_ref().unwrap().id,
        A_ID,
        "stored snapshot must project lowercase"
    );
    assert_eq!(
        store
            .recent_activity_for_project(&upper, 10)
            .unwrap()
            .len(),
        1
    );
    // Invalid (non-UUID) project ids fail closed with nothing persisted.
    let bad = ctx_with(
        2000,
        "0x2",
        "t",
        Some(ResourceContext::new(
            "neovim",
            Some("/tmp/b.md"),
            Some("/tmp"),
            None,
            None,
            None,
            None,
            None,
        )),
        Some(proj("not-a-uuid", "Bad", "file")),
    );
    assert!(store.append("focus", "hyprland", &bad).is_err());
    assert_eq!(store.recent_activity(10).unwrap().len(), 1);
}

// --- CLI ---

fn run_bin(args: &[&str], envs: &[(&str, &str)], clear: &[&str]) -> (i32, String, String) {
    let bin = bin_path();
    let mut cmd = Command::new(&bin);
    cmd.args(args);
    for (k, v) in envs {
        cmd.env(k, v);
    }
    for k in clear {
        cmd.env_remove(k);
    }
    let out = cmd.output().expect("spawn qs-desktop-context");
    let code = out.status.code().unwrap_or(-1);
    (
        code,
        String::from_utf8_lossy(&out.stdout).to_string(),
        String::from_utf8_lossy(&out.stderr).to_string(),
    )
}

#[test]
fn cli_missing_db_validated_first() {
    let dir = tmpdir("cli-missing");
    let missing = dir.join("no-such-dir").join("activity.db");
    let m = missing.to_string_lossy().to_string();
    // Valid queries on missing DB: empty shapes, exit 0.
    let (code, stdout, _) = run_bin(
        &["--db", &m, "history", "--project", A_ID, "--limit", "5"],
        &[],
        &[],
    );
    assert_eq!(code, 0, "missing DB history must exit 0");
    assert_eq!(serde_json::from_str::<serde_json::Value>(&stdout).unwrap(), serde_json::json!([]));
    let (code, stdout, _) = run_bin(&["--db", &m, "last-activity", "--project", A_ID], &[], &[]);
    assert_eq!(code, 0);
    assert_eq!(stdout.trim(), "null");
    let (code, stdout, _) = run_bin(&["--db", &m, "resources", "--project", A_ID], &[], &[]);
    assert_eq!(code, 0);
    assert_eq!(serde_json::from_str::<serde_json::Value>(&stdout).unwrap(), serde_json::json!([]));
    // Invalid args must fail even when DB is missing (validate first).
    let (code, _, _) = run_bin(&["--db", &m, "history", "--project", "bad-id"], &[], &[]);
    assert_ne!(code, 0, "bad UUID must fail before missing-DB fast path");
    let (code, _, _) = run_bin(
        &["--db", &m, "history", "--project", A_ID, "--limit", "0"],
        &[],
        &[],
    );
    assert_ne!(code, 0);
    let (code, _, _) = run_bin(&["--db", &m, "last-activity", "--project", "bad"], &[], &[]);
    assert_ne!(code, 0);
    let (code, _, _) = run_bin(&["--db", &m, "resources", "--project", A_ID, "--limit", "0"], &[], &[]);
    assert_ne!(code, 0);
    // Missing required --project.
    let (code, _, _) = run_bin(&["--db", &m, "last-activity"], &[], &[]);
    assert_eq!(code, 2);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn cli_history_project_and_resources_shapes() {
    let dir = tmpdir("cli-shapes");
    let db = dir.join("activity.db");
    let m = db.to_string_lossy().to_string();
    let store = ActivityStore::open(&db).unwrap();
    let a1 = ctx_with(
        1000,
        "0x1",
        "a",
        Some(ResourceContext::new(
            "neovim",
            Some("/tmp/a.md"),
            Some("/tmp"),
            None,
            Some("main"),
            None,
            None,
            None,
        )),
        Some(proj(A_ID, "Alpha", "file")),
    );
    let a2 = ctx_with(
        2000,
        "0x1",
        "a",
        Some(ResourceContext::new(
            "neovim",
            Some("/tmp/a.md"),
            Some("/tmp"),
            None,
            Some("feature"),
            None,
            None,
            None,
        )),
        Some(proj(A_ID, "Alpha", "file")),
    );
    let b = ctx_with(1500, "0x9", "b", None, Some(proj(B_ID, "B", "cwd")));
    store.append("focus", "hyprland", &a1).unwrap();
    store.append("focus", "hyprland", &b).unwrap();
    store.append("context", "hyprland", &a2).unwrap();
    drop(store);
    // history --project: existing row shape.
    let (code, stdout, _) = run_bin(&["--db", &m, "history", "--project", A_ID, "--limit", "10"], &[], &[]);
    assert_eq!(code, 0);
    let v: serde_json::Value = serde_json::from_str(&stdout).unwrap();
    let arr = v.as_array().unwrap();
    assert_eq!(arr.len(), 2);
    for row in arr {
        assert!(row.get("id").is_some());
        assert!(row.get("observed_at_ms").is_some());
        assert!(row.get("kind").is_some());
        assert!(row.get("source").is_some());
        assert!(row.get("snapshot").is_some());
    }
    assert_eq!(arr[0]["observed_at_ms"], 2000);
    // history --project with range (supported combination).
    let (code, stdout, _) = run_bin(
        &["--db", &m, "history", "--project", A_ID, "--from", "0", "--to", "1500"],
        &[],
        &[],
    );
    assert_eq!(code, 0);
    let v: serde_json::Value = serde_json::from_str(&stdout).unwrap();
    assert_eq!(v.as_array().unwrap().len(), 1);
    // last-activity.
    let (code, stdout, _) = run_bin(&["--db", &m, "last-activity", "--project", A_ID], &[], &[]);
    assert_eq!(code, 0);
    let v: serde_json::Value = serde_json::from_str(&stdout).unwrap();
    assert_eq!(v["observed_at_ms"], 2000);
    let (code, stdout, _) = run_bin(&["--db", &m, "last-activity", "--project", UNKNOWN_ID], &[], &[]);
    assert_eq!(code, 0);
    assert_eq!(stdout.trim(), "null");
    // resources: dedup branch change => one entry for /tmp/a.md.
    let (code, stdout, _) = run_bin(&["--db", &m, "resources", "--project", A_ID], &[], &[]);
    assert_eq!(code, 0);
    let v: serde_json::Value = serde_json::from_str(&stdout).unwrap();
    let arr = v.as_array().unwrap();
    assert_eq!(arr.len(), 1, "branch change must not duplicate: {v}");
    assert_eq!(arr[0]["resource"]["file"], "/tmp/a.md");
    assert!(arr[0].get("observed_at_ms").is_some());
    assert!(arr[0].get("activity_id").is_some());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn cli_current_unavailable_without_compositor() {
    // No sockets: current prints an unavailable DesktopContext, no DB, no HOME.
    let dir = tmpdir("current-unavail");
    let empty_rt = dir.join("empty-rt");
    std::fs::create_dir_all(&empty_rt).unwrap();
    let rt = empty_rt.to_string_lossy().to_string();
    let (code, stdout, _) = run_bin(
        &["current"],
        &[("XDG_RUNTIME_DIR", rt.as_str())],
        &["HYPRLAND_INSTANCE_SIGNATURE"],
    );
    assert_eq!(code, 0);
    let v: serde_json::Value = serde_json::from_str(&stdout).unwrap();
    assert_eq!(v["available"], false);
    assert!(v.get("project").is_none() || v["project"].is_null());
    let (code, stdout, _) = run_bin(
        &["current-project"],
        &[("XDG_RUNTIME_DIR", rt.as_str())],
        &["HYPRLAND_INSTANCE_SIGNATURE"],
    );
    assert_eq!(code, 0);
    assert_eq!(stdout.trim(), "null");
    let _ = std::fs::remove_dir_all(&dir);
}

/// Fake Hyprland request socket: serves canned window/workspace JSON per
/// connection; `switch_after` flips from (win_a, ws_a) to (win_b, ws_b) after
/// that many connections (simulates a focus change landing mid-enrichment).
/// Holds both .socket.sock and .socket2.sock so discovery succeeds with an
/// explicit signature.
struct FakeHypr {
    _req: std::thread::JoinHandle<()>,
    _ev: std::os::unix::net::UnixListener,
    sig: String,
}

fn spawn_fake_hypr(
    base: &Path,
    win_a: &str,
    ws_a: &str,
    win_b: &str,
    ws_b: &str,
    switch_after: usize,
) -> FakeHypr {
    use std::io::{Read, Write};
    use std::os::unix::net::UnixListener;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    static FAKE_SIG_CTR: std::sync::atomic::AtomicU64 =
        std::sync::atomic::AtomicU64::new(0);
    let n = FAKE_SIG_CTR.fetch_add(1, Ordering::SeqCst);
    let sig = format!("fakesig{}-{n}", std::process::id());
    let dir = base.join("hypr").join(&sig);
    std::fs::create_dir_all(&dir).unwrap();
    let req_path = dir.join(".socket.sock");
    let ev_path = dir.join(".socket2.sock");
    let _ = std::fs::remove_file(&req_path);
    let _ = std::fs::remove_file(&ev_path);
    let ev = UnixListener::bind(&ev_path).unwrap();
    let listener = UnixListener::bind(&req_path).unwrap();
    let (wa, sa, wb, sb) = (
        win_a.to_string(),
        ws_a.to_string(),
        win_b.to_string(),
        ws_b.to_string(),
    );
    let count = Arc::new(AtomicUsize::new(0));
    let c2 = Arc::clone(&count);
    let h = std::thread::spawn(move || {
        for conn in listener.incoming() {
            let mut stream = match conn {
                Ok(s) => s,
                Err(_) => return,
            };
            let _ = stream.set_read_timeout(Some(std::time::Duration::from_secs(2)));
            let mut buf = vec![0u8; 4096];
            let n = match stream.read(&mut buf) {
                Ok(n) => n,
                Err(_) => continue,
            };
            if n == 0 {
                continue;
            }
            let idx = c2.fetch_add(1, Ordering::SeqCst);
            let (win, ws) = if idx < switch_after {
                (&wa, &sa)
            } else {
                (&wb, &sb)
            };
            let req = String::from_utf8_lossy(&buf[..n]).to_string();
            let reply = if req.contains("activewindow") {
                win.clone()
            } else if req.contains("activeworkspace") {
                ws.clone()
            } else {
                "{}".to_string()
            };
            let _ = stream.write_all(reply.as_bytes());
        }
    });
    FakeHypr {
        _req: h,
        _ev: ev,
        sig,
    }
}

#[test]
fn cli_current_race_rejects_stale_on_recheck_switch() {
    // Deterministic request-count boundary: `current` performs exactly two
    // `fetch_snapshot` calls (base + post-enrichment recheck) of 4 socket
    // requests each (`j/activewindow`, `j/activeworkspace` x2). With
    // `switch_after == 4` the base snapshot stably sees A and the recheck
    // stably sees B, so the CLI must report unavailable (never stale A).
    // Explicit-signature discovery performs no probe connections, so the
    // boundary is exact and no sleeps are needed.
    let dir = tmpdir("current-race");
    let rt_base = dir.join("rt");
    std::fs::create_dir_all(&rt_base).unwrap();
    let win_a = r#"{"address":"0x1","class":"kitty","title":"stable","workspace":{"id":1,"name":"1"}}"#;
    let ws_a = r#"{"id":1,"name":"1"}"#;
    let win_b = r#"{"address":"0x2","class":"kitty","title":"moved","workspace":{"id":1,"name":"1"}}"#;
    let fake = spawn_fake_hypr(&rt_base, win_a, ws_a, win_b, ws_a, 4);
    let rt = rt_base.to_string_lossy().to_string();
    let sig = fake.sig.clone();
    // Empty registry (missing file => no projects, no Python spawn).
    let missing_reg = dir.join("missing-projects.toml").to_string_lossy().to_string();
    let (code, stdout, stderr) = run_bin(
        &["current"],
        &[
            ("XDG_RUNTIME_DIR", rt.as_str()),
            ("HYPRLAND_INSTANCE_SIGNATURE", sig.as_str()),
            ("QUICKSHELL_PROJECTS_FILE", missing_reg.as_str()),
        ],
        &["QS_KITTY_SOCKET", "KITTY_LISTEN_ON", "QS_NVIM_CONTEXT_DIR", "QS_ZEN_CONTEXT_FILE"],
    );
    assert_eq!(code, 0);
    let v: serde_json::Value = serde_json::from_str(&stdout).unwrap();
    assert_eq!(
        v["available"], false,
        "recheck switch must reject to unavailable, got {v} stderr={stderr}"
    );
    assert!(
        v.get("project").is_none() || v["project"].is_null(),
        "unavailable must carry no project, got {v}"
    );
    drop(fake);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn cli_current_project_positive_neovim() {
    // End-to-end core -> CLI positive path: fake Hypr focused standalone
    // Neovim (known PID) + valid private fresh Neovim publisher record
    // (Phase 2 `{pid,file,cwd,updated_at_ms}` shape) + real `projects.toml`
    // via the canonical `scripts/projects.py create` helper mapping a temp
    // folder. `current` must report the enriched resource and the resolved
    // project (`matched_by == "file"`); `current-project` must report the
    // same object. `current` never creates a DB.
    use std::os::unix::fs::PermissionsExt;

    let dir = tmpdir("current-positive");
    let work = dir.join("work");
    std::fs::create_dir_all(&work).unwrap();
    let note = work.join("note.md");
    std::fs::write(&note, "hello").unwrap();

    // Canonical registry via the real helper (not hand-written TOML).
    let reg = dir.join("projects.toml");
    let helper = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("scripts")
        .join("projects.py");
    assert!(helper.is_file(), "scripts/projects.py must exist");
    let create_out = std::process::Command::new("python3")
        .arg(&helper)
        .arg("--projects-file")
        .arg(&reg)
        .arg("create")
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .and_then(|mut child| {
            use std::io::Write;
            let body = serde_json::json!({
                "name": "Work",
                "local_folder": work.to_string_lossy(),
            })
            .to_string();
            child
                .stdin
                .as_mut()
                .expect("piped stdin")
                .write_all(body.as_bytes())?;
            child.wait_with_output()
        })
        .expect("scripts/projects.py create must run");
    assert!(
        create_out.status.success(),
        "helper create failed: {}",
        String::from_utf8_lossy(&create_out.stderr)
    );
    let created: serde_json::Value =
        serde_json::from_str(&String::from_utf8_lossy(&create_out.stdout))
            .expect("helper create prints JSON");
    let want_id = created
        .get("project")
        .and_then(|p| p.get("id"))
        .and_then(|x| x.as_str())
        .expect("helper returns project.id")
        .to_string();
    let want_name = created
        .get("project")
        .and_then(|p| p.get("name"))
        .and_then(|x| x.as_str())
        .expect("helper returns project.name")
        .to_string();

    // Fresh private Neovim publisher record for a known PID.
    let pid = std::process::id();
    assert!(pid > 0 && pid <= u32::MAX, "test pid must fit u32");
    let nvim_dir = dir.join("nvim-context");
    std::fs::create_dir_all(&nvim_dir).unwrap();
    std::fs::set_permissions(&nvim_dir, std::fs::Permissions::from_mode(0o700)).unwrap();
    let now_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0);
    assert!(now_ms > 0, "wall clock must be available");
    let rec = nvim_dir.join(format!("{pid}.json"));
    std::fs::write(
        &rec,
        serde_json::json!({
            "pid": pid,
            "file": note.to_string_lossy(),
            "cwd": work.to_string_lossy(),
            "updated_at_ms": now_ms,
        })
        .to_string(),
    )
    .unwrap();
    std::fs::set_permissions(&rec, std::fs::Permissions::from_mode(0o600)).unwrap();

    // Fake compositor focused on that exact Neovim client (standalone class,
    // numeric pid so the direct path binds without kitty peer logic).
    let rt_base = dir.join("rt");
    std::fs::create_dir_all(&rt_base).unwrap();
    let win = format!(
        r#"{{"address":"0xabc","class":"neovim","title":"note.md","pid":{pid},"workspace":{{"id":1,"name":"1"}}}}"#,
    );
    let ws = r#"{"id":1,"name":"1"}"#;
    let fake = spawn_fake_hypr(&rt_base, &win, ws, &win, ws, usize::MAX);
    let rt = rt_base.to_string_lossy().to_string();
    let sig = fake.sig.clone();
    let reg_s = reg.to_string_lossy().to_string();
    let nvim_s = nvim_dir.to_string_lossy().to_string();
    // Point state dir at scratch and HOME at a nonexistent dir: `current`
    // must not require DB initialization or a HOME.
    let state = dir.join("state");
    let nohome = dir.join("nohome-missing");

    let state_s = state.to_string_lossy().to_string();
    let nohome_s = nohome.to_string_lossy().to_string();
    let base_env = [
        ("XDG_RUNTIME_DIR", rt.as_str()),
        ("HYPRLAND_INSTANCE_SIGNATURE", sig.as_str()),
        ("QUICKSHELL_PROJECTS_FILE", reg_s.as_str()),
        ("QS_NVIM_CONTEXT_DIR", nvim_s.as_str()),
        ("XDG_STATE_HOME", state_s.as_str()),
        ("HOME", nohome_s.as_str()),
    ];
    // Borrow dance: run_bin takes &[(&str,&str)].
    let env_refs: Vec<(&str, &str)> = base_env.iter().map(|(k, v)| (*k, *v)).collect();
    let (code, stdout, stderr) = run_bin(
        &["current"],
        &env_refs,
        &["QS_KITTY_SOCKET", "KITTY_LISTEN_ON", "QS_ZEN_CONTEXT_FILE"],
    );
    assert_eq!(code, 0, "positive current must succeed: {stderr}");
    let v: serde_json::Value = serde_json::from_str(&stdout).unwrap();
    assert_eq!(v["available"], true, "got {v}");
    assert_eq!(v["focused_window"]["id"], "0xabc");
    assert_eq!(v["resource"]["adapter"], "neovim");
    assert_eq!(v["resource"]["file"], note.to_string_lossy().as_ref());
    assert_eq!(v["project"]["id"], want_id.as_str());
    assert_eq!(v["project"]["name"], want_name.as_str());
    assert_eq!(v["project"]["matched_by"], "file");

    let (code, stdout, stderr) = run_bin(&["current-project"], &env_refs, &[
        "QS_KITTY_SOCKET",
        "KITTY_LISTEN_ON",
        "QS_ZEN_CONTEXT_FILE",
    ]);
    assert_eq!(code, 0, "positive current-project must succeed: {stderr}");
    let p: serde_json::Value = serde_json::from_str(&stdout).unwrap();
    assert_eq!(p["id"], want_id.as_str());
    assert_eq!(p["name"], want_name.as_str());
    assert_eq!(p["matched_by"], "file");

    // `current` creates no DB anywhere near the scratch state dir.
    assert!(
        !state.join("quickshell/desktop-activity/activity.db").exists()
            && !dir.join("activity.db").exists(),
        "current must not create a DB"
    );
    drop(fake);
    let _ = std::fs::remove_dir_all(&dir);
}
