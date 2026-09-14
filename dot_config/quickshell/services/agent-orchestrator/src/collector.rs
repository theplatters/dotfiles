//! Collector event loop: subscribe-before-snapshot, bounded reconciliation.
//!
//! The executable owns paths/lock/database; this module owns the
//! reconnect/mark-unavailable/retry contract:
//! - connect `.socket2.sock` FIRST, then take the initial authoritative
//!   snapshot (no missed changes, no fake restored current);
//! - meaningful events trigger ONE bounded snapshot reconciliation (paired
//!   events coalesced, deduped by semantic equality), never polling;
//! - disconnect/snapshot failure clears live state to unavailable (never
//!   retains stale context) and asks the outer loop to reconnect;
//! - transient SQLite failures are durable: meaningful observations are
//!   retained in a bounded FIFO with ORIGINAL timestamps and retried on
//!   idle/backoff independently of compositor queries (never desktop
//!   polling). Semantic dedup is against the latest queued state so
//!   `A -> B -> A` across failures preserves `B` and FIFO order.
//!
//! Bounded overflow policy (explicit gap, finite memory): the backlog holds at
//! most [`crate::desktop_context::MAX_PENDING_OBSERVATIONS`] observations.
//! When full, the overflowing observation is DROPPED with an explicit
//! `observation gap` line on stderr (no reserved slot, no lossless claim);
//! the pre-existing backlog is retained in the `Tracker` for a later flush
//! and the session surfaces `SessionOutcome::BacklogFull` and ends.
//! While the backlog stays full the outer loop enters an explicit
//! storage-blocked state: ONLY persistence retries + backoff, NO
//! discovery/snapshot queries until capacity recovers. While no subscription
//! is held live `current` reads unavailable (local-only marking, never stale
//! available).
//!
//! Shutdown performs a bounded final drain with a fixed deadline
//! ([`SHUTDOWN_DRAIN_DEADLINE`]), checked between individual appends so
//! slow-but-successful rows cannot push the whole drain past it; at most
//! one in-flight SQLite busy-wait (≤100 ms) may overrun. Any remainder is
//! reported as `shutdown incomplete` with its pending count and surfaces
//! as a nonzero outcome (no durable spool — in-memory backlog is lost).
//!
//! Drain coalescing never swallows terminal state: EOF or a non-timeout
//! read error inside the coalesce window returns an explicit terminal
//! outcome; the collector marks unavailable immediately with no subsequent
//! snapshot fetch on the known-closed subscription.

use crate::desktop_context::{
    now_ms, DesktopContext, EnqueueOutcome, PendingFull, Source, Tracker,
    MAX_PENDING_OBSERVATIONS,
};
use crate::desktop_store::{ActivityStore, StoreError};
use crate::hyprland::{
    CONNECT_TIMEOUT, EVENT_IDLE_TIMEOUT, MAX_EVENT_LINE_BYTES, READ_CHUNK, SocketPaths,
    connect_bounded, fetch_snapshot, is_meaningful_event, parse_event_line,
};
use std::io::Read;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

/// Per-session tunables (all bounded).
#[derive(Clone, Debug)]
pub struct CollectorConfig {
    pub event_socket: std::path::PathBuf,
    pub request_socket: std::path::PathBuf,
    /// Idle read timeout (shutdown responsiveness + idle SQLite retry).
    pub idle_timeout: Duration,
    /// Coalesce window to merge paired events before one snapshot.
    pub coalesce_window: Duration,
}

