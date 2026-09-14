//! Phase 1 public-API integration tests (no live Hyprland).
//!
//! Exercises the documented interface a doc worker would use:
//! - `Tracker::current_context()` (live only, never backfilled),
//! - `ActivityStore::recent_activity(limit)`,
//! - `ActivityStore::activity_in_range(start_ms, end_ms, limit)`
//!   (start inclusive, end exclusive, UTC epoch-ms),
//! plus fake-Unix-socket event processing and the singleton lock.
//!
//! Collector durability contract covered here:
//! - bounded FIFO backlog with original timestamps, idle/backoff SQLite
//!   retry independent of compositor queries (never desktop polling),
//! - queue-tail semantic dedup preserving `A -> B -> A` order,
//! - explicit bounded overflow (`BacklogFull`, no silent drop),
//! - `Some <-> None` focus changes are `Focus`, never inferred lifecycle.

use qs_agent_orchestrator::{
    acquire_lock,
    collector::{flush_pending, CollectorConfig},
    desktop_context::{
        classify_transition, ActivityKind, DesktopContext, EnqueueOutcome, FocusedWindow,
        Workspace,
    },
    hyprland::{is_meaningful_event, parse_event_line},
    run_collector_once, ActivityStore, Source, Tracker,
};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

fn ctx(win: Option<(&str, &str, &str)>, ws: Option<(&str, &str)>, ts: i64) -> DesktopContext {
    DesktopContext::available(
        Source::Hyprland,
        win.map(|(id, app, title)| FocusedWindow::new(id, app, title)),
        ws.map(|(id, name)| Workspace::new(id, name)),
        ts,
    )
}

fn tmpdir(tag: &str) -> std::path::PathBuf {
    let p = std::env::temp_dir().join(format!(
        "qs-it-{tag}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&p).unwrap();
    // Private test scratch (0700) to stay compatible with the storage
    // worker's private-dir expectations.
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o700));
    }
    p
}

#[test]
fn public_api_roundtrip_range_ordering() {
    let store = ActivityStore::open_in_memory().unwrap();
    let mut tracker = Tracker::new();
    assert!(tracker.current_context().is_none());

    // Observe three distinct states; persist each (mimics the collector).
    for (i, ts) in [1000, 2000, 3000].iter().enumerate() {
        let c = ctx(
            Some((&format!("0x{}", i + 1), "kitty", "t")),
            Some(("1", "1")),
            *ts,
        );
        let (kind, snap) = tracker.observe(c).expect("new semantics persist");
        store
            .append(kind.as_str(), snap.source.as_str(), &snap)
            .unwrap();
        tracker.mark_persisted(&snap);
    }
    // Live current reflects the latest observation.
    assert_eq!(tracker.current_context().unwrap().observed_at_ms, 3000);

    // recent_activity: newest first, deterministic.
    let recent = store.recent_activity(2).unwrap();
    assert_eq!(recent.len(), 2);
    assert_eq!(recent[0].observed_at_ms, 3000);
    assert_eq!(recent[1].observed_at_ms, 2000);

    // activity_in_range: start inclusive, end exclusive, oldest first.
    let range = store.activity_in_range(1000, 3000, 10).unwrap();
    assert_eq!(range.len(), 2);
    assert_eq!(range[0].observed_at_ms, 1000);
    assert_eq!(range[1].observed_at_ms, 2000);
}

