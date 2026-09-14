//! Final-fix regressions for the desktop collector only.
//!
//! 1. Storage-blocked: while the bounded backlog stays full, `run_forever`
//!    retries ONLY persistence/backoff with NO discovery/snapshot queries,
//!    live `current` reads unavailable, and the overflowing observation is an
//!    explicit gap (finite memory, no lossless claim). Recovery preserves
//!    FIFO order with original timestamps.
//! 2. Shutdown: bounded final drain with a fixed deadline; remainder is
//!    reported with its pending count and a nonzero outcome. Covers recovery
//!    before shutdown and persistent failure (no durable spool).
//! 3. Snapshot stability is semantic (parsed focus/workspace + embedded
//!    workspace), ignoring formatting/geometry/count/extra keys.

use qs_agent_orchestrator::collector::{
    final_drain, flush_pending, run_collector_forever, SHUTDOWN_DRAIN_DEADLINE,
};
use qs_agent_orchestrator::desktop_context::{
    DesktopContext, EnqueueOutcome, FocusedWindow, Source, Tracker, Workspace,
    MAX_PENDING_OBSERVATIONS,
};
use qs_agent_orchestrator::hyprland::{fetch_snapshot_at, SocketPaths};
use qs_agent_orchestrator::ActivityStore;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

fn tmpdir(tag: &str) -> std::path::PathBuf {
    let p = std::env::temp_dir().join(format!(
        "qs-final-{tag}-{}-{}",
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

fn ctx_win(win_id: &str, title: &str, ws: &str, ts: i64) -> DesktopContext {
    DesktopContext::available(
        Source::Hyprland,
        Some(FocusedWindow::new(win_id, "kitty", title)),
        Some(Workspace::new(ws, ws)),
        ts,
    )
}

fn fill_tracker_full(tracker: &mut Tracker) {
    for i in 0..MAX_PENDING_OBSERVATIONS {
        let c = ctx_win("0x1", &format!("t{i}"), "1", 1000 + i as i64);
        match tracker.enqueue(c).expect("prefill must queue") {
            EnqueueOutcome::Queued(_, _) => {}
            EnqueueOutcome::Deduplicated => panic!("prefill titles differ, must queue"),
        }
    }
    assert_eq!(tracker.pending_len(), MAX_PENDING_OBSERVATIONS);
}

/// Counting fake request socket: every connection bumps `hits` and serves a
/// static snapshot. While storage-blocked there must be zero hits.
fn spawn_counting_request_socket(
    dir: &std::path::Path,
    hits: Arc<AtomicUsize>,
) -> std::path::PathBuf {
    use std::io::{Read, Write};
    use std::os::unix::net::UnixListener;
    let path = dir.join(".socket.sock");
    let _ = std::fs::remove_file(&path);
    let listener = UnixListener::bind(&path).expect("bind counting request socket");
    std::thread::spawn(move || {
        for conn in listener.incoming() {
            let mut stream = match conn {
                Ok(s) => s,
                Err(_) => return,
            };
            hits.fetch_add(1, Ordering::SeqCst);
            let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
            let mut buf = vec![0u8; 4096];
            let n = match stream.read(&mut buf) {
                Ok(n) => n,
                Err(_) => continue,
            };
            if n == 0 {
                continue;
            }
            let req = String::from_utf8_lossy(&buf[..n]).to_string();
            let reply = if req.contains("activewindow") {
                r#"{"address":"0x1","class":"kitty","title":"static","workspace":{"id":1,"name":"1"}}"#
                    .to_string()
            } else if req.contains("activeworkspace") {
                r#"{"id":1,"name":"1"}"#.to_string()
            } else {
                "{}".to_string()
            };
            let _ = stream.write_all(reply.as_bytes());
        }
    });
    path
}

#[test]
fn storage_blocked_no_snapshot_until_recovery_ordered() {
    let dir = tmpdir("blocked");
    let db = dir.join("activity.db");
    // Init schema, then hold a real exclusive lock to block appends.
    drop(ActivityStore::open(&db).unwrap());
    let lock_conn = rusqlite::Connection::open(&db).unwrap();
    lock_conn.execute_batch("BEGIN EXCLUSIVE").unwrap();

    let mut tracker = Tracker::new();
    fill_tracker_full(&mut tracker);

    let hits = Arc::new(AtomicUsize::new(0));
    let req_path = spawn_counting_request_socket(&dir, Arc::clone(&hits));
    let ev_path = dir.join(".socket2.sock");
    {
        use std::os::unix::net::UnixListener;
        let _ = std::fs::remove_file(&ev_path);
        let listener = UnixListener::bind(&ev_path).unwrap();
        std::thread::spawn(move || {
            let (_s, _) = listener.accept().expect("accept");
            std::thread::sleep(Duration::from_secs(10));
        });
    }
    std::thread::sleep(Duration::from_millis(50));

    let discover_hits = Arc::new(AtomicUsize::new(0));
    let shutdown = Arc::new(AtomicBool::new(false));
    let shutdown_child = Arc::clone(&shutdown);
    let _hits_child = Arc::clone(&hits);
    let discover_child = Arc::clone(&discover_hits);
    let req_clone = req_path.clone();
    let ev_clone = ev_path.clone();
    let handle = std::thread::spawn(move || {
        let session_store = ActivityStore::open(&db).unwrap();
        let mut session_tracker = tracker;
        let paths = SocketPaths {
            event_socket: ev_clone,
            request_socket: req_clone,
        };
        let remaining = run_collector_forever(
            &session_store,
            &mut session_tracker,
            shutdown_child.as_ref(),
            || {
                discover_child.fetch_add(1, Ordering::SeqCst);
                Some(paths.clone())
            },
        );
        let cur = session_tracker.current_context();
        let pending = session_tracker.pending_len();
        (remaining, pending, cur, session_store, session_tracker)
    });

    // Multiple blocked rounds (200ms + 400ms backoffs): no discovery, no
    // snapshot queries while the 128-queue stays full.
    std::thread::sleep(Duration::from_millis(900));
    // Still blocked: counters must be zero mid-run is implied by final zero
    // after shutdown while still locked.
    shutdown.store(true, Ordering::SeqCst);
    let (remaining, pending, cur, store_back, mut tracker_back) =
        handle.join().expect("run_forever joins");
    assert_eq!(remaining, MAX_PENDING_OBSERVATIONS);
    assert_eq!(pending, MAX_PENDING_OBSERVATIONS);
    assert_eq!(
        discover_hits.load(Ordering::SeqCst),
        0,
        "storage-blocked must not call discover"
    );
    assert_eq!(
        hits.load(Ordering::SeqCst),
        0,
        "storage-blocked must not issue snapshot queries"
    );
    let cur = cur.expect("current present");
    assert!(
        !cur.available,
        "no subscription while blocked => unavailable, not stale available"
    );

    // Recovery: release the real lock, then the backlog must flush FIFO with
    // original timestamps preserved.
    lock_conn.execute_batch("COMMIT").unwrap();
    let flushed = flush_pending(&store_back, &mut tracker_back).expect("recovery flush");
    assert_eq!(flushed, MAX_PENDING_OBSERVATIONS);
    assert!(tracker_back.pending_is_empty());
    let rows = store_back.activity_in_range(0, 99999, 1000).unwrap();
    assert_eq!(rows.len(), MAX_PENDING_OBSERVATIONS);
    for (i, r) in rows.iter().enumerate() {
        assert_eq!(r.observed_at_ms, 1000 + i as i64, "FIFO order, original ts");
    }
    let _ = std::fs::remove_dir_all(&dir);
    let _ = hits;
    let _ = discover_hits;
}

#[test]
fn shutdown_drain_recovered_before_deadline() {
    let dir = tmpdir("drain-ok");
    let db = dir.join("activity.db");
    let store = ActivityStore::open(&db).unwrap();
    let lock_conn = rusqlite::Connection::open(&db).unwrap();
    lock_conn.execute_batch("BEGIN EXCLUSIVE").unwrap();

    let mut tracker = Tracker::new();
    for i in 0..3 {
        let c = ctx_win("0x1", &format!("drain{i}"), "1", 5000 + i);
        match tracker.enqueue(c).expect("enqueue") {
            EnqueueOutcome::Queued(_, _) => {}
            EnqueueOutcome::Deduplicated => panic!("must queue"),
        }
    }
    // Locked: flush fails, backlog retained.
    assert!(flush_pending(&store, &mut tracker).is_err());
    assert_eq!(tracker.pending_len(), 3);

    // Storage recovers before shutdown: drain persists all within deadline.
    lock_conn.execute_batch("COMMIT").unwrap();
    let start = Instant::now();
    let remaining = final_drain(&store, &mut tracker);
    assert!(start.elapsed() < SHUTDOWN_DRAIN_DEADLINE + Duration::from_secs(1));
    assert_eq!(remaining, 0);
    assert!(tracker.pending_is_empty());
    let rows = store.activity_in_range(0, 99999, 10).unwrap();
    assert_eq!(rows.len(), 3);
    assert_eq!(rows[0].observed_at_ms, 5000);
    assert_eq!(rows[2].observed_at_ms, 5002);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn shutdown_drain_persistent_failure_reports_remaining() {
    let dir = tmpdir("drain-fail");
    let db = dir.join("activity.db");
    let store = ActivityStore::open(&db).unwrap();
    let lock_conn = rusqlite::Connection::open(&db).unwrap();
    lock_conn.execute_batch("BEGIN EXCLUSIVE").unwrap();

    let mut tracker = Tracker::new();
    for i in 0..3 {
        let c = ctx_win("0x1", &format!("stuck{i}"), "1", 6000 + i);
        match tracker.enqueue(c).expect("enqueue") {
            EnqueueOutcome::Queued(_, _) => {}
            EnqueueOutcome::Deduplicated => panic!("must queue"),
        }
    }
    // Persistent failure: deadline-bounded drain keeps the backlog and
    // reports its count (nonzero/incomplete, no durable spool).
    let start = Instant::now();
    let remaining = final_drain(&store, &mut tracker);
    let elapsed = start.elapsed();
    assert_eq!(
        remaining, 3,
        "persistent lock => backlog retained + reported"
    );
    assert_eq!(tracker.pending_len(), 3);
    assert!(
        elapsed >= SHUTDOWN_DRAIN_DEADLINE,
        "drain must use its bounded deadline, took {elapsed:?}"
    );
    assert!(
        elapsed < SHUTDOWN_DRAIN_DEADLINE + Duration::from_secs(2),
        "drain must stay bounded, took {elapsed:?}"
    );
    assert!(
        store.recent_activity(10).unwrap().is_empty(),
        "nothing persisted while locked"
    );
    lock_conn.execute_batch("COMMIT").unwrap();
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn snapshot_semantic_stability_ignores_irrelevant_metadata() {
    use std::io::{Read, Write};
    use std::os::unix::net::UnixListener;
    use std::sync::atomic::AtomicU64;

    let dir = tmpdir("semantic");
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join(".socket.sock");
    let _ = std::fs::remove_file(&path);
    let listener = UnixListener::bind(&path).expect("bind semantic socket");
    let seq = Arc::new(AtomicU64::new(0));
    let seq_child = Arc::clone(&seq);
    std::thread::spawn(move || {
        for conn in listener.incoming() {
            let mut stream = match conn {
                Ok(s) => s,
                Err(_) => return,
            };
            let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
            let mut buf = vec![0u8; 4096];
            let n = match stream.read(&mut buf) {
                Ok(n) => n,
                Err(_) => continue,
            };
            if n == 0 {
                continue;
            }
            let idx = seq_child.fetch_add(1, Ordering::SeqCst);
            let req = String::from_utf8_lossy(&buf[..n]).to_string();
            // Same semantic focus/workspace every query; only irrelevant
            // metadata (geometry/count/whitespace/extra) changes.
            let reply = if req.contains("activewindow") {
                format!(
                    "{{\"address\":\"0x1\",\"class\":\"kitty\",\"title\":\"hi\",\"workspace\":{{\"id\":1,\"name\":\"1\"}},\"geometry\":[{},{},800,600],\"count\":{},\"extra\":\"v{}\"}}",
                    idx, idx + 1, idx, idx
                )
            } else if req.contains("activeworkspace") {
                format!(
                    "{{\"id\":1,\"name\":\"1\",\"windows\":{},\"extra_seq\":{}}}",
                    10 + idx,
                    idx
                )
            } else {
                "{}".to_string()
            };
            let _ = stream.write_all(reply.as_bytes());
        }
    });
    std::thread::sleep(Duration::from_millis(50));
    // Raw-JSON comparison would see four distinct payloads and report
    // Timeout; semantic comparison must return a valid stable snapshot.
    let snap = fetch_snapshot_at(&path, 4242).expect("semantic snapshot must be stable");
    assert!(snap.available);
    assert_eq!(snap.focused_window.unwrap().title, "hi");
    assert_eq!(snap.workspace.unwrap().id, "1");
    let _ = std::fs::remove_dir_all(&dir);
}
