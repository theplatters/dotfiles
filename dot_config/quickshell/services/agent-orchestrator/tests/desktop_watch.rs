//! `watch` mode tests: resident current-project source without a compositor.
//!
//! Covers (all hermetic: a bogus `HYPRLAND_INSTANCE_SIGNATURE` forces the
//! explicit-signature discovery path to `None`, so no live compositor can
//! leak in, and a missing `--db` path must never be created):
//! - startup emission (one line immediately, including the null-project case)
//! - change-only emission / dedup (identical consecutive unavailable states
//!   are not re-emitted even though `observed_at_ms` advances per recheck)
//! - output shape equals the `current` query shape, with the projected
//!   `project` equal to the `current-project` query output
//! - clean SIGTERM shutdown (exit 0, no DB file created, no writes)
//! - `watch` rejects query flags like `current-project` does

use qs_agent_orchestrator::desktop_context::DesktopContext;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::time::Duration;

fn bin_path() -> PathBuf {
    if let Some(p) = option_env!("CARGO_BIN_EXE_qs-desktop-context") {
        return PathBuf::from(p);
    }
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("target")
        .join("debug")
        .join("qs-desktop-context")
}

fn tmpdir(tag: &str) -> PathBuf {
    let p = std::env::temp_dir().join(format!(
        "qs-watch-{tag}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&p).unwrap();
    p
}

/// Hermetic no-compositor environment: a bogus explicit signature can never
/// resolve (modern runtime dir is an empty temp dir, legacy `/tmp/hypr/<sig>`
/// does not exist), so discovery yields `None` and `watch` reports the
/// unavailable object — deterministically, even on a live desktop.
fn no_compositor(cmd: &mut Command, rt: &Path) {
    cmd.env("HYPRLAND_INSTANCE_SIGNATURE", "qs-watch-test-nonexistent");
    cmd.env("XDG_RUNTIME_DIR", rt);
}

fn spawn_watch(db: &Path, rt: &Path) -> Child {
    let mut cmd = Command::new(bin_path());
    cmd.args(["--db", &db.to_string_lossy(), "watch"]);
    cmd.stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .stdin(Stdio::null());
    no_compositor(&mut cmd, rt);
    cmd.spawn().expect("spawn watch")
}

/// RAII owner for a resident `watch` child: the explicit SIGTERM + bounded
/// wait in each test body is the real cleanup (and asserts on it); this
/// Drop is the backstop so a panicking assertion can never leak a resident
/// 2 s recheck loop into the suite's debris. Derefs to `Child`, so existing
/// call sites (`line_reader`, `term`, field access) work unchanged.
struct WatchGuard {
    child: Child,
}

impl WatchGuard {
    fn wrap(child: Child) -> WatchGuard {
        WatchGuard { child }
    }
}

impl std::ops::Deref for WatchGuard {
    type Target = Child;
    fn deref(&self) -> &Child {
        &self.child
    }
}

impl std::ops::DerefMut for WatchGuard {
    fn deref_mut(&mut self) -> &mut Child {
        &mut self.child
    }
}

impl Drop for WatchGuard {
    fn drop(&mut self) {
        // Quiet backstop: skip the signal entirely when the child already
        // exited (the common path — explicit cleanup ran in the test body).
        let alive = matches!(self.child.try_wait(), Ok(None));
        if alive {
            term(&mut self.child);
        }
        let _ = self.child.wait_timeout_or_kill(Duration::from_secs(10));
    }
}

/// Read one stdout line in the background; `recv_timeout` bounds the wait so
/// "no second line" (dedup) is assertable without hanging the suite.
fn line_reader(
    child: &mut Child,
) -> (mpsc::Receiver<Option<String>>, std::thread::JoinHandle<()>) {
    let stdout = child.stdout.take().expect("watch stdout piped");
    let (tx, rx) = mpsc::channel();
    let handle = std::thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        loop {
            let mut line = String::new();
            match reader.read_line(&mut line) {
                Ok(0) => {
                    let _ = tx.send(None);
                    return;
                }
                Ok(_) => {
                    if tx.send(Some(line)).is_err() {
                        return;
                    }
                }
                Err(_) => {
                    let _ = tx.send(None);
                    return;
                }
            }
        }
    });
    (rx, handle)
}

fn term(child: &mut Child) {
    // SIGTERM via libc (already a package dependency); fall back to `kill`.
    #[allow(unsafe_code)]
    let via_libc = unsafe { libc::kill(child.id() as i32, libc::SIGTERM) } == 0;
    if !via_libc {
        let _ = Command::new("kill")
            .args(["-TERM", &child.id().to_string()])
            .status();
    }
}

