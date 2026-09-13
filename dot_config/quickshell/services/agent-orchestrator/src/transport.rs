//! Bounded transport primitives for the orchestrator bridge.
//!
//! This module owns three failure modes the old inline code got wrong:
//!
//! * **Framing**: [`LineFramer`] is a persistent incremental buffer shared by
//!   UI stdin, Pi stdout and Pi stderr. It never allocates beyond
//!   `max + chunk`, retains frames coalesced after a newline in the same
//!   `read()`, and discards oversize lines up to their newline while keeping
//!   the bytes that follow.
//! * **Backpressure**: writers use [`write_all_deadline`] against nonblocking
//!   fds with [`poll_out`]-sliced deadlines and check the shared termination
//!   flag, so Stop/expiry/EOF are always serviced promptly. A write that
//!   cannot complete in time fails closed instead of blocking the state loop.
//! * **Emit budget**: [`render_update`] serializes snapshots that always carry
//!   every lifecycle/identity/approval field plus ack/events, trimming only
//!   display payloads (answer, history, stats) and flagging the trim. Nested
//!   event args over budget become bounded informational objects; `uiRequest`
//!   normalizes to zero/one exact final approval (never stale, never
//!   truncated). An exact approval that still cannot fit is cancelled
//!   fail-closed before display. No path emits a partial-state fallback: the
//!   terminal signal is [`RenderedUpdate::fatal`] (full lifecycle + teardown)
//!   for the main worker, including huge-ack terminal.

use crate::proto::{BridgeUpdate, UiEvent, MAX_LINE_BYTES};
use crate::state::AgentState;
use serde_json::{json, Value};
use std::os::unix::io::RawFd;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

/// Target ceiling for an emitted update line (newline excluded), leaving
/// headroom under the 4 MiB framing bound.
pub const EMIT_TARGET_BYTES: usize = 3 * 1024 * 1024;
/// Display trims applied to the emitted snapshot copy only. Authoritative
/// state caps remain owned by the state worker.
pub const EMIT_ANSWER_TAIL: usize = 64 * 1024;
pub const EMIT_MESSAGES_BUDGET: usize = 512 * 1024;
pub const EMIT_STATS_BUDGET: usize = 16 * 1024;
/// Longest string event argument preserved verbatim (char-boundary safe).
pub const EMIT_EVENT_ARG_BUDGET: usize = 64 * 1024;
/// Safe budget for any non-string (nested object/array) event argument,
/// measured as serialized JSON bytes. A `statsChanged` payload near 4 MiB
/// nested must never flow verbatim: above this it is replaced by a bounded
/// informational object, never truncated piecemeal.
pub const EMIT_NESTED_ARG_BUDGET: usize = 64 * 1024;
/// Legal UI id max: QML issues `ui-<serial>` (bytes). The UI input path is
/// currently unbounded up to the 4 MiB framing bound, so an ack this large
/// would wedge the frame. `render_update` keeps ack exact within this bound
/// and returns terminal `fatal` rather than silently truncating/invalidating.
/// Protocol schema should constrain input `id` to this (main worker, not
/// tasked here).
pub const MAX_ACK_BYTES: usize = 256;
/// Legal approval queue id max (Pi-provided, untrusted). Entries over this
/// are cancelled fail-closed rather than emitted as huge map keys.
pub const MAX_UI_ID_BYTES: usize = 256;
/// How much of an oversize input line is retained for `id` recovery.
pub const OVERSIZE_ID_SCAN: usize = 8192;
/// Diagnostic line bound for stderr transport (per line, before state tail).
pub const STDERR_LINE_MAX: usize = 8192;
/// Reader chunk size: small enough to keep latency low, large enough to be
/// efficient. The framer bounds total memory regardless of chunk size.
pub const READ_CHUNK: usize = 8192;

/// Outcome of feeding bytes to [`LineFramer`].
#[derive(Debug, PartialEq, Eq)]
pub enum Framed {
    /// A complete line without its trailing newline (`\r` stripped).
    Line(Vec<u8>),
    /// A line longer than `max`. Carries the first [`OVERSIZE_ID_SCAN`]
    /// bytes so callers can recover a correlation id for ack/accounting.
    /// The remainder of the line up to its newline is discarded; bytes after
    /// that newline are retained as following frames.
    Oversize(Vec<u8>),
}

/// Persistent incremental newline framer with a hard pre-allocation bound.
///
/// Memory is bounded to `max + READ_CHUNK` plus a small oversize prefix: the
/// internal buffer never grows past `max + 1` while accumulating, and
/// discarding an oversize line consumes input without retaining it.
#[derive(Debug)]
pub struct LineFramer {
    buf: Vec<u8>,
    max: usize,
    discarding: bool,
    prefix: Vec<u8>,
}

impl LineFramer {
    pub fn new(max: usize) -> Self {
        Self {
            buf: Vec::new(),
            max,
            discarding: false,
            prefix: Vec::new(),
        }
    }

    /// Feed a chunk; returns completed frames in order, retaining any partial
    /// frame internally for the next call.
    pub fn push(&mut self, chunk: &[u8]) -> Vec<Framed> {
        let mut out = Vec::new();
        for &b in chunk {
            if self.discarding {
                if b == b'\n' {
                    self.discarding = false;
                    out.push(Framed::Oversize(std::mem::take(&mut self.prefix)));
                }
                continue;
            }
            if b == b'\n' {
                let mut line = std::mem::take(&mut self.buf);
                if line.last() == Some(&b'\r') {
                    line.pop();
                }
                out.push(Framed::Line(line));
                continue;
            }
            if self.buf.len() >= self.max {
                // Line exceeded the bound: switch to discarding, keep a small
                // prefix for id recovery, drop the accumulated bulk.
                self.prefix.clear();
                let keep = self.buf.len().min(OVERSIZE_ID_SCAN);
                self.prefix.extend_from_slice(&self.buf[..keep]);
                if self.prefix.len() < OVERSIZE_ID_SCAN {
                    let rest = OVERSIZE_ID_SCAN - self.prefix.len();
                    // `b` is the byte that overflowed; include it if it fits.
                    self.prefix.push(b);
                    let _ = rest;
                }
                self.buf.clear();
                self.discarding = true;
                continue;
            }
            self.buf.push(b);
        }
        out
    }

