//! Session-change sidecar integration: start/current/closed/dirty/no-repo/
//! old-session/commit-range/repo-switch/drain-skip/failure-safety.
//!
//! The strict activity schema (v4) is untouched: all change state lives in
//! the private `<db>.changes.db` sidecar keyed by immutable `session_id`.
//! These tests drive `ActivityStore::append_with_outcome` (the same outcome
//! the collector's `flush_pending` hook consumes) plus
//! `session_changes::observe_session_change` with an explicit `repo_override`
//! so no registry file is needed.
//!
//! The global sidecar path is process-wide, so every test using it holds the
//! file-local mutex for the whole closure (no races under parallel runs).

use qs_agent_orchestrator::collector::final_drain;
use qs_agent_orchestrator::desktop_context::{
    DesktopContext, FocusedWindow, ProjectContext, Source, Workspace,
};
use qs_agent_orchestrator::desktop_session::SessionConfig;
use qs_agent_orchestrator::session_changes::{self, SessionChangeStore};
use qs_agent_orchestrator::{ActivityStore, Tracker};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

const A_ID: &str = "11111111-1111-1111-1111-111111111111";
const B_ID: &str = "22222222-2222-2222-2222-222222222222";

/// Serializes every test that touches the process-wide sidecar path.
fn sidecar_lock() -> &'static Mutex<()> {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| Mutex::new(()))
}