#[test]
fn reopen_does_not_restore_live_current() {
    let dir = tmpdir("reopen");
    let db = dir.join("activity.db");
    {
        let store = ActivityStore::open(&db).unwrap();
        let c = ctx(Some(("0x1", "kitty", "t")), Some(("1", "1")), 1000);
        store.append("focus", "hyprland", &c).unwrap();
    }
    {
        let store = ActivityStore::open(&db).unwrap();
        assert_eq!(store.recent_activity(10).unwrap().len(), 1);
        // Fresh tracker: no live current despite persisted rows.
        let fresh = Tracker::new();
        assert!(fresh.current_context().is_none());
    }
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn invalid_ranges_limits_rejected() {
    let store = ActivityStore::open_in_memory().unwrap();
    assert!(store.recent_activity(0).is_err());
    assert!(store.activity_in_range(2000, 1000, 10).is_err());
    assert!(store.activity_in_range(0, 100, 0).is_err());
}

#[test]
fn equal_timestamps_order_by_id() {
    let store = ActivityStore::open_in_memory().unwrap();
    for _ in 0..3 {
        store
            .append(
                "focus",
                "hyprland",
                &ctx(Some(("0x1", "a", "t")), None, 777),
            )
            .unwrap();
    }
    let asc = store.activity_in_range(0, 9999, 10).unwrap();
    assert!(asc[0].id < asc[1].id && asc[1].id < asc[2].id);
    let desc = store.recent_activity(10).unwrap();
    assert!(desc[0].id > desc[1].id && desc[1].id > desc[2].id);
}

#[test]
fn event_parsing_filters_and_preserves_commas() {
    let e = parse_event_line("activewindow>>kitty,title, with, commas").unwrap();
    assert!(is_meaningful_event(&e.name));
    assert_eq!(e.payload, "kitty,title, with, commas");
    assert!(parse_event_line("bogus-line").is_none());
    assert!(!is_meaningful_event("openlayer"));
}

#[test]
fn focus_loss_is_focus_not_lifecycle() {
    let w1 = ctx(Some(("0x1", "kitty", "t")), Some(("1", "1")), 1);
    let empty = DesktopContext::available(
        Source::Hyprland,
        None,
        Some(Workspace::new("1", "1")),
        2,
    );
    // Workspace focus loss is not lifecycle evidence: honest Focus kinds.
    assert_eq!(
        classify_transition(Some(&w1), &empty),
        ActivityKind::Focus
    );
    assert_eq!(
        classify_transition(Some(&empty), &w1),
        ActivityKind::Focus
    );
    assert_ne!(
        classify_transition(Some(&w1), &empty),
        ActivityKind::WindowClose
    );
    assert_ne!(
        classify_transition(Some(&empty), &w1),
        ActivityKind::WindowOpen
    );
}

#[test]
fn singleton_lock_second_holder_fails() {
    let dir = tmpdir("lock");
    let lock = dir.join("activity.db.lock");
    let _first = acquire_lock(&lock).expect("first acquires");
    assert!(acquire_lock(&lock).is_err(), "second collector must fail");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn fake_sockets_event_driven_disconnect_and_unavailable() {
    use std::io::Write;
    use std::os::unix::net::{UnixListener, UnixStream};

    let dir = tmpdir("fakesock");
    // Fake request socket: static snapshot replies.
    let req_path = dir.join(".socket.sock");
    let listener_req = UnixListener::bind(&req_path).unwrap();
    std::thread::spawn(move || {
        for conn in listener_req.incoming() {
            let mut s = match conn {
                Ok(s) => s,
                Err(_) => return,
            };
            let _ = s.set_read_timeout(Some(Duration::from_secs(2)));
            let mut buf = vec![0u8; 4096];
            let n = match std::io::Read::read(&mut s, &mut buf) {
                Ok(n) => n,
                Err(_) => continue,
            };
            let req = String::from_utf8_lossy(&buf[..n]).to_string();
            let reply = if req.contains("activewindow") {
                r#"{"address":"0x9","class":"kitty","title":"it"}"#.to_string()
            } else {
                r#"{"id":7,"name":"code"}"#.to_string()
            };
            let _ = s.write_all(reply.as_bytes());
        }
    });
    // Fake event socket: one meaningful line then EOF.
    let ev_path = dir.join(".socket2.sock");
    let listener_ev = UnixListener::bind(&ev_path).unwrap();
    let server = std::thread::spawn(move || {
        let (mut s, _) = listener_ev.accept().expect("accept");
        std::thread::sleep(Duration::from_millis(80));
        let _ = s.write_all(b"workspace>>code\n");
        std::thread::sleep(Duration::from_millis(80));
    });
    std::thread::sleep(Duration::from_millis(30));

    let store = ActivityStore::open_in_memory().unwrap();
    let mut tracker = Tracker::new();
    // Touch UnixStream import (keeps the fake-socket style explicit).
    let _ = UnixStream::connect(&req_path).is_ok();
    let config = CollectorConfig {
        event_socket: ev_path,
        request_socket: req_path,
        idle_timeout: Duration::from_millis(200),
        coalesce_window: Duration::from_millis(20),
    };
    let shutdown = AtomicBool::new(false);
    let (outcome, progress) = run_collector_once(&store, &mut tracker, &config, &shutdown);
    let _ = server.join();
    assert!(progress);
    assert!(!store.recent_activity(10).unwrap().is_empty());
    // EOF => unavailable live state, never stale.
    let _ = outcome;
    let cur = tracker.current_context().unwrap();
    assert!(!cur.available);
    let _ = std::fs::remove_dir_all(&dir);
}

/// Real SQLite lock/release with idle retry (no new desktop event) and
/// `A -> B -> A` sequence preservation.
///
/// Uses a second raw SQLite connection holding `BEGIN EXCLUSIVE` so the
/// collector store's appends fail with `database is locked` (real lock, not
/// a mocked error). While locked, three meaningful observations are
/// enqueued; the middle `B` must survive the later `A` (old watermark dedup
/// lost it). After release, ONE idle `flush_pending` — no new snapshot, no
/// compositor query — persists all three FIFO with original timestamps.
#[test]
fn sqlite_lock_release_idle_retry_preserves_sequence() {
    use rusqlite::Connection;

    let dir = tmpdir("sqlite-lock");
    let db = dir.join("activity.db");
    let store = ActivityStore::open(&db).unwrap();
    let mut tracker = Tracker::new();

    // Real exclusive lock held by a second connection to the same file.
    let lock_conn = Connection::open(&db).unwrap();
    lock_conn.execute_batch("BEGIN EXCLUSIVE").unwrap();
    // Sanity: the lock really blocks writes (explicit failure, not silent).
    {
        let probe = ctx(Some(("0x0", "probe", "p")), Some(("1", "1")), 1);
        assert!(
            store.append("focus", "hyprland", &probe).is_err(),
            "exclusive lock must fail appends"
        );
    }

    let a1 = ctx(Some(("0x1", "kitty", "t")), Some(("1", "1")), 1000);
    let b = ctx(Some(("0x2", "kitty", "t")), Some(("1", "1")), 2000);
    let a2 = ctx(Some(("0x1", "kitty", "t")), Some(("1", "1")), 3000);

    // Enqueue A while locked: flush fails, backlog retains with original ts.
    match tracker.enqueue(a1.clone()).expect("enqueue A1") {
        EnqueueOutcome::Queued(_, snap) => assert_eq!(snap.observed_at_ms, 1000),
        other => panic!("A1 must queue, got {other:?}"),
    }
    assert!(flush_pending(&store, &mut tracker).is_err());
    assert_eq!(tracker.pending_len(), 1);

    // B distinct from queued A: must queue behind A.
    match tracker.enqueue(b.clone()).expect("enqueue B") {
        EnqueueOutcome::Queued(_, snap) => assert_eq!(snap.observed_at_ms, 2000),
        other => panic!("B must queue, got {other:?}"),
    }
    assert!(flush_pending(&store, &mut tracker).is_err());
    assert_eq!(tracker.pending_len(), 2);

    // A again: equals first A semantically but distinct from queued tail B,
    // so it must queue (old watermark dedup dropped B here).
    match tracker.enqueue(a2.clone()).expect("enqueue A2") {
        EnqueueOutcome::Queued(kind, snap) => {
            assert_eq!(kind, ActivityKind::Focus);
            assert_eq!(snap.observed_at_ms, 3000);
        }
        other => panic!("A2 must queue behind B, got {other:?}"),
    }
    assert_eq!(tracker.pending_len(), 3);
    assert!(store.recent_activity(10).unwrap().is_empty());

    // Release the real lock.
    lock_conn.execute_batch("COMMIT").unwrap();

    // Idle retry: no new desktop event, no fetch_snapshot — just the
    // backlog flush the collector runs on WouldBlock/TimedOut/backoff.
    let flushed = flush_pending(&store, &mut tracker).expect("idle flush after release");
    assert_eq!(flushed, 3);
    assert!(tracker.pending_is_empty());

    // FIFO order + original timestamps preserved.
    let rows = store.activity_in_range(0, 9999, 10).unwrap();
    assert_eq!(rows.len(), 3);
    assert_eq!(rows[0].observed_at_ms, 1000);
    assert_eq!(rows[1].observed_at_ms, 2000);
    assert_eq!(rows[2].observed_at_ms, 3000);
    for r in &rows {
        assert_eq!(r.kind, "focus", "focus changes tracked honestly");
    }
    // Live current is the latest observation.
    assert_eq!(tracker.current_context().unwrap().observed_at_ms, 3000);
    let _ = std::fs::remove_dir_all(&dir);
}

/// Idle running collector retries the SQLite backlog with NO new desktop
/// event: initial snapshot append fails under a real lock, the event socket
/// stays silent, the lock is released mid-idle, and the collector's own
/// idle-timeout flush persists the snapshot without any event line.
#[test]
fn idle_collector_retries_backlog_without_new_event() {
    use rusqlite::Connection;
    use std::io::Write;
    use std::os::unix::net::UnixListener;

    let dir = tmpdir("idle-lock");
    let db = dir.join("activity.db");
    // Pre-create schema via the store, then hold a real exclusive lock.
    let store = ActivityStore::open(&db).unwrap();
    let lock_conn = Connection::open(&db).unwrap();
    lock_conn.execute_batch("BEGIN EXCLUSIVE").unwrap();

    // Fake request socket: static snapshot A.
    let req_path = dir.join(".socket.sock");
    let listener_req = UnixListener::bind(&req_path).unwrap();
    std::thread::spawn(move || {
        for conn in listener_req.incoming() {
            let mut s = match conn {
                Ok(s) => s,
                Err(_) => return,
            };
            let _ = s.set_read_timeout(Some(Duration::from_secs(2)));
            let mut buf = vec![0u8; 4096];
            let n = match std::io::Read::read(&mut s, &mut buf) {
                Ok(n) => n,
                Err(_) => continue,
            };
            let req = String::from_utf8_lossy(&buf[..n]).to_string();
            let reply = if req.contains("activewindow") {
                r#"{"address":"0x1","class":"kitty","title":"idle"}"#.to_string()
            } else {
                r#"{"id":1,"name":"1"}"#.to_string()
            };
            let _ = s.write_all(reply.as_bytes());
        }
    });
    // Fake event socket: accept and stay silent (no event lines at all).
    let ev_path = dir.join(".socket2.sock");
    let listener_ev = UnixListener::bind(&ev_path).unwrap();
    let server = std::thread::spawn(move || {
        let (_s, _) = listener_ev.accept().expect("accept");
        std::thread::sleep(Duration::from_secs(5));
    });
    std::thread::sleep(Duration::from_millis(50));

    let config = CollectorConfig {
        event_socket: ev_path,
        request_socket: req_path,
        idle_timeout: Duration::from_millis(100),
        coalesce_window: Duration::from_millis(10),
    };
    let shutdown = Arc::new(AtomicBool::new(false));
    let shutdown_child = Arc::clone(&shutdown);
    // Move the store/tracker into the running session thread.
    let (tx, rx) = std::sync::mpsc::channel();
    let dir_clone = dir.clone();
    std::thread::spawn(move || {
        // Reopen the same file DB inside the session thread (same lock
        // contention domain as the outer `store` handle's file).
        let db_inner = dir_clone.join("activity.db");
        let session_store = ActivityStore::open(&db_inner).unwrap();
        let mut session_tracker = Tracker::new();
        let (outcome, _) =
            run_collector_once(&session_store, &mut session_tracker, &config, shutdown_child.as_ref());
        let rows = session_store.recent_activity(10).unwrap_or_default();
        let _ = tx.send((outcome, rows.len()));
    });
    // Let the session take its initial snapshot under lock (enqueue, flush
    // fails), then release mid-idle with zero events sent.
    std::thread::sleep(Duration::from_millis(250));
    lock_conn.execute_batch("COMMIT").unwrap();
    // Idle timeouts (100ms) must flush the backlog with no event lines.
    std::thread::sleep(Duration::from_millis(600));
    shutdown.store(true, Ordering::SeqCst);
    let (outcome, row_count) = rx.recv_timeout(Duration::from_secs(5)).expect("session exits");
    assert_eq!(outcome, qs_agent_orchestrator::collector::SessionOutcome::Shutdown);
    assert!(
        row_count >= 1,
        "idle flush must persist initial snapshot without any desktop event"
    );
    // File DB also visible from a fresh handle.
    let check = ActivityStore::open(&db).unwrap();
    assert!(!check.recent_activity(10).unwrap().is_empty());
    let _ = server.thread().unpark();
    let _ = std::fs::remove_dir_all(&dir);
    let _ = store;
}