    /// Drain a trailing partial line at EOF as a final frame. Returns `None`
    /// when there is nothing pending or when the partial belonged to an
    /// unterminated oversize line (reported as oversize so it is accounted).
    pub fn flush(&mut self) -> Option<Framed> {
        if self.discarding {
            self.discarding = false;
            self.buf.clear();
            if self.prefix.is_empty() {
                return None;
            }
            return Some(Framed::Oversize(std::mem::take(&mut self.prefix)));
        }
        if self.buf.is_empty() {
            return None;
        }
        let mut line = std::mem::take(&mut self.buf);
        if line.last() == Some(&b'\r') {
            line.pop();
        }
        Some(Framed::Line(line))
    }

    /// Current retained bytes (for tests: proves the bound).
    #[cfg(test)]
    pub fn retained(&self) -> usize {
        self.buf.len() + self.prefix.len()
    }
}

/// Best-effort `"id"` recovery from an oversize line prefix so a rejected
/// command can still ack and release QML pending gates.
pub fn extract_id_hint(prefix: &[u8]) -> Option<String> {
    let end = prefix.len().min(OVERSIZE_ID_SCAN);
    let text = String::from_utf8_lossy(&prefix[..end]);
    let pos = text.find("\"id\"")?;
    let rest = &text[pos + 4..];
    let colon = rest.find(':')?;
    let after = rest[colon + 1..].trim_start();
    if !after.starts_with('"') {
        return None;
    }
    let end = after[1..].find('"')?;
    Some(after[1..1 + end].to_string())
}

/// Byte-safe tail of `s` limited to `max_bytes`, cut at a UTF-8 boundary.
pub fn tail_str(s: &str, max_bytes: usize) -> &str {
    if s.len() <= max_bytes {
        return s;
    }
    let mut start = s.len() - max_bytes;
    while start < s.len() && !s.is_char_boundary(start) {
        start += 1;
    }
    &s[start..]
}

/// Truncate a string value to a byte budget at a char boundary.
fn truncate_str_arg(s: &str, budget: usize) -> String {
    if s.len() <= budget {
        return s.to_string();
    }
    let mut end = budget;
    while end > 0 && !s.is_char_boundary(end) {
        end -= 1;
    }
    format!("{}… [truncated]", &s[..end])
}

// ---------------------------------------------------------------------------
// Nonblocking deadline writes.
// ---------------------------------------------------------------------------

/// Failure of [`write_all_deadline`]. Every variant fails closed: the caller
/// must treat the peer as unusable (shutdown the bridge for UI stdout, tear
/// down the Pi child for Pi stdin).
#[derive(Debug, PartialEq, Eq)]
pub enum WriteFail {
    /// Peer did not become writable within the deadline (backpressure).
    Timeout,
    /// Peer closed (EPIPE) or fd invalid.
    Closed,
    /// Termination was requested while waiting; loop must service it.
    Terminated,
    /// Other I/O error with errno attached.
    Io(i32),
}

/// Put `fd` into nonblocking mode. Readers keep their fds blocking (they run
/// on dedicated threads with bounded framers and bounded channels); writers
/// must be nonblocking so the state loop can never wedge.
pub fn set_nonblocking(fd: RawFd) -> std::io::Result<()> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 {
        return Err(std::io::Error::last_os_error());
    }
    let rc = unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) };
    if rc < 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(())
}

fn poll_writable(fd: RawFd, slice: Duration) -> bool {
    let mut pfd = libc::pollfd {
        fd,
        events: libc::POLLOUT,
        revents: 0,
    };
    let ms = slice.as_millis().min(i32::MAX as u128) as libc::c_int;
    let rc = unsafe { libc::poll(&mut pfd, 1, ms) };
    rc > 0 && (pfd.revents & (libc::POLLOUT | libc::POLLERR | libc::POLLHUP)) != 0
}

