#![recursion_limit = "256"]
mod cli;
mod history;
mod pi_child;
mod proto;
mod state;
mod transport;

use cli::Config;
use pi_child::PiHandles;
use proto::{UiEvent, UiInput};
use serde_json::{json, Value};
use state::AgentState;
use std::os::unix::io::{AsRawFd, RawFd};
use std::sync::{
    atomic::{AtomicBool, AtomicU64, Ordering},
    mpsc::{self, RecvTimeoutError, SyncSender},
};
use std::time::{Duration, Instant};
use transport::{Framed, LineFramer, READ_CHUNK};

/// Global termination flag: signal handlers only set this (async-signal
/// safe). The main loop polls it every tick and runs the supervised
/// shutdown (TERM → finite wait → KILL → reap over the tracked pgid), so a
/// resistant descendant can never orphan even when the direct child already
/// exited. Nothing here calls `_exit` anymore.
static TERMINATED: AtomicBool = AtomicBool::new(false);

extern "C" fn term_handler(_sig: i32) {
    TERMINATED.store(true, Ordering::SeqCst);
}

fn install_signal_handlers() {
    unsafe {
        libc::signal(libc::SIGTERM, term_handler as *const () as usize);
        libc::signal(libc::SIGINT, term_handler as *const () as usize);
        libc::signal(libc::SIGPIPE, libc::SIG_IGN);
    }
}

/// Bound for the single shared main-event queue. UI command frames must be
/// acked and Pi protocol frames must never be dropped, so both senders wait
/// finitely and then latch terminal failure instead of growing memory.
pub const MAIN_QUEUE_BOUND: usize = 64;
/// Finite bound for a Pi protocol frame to wait for queue space. Saturation
/// persisting this long means the state loop is wedged; the sender latches
/// terminal failure so gates can never hang forever on a dropped ACK.
pub const PI_SEND_TIMEOUT: Duration = Duration::from_secs(1);
/// Per-write deadline for Pi stdin and UI stdout. Short enough that Stop,
/// approval expiry and EOF stay responsive; expiry is a fail-closed
/// teardown, never an unbounded retry loop.
pub const WRITE_DEADLINE: Duration = Duration::from_millis(300);
/// Grace period for supervised group shutdowns (TERM → KILL escalation).
pub const GROUP_TERM_WAIT: Duration = Duration::from_secs(3);
/// Deferred resume window after an idle stop settles (cancellable).
const RESUME_DELAY: Duration = Duration::from_millis(20);
/// Main-loop tick: expiry, EOF/signal and group supervision stay timely.
const LOOP_TICK: Duration = Duration::from_millis(50);

/// Counters for Pi stderr output discarded under backpressure
/// (bounded-channel full). Protocol frames are never shed; only diagnostics
/// may be, and the drop is surfaced throttled instead of hanging the loop.
#[derive(Debug, Default)]
struct Dropped {
    diags: AtomicU64,
}

enum MainEvent {
    Ui(Result<UiInput, UiParseError>),
    PiLine {
        generation: u64,
        line: Result<Value, String>,
    },
    PiDiag {
        generation: u64,
        line: String,
    },
    StdinEof,
}

#[derive(Debug)]
struct UiParseError {
    id: Option<String>,
    message: String,
}

/// UI stdin pump: chunked reads through a persistent [`LineFramer`], so
/// coalesced commands in one write all surface and an oversize line's
/// trailing newline in the same chunk still acks promptly. Sends over a
/// bounded channel with a finite fail-closed timeout; never buffers
/// unboundedly.
fn stdin_thread(tx: SyncSender<MainEvent>) {
    use crate::proto::MAX_LINE_BYTES;
    let stdin = std::io::stdin();
    let mut handle = stdin.lock();
    let mut framer = LineFramer::new(MAX_LINE_BYTES);
    let mut chunk = [0u8; READ_CHUNK];
    loop {
        if TERMINATED.load(Ordering::SeqCst) {
            return;
        }
        let n = {
            use std::io::Read;
            match handle.read(&mut chunk) {
                Ok(n) => n,
                Err(_) => 0,
            }
        };
        if n == 0 {
            if let Some(frame) = framer.flush() {
                push_ui_frame(&tx, frame);
            }
            send_finite(&tx, MainEvent::StdinEof, Duration::from_secs(1));
            return;
        }
        for frame in framer.push(&chunk[..n]) {
            push_ui_frame(&tx, frame);
            if TERMINATED.load(Ordering::SeqCst) {
                return;
            }
        }
    }
}

/// Finite bounded send for must-not-drop UI frames: retries `try_send`
/// until `timeout`, servicing termination, then fails closed via the flag.
/// Never blocks the reader thread without limit.
fn send_finite(tx: &SyncSender<MainEvent>, event: MainEvent, timeout: Duration) -> bool {
    send_finite_retry(tx, event, Instant::now() + timeout)
}

