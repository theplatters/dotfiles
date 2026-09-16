//! Phase 5 session-centric searchable history tests.
//!
//! Covers migration preservation/backfill + rollback, append atomic FTS,
//! time/project/application/resource/device/free-text/combined/limit/order,
//! no matches, session detail with/without events, boundaries,
//! escaping/malformed args, compact CLI output, strict schema rejection,
//! and FTS5 availability.

use qs_agent_orchestrator::desktop_context::{
    DesktopContext, FocusedWindow, ProjectContext, ResourceContext, Source, Workspace,
};
use qs_agent_orchestrator::desktop_session::SessionConfig;
use qs_agent_orchestrator::desktop_store::{
    ActivityStore, SessionSearchQuery, StoreError, MAX_QUERY_LIMIT,
};
use qs_agent_orchestrator::{acquire_lock, lock_path_for};
use std::path::{Path, PathBuf};
use std::process::Command;

const A_ID: &str = "11111111-1111-1111-1111-111111111111";
const B_ID: &str = "22222222-2222-2222-2222-222222222222";

fn tmpdir(tag: &str) -> PathBuf {
    let p = std::env::temp_dir().join(format!(
        "qs-search-{tag}-{}-{}",
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

fn run_bin(args: &[&str]) -> (i32, String, String) {
    let bin = bin_path();
    let out = Command::new(&bin)
        .args(args)
        .output()
        .expect("spawn qs-desktop-context");
    (
        out.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&out.stdout).to_string(),
        String::from_utf8_lossy(&out.stderr).to_string(),
    )
}

fn proj(id: &str, name: &str) -> ProjectContext {
    ProjectContext::new(id, name, "file")
}

fn ctx_full(
    ts: i64,
    win: &str,
    app: &str,
    title: &str,
    ws: &str,
    res: Option<ResourceContext>,
    p: Option<ProjectContext>,
) -> DesktopContext {
    let base = DesktopContext::available(
        Source::Hyprland,
        Some(FocusedWindow::new(win, app, title)),
        Some(Workspace::new(ws, ws)),
        ts,
    );
    base.with_resource(res).with_project(p)
}

fn small_cfg() -> SessionConfig {
    SessionConfig::new(1000, 200).unwrap()
}

fn mem_small() -> ActivityStore {
    ActivityStore::open_in_memory_with_config(small_cfg()).unwrap()
}

fn checkpoint(path: &Path) {
    let c = rusqlite::Connection::open(path).unwrap();
    let _ = c.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);");
}

#[cfg(unix)]
fn chmod_0600(p: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = std::fs::set_permissions(p, std::fs::Permissions::from_mode(0o600));
}

fn search_q(limit: i64) -> SessionSearchQuery {
    SessionSearchQuery {
        limit,
        ..Default::default()
    }
}

// ---------- FTS5 availability ----------

#[test]
fn bundled_rusqlite_has_fts5() {
    let store = ActivityStore::open_in_memory().unwrap();
    // FTS5 table exists on fresh v4.
    let n: i64 = store
        .schema_version()
        .map(|_| {
            // Use a raw in-memory check via the store's own FTS: insert then match.
            1
        })
        .unwrap();
    assert_eq!(n, 1);
    assert_eq!(store.schema_version().unwrap(), 4);
    // Direct compile-option probe on a raw connection (bundled SQLite).
    let c = rusqlite::Connection::open_in_memory().unwrap();
    let fts5: bool = c
        .query_row(
            "SELECT COUNT(*) FROM pragma_compile_options WHERE compile_options LIKE '%FTS5%'",
            [],
            |r| r.get::<_, i64>(0),
        )
        .map(|n| n > 0)
        .unwrap_or(false);
    // Fallback: creating an FTS5 table must succeed (proves FTS5 linked).
    c.execute_batch("CREATE VIRTUAL TABLE t USING fts5(x)")
        .expect("bundled rusqlite must support FTS5");
    let _ = fts5;
}

// ---------- append atomic FTS ----------

#[test]
fn append_maintains_fts_one_to_one() {
    let store = mem_small();
    for (i, ts) in [0, 100, 200].iter().enumerate() {
        store
            .append(
                "focus",
                "hyprland",
                &ctx_full(
                    *ts,
                    &format!("0x{i}"),
                    "kitty",
                    "t",
                    "1",
                    None,
                    Some(proj(A_ID, "Alpha")),
                ),
            )
            .unwrap();
    }
    // One-to-one via public search: every session has resources bound, and
    // raw counts match through session events.
    let hits = store.search_sessions(&search_q(10)).unwrap();
    assert_eq!(hits.len(), 1);
    assert_eq!(hits[0].session.event_count, 3);
    // Invalid append leaves FTS untouched (still 1 session, 3 events).
    let bad = ctx_full(
        300,
        "0x9",
        "k",
        "b",
        "1",
        None,
        Some(ProjectContext::new("not-a-uuid", "B", "file")),
    );
    assert!(store.append("focus", "hyprland", &bad).is_err());
    let hits2 = store.search_sessions(&search_q(10)).unwrap();
    assert_eq!(hits2.len(), 1);
    assert_eq!(hits2[0].session.event_count, 3);
}

// ---------- time range ----------

fn three_session_store() -> ActivityStore {
    let store = mem_small();
    // Session 1: A at 0..100. Session 2: B at 5000. Session 3: A at 6000.
    store
        .append(
            "focus",
            "hyprland",
            &ctx_full(0, "0x1", "kitty", "a1", "1", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx_full(5000, "0x2", "kitty", "b", "1", None, Some(proj(B_ID, "B"))),
        )
        .unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx_full(6000, "0x3", "kitty", "a2", "1", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    store
}

#[test]
fn search_time_range_event_level() {
    let store = three_session_store();
    // Range [4000,5500) matches only B (event at 5000 in range).
    let q = SessionSearchQuery {
        start_ms: Some(4000),
        end_ms: Some(5500),
        limit: 10,
        ..Default::default()
    };
    let hits = store.search_sessions(&q).unwrap();
    assert_eq!(hits.len(), 1);
    assert_eq!(hits[0].session.project_id.as_deref(), Some(B_ID));
    // Empty range matches nothing.
    let q = SessionSearchQuery {
        start_ms: Some(1000),
        end_ms: Some(1000),
        limit: 10,
        ..Default::default()
    };
    assert!(store.search_sessions(&q).unwrap().is_empty());
    // Boundaries: start inclusive, end exclusive.
    let q = SessionSearchQuery {
        start_ms: Some(0),
        end_ms: Some(1),
        limit: 10,
        ..Default::default()
    };
    assert_eq!(store.search_sessions(&q).unwrap().len(), 1);
    let q = SessionSearchQuery {
        start_ms: Some(1),
        end_ms: Some(5000),
        limit: 10,
        ..Default::default()
    };
    assert!(store.search_sessions(&q).unwrap().is_empty());
}

#[test]
fn search_text_in_range_must_come_from_row_in_range() {
    // One session with two events: foo at 0, bar at 900 (same session, gap 1000).
    let store = mem_small();
    store
        .append(
            "focus",
            "hyprland",
            &ctx_full(
                0,
                "0x1",
                "kitty",
                "foo marker",
                "1",
                None,
                Some(proj(A_ID, "A")),
            ),
        )
        .unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx_full(
                900,
                "0x2",
                "kitty",
                "bar other",
                "1",
                None,
                Some(proj(A_ID, "A")),
            ),
        )
        .unwrap();
    assert_eq!(store.recent_sessions(10).unwrap().len(), 1);
    // Text "foo" with range [800,1000): matching row at 0 is outside range,
    // so no hit even though the session overlaps via the 900 event.
    let q = SessionSearchQuery {
        start_ms: Some(800),
        end_ms: Some(1000),
        query: Some("foo marker".to_string()),
        limit: 10,
        ..Default::default()
    };
    assert!(
        store.search_sessions(&q).unwrap().is_empty(),
        "text match outside range must not hit via overlapping session"
    );
    // Same range without text matches (event at 900 in range).
    let q = SessionSearchQuery {
        start_ms: Some(800),
        end_ms: Some(1000),
        limit: 10,
        ..Default::default()
    };
    assert_eq!(store.search_sessions(&q).unwrap().len(), 1);
    // Text "bar" with same range hits (matching row in range).
    let q = SessionSearchQuery {
        start_ms: Some(800),
        end_ms: Some(1000),
        query: Some("bar".to_string()),
        limit: 10,
        ..Default::default()
    };
    assert_eq!(store.search_sessions(&q).unwrap().len(), 1);
}

// ---------- project / app / resource / device / free-text ----------

#[test]
fn search_project_filter() {
    let store = three_session_store();
    let q = SessionSearchQuery {
        project_id: Some(A_ID.to_string()),
        limit: 10,
        ..Default::default()
    };
    let hits = store.search_sessions(&q).unwrap();
    assert_eq!(hits.len(), 2);
    assert!(hits
        .iter()
        .all(|h| h.session.project_id.as_deref() == Some(A_ID)));
    // Uppercase UUID normalizes.
    let q = SessionSearchQuery {
        project_id: Some(A_ID.to_uppercase()),
        limit: 10,
        ..Default::default()
    };
    assert_eq!(store.search_sessions(&q).unwrap().len(), 2);
}

#[test]
fn search_application_filter() {
    let store = mem_small();
    store
        .append(
            "focus",
            "hyprland",
            &ctx_full(0, "0x1", "Zen", "t1", "1", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx_full(5000, "0x2", "kitty", "t2", "1", None, Some(proj(B_ID, "B"))),
        )
        .unwrap();
    let q = SessionSearchQuery {
        application: Some("Zen".to_string()),
        limit: 10,
        ..Default::default()
    };
    let hits = store.search_sessions(&q).unwrap();
    assert_eq!(hits.len(), 1);
    assert!(hits[0].session.applications.contains(&"Zen".to_string()));
    // Case-insensitive (FTS unicode61 lowercases).
    let q = SessionSearchQuery {
        application: Some("zen".to_string()),
        limit: 10,
        ..Default::default()
    };
    assert_eq!(store.search_sessions(&q).unwrap().len(), 1);
}

#[test]
fn search_resource_file_style() {
    // search_and_matching.jl style: file substring via FTS tokens.
    let store = mem_small();
    let r1 = ResourceContext::new(
        "neovim",
        Some("/repo/note.md"),
        Some("/repo"),
        Some("/repo"),
        None,
        None,
        None,
        None,
    );
    let r2 = ResourceContext::new(
        "kitty",
        Some("/tmp/other.md"),
        Some("/tmp"),
        None,
        None,
        None,
        None,
        None,
    );
    store
        .append(
            "focus",
            "hyprland",
            &ctx_full(0, "0x1", "nvim", "t", "1", Some(r1), Some(proj(A_ID, "A"))),
        )
        .unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx_full(5000, "0x2", "k", "t", "1", Some(r2), Some(proj(B_ID, "B"))),
        )
        .unwrap();
    for needle in ["note.md", "note", "/repo/note.md"] {
        let q = SessionSearchQuery {
            resource: Some(needle.to_string()),
            limit: 10,
            ..Default::default()
        };
        let hits = store.search_sessions(&q).unwrap();
        assert_eq!(hits.len(), 1, "needle {needle:?} must hit one session");
        assert_eq!(hits[0].session.project_id.as_deref(), Some(A_ID));
        // Matched resources favored: the portable file entry comes first.
        assert!(!hits[0].resources.is_empty());
        let first = &hits[0].resources[0];
        assert!(
            first.portable_identity.as_deref() == Some("file:note.md")
                || first
                    .local_identity
                    .as_deref()
                    .map(|s| s.contains("note"))
                    .unwrap_or(false)
                || format!("{:?}", first.resource).contains("note"),
            "matched resource favored for {needle:?}: {first:?}"
        );
    }
}

#[test]
fn search_device_filter() {
    let store = mem_small();
    store
        .append(
            "focus",
            "hyprland",
            &ctx_full(0, "0x1", "k", "a", "1", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    let local = store.device_id().unwrap();
    let q = SessionSearchQuery {
        device_id: Some(local.clone()),
        limit: 10,
        ..Default::default()
    };
    assert_eq!(store.search_sessions(&q).unwrap().len(), 1);
    // Uppercase normalizes.
    let q = SessionSearchQuery {
        device_id: Some(local.to_uppercase()),
        limit: 10,
        ..Default::default()
    };
    assert_eq!(store.search_sessions(&q).unwrap().len(), 1);
    // Unknown device matches nothing.
    let q = SessionSearchQuery {
        device_id: Some("eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee".to_string()),
        limit: 10,
        ..Default::default()
    };
    assert!(store.search_sessions(&q).unwrap().is_empty());
}

#[test]
fn search_free_text_and_combined() {
    let store = mem_small();
    let r = ResourceContext::new(
        "neovim",
        Some("/repo/standup.md"),
        Some("/repo"),
        Some("/repo"),
        Some("main"),
        None,
        None,
        Some("Standup Title"),
    );
    store
        .append(
            "focus",
            "hyprland",
            &ctx_full(
                0,
                "0x1",
                "Zen",
                "morning standup notes",
                "Eng",
                Some(r),
                Some(proj(A_ID, "Alpha")),
            ),
        )
        .unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx_full(
                5000,
                "0x2",
                "kitty",
                "unrelated",
                "1",
                None,
                Some(proj(B_ID, "B")),
            ),
        )
        .unwrap();
    // Free text across columns.
    let q = SessionSearchQuery {
        query: Some("standup".to_string()),
        limit: 10,
        ..Default::default()
    };
    let hits = store.search_sessions(&q).unwrap();
    assert_eq!(hits.len(), 1);
    assert_eq!(hits[0].session.project_id.as_deref(), Some(A_ID));
    // Combined: project + app + resource + device + range + query.
    let local = store.device_id().unwrap();
    let q = SessionSearchQuery {
        start_ms: Some(0),
        end_ms: Some(1000),
        project_id: Some(A_ID.to_string()),
        application: Some("Zen".to_string()),
        resource: Some("standup.md".to_string()),
        device_id: Some(local),
        query: Some("morning".to_string()),
        limit: 10,
    };
    let hits = store.search_sessions(&q).unwrap();
    assert_eq!(hits.len(), 1);
    // Same combined but wrong project → nothing.
    let mut q2 = q.clone();
    q2.project_id = Some(B_ID.to_string());
    assert!(store.search_sessions(&q2).unwrap().is_empty());
}

#[test]
fn search_limit_order_no_matches() {
    let store = three_session_store();
    // No filter = recent sessions, newest-first.
    let hits = store.search_sessions(&search_q(10)).unwrap();
    assert_eq!(hits.len(), 3);
    assert_eq!(hits[0].session.start_ms, 6000);
    assert_eq!(hits[1].session.start_ms, 5000);
    assert_eq!(hits[2].session.start_ms, 0);
    // limit=1 answers "last" (newest).
    let hits = store.search_sessions(&search_q(1)).unwrap();
    assert_eq!(hits.len(), 1);
    assert_eq!(hits[0].session.start_ms, 6000);
    // No matches.
    let q = SessionSearchQuery {
        query: Some("zzz-no-such-token-qqq".to_string()),
        limit: 10,
        ..Default::default()
    };
    assert!(store.search_sessions(&q).unwrap().is_empty());
    // Resources capped at 8.
    for h in store.search_sessions(&search_q(10)).unwrap() {
        assert!(h.resources.len() as i64 <= 8);
    }
}

// ---------- session detail ----------

#[test]
fn session_detail_with_and_without_events() {
    let store = mem_small();
    let r = ResourceContext::new(
        "neovim",
        Some("/repo/a.md"),
        Some("/repo"),
        Some("/repo"),
        None,
        None,
        None,
        None,
    );
    store
        .append(
            "focus",
            "hyprland",
            &ctx_full(0, "0x1", "k", "a", "1", Some(r), Some(proj(A_ID, "A"))),
        )
        .unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx_full(100, "0x2", "k", "b", "1", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    let sid = store.recent_sessions(10).unwrap()[0].session_id.clone();
    // Without events: no raw fetch.
    let d = store.session_detail(&sid, 10, false, 10).unwrap();
    assert!(d.session.is_some());
    assert!(!d.events_included);
    assert!(d.events.is_empty());
    assert!(!d.resources.is_empty());
    // With events: bounded raw events.
    let d = store.session_detail(&sid, 10, true, 10).unwrap();
    assert!(d.events_included);
    assert_eq!(d.events.len(), 2);
    assert!(d.events.windows(2).all(|w| w[0].id < w[1].id));
    // Resource limit respected.
    let d = store.session_detail(&sid, 1, false, 10).unwrap();
    assert!(d.resources.len() <= 1);
    // Missing session → null shape.
    let missing = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".to_string();
    let d = store.session_detail(&missing, 10, false, 10).unwrap();
    assert!(d.session.is_none());
    assert!(d.resources.is_empty());
    assert!(!d.events_included);
    assert!(d.events.is_empty());
    let d = store.session_detail(&missing, 10, true, 10).unwrap();
    assert!(d.session.is_none());
    assert!(d.events_included);
    assert!(d.events.is_empty());
}

// ---------- escaping / malformed ----------

#[test]
fn search_escaping_no_injection() {
    let store = mem_small();
    store
        .append(
            "focus",
            "hyprland",
            &ctx_full(0, "0x1", "Zen", "hello", "1", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    // FTS syntax characters must be literal, never operators: no crash,
    // and OR/prefix injections must not match unintended rows.
    for evil in [
        "\"",
        "*",
        ":",
        "AND",
        "OR",
        "NOT",
        "(",
        ")",
        "{application} : Zen",
        "NEAR(hello world)",
        "\"Zen\" OR \"kitty\"",
        "foo\"bar",
        "Ze*",
    ] {
        let q = SessionSearchQuery {
            application: Some(evil.to_string()),
            limit: 10,
            ..Default::default()
        };
        // Must not error (literal match, likely empty).
        let hits = store.search_sessions(&q).unwrap();
        // Punctuation-only / operator words quoted → empty; prefix Ze* as
        // literal "Ze*" (tokens ze) must not prefix-match Zen.
        assert!(
            hits.is_empty(),
            "evil application {evil:?} must not inject, got {hits:?}"
        );
        let q = SessionSearchQuery {
            query: Some(evil.to_string()),
            limit: 10,
            ..Default::default()
        };
        let _ = store.search_sessions(&q).unwrap();
    }
    // Literal star-suffix still matches its own token (Zen* → Zen) but must
    // not act as a prefix operator.
    let q = SessionSearchQuery {
        application: Some("Zen*".to_string()),
        limit: 10,
        ..Default::default()
    };
    assert_eq!(
        store.search_sessions(&q).unwrap().len(),
        1,
        "literal Zen* should still match Zen"
    );
    // Diacritics-insensitive: café matches cafe.
    let store2 = ActivityStore::open_in_memory().unwrap();
    store2
        .append(
            "focus",
            "hyprland",
            &ctx_full(0, "0x1", "k", "Café au lait", "1", None, None),
        )
        .unwrap();
    let q = SessionSearchQuery {
        query: Some("cafe".to_string()),
        limit: 10,
        ..Default::default()
    };
    assert_eq!(store2.search_sessions(&q).unwrap().len(), 1);
}

#[test]
fn search_malformed_args_rejected() {
    let store = mem_small();
    // Bad project/device/limit/range/text.
    let cases: Vec<SessionSearchQuery> = vec![
        SessionSearchQuery {
            project_id: Some("bad".to_string()),
            limit: 10,
            ..Default::default()
        },
        SessionSearchQuery {
            device_id: Some("bad".to_string()),
            limit: 10,
            ..Default::default()
        },
        SessionSearchQuery {
            limit: 0,
            ..Default::default()
        },
        SessionSearchQuery {
            limit: MAX_QUERY_LIMIT + 1,
            ..Default::default()
        },
        SessionSearchQuery {
            start_ms: Some(10),
            end_ms: None,
            limit: 10,
            ..Default::default()
        },
        SessionSearchQuery {
            start_ms: Some(20),
            end_ms: Some(10),
            limit: 10,
            ..Default::default()
        },
        SessionSearchQuery {
            start_ms: Some(-1),
            end_ms: Some(10),
            limit: 10,
            ..Default::default()
        },
        SessionSearchQuery {
            application: Some("   ".to_string()),
            limit: 10,
            ..Default::default()
        },
        SessionSearchQuery {
            resource: Some("x".repeat(300)),
            limit: 10,
            ..Default::default()
        },
        SessionSearchQuery {
            query: Some("".to_string()),
            limit: 10,
            ..Default::default()
        },
    ];
    for q in cases {
        assert!(
            store.search_sessions(&q).is_err(),
            "malformed search {q:?} must error"
        );
    }
    assert!(store.session_detail("bad", 10, false, 10).is_err());
    assert!(store.session_detail(&"a".repeat(32), 0, false, 10).is_err());
    assert!(store.session_detail(&"a".repeat(32), 10, true, 0).is_err());
}

// ---------- migration ----------

fn downgrade_v4_to_coherent_v3(db: &Path) {
    let c = rusqlite::Connection::open(db).unwrap();
    c.execute_batch("DROP TABLE activity_fts;").unwrap();
    c.execute_batch("DROP INDEX IF EXISTS idx_sessions_device_time;")
        .unwrap();
    c.execute("UPDATE schema_version SET version = 3", [])
        .unwrap();
    drop(c);
    checkpoint(db);
    chmod_0600(db);
}

#[test]
fn migration_preserves_history_and_backfills_fts() {
    let dir = tmpdir("migrate-preserve");
    let db = dir.join("activity.db");
    let cfg = small_cfg();
    {
        let store = ActivityStore::open_with_session_config(&db, cfg).unwrap();
        assert_eq!(store.schema_version().unwrap(), 4);
        store
            .append(
                "focus",
                "hyprland",
                &ctx_full(
                    0,
                    "0x1",
                    "Zen",
                    "standup notes",
                    "Eng",
                    Some(ResourceContext::new(
                        "neovim",
                        Some("/repo/note.md"),
                        Some("/repo"),
                        Some("/repo"),
                        Some("main"),
                        None,
                        None,
                        Some("T"),
                    )),
                    Some(proj(A_ID, "Alpha")),
                ),
            )
            .unwrap();
        store
            .append(
                "focus",
                "hyprland",
                &ctx_full(
                    100,
                    "0x2",
                    "kitty",
                    "other",
                    "1",
                    None,
                    Some(proj(A_ID, "Alpha")),
                ),
            )
            .unwrap();
        // Accumulate more history to prove preservation.
        store
            .append(
                "focus",
                "hyprland",
                &ctx_full(5000, "0x3", "k", "b", "1", None, Some(proj(B_ID, "Beta"))),
            )
            .unwrap();
    }
    downgrade_v4_to_coherent_v3(&db);
    // Read-only on exact v3 gives a clear migration-required error.
    match ActivityStore::open_read_only(&db) {
        Err(StoreError::IncompatibleSchema(m)) => assert!(
            m.contains("requires migration"),
            "v3 read-only must say migration-required, got {m:?}"
        ),
        Err(e) => panic!("expected migration-required IncompatibleSchema, got {e:?}"),
        Ok(_) => panic!("read-only must not open v3"),
    }
    // Ordinary writable opens reject v3: migration requires the collector
    // lock (an old v3 collector could otherwise still be appending).
    match ActivityStore::open_with_session_config(&db, cfg) {
        Err(StoreError::IncompatibleSchema(m)) => assert!(
            m.contains("collector lock/restart"),
            "ordinary v3 open must demand collector lock/restart, got {m:?}"
        ),
        Err(e) => panic!("expected collector lock/restart IncompatibleSchema, got {e:?}"),
        Ok(_) => panic!("ordinary open must not migrate v3"),
    }
    // A lock for a DIFFERENT database is rejected without touching this one.
    {
        let other_dir = tmpdir("migrate-wronglock");
        let other_db = other_dir.join("other.db");
        let other_lock = acquire_lock(&lock_path_for(&other_db)).unwrap();
        let before = std::fs::read(&db).unwrap();
        match ActivityStore::open_with_session_config_and_lock(&db, cfg, &other_lock) {
            Err(StoreError::InvalidArgument(_)) => {}
            Err(e) => panic!("expected InvalidArgument for foreign lock, got {e:?}"),
            Ok(_) => panic!("foreign lock must not migrate"),
        }
        assert_eq!(
            before,
            std::fs::read(&db).unwrap(),
            "foreign lock must not mutate"
        );
        drop(other_lock);
        let _ = std::fs::remove_dir_all(&other_dir);
    }
    // Lock-capability open migrates to v4, preserving rows.
    {
        let guard = acquire_lock(&lock_path_for(&db)).unwrap();
        let store = ActivityStore::open_with_session_config_and_lock(&db, cfg, &guard).unwrap();
        assert_eq!(store.schema_version().unwrap(), 4);
        assert_eq!(store.recent_activity(10).unwrap().len(), 3);
        assert_eq!(store.recent_sessions(10).unwrap().len(), 2);
        // Backfilled FTS is searchable (project name, app, title, file).
        for (field, val) in [
            ("query", "standup"),
            ("query", "Alpha"),
            ("application", "Zen"),
            ("resource", "note.md"),
        ] {
            let q = match field {
                "application" => SessionSearchQuery {
                    application: Some(val.to_string()),
                    limit: 10,
                    ..Default::default()
                },
                "resource" => SessionSearchQuery {
                    resource: Some(val.to_string()),
                    limit: 10,
                    ..Default::default()
                },
                _ => SessionSearchQuery {
                    query: Some(val.to_string()),
                    limit: 10,
                    ..Default::default()
                },
            };
            let hits = store.search_sessions(&q).unwrap();
            assert!(!hits.is_empty(), "backfilled {field}={val:?} must hit");
        }
        // Device index exists after migration.
        let c = rusqlite::Connection::open(&db).unwrap();
        let n: i64 = c
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_sessions_device_time'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(n, 1);
    }
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn migration_failure_rolls_back_untouched() {
    let dir = tmpdir("migrate-rollback");
    let db = dir.join("activity.db");
    let cfg = small_cfg();
    {
        let store = ActivityStore::open_with_session_config(&db, cfg).unwrap();
        store
            .append(
                "focus",
                "hyprland",
                &ctx_full(0, "0x1", "k", "a", "1", None, Some(proj(A_ID, "A"))),
            )
            .unwrap();
    }
    downgrade_v4_to_coherent_v3(&db);
    // Corrupt one snapshot (malformed JSON): v3 data validation passes
    // (it does not parse JSON), but backfill parsing must fail closed.
    {
        let c = rusqlite::Connection::open(&db).unwrap();
        c.execute(
            "UPDATE activity SET snapshot_json = '{\"bad\": ' WHERE id = 1",
            [],
        )
        .unwrap();
        drop(c);
        checkpoint(&db);
        chmod_0600(&db);
    }
    let before = std::fs::read(&db).unwrap();
    {
        let guard = acquire_lock(&lock_path_for(&db)).unwrap();
        match ActivityStore::open_with_session_config_and_lock(&db, cfg, &guard) {
            Err(StoreError::IncompatibleSchema(_)) => {}
            Err(e) => panic!("expected IncompatibleSchema rollback, got {e:?}"),
            Ok(_) => panic!("malformed snapshot migration must fail"),
        }
        drop(guard);
    }
    let after = std::fs::read(&db).unwrap();
    assert_eq!(before, after, "failed migration must leave DB untouched");
    // Version still 3, no FTS.
    {
        let c = rusqlite::Connection::open(&db).unwrap();
        let v: i64 = c
            .query_row("SELECT version FROM schema_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, 3);
        let n: i64 = c
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='activity_fts'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(n, 0, "failed migration must not leave FTS");
    }
    let _ = std::fs::remove_dir_all(&dir);
}

// ---------- strict schema ----------

#[test]
fn strict_v4_rejects_extra_and_bad_fts_unchanged() {
    use qs_agent_orchestrator::desktop_store::StoreError;
    // Extra objects reject.
    for (tag, sql) in [
        ("extra-table", "CREATE TABLE evil (id INTEGER PRIMARY KEY);"),
        ("extra-view", "CREATE VIEW v AS SELECT 1 AS x;"),
        (
            "extra-trigger",
            "CREATE TRIGGER trg AFTER INSERT ON activity BEGIN SELECT 1; END;",
        ),
        (
            "extra-index",
            "CREATE INDEX idx_evil ON activity (observed_at_ms);",
        ),
        ("drop-fts", "DROP TABLE activity_fts;"),
        (
            "wrong-device-idx",
            "DROP INDEX idx_sessions_device_time; CREATE INDEX idx_sessions_device_time ON sessions (device_id);",
        ),
        (
            "unique-device-idx",
            "DROP INDEX idx_sessions_device_time; CREATE UNIQUE INDEX idx_sessions_device_time ON sessions (device_id, end_ms, start_ms, session_id);",
        ),
    ] {
        let dir = tmpdir(&format!("strict-v4-{tag}"));
        let db = dir.join("activity.db");
        {
            let store = ActivityStore::open(&db).unwrap();
            store
                .append(
                    "focus",
                    "hyprland",
                    &ctx_full(1, "0x1", "t", "t", "1", None, None),
                )
                .unwrap();
            drop(store);
            checkpoint(&db);
        }
        {
            let c = rusqlite::Connection::open(&db).unwrap();
            // DROP TABLE activity_fts removes shadows automatically; other
            // cases add foreign objects or break the device index shape.
            c.execute_batch(sql).unwrap_or_else(|_| {
                // DROP INDEX on missing is fine; ignore.
            });
            drop(c);
            checkpoint(&db);
            chmod_0600(&db);
        }
        let before = std::fs::read(&db).unwrap();
        assert!(
            matches!(
                ActivityStore::open(&db),
                Err(StoreError::IncompatibleSchema(_))
            ),
            "{tag} writable must reject"
        );
        assert!(
            matches!(
                ActivityStore::open_read_only(&db),
                Err(StoreError::IncompatibleSchema(_))
            ),
            "{tag} read-only must reject"
        );
        assert_eq!(before, std::fs::read(&db).unwrap(), "{tag} unchanged");
        let _ = std::fs::remove_dir_all(&dir);
    }
    // Missing non-unique device index is completable (writable recreates,
    // read-only tolerates).
    {
        let dir = tmpdir("strict-v4-missing-device-ok");
        let db = dir.join("activity.db");
        {
            let store = ActivityStore::open(&db).unwrap();
            store
                .append(
                    "focus",
                    "hyprland",
                    &ctx_full(1, "0x1", "t", "t", "1", None, None),
                )
                .unwrap();
            drop(store);
            checkpoint(&db);
        }
        {
            let c = rusqlite::Connection::open(&db).unwrap();
            c.execute_batch("DROP INDEX idx_sessions_device_time;")
                .unwrap();
            drop(c);
            checkpoint(&db);
            chmod_0600(&db);
        }
        let ro = ActivityStore::open_read_only(&db).unwrap();
        assert_eq!(ro.schema_version().unwrap(), 4);
        drop(ro);
        let store = ActivityStore::open(&db).unwrap();
        assert_eq!(store.schema_version().unwrap(), 4);
        let c = rusqlite::Connection::open(&db).unwrap();
        let n: i64 = c
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_sessions_device_time'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(n, 1, "writable must recreate missing device index");
        let _ = std::fs::remove_dir_all(&dir);
    }
}

// ---------- compact CLI ----------

#[test]
fn cli_search_compact_and_missing() {
    let dir = tmpdir("cli-search");
    let missing = dir.join("nope").join("activity.db");
    let m = missing.to_string_lossy().to_string();
    // Missing DB → empty shape, exit 0.
    let (code, out, _) = run_bin(&["--db", &m, "search", "--limit", "5"]);
    assert_eq!(code, 0);
    let v: serde_json::Value = serde_json::from_str(&out).unwrap();
    assert_eq!(v["count"], 0);
    assert_eq!(v["sessions"], serde_json::json!([]));
    assert!(v.get("query").is_some());
    // Bad args fail closed even when missing.
    let (code, _, _) = run_bin(&["--db", &m, "search", "--limit", "0"]);
    assert_ne!(code, 0);
    let (code, _, _) = run_bin(&["--db", &m, "search", "--project", "bad"]);
    assert_ne!(code, 0);
    let (code, _, _) = run_bin(&["--db", &m, "search", "--device", "bad"]);
    assert_ne!(code, 0);
    let (code, _, _) = run_bin(&["--db", &m, "search", "--from", "10"]);
    assert_eq!(code, 2); // unpaired range → usage, exit 2
    let (code, _, _) = run_bin(&["--db", &m, "search", "--application", "   "]);
    // Whitespace-only application is malformed → non-zero (1).
    assert_ne!(code, 0);
    let (code, _, _) = run_bin(&["--db", &m, "search", "--session-gap-ms", "100"]);
    assert_eq!(code, 2);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn cli_search_and_detail_shapes_against_real_db() {
    let dir = tmpdir("cli-search-real");
    let db = dir.join("activity.db");
    let m = db.to_string_lossy().to_string();
    let store = ActivityStore::open(&db).unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx_full(
                1000,
                "0x1",
                "Zen",
                "standup notes",
                "Eng",
                Some(ResourceContext::new(
                    "neovim",
                    Some("/repo/f.md"),
                    Some("/repo"),
                    Some("/repo"),
                    None,
                    None,
                    None,
                    None,
                )),
                Some(proj(A_ID, "Alpha")),
            ),
        )
        .unwrap();
    let sid = store.recent_sessions(10).unwrap()[0].session_id.clone();
    drop(store);
    // search finds it; compact omits thresholds/bound IDs.
    let (code, out, _) = run_bin(&["--db", &m, "search", "--query", "standup"]);
    assert_eq!(code, 0);
    let v: serde_json::Value = serde_json::from_str(&out).unwrap();
    assert_eq!(v["count"], 1);
    let s = &v["sessions"][0];
    for keep in [
        "session_id",
        "device_id",
        "project",
        "start_ms",
        "end_ms",
        "event_count",
        "status",
        "effective_status",
        "matched_at_ms",
        "applications",
        "resources",
    ] {
        assert!(s.get(keep).is_some(), "compact must keep {keep}");
    }
    for omit in [
        "gap_ms",
        "interruption_ms",
        "first_activity_id",
        "last_activity_id",
    ] {
        assert!(s.get(omit).is_none(), "compact must omit {omit}");
    }
    assert!(s["resources"].as_array().unwrap().len() <= 8);
    assert!(v["query"].get("limit").is_some());
    // session-detail without events omits `events`.
    let (code, out, _) = run_bin(&["--db", &m, "session-detail", "--session", &sid]);
    assert_eq!(code, 0);
    let v: serde_json::Value = serde_json::from_str(&out).unwrap();
    assert!(v["session"].get("session_id").is_some());
    assert_eq!(v["events_included"], false);
    assert!(
        v.get("events").is_none(),
        "events must be omitted unless requested"
    );
    // With events includes them.
    let (code, out, _) = run_bin(&[
        "--db",
        &m,
        "session-detail",
        "--session",
        &sid,
        "--include-events",
    ]);
    assert_eq!(code, 0);
    let v: serde_json::Value = serde_json::from_str(&out).unwrap();
    assert_eq!(v["events_included"], true);
    assert_eq!(v["events"].as_array().unwrap().len(), 1);
    // Missing session → null shape.
    let fake = "b".repeat(32);
    let (code, out, _) = run_bin(&["--db", &m, "session-detail", "--session", &fake]);
    assert_eq!(code, 0);
    let v: serde_json::Value = serde_json::from_str(&out).unwrap();
    assert!(v["session"].is_null());
    // --event-limit without --include-events rejects (usage, exit 2).
    let (code, _, _) = run_bin(&[
        "--db",
        &m,
        "session-detail",
        "--session",
        &sid,
        "--event-limit",
        "5",
    ]);
    assert_eq!(code, 2);
    // Missing DB detail → null shape.
    let missing = dir.join("nope2").join("activity.db");
    let mm = missing.to_string_lossy().to_string();
    let (code, out, _) = run_bin(&["--db", &mm, "session-detail", "--session", &sid]);
    assert_eq!(code, 0);
    let v: serde_json::Value = serde_json::from_str(&out).unwrap();
    assert!(v["session"].is_null());
    assert_eq!(v["events_included"], false);
    let _ = std::fs::remove_dir_all(&dir);
}

// ---------- review fixes ----------

/// Rebuild the FTS table with a variant declaration, preserving existing rows
/// (read back first, drop, recreate, reinsert). Panics if the variant DDL
/// itself is invalid.
fn rebuild_fts_with(db: &Path, ddl: &str) {
    let c = rusqlite::Connection::open(db).unwrap();
    let rows: Vec<(
        i64,
        String,
        String,
        String,
        String,
        String,
        String,
        String,
        String,
    )> = {
        let mut stmt = c
            .prepare("SELECT rowid, session_id, resource_key, project, application, window_title, workspace, resource, metadata FROM activity_fts ORDER BY rowid")
            .unwrap();
        stmt.query_map([], |r| {
            Ok((
                r.get(0)?,
                r.get(1)?,
                r.get(2)?,
                r.get(3)?,
                r.get(4)?,
                r.get(5)?,
                r.get(6)?,
                r.get(7)?,
                r.get(8)?,
            ))
        })
        .unwrap()
        .collect::<Result<Vec<_>, _>>()
        .unwrap()
    };
    c.execute_batch("DROP TABLE activity_fts;").unwrap();
    c.execute_batch(ddl).unwrap();
    for (id, sid, key, p, app, title, ws, res, meta) in rows {
        c.execute(
            "INSERT INTO activity_fts(rowid, session_id, resource_key, project, application, window_title, workspace, resource, metadata) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)",
            rusqlite::params![id, sid, key, p, app, title, ws, res, meta],
        )
        .unwrap();
    }
    drop(c);
    checkpoint(db);
    chmod_0600(db);
}

#[test]
fn hidden_sqlite_prefix_objects_rejected_unchanged() {
    // `LIKE 'sqlite_%'` treats `_` as a wildcard, so `sqlitex_…` objects
    // could evade validation as false "internals". Literal `sqlite_*`
    // (GLOB) prefix matching must reject them unchanged on both opens.
    for (tag, sql) in [
        (
            "hidden-table",
            "CREATE TABLE sqlitex_hidden (id INTEGER PRIMARY KEY);",
        ),
        ("hidden-view", "CREATE VIEW sqlitex_view AS SELECT 1 AS x;"),
        (
            "hidden-trigger",
            "CREATE TRIGGER sqlitex_trg AFTER INSERT ON activity BEGIN SELECT 1; END;",
        ),
        (
            "hidden-index",
            "CREATE INDEX sqlitex_idx ON activity (observed_at_ms);",
        ),
    ] {
        let dir = tmpdir(&format!("hidden-{tag}"));
        let db = dir.join("activity.db");
        {
            let store = ActivityStore::open(&db).unwrap();
            store
                .append(
                    "focus",
                    "hyprland",
                    &ctx_full(1, "0x1", "t", "t", "1", None, None),
                )
                .unwrap();
            drop(store);
            checkpoint(&db);
        }
        {
            let c = rusqlite::Connection::open(&db).unwrap();
            c.execute_batch(sql).unwrap();
            drop(c);
            checkpoint(&db);
            chmod_0600(&db);
        }
        let before = std::fs::read(&db).unwrap();
        assert!(
            matches!(
                ActivityStore::open(&db),
                Err(StoreError::IncompatibleSchema(_))
            ),
            "{tag} writable must reject"
        );
        assert!(
            matches!(
                ActivityStore::open_read_only(&db),
                Err(StoreError::IncompatibleSchema(_))
            ),
            "{tag} read-only must reject"
        );
        assert_eq!(before, std::fs::read(&db).unwrap(), "{tag} unchanged");
        let _ = std::fs::remove_dir_all(&dir);
    }
}

#[test]
fn v3_trigger_never_fires_migration_rejects_unchanged() {
    // A v3 database carrying a trigger that WOULD fire on the migration's
    // own `UPDATE schema_version` must be rejected before any mutation, and
    // the trigger must never take effect.
    let dir = tmpdir("v3-trigger");
    let db = dir.join("activity.db");
    let cfg = small_cfg();
    {
        let store = ActivityStore::open(&db).unwrap();
        store
            .append(
                "focus",
                "hyprland",
                &ctx_full(0, "0x1", "k", "a", "1", None, Some(proj(A_ID, "A"))),
            )
            .unwrap();
    }
    downgrade_v4_to_coherent_v3(&db);
    {
        let c = rusqlite::Connection::open(&db).unwrap();
        c.execute_batch(
            "CREATE TRIGGER trg_schema_version AFTER UPDATE ON schema_version BEGIN UPDATE activity SET kind = 'pwned'; END;",
        )
        .unwrap();
        drop(c);
        checkpoint(&db);
        chmod_0600(&db);
    }
    let before = std::fs::read(&db).unwrap();
    // Ordinary open: lock/restart rejection (no validation of the trigger).
    match ActivityStore::open_with_session_config(&db, cfg) {
        Err(StoreError::IncompatibleSchema(m)) => {
            assert!(m.contains("collector lock/restart"), "got {m:?}")
        }
        Err(e) => panic!("expected lock/restart rejection, got {e:?}"),
        Ok(_) => panic!("ordinary open must not migrate"),
    }
    // Lock-capability open: strict v3 object validation rejects the trigger
    // before the migration transaction does anything.
    {
        let guard = acquire_lock(&lock_path_for(&db)).unwrap();
        match ActivityStore::open_with_session_config_and_lock(&db, cfg, &guard) {
            Err(StoreError::IncompatibleSchema(_)) => {}
            Err(e) => panic!("expected trigger rejection, got {e:?}"),
            Ok(_) => panic!("triggered v3 must not migrate"),
        }
        drop(guard);
    }
    assert_eq!(before, std::fs::read(&db).unwrap(), "trigger must not fire");
    {
        let c = rusqlite::Connection::open(&db).unwrap();
        let kind: String = c
            .query_row("SELECT kind FROM activity", [], |r| r.get(0))
            .unwrap();
        assert_eq!(kind, "focus", "trigger effect must be absent");
        let v: i64 = c
            .query_row("SELECT version FROM schema_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, 3, "version must stay 3");
    }
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn ordinary_open_rejects_v3_while_lock_held() {
    // Simulates a still-running old v3 collector holding the per-DB lock:
    // ordinary opens must still refuse to migrate (only the lock-capability
    // constructor migrates, and only with the matching guard).
    let dir = tmpdir("v3-lockheld");
    let db = dir.join("activity.db");
    let cfg = small_cfg();
    {
        let store = ActivityStore::open(&db).unwrap();
        store
            .append(
                "focus",
                "hyprland",
                &ctx_full(0, "0x1", "k", "a", "1", None, Some(proj(A_ID, "A"))),
            )
            .unwrap();
    }
    downgrade_v4_to_coherent_v3(&db);
    let _held = acquire_lock(&lock_path_for(&db)).unwrap();
    let before = std::fs::read(&db).unwrap();
    match ActivityStore::open_with_session_config(&db, cfg) {
        Err(StoreError::IncompatibleSchema(m)) => {
            assert!(m.contains("collector lock/restart"), "got {m:?}")
        }
        Err(e) => panic!("expected lock/restart rejection, got {e:?}"),
        Ok(_) => panic!("ordinary open must not migrate while lock held"),
    }
    match ActivityStore::open(&db) {
        Err(StoreError::IncompatibleSchema(m)) => {
            assert!(m.contains("collector lock/restart"), "got {m:?}")
        }
        Err(e) => panic!("expected lock/restart rejection, got {e:?}"),
        Ok(_) => panic!("ordinary open must not migrate while lock held"),
    }
    assert_eq!(before, std::fs::read(&db).unwrap(), "no mutation");
    drop(_held);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn read_snapshot_and_migration_hold_writer_lock() {
    // Migration runs prevalidation inside the same BEGIN IMMEDIATE that
    // performs it: a concurrent writer holding the database blocks migration
    // instead of racing it.
    let dir = tmpdir("migrate-race");
    let db = dir.join("activity.db");
    let cfg = small_cfg();
    {
        let store = ActivityStore::open(&db).unwrap();
        store
            .append(
                "focus",
                "hyprland",
                &ctx_full(0, "0x1", "k", "a", "1", None, Some(proj(A_ID, "A"))),
            )
            .unwrap();
    }
    downgrade_v4_to_coherent_v3(&db);
    // Hold a RESERVED lock from a second connection.
    let writer = rusqlite::Connection::open(&db).unwrap();
    writer.execute_batch("BEGIN IMMEDIATE;").unwrap();
    writer
        .execute(
            "INSERT INTO activity (observed_at_ms, kind, source, snapshot_json) VALUES (1, 'x', 'y', '{}')",
            [],
        )
        .unwrap();
    let guard = acquire_lock(&lock_path_for(&db)).unwrap();
    let res = ActivityStore::open_with_session_config_and_lock(&db, cfg, &guard);
    // Must fail (busy/locked), never half-migrate.
    assert!(res.is_err(), "migration under a held writer must fail");
    writer.execute_batch("ROLLBACK;").unwrap();
    drop(writer);
    drop(guard);
    // Still a clean v3: no FTS, version 3.
    {
        let c = rusqlite::Connection::open(&db).unwrap();
        let v: i64 = c
            .query_row("SELECT version FROM schema_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, 3);
        let n: i64 = c
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='activity_fts'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(n, 0);
    }
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn concurrent_read_only_never_falsely_corrupt() {
    // Coordinated writer + readers: repeated read-only opens and searches
    // during atomic appends must never report false IncompatibleSchema.
    let dir = tmpdir("read-race");
    let db = dir.join("activity.db");
    let store = ActivityStore::open(&db).unwrap();
    for i in 0..30 {
        store
            .append(
                "focus",
                "hyprland",
                &ctx_full(
                    i * 10,
                    &format!("0x{i}"),
                    "k",
                    &format!("title {i}"),
                    "1",
                    None,
                    Some(proj(A_ID, "A")),
                ),
            )
            .unwrap();
        // Interleaved read-only open + search on every append.
        let ro = ActivityStore::open_read_only(&db).unwrap();
        let hits = ro
            .search_sessions(&SessionSearchQuery {
                query: Some("title".to_string()),
                limit: 5,
                ..Default::default()
            })
            .unwrap();
        assert!(!hits.is_empty());
        let hist = ro.recent_activity(5).unwrap();
        assert!(!hist.is_empty());
    }
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn fts_declaration_exactness() {
    // application UNINDEXED, diacritics off, and extra detail/prefix options
    // all change search semantics and must reject on both opens. Harmless
    // whitespace/case variants of our exact DDL must keep working.
    let base_cols = "session_id UNINDEXED, resource_key UNINDEXED, project, application, window_title, workspace, resource, metadata";
    let variants: Vec<(&str, String, bool)> = vec![
        (
            "application-unindexed",
            format!("CREATE VIRTUAL TABLE activity_fts USING fts5(session_id UNINDEXED, resource_key UNINDEXED, project, application UNINDEXED, window_title, workspace, resource, metadata, tokenize='unicode61 remove_diacritics 2')"),
            false,
        ),
        (
            "remove-diacritics-0",
            format!("CREATE VIRTUAL TABLE activity_fts USING fts5({base_cols}, tokenize='unicode61 remove_diacritics 0')"),
            false,
        ),
        (
            "detail-none",
            format!("CREATE VIRTUAL TABLE activity_fts USING fts5({base_cols}, tokenize='unicode61 remove_diacritics 2', detail='none')"),
            false,
        ),
        (
            "prefix-option",
            format!("CREATE VIRTUAL TABLE activity_fts USING fts5({base_cols}, tokenize='unicode61 remove_diacritics 2', prefix='1')"),
            false,
        ),
        (
            "whitespace-tricks",
            "CREATE VIRTUAL TABLE activity_fts USING  FTS5(  session_id   UNINDEXED ,\n resource_key UNINDEXED ,  project , application , window_title , workspace , resource , metadata ,  TOKENIZE =  'unicode61   remove_diacritics   2'  )".to_string(),
            true,
        ),
    ];
    for (tag, ddl, accept) in variants {
        let dir = tmpdir(&format!("fts-exact-{tag}"));
        let db = dir.join("activity.db");
        {
            let store = ActivityStore::open(&db).unwrap();
            store
                .append(
                    "focus",
                    "hyprland",
                    &ctx_full(1, "0x1", "t", "t", "1", None, None),
                )
                .unwrap();
            drop(store);
            checkpoint(&db);
        }
        rebuild_fts_with(&db, &ddl);
        if accept {
            let ro = ActivityStore::open_read_only(&db)
                .unwrap_or_else(|e| panic!("{tag} whitespace variant must open read-only: {e:?}"));
            assert_eq!(ro.schema_version().unwrap(), 4);
            drop(ro);
            let store = ActivityStore::open(&db)
                .unwrap_or_else(|e| panic!("{tag} must open writable: {e:?}"));
            assert_eq!(store.schema_version().unwrap(), 4);
            drop(store);
        } else {
            let before = std::fs::read(&db).unwrap();
            assert!(
                matches!(
                    ActivityStore::open(&db),
                    Err(StoreError::IncompatibleSchema(_))
                ),
                "{tag} writable must reject"
            );
            assert!(
                matches!(
                    ActivityStore::open_read_only(&db),
                    Err(StoreError::IncompatibleSchema(_))
                ),
                "{tag} read-only must reject"
            );
            assert_eq!(before, std::fs::read(&db).unwrap(), "{tag} unchanged");
        }
        let _ = std::fs::remove_dir_all(&dir);
    }
}

#[test]
fn fts_altered_shadow_rejects() {
    let dir = tmpdir("fts-shadow");
    let db = dir.join("activity.db");
    {
        let store = ActivityStore::open(&db).unwrap();
        store
            .append(
                "focus",
                "hyprland",
                &ctx_full(1, "0x1", "t", "t", "1", None, None),
            )
            .unwrap();
        drop(store);
        checkpoint(&db);
    }
    {
        let c = rusqlite::Connection::open(&db).unwrap();
        c.execute_batch("ALTER TABLE activity_fts_content ADD COLUMN evil TEXT;")
            .unwrap();
        drop(c);
        checkpoint(&db);
        chmod_0600(&db);
    }
    let before = std::fs::read(&db).unwrap();
    assert!(matches!(
        ActivityStore::open(&db),
        Err(StoreError::IncompatibleSchema(_))
    ));
    assert!(matches!(
        ActivityStore::open_read_only(&db),
        Err(StoreError::IncompatibleSchema(_))
    ));
    assert_eq!(before, std::fs::read(&db).unwrap(), "unchanged");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn readonly_lightweight_vs_writable_deep_projection() {
    // Inexact session projection: writable rejects (deep exact validation);
    // read-only opens by design (structural-only) and still serves queries;
    // nothing is mutated either way.
    let dir = tmpdir("ro-light");
    let db = dir.join("activity.db");
    {
        let store = ActivityStore::open(&db).unwrap();
        for (i, ts) in [1000, 1100, 1200].iter().enumerate() {
            store
                .append(
                    "focus",
                    "hyprland",
                    &ctx_full(
                        *ts,
                        &format!("0x{i}"),
                        "t",
                        "t",
                        "1",
                        None,
                        Some(proj(A_ID, "Alpha")),
                    ),
                )
                .unwrap();
        }
        drop(store);
        checkpoint(&db);
    }
    {
        let c = rusqlite::Connection::open(&db).unwrap();
        c.execute("UPDATE sessions SET start_ms = end_ms", [])
            .unwrap();
        drop(c);
        checkpoint(&db);
        chmod_0600(&db);
    }
    let before = std::fs::read(&db).unwrap();
    assert!(matches!(
        ActivityStore::open(&db),
        Err(StoreError::IncompatibleSchema(_))
    ));
    let ro = ActivityStore::open_read_only(&db).expect("read-only opens projection drift");
    assert_eq!(ro.schema_version().unwrap(), 4);
    assert_eq!(ro.recent_sessions(10).unwrap().len(), 1);
    assert_eq!(ro.recent_activity(10).unwrap().len(), 3);
    drop(ro);
    assert_eq!(before, std::fs::read(&db).unwrap(), "no mutation");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn matched_at_ordering_prefers_newest_match() {
    // Session 1 (project A): match at 0, other event at 900 — one session
    // ending at 900. Session 2 (project B): match at 500. limit=1 must
    // return session 2 (newest MATCH at 500 > 0), even though session 1 ends
    // later. This also documents matched_at_ms on hits and CLI.
    let store = mem_small();
    store
        .append(
            "focus",
            "hyprland",
            &ctx_full(
                0,
                "0x1",
                "k",
                "matchme alpha",
                "1",
                None,
                Some(proj(A_ID, "A")),
            ),
        )
        .unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx_full(900, "0x2", "k", "other", "1", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx_full(
                500,
                "0x3",
                "k",
                "matchme beta",
                "1",
                None,
                Some(proj(B_ID, "B")),
            ),
        )
        .unwrap();
    assert_eq!(store.recent_sessions(10).unwrap().len(), 2);
    let q = SessionSearchQuery {
        query: Some("matchme".to_string()),
        limit: 10,
        ..Default::default()
    };
    let hits = store.search_sessions(&q).unwrap();
    assert_eq!(hits.len(), 2);
    assert_eq!(hits[0].session.project_id.as_deref(), Some(B_ID));
    assert_eq!(hits[0].matched_at_ms, Some(500));
    assert_eq!(hits[1].session.project_id.as_deref(), Some(A_ID));
    assert_eq!(hits[1].matched_at_ms, Some(0));
    let one = store
        .search_sessions(&SessionSearchQuery {
            query: Some("matchme".to_string()),
            limit: 1,
            ..Default::default()
        })
        .unwrap();
    assert_eq!(one.len(), 1);
    assert_eq!(one[0].session.project_id.as_deref(), Some(B_ID));
    // No-FTS search carries null matched_at with session recency.
    let recent = store
        .search_sessions(&SessionSearchQuery {
            limit: 10,
            ..Default::default()
        })
        .unwrap();
    assert!(recent.iter().all(|h| h.matched_at_ms.is_none()));
}

#[test]
fn matched_at_multidevice_overlap() {
    // Local session matches at 0 (ends 900); a foreign-device session
    // matches at 500. Newest matching observation wins across devices.
    let dir = tmpdir("matched-multidev");
    let db = dir.join("activity.db");
    let cfg = small_cfg();
    let store = ActivityStore::open_with_session_config(&db, cfg).unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx_full(
                0,
                "0x1",
                "k",
                "matchme local",
                "1",
                None,
                Some(proj(A_ID, "A")),
            ),
        )
        .unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx_full(900, "0x2", "k", "other", "1", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    drop(store);
    let foreign_sid = "ffffffffffffffffffffffffffffffff".to_string();
    let foreign_dev = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee".to_string();
    let foreign_eid = "dddddddddddddddddddddddddddddddd".to_string();
    let snapshot = serde_json::json!({
        "focused_window": {"id": "0x9", "application": "k", "title": "matchme foreign"},
        "workspace": {"id": "1", "name": "1"},
        "available": true,
        "source": "hyprland",
        "observed_at_ms": 500,
        "resource": null,
        "project": null,
    })
    .to_string();
    {
        let c = rusqlite::Connection::open(&db).unwrap();
        c.execute(
            "INSERT INTO activity (observed_at_ms, kind, source, snapshot_json, project_id, event_id, device_id, session_id) VALUES (500, 'focus', 'hyprland', ?1, NULL, ?2, ?3, ?4)",
            rusqlite::params![snapshot, foreign_eid, foreign_dev, foreign_sid],
        )
        .unwrap();
        let row_id = c.last_insert_rowid();
        c.execute(
            "INSERT INTO activity_fts(rowid, session_id, resource_key, project, application, window_title, workspace, resource, metadata) VALUES (?1, ?2, '', '', 'k', 'matchme foreign', '1', '', 'focus hyprland')",
            rusqlite::params![row_id, foreign_sid],
        )
        .unwrap();
        c.execute(
            "INSERT INTO sessions (session_id, project_id, project_name, start_ms, end_ms, first_activity_id, last_activity_id, event_count, status, ended_reason, gap_ms, interruption_ms, device_id, unresolved_start_ms, applications_json) VALUES (?1, NULL, NULL, 500, 500, ?2, ?2, 1, 'open', NULL, 1000, 200, ?3, NULL, '[\"k\"]')",
            rusqlite::params![foreign_sid, row_id, foreign_dev],
        )
        .unwrap();
        drop(c);
        checkpoint(&db);
        chmod_0600(&db);
    }
    let store = ActivityStore::open_with_session_config(&db, cfg).unwrap();
    let hits = store
        .search_sessions(&SessionSearchQuery {
            query: Some("matchme".to_string()),
            limit: 1,
            ..Default::default()
        })
        .unwrap();
    assert_eq!(hits.len(), 1);
    assert_eq!(hits[0].session.session_id, foreign_sid);
    assert_eq!(hits[0].matched_at_ms, Some(500));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn ranked_resources_newest_eight_beyond_32() {
    // 40 distinct matching resources: the search must return the NEWEST 8
    // by latest matching activity (deterministic), not an unordered
    // DISTINCT LIMIT 32 sample.
    let store = mem_small();
    for i in 0..40 {
        let r = ResourceContext::new(
            "neovim",
            Some(&format!("/r/f{i:02}.md")),
            Some("/r"),
            Some("/r"),
            None,
            None,
            None,
            None,
        );
        store
            .append(
                "focus",
                "hyprland",
                &ctx_full(
                    (i as i64) * 100,
                    &format!("0x{i}"),
                    "nvim",
                    &format!("matchme file {i}"),
                    "1",
                    Some(r),
                    Some(proj(A_ID, "A")),
                ),
            )
            .unwrap();
    }
    assert_eq!(store.recent_sessions(10).unwrap().len(), 1);
    let hits = store
        .search_sessions(&SessionSearchQuery {
            query: Some("matchme".to_string()),
            limit: 10,
            ..Default::default()
        })
        .unwrap();
    assert_eq!(hits.len(), 1);
    assert_eq!(hits[0].matched_at_ms, Some(3900));
    let res = &hits[0].resources;
    assert_eq!(res.len(), 8, "capped at 8");
    let mut seen_ms: Vec<i64> = res.iter().map(|r| r.last_seen_ms).collect();
    seen_ms.sort_unstable();
    assert_eq!(
        seen_ms,
        vec![3200, 3300, 3400, 3500, 3600, 3700, 3800, 3900]
    );
    // Deterministic order: newest matching first.
    assert_eq!(res[0].last_seen_ms, 3900);
    assert_eq!(res[7].last_seen_ms, 3200);
}

#[test]
fn fts_insert_failure_rolls_back_append() {
    // Real mid-transaction FTS failure (not a pre-transaction argument
    // error): drop the FTS table out from under a live store handle, then
    // append must fail with raw activity, session, and resource changes all
    // rolled back.
    let dir = tmpdir("fts-fail");
    let db = dir.join("activity.db");
    let cfg = small_cfg();
    let store = ActivityStore::open_with_session_config(&db, cfg).unwrap();
    let r = ResourceContext::new(
        "neovim",
        Some("/repo/a.md"),
        Some("/repo"),
        Some("/repo"),
        None,
        None,
        None,
        None,
    );
    store
        .append(
            "focus",
            "hyprland",
            &ctx_full(0, "0x1", "k", "a", "1", Some(r), Some(proj(A_ID, "A"))),
        )
        .unwrap();
    let n_activity = store.recent_activity(100).unwrap().len();
    let last_id = store.recent_activity(100).unwrap()[0].id;
    let sid = store.recent_sessions(10).unwrap()[0].session_id.clone();
    let event_count = store.recent_sessions(10).unwrap()[0].event_count;
    let n_resources = store.session_resources(&sid, 100).unwrap().len();
    // Break FTS behind the store's back.
    {
        let c = rusqlite::Connection::open(&db).unwrap();
        c.execute_batch("DROP TABLE activity_fts;").unwrap();
        drop(c);
        checkpoint(&db);
    }
    let r2 = ResourceContext::new(
        "neovim",
        Some("/repo/b.md"),
        Some("/repo"),
        Some("/repo"),
        None,
        None,
        None,
        None,
    );
    let res = store.append(
        "focus",
        "hyprland",
        &ctx_full(100, "0x2", "k", "b", "1", Some(r2), Some(proj(A_ID, "A"))),
    );
    assert!(res.is_err(), "append with broken FTS must fail");
    // Everything rolled back: no new activity row, same session count,
    // same resource rollup.
    let after = store.recent_activity(100).unwrap();
    assert_eq!(after.len(), n_activity);
    assert_eq!(after[0].id, last_id);
    {
        let c = rusqlite::Connection::open(&db).unwrap();
        let n: i64 = c
            .query_row("SELECT COUNT(*) FROM activity", [], |rr| rr.get(0))
            .unwrap();
        assert_eq!(n, n_activity as i64);
        let ec: i64 = c
            .query_row(
                "SELECT event_count FROM sessions WHERE session_id = ?1",
                rusqlite::params![sid],
                |rr| rr.get(0),
            )
            .unwrap();
        assert_eq!(ec, event_count);
        let nr: i64 = c
            .query_row(
                "SELECT COUNT(*) FROM session_resources WHERE session_id = ?1",
                rusqlite::params![sid],
                |rr| rr.get(0),
            )
            .unwrap();
        assert_eq!(nr, n_resources as i64);
    }
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn search_scale_single_traversal_regression() {
    // 300 single-event sessions with distinct app tokens: one token matches
    // exactly one session; the discovery traversal stays correct and bounded
    // (limit respected) at scale.
    let store = mem_small();
    for i in 0..300 {
        let p = if i % 2 == 0 {
            proj(A_ID, "A")
        } else {
            proj(B_ID, "B")
        };
        store
            .append(
                "focus",
                "hyprland",
                &ctx_full(
                    (i as i64) * 5000,
                    &format!("0x{i}"),
                    &format!("apptoken{i:03}"),
                    "t",
                    "1",
                    None,
                    Some(p),
                ),
            )
            .unwrap();
    }
    assert_eq!(store.recent_sessions(1000).unwrap().len(), 300);
    let hits = store
        .search_sessions(&SessionSearchQuery {
            application: Some("apptoken042".to_string()),
            limit: 5,
            ..Default::default()
        })
        .unwrap();
    assert_eq!(hits.len(), 1);
    assert!(hits[0]
        .session
        .applications
        .contains(&"apptoken042".to_string()));
    assert_eq!(hits[0].matched_at_ms, Some(42 * 5000));
}
