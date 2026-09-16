//! Phase 4 deterministic work-session integration tests.
//!
//! Covers the required Rust surface: continuous work, app switching,
//! project switch, gap boundary, brief/long unresolved interruptions,
//! unresolved-only, resource aggregation + portable identity, restart
//! continuation, stale current-session, recent/range/project/last/resources/
//! events queries, CLI JSON/missing/validation/config, and
//! transactional rollback on failing sessionization.

use qs_agent_orchestrator::desktop_context::{
    DesktopContext, FocusedWindow, ProjectContext, ResourceContext, Source, Workspace,
};
use qs_agent_orchestrator::desktop_session::SessionConfig;
use qs_agent_orchestrator::ActivityStore;
use std::path::{Path, PathBuf};
use std::process::Command;

const A_ID: &str = "11111111-1111-1111-1111-111111111111";
const B_ID: &str = "22222222-2222-2222-2222-222222222222";
const UNKNOWN_ID: &str = "99999999-9999-9999-9999-999999999999";

fn tmpdir(tag: &str) -> PathBuf {
    let p = std::env::temp_dir().join(format!(
        "qs-sess-{tag}-{}-{}",
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
    (
        out.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&out.stdout).to_string(),
        String::from_utf8_lossy(&out.stderr).to_string(),
    )
}

fn proj(id: &str, name: &str) -> ProjectContext {
    ProjectContext::new(id, name, "file")
}

fn ctx(
    ts: i64,
    win: &str,
    app: &str,
    title: &str,
    res: Option<ResourceContext>,
    p: Option<ProjectContext>,
) -> DesktopContext {
    let base = DesktopContext::available(
        Source::Hyprland,
        Some(FocusedWindow::new(win, app, title)),
        Some(Workspace::new("1", "1")),
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

fn is_hex32(s: &str) -> bool {
    s.len() == 32 && s.chars().all(|c| c.is_ascii_hexdigit())
}

#[test]
fn continuous_one_project_single_session() {
    let store = mem_small();
    for (i, ts) in [0, 100, 200, 500].iter().enumerate() {
        store
            .append(
                "focus",
                "hyprland",
                &ctx(
                    *ts,
                    &format!("0x{i}"),
                    "kitty",
                    "t",
                    None,
                    Some(proj(A_ID, "Alpha")),
                ),
            )
            .unwrap();
    }
    let sessions = store.recent_sessions(10).unwrap();
    assert_eq!(sessions.len(), 1);
    let s = &sessions[0];
    assert_eq!(s.project_id.as_deref(), Some(A_ID));
    assert_eq!(s.event_count, 4);
    assert_eq!(s.start_ms, 0);
    assert_eq!(s.end_ms, 500);
    assert_eq!(s.status, "open");
    assert!(s.ended_reason.is_none());
    assert_eq!(s.gap_ms, 1000);
    assert_eq!(s.interruption_ms, 200);
    assert!(is_hex32(&s.session_id));
    assert!(is_hex32(&s.device_id));
    // Events all point at the session, oldest-first.
    let ev = store.session_events(&s.session_id, 10).unwrap();
    assert_eq!(ev.len(), 4);
    assert!(ev.windows(2).all(|w| w[0].id < w[1].id));
    assert!(ev
        .iter()
        .all(|r| r.session_id.as_deref() == Some(s.session_id.as_str())));
}

#[test]
fn app_window_workspace_resource_changes_never_split_same_project() {
    let store = mem_small();
    let r1 = ResourceContext::new(
        "neovim",
        Some("/tmp/a.md"),
        Some("/tmp"),
        None,
        None,
        None,
        None,
        None,
    );
    let r2 = ResourceContext::new(
        "kitty",
        None,
        Some("/tmp/other"),
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
            &ctx(0, "0x1", "kitty", "t1", Some(r1), Some(proj(A_ID, "A"))),
        )
        .unwrap();
    // Different app/title/window/workspace/resource, same project.
    let c2 = DesktopContext::available(
        Source::Hyprland,
        Some(FocusedWindow::new("0x2", "zen", "other title")),
        Some(Workspace::new("2", "2")),
        100,
    )
    .with_resource(Some(r2))
    .with_project(Some(proj(A_ID, "A")));
    store.append("focus", "hyprland", &c2).unwrap();
    let sessions = store.recent_sessions(10).unwrap();
    assert_eq!(sessions.len(), 1);
    assert_eq!(sessions[0].event_count, 2);
    assert!(sessions[0].applications.contains(&"kitty".to_string()));
    assert!(sessions[0].applications.contains(&"zen".to_string()));
}

#[test]
fn project_switch_splits_immediately() {
    let store = mem_small();
    store
        .append(
            "focus",
            "hyprland",
            &ctx(0, "0x1", "kitty", "a", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx(100, "0x2", "kitty", "b", None, Some(proj(B_ID, "B"))),
        )
        .unwrap();
    let sessions = store.recent_sessions(10).unwrap();
    assert_eq!(sessions.len(), 2);
    // Newest-first by end.
    assert_eq!(sessions[0].project_id.as_deref(), Some(B_ID));
    assert_eq!(sessions[0].status, "open");
    assert_eq!(sessions[1].project_id.as_deref(), Some(A_ID));
    assert_eq!(sessions[1].status, "closed");
    assert_eq!(sessions[1].ended_reason.as_deref(), Some("project_switch"));
    assert_ne!(sessions[0].session_id, sessions[1].session_id);
}

#[test]
fn inactivity_gap_exact_boundary_splits() {
    let store = mem_small();
    store
        .append(
            "focus",
            "hyprland",
            &ctx(0, "0x1", "k", "a", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx(999, "0x2", "k", "b", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    assert_eq!(
        store.recent_sessions(10).unwrap().len(),
        1,
        "999 < 1000 continues"
    );
    // Exact boundary >= gap finalizes.
    store
        .append(
            "focus",
            "hyprland",
            &ctx(1999, "0x3", "k", "c", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    // 1999 - 999 = 1000 >= gap => split.
    let sessions = store.recent_sessions(10).unwrap();
    assert_eq!(sessions.len(), 2);
    assert_eq!(sessions[1].ended_reason.as_deref(), Some("inactivity"));
}

#[test]
fn brief_unresolved_interruption_continues() {
    let store = mem_small();
    store
        .append(
            "focus",
            "hyprland",
            &ctx(0, "0x1", "k", "a", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    let un = DesktopContext::unavailable(Source::Hyprland, 100);
    store.append("availability", "hyprland", &un).unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx(150, "0x1", "k", "a", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    let sessions = store.recent_sessions(10).unwrap();
    assert_eq!(sessions.len(), 1, "short run absorbed");
    assert_eq!(sessions[0].event_count, 3);
}

#[test]
fn long_unresolved_run_splits_online_no_retroactive_move() {
    let store = mem_small();
    store
        .append(
            "focus",
            "hyprland",
            &ctx(0, "0x1", "k", "a", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    store
        .append(
            "availability",
            "hyprland",
            &DesktopContext::unavailable(Source::Hyprland, 100),
        )
        .unwrap();
    // 300 - 100 = 200 >= grace 200 => split at this subsequent unresolved event.
    store
        .append(
            "availability",
            "hyprland",
            &DesktopContext::unavailable(Source::Hyprland, 300),
        )
        .unwrap();
    let sessions = store.recent_sessions(10).unwrap();
    assert_eq!(sessions.len(), 2);
    // Earlier interruption stays in prior session (no lookahead reassignment).
    let first = sessions.iter().find(|s| s.start_ms == 0).unwrap();
    assert_eq!(first.event_count, 2, "A + first unresolved stay together");
    assert_eq!(first.ended_reason.as_deref(), Some("interruption"));
    let second = sessions.iter().find(|s| s.start_ms == 300).unwrap();
    assert!(
        second.project_id.is_none(),
        "new session is unresolved-only"
    );

    // Return-to-same long run also splits.
    let store2 = mem_small();
    store2
        .append(
            "focus",
            "hyprland",
            &ctx(0, "0x1", "k", "a", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    store2
        .append(
            "availability",
            "hyprland",
            &DesktopContext::unavailable(Source::Hyprland, 100),
        )
        .unwrap();
    store2
        .append(
            "focus",
            "hyprland",
            &ctx(350, "0x1", "k", "a", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    let ss = store2.recent_sessions(10).unwrap();
    assert_eq!(ss.len(), 2);
    assert_eq!(ss.iter().find(|s| s.start_ms == 0).unwrap().event_count, 2);
    assert_eq!(
        ss.iter()
            .find(|s| s.start_ms == 350)
            .unwrap()
            .project_id
            .as_deref(),
        Some(A_ID)
    );
}

#[test]
fn unresolved_session_to_resolved_starts_new() {
    let store = mem_small();
    store
        .append(
            "availability",
            "hyprland",
            &DesktopContext::unavailable(Source::Hyprland, 0),
        )
        .unwrap();
    store
        .append(
            "availability",
            "hyprland",
            &DesktopContext::unavailable(Source::Hyprland, 100),
        )
        .unwrap();
    assert_eq!(store.recent_sessions(10).unwrap().len(), 1);
    store
        .append(
            "focus",
            "hyprland",
            &ctx(200, "0x1", "k", "a", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    let ss = store.recent_sessions(10).unwrap();
    assert_eq!(ss.len(), 2);
}

#[test]
fn unresolved_only_stays_one_session_unless_gap() {
    let store = mem_small();
    for ts in [0, 100, 200] {
        store
            .append(
                "availability",
                "hyprland",
                &DesktopContext::unavailable(Source::Hyprland, ts),
            )
            .unwrap();
    }
    let ss = store.recent_sessions(10).unwrap();
    assert_eq!(ss.len(), 1);
    assert!(ss[0].project_id.is_none());
}

#[test]
fn resource_aggregation_portable_identity_dedups() {
    let store = mem_small();
    // File under git_root => portable file:note.md regardless of adapter/branch/title.
    let mk = |adapter: &str, branch: &str, title: &str| {
        ResourceContext::new(
            adapter,
            Some("/repo/note.md"),
            Some("/repo"),
            Some("/repo"),
            Some(branch),
            None,
            None,
            Some(title),
        )
    };
    store
        .append(
            "focus",
            "hyprland",
            &ctx(
                0,
                "0x1",
                "nvim",
                "t",
                Some(mk("neovim", "main", "T1")),
                Some(proj(A_ID, "A")),
            ),
        )
        .unwrap();
    store
        .append(
            "context",
            "hyprland",
            &ctx(
                100,
                "0x1",
                "nvim",
                "t2",
                Some(mk("kitty", "feature", "T2")),
                Some(proj(A_ID, "A")),
            ),
        )
        .unwrap();
    // Same portable file with different cwd but same file => same entry.
    let sessions = store.recent_sessions(10).unwrap();
    assert_eq!(sessions.len(), 1);
    let res = store
        .session_resources(&sessions[0].session_id, 10)
        .unwrap();
    assert_eq!(res.len(), 1, "portable identity dedups: {res:?}");
    let r = &res[0];
    assert_eq!(r.kind, "portable");
    assert_eq!(r.portable_identity.as_deref(), Some("file:note.md"));
    assert!(r.local_identity.is_none());
    assert_eq!(r.occurrence_count, 2);
    assert_eq!(r.first_seen_ms, 0);
    assert_eq!(r.last_seen_ms, 100);
    assert!(r.first_activity_id < r.last_activity_id);
    // Latest compact metadata retained (branch/title from newest).
    assert_eq!(r.resource.git_branch.as_deref(), Some("feature"));
    // Machine-local absolute file is distinct from portable.
    store
        .append(
            "focus",
            "hyprland",
            &ctx(
                200,
                "0x2",
                "k",
                "t",
                Some(ResourceContext::new(
                    "kitty",
                    Some("/tmp/local.md"),
                    Some("/tmp"),
                    None,
                    None,
                    None,
                    None,
                    None,
                )),
                Some(proj(A_ID, "A")),
            ),
        )
        .unwrap();
    let res2 = store
        .session_resources(&sessions[0].session_id, 10)
        .unwrap();
    assert_eq!(res2.len(), 2);
    let local = res2.iter().find(|x| x.kind == "local").unwrap();
    assert_eq!(local.local_identity.as_deref(), Some("file:/tmp/local.md"));
    assert!(local.portable_identity.is_none());
}

#[test]
fn restart_reopen_continues_same_session() {
    let dir = tmpdir("restart");
    let db = dir.join("activity.db");
    let cfg = small_cfg();
    let sid = {
        let store = ActivityStore::open_with_session_config(&db, cfg).unwrap();
        store
            .append(
                "focus",
                "hyprland",
                &ctx(0, "0x1", "k", "a", None, Some(proj(A_ID, "A"))),
            )
            .unwrap();
        store.recent_sessions(10).unwrap()[0].session_id.clone()
    };
    // Reopen (simulated restart) shortly after: identical startup row continues.
    {
        let store = ActivityStore::open_with_session_config(&db, cfg).unwrap();
        store
            .append(
                "focus",
                "hyprland",
                &ctx(100, "0x1", "k", "a", None, Some(proj(A_ID, "A"))),
            )
            .unwrap();
        let ss = store.recent_sessions(10).unwrap();
        assert_eq!(ss.len(), 1);
        assert_eq!(ss[0].session_id, sid, "stable ID survives restart");
        assert_eq!(ss[0].event_count, 2);
    }
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn stale_current_session_returns_null_without_mutation() {
    let store = mem_small();
    store
        .append(
            "focus",
            "hyprland",
            &ctx(0, "0x1", "k", "a", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    // end=0, gap=1000: active at 999, stale at 1000.
    assert!(store.current_session(999).unwrap().is_some());
    assert!(
        store.current_session(1000).unwrap().is_none(),
        "boundary >= gap is inactive"
    );
    // Read-only staleness does not finalize: still one open session.
    let ss = store.recent_sessions(10).unwrap();
    assert_eq!(ss.len(), 1);
    assert_eq!(ss[0].status, "open");
}

#[test]
fn session_query_ordering_and_filters() {
    let store = mem_small();
    // Session 1: A at 0..100 (closed by switch). Session 2: B at 5000. Session 3: A at 6000.
    store
        .append(
            "focus",
            "hyprland",
            &ctx(0, "0x1", "k", "a1", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx(5000, "0x2", "k", "b", None, Some(proj(B_ID, "B"))),
        )
        .unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx(6000, "0x3", "k", "a2", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    // Recent newest-first by end.
    let recent = store.recent_sessions(10).unwrap();
    assert_eq!(recent.len(), 3);
    assert_eq!(recent[0].start_ms, 6000);
    assert_eq!(recent[1].start_ms, 5000);
    assert_eq!(recent[2].start_ms, 0);
    // Project filter.
    let for_a = store.recent_sessions_for_project(A_ID, 10).unwrap();
    assert_eq!(for_a.len(), 2);
    // Last session per project.
    assert_eq!(
        store
            .last_session_for_project(A_ID)
            .unwrap()
            .unwrap()
            .start_ms,
        6000
    );
    assert!(store
        .last_session_for_project(UNKNOWN_ID)
        .unwrap()
        .is_none());
    // Range overlap: [4000,5500) overlaps only B (end 5000 >= 4000 and start 5000 < 5500).
    let range = store.sessions_in_range(4000, 5500, 10).unwrap();
    assert_eq!(range.len(), 1);
    assert_eq!(range[0].project_id.as_deref(), Some(B_ID));
    // Range oldest-first.
    let all_range = store.sessions_in_range(0, 7000, 10).unwrap();
    assert_eq!(all_range.len(), 3);
    assert_eq!(all_range[0].start_ms, 0);
    // Events oldest-first; resources newest-first tested elsewhere.
    let sid_b = recent[1].session_id.clone();
    let ev = store.session_events(&sid_b, 10).unwrap();
    assert_eq!(ev.len(), 1);
}

#[test]
fn cli_session_json_missing_validation() {
    let dir = tmpdir("cli-sess");
    let missing = dir.join("nope").join("activity.db");
    let m = missing.to_string_lossy().to_string();
    // Missing DB shapes: [] / null, exit 0.
    let (code, out, _) = run_bin(&["--db", &m, "current-session"], &[], &[]);
    assert_eq!(code, 0);
    assert_eq!(out.trim(), "null");
    let (code, out, _) = run_bin(&["--db", &m, "sessions"], &[], &[]);
    assert_eq!(code, 0);
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&out).unwrap(),
        serde_json::json!([])
    );
    let (code, out, _) = run_bin(&["--db", &m, "last-session", "--project", A_ID], &[], &[]);
    assert_eq!(code, 0);
    assert_eq!(out.trim(), "null");
    let fake_sid = "a".repeat(32);
    let (code, out, _) = run_bin(
        &["--db", &m, "session-resources", "--session", &fake_sid],
        &[],
        &[],
    );
    assert_eq!(code, 0);
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&out).unwrap(),
        serde_json::json!([])
    );
    let (code, out, _) = run_bin(
        &["--db", &m, "session-events", "--session", &fake_sid],
        &[],
        &[],
    );
    assert_eq!(code, 0);
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&out).unwrap(),
        serde_json::json!([])
    );
    // Validation before missing-DB fast path.
    let (code, _, _) = run_bin(&["--db", &m, "sessions", "--limit", "0"], &[], &[]);
    assert_ne!(code, 0);
    let (code, _, _) = run_bin(&["--db", &m, "last-session", "--project", "bad"], &[], &[]);
    assert_ne!(code, 0);
    let (code, _, _) = run_bin(
        &["--db", &m, "session-resources", "--session", "bad"],
        &[],
        &[],
    );
    assert_ne!(code, 0);
    let (code, _, _) = run_bin(
        &["--db", &m, "session-events", "--session", "bad"],
        &[],
        &[],
    );
    assert_ne!(code, 0);
    let (code, _, _) = run_bin(&["--db", &m, "session-resources"], &[], &[]);
    assert_eq!(code, 2);
    // Collect-only overrides rejected on queries.
    let (code, _, _) = run_bin(
        &["--db", &m, "sessions", "--session-gap-ms", "100"],
        &[],
        &[],
    );
    assert_eq!(code, 2);
    // Invalid collect config fails fast (no blocking).
    let (code, _, _) = run_bin(&["--db", &m, "collect", "--session-gap-ms", "0"], &[], &[]);
    assert_ne!(code, 0);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn cli_session_shapes_against_real_db() {
    let dir = tmpdir("cli-shapes-sess");
    let db = dir.join("activity.db");
    let m = db.to_string_lossy().to_string();
    let store = ActivityStore::open(&db).unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx(
                1000,
                "0x1",
                "kitty",
                "a",
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
    let (code, out, _) = run_bin(&["--db", &m, "current-session"], &[], &[]);
    assert_eq!(code, 0);
    // current-session may be stale (end 1000 + default 30min gap vs now) or active;
    // either null or a well-shaped object is acceptable, but shape must validate when present.
    let v: serde_json::Value = serde_json::from_str(&out).unwrap();
    if !v.is_null() {
        assert!(v.get("session_id").is_some());
        assert!(v.get("device_id").is_some());
        assert!(v.get("project").is_some());
        assert!(v.get("applications").is_some());
    }
    let (code, out, _) = run_bin(&["--db", &m, "sessions", "--limit", "5"], &[], &[]);
    assert_eq!(code, 0);
    let v: serde_json::Value = serde_json::from_str(&out).unwrap();
    assert_eq!(v.as_array().unwrap().len(), 1);
    assert_eq!(v[0]["session_id"], sid);
    assert!(v[0].get("event_count").is_some());
    // Query-time effective state is computed at one captured now per response.
    assert!(v[0].get("unresolved_start_ms").is_some());
    assert!(v[0].get("active").is_some());
    assert!(v[0]["effective_status"].is_string());
    let (code, out, _) = run_bin(&["--db", &m, "last-session", "--project", A_ID], &[], &[]);
    assert_eq!(code, 0);
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&out).unwrap()["session_id"],
        sid
    );
    let (code, out, _) = run_bin(
        &["--db", &m, "session-resources", "--session", &sid],
        &[],
        &[],
    );
    assert_eq!(code, 0);
    let v: serde_json::Value = serde_json::from_str(&out).unwrap();
    assert_eq!(v.as_array().unwrap().len(), 1);
    assert!(v[0].get("occurrence_count").is_some());
    assert!(v[0].get("first_seen_ms").is_some());
    let (code, out, _) = run_bin(&["--db", &m, "session-events", "--session", &sid], &[], &[]);
    assert_eq!(code, 0);
    let v: serde_json::Value = serde_json::from_str(&out).unwrap();
    assert_eq!(v.as_array().unwrap().len(), 1);
    assert!(v[0].get("event_id").is_some());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn append_rollback_when_sessionization_fails() {
    let dir = tmpdir("rollback");
    let db = dir.join("activity.db");
    let cfg = small_cfg();
    let store = ActivityStore::open_with_session_config(&db, cfg).unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx(0, "0x1", "k", "a", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    let before = store.recent_activity(10).unwrap().len();
    let before_sess = store.recent_sessions(10).unwrap().len();
    // Corrupt the open session thresholds directly through a second
    // connection while the store stays open (simulates failing
    // sessionization without tripping open-time validation).
    {
        let c = rusqlite::Connection::open(&db).unwrap();
        c.execute("UPDATE sessions SET gap_ms = 0", []).unwrap();
    }
    let bad = ctx(100, "0x2", "k", "b", None, Some(proj(A_ID, "A")));
    assert!(
        store.append("focus", "hyprland", &bad).is_err(),
        "corrupt thresholds must fail closed"
    );
    // Raw insert rolled back: nothing new persisted (raw queries avoid sessions).
    assert_eq!(store.recent_activity(10).unwrap().len(), before);
    // Session reads on the corrupt row fail closed (no silent wrong sessions).
    assert!(store.recent_sessions(10).is_err());
    drop(store);
    // A fresh writable open of the corrupt database rejects (deep exact
    // validation). Read-only opens by design (lightweight structural-only
    // contract); touching the corrupt row still fails closed via parsers.
    {
        use qs_agent_orchestrator::desktop_store::StoreError;
        assert!(matches!(
            ActivityStore::open_with_session_config(&db, cfg),
            Err(StoreError::IncompatibleSchema(_))
        ));
        let ro = ActivityStore::open_read_only(&db)
            .expect("read-only opens corrupt thresholds (structural-only)");
        assert!(ro.recent_sessions(10).is_err());
    }
    // Invalid project UUID on a clean DB also fails with nothing persisted.
    let dir2 = tmpdir("rollback-clean");
    let db2 = dir2.join("activity.db");
    let clean = ActivityStore::open_with_session_config(&db2, cfg).unwrap();
    clean
        .append(
            "focus",
            "hyprland",
            &ctx(0, "0x1", "k", "a", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    let before2 = clean.recent_activity(10).unwrap().len();
    let invalid = ctx(
        200,
        "0x3",
        "k",
        "c",
        None,
        Some(ProjectContext::new("not-a-uuid", "B", "file")),
    );
    assert!(clean.append("focus", "hyprland", &invalid).is_err());
    assert_eq!(clean.recent_activity(10).unwrap().len(), before2);
    let _ = std::fs::remove_dir_all(&dir);
    let _ = std::fs::remove_dir_all(&dir2);
    let _ = before_sess;
}

fn checkpoint(path: &Path) {
    let c = rusqlite::Connection::open(path).unwrap();
    let _ = c.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);");
}

/// Inserts a coherent foreign-device session plus its foreign activity row.
/// Returns (foreign session id, foreign device id, foreign event id).
fn insert_foreign_session_with_activity(
    db: &Path,
    observed_at_ms: i64,
) -> (String, String, String) {
    let foreign_sid = "ffffffffffffffffffffffffffffffff".to_string();
    let foreign_dev = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee".to_string();
    let foreign_eid = "dddddddddddddddddddddddddddddddd".to_string();
    let snapshot = serde_json::json!({
        "focused_window": null,
        "workspace": null,
        "available": false,
        "source": "hyprland",
        "observed_at_ms": observed_at_ms,
        "resource": null,
        "project": null,
    })
    .to_string();
    let c = rusqlite::Connection::open(db).unwrap();
    c.execute(
        "INSERT INTO activity (observed_at_ms, kind, source, snapshot_json, project_id,
         event_id, device_id, session_id)
         VALUES (?1, 'focus', 'hyprland', ?2, NULL, ?3, ?4, ?5)",
        rusqlite::params![
            observed_at_ms,
            snapshot,
            foreign_eid,
            foreign_dev,
            foreign_sid
        ],
    )
    .unwrap();
    let row_id = c.last_insert_rowid();
    // Coherent v4 requires a matching FTS row (rowid = activity.id).
    c.execute(
        "INSERT INTO activity_fts(rowid, session_id, resource_key, project, application, window_title, workspace, resource, metadata) VALUES (?1, ?2, '', '', '', '', '', '', 'focus hyprland')",
        rusqlite::params![row_id, foreign_sid],
    )
    .unwrap();
    c.execute(
        "INSERT INTO sessions (session_id, project_id, project_name, start_ms, end_ms,
         first_activity_id, last_activity_id, event_count, status, ended_reason,
         gap_ms, interruption_ms, device_id, unresolved_start_ms, applications_json)
         VALUES (?1, NULL, NULL, ?2, ?2, ?3, ?3, 1, 'open', NULL, 1000, 200, ?4, NULL, '[]')",
        rusqlite::params![foreign_sid, observed_at_ms, row_id, foreign_dev],
    )
    .unwrap();
    drop(c);
    checkpoint(db);
    (foreign_sid, foreign_dev, foreign_eid)
}

#[test]
fn foreign_open_session_is_neither_closed_nor_extended() {
    // A foreign-device open session is cross-device history: the local
    // writer must not close it, extend it, or report it as current, and a
    // second local open must not be treated as corruption.
    let dir = tmpdir("multidevice");
    let db = dir.join("activity.db");
    let cfg = small_cfg();
    let store = ActivityStore::open_with_session_config(&db, cfg).unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx(0, "0x1", "k", "a", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    let local_sid = store.recent_sessions(10).unwrap()[0].session_id.clone();
    let (foreign_sid, foreign_dev, _foreign_eid) = insert_foreign_session_with_activity(&db, 50);
    // Local append continues the LOCAL session; the foreign rows are untouched.
    store
        .append(
            "focus",
            "hyprland",
            &ctx(100, "0x2", "k", "b", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    let local = store.session_by_id(&local_sid).unwrap().unwrap();
    assert_eq!(local.event_count, 2);
    assert_eq!(local.status, "open");
    let foreign = store.session_by_id(&foreign_sid).unwrap().unwrap();
    assert_eq!(foreign.status, "open");
    assert_eq!(foreign.event_count, 1);
    assert_eq!(foreign.end_ms, 50);
    assert_eq!(foreign.device_id, foreign_dev);
    let foreign_events = store.session_events(&foreign_sid, 10).unwrap();
    assert_eq!(foreign_events.len(), 1);
    assert_eq!(
        foreign_events[0].device_id.as_deref(),
        Some(foreign_dev.as_str())
    );
    // Current means local-device current.
    let cur = store.current_session(150).unwrap().unwrap();
    assert_eq!(cur.session_id, local_sid);
    drop(store);
    // Reopen tolerates the foreign open (no repair, no corruption error).
    let store2 = ActivityStore::open_with_session_config(&db, cfg).unwrap();
    assert_eq!(store2.recent_sessions(10).unwrap().len(), 2);
    assert_eq!(
        store2.session_by_id(&foreign_sid).unwrap().unwrap().status,
        "open"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn foreign_session_with_activity_passes_both_opens_untouched_by_append() {
    // Well-formed foreign-device history (session + activity, devices
    // agreeing with each other but not the local singleton) passes strict
    // validation on both read-only and writable opens, and a local append
    // leaves every foreign byte intact.
    let dir = tmpdir("multidevice-full");
    let db = dir.join("activity.db");
    let cfg = small_cfg();
    let store = ActivityStore::open_with_session_config(&db, cfg).unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx(0, "0x1", "k", "a", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    let local_sid = store.recent_sessions(10).unwrap()[0].session_id.clone();
    drop(store);
    let (foreign_sid, foreign_dev, _foreign_eid) = insert_foreign_session_with_activity(&db, 50);
    // Both opens accept coherent foreign history.
    let ro = ActivityStore::open_read_only(&db).unwrap();
    assert_eq!(ro.recent_sessions(10).unwrap().len(), 2);
    drop(ro);
    let store = ActivityStore::open_with_session_config(&db, cfg).unwrap();
    assert_eq!(store.recent_sessions(10).unwrap().len(), 2);
    // Local append: only local rows change.
    let before_foreign_session = store.session_by_id(&foreign_sid).unwrap().unwrap();
    let before_foreign_events = store.session_events(&foreign_sid, 10).unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx(100, "0x2", "k", "b", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    assert_eq!(
        store
            .session_by_id(&local_sid)
            .unwrap()
            .unwrap()
            .event_count,
        2
    );
    assert_eq!(
        store.session_by_id(&foreign_sid).unwrap().unwrap(),
        before_foreign_session
    );
    assert_eq!(
        store.session_events(&foreign_sid, 10).unwrap(),
        before_foreign_events
    );
    // Cross-device listing still shows both sessions.
    assert_eq!(store.recent_sessions(10).unwrap().len(), 2);
    drop(store);
    // Reopen keeps tolerating the foreign pair.
    let store2 = ActivityStore::open_with_session_config(&db, cfg).unwrap();
    assert_eq!(store2.recent_sessions(10).unwrap().len(), 2);
    drop(store2);
    let _ = foreign_dev;
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn activity_session_device_mismatch_rejects_untouched() {
    // An event pointing at another device's session is corruption, not sync:
    // the writable open rejects (deep validation) and the database is
    // byte-unchanged. Read-only opens by design (lightweight structural-only
    // contract) without mutating.
    use qs_agent_orchestrator::desktop_store::StoreError;

    let dir = tmpdir("multidevice-mismatch");
    let db = dir.join("activity.db");
    let cfg = small_cfg();
    let store = ActivityStore::open_with_session_config(&db, cfg).unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx(0, "0x1", "k", "a", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    drop(store);
    let (_foreign_sid, _foreign_dev, _foreign_eid) = insert_foreign_session_with_activity(&db, 50);
    let local_dev = ActivityStore::open_read_only(&db)
        .unwrap()
        .device_id()
        .unwrap();
    // Point the foreign event at the LOCAL device while its session stays
    // foreign: devices disagree across the reference.
    {
        let c = rusqlite::Connection::open(&db).unwrap();
        c.execute(
            "UPDATE activity SET device_id = ?1 WHERE observed_at_ms = 50",
            rusqlite::params![local_dev],
        )
        .unwrap();
    }
    checkpoint(&db);
    let before = std::fs::read(&db).unwrap();
    match ActivityStore::open_with_session_config(&db, cfg) {
        Err(StoreError::IncompatibleSchema(_)) => {}
        Err(e) => panic!("writable open must reject IncompatibleSchema, got {e:?}"),
        Ok(_) => panic!("writable open must reject device mismatch"),
    }
    match ActivityStore::open_read_only(&db) {
        Ok(ro) => {
            // Structural-only contract: opens, never mutates.
            assert_eq!(ro.schema_version().unwrap(), 4);
            drop(ro);
        }
        Err(e) => panic!("read-only lightweight open must tolerate device mismatch, got {e:?}"),
    }
    let after = std::fs::read(&db).unwrap();
    assert_eq!(before, after, "rejected DB must be byte-unchanged");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn effective_status_tracks_interruption_exact_boundary() {
    // gap 1000, grace 200. Unresolved run starts at 100.
    let store = mem_small();
    store
        .append(
            "focus",
            "hyprland",
            &ctx(0, "0x1", "k", "a", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    store
        .append(
            "availability",
            "hyprland",
            &DesktopContext::unavailable(Source::Hyprland, 100),
        )
        .unwrap();
    let s = store.recent_sessions(10).unwrap()[0].clone();
    assert_eq!(s.unresolved_start_ms, Some(100));
    assert_eq!(s.effective_status_at(99), "active");
    assert!(s.is_active_at(99));
    assert_eq!(s.effective_status_at(299), "active");
    // 300 - 100 = 200 >= grace 200: interrupted, hence non-current.
    assert_eq!(s.effective_status_at(300), "interrupted");
    assert!(!s.is_active_at(300));
    assert!(store.current_session(299).unwrap().is_some());
    assert!(store.current_session(300).unwrap().is_none());
    // Gap expiry dominates an open run (writer checks gap first): stale.
    assert_eq!(s.effective_status_at(1100), "stale");
    assert!(!s.is_active_at(1100));
    // Closed sessions report closed regardless of timestamps.
    store
        .append(
            "focus",
            "hyprland",
            &ctx(400, "0x2", "k", "b", None, Some(proj(B_ID, "B"))),
        )
        .unwrap();
    let closed = store
        .recent_sessions(10)
        .unwrap()
        .into_iter()
        .find(|x| x.start_ms == 0)
        .unwrap();
    assert_eq!(closed.status, "closed");
    assert_eq!(closed.effective_status_at(150), "closed");
    assert!(!closed.is_active_at(150));
}

#[test]
fn effective_status_grace_zero_deactivates_at_first_unresolved() {
    let cfg = SessionConfig::new(1000, 0).unwrap();
    let store = ActivityStore::open_in_memory_with_config(cfg).unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx(0, "0x1", "k", "a", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    assert!(store.current_session(0).unwrap().is_some());
    store
        .append(
            "availability",
            "hyprland",
            &DesktopContext::unavailable(Source::Hyprland, 100),
        )
        .unwrap();
    let s = store.recent_sessions(10).unwrap()[0].clone();
    assert_eq!(s.unresolved_start_ms, Some(100));
    // grace 0: now - start = 0 >= 0 at the event itself.
    assert_eq!(s.effective_status_at(100), "interrupted");
    assert!(store.current_session(100).unwrap().is_none());
    // The next event proves the run and splits.
    store
        .append(
            "availability",
            "hyprland",
            &DesktopContext::unavailable(Source::Hyprland, 150),
        )
        .unwrap();
    assert_eq!(store.recent_sessions(10).unwrap().len(), 2);
}

#[test]
fn resource_identity_anchored_across_repos() {
    let store = mem_small();
    let repo_file = |file: &str, root: &str| {
        ResourceContext::new(
            "neovim",
            Some(file),
            Some(root),
            Some(root),
            None,
            None,
            None,
            None,
        )
    };
    // Resolved event in project A: portable project-anchored key.
    store
        .append(
            "focus",
            "hyprland",
            &ctx(
                0,
                "0x1",
                "nvim",
                "t",
                Some(repo_file("/repo/note.md", "/repo")),
                Some(proj(A_ID, "A")),
            ),
        )
        .unwrap();
    // Absorbed unresolved event, same relative path, different root, no
    // project and no remote: explicitly local WITH the root embedded, so it
    // cannot collide with the portable entry above.
    let unprojected = DesktopContext::available(
        Source::Hyprland,
        Some(FocusedWindow::new("0x2", "nvim", "t")),
        Some(Workspace::new("1", "1")),
        100,
    )
    .with_resource(Some(repo_file("/other/note.md", "/other")));
    store.append("focus", "hyprland", &unprojected).unwrap();
    // Same rel path under a third root but with a shared remote: portable
    // remote-anchored, distinct from both entries above.
    let remote_res =
        repo_file("/third/note.md", "/third").with_git_remote(Some("https://github.com/o/r"));
    let remote_ctx = DesktopContext::available(
        Source::Hyprland,
        Some(FocusedWindow::new("0x3", "nvim", "t")),
        Some(Workspace::new("1", "1")),
        150,
    )
    .with_resource(Some(remote_res));
    store.append("focus", "hyprland", &remote_ctx).unwrap();
    // All three absorbed into one session...
    let sessions = store.recent_sessions(10).unwrap();
    assert_eq!(sessions.len(), 1);
    // ...but aggregated as three distinct resources.
    let res = store
        .session_resources(&sessions[0].session_id, 10)
        .unwrap();
    assert_eq!(
        res.len(),
        3,
        "cross-repo rel paths must stay distinct: {res:?}"
    );
    let portable = res
        .iter()
        .find(|r| {
            r.kind == "portable"
                && r.portable_identity.as_deref() == Some("file:note.md")
                && r.resource_key.contains(&A_ID.to_lowercase())
        })
        .expect("project-anchored entry");
    assert_eq!(portable.occurrence_count, 1);
    let locals: Vec<_> = res.iter().filter(|r| r.kind == "local").collect();
    assert_eq!(locals.len(), 1);
    assert!(locals[0]
        .local_identity
        .as_deref()
        .unwrap()
        .contains("/other"));
    assert_eq!(locals[0].occurrence_count, 1);
    let remote = res
        .iter()
        .find(|r| r.resource_key.contains("https://github.com/o/r"))
        .expect("remote-anchored entry");
    assert_eq!(remote.kind, "portable");
    // Same repo + same remote + same rel dedups despite title changes.
    let remote_ctx2 = DesktopContext::available(
        Source::Hyprland,
        Some(FocusedWindow::new("0x3", "nvim", "other title")),
        Some(Workspace::new("1", "1")),
        180,
    )
    .with_resource(Some(
        repo_file("/third/note.md", "/third").with_git_remote(Some("https://github.com/o/r")),
    ));
    store.append("context", "hyprland", &remote_ctx2).unwrap();
    let res2 = store
        .session_resources(&sessions[0].session_id, 10)
        .unwrap();
    assert_eq!(res2.len(), 3);
    assert_eq!(
        res2.iter()
            .find(|r| r.resource_key.contains("https://github.com/o/r"))
            .unwrap()
            .occurrence_count,
        2
    );
}

#[test]
fn clock_rollback_keeps_min_max_bounds() {
    let store = mem_small();
    let f = |ts: i64, win: &str| {
        ctx(
            ts,
            win,
            "nvim",
            "t",
            Some(ResourceContext::new(
                "neovim",
                Some("/repo/a.md"),
                Some("/repo"),
                Some("/repo"),
                None,
                None,
                None,
                None,
            )),
            Some(proj(A_ID, "A")),
        )
    };
    store.append("focus", "hyprland", &f(1000, "0x1")).unwrap();
    // Clock jumps backwards; the event still joins the session.
    store.append("focus", "hyprland", &f(500, "0x2")).unwrap();
    let s = store.recent_sessions(10).unwrap()[0].clone();
    assert_eq!(s.start_ms, 500, "start is min over rollback");
    assert_eq!(s.end_ms, 1000, "end is max over rollback");
    assert_eq!(s.event_count, 2);
    // Activity ids stay in insertion order.
    assert!(s.first_activity_id < s.last_activity_id);
    let ev = store.session_events(&s.session_id, 10).unwrap();
    assert_eq!(ev[0].observed_at_ms, 500);
    assert_eq!(ev[1].observed_at_ms, 1000);
    // Overlap finds the session through the rolled-back bound...
    assert_eq!(store.sessions_in_range(400, 600, 10).unwrap().len(), 1);
    // ...and the raw range finds the rolled-back event itself.
    assert_eq!(store.activity_in_range(400, 600, 10).unwrap().len(), 1);
    // Resource sightings track min/max; activity ids track insertion.
    let res = store.session_resources(&s.session_id, 10).unwrap();
    assert_eq!(res.len(), 1);
    assert_eq!(res[0].first_seen_ms, 500);
    assert_eq!(res[0].last_seen_ms, 1000);
    assert_eq!(res[0].occurrence_count, 2);
    assert!(res[0].first_activity_id < res[0].last_activity_id);
}

#[test]
fn session_queries_ignore_malformed_session_env() {
    // Read-only session queries never consult session env; malformed values
    // must not break them (config applies to the collect path only).
    let dir = tmpdir("cli-env");
    let db = dir.join("activity.db");
    let m = db.to_string_lossy().to_string();
    let store = ActivityStore::open(&db).unwrap();
    store
        .append(
            "focus",
            "hyprland",
            &ctx(1000, "0x1", "k", "a", None, Some(proj(A_ID, "A"))),
        )
        .unwrap();
    drop(store);
    let bad_env = [
        ("QS_DESKTOP_SESSION_GAP_MS", "not-a-number"),
        ("QS_DESKTOP_SESSION_INTERRUPTION_MS", "also-bad"),
    ];
    let (code, _, _) = run_bin(&["--db", &m, "collect", "--session-gap-ms", "0"], &[], &[]);
    assert_ne!(code, 0, "out-of-bounds CLI gap must fail fast");
    // Malformed env fails before ANY filesystem/database mutation: collect
    // with a missing DB path exits non-zero and creates nothing.
    let missing2 = dir.join("never").join("activity.db");
    let m2 = missing2.to_string_lossy().to_string();
    let (code, _, _) = run_bin(&["--db", &m2, "collect"], &bad_env, &[]);
    assert_ne!(code, 0, "malformed env must fail collect");
    assert!(
        !missing2.exists(),
        "failed env validation must not create the database"
    );
    assert!(
        !dir.join("never").exists(),
        "failed env validation must not create parent dirs either"
    );
    let (code, out, _) = run_bin(&["--db", &m, "sessions"], &bad_env, &[]);
    assert_eq!(code, 0);
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&out)
            .unwrap()
            .as_array()
            .unwrap()
            .len(),
        1
    );
    let _ = std::fs::remove_dir_all(&dir);
}