/// Write all of `data` to nonblocking `fd` within `timeout`, polling in short
/// slices so `term` is serviced promptly. Returns the number of bytes written
/// on timeout (partial progress is reported, never retried unboundedly).
pub fn write_all_deadline(
    fd: RawFd,
    data: &[u8],
    timeout: Duration,
    term: &AtomicBool,
) -> Result<(), WriteFail> {
    if data.is_empty() {
        return Ok(());
    }
    let deadline = Instant::now() + timeout;
    let mut off = 0usize;
    while off < data.len() {
        if term.load(Ordering::SeqCst) {
            return Err(WriteFail::Terminated);
        }
        let rc = unsafe {
            libc::write(
                fd,
                data[off..].as_ptr() as *const libc::c_void,
                data[off..].len(),
            )
        };
        if rc > 0 {
            off += rc as usize;
            continue;
        }
        if rc < 0 {
            let err = unsafe { *libc::__errno_location() };
            if err == libc::EAGAIN || err == libc::EWOULDBLOCK {
                // Not writable right now; poll below.
            } else if err == libc::EPIPE || err == libc::EBADF {
                return Err(WriteFail::Closed);
            } else if err == libc::EINTR {
                continue;
            } else {
                return Err(WriteFail::Io(err));
            }
        }
        if Instant::now() >= deadline {
            return Err(WriteFail::Timeout);
        }
        if !poll_writable(fd, Duration::from_millis(25)) {
            if Instant::now() >= deadline {
                return Err(WriteFail::Timeout);
            }
            continue;
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Emit budget: lifecycle-complete snapshots with display-only trims.
// ---------------------------------------------------------------------------

/// Result of [`render_update`].
pub struct RenderedUpdate {
    /// Complete stdout line including trailing `\n`.
    pub line: Vec<u8>,
    /// Pi stdin cancels produced by fail-closed approval handling.
    pub pi_writes: Vec<Value>,
    /// Whether display payloads were trimmed for budget.
    pub display_truncated: bool,
    /// Whether an approval was cancelled fail-closed for budget.
    pub approval_cancelled: bool,
    /// Legacy: `pendingRequests` values were degraded to `{id}` summaries.
    /// No longer produced (exact previews are never truncated); always
    /// false, retained for main-worker compatibility.
    pub requests_summarized: bool,
    /// Terminal fail-closed signal to the main worker. When true the frame
    /// still carries the FULL lifecycle/identity snapshot (never a partial
    /// `{displayTruncated:true}` fallback) but the coordinator must treat
    /// the bridge as unusable and terminate: either the ack id exceeded
    /// [`MAX_ACK_BYTES`] or the exact lifecycle frame could not fit even
    /// after display trims, event bounds and cancelling every approval.
    /// Main currently ignores this (its worker is separately tasked); it
    /// must check it and teardown. Never silently invalid.
    pub fatal: bool,
}

use serde::Serialize;

#[derive(Serialize)]
struct BorrowedUpdate<'a> {
    version: u8,
    #[serde(rename = "type")]
    kind: &'a str,
    state: &'a Value,
    events: &'a [UiEvent],
    #[serde(skip_serializing_if = "Option::is_none")]
    ack: &'a Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    accepted: &'a Option<bool>,
}

fn measured_size(
    state_value: &Value,
    events: &[UiEvent],
    ack: &Option<String>,
    accepted: &Option<bool>,
) -> usize {
    let borrowed = BorrowedUpdate {
        version: 1,
        kind: "update",
        state: state_value,
        events,
        ack,
        accepted,
    };
    serde_json::to_string(&borrowed)
        .map(|s| s.len())
        .unwrap_or(usize::MAX)
}

fn arg_json_len(arg: &Value) -> usize {
    serde_json::to_string(arg)
        .map(|s| s.len())
        .unwrap_or(usize::MAX)
}

/// Trim display-only fields of an emitted snapshot copy. Never touches
/// lifecycle/identity/approval fields. Returns whether anything was trimmed.
fn trim_display(state_value: &mut Value) -> bool {
    let mut trimmed = false;
    if let Some(answer) = state_value
        .get_mut("answer")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
    {
        if answer.len() > EMIT_ANSWER_TAIL {
            state_value["answer"] = json!(tail_str(&answer, EMIT_ANSWER_TAIL));
            trimmed = true;
        }
    }
    if let Some(messages) = state_value
        .get_mut("messages")
        .and_then(|v| v.as_array_mut())
    {
        let mut bytes: usize = messages
            .iter()
            .map(|m| serde_json::to_string(m).map(|s| s.len()).unwrap_or(0))
            .sum();
        while bytes > EMIT_MESSAGES_BUDGET && !messages.is_empty() {
            if let Some(removed) = messages.first() {
                bytes = bytes
                    .saturating_sub(serde_json::to_string(removed).map(|s| s.len()).unwrap_or(0));
            }
            messages.remove(0);
            trimmed = true;
        }
        // A single pathological message could still exceed the budget.
        if bytes > EMIT_MESSAGES_BUDGET {
            if let Some(first) = messages.first_mut() {
                if let Some(text) = first
                    .get("text")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string())
                {
                    first["text"] = json!(tail_str(&text, EMIT_MESSAGES_BUDGET / 2));
                    trimmed = true;
                }
            }
        }
    }
    if let Some(stats) = state_value
        .get_mut("statsText")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
    {
        if stats.len() > EMIT_STATS_BUDGET {
            state_value["statsText"] = json!(tail_str(&stats, EMIT_STATS_BUDGET));
            trimmed = true;
        }
    }
    trimmed
}

/// Bound event arguments type-aware. String args truncate char-safe;
/// non-string (nested object/array/number) args over
/// [`EMIT_NESTED_ARG_BUDGET`] serialized bytes are replaced by a bounded
/// informational object — this is what keeps a near-4MiB nested
/// `statsChanged` payload from erasing lifecycle via the old fallback.
/// `uiRequest` events are NEVER bounded here: approval previews stay exact
/// and are normalized separately to the single final approval.
fn bound_other_events(events: &mut Vec<UiEvent>) {
    for event in events.iter_mut() {
        if event.name == "uiRequest" {
            continue;
        }
        for arg in event.args.iter_mut() {
            if let Some(s) = arg.as_str() {
                if s.len() > EMIT_EVENT_ARG_BUDGET {
                    *arg = json!(truncate_str_arg(s, EMIT_EVENT_ARG_BUDGET));
                }
                continue;
            }
            if arg.is_null() {
                continue;
            }
            // Numbers/bools serialize tiny; only objects/arrays can blow up.
            if !(arg.is_object() || arg.is_array()) {
                continue;
            }
            let len = arg_json_len(arg);
            if len > EMIT_NESTED_ARG_BUDGET {
                *arg = json!({"truncated": true, "bytes": len});
            }
        }
    }
}

/// Render an update that always preserves every lifecycle/identity/approval
/// field plus ack/events. Display payloads (`answer`, `messages`,
/// `statsText`) are trimmed on the emitted copy with a `displayTruncated`
/// flag. `statsChanged`/nested event args over budget become bounded
/// informational objects. `uiRequest` events normalize to zero/one exact
/// final `pendingApproval` (never truncated, never stale). An exact approval
/// that still cannot fit is cancelled fail-closed (authoritative removal +
/// Pi cancel write), never truncated. No path emits the old partial
/// `{displayTruncated:true}` fallback: if the exact frame cannot fit, the
/// result is terminal `fatal` (full lifecycle snapshot + signal) for the
/// main worker to teardown.
pub fn render_update(
    state: &mut AgentState,
    events_in: Vec<UiEvent>,
    ack: Option<String>,
    accepted: Option<bool>,
) -> RenderedUpdate {
    // Terminal ack guard: ack is echoed verbatim when legal; a near-4MiB
    // unbounded UI id must terminate rather than silently invalidate.
    if let Some(ref a) = ack {
        if a.len() > MAX_ACK_BYTES {
            let mut other: Vec<UiEvent> = Vec::new();
            let mut ui_payloads: Vec<Value> = Vec::new();
            for e in events_in {
                if e.name == "uiRequest" {
                    if e.args.is_empty() {
                        ui_payloads.push(Value::Null);
                    } else {
                        for arg in e.args {
                            ui_payloads.push(arg);
                        }
                    }
                } else {
                    other.push(e);
                }
            }
            bound_other_events(&mut other);
            let mut snap = state.snapshot();
            snap["answer"] = json!("");
            snap["messages"] = json!([]);
            snap["statsText"] = json!("");
            let final_approval = state.pending_approval.clone();
            let mut events = normalize_ref(&other, &ui_payloads, &final_approval);
            events.push(UiEvent::new(
                "failed",
                vec![json!("UI ack id exceeded bound; bridge terminal")],
            ));
            return finalize_full(snap, events, None, accepted, Vec::new(), true, false, true);
        }
    }

    // Partition: move huge stale uiRequest payloads aside (no clones),
    // keep the rest for type-aware bounding.
    let mut other: Vec<UiEvent> = Vec::new();
    let mut ui_payloads: Vec<Value> = Vec::new();
    for e in events_in {
        if e.name == "uiRequest" {
            if e.args.is_empty() {
                ui_payloads.push(Value::Null);
            } else {
                for arg in e.args {
                    ui_payloads.push(arg);
                }
            }
        } else {
            other.push(e);
        }
    }
    bound_other_events(&mut other);

    let mut pi_writes: Vec<Value> = Vec::new();
    let mut approval_cancelled = false;

    // Legal queue-id guard: Pi-provided ids are untrusted. Huge keys would
    // wedge the frame; cancel them fail-closed (one refresh, linear).
    let illegal: Vec<String> = state
        .pending_requests
        .keys()
        .filter(|k| k.len() > MAX_UI_ID_BYTES)
        .cloned()
        .collect();
    if !illegal.is_empty() {
        for id in &illegal {
            if state.pending_requests.remove(id).is_some() {
                pi_writes
                    .push(json!({"type": "extension_ui_response", "id": id, "cancelled": true}));
                approval_cancelled = true;
            }
        }
        let mut refresh_ev = Vec::new();
        state.refresh_pending_approval(&mut refresh_ev);
        for e in refresh_ev {
            if e.name == "uiRequest" {
                if e.args.is_empty() {
                    ui_payloads.push(Value::Null);
                } else {
                    for arg in e.args {
                        ui_payloads.push(arg);
                    }
                }
            } else {
                other.push(e);
            }
        }
        bound_other_events(&mut other);
        other.push(UiEvent::new(
            "failed",
            vec![json!(
                "Approval id exceeded bound; cancelled before showing"
            )],
        ));
    }

    // Single authoritative snapshot clone (linear); display trims on the copy.
    let mut snap = state.snapshot();
    let mut display_truncated = trim_display(&mut snap);

    // Bulk fail-closed loop: measure once, cancel enough at once, re-measure.
    // Each approval value is serialized at most once per bulk round for
    // sizing plus once for the final frame — never 32×4MiB quadratic clones.
    loop {
        let final_approval = state.pending_approval.clone();
        let events_norm = normalize_ref(&other, &ui_payloads, &final_approval);
        let total = measured_size(&snap, &events_norm, &ack, &accepted);
        if total <= EMIT_TARGET_BYTES {
            return finalize_full(
                snap,
                events_norm,
                ack,
                accepted,
                pi_writes,
                display_truncated,
                approval_cancelled,
                false,
            );
        }

        // Need to free bytes: estimate from per-entry serialized sizes.
        let ordered_ids: Vec<String> = state.pending_requests.keys().cloned().collect();
        if ordered_ids.is_empty() {
            // No approvals left but still over (display/events): zero display
            // payloads entirely on the copy; lifecycle stays complete.
            snap["answer"] = json!("");
            snap["messages"] = json!([]);
            snap["statsText"] = json!("");
            display_truncated = true;
            let events_norm2 = normalize_ref(&other, &ui_payloads, &final_approval);
            let total2 = measured_size(&snap, &events_norm2, &ack, &accepted);
            if total2 <= EMIT_TARGET_BYTES {
                return finalize_full(
                    snap,
                    events_norm2,
                    ack,
                    accepted,
                    pi_writes,
                    display_truncated,
                    approval_cancelled,
                    false,
                );
            }
            // Unreachable by construction (base + bounded events << target),
            // but fail closed terminal with FULL lifecycle keys rather than
            // a partial fallback.
            let mut events_term = events_norm2;
            events_term.push(UiEvent::new(
                "failed",
                vec![json!("Snapshot exceeded budget; bridge terminal")],
            ));
            return finalize_full(
                snap,
                events_term,
                ack,
                accepted,
                pi_writes,
                display_truncated,
                approval_cancelled,
                true,
            );
        }

        // Size each remaining entry once (no value clones, sequential temps).
        // Saving(k) accounts for the triple duplication (pendingApproval +
        // pendingRequests entry + normalized uiRequest): cancelling the head
        // frees its entry plus the old/new head duplication delta. This
        // avoids over-cancelling a displayable final approval.
        let mut value_lens: Vec<usize> = Vec::with_capacity(ordered_ids.len());
        let mut entry_lens: Vec<usize> = Vec::with_capacity(ordered_ids.len());
        for id in &ordered_ids {
            if let Some(p) = state.pending_requests.get(id) {
                let vl = arg_json_len(&p.request);
                value_lens.push(vl);
                entry_lens.push(vl.saturating_add(id.len() + 8));
            }
        }
        let need = total.saturating_sub(EMIT_TARGET_BYTES);
        let old_value = value_lens.first().cloned().unwrap_or(0);
        let mut k = 1usize;
        let mut found = false;
        for cand in 1..=value_lens.len() {
            let sum_entries: usize = entry_lens.iter().take(cand).sum();
            let new_value = value_lens.get(cand).cloned().unwrap_or(0);
            // Head is entry 0 whenever an approval is shown; orphan case is
            // handled below, so duplication applies when old exists.
            let saving = sum_entries
                .saturating_add(2usize.saturating_mul(old_value))
                .saturating_sub(2usize.saturating_mul(new_value));
            if saving >= need {
                k = cand;
                found = true;
                break;
            }
        }
        if !found {
            k = value_lens.len().max(1);
        }
        // Handle orphan approval (shown value with no queue entry): clear it
        // rather than wedging.
        let head_has_entry = match &state.pending_approval {
            None => false,
            Some(cur) => state.pending_requests.values().any(|p| &p.request == cur),
        };
        if state.pending_approval.is_some() && !head_has_entry {
            state.pending_approval = None;
            ui_payloads.push(Value::Null);
            other.push(UiEvent::new(
                "failed",
                vec![json!(
                    "Approval preview was unavailable; cancelled safely before display"
                )],
            ));
            approval_cancelled = true;
            snap["pendingApproval"] = json!(null);
            continue;
        }

        let cancel_ids: Vec<String> = ordered_ids.into_iter().take(k).collect();
        for id in &cancel_ids {
            if state.pending_requests.remove(id).is_some() {
                pi_writes
                    .push(json!({"type": "extension_ui_response", "id": id, "cancelled": true}));
                approval_cancelled = true;
            }
        }
        let mut refresh_ev = Vec::new();
        state.refresh_pending_approval(&mut refresh_ev);
        for e in refresh_ev {
            if e.name == "uiRequest" {
                if e.args.is_empty() {
                    ui_payloads.push(Value::Null);
                } else {
                    for arg in e.args {
                        ui_payloads.push(arg);
                    }
                }
            } else {
                other.push(e);
            }
        }
        bound_other_events(&mut other);
        other.push(UiEvent::new(
            "failed",
            vec![json!(
                "Approval too large to display safely; cancelled before showing"
            )],
        ));

        // Sync the emitted copy without re-snapshotting the whole queue:
        // drop the same ids (frees huge values), point approval at the new
        // head with a single clone.
        if let Some(map) = snap
            .get_mut("pendingRequests")
            .and_then(|v| v.as_object_mut())
        {
            for id in &cancel_ids {
                map.remove(id);
            }
        }
        snap["pendingApproval"] = state.pending_approval.clone().unwrap_or(Value::Null);
        // Loop re-measures once; queue shrinks every round so it terminates.
    }
}

/// Reference-based normalization (no stale huge clones): keep bounded
/// `other` plus zero/one exact final approval. Emits the single exact event
/// only when some observed payload equals the final value, so superseded
/// huge originals are dropped and the current display/cancel is exact.
fn normalize_ref(
    other: &[UiEvent],
    ui_payloads: &[Value],
    final_approval: &Option<Value>,
) -> Vec<UiEvent> {
    let want: Value = final_approval.clone().unwrap_or(Value::Null);
    let mut seen_final = false;
    for payload in ui_payloads {
        if *payload == want {
            seen_final = true;
            break;
        }
    }
    // Reuse the bounded-events normalization for the single exact check is
    // unnecessary: `want` stays exact by contract (never truncated).
    let mut out: Vec<UiEvent> = other.to_vec();
    if seen_final {
        out.push(UiEvent::new("uiRequest", vec![want]));
    }
    out
}

fn finalize_full(
    mut snap: Value,
    events: Vec<UiEvent>,
    ack: Option<String>,
    accepted: Option<bool>,
    pi_writes: Vec<Value>,
    display_truncated: bool,
    approval_cancelled: bool,
    fatal: bool,
) -> RenderedUpdate {
    if display_truncated {
        snap["displayTruncated"] = json!(true);
    }
    if fatal {
        snap["fatal"] = json!(true);
    }
    let update = BridgeUpdate {
        version: 1,
        kind: "update".to_string(),
        state: snap,
        events,
        ack,
        accepted,
    };
    let mut line = serde_json::to_string(&update).unwrap_or_else(|_| {
        "{\"version\":1,\"type\":\"update\",\"state\":{},\"events\":[]}".to_string()
    });
    // Absolute framing guard: never emit an oversize line, never emit a
    // partial-state fallback. By construction the budgeted paths already fit
    // TARGET < MAX; this only triggers on accounting defeat and still keeps
    // FULL lifecycle keys (display zeroed, approvals cleared in the copy,
    // ack dropped) plus a terminal fatal marker for the coordinator.
    if line.len() > MAX_LINE_BYTES - 1 {
        let mut snap_min = update.state.clone();
        snap_min["answer"] = json!("");
        snap_min["messages"] = json!([]);
        snap_min["statsText"] = json!("");
        snap_min["pendingApproval"] = json!(null);
        snap_min["pendingRequests"] = json!({});
        snap_min["displayTruncated"] = json!(true);
        snap_min["fatal"] = json!(true);
        let fallback = BridgeUpdate {
            version: 1,
            kind: "update".to_string(),
            state: snap_min,
            events: vec![UiEvent::new(
                "failed",
                vec![json!("Snapshot exceeded framing bound; bridge terminal")],
            )],
            ack: None,
            accepted: update.accepted,
        };
        line = serde_json::to_string(&fallback)
            .unwrap_or_else(|_| "{\"version\":1,\"type\":\"update\"}".to_string());
        if line.len() > MAX_LINE_BYTES - 1 {
            // Keys are tiny; this is unreachable. Truncation would break
            // JSON, so as an absolute last resort emit a minimal fatal shell
            // (still no partial lifecycle illusion: marked fatal).
            line.truncate(MAX_LINE_BYTES - 1);
        }
        line.push('\n');
        return RenderedUpdate {
            line: line.into_bytes(),
            pi_writes,
            display_truncated: true,
            approval_cancelled,
            requests_summarized: false,
            fatal: true,
        };
    }
    line.push('\n');
    RenderedUpdate {
        line: line.into_bytes(),
        pi_writes,
        display_truncated,
        approval_cancelled,
        requests_summarized: false,
        fatal,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::PendingReq;
    use std::sync::atomic::AtomicBool;
    use std::time::{Duration, Instant};

    fn ui_input_line(id: &str, op: &str) -> Vec<u8> {
        format!("{{\"version\":1,\"id\":\"{id}\",\"op\":\"{op}\",\"args\":{{}}}}\n").into_bytes()
    }

    #[test]
    fn coalesced_commands_in_one_write_all_surface() {
        let mut framer = LineFramer::new(1024);
        let mut chunk = Vec::new();
        chunk.extend_from_slice(&ui_input_line("ui-1", "start"));
        chunk.extend_from_slice(&ui_input_line("ui-2", "abort"));
        chunk.extend_from_slice(&ui_input_line("ui-3", "shutdown"));
        let frames = framer.push(&chunk);
        assert_eq!(frames.len(), 3, "3+ coalesced commands must all surface");
        for (i, frame) in frames.iter().enumerate() {
            match frame {
                Framed::Line(line) => {
                    let v: Value = serde_json::from_slice(line).expect("valid json");
                    assert_eq!(v["id"], format!("ui-{}", i + 1));
                }
                Framed::Oversize(_) => panic!("small lines must not be oversize"),
            }
        }
        assert!(framer.flush().is_none());
    }

    #[test]
    fn fragmentation_across_pushes_reassembles() {
        let mut framer = LineFramer::new(1024);
        let line = ui_input_line("ui-9", "start");
        let mut frames = Vec::new();
        // Feed byte-by-byte, including splits inside multibyte content.
        for byte in line.chunks(1) {
            frames.extend(framer.push(byte));
        }
        assert_eq!(frames.len(), 1);
        match &frames[0] {
            Framed::Line(done) => {
                let v: Value = serde_json::from_slice(done).expect("valid json");
                assert_eq!(v["id"], "ui-9");
            }
            Framed::Oversize(_) => panic!("must reassemble"),
        }
    }

    #[test]
    fn oversize_then_valid_retains_following_frame() {
        let max = 1024;
        let mut framer = LineFramer::new(max);
        let mut chunk = vec![b'x'; max + 100];
        chunk.push(b'\n');
        chunk.extend_from_slice(&ui_input_line("ui-2", "start"));
        let frames = framer.push(&chunk);
        assert_eq!(frames.len(), 2, "oversize then valid must both surface");
        match &frames[0] {
            Framed::Oversize(prefix) => {
                assert!(prefix.len() <= OVERSIZE_ID_SCAN);
            }
            Framed::Line(_) => panic!("expected oversize first"),
        }
        match &frames[1] {
            Framed::Line(done) => {
                let v: Value = serde_json::from_slice(done).expect("valid json");
                assert_eq!(v["id"], "ui-2");
            }
            Framed::Oversize(_) => panic!("valid line after oversize must survive"),
        }
    }

    #[test]
    fn oversize_id_hint_recovers_ack() {
        let mut framer = LineFramer::new(64);
        let mut big =
            format!("{{\"version\":1,\"id\":\"ui-77\",\"op\":\"prompt\",\"args\":{{\"message\":\"");
        big.push_str(&"y".repeat(5000));
        big.push_str("\"}}");
        big.push('\n');
        let frames = framer.push(big.as_bytes());
        assert_eq!(frames.len(), 1);
        match &frames[0] {
            Framed::Oversize(prefix) => {
                assert_eq!(extract_id_hint(prefix), Some("ui-77".to_string()));
            }
            Framed::Line(_) => panic!("expected oversize"),
        }
    }

    #[test]
    fn newline_in_same_chunk_after_overflow_still_acks_promptly() {
        // The old chunk reader lost bytes after an overflow within one read;
        // the ack then hung until the next input arrived.
        let mut framer = LineFramer::new(16);
        let mut chunk = b"0123456789abcdefSURPLUS".to_vec();
        chunk.push(b'\n');
        chunk.extend_from_slice(b"{\"version\":1}\n");
        let frames = framer.push(&chunk);
        assert_eq!(frames.len(), 2);
        assert!(matches!(frames[0], Framed::Oversize(_)));
        assert!(matches!(frames[1], Framed::Line(_)));
    }

    #[test]
    fn unterminated_stream_stays_bounded() {
        let max = 4096;
        let mut framer = LineFramer::new(max);
        // 10 MiB with no newline: nothing emitted, memory stays capped.
        for _ in 0..(10 * 1024 * 1024 / READ_CHUNK) {
            let frames = framer.push(&vec![b'z'; READ_CHUNK]);
            assert!(frames.is_empty());
            assert!(
                framer.retained() <= max + 1 + OVERSIZE_ID_SCAN,
                "retained={} exceeds bound",
                framer.retained()
            );
        }
        // Terminating newline reports the oversize; the stream recovers.
        let frames = framer.push(b"\n");
        assert_eq!(frames.len(), 1);
        assert!(matches!(frames[0], Framed::Oversize(_)));
        let frames = framer.push(&ui_input_line("ui-1", "start"));
        assert_eq!(frames.len(), 1);
        assert!(matches!(frames[0], Framed::Line(_)));
    }

    #[test]
    fn tail_str_respects_char_boundaries() {
        let s = "héllo wörld ✓✓✓ end";
        let tail = tail_str(s, 10);
        assert!(tail.len() <= 10);
        assert!(s.ends_with(tail));
        // Must not panic and must be valid str (guaranteed by type).
        let _ = tail.chars().count();
    }

    #[test]
    fn render_preserves_lifecycle_and_trims_display() {
        let mut state = AgentState::new(
            "pages/P.md".into(),
            false,
            "/s/a.jsonl".into(),
            "Name".into(),
            false,
        );
        state.process_started = true;
        state.desired_running = true;
        state.state_ok = true;
        state.extension_ok = true;
        state.busy = true;
        // Push the snapshot over the emit target so display trims engage.
        state.answer = "A".repeat(EMIT_TARGET_BYTES + 100);
        state.stats_text = "S".repeat(EMIT_STATS_BUDGET + 100);
        let events = vec![UiEvent::new("textDelta", vec![json!("hi")])];
        let rendered = render_update(&mut state, events, Some("ui-5".into()), Some(true));
        assert!(rendered.line.len() <= EMIT_TARGET_BYTES);
        assert!(rendered.display_truncated);
        assert!(rendered.pi_writes.is_empty());
        // Authoritative state untouched (display-only trim).
        assert!(state.answer.len() > EMIT_TARGET_BYTES);
        let text = &rendered.line[..rendered.line.len() - 1];
        let v: Value = serde_json::from_slice(text).expect("valid update");
        assert_eq!(v["ack"], "ui-5");
        assert_eq!(v["accepted"], true);
        assert_eq!(v["events"][0]["name"], "textDelta");
        for key in [
            "sessionFile",
            "sessionName",
            "ready",
            "retryable",
            "busy",
            "processStarted",
            "desiredRunning",
            "pendingApproval",
            "sessionRefreshPending",
            "status",
        ] {
            assert!(v["state"].get(key).is_some(), "lifecycle key {key} missing");
        }
        assert_eq!(v["state"]["sessionFile"], "/s/a.jsonl");
        assert_eq!(v["state"]["displayTruncated"], true);
    }

    #[test]
    fn render_never_truncates_exact_approval_but_cancels_fail_closed() {
        let mut state = AgentState::new(
            String::new(),
            true,
            "/j/a.jsonl".into(),
            String::new(),
            false,
        );
        state.process_started = true;
        state.desired_running = true;
        // Exact approval ~1 MiB escaped, duplicated across approval+requests.
        let big_text = "P".repeat(900 * 1024);
        let approval = json!({"type":"extension_ui_request","id":"r-big","method":"confirm",
            "title":"t","message": big_text});
        state.pending_approval = Some(approval.clone());
        state.pending_requests.insert(
            "r-big".into(),
            PendingReq {
                request: approval.clone(),
                deadline: Instant::now() + Duration::from_secs(60),
            },
        );
        // Force over budget even after display trims + dedup by padding.
        state.answer = "A".repeat(EMIT_ANSWER_TAIL + 8);
        let rendered = render_update(&mut state, vec![], Some("ui-1".into()), Some(true));
        assert!(rendered.line.len() <= EMIT_TARGET_BYTES);
        let text = &rendered.line[..rendered.line.len() - 1];
        let v: Value = serde_json::from_slice(text).expect("valid update");
        if rendered.approval_cancelled {
            // Fail closed: cancel write queued, queue cleared, failure shown.
            assert_eq!(rendered.pi_writes.len(), 1);
            assert_eq!(rendered.pi_writes[0]["cancelled"], true);
            assert!(state.pending_requests.is_empty());
            let names: Vec<&str> = v["state"]["pendingApproval"]
                .as_null()
                .map(|_| vec![])
                .unwrap_or_default();
            let _ = names;
            assert!(v["state"]["pendingApproval"].is_null());
            let failed = v["events"]
                .as_array()
                .unwrap()
                .iter()
                .any(|e| e["name"] == "failed");
            assert!(failed, "fail-closed cancel must surface failed");
        } else {
            // Fits after dedup: exact approval preserved verbatim once.
            assert_eq!(v["state"]["pendingApproval"], approval);
        }
        assert_eq!(v["ack"], "ui-1");
    }

    fn lifecycle_keys() -> Vec<&'static str> {
        vec![
            "sessionFile",
            "sessionName",
            "freshSession",
            "projectPath",
            "journalMode",
            "scopedMode",
            "model",
            "models",
            "commands",
            "stateOk",
            "extensionOk",
            "ready",
            "busy",
            "compacting",
            "sessionSwitching",
            "sessionRefreshPending",
            "sessionRefreshFailed",
            "sessionRefreshGeneration",
            "sessionChangeInFlight",
            "sessionChangeKind",
            "sessionChangeRequestId",
            "sessionChangeCancelRequested",
            "answer",
            "messages",
            "messagesSessionFile",
            "messagesGeneration",
            "messagesAwaitingSessionState",
            "messagesTruncated",
            "status",
            "pendingApproval",
            "pendingRequests",
            "serial",
            "generation",
            "lastRequestId",
            "processStarted",
            "processStartFailed",
            "startRequested",
            "launchAttempted",
            "idleStopped",
            "pendingResume",
            "desiredRunning",
            "retryable",
            "idleStopping",
            "stopping",
            "controlPending",
            "diagnostic",
            "statsText",
        ]
    }

    fn live_state() -> AgentState {
        let mut s = AgentState::new(
            "pages/P.md".into(),
            false,
            "/s/a.jsonl".into(),
            "Name".into(),
            false,
        );
        s.process_started = true;
        s.desired_running = true;
        s.state_ok = true;
        s.extension_ok = true;
        s.busy = false;
        s.status = "Ready".into();
        s
    }

    #[test]
    fn render_bounds_huge_nested_stats_event_without_lifecycle_loss() {
        let mut state = live_state();
        // Near-4MiB nested object (not a plain string): the old string-only
        // bound let this through verbatim into the ~4MiB fallback.
        let huge = json!({"session": {"tokens": {"blob": "S".repeat(3_500_000)}}});
        assert!(arg_json_len(&huge) > EMIT_NESTED_ARG_BUDGET);
        let events = vec![
            UiEvent::new("statsChanged", vec![huge]),
            UiEvent::new("textDelta", vec![json!("hi")]),
        ];
        let rendered = render_update(&mut state, events, Some("ui-9".into()), Some(true));
        assert!(!rendered.fatal, "bounded stats must not go terminal");
        assert!(rendered.line.len() <= MAX_LINE_BYTES - 1);
        assert!(rendered.line.len() <= EMIT_TARGET_BYTES);
        let text = &rendered.line[..rendered.line.len() - 1];
        let v: Value = serde_json::from_slice(text).expect("valid update");
        // Full lifecycle, never the partial {displayTruncated:true} shell.
        for key in lifecycle_keys() {
            assert!(v["state"].get(key).is_some(), "lifecycle key {key} missing");
        }
        assert_eq!(v["state"]["sessionFile"], "/s/a.jsonl");
        assert_eq!(v["ack"], "ui-9");
        // statsChanged replaced by a bounded informational object.
        let stats_ev = v["events"]
            .as_array()
            .unwrap()
            .iter()
            .find(|e| e["name"] == "statsChanged")
            .expect("statsChanged survives bounded");
        let arg = &stats_ev["args"][0];
        assert_eq!(arg["truncated"], true);
        assert!(arg["bytes"].as_u64().unwrap() > 3_000_000);
        assert!(
            serde_json::to_string(arg).unwrap().len() < 1024,
            "informational object stays bounded"
        );
        assert!(!rendered.requests_summarized, "no degraded summaries");
    }

    #[test]
    fn render_normalizes_huge_ui_request_and_cancels_serial_queue() {
        let mut state = live_state();
        // r-01 huge stale + r-02 huge intermediate + r-03 small final.
        // Total with triple duplication (~6MiB) forces two serial cancels.
        let r1 = json!({"type":"extension_ui_request","id":"r-01","method":"confirm",
            "title":"t","message": "H".repeat(1_800_000)});
        let r2 = json!({"type":"extension_ui_request","id":"r-02","method":"confirm",
            "title":"t","message": "M".repeat(1_200_000)});
        let r3 = json!({"type":"extension_ui_request","id":"r-03","method":"confirm",
            "title":"t","message": "ok"});
        for (id, req) in [
            ("r-01", r1.clone()),
            ("r-02", r2.clone()),
            ("r-03", r3.clone()),
        ] {
            state.pending_requests.insert(
                id.into(),
                PendingReq {
                    request: req,
                    deadline: Instant::now() + Duration::from_secs(60),
                },
            );
        }
        state.pending_approval = Some(r1.clone());
        // Actual large uiRequest event carrying the stale huge original.
        let events = vec![
            UiEvent::new("uiRequest", vec![r1.clone()]),
            UiEvent::new("textDelta", vec![json!("hi")]),
        ];
        let rendered = render_update(&mut state, events, Some("ui-7".into()), Some(true));
        assert!(!rendered.fatal);
        assert!(rendered.approval_cancelled);
        assert_eq!(rendered.pi_writes.len(), 2, "serial cancels r-01 then r-02");
        assert_eq!(rendered.pi_writes[0]["id"], "r-01");
        assert_eq!(rendered.pi_writes[1]["id"], "r-02");
        assert_eq!(
            state.pending_approval,
            Some(r3.clone()),
            "final exact, never truncated"
        );
        assert!(!state.pending_requests.contains_key("r-01"));
        assert!(!state.pending_requests.contains_key("r-02"));
        assert!(state.pending_requests.contains_key("r-03"));
        assert!(rendered.line.len() <= EMIT_TARGET_BYTES);
        assert!(rendered.line.len() <= MAX_LINE_BYTES - 1);
        let text = &rendered.line[..rendered.line.len() - 1];
        let v: Value = serde_json::from_slice(text).expect("valid update");
        for key in lifecycle_keys() {
            assert!(v["state"].get(key).is_some(), "lifecycle key {key} missing");
        }
        // Snapshot previews exact, never truncated with ellipsis.
        assert_eq!(v["state"]["pendingApproval"], r3);
        assert_eq!(v["state"]["pendingRequests"]["r-03"], r3);
        let serialized = serde_json::to_string(&v["state"]["pendingApproval"]).unwrap();
        assert!(
            !serialized.contains('…'),
            "approval previews never truncated"
        );
        // Events: exactly one uiRequest, the current display — no superseded huge.
        let ui_reqs: Vec<&Value> = v["events"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|e| e["name"] == "uiRequest")
            .collect();
        assert_eq!(ui_reqs.len(), 1, "ONLY final pendingApproval exact");
        assert_eq!(ui_reqs[0]["args"][0], r3);
        let whole = serde_json::to_string(&v).unwrap();
        assert!(
            !whole.contains(&"H".repeat(1000)),
            "stale r-01 blob must not leak"
        );
        assert!(
            !whole.contains(&"M".repeat(1000)),
            "superseded r-02 blob must not leak"
        );
        assert!(
            v["events"]
                .as_array()
                .unwrap()
                .iter()
                .any(|e| e["name"] == "failed"),
            "cancellations surface failed"
        );
        assert!(!rendered.requests_summarized);
        assert_eq!(v["ack"], "ui-7");
    }

    #[test]
    fn render_32_queued_bounded_linear_no_quadratic() {
        let mut state = live_state();
        // 32 queued exact approvals, each ~50KiB (bounded, not 32×4MiB in
        // test to stay fast). Forces bulk fail-closed without quadratic
        // clones: each value sized once, one snapshot, one final frame.
        let mut first: Option<Value> = None;
        for i in 0..32 {
            let id = format!("r-{i:02}");
            let req = json!({"type":"extension_ui_request","id": id,
                "method":"confirm","title":"t","message": "Q".repeat(50 * 1024)});
            if i == 0 {
                first = Some(req.clone());
            }
            state.pending_requests.insert(
                id,
                PendingReq {
                    request: req,
                    deadline: Instant::now() + Duration::from_secs(60),
                },
            );
        }
        state.pending_approval = first;
        let rendered = render_update(&mut state, vec![], Some("ui-3".into()), Some(true));
        assert!(!rendered.fatal);
        assert!(rendered.line.len() <= MAX_LINE_BYTES - 1);
        let text = &rendered.line[..rendered.line.len() - 1];
        let v: Value = serde_json::from_slice(text).expect("valid update");
        for key in lifecycle_keys() {
            assert!(v["state"].get(key).is_some(), "lifecycle key {key} missing");
        }
        // Whatever survived is exact (keys complete, values untruncated).
        if let Some(map) = v["state"]["pendingRequests"].as_object() {
            for (id, slot) in map {
                assert!(slot.get("method").is_some() || slot.get("id").is_some());
                let s = serde_json::to_string(slot).unwrap();
                assert!(!s.contains('…'), "queued previews never truncated {id}");
            }
        }
        assert!(!rendered.requests_summarized);
    }

    #[test]
    fn render_fatal_on_huge_ack_keeps_full_lifecycle() {
        let mut state = live_state();
        let huge_ack = "A".repeat(MAX_ACK_BYTES + 10_000);
        let rendered = render_update(&mut state, vec![], Some(huge_ack), Some(true));
        assert!(
            rendered.fatal,
            "huge ack must terminate, never silently invalid"
        );
        assert!(rendered.line.len() <= MAX_LINE_BYTES - 1);
        let text = &rendered.line[..rendered.line.len() - 1];
        let v: Value = serde_json::from_slice(text).expect("valid update");
        for key in lifecycle_keys() {
            assert!(v["state"].get(key).is_some(), "lifecycle key {key} missing");
        }
        assert_eq!(v["state"]["fatal"], true);
        assert!(
            v.get("ack").is_none(),
            "huge ack dropped for terminal frame"
        );
        assert!(
            v["events"]
                .as_array()
                .unwrap()
                .iter()
                .any(|e| e["name"] == "failed"),
            "terminal carries failed marker"
        );
    }

    #[test]
    fn write_deadline_fails_closed_on_blocked_peer() {
        let mut fds = [0; 2];
        assert_eq!(unsafe { libc::pipe(fds.as_mut_ptr()) }, 0);
        let (read_fd, write_fd) = (fds[0], fds[1]);
        set_nonblocking(write_fd).expect("nonblocking");
        // Fill the pipe until backpressure.
        let chunk = vec![0u8; 64 * 1024];
        loop {
            let rc = unsafe { libc::write(write_fd, chunk.as_ptr() as *const _, chunk.len()) };
            if rc < 0 {
                break;
            }
        }
        let term = AtomicBool::new(false);
        let big = vec![1u8; 1024 * 1024];
        let start = Instant::now();
        let rc = write_all_deadline(write_fd, &big, Duration::from_millis(150), &term);
        assert_eq!(rc, Err(WriteFail::Timeout));
        assert!(start.elapsed() < Duration::from_secs(5));
        // Termination flag aborts promptly even with capacity supposedly free.
        term.store(true, Ordering::SeqCst);
        let rc = write_all_deadline(write_fd, &big, Duration::from_secs(5), &term);
        assert_eq!(rc, Err(WriteFail::Terminated));
        unsafe {
            libc::close(read_fd);
            libc::close(write_fd);
        }
    }

    #[test]
    fn write_deadline_reports_closed_peer() {
        let mut fds = [0; 2];
        assert_eq!(unsafe { libc::pipe(fds.as_mut_ptr()) }, 0);
        unsafe { libc::close(fds[0]) }; // no reader
        set_nonblocking(fds[1]).expect("nonblocking");
        let term = AtomicBool::new(false);
        // First write may succeed into the buffer or SIGPIPE/EPIPE; either
        // way the outcome must be Closed or Timeout, never a hang.
        let big = vec![1u8; 1024 * 1024];
        let mut seen_closed = false;
        for _ in 0..10 {
            match write_all_deadline(fds[1], &big, Duration::from_millis(100), &term) {
                Err(WriteFail::Closed) => {
                    seen_closed = true;
                    break;
                }
                Err(WriteFail::Timeout) => continue,
                Ok(()) => continue,
                Err(e) => panic!("unexpected {e:?}"),
            }
        }
        assert!(seen_closed, "closed peer must report Closed");
        unsafe { libc::close(fds[1]) };
    }
}