impl CollectorConfig {
    pub fn new(event_socket: &Path, request_socket: &Path) -> Self {
        Self {
            event_socket: event_socket.to_path_buf(),
            request_socket: request_socket.to_path_buf(),
            idle_timeout: EVENT_IDLE_TIMEOUT,
            coalesce_window: Duration::from_millis(50),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SessionOutcome {
    /// Shutdown flag set; caller should exit.
    Shutdown,
    /// Event socket EOF/read failure; live state marked unavailable.
    Disconnected,
    /// Snapshot query failed; live state marked unavailable.
    SnapshotFailed,
    /// Could not connect to the event socket at all.
    ConnectFailed,
    /// Bounded history backlog full: explicit overflow, no silent drop.
    /// Session ends so the outer loop backs off; backlog retained for retry.
    BacklogFull,
}

/// Backoff bounds for the outer reconnect loop.
pub const BACKOFF_INITIAL: Duration = Duration::from_millis(200);
pub const BACKOFF_MAX: Duration = Duration::from_secs(5);
/// Fixed deadline for the bounded shutdown drain (no durable spool).
pub const SHUTDOWN_DRAIN_DEADLINE: Duration = Duration::from_secs(2);

/// Run ONE event session: connect, snapshot, consume events until the socket
/// dies, a snapshot fails, the backlog overflows, or `shutdown` is set.
/// Returns the outcome plus whether any snapshot was successfully observed
/// (for backoff reset; `BacklogFull` reports no progress so backoff grows).
pub fn run_collector_once(
    store: &ActivityStore,
    tracker: &mut Tracker,
    config: &CollectorConfig,
    shutdown: &AtomicBool,
) -> (SessionOutcome, bool) {
    if shutdown.load(Ordering::SeqCst) {
        return (SessionOutcome::Shutdown, false);
    }
    let mut stream = match connect_bounded(&config.event_socket, CONNECT_TIMEOUT) {
        Ok(s) => s,
        Err(e) => {
            eprintln!(
                "qs-desktop-context: event socket connect {} failed: {e}",
                config.event_socket.display()
            );
            if mark_unavailable_via_backlog(store, tracker).is_err() {
                // No subscription held and no backlog room: unavailable,
                // not stale available; overflowing marker already gap-logged.
                tracker.mark_current_unavailable_local(Source::Hyprland, now_ms());
                return (SessionOutcome::BacklogFull, false);
            }
            return (SessionOutcome::ConnectFailed, false);
        }
    };
    if let Err(e) = stream.set_read_timeout(Some(config.idle_timeout)) {
        eprintln!("qs-desktop-context: set read timeout failed: {e}");
        if mark_unavailable_via_backlog(store, tracker).is_err() {
            tracker.mark_current_unavailable_local(Source::Hyprland, now_ms());
            return (SessionOutcome::BacklogFull, false);
        }
        return (SessionOutcome::ConnectFailed, false);
    }

    // Authoritative initial snapshot AFTER subscribing.
    let made_progress = match fetch_snapshot(&config.request_socket) {
        Ok(ctx) => {
            match persist_observation_via_backlog(store, tracker, ctx) {
                Ok(()) => true,
                Err(_) => {
                    // Overflowing snapshot dropped (gap-logged); no
                    // subscription survives, so live reads unavailable.
                    tracker.mark_current_unavailable_local(Source::Hyprland, now_ms());
                    return (SessionOutcome::BacklogFull, false);
                }
            }
        }
        Err(e) => {
            eprintln!("qs-desktop-context: initial snapshot failed: {e}");
            if mark_unavailable_via_backlog(store, tracker).is_err() {
                tracker.mark_current_unavailable_local(Source::Hyprland, now_ms());
                return (SessionOutcome::BacklogFull, false);
            }
            return (SessionOutcome::SnapshotFailed, false);
        }
    };
    let mut made_progress = made_progress;

    let mut pending: Vec<u8> = Vec::new();
    let mut discarding = false;
    let mut chunk = [0u8; READ_CHUNK];

    loop {
        if shutdown.load(Ordering::SeqCst) {
            return (SessionOutcome::Shutdown, made_progress);
        }
        match stream.read(&mut chunk) {
            Ok(0) => {
                eprintln!("qs-desktop-context: event socket EOF (compositor gone?)");
                if mark_unavailable_via_backlog(store, tracker).is_err() {
                    tracker.mark_current_unavailable_local(Source::Hyprland, now_ms());
                    return (SessionOutcome::BacklogFull, made_progress);
                }
                return (SessionOutcome::Disconnected, made_progress);
            }
            Ok(n) => {
                let mut want_refresh = false;
                feed_bytes(
                    &chunk[..n],
                    &mut pending,
                    &mut discarding,
                    &mut want_refresh,
                );
                if want_refresh {
                    // Coalesce paired bursts (activewindow+activewindowv2)
                    // into a single snapshot reconciliation.
                    match drain_coalesce(
                        &mut stream,
                        &mut pending,
                        &mut discarding,
                        &mut want_refresh,
                        config,
                    ) {
                        DrainOutcome::Closed => {
                            eprintln!(
                                "qs-desktop-context: event socket closed during coalesce"
                            );
                            if mark_unavailable_via_backlog(store, tracker).is_err() {
                                tracker.mark_current_unavailable_local(Source::Hyprland, now_ms());
                                return (SessionOutcome::BacklogFull, made_progress);
                            }
                            return (SessionOutcome::Disconnected, made_progress);
                        }
                        DrainOutcome::Ready => {}
                    }
                    match fetch_snapshot(&config.request_socket) {
                        Ok(ctx) => {
                            made_progress = true;
                            match persist_observation_via_backlog(store, tracker, ctx) {
                                Ok(()) => {}
                                Err(_) => {
                                    tracker.mark_current_unavailable_local(Source::Hyprland, now_ms());
                                    return (SessionOutcome::BacklogFull, false);
                                }
                            }
                        }
                        Err(e) => {
                            eprintln!("qs-desktop-context: snapshot refresh failed: {e}");
                            if mark_unavailable_via_backlog(store, tracker).is_err() {
                                tracker.mark_current_unavailable_local(Source::Hyprland, now_ms());
                                return (SessionOutcome::BacklogFull, made_progress);
                            }
                            return (SessionOutcome::SnapshotFailed, made_progress);
                        }
                    }
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                // Idle: retry SQLite backlog independently of compositor
                // queries (no fetch_snapshot here — never desktop polling).
                flush_pending_and_log(store, tracker);
                continue;
            }
            Err(e) if e.kind() == std::io::ErrorKind::TimedOut => {
                // Idle: same backlog retry, no compositor query.
                flush_pending_and_log(store, tracker);
                continue;
            }
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => {
                eprintln!("qs-desktop-context: event socket read failed: {e}");
                if mark_unavailable_via_backlog(store, tracker).is_err() {
                    tracker.mark_current_unavailable_local(Source::Hyprland, now_ms());
                    return (SessionOutcome::BacklogFull, made_progress);
                }
                return (SessionOutcome::Disconnected, made_progress);
            }
        }
    }
}

/// Outer loop with discovery + bounded backoff. `discover` is called each
/// round so disappeared/reappeared sockets recover. Returns the remaining
/// pending count after a bounded shutdown drain when `shutdown` is set.
/// SQLite backlog retries happen on backoff rounds independently of
/// compositor queries (no snapshot polling while sockets are absent).
///
/// Storage-blocked state: while the bounded backlog stays at capacity
/// (`pending_len >= MAX_PENDING_OBSERVATIONS`) the loop retries ONLY
/// persistence + backoff — NO `discover`/snapshot queries — until capacity
/// recovers. Live `current` is forced unavailable (local-only) while no
/// subscription is held. Overflowing observations are dropped with an
/// explicit `observation gap` on stderr (finite memory, no lossless claim).
pub fn run_collector_forever(
    store: &ActivityStore,
    tracker: &mut Tracker,
    shutdown: &AtomicBool,
    discover: impl Fn() -> Option<SocketPaths>,
) -> usize {
    let mut backoff = BACKOFF_INITIAL;
    // Track whether the last round already reported absence, to avoid
    // spamming unavailable appends (dedup covers it, but avoid log spam).
    let mut absence_logged = false;
    let mut storage_blocked_logged = false;
    loop {
        if shutdown.load(Ordering::SeqCst) {
            break;
        }
        // Explicit storage-blocked: full backlog => persistence-only backoff.
        if tracker.pending_len() >= MAX_PENDING_OBSERVATIONS {
            if !storage_blocked_logged {
                eprintln!(
                    "qs-desktop-context: storage blocked (backlog full capacity {}); retrying persistence only, no discovery/snapshot; new observations report observation gaps until capacity recovers",
                    MAX_PENDING_OBSERVATIONS
                );
                storage_blocked_logged = true;
            }
            // No live subscription: live state must read unavailable.
            tracker.mark_current_unavailable_local(Source::Hyprland, now_ms());
            flush_pending_and_log(store, tracker);
            if tracker.pending_len() >= MAX_PENDING_OBSERVATIONS {
                interruptible_sleep(backoff, shutdown);
                backoff = (backoff * 2).min(BACKOFF_MAX);
                continue;
            }
            eprintln!("qs-desktop-context: storage recovered; resuming discovery");
            storage_blocked_logged = false;
            backoff = BACKOFF_INITIAL;
            continue;
        }
        storage_blocked_logged = false;
        let Some(paths) = discover() else {
            if !absence_logged {
                eprintln!("qs-desktop-context: no hyprland sockets; waiting");
                absence_logged = true;
            }
            let _ = mark_unavailable_via_backlog(store, tracker);
            // Backoff retry of the SQLite backlog without any compositor
            // query (independent of desktop polling).
            flush_pending_and_log(store, tracker);
            interruptible_sleep(backoff, shutdown);
            backoff = (backoff * 2).min(BACKOFF_MAX);
            continue;
        };
        absence_logged = false;
        let config = CollectorConfig::new(&paths.event_socket, &paths.request_socket);
        let (outcome, progress) = run_collector_once(store, tracker, &config, shutdown);
        match outcome {
            SessionOutcome::Shutdown => break,
            SessionOutcome::BacklogFull => {
                eprintln!("qs-desktop-context: history backlog full; backing off");
                // Session ended with no subscription: unavailable locally.
                tracker.mark_current_unavailable_local(Source::Hyprland, now_ms());
                // Retry backlog before sleeping: storage may have recovered
                // while the session was ending.
                flush_pending_and_log(store, tracker);
                backoff = (backoff * 2).min(BACKOFF_MAX);
                eprintln!("qs-desktop-context: session ended ({outcome:?}); reconnecting");
                interruptible_sleep(backoff, shutdown);
            }
            _ => {
                // Opportunistic backlog retry across reconnects.
                flush_pending_and_log(store, tracker);
                if progress {
                    backoff = BACKOFF_INITIAL;
                } else {
                    backoff = (backoff * 2).min(BACKOFF_MAX);
                }
                eprintln!("qs-desktop-context: session ended ({outcome:?}); reconnecting");
                interruptible_sleep(backoff, shutdown);
            }
        }
    }
    // Bounded final drain with a fixed deadline (no durable spool).
    let remaining = final_drain(store, tracker);
    if remaining > 0 {
        eprintln!(
            "qs-desktop-context: shutdown incomplete: {remaining} pending observations not persisted (in-memory backlog lost; no durable spool)"
        );
    }
    remaining
}

/// Bounded final drain on shutdown with a fixed deadline.
///
/// Drains row-by-row with the deadline checked between individual appends
/// (never a whole-FIFO [`flush_pending`] call past the deadline). A single
/// in-flight append may overrun the deadline by at most one SQLite
/// busy-wait (≤100 ms); total wall time is therefore bounded by
/// [`SHUTDOWN_DRAIN_DEADLINE`] plus one append. Returns the remaining
/// pending count (0 = fully drained). No durable spool: any remainder is
/// in-memory loss reported by the caller.
pub fn final_drain(store: &ActivityStore, tracker: &mut Tracker) -> usize {
    let deadline = Instant::now() + SHUTDOWN_DRAIN_DEADLINE;
    drain_rows_with_deadline(tracker, deadline, |kind, snap| {
        store
            .append(kind.as_str(), snap.source.as_str(), snap)
            .map(|_| ())
    })
}

/// Deadline-aware per-row drain core.
///
/// Checks the deadline before every row so slow-but-successful appends
/// cannot push the whole drain past it; only the one already-started
/// append may overrun. `append` is injected so tests can simulate
/// slow success deterministically (no wall-clock contention flakiness).
fn drain_rows_with_deadline(
    tracker: &mut Tracker,
    deadline: Instant,
    mut append: impl FnMut(&crate::desktop_context::ActivityKind, &DesktopContext) -> Result<(), StoreError>,
) -> usize {
    loop {
        if tracker.pending_is_empty() {
            break;
        }
        if Instant::now() >= deadline {
            break;
        }
        let Some((kind, snap)) = tracker.pending_front() else {
            break;
        };
        match append(&kind, &snap) {
            Ok(()) => {
                tracker.mark_persisted(&snap);
                let _ = tracker.pop_pending_front();
            }
            Err(e) => {
                eprintln!(
                    "qs-desktop-context: final drain append failed (backlog {}): {e}",
                    tracker.pending_len()
                );
                let remaining = deadline.saturating_duration_since(Instant::now());
                if remaining.is_zero() {
                    break;
                }
                std::thread::sleep(Duration::from_millis(50).min(remaining));
            }
        }
    }
    tracker.pending_len()
}

/// Flush the bounded FIFO in order, preserving original timestamps.
///
/// Attempts each queued `(kind, snapshot)` via `store.append` in FIFO order;
/// on each success advances the watermark (`mark_persisted`) and pops the
/// front. Stops at the first failure so `A -> B -> A` order is preserved
/// (nothing reordered, nothing skipped).
///
/// - `Ok(n)`: backlog drained (or empty); `n` rows newly persisted.
/// - `Err(e)`: first SQLite failure after any prior successes in this call;
///   remaining backlog retained for idle/backoff retry. No compositor query
///   is performed here.
pub fn flush_pending(store: &ActivityStore, tracker: &mut Tracker) -> Result<usize, StoreError> {
    let mut flushed = 0usize;
    while let Some((kind, snap)) = tracker.pending_front() {
        match store.append(kind.as_str(), snap.source.as_str(), &snap) {
            Ok(_) => {
                tracker.mark_persisted(&snap);
                let _ = tracker.pop_pending_front();
                flushed += 1;
            }
            Err(e) => {
                if flushed == 0 {
                    return Err(e);
                }
                return Err(e);
            }
        }
    }
    Ok(flushed)
}

fn flush_pending_and_log(store: &ActivityStore, tracker: &mut Tracker) {
    match flush_pending(store, tracker) {
        Ok(_) => {}
        Err(e) => eprintln!("qs-desktop-context: history append failed (backlog {}): {e}", tracker.pending_len()),
    }
}

/// Enqueue one snapshot then flush FIFO in order.
///
/// Semantic dedup is against the latest queued state (queue-tail wins), so a
/// later `A` behind a pending `B` is retained rather than lost to watermark
/// dedup. Original `observed_at_ms` is preserved end-to-end.
///
/// Returns `Err(PendingFull)` on bounded overflow (explicit, no silent
/// drop); caller must end the session with `BacklogFull`.
fn persist_observation_via_backlog(
    store: &ActivityStore,
    tracker: &mut Tracker,
    ctx: DesktopContext,
) -> Result<(), PendingFull> {
    match tracker.enqueue(ctx) {
        Ok(EnqueueOutcome::Deduplicated) => {
            // Already queued/persisted semantically; still attempt flush so
            // an earlier failure retries even without new distinct state.
            flush_pending_and_log(store, tracker);
            Ok(())
        }
        Ok(EnqueueOutcome::Queued(_, _)) => {
            flush_pending_and_log(store, tracker);
            Ok(())
        }
        Err(full) => {
            eprintln!(
                "qs-desktop-context: observation gap: dropped 1 observation (history backlog full capacity {}); backlog retained, session stopping; history has explicit gap",
                full.capacity
            );
            Err(full)
        }
    }
}

/// Enqueue an unavailable marker then flush. Returns `Err(PendingFull)` when
/// the bounded backlog is full (explicit overflow; session must end).
fn mark_unavailable_via_backlog(
    store: &ActivityStore,
    tracker: &mut Tracker,
) -> Result<(), PendingFull> {
    match tracker.enqueue_unavailable(Source::Hyprland, now_ms()) {
        Ok(_) => {
            flush_pending_and_log(store, tracker);
            Ok(())
        }
        Err(full) => {
            eprintln!(
                "qs-desktop-context: observation gap: unavailable marker not retained (history backlog full capacity {}); stopping session; history has explicit gap",
                full.capacity
            );
            Err(full)
        }
    }
}

/// Feed raw bytes into line framing (bounded, preserves partial lines across
/// reads). Sets `want_refresh` when a meaningful event line completes.
/// Malformed/oversize lines are ignored without touching context.
fn feed_bytes(bytes: &[u8], pending: &mut Vec<u8>, discarding: &mut bool, want_refresh: &mut bool) {
    for &b in bytes {
        if *discarding {
            if b == b'\n' {
                *discarding = false;
            }
            continue;
        }
        if b == b'\n' {
            let line = std::mem::take(pending);
            if let Ok(text) = std::str::from_utf8(&line) {
                if let Some(ev) = parse_event_line(text) {
                    if is_meaningful_event(&ev.name) {
                        *want_refresh = true;
                    }
                }
                // Unknown/malformed: ignored, context untouched.
            }
            continue;
        }
        if pending.len() >= MAX_EVENT_LINE_BYTES {
            pending.clear();
            *discarding = true;
            continue;
        }
        pending.push(b);
    }
}

/// Explicit coalesce-drain outcome: `Ready` means proceed to one snapshot;
/// `Closed` means the subscription is known closed (EOF or non-timeout read
/// error) — caller must mark unavailable immediately with NO subsequent
/// snapshot fetch on the dead subscription.
#[derive(Clone, Debug, PartialEq, Eq)]
enum DrainOutcome {
    Ready,
    Closed,
}

/// Best-effort drain of an immediate burst with a short timeout so paired
/// events share one snapshot. Bounded to a few extra reads.
///
/// Terminal semantics: `Ok(0)` (EOF) or any non-timeout error (connection
/// reset, etc.) returns `Closed` — the main loop must NOT fetch/persist an
/// available snapshot afterwards. Timeouts/interrupts return `Ready` (burst
/// over, proceed to snapshot).
fn drain_coalesce(
    stream: &mut UnixStream,
    pending: &mut Vec<u8>,
    discarding: &mut bool,
    want_refresh: &mut bool,
    config: &CollectorConfig,
) -> DrainOutcome {
    if let Err(e) = stream.set_read_timeout(Some(config.coalesce_window)) {
        let _ = e;
        restore_idle(stream, config);
        return DrainOutcome::Ready;
    }
    let start = Instant::now();
    let mut chunk = [0u8; READ_CHUNK];
    // At most ~3 extra reads or until the coalesce budget elapses.
    let mut outcome = DrainOutcome::Ready;
    for _ in 0..3 {
        if start.elapsed() >= config.coalesce_window + Duration::from_millis(10) {
            break;
        }
        match stream.read(&mut chunk) {
            Ok(0) => {
                outcome = DrainOutcome::Closed;
                break;
            }
            Ok(n) => feed_bytes(&chunk[..n], pending, discarding, want_refresh),
            Err(e)
                if e.kind() == std::io::ErrorKind::TimedOut
                    || e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::Interrupted =>
            {
                break;
            }
            Err(_) => {
                outcome = DrainOutcome::Closed;
                break;
            }
        }
    }
    restore_idle(stream, config);
    outcome
}

fn restore_idle(stream: &mut UnixStream, config: &CollectorConfig) {
    let _ = stream.set_read_timeout(Some(config.idle_timeout));
}

fn interruptible_sleep(dur: Duration, shutdown: &AtomicBool) {
    let start = Instant::now();
    while start.elapsed() < dur {
        if shutdown.load(Ordering::SeqCst) {
            return;
        }
        std::thread::sleep(Duration::from_millis(50).min(dur.saturating_sub(start.elapsed())));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::net::UnixListener;
    use std::sync::Arc;

    #[cfg(test)]
    fn spawn_fake_request_socket(dir: &Path, activewindow: &str, activeworkspace: &str) -> PathBuf {
        use std::io::Write;
        let path = dir.join(".socket.sock");
        let _ = std::fs::remove_file(&path);
        let listener = UnixListener::bind(&path).expect("bind fake request socket");
        let win = activewindow.to_string();
        let ws = activeworkspace.to_string();
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
        path
    }

    use std::path::PathBuf;

    fn tmpdir(tag: &str) -> std::path::PathBuf {
        let p = std::env::temp_dir().join(format!(
            "qs-collector-{tag}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        std::fs::create_dir_all(&p).unwrap();
        // Private collector scratch (0700) to stay compatible with the
        // storage worker's private-dir expectations.
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o700));
        }
        p
    }

    #[test]
    fn event_driven_snapshot_dedup_and_disconnect() {
        let dir = tmpdir("session");
        let req = spawn_fake_request_socket(
            &dir,
            r#"{"address":"0x1","class":"kitty","title":"one"}"#,
            r#"{"id":1,"name":"1"}"#,
        );
        let ev_path = dir.join(".socket2.sock");
        let _ = std::fs::remove_file(&ev_path);
        let listener = UnixListener::bind(&ev_path).unwrap();
        std::thread::sleep(Duration::from_millis(50));

        let store = ActivityStore::open_in_memory().unwrap();
        let mut tracker = Tracker::new();
        let shutdown = Arc::new(AtomicBool::new(false));
        let config = CollectorConfig {
            event_socket: ev_path.clone(),
            request_socket: req.clone(),
            idle_timeout: Duration::from_millis(200),
            coalesce_window: Duration::from_millis(20),
        };

        // Fake event server: accept, then send paired meaningful events with
        // commas in the title payload, then close (EOF => disconnect).
        let server = std::thread::spawn(move || {
            let (mut s, _) = listener.accept().expect("accept");
            std::thread::sleep(Duration::from_millis(100));
            use std::io::Write;
            // Paired burst: two lines back-to-back (should coalesce/dedup
            // to a single extra snapshot at most).
            let _ = s.write_all(b"activewindow>>kitty,one\nactivewindowv2>>0x1\n");
            std::thread::sleep(Duration::from_millis(100));
            // Malformed line + cosmetic event: must not trigger snapshots
            // nor corrupt context.
            let _ = s.write_all(b"garbage-without-separator\nopenlayer>>foo\n");
            std::thread::sleep(Duration::from_millis(100));
            // Oversize line: discarded to newline, no corruption.
            let big = vec![b'x'; MAX_EVENT_LINE_BYTES + 100];
            let _ = s.write_all(&big);
            let _ = s.write_all(b"\n");
            std::thread::sleep(Duration::from_millis(100));
            // Closing the socket yields EOF on the collector side.
        });

        let shutdown_flag = Arc::clone(&shutdown);
        let (outcome, progress) =
            run_collector_once(&store, &mut tracker, &config, shutdown_flag.as_ref());
        let _ = server.join();
        // EOF disconnect expected (server thread dropped the socket).
        assert_eq!(outcome, SessionOutcome::Disconnected);
        assert!(progress, "initial snapshot must count as progress");
        // Initial snapshot persisted; paired duplicate events deduped (at
        // most one extra row from the refresh, not two).
        let rows = store.recent_activity(10).unwrap();
        assert!(!rows.is_empty());
        assert!(
            rows.len() <= 2,
            "paired events must dedup, got {}",
            rows.len()
        );
        // Disconnect marked live state unavailable (no stale current).
        let cur = tracker.current_context().unwrap();
        assert!(!cur.available);
        assert!(cur.focused_window.is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn shutdown_while_running_exits_promptly() {
        // Real active-running shutdown: collector blocks in idle read while
        // the server holds the connection open; another thread sets the
        // shutdown flag mid-run and the session must exit promptly.
        let dir = tmpdir("stop-running");
        let req = spawn_fake_request_socket(&dir, r#"{"address":"0x1"}"#, r#"{"id":1}"#);
        let ev_path = dir.join(".socket2.sock");
        let _ = std::fs::remove_file(&ev_path);
        let listener = UnixListener::bind(&ev_path).unwrap();
        std::thread::sleep(Duration::from_millis(30));
        // Server holds the connection open without sending anything.
        let server = std::thread::spawn(move || {
            let (_s, _) = listener.accept().expect("accept");
            std::thread::sleep(Duration::from_secs(5));
        });
        std::thread::sleep(Duration::from_millis(50));
        let store = ActivityStore::open_in_memory().unwrap();
        let mut tracker = Tracker::new();
        let shutdown = Arc::new(AtomicBool::new(false));
        let config = CollectorConfig {
            event_socket: ev_path,
            request_socket: req,
            idle_timeout: Duration::from_millis(100),
            coalesce_window: Duration::from_millis(10),
        };
        let flag = Arc::clone(&shutdown);
        let _ = &flag;
        // Drive the real session in this thread via a helper thread so we
        // can flip shutdown mid-run.
        let shutdown2 = Arc::clone(&shutdown);
        let config2 = config.clone();
        // Move store/tracker into the session thread by recreating handles:
        // use a scoped session thread with its own store/tracker is not what
        // we assert on; instead run the session inline in a thread that owns
        // store/tracker, then flip the flag from here.
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let (outcome, _) = run_collector_once(&store, &mut tracker, &config2, shutdown2.as_ref());
            let _ = tx.send(outcome);
        });
        // Let the session reach idle-read, then request shutdown mid-run.
        std::thread::sleep(Duration::from_millis(250));
        let start = Instant::now();
        shutdown.store(true, Ordering::SeqCst);
        let outcome = rx.recv_timeout(Duration::from_secs(3)).expect("session must exit");
        assert_eq!(outcome, SessionOutcome::Shutdown);
        assert!(
            start.elapsed() < Duration::from_secs(3),
            "shutdown must be prompt"
        );
        // Detach server (it sleeps, but the test process exits the listener).
        std::thread::sleep(Duration::from_millis(50));
        let _ = server.thread().unpark();
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn snapshot_failure_marks_unavailable() {
        let dir = tmpdir("snapfail");
        // Request socket path that nothing listens on.
        let missing = dir.join("missing.socket.sock");
        let ev_path = dir.join(".socket2.sock");
        let _ = std::fs::remove_file(&ev_path);
        let listener = UnixListener::bind(&ev_path).unwrap();
        let _server = std::thread::spawn(move || {
            let (_s, _) = listener.accept().expect("accept");
            std::thread::sleep(Duration::from_secs(2));
        });
        std::thread::sleep(Duration::from_millis(30));
        let store = ActivityStore::open_in_memory().unwrap();
        let mut tracker = Tracker::new();
        let shutdown = AtomicBool::new(false);
        let config = CollectorConfig {
            event_socket: ev_path,
            request_socket: missing,
            idle_timeout: Duration::from_millis(100),
            coalesce_window: Duration::from_millis(10),
        };
        let (outcome, _) = run_collector_once(&store, &mut tracker, &config, &shutdown);
        assert_eq!(outcome, SessionOutcome::SnapshotFailed);
        let cur = tracker.current_context().unwrap();
        assert!(!cur.available, "failed snapshot => unavailable, not stale");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn coalesce_close_marks_unavailable_without_snapshot() {
        // EOF during the coalesce window must end the session as
        // Disconnected with unavailable live state — never a subsequent
        // available snapshot fetch on the known-closed subscription.
        let dir = tmpdir("coalesce-close");
        let req = spawn_fake_request_socket(
            &dir,
            r#"{"address":"0x1","class":"kitty","title":"one"}"#,
            r#"{"id":1,"name":"1"}"#,
        );
        let ev_path = dir.join(".socket2.sock");
        let _ = std::fs::remove_file(&ev_path);
        let listener = UnixListener::bind(&ev_path).unwrap();
        std::thread::sleep(Duration::from_millis(30));
        let server = std::thread::spawn(move || {
            use std::io::Write;
            let (mut s, _) = listener.accept().expect("accept");
            std::thread::sleep(Duration::from_millis(80));
            // One meaningful line, then immediate close: the close races the
            // coalesce drain and must be treated as terminal.
            let _ = s.write_all(b"workspace>>code\n");
            // Drop without delay => EOF visible inside drain_coalesce.
        });
        std::thread::sleep(Duration::from_millis(30));
        let store = ActivityStore::open_in_memory().unwrap();
        let mut tracker = Tracker::new();
        let shutdown = AtomicBool::new(false);
        let config = CollectorConfig {
            event_socket: ev_path,
            request_socket: req,
            idle_timeout: Duration::from_millis(200),
            coalesce_window: Duration::from_millis(100),
        };
        let (outcome, _) = run_collector_once(&store, &mut tracker, &config, &shutdown);
        let _ = server.join();
        assert_eq!(outcome, SessionOutcome::Disconnected);
        let cur = tracker.current_context().unwrap();
        assert!(!cur.available, "coalesce EOF => unavailable, no stale snapshot");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn final_drain_stops_between_rows_on_slow_success() {
        // Deterministic slow-success: injected append sleeps 100 ms per row
        // and always succeeds. The deadline must be honored between rows —
        // only the one in-flight append may overrun — with FIFO order kept.
        use crate::desktop_context::{FocusedWindow, Workspace};
        let mut tracker = Tracker::new();
        for i in 0..10 {
            let ctx = DesktopContext::available(
                Source::Hyprland,
                Some(FocusedWindow::new("0x1", "kitty", &format!("slow{i}"))),
                Some(Workspace::new("1", "1")),
                1000 + i,
            );
            tracker.enqueue(ctx).expect("queue");
        }
        assert_eq!(tracker.pending_len(), 10);
        let per_row = Duration::from_millis(100);
        let budget = Duration::from_millis(250);
        let deadline = Instant::now() + budget;
        let start = Instant::now();
        let remaining = drain_rows_with_deadline(&mut tracker, deadline, |_, _| {
            std::thread::sleep(per_row);
            Ok(())
        });
        let elapsed = start.elapsed();
        let drained = 10 - remaining;
        assert!(
            (2..=3).contains(&drained),
            "250 ms budget at 100 ms/row must drain 2-3 rows, drained {drained}"
        );
        assert!(remaining > 0, "slow success must leave a reported remainder");
        assert!(
            elapsed < budget + per_row + Duration::from_millis(150),
            "only one in-flight append may overrun, took {elapsed:?}"
        );
        // FIFO order: the oldest rows drained first (watermark advanced).
        let persisted = tracker.last_persisted().expect("watermark");
        assert_eq!(
            persisted.observed_at_ms,
            1000 + drained as i64 - 1,
            "watermark must reflect FIFO prefix"
        );
        let front = tracker.pending_front().expect("remainder front");
        assert_eq!(
            front.1.observed_at_ms,
            1000 + drained as i64,
            "remainder must resume right after the drained prefix"
        );
    }
}