fn send_finite_retry(tx: &SyncSender<MainEvent>, mut event: MainEvent, deadline: Instant) -> bool {
    loop {
        match tx.try_send(event) {
            Ok(()) => return true,
            Err(mpsc::TrySendError::Full(returned)) => {
                event = returned;
                if TERMINATED.load(Ordering::SeqCst) || Instant::now() >= deadline {
                    TERMINATED.store(true, Ordering::SeqCst);
                    return false;
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            Err(mpsc::TrySendError::Disconnected(_)) => return false,
        }
    }
}
fn push_ui_frame(tx: &SyncSender<MainEvent>, frame: Framed) {
    let event = match frame {
        Framed::Line(buf) => {
            if buf.iter().all(|b| b.is_ascii_whitespace()) {
                return;
            }
            match serde_json::from_slice::<UiInput>(&buf) {
                Ok(input) => MainEvent::Ui(Ok(input)),
                Err(e) => {
                    let text = String::from_utf8_lossy(&buf);
                    // Truncate the echo: the full line is already bounded, but
                    // the failure message must stay small.
                    let short: String = text.chars().take(200).collect();
                    MainEvent::Ui(Err(UiParseError {
                        id: transport::extract_id_hint(&buf),
                        message: format!("Invalid UI command: {e} {short}"),
                    }))
                }
            }
        }
        Framed::Oversize(prefix) => MainEvent::Ui(Err(UiParseError {
            id: transport::extract_id_hint(&prefix),
            message: "UI command exceeded 4 MiB; rejected without writes".to_string(),
        })),
    };
    // UI commands must be acked; dropping one would hang QML pending gates.
    // Bounded wait, then fail closed via the termination flag.
    send_finite(tx, event, Duration::from_secs(1));
}

fn spawn_pi_readers(
    tx: SyncSender<MainEvent>,
    stdout: std::process::ChildStdout,
    stderr: std::process::ChildStderr,
    generation: u64,
    dropped: std::sync::Arc<Dropped>,
) {
    use crate::proto::MAX_LINE_BYTES;
    let tx_out = tx.clone();
    std::thread::spawn(move || {
        use std::io::Read;
        let mut src = stdout;
        let mut framer = LineFramer::new(MAX_LINE_BYTES);
        let mut chunk = [0u8; READ_CHUNK];
        loop {
            if TERMINATED.load(Ordering::SeqCst) {
                return;
            }
            match src.read(&mut chunk) {
                Ok(0) => {
                    if let Some(frame) = framer.flush() {
                        push_pi_frame(&tx_out, generation, frame);
                    }
                    return;
                }
                Ok(n) => {
                    for frame in framer.push(&chunk[..n]) {
                        push_pi_frame(&tx_out, generation, frame);
                    }
                }
                Err(_) => return,
            }
        }
    });
    std::thread::spawn(move || {
        use std::io::Read;
        let mut src = stderr;
        let mut framer = LineFramer::new(transport::STDERR_LINE_MAX);
        let mut chunk = [0u8; READ_CHUNK];
        loop {
            if TERMINATED.load(Ordering::SeqCst) {
                return;
            }
            match src.read(&mut chunk) {
                Ok(0) => {
                    if let Some(frame) = framer.flush() {
                        push_diag_frame(&tx, &dropped, generation, frame);
                    }
                    return;
                }
                Ok(n) => {
                    for frame in framer.push(&chunk[..n]) {
                        push_diag_frame(&tx, &dropped, generation, frame);
                    }
                }
                Err(_) => return,
            }
        }
    });
}

/// Bounded finite send for Pi protocol frames: they must NEVER be dropped
/// (a dropped new/restore ACK hangs an infinite gate, a dropped approval
/// skips expiry). Waits finitely for queue space, then latches terminal
/// failure so the bridge tears down fail-closed instead of hanging a gate
/// forever. Returns false when the terminal latch was engaged.
fn send_pi_frame(tx: &SyncSender<MainEvent>, event: MainEvent) -> bool {
    send_pi_frame_timeout(tx, event, PI_SEND_TIMEOUT)
}

fn send_pi_frame_timeout(
    tx: &SyncSender<MainEvent>,
    mut event: MainEvent,
    timeout: Duration,
) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        match tx.try_send(event) {
            Ok(()) => return true,
            Err(mpsc::TrySendError::Full(returned)) => {
                event = returned;
                if TERMINATED.load(Ordering::SeqCst) || Instant::now() >= deadline {
                    // Saturation outlived the finite bound: fail closed.
                    // Terminating the bridge surfaces every stuck gate as an
                    // explicit failure on the QML retry path.
                    TERMINATED.store(true, Ordering::SeqCst);
                    return false;
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            Err(mpsc::TrySendError::Disconnected(_)) => return false,
        }
    }
}

fn push_pi_frame(tx: &SyncSender<MainEvent>, generation: u64, frame: Framed) {
    let event = match frame {
        Framed::Line(buf) => {
            if buf.iter().all(|b| b.is_ascii_whitespace()) {
                return;
            }
            match serde_json::from_slice::<Value>(&buf) {
                Ok(v) => MainEvent::PiLine {
                    generation,
                    line: Ok(v),
                },
                Err(_) => MainEvent::PiLine {
                    generation,
                    line: Err("Invalid pi RPC output".to_string()),
                },
            }
        }
        Framed::Oversize(_) => MainEvent::PiLine {
            generation,
            line: Err("Pi output exceeded 4 MiB; discarded".to_string()),
        },
    };
    // Protocol frames are never shed: malformed/oversize lines still reach
    // the state loop, which terminates the session fail-closed (no replay)
    // instead of leaving gates stuck.
    send_pi_frame(tx, event);
}

fn push_diag_frame(tx: &SyncSender<MainEvent>, dropped: &Dropped, generation: u64, frame: Framed) {
    let text = match frame {
        Framed::Line(buf) => String::from_utf8_lossy(&buf).into_owned(),
        Framed::Oversize(prefix) => String::from_utf8_lossy(&prefix).into_owned() + "… [truncated]",
    };
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return;
    }
    // Char-boundary-safe tail; the old byte slice could split UTF-8.
    let bounded = transport::tail_str(trimmed, transport::STDERR_LINE_MAX).to_string();
    if tx
        .try_send(MainEvent::PiDiag {
            generation,
            line: bounded,
        })
        .is_err()
    {
        dropped.diags.fetch_add(1, Ordering::SeqCst);
    }
}

struct Bridge {
    cfg: Config,
    state: AgentState,
    pi: Option<PiHandles>,
    /// Tracked process group through cleanup, independent of the direct
    /// child handle: stays `Some` until the whole group is confirmed dead,
    /// so a resistant descendant cannot orphan when the leader exits first.
    /// Cleared only when `group_alive` is false. Never reused across
    /// generations without that confirmation (spawn refuses overlap).
    supervised_pgid: Option<i32>,
    pi_generation: u64,
    idle_stop_deadline: Option<Instant>,
    resume_due: Option<Instant>,
    last_expiry: Instant,
    last_drop_notice: Instant,
    tx: SyncSender<MainEvent>,
    dropped: std::sync::Arc<Dropped>,
    stdout_fd: RawFd,
    /// Latched on the FIRST transport failure (write timeout/close, oversize
    /// outbound, malformed/oversize Pi output, queue saturation). While set,
    /// no further Pi writes happen and start/control ops fail explicitly, so
    /// a partial stdin frame can never be extended and the cleanup deadline
    /// below is never reset.
    transport_dead: bool,
    /// Instant of the first transport failure. Immutable: escalation and
    /// shutdown reason off this anchor exactly once.
    transport_fail_at: Option<Instant>,
    /// Set alongside the latch: emit the pending failure update first, then
    /// shut down and exit so QML takes its dead/retry path.
    exit_after_emit: bool,
}

impl Bridge {
    fn child_live(&mut self) -> bool {
        if self.pi.is_none() {
            return false;
        }
        // Reap check without blocking.
        if let Some(h) = self.pi.as_mut() {
            match h.child.try_wait() {
                Ok(Some(_)) => false,
                Ok(None) => true,
                Err(_) => false,
            }
        } else {
            false
        }
    }

    /// Deadline write of Pi RPC lines to the nonblocking child stdin.
    ///
    /// First failure latches the transport unusable fail-closed: the group
    /// is torn down, `ready` drops (via the state flags below), and every
    /// later op fails explicitly without writing, so subsequent bytes can
    /// never append onto a partial frame and the escalation deadline is
    /// never extended. An oversize outbound line is a bug, not a skip: it
    /// fails closed the same way instead of silently continuing.
    fn write_pi(&mut self, values: Vec<Value>) -> bool {
        use crate::proto::MAX_LINE_BYTES;
        if self.transport_dead {
            return false;
        }
        let raw: RawFd = match self.pi.as_ref() {
            Some(h) => h.stdin.as_raw_fd(),
            None => return false,
        };
        for v in values {
            if v.is_null() {
                continue;
            }
            let mut s = match serde_json::to_string(&v) {
                Ok(s) => s,
                Err(_) => continue,
            };
            s.push('\n');
            if s.len() > MAX_LINE_BYTES {
                // Outbound over the framing bound: fail closed, never skip.
                self.note_transport_failure();
                return false;
            }
            match transport::write_all_deadline(raw, s.as_bytes(), WRITE_DEADLINE, &TERMINATED) {
                Ok(()) => {}
                Err(transport::WriteFail::Terminated) => return false,
                Err(_) => {
                    // Backpressure or closed peer: latch fail-closed. The
                    // exit poll/shutdown path below produces the failure
                    // update; no further writes will extend a partial frame.
                    self.note_transport_failure();
                    return false;
                }
            }
        }
        true
    }

    /// Latch the FIRST transport failure. Idempotent: the escalation anchor
    /// is recorded once and never reset, and the generation bump invalidates
    /// any in-flight frames from the dead generation so no further writes or
    /// events from it are honored.
    fn note_transport_failure(&mut self) {
        if self.transport_dead {
            return;
        }
        self.transport_dead = true;
        self.transport_fail_at = Some(Instant::now());
        // Drop `ready` explicitly: the transport can no longer carry RPC, so
        // start/control gates must read closed until the bridge exits.
        self.state.process_started = false;
        self.state.state_ok = false;
        self.state.extension_ok = false;
        self.state.busy = false;
        self.state.status = "Pi transport failed; retry".to_string();
        self.pi_generation = self.pi_generation.wrapping_add(1);
        self.teardown_live_group();
        self.exit_after_emit = true;
    }

    /// Fail-closed handling for malformed/oversize Pi stdout: terminate the
    /// session with an explicit failure and never replay. A bare `failed`
    /// event would leave prompt/control gates stuck against a live-looking
    /// session; the latch above releases them via `ready=false` instead.
    fn fail_pi_output(&mut self, message: &str, events: &mut Vec<UiEvent>) {
        events.push(UiEvent::new(
            "failed",
            vec![json!(format!(
                "{message}; session terminated without replay"
            ))],
        ));
        self.note_transport_failure();
    }

    /// Runs the pending-exit shutdown exactly once after its failure update
    /// was emitted. Returns true when the caller must break out of the loop.
    fn take_exit(&mut self) -> bool {
        if self.exit_after_emit {
            self.exit_after_emit = false;
            self.op_shutdown();
            true
        } else {
            false
        }
    }

    /// Render (lifecycle-complete, display-budgeted) and deadline-write one
    /// update line to UI stdout. On backpressure the bridge fails closed via
    /// the termination flag; the line is dropped rather than queued without
    /// bound.
    fn emit(&mut self, events: Vec<UiEvent>, ack: Option<String>, accepted: Option<bool>) {
        let rendered = transport::render_update(&mut self.state, events, ack, accepted);
        for w in rendered.pi_writes {
            self.write_pi(vec![w]);
        }
        if rendered.fatal {
            // Terminal fail-closed signal from the snapshot worker: the frame
            // above still carries the FULL lifecycle/identity snapshot (never
            // a partial fallback), but the bridge is unusable from here.
            // Latch, tear down, and exit after this update so QML takes its
            // dead/retry path instead of hanging on the generation.
            self.note_transport_failure();
        }
        match transport::write_all_deadline(
            self.stdout_fd,
            &rendered.line,
            WRITE_DEADLINE,
            &TERMINATED,
        ) {
            Ok(()) => {}
            Err(transport::WriteFail::Terminated) => {}
            Err(_) => {
                // UI stdout backpressure/close: fail closed. QML is gone or
                // not draining; terminate promptly instead of buffering
                // updates without limit.
                TERMINATED.store(true, Ordering::SeqCst);
            }
        }
    }

    fn do_spawn(&mut self, events: &mut Vec<UiEvent>) -> bool {
        if self.rejected_transport_dead(events) {
            return false;
        }
        // Never overlap generations: a previously supervised group must be
        // confirmed dead before its pgid can be reused.
        if let Some(pgid) = self.supervised_pgid {
            if pi_child::group_alive(pgid) {
                pi_child::teardown_group(
                    pgid,
                    self.pi.as_mut().map(|h| &mut h.child),
                    GROUP_TERM_WAIT,
                );
                self.pi = None;
                if pi_child::group_alive(pgid) {
                    self.state.process_start_failed = true;
                    self.state.start_requested = false;
                    self.state.status = "Previous session group did not exit; retry".to_string();
                    events.push(UiEvent::new(
                        "failed",
                        vec![json!("Previous session group did not exit; retry")],
                    ));
                    return false;
                }
            }
            self.supervised_pgid = None;
        }
        let res = pi_child::spawn_pi(
            &self.cfg,
            &self.state.session_file,
            &self.state.session_name,
            self.state.fresh_session,
        );
        match res {
            Ok(mut handles) => {
                let stdout = handles.child.stdout.take();
                let stderr = handles.child.stderr.take();
                self.pi_generation += 1;
                let gen = self.pi_generation;
                if let (Some(o), Some(e)) = (stdout, stderr) {
                    spawn_pi_readers(self.tx.clone(), o, e, gen, self.dropped.clone());
                }
                self.supervised_pgid = Some(handles.pgid);
                self.pi = Some(handles);
                self.state.process_started = true;
                self.state.start_requested = false;
                self.state.process_start_failed = false;
                self.state.idle_stopped = false;
                let writes = self.state.start_ready_writes();
                self.write_pi(writes);
                true
            }
            Err(e) => {
                // Mirror FailedToStart: release the startup gate, expose Retry.
                self.state.process_start_failed = true;
                self.state.start_requested = false;
                self.state.desired_running = false;
                self.state.idle_stopping = false;
                self.state.idle_stopped = false;
                self.state.pending_resume = false;
                self.state.status = "pi could not start".to_string();
                events.push(UiEvent::new(
                    "failed",
                    vec![json!("pi process could not start")],
                ));
                eprintln!("bridge spawn failed: {e}");
                false
            }
        }
    }

    /// TERM a live group without blocking the loop; the exit poll and the
    /// group reaper below finish the job (escalation included).
    fn teardown_live_group(&mut self) {
        if let Some(h) = self.pi.as_mut() {
            unsafe {
                libc::killpg(h.pgid, libc::SIGTERM);
            }
        } else if let Some(pgid) = self.supervised_pgid {
            unsafe {
                libc::killpg(pgid, libc::SIGTERM);
            }
        }
        self.idle_stop_deadline = Some(Instant::now() + GROUP_TERM_WAIT);
    }

    /// Poll the supervised group: escalate TERM → KILL while it stays alive,
    /// clear supervision once it is gone. Runs every tick; never blocks.
    /// After a latched transport failure the escalation anchor is immutable:
    /// a surviving group is KILLed once the fixed grace period elapses, and
    /// the deadline is never extended by later events.
    fn poll_group(&mut self) {
        let Some(pgid) = self.supervised_pgid else {
            return;
        };
        if !pi_child::group_alive(pgid) {
            self.supervised_pgid = None;
            self.idle_stop_deadline = None;
            return;
        }
        if self.transport_dead {
            if self
                .transport_fail_at
                .is_some_and(|t0| Instant::now() >= t0 + GROUP_TERM_WAIT)
            {
                unsafe {
                    libc::killpg(pgid, libc::SIGKILL);
                }
                self.idle_stop_deadline = None;
            }
            return;
        }
        // Direct child already gone but the group survives (resistant
        // descendant): escalate promptly rather than waiting out the clock.
        let child_gone = self.pi.is_none();
        if child_gone || self.idle_stop_deadline.is_some_and(|d| Instant::now() >= d) {
            unsafe {
                libc::killpg(pgid, libc::SIGKILL);
            }
            self.idle_stop_deadline = None;
        }
    }

    fn handle_exit(&mut self, code: i32, events: &mut Vec<UiEvent>) {
        // Unresolved refresh on exit fails for all modes (palette included):
        // never cache a half-replaced identity for the next spawn.
        let lost = self.state.session_refresh_pending;
        self.state.process_started = false;
        self.state.start_requested = false;
        self.state.request_kinds.clear();
        let _ = self.state.reject_all_requests(events);
        self.state.state_ok = false;
        self.state.extension_ok = false;
        self.state.control_pending = false;
        self.state.stopping = false;
        self.state.busy = false;
        self.state.compacting = false;
        self.state.session_switching = false;
        self.state.session_refresh_pending = false;
        if lost {
            self.state.session_refresh_failed = true;
            self.state.settle_session_change();
            self.state.session_file.clear();
            self.state.session_name.clear();
        }
        let was_idle = self.state.idle_stopping || self.state.idle_stopped;
        let restart = was_idle && self.state.desired_running;
        self.state.idle_stopping = false;
        self.idle_stop_deadline = None;
        self.state.status = if lost {
            "Session refresh failed; retry".to_string()
        } else if self.state.process_start_failed {
            "pi could not start".to_string()
        } else if was_idle {
            if restart {
                "Restarting pi…".to_string()
            } else {
                "Paused (session preserved)".to_string()
            }
        } else {
            "pi stopped".to_string()
        };
        if was_idle {
            self.state.process_start_failed = false;
        }
        if !was_idle {
            self.state.desired_running = false;
        }
        if restart {
            self.state.pending_resume = true;
            self.resume_due = Some(Instant::now() + RESUME_DELAY);
        }
        // Drop the direct handle (already reaped by the caller). The pgid
        // stays supervised until the whole group is confirmed dead, so a
        // resistant descendant is escalated by poll_group instead of
        // orphaning when the leader exits first.
        self.pi = None;
        if code != 0 && !self.state.process_start_failed && !was_idle {
            events.push(UiEvent::new(
                "failed",
                vec![json!(format!(
                    "pi interrupted (exit {code}); no prompt was replayed"
                ))],
            ));
        }
    }

    fn process_ui(&mut self, input: UiInput) -> (Vec<UiEvent>, Option<String>, Option<bool>, bool) {
        let mut events: Vec<UiEvent> = Vec::new();
        let ack = input.id.clone();
        if input.version != Some(1) {
            events.push(UiEvent::new(
                "failed",
                vec![json!("Unsupported UI protocol version")],
            ));
            return (events, ack, Some(false), false);
        }
        let args = input.args.clone().unwrap_or(json!({}));
        match input.op.as_str() {
            "start" => {
                let accepted = self.op_start(&mut events);
                (events, ack, Some(accepted), false)
            }
            "stopIdle" => {
                let accepted = self.op_stop_idle(&mut events);
                (events, ack, Some(accepted), false)
            }
            "prompt" => {
                let message = args
                    .get("message")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let images = args.get("images").cloned();
                let accepted = self.op_prompt(&message, images, &mut events);
                (events, ack, Some(accepted), false)
            }
            "abort" => {
                self.op_abort(&mut events);
                (events, ack, Some(true), false)
            }
            "newSession" => {
                let accepted = self.op_new_session(&mut events);
                (events, ack, Some(accepted), false)
            }
            "switchSession" => {
                let accepted = self.op_switch_session(&mut events);
                (events, ack, Some(accepted), false)
            }
            "rename" => {
                let name = args
                    .get("name")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let accepted = self.op_rename(&name, &mut events);
                (events, ack, Some(accepted), false)
            }
            "requestMessages" => {
                let accepted = self.op_request_messages(&mut events);
                (events, ack, Some(accepted), false)
            }
            "requestStats" => {
                let accepted = self.op_request_stats(&mut events);
                (events, ack, Some(accepted), false)
            }
            "respond" => {
                let request_id = args
                    .get("requestId")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let fields = args.get("fields").cloned().unwrap_or(json!({}));
                let accepted = self.op_respond(&request_id, &fields, &mut events);
                (events, ack, Some(accepted), false)
            }
            "surfaceRequest" => {
                let request_id = args
                    .get("requestId")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let accepted = self.op_surface_request(&request_id, &mut events);
                (events, ack, Some(accepted), false)
            }
            "deferRequest" => {
                let request_id = args
                    .get("requestId")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let accepted = self.op_defer_request(&request_id, &mut events);
                (events, ack, Some(accepted), false)
            }
            "compact" => {
                let accepted = self.op_compact(&mut events);
                (events, ack, Some(accepted), false)
            }
            "chooseModel" => {
                let item = args.get("item").cloned().unwrap_or(Value::Null);
                let accepted = self.op_choose_model(&item, &mut events);
                (events, ack, Some(accepted), false)
            }
            "shutdown" => {
                let _ = self.op_shutdown();
                (events, ack, Some(true), true)
            }
            other => {
                events.push(UiEvent::new(
                    "failed",
                    vec![json!(format!("Unknown UI op: {other}"))],
                ));
                (events, ack, Some(false), false)
            }
        }
    }

    fn ensure_live(&mut self) -> bool {
        self.child_live() && self.state.process_started
    }

    /// Explicit rejection once the transport latch is set. The bridge is
    /// already on its way out; every control reports closed instead of
    /// queuing work onto a dead generation.
    fn rejected_transport_dead(&self, events: &mut Vec<UiEvent>) -> bool {
        if self.transport_dead {
            events.push(UiEvent::new(
                "failed",
                vec![json!("Pi transport failed; retry")],
            ));
            return true;
        }
        false
    }

    fn op_start(&mut self, events: &mut Vec<UiEvent>) -> bool {
        if self.rejected_transport_dead(events) {
            return false;
        }
        self.state.desired_running = true;
        if self.state.idle_stopping {
            return true;
        }
        if self.state.pending_resume {
            return true;
        }
        if self.child_live() || self.state.process_started || self.state.start_requested {
            if self.state.process_started && self.state.session_refresh_failed {
                self.state.session_refresh_failed = false;
                self.state.session_refresh_pending = false;
                let writes = self.state.start_ready_writes();
                self.write_pi(writes);
                return true;
            }
            return false;
        }
        self.state.process_start_failed = false;
        self.state.session_refresh_failed = false;
        self.state.session_refresh_pending = false;
        self.state.idle_stopping = false;
        self.state.pending_resume = false;
        self.resume_due = None;
        self.state.idle_stopped = false;
        self.state.launch_attempted = true;
        self.state.start_requested = true;
        self.do_spawn(events);
        // do_spawn emits failure event on error; accepted means queued.
        true
    }

    fn op_stop_idle(&mut self, events: &mut Vec<UiEvent>) -> bool {
        if self.state.busy
            || self.state.pending_approval.is_some()
            || self.state.control_pending
            || self.state.stopping
            || self.state.compacting
            || self.state.session_switching
            || self.state.session_refresh_pending
        {
            return false;
        }
        if self.state.pending_resume {
            self.state.desired_running = false;
            self.state.pending_resume = false;
            self.resume_due = None;
            self.state.idle_stopped = true;
            self.state.status = "Paused".to_string();
            return true;
        }
        if !self.child_live()
            && !self.state.process_started
            && !self.state.start_requested
            && self.state.retryable()
        {
            return false;
        }
        self.state.desired_running = false;
        self.state.idle_stopped = true;
        self.state.idle_stopping = true;
        self.state.status = "Stopping idle pi…".to_string();
        self.state.start_requested = false;
        self.state.state_ok = false;
        self.state.extension_ok = false;
        let cancels = self.state.reject_all_requests(events);
        if self.child_live() {
            self.write_pi(cancels);
            self.teardown_live_group();
        } else {
            self.state.process_started = false;
            self.state.idle_stopping = false;
            self.state.status = "Paused".to_string();
        }
        true
    }

    fn op_prompt(
        &mut self,
        message: &str,
        images: Option<Value>,
        events: &mut Vec<UiEvent>,
    ) -> bool {
        let msg = message.trim();
        if self.rejected_transport_dead(events) {
            return false;
        }
        if msg.is_empty()
            || !self.state.ready()
            || self.state.compacting
            || self.state.stopping
            || self.state.control_pending
            || self.state.session_switching
            || self.state.session_refresh_pending
        {
            events.push(UiEvent::new(
                "failed",
                vec![json!("Prompt rejected; agent not ready")],
            ));
            return false;
        }
        if !self.ensure_live() {
            events.push(UiEvent::new(
                "failed",
                vec![json!("Prompt rejected; pi not running")],
            ));
            return false;
        }
        let mut payload = json!({"message": msg});
        if let Some(im) = images {
            if let Some(arr) = im.as_array() {
                if !arr.is_empty() {
                    payload["images"] = im;
                }
            }
        }
        if self.state.busy {
            payload["streamingBehavior"] = json!("followUp");
        }
        self.state.answer.clear();
        self.state.busy = true;
        self.state.status = "Thinking…".to_string();
        let id = self.state.next_rpc_id();
        payload["type"] = json!("prompt");
        payload["id"] = json!(id.clone());
        self.state.request_kinds.insert(
            id.clone(),
            state::RequestKind {
                kind: "prompt".to_string(),
                message: Some(msg.to_string()),
                session_file: None,
                generation: None,
                refresh_generation: None,
            },
        );
        self.state.last_request_id = id;
        if !self.write_pi(vec![payload]) {
            self.state.busy = false;
            events.push(UiEvent::new(
                "failed",
                vec![json!("Prompt rejected; pi not running")],
            ));
            return false;
        }
        true
    }

    fn op_abort(&mut self, events: &mut Vec<UiEvent>) {
        let cancels = self.state.reject_all_requests(events);
        let was_switching = self.state.session_switching;
        self.state.session_switching = false;
        if self.child_live() && self.state.process_started {
            let mut to_write = cancels;
            // clear_queue + abort, tracked for correlation.
            let id1 = self.state.next_rpc_id();
            self.state
                .request_kinds
                .insert(id1.clone(), state::RequestKind::simple("clear_queue"));
            to_write.push(json!({"type": "clear_queue", "id": id1}));
            let id2 = self.state.next_rpc_id();
            self.state
                .request_kinds
                .insert(id2.clone(), state::RequestKind::simple("abort"));
            to_write.push(json!({"type": "abort", "id": id2}));
            self.state.stopping = true;
            self.write_pi(to_write);
        }
        self.state.generation += 1;
        // Stop during New/Restore never releases the gate: without a settled
        // command this only marks cancel-requested; the ACK + authoritative
        // state still own the release (all modes).
        let refresh_failed = self.state.session_refresh_pending;
        if refresh_failed {
            self.state.fail_session_refresh(
                "Session refresh cancelled; retry the session",
                false,
                events,
            );
        }
        if was_switching {
            self.state.busy = false;
        }
        if !refresh_failed {
            self.state.status = if was_switching {
                self.state.ready_status()
            } else {
                "Stopping…".to_string()
            };
        }
    }

    fn op_new_session(&mut self, events: &mut Vec<UiEvent>) -> bool {
        if self.rejected_transport_dead(events) {
            return false;
        }
        if self.state.busy
            || self.state.compacting
            || self.state.stopping
            || self.state.control_pending
            || self.state.session_switching
            || self.state.session_refresh_pending
            || !self.state.ready()
        {
            events.push(UiEvent::new(
                "failed",
                vec![json!("New session rejected; agent not ready")],
            ));
            return false;
        }
        if !self.ensure_live() {
            events.push(UiEvent::new(
                "failed",
                vec![json!("New session rejected; pi not running")],
            ));
            return false;
        }
        self.state.control_pending = true;
        // All modes clear the cached file on New so a restart can never
        // resume stale state before the replacement ACK + authoritative
        // get_state installs the new identity.
        self.state.fresh_session = true;
        self.state.session_file.clear();
        self.state.begin_session_refresh();
        let id = self.state.next_rpc_id();
        self.state
            .request_kinds
            .insert(id.clone(), state::RequestKind::simple("new_session"));
        self.state.begin_session_change("new", &id.clone());
        if !self.write_pi(vec![json!({"type": "new_session", "id": id})]) {
            events.push(UiEvent::new(
                "failed",
                vec![json!("New session rejected; pi not running")],
            ));
            return false;
        }
        true
    }

    fn op_switch_session(&mut self, events: &mut Vec<UiEvent>) -> bool {
        if self.rejected_transport_dead(events) {
            return false;
        }
        if self.state.busy
            || self.state.compacting
            || self.state.stopping
            || self.state.control_pending
            || self.state.session_switching
            || self.state.session_refresh_pending
            || !self.state.ready()
        {
            events.push(UiEvent::new(
                "failed",
                vec![json!("Session picker rejected; agent not ready")],
            ));
            return false;
        }
        if !self.ensure_live() {
            events.push(UiEvent::new(
                "failed",
                vec![json!("Session picker rejected; pi not running")],
            ));
            return false;
        }
        // Session switch parity: prompt /desktop-sessions, then gate.
        self.state.answer.clear();
        self.state.busy = true;
        self.state.status = "Thinking…".to_string();
        let picker_id = self.state.next_rpc_id();
        self.state.request_kinds.insert(
            picker_id.clone(),
            state::RequestKind {
                kind: "prompt".to_string(),
                message: Some("/desktop-sessions".to_string()),
                session_file: None,
                generation: None,
                refresh_generation: None,
            },
        );
        self.state.last_request_id = picker_id.clone();
        let payload = json!({"type": "prompt", "id": picker_id, "message": "/desktop-sessions"});
        if !self.write_pi(vec![payload]) {
            self.state.busy = false;
            events.push(UiEvent::new(
                "failed",
                vec![json!("Session picker rejected; pi not running")],
            ));
            return false;
        }
        self.state.begin_session_refresh();
        self.state
            .begin_session_change("restore", &self.state.last_request_id.clone());
        self.state.session_switching = true;
        self.state.busy = true;
        self.state.status = "Loading sessions…".to_string();
        true
    }

    fn op_rename(&mut self, name: &str, events: &mut Vec<UiEvent>) -> bool {
        if self.rejected_transport_dead(events) {
            return false;
        }
        let clean: String = name.trim().to_string();
        if clean.is_empty()
            || self.state.busy
            || self.state.compacting
            || self.state.stopping
            || self.state.control_pending
            || self.state.session_switching
            || self.state.session_refresh_pending
            || !self.state.ready()
        {
            events.push(UiEvent::new(
                "failed",
                vec![json!("Rename rejected; agent not ready")],
            ));
            return false;
        }
        if !self.ensure_live() {
            events.push(UiEvent::new(
                "failed",
                vec![json!("Rename rejected; pi not running")],
            ));
            return false;
        }
        // Bound name like the QML side (120 chars).
        let bounded: String = clean.chars().take(120).collect();
        self.state.control_pending = true;
        self.state.mark_session_refresh();
        let id = self.state.next_rpc_id();
        self.state
            .request_kinds
            .insert(id.clone(), state::RequestKind::simple("set_session_name"));
        self.state.begin_session_change("rename", &id.clone());
        if !self.write_pi(vec![
            json!({"type": "set_session_name", "id": id, "name": bounded}),
        ]) {
            events.push(UiEvent::new(
                "failed",
                vec![json!("Rename rejected; pi not running")],
            ));
            return false;
        }
        true
    }

    fn op_compact(&mut self, events: &mut Vec<UiEvent>) -> bool {
        if self.rejected_transport_dead(events) {
            return false;
        }
        if self.state.busy
            || self.state.compacting
            || self.state.stopping
            || self.state.control_pending
            || self.state.session_switching
            || self.state.session_refresh_pending
            || !self.state.ready()
        {
            events.push(UiEvent::new(
                "failed",
                vec![json!("Compact rejected; agent not ready")],
            ));
            return false;
        }
        if !self.ensure_live() {
            events.push(UiEvent::new(
                "failed",
                vec![json!("Compact rejected; pi not running")],
            ));
            return false;
        }
        self.state.control_pending = true;
        self.state.compacting = true;
        self.state.status = "Compacting…".to_string();
        let id = self.state.next_rpc_id();
        self.state
            .request_kinds
            .insert(id.clone(), state::RequestKind::simple("compact"));
        if !self.write_pi(vec![json!({"type": "compact", "id": id})]) {
            self.state.compacting = false;
            events.push(UiEvent::new(
                "failed",
                vec![json!("Compact rejected; pi not running")],
            ));
            return false;
        }
        true
    }

    fn op_choose_model(&mut self, item: &Value, events: &mut Vec<UiEvent>) -> bool {
        if self.rejected_transport_dead(events) {
            return false;
        }
        let provider = item.get("provider").and_then(|v| v.as_str()).unwrap_or("");
        let model_id = item
            .get("modelId")
            .and_then(|v| v.as_str())
            .or_else(|| item.get("id").and_then(|v| v.as_str()))
            .unwrap_or("");
        if provider.is_empty()
            || model_id.is_empty()
            || self.state.busy
            || self.state.compacting
            || self.state.stopping
            || self.state.control_pending
            || self.state.session_switching
            || self.state.session_refresh_pending
            || !self.state.ready()
        {
            events.push(UiEvent::new(
                "failed",
                vec![json!("Model change rejected; agent not ready")],
            ));
            return false;
        }
        if !self.ensure_live() {
            events.push(UiEvent::new(
                "failed",
                vec![json!("Model change rejected; pi not running")],
            ));
            return false;
        }
        self.state.control_pending = true;
        let id = self.state.next_rpc_id();
        self.state
            .request_kinds
            .insert(id.clone(), state::RequestKind::simple("set_model"));
        if !self.write_pi(vec![
            json!({"type": "set_model", "id": id, "provider": provider, "modelId": model_id}),
        ]) {
            events.push(UiEvent::new(
                "failed",
                vec![json!("Model change rejected; pi not running")],
            ));
            return false;
        }
        true
    }

    fn op_request_messages(&mut self, events: &mut Vec<UiEvent>) -> bool {
        // Refusals must be visible to the history UI, not just a generic
        // failure: correlate with the current identity when one exists so
        // Retry-history flows (which match on sessionFile+generation) work.
        // The ack still carries accepted:false.
        let readyish =
            self.state.state_ok && self.state.extension_ok && !self.state.session_refresh_pending;
        if self.transport_dead
            || TERMINATED.load(Ordering::SeqCst)
            || !self.ensure_live()
            || !readyish
        {
            let message = if self.transport_dead {
                "Session history unavailable; transport failed"
            } else if TERMINATED.load(Ordering::SeqCst) {
                "Session history unavailable; bridge is shutting down"
            } else if !self.ensure_live() {
                "Session history unavailable; pi is not running"
            } else {
                "Session history unavailable; session state is refreshing"
            };
            if !self.state.session_file.is_empty() {
                events.push(UiEvent::new(
                    "historyFailed",
                    vec![
                        json!(message),
                        json!(self.state.session_file.clone()),
                        json!(self.state.messages_generation),
                    ],
                ));
            } else {
                events.push(UiEvent::new("failed", vec![json!(message)]));
            }
            return false;
        }
        let id = self.state.next_rpc_id();
        self.state.request_kinds.insert(
            id.clone(),
            state::RequestKind {
                kind: "get_messages".to_string(),
                message: None,
                session_file: Some(self.state.session_file.clone()),
                generation: Some(self.state.messages_generation),
                refresh_generation: None,
            },
        );
        self.write_pi(vec![json!({"type": "get_messages", "id": id})])
    }

    /// Explicit read-only stats request. No args, no generic raw tunnel:
    /// the UI worker maps `request('get_session_stats')` to this op. Guarded
    /// on ready + live like history (tracked `get_session_stats`), rejected
    /// without launching or mutating state.
    fn op_request_stats(&mut self, events: &mut Vec<UiEvent>) -> bool {
        if self.transport_dead || TERMINATED.load(Ordering::SeqCst) {
            events.push(UiEvent::new(
                "failed",
                vec![json!("Session stats unavailable; bridge unavailable")],
            ));
            return false;
        }
        if !self.ensure_live() || !self.state.ready() {
            let message = if !self.ensure_live() {
                "Session stats unavailable; pi is not running"
            } else {
                "Session stats unavailable; agent not ready"
            };
            events.push(UiEvent::new("failed", vec![json!(message)]));
            return false;
        }
        let id = self.state.next_rpc_id();
        self.state
            .request_kinds
            .insert(id.clone(), state::RequestKind::simple("get_session_stats"));
        self.write_pi(vec![json!({"type": "get_session_stats", "id": id})])
    }

    fn op_respond(&mut self, request_id: &str, fields: &Value, events: &mut Vec<UiEvent>) -> bool {
        if self.rejected_transport_dead(events) {
            return false;
        }
        if request_id.is_empty() || !self.state.pending_requests.contains_key(request_id) {
            // Unknown/stale replies never release gates and never relaunch.
            return false;
        }
        let mut payload = match fields.as_object() {
            Some(o) => Value::Object(o.clone()),
            None => json!({}),
        };
        payload["type"] = json!("extension_ui_response");
        payload["id"] = json!(request_id);
        self.state.pending_requests.remove(request_id);
        self.state.refresh_pending_approval(events);
        if !self.ensure_live() {
            return false;
        }
        // Advance-before-write is handled by removal above; Pi may emit
        // another UI request while handling this write.
        self.write_pi(vec![payload])
    }

    /// UI opened a dialog for `request_id` (S-048): the request is marked
    /// surfaced and no longer expires. Local-only, no Pi write. Unknown or
    /// empty ids are rejected without side effects, like `respond`.
    fn op_surface_request(&mut self, request_id: &str, events: &mut Vec<UiEvent>) -> bool {
        if request_id.is_empty() {
            return false;
        }
        self.state.mark_surfaced(request_id, events)
    }

    /// UI deferred `request_id` (S-048): the request is parked round-robin
    /// and never expires. Local-only, no Pi write. Unknown or empty ids
    /// are rejected without side effects, like `respond`.
    fn op_defer_request(&mut self, request_id: &str, events: &mut Vec<UiEvent>) -> bool {
        if request_id.is_empty() {
            return false;
        }
        self.state.defer_request(request_id, events)
    }

    fn op_shutdown(&mut self) -> bool {
        // NOTE: intentionally does NOT set TERMINATED: the UI `shutdown`
        // command still owes its ack update, which is emitted after this
        // returns. The flag is set by the caller once the ack is out (and
        // EOF/disconnect paths break out of the loop directly).
        // Supervised, finite teardown of the whole group (leader-exited
        // groups included), then reap the direct child.
        if let Some(pgid) = self.supervised_pgid {
            let code = pi_child::teardown_group(
                pgid,
                self.pi.as_mut().map(|h| &mut h.child),
                GROUP_TERM_WAIT,
            );
            let _ = code;
            self.pi = None;
            if !pi_child::group_alive(pgid) {
                self.supervised_pgid = None;
            }
        } else if let Some(mut h) = self.pi.take() {
            let _ = h.child.try_wait();
        }
        true
    }
}

/// Best-effort orphan prevention on drop/panic unwind. No blocking waits
/// here (Drop must not wedge): signal the supervised group; the OS reaps
/// orphans via init. NOTE: pgid reuse across an arbitrary delay could in
/// theory address a recycled group; the main loop's confirmed-death clearing
/// keeps that window minimal, and process exit is the last resort anyway.
impl Drop for Bridge {
    fn drop(&mut self) {
        if let Some(pgid) = self.supervised_pgid {
            unsafe {
                libc::killpg(pgid, libc::SIGTERM);
                libc::killpg(pgid, libc::SIGKILL);
            }
        } else if let Some(h) = self.pi.as_mut() {
            unsafe {
                libc::killpg(h.pgid, libc::SIGTERM);
                libc::killpg(h.pgid, libc::SIGKILL);
            }
        }
    }
}

fn main() {
    install_signal_handlers();
    let argv: Vec<String> = std::env::args().collect();
    let cfg: Config = match cli::parse_args(&argv) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(2);
        }
    };
    // Palette carries no graph scope: empty project id/path, journalMode false,
    // so `scopedMode` stays false while session fencing still applies.
    // Project workers are pinned by UUID (QS_PROJECT_ID) with an optional
    // legacy page for explicit migration restores.
    let (project_id, project_path) = if cfg.mode == "project" {
        (
            cfg.project_id.clone().unwrap_or_default(),
            cfg.project.clone().unwrap_or_default(),
        )
    } else {
        (String::new(), String::new())
    };
    let journal_mode = cfg.mode == "journal";
    let session = cfg.session.clone().unwrap_or_default();
    let pending = cfg.pending_name.clone().unwrap_or_default();
    let fresh = cfg.new_session;
    let state = AgentState::new_with_id(project_id, project_path, journal_mode, session, pending, fresh);

    // UI stdout must be nonblocking so a stalled QML reader fails closed via
    // deadline instead of wedging the state loop.
    let stdout_fd: RawFd = std::io::stdout().as_raw_fd();
    if let Err(e) = transport::set_nonblocking(stdout_fd) {
        eprintln!("bridge stdout nonblocking failed: {e}");
        std::process::exit(2);
    }

    // Bounded queue: UI frames must be acked (sender waits finitely then
    // fails closed); Pi frames drop with accounting when full.
    let (tx, rx) = mpsc::sync_channel::<MainEvent>(MAIN_QUEUE_BOUND);
    {
        let txc = tx.clone();
        std::thread::spawn(move || stdin_thread(txc));
    }
    // Share sender with bridge for reader threads.
    let tx_shared = tx.clone();
    let dropped = std::sync::Arc::new(Dropped::default());
    let mut bridge = Bridge {
        cfg,
        state,
        pi: None,
        supervised_pgid: None,
        pi_generation: 0,
        idle_stop_deadline: None,
        resume_due: None,
        last_expiry: Instant::now(),
        last_drop_notice: Instant::now() - Duration::from_secs(60),
        tx: tx_shared,
        dropped: dropped.clone(),
        stdout_fd,
        transport_dead: false,
        transport_fail_at: None,
        exit_after_emit: false,
    };

    loop {
        // Termination (signal/EOF/backpressure) is serviced first, every
        // tick, before any other work.
        if TERMINATED.load(Ordering::SeqCst) {
            bridge.op_shutdown();
            break;
        }
        match rx.recv_timeout(LOOP_TICK) {
            Ok(MainEvent::Ui(Ok(input))) => {
                let (events, ack, accepted, shutdown) = bridge.process_ui(input);
                bridge.emit(events, ack, accepted);
                if shutdown {
                    TERMINATED.store(true, Ordering::SeqCst);
                    break;
                }
            }
            Ok(MainEvent::Ui(Err(e))) => {
                let events = vec![UiEvent::new("failed", vec![json!(e.message)])];
                bridge.emit(events, e.id, Some(false));
            }
            Ok(MainEvent::PiLine { generation, line }) => {
                if generation != bridge.pi_generation {
                    continue;
                }
                match line {
                    Ok(v) => {
                        let mut events = Vec::new();
                        let writes = bridge.state.handle_pi_value(&v, &mut events);
                        // Filter null placeholders from refresh_after.
                        let writes: Vec<Value> =
                            writes.into_iter().filter(|w| !w.is_null()).collect();
                        if !writes.is_empty() {
                            bridge.write_pi(writes);
                        }
                        bridge.emit(events, None, None);
                    }
                    Err(msg) => {
                        // Malformed/oversize Pi output terminates the session
                        // fail-closed: a bare `failed` against a live-looking
                        // session would leave prompt/control gates stuck, and
                        // the framing position is no longer trustworthy, so
                        // the session ends with no replay instead.
                        let mut events = Vec::new();
                        bridge.fail_pi_output(&msg, &mut events);
                        bridge.emit(events, None, None);
                    }
                }
            }
            Ok(MainEvent::PiDiag { generation, line }) => {
                if generation != bridge.pi_generation {
                    continue;
                }
                bridge.state.push_diagnostic(&line);
                bridge.emit(Vec::new(), None, None);
            }
            Ok(MainEvent::StdinEof) => {
                bridge.op_shutdown();
                break;
            }
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => {
                bridge.op_shutdown();
                break;
            }
        }
        if TERMINATED.load(Ordering::SeqCst) {
            bridge.op_shutdown();
            break;
        }
        // A latched transport failure exits right after its failure update
        // was emitted above, so QML takes the dead/retry path instead of
        // hanging against a half-dead session.
        if bridge.take_exit() {
            break;
        }
        // Approval expiry is serviced every tick window regardless of load.
        if bridge.last_expiry.elapsed() >= Duration::from_secs(1) {
            bridge.last_expiry = Instant::now();
            let mut events = Vec::new();
            let lines = bridge.state.expire_approvals(&mut events);
            if !lines.is_empty() {
                bridge.write_pi(lines);
                bridge.emit(events, None, None);
            }
        }
        // Surface shed stderr diagnostics (throttled). Protocol frames are
        // never shed; only diagnostics may be, and the drop is reported
        // instead of hanging the loop.
        let dropped_diags = bridge.dropped.diags.swap(0, Ordering::SeqCst);
        if dropped_diags > 0 && bridge.last_drop_notice.elapsed() >= Duration::from_secs(5) {
            bridge.last_drop_notice = Instant::now();
            bridge.emit(
                vec![UiEvent::new(
                    "failed",
                    vec![json!(format!(
                        "Pi stderr backpressure; shed {dropped_diags} diagnostic(s) to stay bounded"
                    ))],
                )],
                None,
                None,
            );
        }
        // Group supervision every tick: escalate TERM → KILL while the group
        // survives, clear once it is gone (leader-exited groups included).
        bridge.poll_group();
        // Child exit polling (non-blocking reap).
        if bridge.pi.is_some() {
            let exited = match bridge.pi.as_mut().unwrap().child.try_wait() {
                Ok(Some(status)) => Some(status.code().unwrap_or(1)),
                Ok(None) => None,
                Err(_) => Some(1),
            };
            if let Some(code) = exited {
                if let Some(mut h) = bridge.pi.take() {
                    let _ = h.child.wait();
                }
                let mut events = Vec::new();
                bridge.handle_exit(code, &mut events);
                bridge.emit(events, None, None);
            }
        }
        // Deferred resume after idle stop settles (cancellable window).
        if bridge.state.pending_resume {
            let due = bridge.resume_due.unwrap_or_else(Instant::now);
            if Instant::now() >= due {
                if bridge.state.pending_resume
                    && bridge.state.desired_running
                    && bridge.state.idle_stopped
                    && bridge.pi.is_none()
                {
                    bridge.state.pending_resume = false;
                    bridge.resume_due = None;
                    bridge.state.idle_stopped = false;
                    if bridge.state.desired_running
                        && !bridge.child_live()
                        && !bridge.state.process_started
                    {
                        let mut events = Vec::new();
                        bridge.state.launch_attempted = true;
                        bridge.state.start_requested = true;
                        bridge.do_spawn(&mut events);
                        bridge.emit(events, None, None);
                    }
                } else {
                    bridge.resume_due = None;
                    if !bridge.state.desired_running {
                        bridge.state.pending_resume = false;
                    }
                }
            }
        }
    }
    // Bridge Drop performs best-effort group signaling on unwind/exit paths.
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Serializes tests touching the shared `TERMINATED` flag or `Bridge`
    /// (which observes it): cargo runs tests in parallel by default, so one
    /// test resetting the flag could clear another's saturation latch.
    static TEST_GUARD: std::sync::Mutex<()> = std::sync::Mutex::new(());

    fn test_bridge() -> Bridge {
        let (tx, _rx) = mpsc::sync_channel::<MainEvent>(8);
        Bridge {
            cfg: Config {
                root: std::path::PathBuf::from("/tmp"),
                mode: "journal".to_string(),
                project: None,
                project_id: None,
                session: None,
                pending_name: None,
                new_session: false,
            },
            state: AgentState::new(
                String::new(),
                true,
                "/j/current.jsonl".into(),
                String::new(),
                false,
            ),
            pi: None,
            supervised_pgid: None,
            pi_generation: 0,
            idle_stop_deadline: None,
            resume_due: None,
            last_expiry: Instant::now(),
            last_drop_notice: Instant::now(),
            tx,
            dropped: std::sync::Arc::new(Dropped::default()),
            // Refusal paths never write; invalid fd is never touched.
            stdout_fd: -1,
            transport_dead: false,
            transport_fail_at: None,
            exit_after_emit: false,
        }
    }

    #[test]
    fn request_messages_refused_when_not_live_emits_correlated_history_failed() {
        let _test_guard = TEST_GUARD
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        TERMINATED.store(false, Ordering::SeqCst);
        let mut bridge = test_bridge();
        let mut events = Vec::new();
        let accepted = bridge.op_request_messages(&mut events);
        assert!(!accepted, "must reject with accepted:false, no Pi writes");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].name, "historyFailed");
        assert_eq!(events[0].args[1], json!("/j/current.jsonl"));
        assert_eq!(
            events[0].args[2],
            json!(bridge.state.messages_generation),
            "generation must correlate so the UI matches current identity"
        );
        assert!(bridge.pi.is_none(), "refusal must never launch a child");
    }

    #[test]
    fn request_messages_refused_during_refresh_stays_correlated() {
        let _test_guard = TEST_GUARD
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        TERMINATED.store(false, Ordering::SeqCst);
        let mut bridge = test_bridge();
        bridge.state.process_started = true;
        bridge.state.state_ok = true;
        bridge.state.extension_ok = true;
        bridge.state.session_refresh_pending = true;
        let mut events = Vec::new();
        let accepted = bridge.op_request_messages(&mut events);
        assert!(!accepted);
        assert_eq!(events[0].name, "historyFailed");
        assert_eq!(events[0].args[1], json!("/j/current.jsonl"));
    }

    #[test]
    fn request_messages_refused_without_identity_falls_back_to_failed() {
        let _test_guard = TEST_GUARD
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        TERMINATED.store(false, Ordering::SeqCst);
        let mut bridge = test_bridge();
        bridge.state.session_file.clear();
        let mut events = Vec::new();
        let accepted = bridge.op_request_messages(&mut events);
        assert!(!accepted);
        assert_eq!(events[0].name, "failed");
        TERMINATED.store(false, Ordering::SeqCst);
    }

    #[test]
    fn saturated_pi_queue_latches_terminal_failure_instead_of_dropping() {
        let _test_guard = TEST_GUARD
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        TERMINATED.store(false, Ordering::SeqCst);
        // Fake saturation: bound-1 queue prefilled, nothing draining.
        let (tx, _rx) = mpsc::sync_channel::<MainEvent>(1);
        tx.try_send(MainEvent::StdinEof).expect("prefill");
        let frame = MainEvent::PiLine {
            generation: 7,
            line: Ok(json!({"type": "agent_settled"})),
        };
        let ok = send_pi_frame_timeout(&tx, frame, Duration::from_millis(100));
        assert!(!ok, "saturated protocol queue must fail, never drop");
        assert!(
            TERMINATED.load(Ordering::SeqCst),
            "saturation must latch terminal failure so gates surface it"
        );
        TERMINATED.store(false, Ordering::SeqCst);
    }

    #[test]
    fn saturated_control_queue_fails_closed() {
        let _test_guard = TEST_GUARD
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        TERMINATED.store(false, Ordering::SeqCst);
        let (tx, _rx) = mpsc::sync_channel::<MainEvent>(1);
        tx.try_send(MainEvent::StdinEof).expect("prefill");
        let ok = send_finite(&tx, MainEvent::StdinEof, Duration::from_millis(50));
        assert!(!ok, "saturated control queue must fail closed");
        assert!(TERMINATED.load(Ordering::SeqCst));
        TERMINATED.store(false, Ordering::SeqCst);
    }

    #[test]
    fn first_transport_failure_latches_deadline_and_blocks_further_writes() {
        let _test_guard = TEST_GUARD
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        TERMINATED.store(false, Ordering::SeqCst);
        let mut bridge = test_bridge();
        bridge.note_transport_failure();
        let anchor = bridge.transport_fail_at.expect("anchor recorded");
        assert!(bridge.transport_dead);
        assert!(bridge.exit_after_emit);
        assert!(!bridge.state.ready(), "ready must read closed after latch");
        // A second failure must not move the immutable escalation anchor.
        std::thread::sleep(Duration::from_millis(5));
        bridge.note_transport_failure();
        assert_eq!(
            bridge.transport_fail_at,
            Some(anchor),
            "escalation deadline must never be extended"
        );
        // No further writes: every control fails explicitly without touching
        // the (possibly partial) stdin frame.
        assert!(!bridge.write_pi(vec![json!({"type": "get_state"})]));
        let mut ev2 = Vec::new();
        assert!(!bridge.op_prompt("hello", None, &mut ev2));
        assert_eq!(ev2.last().expect("explicit failure").name, "failed");
        let mut ev3 = Vec::new();
        assert!(!bridge.op_request_messages(&mut ev3));
        assert_eq!(ev3[0].name, "historyFailed");
        assert_eq!(ev3[0].args[1], json!("/j/current.jsonl"));
        assert!(!bridge.op_start(&mut Vec::new()));
        TERMINATED.store(false, Ordering::SeqCst);
    }

    #[test]
    fn oversize_outbound_line_fails_closed_without_silent_skip() {
        let _test_guard = TEST_GUARD
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        TERMINATED.store(false, Ordering::SeqCst);
        // Fake Pi stdin: a real `sleep` child with piped stdin, no Pi,
        // no model. Over 4 MiB serializes past the framing bound. Own
        // process group so teardown signals can never hit the test runner.
        use std::os::unix::process::CommandExt;
        use std::process::{Command, Stdio};
        let mut cmd = Command::new("sleep");
        cmd.arg("30")
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        unsafe {
            cmd.pre_exec(|| {
                libc::setpgid(0, 0);
                Ok(())
            });
        }
        let mut child = cmd.spawn().expect("fake sleeper");
        let stdin = child.stdin.take().expect("piped stdin");
        // Match production: writers require a nonblocking peer.
        transport::set_nonblocking(stdin.as_raw_fd()).expect("nonblocking");
        let pid = child.id();
        let mut bridge = test_bridge();
        bridge.pi = Some(pi_child::PiHandles {
            child,
            stdin,
            pid,
            pgid: pid as i32,
        });
        let huge = "x".repeat(4 * 1024 * 1024 + 16);
        assert!(!bridge.write_pi(vec![json!({"type": "prompt", "blob": huge})]));
        assert!(bridge.transport_dead, "oversize outbound must latch");
        assert!(bridge.exit_after_emit);
        // Latched: further attempts cannot write or extend cleanup.
        let anchor = bridge.transport_fail_at;
        assert!(!bridge.write_pi(vec![json!({"type": "get_state"})]));
        assert_eq!(bridge.transport_fail_at, anchor);
        // Cleanup the fake child without orphans.
        if let Some(handles) = bridge.pi.as_mut() {
            pi_child::teardown_group(
                handles.pgid,
                Some(&mut handles.child),
                Duration::from_millis(300),
            );
        }
        TERMINATED.store(false, Ordering::SeqCst);
    }

    fn palette_bridge() -> Bridge {
        let (tx, _rx) = mpsc::sync_channel::<MainEvent>(8);
        Bridge {
            cfg: Config {
                root: std::path::PathBuf::from("/tmp"),
                mode: "palette".to_string(),
                project: None,
                project_id: None,
                session: None,
                pending_name: None,
                new_session: false,
            },
            state: AgentState::new(
                String::new(),
                false,
                "/pal/a.jsonl".into(),
                "A".into(),
                false,
            ),
            pi: None,
            supervised_pgid: None,
            pi_generation: 0,
            idle_stop_deadline: None,
            resume_due: None,
            last_expiry: Instant::now(),
            last_drop_notice: Instant::now(),
            tx,
            dropped: std::sync::Arc::new(Dropped::default()),
            stdout_fd: -1,
            transport_dead: false,
            transport_fail_at: None,
            exit_after_emit: false,
        }
    }

    fn attach_fake_child(bridge: &mut Bridge) {
        // Fake live Pi: `sleep` with piped stdin, no Pi/model. Own group.
        use std::os::unix::process::CommandExt;
        use std::process::{Command, Stdio};
        let mut cmd = Command::new("sleep");
        cmd.arg("30")
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        unsafe {
            cmd.pre_exec(|| {
                libc::setpgid(0, 0);
                Ok(())
            });
        }
        let mut child = cmd.spawn().expect("fake sleeper");
        let stdin = child.stdin.take().expect("piped stdin");
        transport::set_nonblocking(stdin.as_raw_fd()).expect("nonblocking");
        let pid = child.id();
        bridge.pi = Some(pi_child::PiHandles {
            child,
            stdin,
            pid,
            pgid: pid as i32,
        });
        bridge.state.process_started = true;
        bridge.state.desired_running = true;
        bridge.state.state_ok = true;
        bridge.state.extension_ok = true;
        bridge.state.status = "Ready".to_string();
    }

    fn detach_fake_child(bridge: &mut Bridge) {
        if let Some(handles) = bridge.pi.as_mut() {
            pi_child::teardown_group(
                handles.pgid,
                Some(&mut handles.child),
                Duration::from_millis(300),
            );
        }
        bridge.pi = None;
        TERMINATED.store(false, Ordering::SeqCst);
    }

    #[test]
    fn palette_request_stats_needs_ready_live_and_tracks_read_only() {
        let _test_guard = TEST_GUARD
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        TERMINATED.store(false, Ordering::SeqCst);
        let mut bridge = palette_bridge();
        // Not live: rejected, never launches, never writes.
        let mut events = Vec::new();
        assert!(!bridge.op_request_stats(&mut events));
        assert_eq!(events.last().expect("failed").name, "failed");
        assert!(bridge.pi.is_none(), "refusal must never launch");
        // Live + ready: accepted, tracked as read-only get_session_stats.
        attach_fake_child(&mut bridge);
        let mut events2 = Vec::new();
        assert!(bridge.op_request_stats(&mut events2));
        let kinds: Vec<String> = bridge
            .state
            .request_kinds
            .values()
            .map(|r| r.kind.clone())
            .collect();
        assert!(kinds.contains(&"get_session_stats".to_string()));
        // No gates touched: read-only request never sets control/busy.
        assert!(!bridge.state.control_pending);
        assert!(!bridge.state.busy);
        assert!(!bridge.state.session_refresh_pending);
        detach_fake_child(&mut bridge);
    }

    #[test]
    fn palette_request_stats_rejected_while_refreshing() {
        let _test_guard = TEST_GUARD
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        TERMINATED.store(false, Ordering::SeqCst);
        let mut bridge = palette_bridge();
        attach_fake_child(&mut bridge);
        bridge.state.session_refresh_pending = true;
        let mut events = Vec::new();
        assert!(!bridge.op_request_stats(&mut events));
        assert_eq!(events.last().expect("failed").name, "failed");
        detach_fake_child(&mut bridge);
    }

    #[test]
    fn palette_new_clears_file_and_gates_until_authoritative() {
        let _test_guard = TEST_GUARD
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        TERMINATED.store(false, Ordering::SeqCst);
        let mut bridge = palette_bridge();
        attach_fake_child(&mut bridge);
        let mut events = Vec::new();
        assert!(bridge.op_new_session(&mut events));
        assert!(
            bridge.state.session_file.is_empty(),
            "New clears cached file"
        );
        assert!(bridge.state.fresh_session);
        assert!(bridge.state.session_refresh_pending);
        assert!(bridge.state.session_change_in_flight);
        assert_eq!(bridge.state.session_change_kind, "new");
        assert!(!bridge.state.ready(), "gate holds before ACK");
        detach_fake_child(&mut bridge);
    }

    #[test]
    fn palette_abort_before_ack_retains_refresh_gate() {
        let _test_guard = TEST_GUARD
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        TERMINATED.store(false, Ordering::SeqCst);
        let mut bridge = palette_bridge();
        attach_fake_child(&mut bridge);
        let mut events = Vec::new();
        assert!(bridge.op_new_session(&mut events));
        let mut abort_ev = Vec::new();
        bridge.op_abort(&mut abort_ev);
        assert!(bridge.state.session_refresh_pending, "abort retains gate");
        assert!(bridge.state.session_change_in_flight);
        assert!(bridge.state.session_change_cancel_requested);
        detach_fake_child(&mut bridge);
    }

    #[test]
    fn no_generic_raw_request_tunnel_close_is_unknown() {
        let _test_guard = TEST_GUARD
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        TERMINATED.store(false, Ordering::SeqCst);
        let mut bridge = palette_bridge();
        attach_fake_child(&mut bridge);
        // A generic raw tunnel must not exist: unknown ops reject.
        let (events, ack, accepted, shutdown) = bridge.process_ui(UiInput {
            version: Some(1),
            id: Some("ui-1".to_string()),
            op: "request".to_string(),
            args: Some(json!({"type": "get_session_stats"})),
        });
        assert_eq!(ack.as_deref(), Some("ui-1"));
        assert_eq!(accepted, Some(false));
        assert!(!shutdown);
        assert!(events.iter().any(|e| e.name == "failed"));
        // Palette `close` is UI-owned: no bridge stop side effect.
        let (events2, ack2, accepted2, _) = bridge.process_ui(UiInput {
            version: Some(1),
            id: Some("ui-2".to_string()),
            op: "close".to_string(),
            args: Some(json!({})),
        });
        assert_eq!(ack2.as_deref(), Some("ui-2"));
        assert_eq!(accepted2, Some(false));
        assert!(events2.iter().any(|e| e.name == "failed"));
        assert!(bridge.pi.is_some(), "close must not stop the child");
        // Explicit requestStats maps correctly.
        let (events3, ack3, accepted3, _) = bridge.process_ui(UiInput {
            version: Some(1),
            id: Some("ui-3".to_string()),
            op: "requestStats".to_string(),
            args: Some(json!({})),
        });
        assert_eq!(ack3.as_deref(), Some("ui-3"));
        assert_eq!(accepted3, Some(true), "events: {:?}", events3);
        detach_fake_child(&mut bridge);
    }

    #[test]
    fn palette_exit_without_refresh_preserves_file_no_replay() {
        let _test_guard = TEST_GUARD
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        TERMINATED.store(false, Ordering::SeqCst);
        let mut bridge = palette_bridge();
        bridge.state.process_started = true;
        bridge.state.desired_running = true;
        let mut events = Vec::new();
        bridge.handle_exit(1, &mut events);
        // No refresh was pending: cached identity survives for resume.
        assert_eq!(bridge.state.session_file, "/pal/a.jsonl");
        assert!(!bridge.state.session_refresh_failed);
        assert!(
            events.iter().any(|e| e.name == "failed"),
            "non-zero exit surfaces"
        );
        assert!(bridge.pi.is_none(), "handle dropped, no replay queued");
    }
}