#[test]
fn watch_startup_emits_null_project_line_without_compositor() {
    let base = tmpdir("startup");
    let rt = base.join("rt");
    std::fs::create_dir_all(&rt).unwrap();
    let db = base.join("missing.db");
    let mut child = WatchGuard::wrap(spawn_watch(&db, &rt));
    let (rx, _handle) = line_reader(&mut child);
    let line = rx
        .recv_timeout(Duration::from_secs(20))
        .expect("watch must emit a startup line")
        .expect("watch stdout must stay open for the startup line");
    assert!(!line.trim().is_empty(), "startup line must be non-empty");
    assert!(
        !line.trim_end().contains('\n'),
        "watch must emit single-line JSON"
    );
    let ctx: DesktopContext =
        serde_json::from_str(line.trim()).expect("startup line parses as DesktopContext");
    assert!(
        ctx.project.is_none(),
        "no compositor => null project, got {:?}",
        ctx.project
    );
    term(&mut child);
    let status = child.wait_timeout_or_kill(Duration::from_secs(10));
    assert!(status.success(), "SIGTERM must shut watch down cleanly");
    assert!(!db.exists(), "watch must not create the database");
    let _ = std::fs::remove_dir_all(&base);
}

trait WaitTimeoutOrKill {
    fn wait_timeout_or_kill(&mut self, dur: Duration) -> std::process::ExitStatus;
}

impl WaitTimeoutOrKill for Child {
    fn wait_timeout_or_kill(&mut self, dur: Duration) -> std::process::ExitStatus {
        let deadline = std::time::Instant::now() + dur;
        loop {
            match self.try_wait().expect("try_wait") {
                Some(status) => return status,
                None => {
                    if std::time::Instant::now() >= deadline {
                        self.kill().expect("kill stuck watch");
                        return self.wait().expect("wait after kill");
                    }
                    std::thread::sleep(Duration::from_millis(50));
                }
            }
        }
    }
}