fn tmpdir(tag: &str) -> PathBuf {
    let p = std::env::temp_dir().join(format!(
        "qs-chgit-{tag}-{}-{}",
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

fn ctx(ts: i64, win: &str, proj: Option<&str>) -> DesktopContext {
    let base = DesktopContext::available(
        Source::Hyprland,
        Some(FocusedWindow::new(win, "kitty", "t")),
        Some(Workspace::new("1", "1")),
        ts,
    );
    match proj {
        Some(id) => base.with_project(Some(ProjectContext::new(id, "Alpha", "file"))),
        None => base,
    }
}

fn git(args: &[&str], cwd: &Path) {
    let st = std::process::Command::new("git")
        .args(args)
        .current_dir(cwd)
        .env("LC_ALL", "C")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_AUTHOR_NAME", "t")
        .env("GIT_AUTHOR_EMAIL", "t@t")
        .env("GIT_COMMITTER_NAME", "t")
        .env("GIT_COMMITTER_EMAIL", "t@t")
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .expect("spawn git");
    assert!(st.success(), "git {args:?} failed");
}

fn init_repo(tag: &str) -> PathBuf {
    let dir = tmpdir(tag);
    git(&["init", "-q"], &dir);
    git(&["config", "user.name", "t"], &dir);
    git(&["config", "user.email", "t@t"], &dir);
    std::fs::write(dir.join("note.md"), "one\n").unwrap();
    git(&["add", "note.md"], &dir);
    git(&["commit", "-qm", "init"], &dir);
    dir
}

fn sidecar_in(dir: &Path, tag: &str) -> PathBuf {
    dir.join(format!("{tag}.changes.db"))
}

fn with_sidecar(path: &Path, f: impl FnOnce()) {
    let _guard = sidecar_lock().lock().unwrap_or_else(|e| e.into_inner());
    session_changes::set_sidecar_path(Some(path.to_path_buf()));
    let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(f));
    session_changes::set_sidecar_path(None);
    if let Err(e) = r {
        std::panic::resume_unwind(e);
    }
}

fn observe(sid: &str, pid: Option<&str>, at: i64, is_new: bool, repo: &Path) {
    session_changes::observe_session_change(sid, pid, at, is_new, Some(repo));
}

#[test]
fn start_session_captures_baseline_immediately() {
    let repo = init_repo("start");
    let dir = tmpdir("start-side");
    let sc = sidecar_in(&dir, "start");
    let store =
        ActivityStore::open_in_memory_with_config(SessionConfig::new(60_000, 5_000).unwrap())
            .unwrap();
    let outcome = store
        .append_with_outcome("focus", "hyprland", &ctx(1000, "0x1", Some(A_ID)))
        .unwrap();
    assert!(outcome.is_new_session);
    with_sidecar(&sc, || {
        observe(
            &outcome.session_id,
            outcome.project_id.as_deref(),
            1000,
            outcome.is_new_session,
            &repo,
        );
        let back = SessionChangeStore::open(&sc).unwrap();
        let row = back.get(&outcome.session_id).unwrap().expect("row");
        assert!(row.has_baseline);
        assert!(row.baseline_commit.is_some());
        assert_eq!(row.baseline_commit, row.latest_commit);
        assert!(row.baseline_complete && row.latest_complete);
        assert_eq!(row.repo_status, "ok");
        assert!(!row.repo_gitdir.is_empty());
        assert!(row.reason.is_empty());
    });
    let _ = std::fs::remove_dir_all(&repo);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn current_session_latest_updates_while_active() {
    let repo = init_repo("current");
    let dir = tmpdir("current-side");
    let sc = sidecar_in(&dir, "current");
    let store =
        ActivityStore::open_in_memory_with_config(SessionConfig::new(60_000, 5_000).unwrap())
            .unwrap();
    let o1 = store
        .append_with_outcome("focus", "hyprland", &ctx(1000, "0x1", Some(A_ID)))
        .unwrap();
    with_sidecar(&sc, || {
        observe(
            &o1.session_id,
            o1.project_id.as_deref(),
            1000,
            o1.is_new_session,
            &repo,
        );
    });
    // New worktree edit, same session continues.
    std::fs::write(repo.join("note.md"), "one\ntwo\n").unwrap();
    let o2 = store
        .append_with_outcome("focus", "hyprland", &ctx(2000, "0x2", Some(A_ID)))
        .unwrap();
    assert!(!o2.is_new_session);
    assert_eq!(o1.session_id, o2.session_id);
    with_sidecar(&sc, || {
        // Force the refresh past the rate limit by backdating updated_at.
        {
            let back = SessionChangeStore::open(&sc).unwrap();
            back.conn_execute_backdate(&o1.session_id, 0);
        }
        observe(
            &o2.session_id,
            o2.project_id.as_deref(),
            2000,
            o2.is_new_session,
            &repo,
        );
        let back = SessionChangeStore::open(&sc).unwrap();
        let row = back.get(&o1.session_id).unwrap().expect("row");
        // Baseline frozen at creation; latest reflects the new edit.
        // Both patches are persisted separately with the attribution
        // limitation stated at read time.
        assert!(row.has_baseline);
        assert_ne!(row.baseline_diff, row.latest_diff);
        assert!(row.latest_diff.contains("two") || row.latest_status.contains("note.md"));
    });
    let _ = std::fs::remove_dir_all(&repo);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn clean_commit_during_session_freezes_patch() {
    // Clean baseline -> commit -> clean latest: the frozen commit range
    // must carry the actual session work, not just hashes.
    let repo = init_repo("ccrange");
    let dir = tmpdir("ccrange-side");
    let sc = sidecar_in(&dir, "ccrange");
    let store =
        ActivityStore::open_in_memory_with_config(SessionConfig::new(60_000, 5_000).unwrap())
            .unwrap();
    let o1 = store
        .append_with_outcome("focus", "hyprland", &ctx(1000, "0x1", Some(A_ID)))
        .unwrap();
    with_sidecar(&sc, || {
        observe(
            &o1.session_id,
            o1.project_id.as_deref(),
            1000,
            o1.is_new_session,
            &repo,
        );
    });
    std::fs::write(repo.join("note.md"), "one\nsession work\n").unwrap();
    git(&["add", "note.md"], &repo);
    git(&["commit", "-qm", "session work"], &repo);
    let o2 = store
        .append_with_outcome("focus", "hyprland", &ctx(2000, "0x2", Some(A_ID)))
        .unwrap();
    assert_eq!(o1.session_id, o2.session_id);
    with_sidecar(&sc, || {
        {
            let back = SessionChangeStore::open(&sc).unwrap();
            back.conn_execute_backdate(&o1.session_id, 0);
        }
        observe(
            &o2.session_id,
            o2.project_id.as_deref(),
            2000,
            o2.is_new_session,
            &repo,
        );
        let back = SessionChangeStore::open(&sc).unwrap();
        let row = back.get(&o1.session_id).unwrap().expect("row");
        assert!(row.latest_diff.is_empty(), "worktree clean");
        assert_ne!(
            row.baseline_commit, row.latest_commit,
            "HEAD advanced mid-session"
        );
        assert!(row.commit_complete, "note: {}", row.capture_note);
        assert!(
            row.commit_patch.contains("session work"),
            "patch: {}",
            row.commit_patch
        );
        assert!(
            row.commit_meta.contains("session work"),
            "meta: {}",
            row.commit_meta
        );
    });
    let _ = std::fs::remove_dir_all(&repo);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn closed_session_freezes_when_next_begins() {
    let repo = init_repo("closed");
    let dir = tmpdir("closed-side");
    let sc = sidecar_in(&dir, "closed");
    let store =
        ActivityStore::open_in_memory_with_config(SessionConfig::new(60_000, 5_000).unwrap())
            .unwrap();
    let o1 = store
        .append_with_outcome("focus", "hyprland", &ctx(1000, "0x1", Some(A_ID)))
        .unwrap();
    with_sidecar(&sc, || {
        observe(
            &o1.session_id,
            o1.project_id.as_deref(),
            1000,
            o1.is_new_session,
            &repo,
        );
    });
    let before = SessionChangeStore::open(&sc)
        .unwrap()
        .get(&o1.session_id)
        .unwrap()
        .expect("row")
        .latest_diff
        .clone();
    // Project switch splits: old session closes, new one starts.
    let o2 = store
        .append_with_outcome("focus", "hyprland", &ctx(2000, "0x2", Some(B_ID)))
        .unwrap();
    assert!(o2.is_new_session);
    assert_ne!(o1.session_id, o2.session_id);
    // More edits happen under the NEW session.
    std::fs::write(repo.join("note.md"), "one\nnew-session-edit\n").unwrap();
    with_sidecar(&sc, || {
        observe(
            &o2.session_id,
            o2.project_id.as_deref(),
            2000,
            o2.is_new_session,
            &repo,
        );
    });
    // Old session frozen: its latest never compared against today's HEAD.
    let back = SessionChangeStore::open(&sc).unwrap();
    let old = back.get(&o1.session_id).unwrap().expect("old row");
    assert_eq!(old.latest_diff, before);
    assert!(back.get(&o2.session_id).unwrap().is_some());
    let _ = std::fs::remove_dir_all(&repo);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn linked_folder_switch_freezes_row() {
    // Same session observed against a DIFFERENT repository: the row must
    // freeze as mismatched, never compare across repos.
    let repo_a = init_repo("switch-a");
    let repo_b = init_repo("switch-b");
    let dir = tmpdir("switch-side");
    let sc = sidecar_in(&dir, "switch");
    let store =
        ActivityStore::open_in_memory_with_config(SessionConfig::new(60_000, 5_000).unwrap())
            .unwrap();
    let o1 = store
        .append_with_outcome("focus", "hyprland", &ctx(1000, "0x1", Some(A_ID)))
        .unwrap();
    with_sidecar(&sc, || {
        observe(
            &o1.session_id,
            o1.project_id.as_deref(),
            1000,
            o1.is_new_session,
            &repo_a,
        );
    });
    let before = SessionChangeStore::open(&sc)
        .unwrap()
        .get(&o1.session_id)
        .unwrap()
        .expect("row");
    std::fs::write(repo_b.join("note.md"), "one\nforeign\n").unwrap();
    let o2 = store
        .append_with_outcome("focus", "hyprland", &ctx(2000, "0x2", Some(A_ID)))
        .unwrap();
    assert_eq!(o1.session_id, o2.session_id);
    with_sidecar(&sc, || {
        {
            let back = SessionChangeStore::open(&sc).unwrap();
            back.conn_execute_backdate(&o1.session_id, 0);
        }
        observe(
            &o2.session_id,
            o2.project_id.as_deref(),
            2000,
            o2.is_new_session,
            &repo_b,
        );
        let back = SessionChangeStore::open(&sc).unwrap();
        let row = back.get(&o1.session_id).unwrap().expect("row");
        assert_eq!(row.repo_status, "mismatch");
        assert_eq!(row.latest_diff, before.latest_diff);
        assert_eq!(row.baseline_diff, before.baseline_diff);
        assert!(!row.capture_note.is_empty());
    });
    let _ = std::fs::remove_dir_all(&repo_a);
    let _ = std::fs::remove_dir_all(&repo_b);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn failed_refresh_preserves_prior() {
    // Capture failure after a good baseline: prior snapshots stay, the note
    // says so explicitly (never an empty success).
    let repo = init_repo("failkeep");
    let dir = tmpdir("failkeep-side");
    let sc = sidecar_in(&dir, "failkeep");
    let store =
        ActivityStore::open_in_memory_with_config(SessionConfig::new(60_000, 5_000).unwrap())
            .unwrap();
    let o1 = store
        .append_with_outcome("focus", "hyprland", &ctx(1000, "0x1", Some(A_ID)))
        .unwrap();
    with_sidecar(&sc, || {
        observe(
            &o1.session_id,
            o1.project_id.as_deref(),
            1000,
            o1.is_new_session,
            &repo,
        );
    });
    std::fs::write(repo.join("note.md"), "one\nv2\n").unwrap();
    let o2 = store
        .append_with_outcome("focus", "hyprland", &ctx(2000, "0x2", Some(A_ID)))
        .unwrap();
    // Destroy the repo before the refresh: capture must fail honestly,
    // retaining the entire prior snapshot including its timestamp. Backdate
    // to an hour before the stored stamp: old enough to defeat the rate
    // limit, exact enough to prove retention verbatim.
    let _ = std::fs::remove_dir_all(&repo);
    let before = SessionChangeStore::open(&sc)
        .unwrap()
        .get(&o1.session_id)
        .unwrap()
        .expect("row");
    let aged = before.updated_at_ms - 3_600_000;
    with_sidecar(&sc, || {
        {
            let back = SessionChangeStore::open(&sc).unwrap();
            back.conn_execute_backdate(&o1.session_id, aged);
        }
        observe(
            &o2.session_id,
            o2.project_id.as_deref(),
            2000,
            o2.is_new_session,
            &repo,
        );
        let back = SessionChangeStore::open(&sc).unwrap();
        let row = back.get(&o1.session_id).unwrap().expect("row");
        assert_eq!(row.baseline_commit, row.latest_commit);
        assert_eq!(row.latest_commit, before.latest_commit);
        assert_eq!(row.updated_at_ms, aged, "capture timestamp retained");
        assert!(row.latest_complete, "stored snapshot stays marked complete");
        assert!(
            row.capture_note.contains("prior state"),
            "visible failure: {}",
            row.capture_note
        );
    });
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn repeated_incomplete_preserves_prior_then_defers() {
    // Oversized status (output cap) after a good snapshot: the prior
    // content is preserved (never an empty success), incompletes count
    // toward backoff, and the fourth consecutive failure defers with an
    // explicit note instead of burning git budget.
    let repo = init_repo("flood");
    let dir = tmpdir("flood-side");
    let sc = sidecar_in(&dir, "flood");
    let store =
        ActivityStore::open_in_memory_with_config(SessionConfig::new(60_000, 5_000).unwrap())
            .unwrap();
    let o1 = store
        .append_with_outcome("focus", "hyprland", &ctx(1000, "0x1", Some(A_ID)))
        .unwrap();
    with_sidecar(&sc, || {
        observe(
            &o1.session_id,
            o1.project_id.as_deref(),
            1000,
            o1.is_new_session,
            &repo,
        );
    });
    // Good latest first so there is prior content worth preserving.
    std::fs::write(repo.join("note.md"), "one\nv2\n").unwrap();
    let o2 = store
        .append_with_outcome("focus", "hyprland", &ctx(2000, "0x2", Some(A_ID)))
        .unwrap();
    with_sidecar(&sc, || {
        {
            let back = SessionChangeStore::open(&sc).unwrap();
            back.conn_execute_backdate(&o1.session_id, 0);
        }
        observe(
            &o2.session_id,
            o2.project_id.as_deref(),
            2000,
            o2.is_new_session,
            &repo,
        );
        let back = SessionChangeStore::open(&sc).unwrap();
        let row = back.get(&o1.session_id).unwrap().expect("row");
        assert!(row.latest_diff.contains("v2"), "diff: {}", row.latest_diff);
    });
    // Flood past the status cap with long names (fast, deterministic).
    for i in 0..100 {
        let name = format!("f{i:0190}-{i:04}.txt");
        std::fs::write(repo.join(&name), "x\n").unwrap();
    }
    // Three consecutive incomplete refreshes preserve the prior "v2" diff.
    for (n, ts) in [3000, 4000, 5000].iter().enumerate() {
        let o = store
            .append_with_outcome("focus", "hyprland", &ctx(*ts, &format!("0x{}", n + 3), Some(A_ID)))
            .unwrap();
        assert_eq!(o.session_id, o1.session_id);
        with_sidecar(&sc, || {
            {
                let back = SessionChangeStore::open(&sc).unwrap();
                back.conn_execute_backdate(&o1.session_id, 0);
            }
            observe(
                &o.session_id,
                o.project_id.as_deref(),
                *ts,
                o.is_new_session,
                &repo,
            );
            let back = SessionChangeStore::open(&sc).unwrap();
            let row = back.get(&o1.session_id).unwrap().expect("row");
            assert!(
                row.latest_diff.contains("v2"),
                "prior must survive flood #{n}: {}",
                row.latest_diff
            );
            // Coherent snapshot: the STORED snapshot is still the complete
            // prior one (flags describe stored data); the failed attempt is
            // visible in the note, never in the flags.
            assert!(row.latest_complete, "stored snapshot intact #{n}");
            assert!(
                row.capture_note.contains("showing prior snapshot"),
                "visible failure #{n}: {}",
                row.capture_note
            );
        });
    }
    // Fourth consecutive incomplete: backed off, honestly noted.
    let o = store
        .append_with_outcome("focus", "hyprland", &ctx(6000, "0x6", Some(A_ID)))
        .unwrap();
    with_sidecar(&sc, || {
        {
            let back = SessionChangeStore::open(&sc).unwrap();
            back.conn_execute_backdate(&o1.session_id, 0);
        }
        observe(
            &o.session_id,
            o.project_id.as_deref(),
            6000,
            o.is_new_session,
            &repo,
        );
        let back = SessionChangeStore::open(&sc).unwrap();
        let row = back.get(&o1.session_id).unwrap().expect("row");
        assert!(
            row.capture_note.contains("deferred after repeated failures"),
            "note: {}",
            row.capture_note
        );
        assert!(row.latest_diff.contains("v2"), "prior still kept");
    });
    let _ = std::fs::remove_dir_all(&repo);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn rate_limit_skips_git_entirely() {
    // A second observation inside the refresh window must not spawn git at
    // all: updated_at stays frozen (only the cheap observed bump applies).
    let repo = init_repo("ratelimit");
    let dir = tmpdir("ratelimit-side");
    let sc = sidecar_in(&dir, "ratelimit");
    let store =
        ActivityStore::open_in_memory_with_config(SessionConfig::new(60_000, 5_000).unwrap())
            .unwrap();
    let o1 = store
        .append_with_outcome("focus", "hyprland", &ctx(1000, "0x1", Some(A_ID)))
        .unwrap();
    with_sidecar(&sc, || {
        observe(
            &o1.session_id,
            o1.project_id.as_deref(),
            1000,
            o1.is_new_session,
            &repo,
        );
        let stamp = SessionChangeStore::open(&sc)
            .unwrap()
            .get(&o1.session_id)
            .unwrap()
            .expect("row")
            .updated_at_ms;
        // Immediate follow-up (no backdate): rate-limited, no refresh.
        let o2 = store
            .append_with_outcome("focus", "hyprland", &ctx(1001, "0x2", Some(A_ID)))
            .unwrap();
        assert!(!o2.is_new_session);
        observe(
            &o2.session_id,
            o2.project_id.as_deref(),
            1001,
            o2.is_new_session,
            &repo,
        );
        let row = SessionChangeStore::open(&sc)
            .unwrap()
            .get(&o1.session_id)
            .unwrap()
            .expect("row");
        assert_eq!(row.updated_at_ms, stamp, "no git refresh happened");
        assert_eq!(row.observed_at_ms, 1001, "observed still advances");
    });
    let _ = std::fs::remove_dir_all(&repo);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn directory_replaced_by_file_keeps_secret_excluded() {
    // Tracked `bundle/.env`, then the directory is replaced by a file
    // `bundle`: the allowlisted file diff must not leak the deleted
    // secret beneath it (forbidden descendant exclusion).
    let repo = init_repo("dirfile");
    std::fs::create_dir_all(repo.join("bundle")).unwrap();
    std::fs::write(repo.join("bundle/.env"), "SECRET=abcdef\n").unwrap();
    std::fs::write(repo.join("notes.md"), "hi\n").unwrap();
    git(&["add", "."], &repo);
    git(&["commit", "-qm", "add bundle"], &repo);
    let dir = tmpdir("dirfile-side");
    let sc = sidecar_in(&dir, "dirfile");
    let store =
        ActivityStore::open_in_memory_with_config(SessionConfig::new(60_000, 5_000).unwrap())
            .unwrap();
    let o1 = store
        .append_with_outcome("focus", "hyprland", &ctx(1000, "0x1", Some(A_ID)))
        .unwrap();
    with_sidecar(&sc, || {
        observe(
            &o1.session_id,
            o1.project_id.as_deref(),
            1000,
            o1.is_new_session,
            &repo,
        );
    });
    let _ = std::fs::remove_dir_all(repo.join("bundle"));
    std::fs::write(repo.join("bundle"), "replacement file\n").unwrap();
    std::fs::write(repo.join("notes.md"), "hi\nmore\n").unwrap();
    let o2 = store
        .append_with_outcome("focus", "hyprland", &ctx(2000, "0x2", Some(A_ID)))
        .unwrap();
    with_sidecar(&sc, || {
        {
            let back = SessionChangeStore::open(&sc).unwrap();
            back.conn_execute_backdate(&o1.session_id, 0);
        }
        observe(
            &o2.session_id,
            o2.project_id.as_deref(),
            2000,
            o2.is_new_session,
            &repo,
        );
        let back = SessionChangeStore::open(&sc).unwrap();
        let row = back.get(&o1.session_id).unwrap().expect("row");
        assert!(row.latest_complete, "note: {}", row.capture_note);
        assert!(row.latest_diff.contains("more"), "diff: {}", row.latest_diff);
        assert!(
            !row.latest_diff.contains("SECRET=abcdef"),
            "leak: {}",
            row.latest_diff
        );
        assert!(
            !row.latest_status.contains("bundle/.env"),
            "status: {}",
            row.latest_status
        );
    });
    let _ = std::fs::remove_dir_all(&repo);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn range_failure_retains_entire_snapshot() {
    // Successful A->B (frozen B patch at T1), then HEAD advances to C whose
    // range patch overflows: the ENTIRE prior snapshot stays labeled
    // B/T1 (never C/T2), the failure is visible, and repeats back off.
    let repo = init_repo("rangefail");
    let dir = tmpdir("rangefail-side");
    let sc = sidecar_in(&dir, "rangefail");
    let store =
        ActivityStore::open_in_memory_with_config(SessionConfig::new(60_000, 5_000).unwrap())
            .unwrap();
    let o1 = store
        .append_with_outcome("focus", "hyprland", &ctx(1000, "0x1", Some(A_ID)))
        .unwrap();
    with_sidecar(&sc, || {
        observe(
            &o1.session_id,
            o1.project_id.as_deref(),
            1000,
            o1.is_new_session,
            &repo,
        );
    });
    // Commit B with real session work; refresh succeeds fully.
    std::fs::write(repo.join("note.md"), "one\nb-work\n").unwrap();
    git(&["add", "note.md"], &repo);
    git(&["commit", "-qm", "b work"], &repo);
    let o2 = store
        .append_with_outcome("focus", "hyprland", &ctx(2000, "0x2", Some(A_ID)))
        .unwrap();
    assert_eq!(o1.session_id, o2.session_id);
    with_sidecar(&sc, || {
        {
            let back = SessionChangeStore::open(&sc).unwrap();
            back.conn_execute_backdate(&o1.session_id, 0);
        }
        observe(
            &o2.session_id,
            o2.project_id.as_deref(),
            2000,
            o2.is_new_session,
            &repo,
        );
        let back = SessionChangeStore::open(&sc).unwrap();
        let row = back.get(&o1.session_id).unwrap().expect("row");
        assert!(row.commit_patch.contains("b-work"), "patch: {}", row.commit_patch);
        assert!(row.commit_complete);
    });
    let t1 = SessionChangeStore::open(&sc)
        .unwrap()
        .get(&o1.session_id)
        .unwrap()
        .expect("row")
        .updated_at_ms;
    let commit_b = SessionChangeStore::open(&sc)
        .unwrap()
        .get(&o1.session_id)
        .unwrap()
        .expect("row")
        .latest_commit
        .clone();
    // 60 commits of 2 KiB each: the B..C range patch (~120 KiB) overflows
    // the 32 KiB cap while the worktree itself stays clean and complete.
    for i in 0..60 {
        let name = format!("bulk{i:03}.txt");
        std::fs::write(repo.join(&name), "y\n".repeat(1024)).unwrap();
        git(&["add", &name], &repo);
        git(&["commit", "-qm", &format!("bulk {i}")], &repo);
    }
    // Three failing refreshes: full retention every time (B patch, B
    // commit, T1 timestamp), visible failure notes. Backdate to an hour
    // before T1: old enough to defeat the rate limit, exact enough to
    // prove the capture timestamp is retained verbatim.
    let aged = t1 - 3_600_000;
    for (n, ts) in [3000, 4000, 5000].iter().enumerate() {
        with_sidecar(&sc, || {
            {
                let back = SessionChangeStore::open(&sc).unwrap();
                back.conn_execute_backdate(&o1.session_id, aged);
            }
            observe(&o1.session_id, Some(A_ID), *ts, false, &repo);
            let back = SessionChangeStore::open(&sc).unwrap();
            let row = back.get(&o1.session_id).unwrap().expect("row");
            assert_eq!(row.latest_commit, commit_b, "HEAD must stay B (#{n})");
            assert!(
                row.commit_patch.contains("b-work"),
                "B patch kept (#{n}): {}",
                row.commit_patch
            );
            assert_eq!(row.updated_at_ms, aged, "capture timestamp kept (#{n})");
            assert!(
                row.capture_note.contains("commit range refresh failed"),
                "visible failure (#{n}): {}",
                row.capture_note
            );
        });
    }
    // Fourth consecutive failure: backed off without git.
    with_sidecar(&sc, || {
        {
            let back = SessionChangeStore::open(&sc).unwrap();
            back.conn_execute_backdate(&o1.session_id, aged);
        }
        observe(&o1.session_id, Some(A_ID), 6000, false, &repo);
        let back = SessionChangeStore::open(&sc).unwrap();
        let row = back.get(&o1.session_id).unwrap().expect("row");
        assert!(
            row.capture_note.contains("deferred after repeated failures"),
            "note: {}",
            row.capture_note
        );
        assert_eq!(row.latest_commit, commit_b);
        assert_eq!(row.updated_at_ms, aged);
    });
    let _ = std::fs::remove_dir_all(&repo);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn shutdown_drain_skips_capture() {    // The bounded shutdown drain must never spend the git budget: activity
    // rows persist, sidecar rows are honestly absent.
    use qs_agent_orchestrator::desktop_context::EnqueueOutcome;

    let dir = tmpdir("drain-side");
    let sc = sidecar_in(&dir, "drain");
    let store = ActivityStore::open_in_memory().unwrap();
    let mut tracker = Tracker::new();
    match tracker.enqueue(ctx(1000, "0x1", Some(A_ID))) {
        Ok(EnqueueOutcome::Queued(_, _)) => {}
        other => panic!("expected queued, got {other:?}"),
    }
    with_sidecar(&sc, || {
        let remaining = final_drain(&store, &mut tracker);
        assert_eq!(remaining, 0);
        assert_eq!(store.recent_activity(10).unwrap().len(), 1);
        // No sidecar file was even created by the drain.
        assert!(!sc.exists(), "drain must not capture");
    });
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn dirty_baseline_distinguishes_preexisting_edits() {
    let repo = init_repo("dirty");
    // Dirty BEFORE the session starts.
    std::fs::write(repo.join("note.md"), "one\ndirty-before\n").unwrap();
    let dir = tmpdir("dirty-side");
    let sc = sidecar_in(&dir, "dirty");
    let store =
        ActivityStore::open_in_memory_with_config(SessionConfig::new(60_000, 5_000).unwrap())
            .unwrap();
    let o1 = store
        .append_with_outcome("focus", "hyprland", &ctx(1000, "0x1", Some(A_ID)))
        .unwrap();
    with_sidecar(&sc, || {
        observe(
            &o1.session_id,
            o1.project_id.as_deref(),
            1000,
            o1.is_new_session,
            &repo,
        );
        let back = SessionChangeStore::open(&sc).unwrap();
        let row = back.get(&o1.session_id).unwrap().expect("row");
        // Baseline already records the dirty file: not attributed as new.
        assert!(row.baseline_status.contains("note.md"));
        assert!(row.baseline_diff.contains("dirty-before"));
    });
    let _ = std::fs::remove_dir_all(&repo);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn no_repo_records_unavailable_without_fabrication() {
    let plain = tmpdir("norepo");
    let dir = tmpdir("norepo-side");
    let sc = sidecar_in(&dir, "norepo");
    let store = ActivityStore::open_in_memory().unwrap();
    let outcome = store
        .append_with_outcome("focus", "hyprland", &ctx(1000, "0x1", Some(A_ID)))
        .unwrap();
    with_sidecar(&sc, || {
        observe(
            &outcome.session_id,
            outcome.project_id.as_deref(),
            1000,
            outcome.is_new_session,
            &plain,
        );
        let back = SessionChangeStore::open(&sc).unwrap();
        let row = back.get(&outcome.session_id).unwrap().expect("row");
        assert!(!row.has_baseline);
        assert!(row.baseline_commit.is_none());
        assert!(!row.reason.is_empty());
    });
    let _ = std::fs::remove_dir_all(&plain);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn old_session_first_seen_after_upgrade_marks_missing_baseline() {
    let repo = init_repo("old");
    let dir = tmpdir("old-side");
    let sc = sidecar_in(&dir, "old");
    let store =
        ActivityStore::open_in_memory_with_config(SessionConfig::new(60_000, 5_000).unwrap())
            .unwrap();
    // Two events in one session, but the sidecar only sees the SECOND
    // (simulating a session that started before the upgrade).
    let o1 = store
        .append_with_outcome("focus", "hyprland", &ctx(1000, "0x1", Some(A_ID)))
        .unwrap();
    let o2 = store
        .append_with_outcome("focus", "hyprland", &ctx(2000, "0x2", Some(A_ID)))
        .unwrap();
    assert!(!o2.is_new_session);
    with_sidecar(&sc, || {
        observe(
            &o2.session_id,
            o2.project_id.as_deref(),
            2000,
            false, // NOT new: must not fabricate a baseline from HEAD.
            &repo,
        );
        let back = SessionChangeStore::open(&sc).unwrap();
        let row = back.get(&o1.session_id).unwrap().expect("row");
        assert!(!row.has_baseline);
        assert!(row.baseline_commit.is_none());
        assert!(row.reason.contains("before change capture"));
    });
    let _ = std::fs::remove_dir_all(&repo);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn capture_failure_never_breaks_activity_persistence() {
    use qs_agent_orchestrator::collector::flush_pending;

    let dir = tmpdir("safety-side");
    let sc = sidecar_in(&dir, "safety");
    let store = ActivityStore::open_in_memory().unwrap();
    let mut tracker = Tracker::new();
    tracker.enqueue(ctx(1000, "0x1", Some(A_ID))).expect("enqueue");
    with_sidecar(&sc, || {
        // Unusable repo: observe returns normally (no panic, no hang)…
        session_changes::observe_session_change(
            &"d".repeat(32),
            Some(A_ID),
            1000,
            true,
            Some(Path::new("/nonexistent-qs-repo-xyz")),
        );
        // …and the activity flush still succeeds regardless.
        let n = flush_pending(&store, &mut tracker).expect("flush survives");
        assert_eq!(n, 1);
        assert_eq!(store.recent_activity(10).unwrap().len(), 1);
    });
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn secret_paths_redacted_and_index_untouched() {
    let repo = init_repo("secrets");
    std::fs::write(repo.join(".env"), "TOKEN=super-secret-123\n").unwrap();
    let state = session_changes::capture_repo_state(&repo).expect("capture");
    assert!(!state.diff.contains("super-secret-123"));
    assert!(!state.status.contains("super-secret-123"));
    // Index untouched: only the untracked .env shows in status, commit count stays 1.
    let count = std::process::Command::new("git")
        .args(["rev-list", "--count", "HEAD"])
        .current_dir(&repo)
        .output()
        .unwrap();
    assert_eq!(String::from_utf8_lossy(&count.stdout).trim(), "1");
    let _ = std::fs::remove_dir_all(&repo);
}