#[test]
fn watch_dedups_identical_states_then_sigterm_exits_zero() {
    let base = tmpdir("dedup");
    let rt = base.join("rt");
    std::fs::create_dir_all(&rt).unwrap();
    let db = base.join("missing.db");
    let mut child = WatchGuard::wrap(spawn_watch(&db, &rt));
    let (rx, _handle) = line_reader(&mut child);
    let first = rx
        .recv_timeout(Duration::from_secs(20))
        .expect("startup line")
        .expect("stdout open");
    let first_ctx: DesktopContext =
        serde_json::from_str(first.trim()).expect("startup line parses");
    assert!(first_ctx.project.is_none());
    // The recheck interval is 2 s and every recheck advances
    // `observed_at_ms`, yet the projected project state is unchanged, so no
    // second line may arrive within more than two intervals.
    let second = rx.recv_timeout(Duration::from_secs(6));
    assert!(
        second.is_err(),
        "identical consecutive states must not be re-emitted, got {second:?}"
    );
    term(&mut child);
    let status = child.wait_timeout_or_kill(Duration::from_secs(10));
    assert!(
        status.success(),
        "clean SIGTERM shutdown must exit 0, got {status:?}"
    );
    let mut stderr = String::new();
    if let Some(mut err) = child.stderr.take() {
        use std::io::Read;
        let _ = err.read_to_string(&mut stderr);
    }
    assert!(
        stderr.contains("shutting down"),
        "shutdown must be logged to stderr, got {stderr:?}"
    );
    assert!(!db.exists(), "watch must not create the database");
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn watch_line_shape_matches_current_and_current_project() {
    let base = tmpdir("shape");
    let rt = base.join("rt");
    std::fs::create_dir_all(&rt).unwrap();
    let db = base.join("missing.db");
    // Reference one-shot outputs under the same hermetic environment.
    let run = |args: &[&str]| -> (String, String, bool) {
        let mut cmd = Command::new(bin_path());
        cmd.args(args);
        no_compositor(&mut cmd, &rt);
        let out = cmd.output().expect("run query");
        (
            String::from_utf8_lossy(&out.stdout).to_string(),
            String::from_utf8_lossy(&out.stderr).to_string(),
            out.status.success(),
        )
    };
    let (current_out, _, current_ok) = run(&["--db", &db.to_string_lossy(), "current"]);
    assert!(current_ok, "current must succeed without a compositor");
    let current_ctx: DesktopContext =
        serde_json::from_str(current_out.trim()).expect("current parses");
    let (proj_out, _, proj_ok) = run(&["--db", &db.to_string_lossy(), "current-project"]);
    assert!(proj_ok, "current-project must succeed without a compositor");
    assert_eq!(proj_out.trim(), "null");
    // The watch line carries the exact `current` shape; its projection equals
    // the `current-project` query output.
    let mut child = WatchGuard::wrap(spawn_watch(&db, &rt));
    let (rx, _handle) = line_reader(&mut child);
    let line = rx
        .recv_timeout(Duration::from_secs(20))
        .expect("startup line")
        .expect("stdout open");
    let watch_ctx: DesktopContext =
        serde_json::from_str(line.trim()).expect("watch line parses");
    assert_eq!(
        serde_json::to_value(&watch_ctx).expect("serialize").as_object().map(|o| {
            let mut keys: Vec<&str> = o.keys().map(|k| k.as_str()).collect();
            keys.sort_unstable();
            keys
        }),
        serde_json::to_value(&current_ctx).expect("serialize").as_object().map(|o| {
            let mut keys: Vec<&str> = o.keys().map(|k| k.as_str()).collect();
            keys.sort_unstable();
            keys
        }),
        "watch line must carry the exact `current` query shape"
    );
    let watch_proj = match &watch_ctx.project {
        Some(p) => serde_json::to_string(p).expect("project serializes"),
        None => "null".to_string(),
    };
    assert_eq!(
        watch_proj,
        proj_out.trim(),
        "watch projection must equal the `current-project` query output"
    );
    term(&mut child);
    let status = child.wait_timeout_or_kill(Duration::from_secs(10));
    assert!(status.success());
    assert!(!db.exists());
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn watch_rejects_query_flags() {
    let mut cmd = Command::new(bin_path());
    cmd.args(["watch", "--limit", "5"])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let out = cmd.output().expect("run watch --limit");
    assert!(!out.status.success(), "watch must reject --limit");
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("not for watch"),
        "unexpected stderr: {stderr}"
    );
}

#[test]
fn watch_exits_on_stdin_eof_without_signal() {
    // Leak regression: with a piped stdin (what the `desktop_projects.py`
    // bridge holds after the supervision fix), dropping the write end —
    // i.e. a supervisor dying without SIGTERM, which no signal handler can
    // catch — must shut the resident loop down like SIGTERM, with no
    // emission to trigger it.
    let base = tmpdir("stdin-eof");
    let rt = base.join("rt");
    std::fs::create_dir_all(&rt).unwrap();
    let db = base.join("missing.db");
    let mut cmd = Command::new(bin_path());
    cmd.args(["--db", &db.to_string_lossy(), "watch"]);
    cmd.stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .stdin(Stdio::piped());
    no_compositor(&mut cmd, &rt);
    let mut child = WatchGuard::wrap(cmd.spawn().expect("spawn watch"));
    // Wait for the startup line first: proves the loop is up, so the timed
    // window below is purely supervisor-death -> exit.
    let (rx, _handle) = line_reader(&mut child);
    let line = rx
        .recv_timeout(Duration::from_secs(20))
        .expect("watch must emit a startup line")
        .expect("watch stdout must stay open for the startup line");
    let ctx: DesktopContext =
        serde_json::from_str(line.trim()).expect("startup line parses");
    assert!(
        ctx.project.is_none(),
        "no compositor => null project, got {:?}",
        ctx.project
    );
    // Supervisor death: close the write end; no signal is ever sent.
    drop(child.stdin.take());
    let start = std::time::Instant::now();
    let status = child.wait_timeout_or_kill(Duration::from_secs(10));
    let elapsed = start.elapsed();
    assert!(
        status.success(),
        "stdin EOF must shut watch down cleanly (exit 0), got {status:?}"
    );
    assert!(
        elapsed < Duration::from_secs(3),
        "stdin EOF must exit promptly (<3 s), took {elapsed:?}"
    );
    let mut stderr = String::new();
    if let Some(mut err) = child.stderr.take() {
        use std::io::Read;
        let _ = err.read_to_string(&mut stderr);
    }
    assert!(
        stderr.contains("shutting down"),
        "EOF shutdown must be logged to stderr, got {stderr:?}"
    );
    assert!(!db.exists(), "watch must not create the database");
    let _ = std::fs::remove_dir_all(&base);
}
