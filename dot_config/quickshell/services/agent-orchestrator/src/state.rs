use crate::history::{
    history_msg_to_value, normalize_messages, text_from_message, truncate_head, truncate_tail,
    HistoryMsg,
};
use crate::proto::UiEvent;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::time::Instant;

/// Approval/UI timeout cap: min(timeout ?? 120s, 120s).
pub const APPROVAL_MAX_MS: u64 = 120_000;
const APPROVAL_DEFAULT_MS: u64 = 120_000;
pub const MAX_ANSWER_BYTES: usize = 512 * 1024;
pub const MAX_MESSAGES: usize = 1000;
pub const MAX_MESSAGE_TEXT: usize = 64 * 1024;
/// Aggregate display budget for history text. Prevents 1000 * 64KiB (64MiB)
/// snapshots; oldest messages are dropped first and `messagesTruncated`
/// reports the loss truthfully.
pub const MAX_HISTORY_BYTES: usize = 512 * 1024;
pub const MAX_DIAGNOSTIC_BYTES: usize = 2048;
pub const MAX_STATS_BYTES: usize = 64 * 1024;
/// Untrusted agent metadata budgets.
pub const MAX_SESSION_FILE_BYTES: usize = 4096;
pub const MAX_SESSION_NAME_BYTES: usize = 1024;
pub const MAX_MODEL_BYTES: usize = 16 * 1024;
pub const MAX_COMMANDS_ITEMS: usize = 256;
pub const MAX_COMMANDS_BYTES: usize = 256 * 1024;
pub const MAX_MODELS_ITEMS: usize = 256;
pub const MAX_MODELS_BYTES: usize = 256 * 1024;

#[derive(Debug, Clone)]
pub struct PendingReq {
    pub request: Value,
    pub deadline: Instant,
    /// True once any UI has opened a dialog for this request (S-048). A
    /// surfaced request never expires silently: it lives until answered,
    /// stopped, or bridge/child exit.
    pub surfaced: bool,
    /// True while parked by an explicit defer (S-048). A deferred request
    /// never expires and is skipped while non-deferred requests remain;
    /// when only deferred requests remain a new round clears the flags.
    pub deferred: bool,
}

#[derive(Debug, Clone)]
pub struct RequestKind {
    pub kind: String,
    pub message: Option<String>,
    pub session_file: Option<String>,
    pub generation: Option<u64>,
    pub refresh_generation: Option<u64>,
}

impl RequestKind {
    pub fn simple(kind: &str) -> Self {
        Self {
            kind: kind.to_string(),
            message: None,
            session_file: None,
            generation: None,
            refresh_generation: None,
        }
    }
}

/// Typed orchestrator state mirroring the legacy scoped-worker semantics.
#[derive(Debug)]
pub struct AgentState {
    pub session_file: String,
    pub session_name: String,
    pub fresh_session: bool,
    pub project_id: String,
    pub project_path: String,
    pub journal_mode: bool,
    pub model: Value,
    pub models: Vec<Value>,
    pub commands: Vec<Value>,
    pub state_ok: bool,
    pub extension_ok: bool,
    pub busy: bool,
    pub compacting: bool,
    pub session_switching: bool,
    pub session_refresh_pending: bool,
    pub session_refresh_failed: bool,
    pub session_refresh_generation: u64,
    pub session_change_in_flight: bool,
    pub session_change_kind: String,
    pub session_change_request_id: String,
    pub session_change_cancel_requested: bool,
    pub answer: String,
    pub messages: Vec<HistoryMsg>,
    pub messages_session_file: String,
    pub messages_generation: u64,
    pub messages_awaiting_session_state: bool,
    /// True when history was dropped for display budgeting (count or byte
    /// cap). Surfaced as `messagesTruncated` so the UI never mistakes a
    /// partial view for the full session.
    pub messages_truncated: bool,
    pub status: String,
    pub pending_approval: Option<Value>,
    pub pending_requests: BTreeMap<String, PendingReq>,
    pub serial: u64,
    pub generation: u64,
    pub request_kinds: BTreeMap<String, RequestKind>,
    pub last_request_id: String,
    pub process_started: bool,
    pub process_start_failed: bool,
    pub start_requested: bool,
    pub launch_attempted: bool,
    pub idle_stopped: bool,
    pub pending_resume: bool,
    pub desired_running: bool,
    pub idle_stopping: bool,
    pub stopping: bool,
    pub control_pending: bool,
    pub diagnostic: String,
    pub stats_text: String,
}

impl AgentState {
    pub fn new(
        project_path: String,
        journal_mode: bool,
        session: String,
        pending_name: String,
        fresh: bool,
    ) -> Self {
        Self::new_with_id(String::new(), project_path, journal_mode, session, pending_name, fresh)
    }

    pub fn new_with_id(
        project_id: String,
        project_path: String,
        journal_mode: bool,
        session: String,
        pending_name: String,
        fresh: bool,
    ) -> Self {
        let session_name = pending_name;
        Self {
            session_file: session,
            session_name,
            fresh_session: fresh,
            project_id,
            project_path,
            journal_mode,
            model: Value::Null,
            models: vec![],
            commands: vec![],
            state_ok: false,
            extension_ok: false,
            busy: false,
            compacting: false,
            session_switching: false,
            session_refresh_pending: false,
            session_refresh_failed: false,
            session_refresh_generation: 0,
            session_change_in_flight: false,
            session_change_kind: String::new(),
            session_change_request_id: String::new(),
            session_change_cancel_requested: false,
            answer: String::new(),
            messages: vec![],
            messages_session_file: String::new(),
            messages_generation: 0,
            messages_awaiting_session_state: false,
            messages_truncated: false,
            status: "Starting pi…".to_string(),
            pending_approval: None,
            pending_requests: BTreeMap::new(),
            serial: 0,
            generation: 0,
            request_kinds: BTreeMap::new(),
            last_request_id: String::new(),
            process_started: false,
            process_start_failed: false,
            start_requested: false,
            launch_attempted: false,
            idle_stopped: false,
            pending_resume: false,
            desired_running: false,
            idle_stopping: false,
            stopping: false,
            control_pending: false,
            diagnostic: String::new(),
            stats_text: String::new(),
        }
    }

    /// Private graph scope only (project page or journal pool). The palette
    /// (`--mode palette`) is never scoped: its snapshot keeps
    /// `scopedMode=false` so no graph tool allowlist is implied. Session
    /// ACK/authoritative fencing below intentionally applies to ALL modes
    /// (separate from this property) so palette New/Restore cannot release
    /// its gate before the matching ACK + authoritative state.
    pub fn scoped_mode(&self) -> bool {
        !self.project_id.is_empty() || !self.project_path.is_empty() || self.journal_mode
    }

    pub fn ready(&self) -> bool {
        self.process_started && self.state_ok && self.extension_ok && !self.session_refresh_pending
    }

    pub fn retryable(&self) -> bool {
        self.session_refresh_failed
            || (self.launch_attempted
                && !self.process_started
                && !self.start_requested
                && !self.idle_stopped)
    }

    pub fn ready_status(&self) -> String {
        if self.busy {
            "Thinking…".to_string()
        } else if self.compacting {
            "Compacting…".to_string()
        } else {
            "Ready".to_string()
        }
    }

    // -- session-refresh helpers (legacy parity) --
    // Fencing is intentionally mode-agnostic: every mode (including palette)
    // gates New/Rename/Restore on the matching ACK + authoritative get_state.
    // Only `scoped_mode()` (snapshot graph scope) stays mode-specific.
    pub fn mark_session_refresh(&mut self) {
        self.session_refresh_generation += 1;
        self.session_refresh_pending = true;
        self.session_refresh_failed = false;
    }

    pub fn begin_session_change(&mut self, kind: &str, request_id: &str) {
        self.session_change_in_flight = true;
        self.session_change_kind = kind.to_string();
        self.session_change_request_id = request_id.to_string();
        self.session_change_cancel_requested = false;
    }

    pub fn settle_session_change(&mut self) {
        self.session_change_in_flight = false;
        self.session_change_kind.clear();
        self.session_change_request_id.clear();
        self.session_change_cancel_requested = false;
    }

    pub fn begin_session_refresh(&mut self) {
        self.mark_session_refresh();
        self.messages_awaiting_session_state = true;
        self.clear_messages("");
    }

    pub fn clear_messages(&mut self, session: &str) {
        self.messages.clear();
        self.messages_session_file = session.to_string();
        self.messages_generation += 1;
        self.messages_truncated = false;
    }

    pub fn is_authoritative_state(&self, info: Option<&RequestKind>) -> bool {
        if !self.session_refresh_pending {
            return false;
        }
        match info {
            Some(r) => {
                r.kind == "get_state"
                    && r.refresh_generation == Some(self.session_refresh_generation)
            }
            None => false,
        }
    }

    /// True when `id` is the inflight session-change ACK that may fail the
    /// refresh. Startup (no inflight) never qualifies here; only the
    /// authoritative get_state may fail it.
    fn is_matching_change_failure(&self, id: &str, info: Option<&RequestKind>) -> bool {
        if !self.session_change_in_flight {
            return false;
        }
        if id.is_empty() || id != self.session_change_request_id {
            return false;
        }
        let kind = match info {
            Some(r) => r.kind.as_str(),
            None => return false,
        };
        match self.session_change_kind.as_str() {
            "new" => kind == "new_session",
            "rename" => kind == "set_session_name",
            "restore" => {
                kind == "prompt"
                    && info.as_ref().and_then(|r| r.message.as_deref()) == Some("/desktop-sessions")
            }
            _ => matches!(kind, "prompt" | "new_session" | "set_session_name"),
        }
    }

    /// True when a refresh triggered by `ack_id` may be tagged authoritative.
    /// During a session change only the matching change ACK authorizes a new
    /// generation; unsolicited/settled refreshes stay untagged so old state
    /// cannot release the gate. Startup (no inflight) is separately allowed.
    fn refresh_authorizes(&self, ack_id: &str) -> bool {
        if !self.session_change_in_flight {
            return true;
        }
        !ack_id.is_empty() && ack_id == self.session_change_request_id
    }

    /// Returns true when the refresh was failed now (and a failed event was pushed).
    pub fn fail_session_refresh(
        &mut self,
        message: &str,
        command_settled: bool,
        events: &mut Vec<UiEvent>,
    ) -> bool {
        if self.session_change_in_flight && !command_settled && self.process_started {
            self.session_change_cancel_requested = true;
            self.status = "Cancelling session change…".to_string();
            return false;
        }
        self.session_refresh_pending = false;
        self.session_refresh_failed = true;
        self.settle_session_change();
        self.session_switching = false;
        self.control_pending = false;
        self.busy = false;
        self.compacting = false;
        self.stopping = false;
        self.state_ok = false;
        self.extension_ok = false;
        self.session_file.clear();
        self.session_name.clear();
        self.model = Value::Null;
        self.messages_awaiting_session_state = false;
        self.clear_messages("");
        self.status = "Session refresh failed; retry".to_string();
        events.push(UiEvent::new("failed", vec![json!(message)]));
        true
    }

    pub fn finish_session_switch(&mut self) {
        if self.session_refresh_pending {
            return;
        }
        self.session_switching = false;
        self.busy = false;
        self.status = self.ready_status();
    }

    pub fn accepts_message_response(
        &self,
        requested_session: &str,
        requested_generation: u64,
        has_generation: bool,
    ) -> bool {
        if self.session_refresh_failed {
            return false;
        }
        if self.messages_awaiting_session_state {
            return false;
        }
        if has_generation && requested_generation != self.messages_generation {
            return false;
        }
        if !requested_session.is_empty()
            && !self.session_file.is_empty()
            && requested_session != self.session_file
        {
            return false;
        }
        true
    }

    /// Returns true when history was applied (caller emits historyLoaded).
    pub fn apply_messages(
        &mut self,
        values: &Value,
        requested_session: &str,
        requested_generation: u64,
        has_generation: bool,
    ) -> bool {
        if !self.accepts_message_response(requested_session, requested_generation, has_generation) {
            return false;
        }
        // Authoritative identity + history-generation gates for all modes
        // (no Python scope enforcement for palette): history only applies
        // when it was requested for the current file + generation.
        if self.session_file.is_empty()
            || requested_session != self.session_file
            || (has_generation && requested_generation != self.messages_generation)
        {
            return false;
        }
        let mut msgs = normalize_messages(values);
        let mut truncated = false;
        if msgs.len() > MAX_MESSAGES {
            let skip = msgs.len() - MAX_MESSAGES;
            msgs = msgs.into_iter().skip(skip).collect();
            truncated = true;
        }
        for m in msgs.iter_mut() {
            if m.text.len() > MAX_MESSAGE_TEXT {
                m.text = truncate_head(&m.text, MAX_MESSAGE_TEXT);
                truncated = true;
            }
        }
        // Aggregate display budget: 512KiB total, oldest dropped first.
        // `text_from_message` already bounds each message to 64KiB, but
        // 1000 * 64KiB would still blow the 4MiB snapshot frame.
        let mut total: usize = msgs.iter().map(|m| m.text.len()).sum();
        if total > MAX_HISTORY_BYTES {
            // Drop oldest whole messages first.
            let mut drop_count = 0;
            while drop_count < msgs.len() && total > MAX_HISTORY_BYTES {
                total = total.saturating_sub(msgs[drop_count].text.len());
                drop_count += 1;
            }
            if drop_count > 0 {
                msgs = msgs.into_iter().skip(drop_count).collect();
                truncated = true;
            }
            // A single pathological message is already capped to 64KiB by
            // the per-message step, so total must now fit.
        }
        self.messages = msgs;
        self.messages_truncated = truncated;
        // Session identity is never fabricated: requested_session echoes our
        // own tracked session_file (already validated on get_state), so copy
        // it verbatim instead of silently truncating.
        self.messages_session_file = if self.session_file.is_empty() {
            requested_session.to_string()
        } else {
            self.session_file.clone()
        };
        true
    }

    pub fn next_rpc_id(&mut self) -> String {
        self.serial += 1;
        format!("qs-{}", self.serial)
    }

    /// Queue an RPC to Pi. Caller must actually write the line; this only
    /// tracks correlation. Returns the RPC id.
    #[allow(dead_code)]
    pub fn track_request(&mut self, kind: RequestKind) -> String {
        let id = self.next_rpc_id();
        self.request_kinds.insert(id.clone(), kind);
        id
    }

    pub fn push_answer_delta(&mut self, delta: &str) {
        if delta.is_empty() {
            return;
        }
        // Bound the delta itself first so a single hostile frame cannot
        // balloon the buffer before the tail cap applies.
        let delta = truncate_tail(delta, MAX_ANSWER_BYTES);
        self.answer.push_str(&delta);
        if self.answer.len() > MAX_ANSWER_BYTES {
            self.answer = truncate_tail(&self.answer, MAX_ANSWER_BYTES);
        }
    }

    pub fn set_answer(&mut self, text: String) {
        if text.len() > MAX_ANSWER_BYTES {
            self.answer = truncate_tail(&text, MAX_ANSWER_BYTES);
        } else {
            self.answer = text;
        }
    }

    pub fn set_answer_str(&mut self, text: &str) {
        self.answer = truncate_tail(text, MAX_ANSWER_BYTES);
    }

    pub fn push_diagnostic(&mut self, line: &str) {
        // Keep the stderr tail UTF-8 safe (legacy slice(-2048) parity).
        let line = truncate_tail(line, MAX_DIAGNOSTIC_BYTES);
        let mut combined = self.diagnostic.clone();
        if !combined.is_empty() {
            combined.push('\n');
        }
        combined.push_str(&line);
        if combined.len() > MAX_DIAGNOSTIC_BYTES {
            combined = truncate_tail(&combined, MAX_DIAGNOSTIC_BYTES);
        }
        self.diagnostic = combined;
    }

    fn bound_stats_text(text: &str) -> String {
        truncate_head(text, MAX_STATS_BYTES)
    }

    fn bound_value_array(items: Vec<Value>, max_items: usize, max_bytes: usize) -> Vec<Value> {
        let mut out: Vec<Value> = items.into_iter().take(max_items).collect();
        // Drop trailing entries until the serialized budget fits. Keeps the
        // first (most relevant) entries, matching list() ordering.
        loop {
            let bytes: usize = out.iter().map(|v| v.to_string().len()).sum();
            if bytes <= max_bytes || out.is_empty() {
                break;
            }
            out.pop();
        }
        out
    }

    fn bound_model(value: Value) -> Value {
        if value.is_null() {
            return value;
        }
        let s = value.to_string();
        if s.len() <= MAX_MODEL_BYTES {
            return value;
        }
        Value::Null
    }

    pub fn reject_all_requests(&mut self, events: &mut Vec<UiEvent>) -> Vec<Value> {
        // Returns extension_ui_response lines to write when the child is live.
        // Never starts the child; caller decides whether to write.
        let mut lines = Vec::new();
        for id in self.pending_requests.keys().cloned().collect::<Vec<_>>() {
            lines.push(json!({"type": "extension_ui_response", "id": id, "cancelled": true}));
        }
        self.pending_requests.clear();
        if self.pending_approval.is_some() {
            self.pending_approval = None;
            events.push(UiEvent::new("uiRequest", vec![Value::Null]));
        }
        lines
    }

    pub fn refresh_pending_approval(&mut self, events: &mut Vec<UiEvent>) {
        // Round-robin (S-048): surface the first non-deferred request. A
        // deferred request is skipped while other non-deferred requests
        // remain; when only deferred requests remain a new round clears
        // their flags and the first is surfaced again.
        if !self.pending_requests.values().any(|p| !p.deferred) {
            for p in self.pending_requests.values_mut() {
                p.deferred = false;
            }
        }
        let next = self
            .pending_requests
            .values()
            .find(|p| !p.deferred)
            .or_else(|| self.pending_requests.values().next())
            .map(|p| p.request.clone());
        let changed = next != self.pending_approval;
        self.pending_approval = next;
        if changed {
            events.push(UiEvent::new(
                "uiRequest",
                vec![self.pending_approval.clone().unwrap_or(Value::Null)],
            ));
        }
    }

    pub fn expire_approvals(&mut self, events: &mut Vec<UiEvent>) -> Vec<Value> {
        // Fail-closed expiry (S-048) applies ONLY to requests that were
        // never surfaced to any UI: the safety valve for dead/unresponsive
        // UIs. Surfaced and deferred requests never expire silently; they
        // are cancelled only by explicit Reject/Stop/exit.
        let now = Instant::now();
        let expired: Vec<String> = self
            .pending_requests
            .iter()
            .filter(|(_, p)| !p.surfaced && !p.deferred && p.deadline <= now)
            .map(|(k, _)| k.clone())
            .collect();
        let mut lines = Vec::new();
        for id in expired {
            self.pending_requests.remove(&id);
            lines.push(json!({"type": "extension_ui_response", "id": id, "cancelled": true}));
        }
        if !lines.is_empty() {
            self.refresh_pending_approval(events);
        }
        lines
    }

    pub fn handle_ui_request(&mut self, value: &Value, events: &mut Vec<UiEvent>) -> Vec<Value> {
        // Returns an extension_ui_response line to write when the child is
        // live (overflow cancel only). Never starts the child; caller decides
        // whether to write. Mirrors the reject_all_requests/expire_approvals
        // contract.
        let mut lines = Vec::new();
        let method = value.get("method").and_then(|v| v.as_str()).unwrap_or("");
        if !["select", "confirm", "input", "editor"].contains(&method) {
            if method == "notify" {
                if let Some(msg) = value.get("message").and_then(|v| v.as_str()) {
                    if !msg.is_empty() {
                        self.status = truncate_head(msg, MAX_SESSION_NAME_BYTES);
                    }
                }
            }
            return lines;
        }
        if self.session_switching && method == "select" {
            self.busy = true;
            self.status = "Choose a session".to_string();
        }
        let id = value
            .get("id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if id.is_empty() {
            return lines;
        }
        // Fail-closed on duplicate ids: never replace the displayed payload
        // under an id. The first request wins; a reused id (stale Pi
        // generation or hostile child) must not swap approval content under
        // the visible dialog. Stale reuse across restarts is already fenced
        // by reject_all_requests() on exit, which clears the whole map.
        if self.pending_requests.contains_key(&id) {
            return lines;
        }
        let timeout_ms = value
            .get("timeout")
            .and_then(|v| v.as_u64())
            .unwrap_or(APPROVAL_DEFAULT_MS)
            .min(APPROVAL_MAX_MS);
        let deadline = Instant::now() + std::time::Duration::from_millis(timeout_ms);
        // Bound the approval queue sensibly. Overflow is TOLD, not silent:
        // answer immediately with the same cancelled line expire_approvals /
        // reject_all_requests produce so the child's tool call never hangs.
        // The refused id is never inserted, so expiry can never touch it.
        if self.pending_requests.len() >= 32 {
            lines.push(json!({"type": "extension_ui_response", "id": id, "cancelled": true}));
            self.push_diagnostic(&format!("approval queue full; cancelled {id}"));
            return lines;
        }
        self.pending_requests.insert(
            id,
            PendingReq {
                request: value.clone(),
                deadline,
                surfaced: false,
                deferred: false,
            },
        );
        if self.pending_approval.is_none() {
            self.pending_approval = Some(value.clone());
            events.push(UiEvent::new("uiRequest", vec![value.clone()]));
        }
        lines
    }

    /// Mark a request as surfaced: a UI opened a dialog for it (S-048).
    /// Returns false for unknown ids. A surfaced request never expires.
    pub fn mark_surfaced(&mut self, request_id: &str, events: &mut Vec<UiEvent>) -> bool {
        let Some(entry) = self.pending_requests.get_mut(request_id) else {
            return false;
        };
        entry.surfaced = true;
        self.refresh_pending_approval(events);
        true
    }

    /// Park a request via an explicit defer (S-048): it is skipped while
    /// other non-deferred requests remain and re-surfaces when the round
    /// reaches it. Returns false for unknown ids. A deferred request never
    /// expires; it is cancelled only by explicit Reject/Stop/exit.
    pub fn defer_request(&mut self, request_id: &str, events: &mut Vec<UiEvent>) -> bool {
        let Some(entry) = self.pending_requests.get_mut(request_id) else {
            return false;
        };
        // Defer implies the request was visible, hence surfaced.
        entry.surfaced = true;
        entry.deferred = true;
        self.refresh_pending_approval(events);
        true
    }

    pub fn deferred_count(&self) -> usize {
        self.pending_requests.values().filter(|p| p.deferred).count()
    }

    pub fn snapshot(&self) -> Value {
        let messages: Vec<Value> = self.messages.iter().map(history_msg_to_value).collect();
        let pending_requests: BTreeMap<String, Value> = self
            .pending_requests
            .iter()
            .map(|(k, v)| (k.clone(), v.request.clone()))
            .collect();
        json!({
            "sessionFile": self.session_file,
            "sessionName": self.session_name,
            "freshSession": self.fresh_session,
            "projectId": self.project_id,
            "projectPath": self.project_path,
            "journalMode": self.journal_mode,
            "scopedMode": self.scoped_mode(),
            "model": self.model,
            "models": self.models,
            "commands": self.commands,
            "stateOk": self.state_ok,
            "extensionOk": self.extension_ok,
            "ready": self.ready(),
            "busy": self.busy,
            "compacting": self.compacting,
            "sessionSwitching": self.session_switching,
            "sessionRefreshPending": self.session_refresh_pending,
            "sessionRefreshFailed": self.session_refresh_failed,
            "sessionRefreshGeneration": self.session_refresh_generation,
            "sessionChangeInFlight": self.session_change_in_flight,
            "sessionChangeKind": self.session_change_kind,
            "sessionChangeRequestId": self.session_change_request_id,
            "sessionChangeCancelRequested": self.session_change_cancel_requested,
            "answer": self.answer,
            "messages": messages,
            "messagesSessionFile": self.messages_session_file,
            "messagesGeneration": self.messages_generation,
            "messagesAwaitingSessionState": self.messages_awaiting_session_state,
            "messagesTruncated": self.messages_truncated,
            "status": self.status,
            "pendingApproval": self.pending_approval.clone().unwrap_or(Value::Null),
            "pendingRequests": pending_requests,
            "deferredCount": self.deferred_count(),
            "serial": self.serial,
            "generation": self.generation,
            "lastRequestId": self.last_request_id,
            "processStarted": self.process_started,
            "processStartFailed": self.process_start_failed,
            "startRequested": self.start_requested,
            "launchAttempted": self.launch_attempted,
            "idleStopped": self.idle_stopped,
            "pendingResume": self.pending_resume,
            "desiredRunning": self.desired_running,
            "retryable": self.retryable(),
            "idleStopping": self.idle_stopping,
            "stopping": self.stopping,
            "controlPending": self.control_pending,
            "diagnostic": self.diagnostic,
            "statsText": self.stats_text,
        })
    }

    // -- Pi event dispatcher (legacy handle parity) --
    // Returns lines to write to the Pi child stdin.
    pub fn handle_pi_value(&mut self, value: &Value, events: &mut Vec<UiEvent>) -> Vec<Value> {
        let mut writes: Vec<Value> = Vec::new();
        // All modes fence Pi frames while not intentionally running: a stale
        // generation or an idle/paused worker never mutates state.
        if !self.desired_running || self.idle_stopping || self.idle_stopped || !self.process_started
        {
            return writes;
        }
        let vtype = value.get("type").and_then(|v| v.as_str()).unwrap_or("");
        if vtype == "response" {
            self.handle_pi_response(value, events, &mut writes);
            return writes;
        }
        if vtype == "message_update" {
            let is_delta = value
                .get("assistantMessageEvent")
                .and_then(|e| e.get("type"))
                .and_then(|v| v.as_str())
                == Some("text_delta");
            if is_delta {
                if self.stopping {
                    return writes;
                }
                let delta = value
                    .get("assistantMessageEvent")
                    .and_then(|e| e.get("delta"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                self.push_answer_delta(delta);
                events.push(UiEvent::new("textDelta", vec![json!(delta)]));
            }
            return writes;
        }
        if vtype == "message_end" {
            if self.stopping {
                return writes;
            }
            if let Some(msg) = value.get("message") {
                if msg.get("role").and_then(|v| v.as_str()) == Some("assistant") {
                    let text = text_from_message(msg);
                    if !text.is_empty() {
                        self.set_answer(text);
                    }
                }
            }
            return writes;
        }
        if vtype == "agent_start" {
            if self.stopping {
                return writes;
            }
            self.busy = true;
            return writes;
        }
        if vtype == "agent_end" {
            if self.stopping {
                return writes;
            }
            self.status = "Finishing…".to_string();
            return writes;
        }
        if vtype == "agent_settled" {
            self.busy = false;
            self.stopping = false;
            self.compacting = false;
            self.fresh_session = false;
            self.status = "Ready".to_string();
            events.push(UiEvent::new("finished", vec![]));
            // Settled refresh is always untracked (legacy parity).
            // A tagged get_state here would mark the current refresh
            // generation authoritative before the actual new/restore ACK, so
            // a Stop + old-state reply could release the gate early.
            writes.push(track_simple(
                &mut self.request_kinds,
                &mut self.serial,
                "get_state",
            ));
            writes.push(self.queue_get_messages_tracked());
            writes.push(track_simple(
                &mut self.request_kinds,
                &mut self.serial,
                "get_last_assistant_text",
            ));
            writes.push(track_simple(
                &mut self.request_kinds,
                &mut self.serial,
                "get_commands",
            ));
            return writes;
        }
        if vtype == "compaction_start" {
            self.compacting = true;
            self.status = "Compacting…".to_string();
            return writes;
        }
        if vtype == "compaction_end" {
            self.compacting = false;
            writes.push(track_simple(
                &mut self.request_kinds,
                &mut self.serial,
                "get_state",
            ));
            return writes;
        }
        if vtype == "extension_error" {
            self.busy = false;
            self.compacting = false;
            let msg = value
                .get("error")
                .and_then(|v| v.as_str())
                .unwrap_or("Extension error")
                .to_string();
            if self.session_refresh_pending {
                self.fail_session_refresh(&msg.clone(), false, events);
            } else {
                if self.session_switching {
                    self.finish_session_switch();
                }
                events.push(UiEvent::new("failed", vec![json!(msg)]));
                writes.push(track_simple(
                    &mut self.request_kinds,
                    &mut self.serial,
                    "get_state",
                ));
            }
            return writes;
        }
        if vtype == "extension_ui_request" {
            let mut out = self.handle_ui_request(value, events);
            writes.append(&mut out);
            return writes;
        }
        writes
    }

    fn handle_pi_response(
        &mut self,
        value: &Value,
        events: &mut Vec<UiEvent>,
        writes: &mut Vec<Value>,
    ) {
        let id = value
            .get("id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let command_field = value
            .get("command")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        // Peek first: validate tracked kind + change identity BEFORE consuming.
        // A foreign frame reusing a known id with a mismatched command must be
        // ignored without consuming the genuine entry, otherwise the real
        // new/rename/restore ACK arriving later would be treated as unknown.
        let peeked: Option<RequestKind> = if id.is_empty() {
            None
        } else {
            self.request_kinds.get(&id).cloned()
        };
        // Strict correlation: when we know the request kind, value.command
        // must either be empty or agree. Mismatch => ignore, keep the entry.
        if let Some(r) = &peeked {
            if !command_field.is_empty() && command_field != r.kind {
                return;
            }
        }
        // Prefer tracked kind over the untrusted command field. Unknown ids
        // fall back to the field for read-only queries; control commands
        // below reject unknown ids explicitly.
        let command_name = if let Some(r) = &peeked {
            r.kind.clone()
        } else if !command_field.is_empty() {
            command_field.clone()
        } else {
            String::new()
        };
        // Session-changing ACKs require a known, matching request. This
        // rejects foreign replies that carry value.command alone.
        let is_session_control = matches!(
            command_name.as_str(),
            "new_session" | "set_session_name" | "prompt"
        );
        if is_session_control && peeked.is_none() {
            return;
        }
        if self.session_change_in_flight && is_session_control {
            if id != self.session_change_request_id {
                // Stale/foreign new/rename/restore must retain gates until
                // the actual changing command ACK + authoritative state.
                // Keep the peeked entry for its genuine owner.
                return;
            }
            // Kind must match the inflight change (new/rename/restore).
            let kind_ok = match self.session_change_kind.as_str() {
                "new" => command_name == "new_session",
                "rename" => command_name == "set_session_name",
                "restore" => {
                    command_name == "prompt"
                        && peeked.as_ref().and_then(|r| r.message.as_deref())
                            == Some("/desktop-sessions")
                }
                _ => true,
            };
            if !kind_ok {
                return;
            }
        }
        // Validation passed: now consume the matching entry.
        let info: Option<RequestKind> = if id.is_empty() {
            None
        } else {
            self.request_kinds.remove(&id)
        };
        let authoritative = self.is_authoritative_state(info.as_ref());
        let success = value
            .get("success")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let error_msg = || {
            value
                .get("error")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string())
                .or_else(|| {
                    value
                        .get("data")
                        .and_then(|d| d.get("error"))
                        .and_then(|v| v.as_str())
                        .map(|s| s.to_string())
                })
                .unwrap_or_else(|| format!("RPC {command_name} failed"))
        };
        if !success {
            if command_name == "get_messages" {
                let current = match &info {
                    Some(r) if r.kind == "get_messages" => {
                        !self.session_refresh_failed
                            && !self.messages_awaiting_session_state
                            && r.generation == Some(self.messages_generation)
                            && !self.session_file.is_empty()
                            && r.session_file.as_deref() == Some(self.session_file.as_str())
                    }
                    _ => false,
                };
                if !current {
                    return;
                }
                let msg = error_msg();
                if msg.is_empty() {
                    events.push(UiEvent::new(
                        "historyFailed",
                        vec![
                            json!("Session history could not be loaded"),
                            json!(self.session_file.clone()),
                            json!(self.messages_generation),
                        ],
                    ));
                } else {
                    events.push(UiEvent::new(
                        "historyFailed",
                        vec![
                            json!(msg.clone()),
                            json!(self.session_file.clone()),
                            json!(self.messages_generation),
                        ],
                    ));
                }
                if !self.journal_mode {
                    events.push(UiEvent::new("failed", vec![json!(msg)]));
                    writes.push(track_simple(
                        &mut self.request_kinds,
                        &mut self.serial,
                        "get_state",
                    ));
                }
                return;
            }
            if command_name == "get_state" && self.session_refresh_failed {
                return;
            }
            if command_name == "get_state" && self.session_refresh_pending && !authoritative {
                return;
            }
            if self.session_refresh_pending
                && (authoritative || self.is_matching_change_failure(&id, info.as_ref()))
            {
                self.settle_session_change();
                let msg = error_msg();
                let label = if self.journal_mode {
                    "Authoritative journal session refresh failed"
                } else if !self.project_id.is_empty() || !self.project_path.is_empty() {
                    "Authoritative project session refresh failed"
                } else {
                    "Authoritative session refresh failed"
                };
                let final_msg = if msg.is_empty() {
                    label.to_string()
                } else {
                    msg
                };
                self.fail_session_refresh(&final_msg.clone(), true, events);
                return;
            }
            if command_name == "compact" {
                self.compacting = false;
            }
            if command_name == "prompt" {
                self.busy = false;
            }
            if command_name == "new_session" {
                self.fresh_session = false;
            }
            if self.session_switching {
                self.finish_session_switch();
            }
            if ["new_session", "set_model", "set_session_name", "compact"]
                .contains(&command_name.as_str())
            {
                self.control_pending = false;
            }
            events.push(UiEvent::new("failed", vec![json!(error_msg())]));
            if command_name != "get_state" {
                writes.push(track_simple(
                    &mut self.request_kinds,
                    &mut self.serial,
                    "get_state",
                ));
            }
            return;
        }
        match command_name.as_str() {
            "get_state" => {
                if self.session_refresh_failed {
                    return;
                }
                if self.session_refresh_pending && !authoritative {
                    return;
                }
                let data = value.get("data").cloned().unwrap_or(json!({}));
                let raw_session_file = data
                    .get("sessionFile")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let raw_session_name = data
                    .get("sessionName")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                // Never silently truncate session identity: no real path exceeds
                // 4096 bytes, so an over-long value is invalid, not a prefix to
                // install. Authoritative invalid fails the refresh; anything
                // else is ignored without fabricating a path.
                if raw_session_file.len() > MAX_SESSION_FILE_BYTES
                    || raw_session_name.len() > MAX_SESSION_NAME_BYTES
                {
                    if authoritative {
                        self.settle_session_change();
                        self.fail_session_refresh(
                            "Session identity was unavailable; retry",
                            true,
                            events,
                        );
                    }
                    return;
                }
                let next_session = raw_session_file.to_string();
                if authoritative && next_session.is_empty() {
                    self.settle_session_change();
                    self.fail_session_refresh(
                        "Session identity was unavailable; retry",
                        true,
                        events,
                    );
                    return;
                }
                let changed = !self.session_file.is_empty()
                    && !next_session.is_empty()
                    && self.session_file != next_session;
                let awaiting = self.messages_awaiting_session_state;
                if changed || awaiting {
                    self.clear_messages(&next_session);
                }
                self.session_file = next_session;
                self.messages_awaiting_session_state = false;
                self.session_name = raw_session_name.to_string();
                self.model = data
                    .get("model")
                    .cloned()
                    .map(Self::bound_model)
                    .unwrap_or(Value::Null);
                self.busy = if self.session_switching {
                    true
                } else {
                    data.get("isStreaming")
                        .and_then(|v| v.as_bool())
                        .unwrap_or(false)
                };
                self.compacting = data
                    .get("isCompacting")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                if !self.busy && !self.compacting {
                    self.stopping = false;
                }
                self.state_ok = true;
                if authoritative {
                    self.session_refresh_pending = false;
                    self.session_refresh_failed = false;
                    self.settle_session_change();
                    if self.session_switching {
                        self.finish_session_switch();
                    }
                }
                if !self.session_switching {
                    self.status = self.ready_status();
                }
                events.push(UiEvent::new("stateUpdated", vec![]));
                if changed || awaiting {
                    writes.push(self.queue_get_messages_tracked());
                }
            }
            "get_messages" => {
                let (sess, gen, has_gen) = match &info {
                    Some(r) => (
                        r.session_file.clone().unwrap_or_default(),
                        r.generation.unwrap_or(0),
                        r.generation.is_some(),
                    ),
                    None => (String::new(), 0, false),
                };
                let msgs = value
                    .get("data")
                    .and_then(|d| d.get("messages"))
                    .cloned()
                    .unwrap_or(json!([]));
                // historyLoaded/historyFailed emit for every mode (palette
                // included) once identity + generation correlate.
                if self.apply_messages(&msgs, &sess, gen, has_gen) {
                    events.push(UiEvent::new(
                        "historyLoaded",
                        vec![
                            json!(self.messages_session_file.clone()),
                            json!(self.messages_generation),
                        ],
                    ));
                }
            }
            "get_commands" => {
                let cmds = value
                    .get("data")
                    .and_then(|d| d.get("commands"))
                    .and_then(|v| v.as_array())
                    .cloned()
                    .unwrap_or_default();
                self.commands =
                    Self::bound_value_array(cmds, MAX_COMMANDS_ITEMS, MAX_COMMANDS_BYTES);
                self.extension_ok = self
                    .commands
                    .iter()
                    .any(|c| c.get("name").and_then(|v| v.as_str()) == Some("desktop-sessions"));
                if !self.session_switching {
                    self.status = if self.extension_ok {
                        self.ready_status()
                    } else {
                        "Safety extension unavailable; prompts disabled".to_string()
                    };
                }
            }
            "get_available_models" => {
                let models = value
                    .get("data")
                    .and_then(|d| d.get("models"))
                    .and_then(|v| v.as_array())
                    .cloned()
                    .unwrap_or_default();
                self.models = Self::bound_value_array(models, MAX_MODELS_ITEMS, MAX_MODELS_BYTES);
            }
            "get_session_stats" => {
                let data = value.get("data").cloned().unwrap_or(json!({}));
                let text = serde_json::to_string_pretty(&data).unwrap_or_default();
                self.stats_text = Self::bound_stats_text(&text);
                events.push(UiEvent::new("statsChanged", vec![data]));
            }
            "get_last_assistant_text" => {
                let data = value.get("data").cloned().unwrap_or(json!({}));
                let text = data
                    .get("text")
                    .and_then(|v| v.as_str())
                    .or_else(|| data.get("message").and_then(|v| v.as_str()))
                    .unwrap_or("");
                self.set_answer_str(text);
            }
            "new_session" | "set_model" | "set_session_name" => {
                // Control replies without correlation are foreign (value.command
                // alone) and must not settle gates.
                if info.is_none() {
                    return;
                }
                self.busy = false;
                self.control_pending = false;
                if command_name == "new_session" {
                    let cancelled = value
                        .get("data")
                        .and_then(|d| d.get("cancelled"))
                        .and_then(|v| v.as_bool())
                        .unwrap_or(false);
                    if cancelled {
                        self.fresh_session = false;
                        self.status =
                            "New session cancelled; restoring previous session…".to_string();
                    } else {
                        self.clear_messages("");
                    }
                }
                let (w1, w2, w3, w4, w5) = self.refresh_after(&command_name, true, &id);
                writes.push(w1);
                writes.push(w2);
                writes.push(w3);
                writes.push(w4);
                writes.push(w5);
                if command_name == "new_session" {
                    self.answer.clear();
                }
            }
            "compact" => {
                if info.is_none() {
                    return;
                }
                self.compacting = false;
                self.control_pending = false;
                let (w1, w2, w3, w4, w5) = self.refresh_after(&command_name, true, &id);
                writes.push(w1);
                writes.push(w2);
                writes.push(w3);
                writes.push(w4);
                writes.push(w5);
            }
            "prompt" => {
                let prompt_message = info
                    .as_ref()
                    .and_then(|r| r.message.clone())
                    .unwrap_or_default();
                if prompt_message == "/desktop-sessions" {
                    // Restore gates for all modes: never release before the
                    // picker ACK + authoritative state.
                    if !self.session_refresh_pending {
                        self.begin_session_refresh();
                    }
                    if !self.session_change_in_flight {
                        self.begin_session_change("restore", &id);
                    }
                    self.clear_messages("");
                }
                let include_last = prompt_message == "/desktop-sessions";
                let (w1, w2, w3, w4, w5) = self.refresh_after(&command_name, include_last, &id);
                writes.push(w1);
                writes.push(w2);
                writes.push(w3);
                writes.push(w4);
                writes.push(w5);
            }
            "abort" => {
                writes.push(track_simple(
                    &mut self.request_kinds,
                    &mut self.serial,
                    "get_state",
                ));
            }
            _ => {}
        }
    }

    fn queue_get_state_tracked(&mut self) -> Value {
        let id = self.next_rpc_id();
        if self.session_refresh_pending {
            self.request_kinds.insert(
                id.clone(),
                RequestKind {
                    kind: "get_state".to_string(),
                    message: None,
                    session_file: None,
                    generation: None,
                    refresh_generation: Some(self.session_refresh_generation),
                },
            );
        } else {
            self.request_kinds
                .insert(id.clone(), RequestKind::simple("get_state"));
        }
        json!({"type": "get_state", "id": id})
    }

    fn queue_get_messages_tracked(&mut self) -> Value {
        let id = self.next_rpc_id();
        self.request_kinds.insert(
            id.clone(),
            RequestKind {
                kind: "get_messages".to_string(),
                message: None,
                session_file: Some(self.session_file.clone()),
                generation: Some(self.messages_generation),
                refresh_generation: None,
            },
        );
        json!({"type": "get_messages", "id": id})
    }

    fn refresh_after(
        &mut self,
        command_name: &str,
        include_last: bool,
        ack_id: &str,
    ) -> (Value, Value, Value, Value, Value) {
        // Only the matching sessionChangeRequestId ACK authorizes a tagged
        // (authoritative) state refresh. Unsolicited/settled refreshes stay
        // untagged so old state cannot release the gate. Startup (no
        // inflight change) is separately allowed and stays tagged.
        let authorizes = self.refresh_authorizes(ack_id);
        let w1 = if authorizes {
            self.queue_get_state_tracked()
        } else {
            track_simple(&mut self.request_kinds, &mut self.serial, "get_state")
        };
        let w2 = self.queue_get_messages_tracked();
        let w3 = track_simple(&mut self.request_kinds, &mut self.serial, "get_commands");
        let w4 = track_simple(
            &mut self.request_kinds,
            &mut self.serial,
            "get_session_stats",
        );
        let w5 = if include_last {
            track_simple(
                &mut self.request_kinds,
                &mut self.serial,
                "get_last_assistant_text",
            )
        } else {
            json!(null)
        };
        let _ = command_name;
        (w1, w2, w3, w4, w5)
    }

    /// Startup RPC burst after the Pi child reports started. All modes defer
    /// history until the authoritative get_state: early get_messages frames
    /// are fenced and rerequested once identity is known.
    pub fn start_ready_writes(&mut self) -> Vec<Value> {
        self.state_ok = false;
        self.extension_ok = false;
        self.stopping = false;
        self.mark_session_refresh();
        self.messages_awaiting_session_state = true;
        self.clear_messages("");
        let mut out = Vec::new();
        out.push(self.queue_get_state_tracked());
        out.push(track_simple(
            &mut self.request_kinds,
            &mut self.serial,
            "get_commands",
        ));
        out.push(track_simple(
            &mut self.request_kinds,
            &mut self.serial,
            "get_available_models",
        ));
        out.push(track_simple(
            &mut self.request_kinds,
            &mut self.serial,
            "get_session_stats",
        ));
        out.push(track_simple(
            &mut self.request_kinds,
            &mut self.serial,
            "get_last_assistant_text",
        ));
        out.push(self.queue_get_messages_tracked());
        out
    }
}

fn track_simple(kinds: &mut BTreeMap<String, RequestKind>, serial: &mut u64, kind: &str) -> Value {
    *serial += 1;
    let id = format!("qs-{}", *serial);
    kinds.insert(id.clone(), RequestKind::simple(kind));
    json!({"type": kind, "id": id})
}

/// Normalize UI prompt text (trim, legacy parity).
#[allow(dead_code)]
pub fn clean_text(v: Option<&Value>) -> String {
    v.and_then(|x| x.as_str()).unwrap_or("").trim().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn journal_state() -> AgentState {
        let mut s = AgentState::new(String::new(), true, String::new(), String::new(), false);
        // Simulate a live, ready child.
        s.process_started = true;
        s.desired_running = true;
        s.state_ok = true;
        s.extension_ok = true;
        s.session_file = "/scope/a.jsonl".to_string();
        s.status = "Ready".to_string();
        s
    }

    #[test]
    fn stale_get_state_does_not_release_refresh_gate() {
        let mut s = journal_state();
        s.begin_session_refresh();
        let gen = s.session_refresh_generation;
        // Stale (untagged) get_state must be ignored.
        s.request_kinds
            .insert("old".to_string(), RequestKind::simple("get_state"));
        let mut events = Vec::new();
        let mut writes = Vec::new();
        s.handle_pi_response(
            &json!({"type":"response","id":"old","command":"get_state","success":true,
                "data":{"sessionFile":"/scope/a.jsonl","sessionName":"A"}}),
            &mut events,
            &mut writes,
        );
        assert!(s.session_refresh_pending);
        assert_eq!(s.session_file, "/scope/a.jsonl");
        // Authoritative tagged state releases the gate and installs identity.
        let auth_id = "auth".to_string();
        s.request_kinds.insert(
            auth_id.clone(),
            RequestKind {
                kind: "get_state".to_string(),
                message: None,
                session_file: None,
                generation: None,
                refresh_generation: Some(gen),
            },
        );
        s.handle_pi_response(
            &json!({"type":"response","id":"auth","command":"get_state","success":true,
                "data":{"sessionFile":"/scope/b.jsonl","sessionName":"B"}}),
            &mut events,
            &mut writes,
        );
        assert!(!s.session_refresh_pending);
        assert_eq!(s.session_file, "/scope/b.jsonl");
        assert_eq!(s.session_name, "B");
    }

    #[test]
    fn stale_history_success_and_failure_are_fenced() {
        let mut s = journal_state();
        s.messages_generation = 4;
        s.messages_session_file = s.session_file.clone();
        // Current failure uses the dedicated signal, never generic failed.
        s.request_kinds.insert(
            "cur".to_string(),
            RequestKind {
                kind: "get_messages".to_string(),
                message: None,
                session_file: Some(s.session_file.clone()),
                generation: Some(4),
                refresh_generation: None,
            },
        );
        let mut events = Vec::new();
        let mut writes = Vec::new();
        s.handle_pi_response(
            &json!({"type":"response","id":"cur","command":"get_messages","success":false,"error":"down"}),
            &mut events,
            &mut writes,
        );
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].name, "historyFailed");
        // Stale generation failure is ignored entirely.
        s.request_kinds.insert(
            "old".to_string(),
            RequestKind {
                kind: "get_messages".to_string(),
                message: None,
                session_file: Some(s.session_file.clone()),
                generation: Some(3),
                refresh_generation: None,
            },
        );
        let before = events.len();
        s.handle_pi_response(
            &json!({"type":"response","id":"old","command":"get_messages","success":false,"error":"old"}),
            &mut events,
            &mut writes,
        );
        assert_eq!(events.len(), before);
        // Empty current success emits historyLoaded with the same identity.
        s.request_kinds.insert(
            "empty".to_string(),
            RequestKind {
                kind: "get_messages".to_string(),
                message: None,
                session_file: Some(s.session_file.clone()),
                generation: Some(4),
                refresh_generation: None,
            },
        );
        s.handle_pi_response(
            &json!({"type":"response","id":"empty","command":"get_messages","success":true,"data":{"messages":[]}}),
            &mut events,
            &mut writes,
        );
        assert_eq!(events.last().unwrap().name, "historyLoaded");
        assert!(s.messages.is_empty());
    }

    #[test]
    fn approval_timeout_caps_at_120s_and_unknown_reply_ignored() {
        let mut s = journal_state();
        let mut events = Vec::new();
        // Timeout above the cap is clamped; the request is queued.
        s.handle_ui_request(
            &json!({"type":"extension_ui_request","id":"r1","method":"confirm","timeout":999999999}),
            &mut events,
        );
        assert!(s.pending_approval.is_some());
        assert_eq!(events.last().unwrap().name, "uiRequest");
        // Unknown request ids never release gates.
        assert!(!s.pending_requests.contains_key("nope"));
        assert!(s.pending_requests.contains_key("r1"));
    }

    #[test]
    fn history_normalization_skips_non_text() {
        let raw = json!([
            {"role":"user","content":"hello"},
            {"role":"assistant","content":[{"type":"thinking","thinking":"secret"},{"type":"text","text":"done"}]},
            {"role":"toolResult","content":[{"type":"text","text":"tool"}]},
            {"role":"user","content":"   "}
        ]);
        let msgs = normalize_messages(&raw);
        assert_eq!(msgs.len(), 2);
        assert_eq!(msgs[1].text, "done");
    }

    // -- HIGH: session-change gating --

    fn begin_new_flow(s: &mut AgentState) -> String {
        // Mirror Bridge::op_new_session without transport.
        s.control_pending = true;
        s.fresh_session = true;
        s.session_file.clear();
        s.begin_session_refresh();
        let id = s.next_rpc_id();
        s.request_kinds
            .insert(id.clone(), RequestKind::simple("new_session"));
        s.begin_session_change("new", &id);
        id
    }

    fn begin_rename_flow(s: &mut AgentState) -> String {
        s.control_pending = true;
        s.mark_session_refresh();
        let id = s.next_rpc_id();
        s.request_kinds
            .insert(id.clone(), RequestKind::simple("set_session_name"));
        s.begin_session_change("rename", &id);
        id
    }

    fn begin_restore_flow(s: &mut AgentState) -> String {
        // Mirror Bridge::op_switch_session without transport.
        s.answer.clear();
        s.busy = true;
        s.status = "Thinking…".to_string();
        let picker_id = s.next_rpc_id();
        s.request_kinds.insert(
            picker_id.clone(),
            RequestKind {
                kind: "prompt".to_string(),
                message: Some("/desktop-sessions".to_string()),
                session_file: None,
                generation: None,
                refresh_generation: None,
            },
        );
        s.last_request_id = picker_id.clone();
        s.begin_session_refresh();
        s.begin_session_change("restore", &s.last_request_id.clone());
        s.session_switching = true;
        s.busy = true;
        s.status = "Loading sessions…".to_string();
        picker_id
    }

    fn settled_get_state_id(writes: &[Value]) -> String {
        writes
            .iter()
            .find(|w| w.get("type").and_then(|v| v.as_str()) == Some("get_state"))
            .and_then(|w| w.get("id"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string()
    }

    #[test]
    fn new_settled_old_state_retains_gate_until_ack() {
        let mut s = journal_state();
        let change_id = begin_new_flow(&mut s);
        let gen = s.session_refresh_generation;
        assert!(s.session_refresh_pending);
        assert!(s.session_change_in_flight);

        // agent_settled must queue an UNTRACKED get_state (legacy parity).
        let mut events = Vec::new();
        let writes = s.handle_pi_value(&json!({"type":"agent_settled"}), &mut events);
        let settled_id = settled_get_state_id(&writes);
        assert!(!settled_id.is_empty());
        let info = s
            .request_kinds
            .get(&settled_id)
            .expect("settled get_state tracked");
        assert_eq!(info.kind, "get_state");
        assert_eq!(
            info.refresh_generation, None,
            "settled refresh must stay untagged during session change"
        );

        // Old-state reply to the settled poll must be ignored; gates retained.
        let mut writes2 = Vec::new();
        s.handle_pi_response(
            &json!({"type":"response","id":settled_id,"command":"get_state","success":true,
                "data":{"sessionFile":"/scope/a.jsonl","sessionName":"A"}}),
            &mut events,
            &mut writes2,
        );
        assert!(
            s.session_refresh_pending,
            "gate must survive settled old state"
        );
        assert!(s.session_change_in_flight);
        assert!(s.control_pending);
        assert!(
            s.session_file.is_empty(),
            "new flow cleared file; old must not restore"
        );

        // Foreign reply via value.command alone must not settle.
        let mut writes_f = Vec::new();
        let before_gen = s.session_refresh_generation;
        s.handle_pi_response(
            &json!({"type":"response","id":"foreign-1","command":"new_session","success":true,
                "data":{}}),
            &mut events,
            &mut writes_f,
        );
        assert!(s.session_refresh_pending);
        assert!(s.session_change_in_flight);
        assert_eq!(s.session_refresh_generation, before_gen);

        // Mismatched known-id + foreign command must not settle either.
        // The peeked entry must be preserved for its genuine owner.
        s.request_kinds.insert(
            "qs-other".to_string(),
            RequestKind {
                kind: "prompt".to_string(),
                message: Some("hello".to_string()),
                session_file: None,
                generation: None,
                refresh_generation: None,
            },
        );
        s.handle_pi_response(
            &json!({"type":"response","id":"qs-other","command":"new_session","success":true,
                "data":{}}),
            &mut events,
            &mut writes_f,
        );
        assert!(s.session_refresh_pending);
        assert!(s.session_change_in_flight);
        assert!(
            s.request_kinds.contains_key("qs-other"),
            "mismatched frame must not consume the tracked entry"
        );

        // Actual changing-command ACK authorizes a TAGGED refresh.
        let mut writes3 = Vec::new();
        s.handle_pi_response(
            &json!({"type":"response","id":change_id,"command":"new_session","success":true,
                "data":{}}),
            &mut events,
            &mut writes3,
        );
        // ACK itself does not release; it queues the authoritative poll.
        assert!(s.session_refresh_pending);
        assert!(s.session_change_in_flight);
        let auth_id = settled_get_state_id(&writes3);
        assert!(!auth_id.is_empty());
        let auth_info = s.request_kinds.get(&auth_id).expect("ack refresh tracked");
        assert_eq!(auth_info.refresh_generation, Some(gen));

        // Authoritative state with the new identity releases the gate.
        let mut writes4 = Vec::new();
        s.handle_pi_response(
            &json!({"type":"response","id":auth_id,"command":"get_state","success":true,
                "data":{"sessionFile":"/scope/b.jsonl","sessionName":"B"}}),
            &mut events,
            &mut writes4,
        );
        assert!(!s.session_refresh_pending);
        assert!(!s.session_change_in_flight);
        assert!(!s.control_pending);
        assert_eq!(s.session_file, "/scope/b.jsonl");
        assert_eq!(s.session_name, "B");
    }

    #[test]
    fn restore_settled_old_state_retains_gate_until_ack() {
        let mut s = journal_state();
        let picker_id = begin_restore_flow(&mut s);
        let gen = s.session_refresh_generation;

        let mut events = Vec::new();
        let writes = s.handle_pi_value(&json!({"type":"agent_settled"}), &mut events);
        let settled_id = settled_get_state_id(&writes);
        let info = s.request_kinds.get(&settled_id).expect("settled tracked");
        assert_eq!(info.refresh_generation, None);

        // Old state ignored.
        let mut w2 = Vec::new();
        s.handle_pi_response(
            &json!({"type":"response","id":settled_id,"command":"get_state","success":true,
                "data":{"sessionFile":"/scope/a.jsonl"}}),
            &mut events,
            &mut w2,
        );
        assert!(s.session_refresh_pending);
        assert!(s.session_change_in_flight);
        assert_eq!(s.session_change_request_id, picker_id);

        // Non-picker prompt with same session-change id shape but wrong
        // message must not authorize (extension valid picker matching).
        s.request_kinds.insert(
            "qs-plain".to_string(),
            RequestKind {
                kind: "prompt".to_string(),
                message: Some("hello".to_string()),
                session_file: None,
                generation: None,
                refresh_generation: None,
            },
        );
        let mut w_plain = Vec::new();
        // Directly exercise kind check: a plain prompt ACK while restore is
        // inflight with a different id must be ignored for gating. It still
        // processes as a generic prompt but its refresh stays untagged.
        s.handle_pi_response(
            &json!({"type":"response","id":"qs-plain","success":true,"data":{}}),
            &mut events,
            &mut w_plain,
        );
        assert!(s.session_refresh_pending);
        assert!(s.session_change_in_flight);
        // Its follow-up get_state must be untagged.
        let plain_state = settled_get_state_id(&w_plain);
        if !plain_state.is_empty() {
            assert_eq!(
                s.request_kinds
                    .get(&plain_state)
                    .and_then(|r| r.refresh_generation),
                None
            );
        }

        // Actual picker ACK authorizes.
        let mut w3 = Vec::new();
        s.handle_pi_response(
            &json!({"type":"response","id":picker_id,"command":"prompt","success":true,
                "data":{}}),
            &mut events,
            &mut w3,
        );
        assert!(s.session_refresh_pending);
        let auth_id = settled_get_state_id(&w3);
        assert_eq!(
            s.request_kinds
                .get(&auth_id)
                .and_then(|r| r.refresh_generation),
            Some(gen)
        );
        let mut w4 = Vec::new();
        s.handle_pi_response(
            &json!({"type":"response","id":auth_id,"command":"get_state","success":true,
                "data":{"sessionFile":"/scope/c.jsonl","sessionName":"C"}}),
            &mut events,
            &mut w4,
        );
        assert!(!s.session_refresh_pending);
        assert_eq!(s.session_file, "/scope/c.jsonl");
    }

    #[test]
    fn startup_retry_and_cancel_semantics_preserved() {
        // Startup: no inflight change, tagged poll is allowed.
        let mut s = AgentState::new(String::new(), true, String::new(), String::new(), false);
        s.process_started = true;
        s.desired_running = true;
        let writes = s.start_ready_writes();
        assert!(s.session_refresh_pending);
        assert!(!s.session_change_in_flight);
        let auth_id = writes
            .iter()
            .find(|w| w.get("type").and_then(|v| v.as_str()) == Some("get_state"))
            .and_then(|w| w.get("id"))
            .and_then(|v| v.as_str())
            .unwrap()
            .to_string();
        assert_eq!(
            s.request_kinds
                .get(&auth_id)
                .and_then(|r| r.refresh_generation),
            Some(s.session_refresh_generation)
        );
        let mut events = Vec::new();
        let mut w = Vec::new();
        s.handle_pi_response(
            &json!({"type":"response","id":auth_id,"command":"get_state","success":true,
                "data":{"sessionFile":"/scope/s.jsonl","sessionName":"S"}}),
            &mut events,
            &mut w,
        );
        assert!(!s.session_refresh_pending);
        assert_eq!(s.session_file, "/scope/s.jsonl");

        // Session cancel: fail without settled requests cancellation, not failure.
        let mut c = journal_state();
        c.begin_session_refresh();
        c.begin_session_change("new", "qs-9");
        c.process_started = true;
        let mut ev = Vec::new();
        let failed = c.fail_session_refresh("cancel?", false, &mut ev);
        assert!(!failed);
        assert!(c.session_change_cancel_requested);
        assert!(c.session_refresh_pending);
        assert!(ev.is_empty());
        // Settled cancel actually fails and is retryable.
        let failed2 = c.fail_session_refresh("gone", true, &mut ev);
        assert!(failed2);
        assert!(c.session_refresh_failed);
        assert_eq!(ev.last().unwrap().name, "failed");
        assert!(c.retryable());

        // New cancelled by the extension keeps restoring via authoritative state.
        let mut n = journal_state();
        let change_id = begin_new_flow(&mut n);
        let mut ev2 = Vec::new();
        let mut w2 = Vec::new();
        n.handle_pi_response(
            &json!({"type":"response","id":change_id,"command":"new_session","success":true,
                "data":{"cancelled":true}}),
            &mut ev2,
            &mut w2,
        );
        assert_eq!(
            n.status,
            "New session cancelled; restoring previous session…"
        );
        assert!(n.session_refresh_pending);
        assert!(n.session_change_in_flight);
    }

    // -- MEDIUM: UTF-8-safe truncation --

    #[test]
    fn multibyte_truncation_never_panics_and_never_splits() {
        use crate::history::{truncate_head, truncate_tail};
        // 64KiB history limit with 2-byte and 4-byte chars.
        for filler in ["é", "😀", "a"] {
            let big = filler.repeat(70 * 1024);
            let head = truncate_head(&big, MAX_MESSAGE_TEXT);
            assert!(head.len() <= MAX_MESSAGE_TEXT);
            assert!(head.is_char_boundary(head.len()));
            let tail = truncate_tail(&big, MAX_MESSAGE_TEXT);
            assert!(tail.len() <= MAX_MESSAGE_TEXT);
            assert!(tail.is_char_boundary(0));
        }
        // Answer 512KiB.
        let big_ans = "😀".repeat(200 * 1024);
        let mut s = journal_state();
        s.set_answer(big_ans.clone());
        assert!(s.answer.len() <= MAX_ANSWER_BYTES);
        s.answer.clear();
        s.push_answer_delta(&big_ans);
        assert!(s.answer.len() <= MAX_ANSWER_BYTES);
        // Diagnostic 2048.
        let big_diag = "é".repeat(5000);
        let mut d = journal_state();
        d.push_diagnostic(&big_diag);
        assert!(d.diagnostic.len() <= MAX_DIAGNOSTIC_BYTES);
        // Stats 64KiB.
        let big_stats = "😀".repeat(30 * 1024);
        let bounded = AgentState::bound_stats_text(&big_stats);
        assert!(bounded.len() <= MAX_STATS_BYTES);
        // Boundary-specific: limit lands inside a char.
        let s3 = "aé😀b";
        // "a"(1) + "é"(2) = 3 bytes; cut at 2 lands inside é.
        assert_eq!(truncate_head(s3, 2), "a");
        // Tail cut inside é as well.
        let tail = truncate_tail(s3, 2);
        assert!(tail.len() <= 2);
    }

    #[test]
    fn history_aggregate_budget_sets_truncated_flag() {
        let mut s = journal_state();
        // 20 * 64KiB = 1.28MiB > 512KiB budget.
        let msgs: Vec<Value> = (0..20)
            .map(|i| json!({"role":"user","content": "x".repeat(64*1024), "id": i}))
            .collect();
        let ok = s.apply_messages(
            &json!(msgs),
            &s.session_file.clone(),
            s.messages_generation,
            true,
        );
        // Non-scoped? journal is scoped, needs session match: session_file is
        // set and requested_session must equal it.
        assert!(ok);
        let total: usize = s.messages.iter().map(|m| m.text.len()).sum();
        assert!(total <= MAX_HISTORY_BYTES);
        assert!(s.messages_truncated);
        assert!(s.snapshot()["messagesTruncated"] == json!(true));
        // Small history is not flagged.
        let mut s2 = journal_state();
        let small = json!([{"role":"user","content":"hi"}]);
        assert!(s2.apply_messages(
            &small,
            &s2.session_file.clone(),
            s2.messages_generation,
            true
        ));
        assert!(!s2.messages_truncated);
        assert!(s2.snapshot()["messagesTruncated"] == json!(false));
    }

    #[test]
    fn untrusted_metadata_is_bounded() {
        let mut s = journal_state();
        // Commands: 1000 entries -> capped.
        let cmds: Vec<Value> = (0..1000)
            .map(|i| json!({"name": format!("c-{i}")}))
            .collect();
        s.request_kinds
            .insert("c1".to_string(), RequestKind::simple("get_commands"));
        let mut ev = Vec::new();
        let mut w = Vec::new();
        s.handle_pi_response(
            &json!({"type":"response","id":"c1","command":"get_commands","success":true,
                "data":{"commands": cmds}}),
            &mut ev,
            &mut w,
        );
        assert!(s.commands.len() <= MAX_COMMANDS_ITEMS);
        // Models similarly.
        let models: Vec<Value> = (0..1000).map(|i| json!({"id": format!("m-{i}")})).collect();
        s.request_kinds.insert(
            "m1".to_string(),
            RequestKind::simple("get_available_models"),
        );
        s.handle_pi_response(
            &json!({"type":"response","id":"m1","command":"get_available_models","success":true,
                "data":{"models": models}}),
            &mut ev,
            &mut w,
        );
        assert!(s.models.len() <= MAX_MODELS_ITEMS);
        // Over-long session identity is never installed silently: no real path
        // exceeds 4096 bytes. Authoritative invalid fails the refresh;
        // non-authoritative over-long is ignored without fabricating a path.
        // Transport owns event-serialization budgets, so the statsChanged event
        // still carries the full nested data; only state text is bounded.
        let huge_name = "n".repeat(10_000);
        let huge_file = "f".repeat(20_000);
        let huge_model = json!({"blob": "x".repeat(100_000)});
        // Authoritative (scoped refresh) over-long -> refresh fails.
        let mut auth = journal_state();
        auth.begin_session_refresh();
        let auth_gen = auth.session_refresh_generation;
        let auth_id = {
            let id = auth.next_rpc_id();
            auth.request_kinds.insert(
                id.clone(),
                RequestKind {
                    kind: "get_state".to_string(),
                    message: None,
                    session_file: None,
                    generation: None,
                    refresh_generation: Some(auth_gen),
                },
            );
            id
        };
        let mut ev_auth = Vec::new();
        let mut w_auth = Vec::new();
        let file_before = auth.session_file.clone();
        auth.handle_pi_response(
            &json!({"type":"response","id":auth_id,"command":"get_state","success":true,
                "data":{"sessionFile": huge_file, "sessionName": huge_name, "model": huge_model}}),
            &mut ev_auth,
            &mut w_auth,
        );
        assert!(
            auth.session_refresh_failed,
            "authoritative over-long must fail"
        );
        assert!(auth.session_file.is_empty(), "no fabricated path installed");
        assert_ne!(auth.session_file, huge_file[..100].to_string());
        let _ = file_before;
        // Non-authoritative (non-scoped, no gate) over-long -> ignored.
        let mut plain = AgentState::new(String::new(), false, String::new(), String::new(), false);
        plain.process_started = true;
        plain.desired_running = true;
        plain
            .request_kinds
            .insert("gs".to_string(), RequestKind::simple("get_state"));
        let mut ev2 = Vec::new();
        let mut w2 = Vec::new();
        plain.handle_pi_response(
            &json!({"type":"response","id":"gs","command":"get_state","success":true,
                "data":{"sessionFile": "f".repeat(20_000), "sessionName": "n".repeat(10_000),
                    "model": {"blob": "x".repeat(100_000)}}}),
            &mut ev2,
            &mut w2,
        );
        assert!(
            plain.session_file.is_empty(),
            "over-long identity must not be installed when non-authoritative"
        );
        assert_eq!(plain.model, Value::Null, "huge model still nulled");
    }

    #[test]
    fn same_id_mismatch_preserves_entry_for_real_ack() {
        // Foreign frame reusing the genuine change id with a mismatched
        // command must not consume the entry: the real ACK with the same id
        // must still reach ready.
        for flow in ["new", "rename", "restore"] {
            let mut s = journal_state();
            let (change_id, gen) = match flow {
                "new" => {
                    let id = begin_new_flow(&mut s);
                    (id, s.session_refresh_generation)
                }
                "rename" => {
                    let id = begin_rename_flow(&mut s);
                    (id, s.session_refresh_generation)
                }
                _ => {
                    let id = begin_restore_flow(&mut s);
                    (id, s.session_refresh_generation)
                }
            };
            let mut events = Vec::new();
            let mut writes = Vec::new();
            // Same-id foreign kind: tracked kind is new/prompt, field says
            // get_commands. Must be ignored AND preserve the entry.
            s.handle_pi_response(
                &json!({"type":"response","id":change_id,"command":"get_commands","success":true,
                    "data":{"commands":[]}}),
                &mut events,
                &mut writes,
            );
            assert!(
                s.session_refresh_pending,
                "{flow}: gate must survive same-id mismatch"
            );
            assert!(
                s.session_change_in_flight,
                "{flow}: change must survive same-id mismatch"
            );
            assert!(
                s.request_kinds.contains_key(&change_id),
                "{flow}: genuine entry must be preserved"
            );
            // Real ACK with the same id now authorizes and reaches ready.
            let ack_body = match flow {
                "restore" => json!({"type":"response","id":change_id,"command":"prompt",
                    "success":true,"data":{}}),
                "rename" => json!({"type":"response","id":change_id,"command":"set_session_name",
                    "success":true,"data":{}}),
                _ => json!({"type":"response","id":change_id,"command":"new_session",
                    "success":true,"data":{}}),
            };
            let mut w_ack = Vec::new();
            s.handle_pi_response(&ack_body, &mut events, &mut w_ack);
            assert!(
                s.session_refresh_pending,
                "{flow}: ACK queues poll, not release"
            );
            let auth_id = settled_get_state_id(&w_ack);
            assert!(!auth_id.is_empty(), "{flow}: ACK must queue get_state");
            assert_eq!(
                s.request_kinds
                    .get(&auth_id)
                    .and_then(|r| r.refresh_generation),
                Some(gen),
                "{flow}: ACK refresh must be authoritative"
            );
            let mut w_state = Vec::new();
            s.handle_pi_response(
                &json!({"type":"response","id":auth_id,"command":"get_state","success":true,
                    "data":{"sessionFile":"/scope/z.jsonl","sessionName":"Z"}}),
                &mut events,
                &mut w_state,
            );
            assert!(!s.session_refresh_pending, "{flow}: must reach ready");
            assert!(!s.session_change_in_flight);
            assert_eq!(s.session_file, "/scope/z.jsonl");
        }
    }

    #[test]
    fn duplicate_approval_id_never_replaces_displayed_payload() {
        let mut s = journal_state();
        let mut events = Vec::new();
        let first = json!({"type":"extension_ui_request","id":"dup","method":"confirm",
            "message":"first","timeout":60000});
        s.handle_ui_request(&first, &mut events);
        assert_eq!(events.len(), 1);
        let displayed = s.pending_approval.clone().expect("displayed");
        assert_eq!(displayed["message"], json!("first"));
        // Duplicate id with different content must be rejected fail-closed.
        let before_len = events.len();
        let second = json!({"type":"extension_ui_request","id":"dup","method":"confirm",
            "message":"EVIL-SWAP","timeout":60000});
        s.handle_ui_request(&second, &mut events);
        assert_eq!(events.len(), before_len, "no new uiRequest on duplicate");
        assert_eq!(s.pending_requests.len(), 1);
        let still = s.pending_requests.get("dup").expect("kept").request.clone();
        assert_eq!(still, first, "queued payload must stay identical");
        assert_eq!(
            s.pending_approval.clone().expect("displayed"),
            first,
            "displayed approval must stay identical exact fields"
        );
        assert_eq!(displayed, first);
    }

    // -- S-048: real deferral (surfaced/deferred never expire; round-robin) --

    fn approval_value(id: &str) -> Value {
        json!({"type":"extension_ui_request","id":id,"method":"confirm",
            "message":format!("approve {id}?"),"timeout":60000})
    }

    fn force_past_deadline(s: &mut AgentState, id: &str) {
        let entry = s.pending_requests.get_mut(id).expect("queued");
        entry.deadline = Instant::now() - std::time::Duration::from_secs(1);
    }

    #[test]
    fn unsurfaced_request_expires_fail_closed() {
        // A request NEVER surfaced to any UI keeps today's deadline and is
        // cancelled on expiry: the safety valve for dead UIs.
        let mut s = journal_state();
        let mut events = Vec::new();
        s.handle_ui_request(&approval_value("never-seen"), &mut events);
        assert_eq!(s.snapshot()["deferredCount"], json!(0));
        force_past_deadline(&mut s, "never-seen");
        let lines = s.expire_approvals(&mut events);
        assert_eq!(lines.len(), 1);
        assert_eq!(lines[0]["id"], json!("never-seen"));
        assert_eq!(lines[0]["cancelled"], json!(true));
        assert!(s.pending_requests.is_empty());
        assert!(s.pending_approval.is_none());
        assert!(events.iter().any(|e| e.name == "uiRequest"));
    }

    #[test]
    fn surfaced_request_never_expires() {
        // Once surfaced, no silent expiry: the request lives past its
        // deadline until answered, stopped, or exit.
        let mut s = journal_state();
        let mut events = Vec::new();
        s.handle_ui_request(&approval_value("shown"), &mut events);
        assert!(s.mark_surfaced("shown", &mut events));
        assert!(!s.mark_surfaced("no-such-id", &mut events));
        force_past_deadline(&mut s, "shown");
        let lines = s.expire_approvals(&mut events);
        assert!(lines.is_empty(), "surfaced request must not expire");
        assert!(s.pending_requests.contains_key("shown"));
        assert_eq!(
            s.pending_approval.as_ref().and_then(|v| v.get("id")),
            Some(&json!("shown"))
        );
        assert_eq!(s.snapshot()["deferredCount"], json!(0));
    }

    #[test]
    fn defer_parks_round_robin_and_never_expires() {
        let mut s = journal_state();
        let mut events = Vec::new();
        s.handle_ui_request(&approval_value("a-first"), &mut events);
        s.handle_ui_request(&approval_value("b-second"), &mut events);
        // BTreeMap key order: a-first is displayed first.
        assert_eq!(
            s.pending_approval.as_ref().and_then(|v| v.get("id")),
            Some(&json!("a-first"))
        );
        // Defer parks a-first: b-second surfaces, deferredCount reports it.
        assert!(s.defer_request("a-first", &mut events));
        assert!(!s.defer_request("no-such-id", &mut events));
        assert_eq!(
            s.pending_approval.as_ref().and_then(|v| v.get("id")),
            Some(&json!("b-second"))
        );
        assert_eq!(s.snapshot()["deferredCount"], json!(1));
        // Neither expires past the deadline, deferred or not-yet-surfaced.
        force_past_deadline(&mut s, "a-first");
        force_past_deadline(&mut s, "b-second");
        // b-second was never explicitly surfaced, but the deferral round
        // advanced to it; surface it as the displaying UI would.
        assert!(s.mark_surfaced("b-second", &mut events));
        let lines = s.expire_approvals(&mut events);
        assert!(lines.is_empty(), "deferred/surfaced requests must not expire");
        assert_eq!(s.pending_requests.len(), 2);
        // Answering b-second starts a new round: the parked a-first is
        // re-surfaced with its deferred flag cleared.
        s.pending_requests.remove("b-second");
        s.refresh_pending_approval(&mut events);
        assert_eq!(
            s.pending_approval.as_ref().and_then(|v| v.get("id")),
            Some(&json!("a-first"))
        );
        assert_eq!(s.snapshot()["deferredCount"], json!(0));
        // The re-surfaced request still never expires.
        let lines = s.expire_approvals(&mut events);
        assert!(lines.is_empty());
        assert!(s.pending_requests.contains_key("a-first"));
    }

    #[test]
    fn single_defer_starts_a_new_round_in_place() {
        // Deferring the only request clears the flag via the new-round rule
        // and keeps displaying it; nothing is lost and nothing expires.
        let mut s = journal_state();
        let mut events = Vec::new();
        s.handle_ui_request(&approval_value("only"), &mut events);
        let shown = s.pending_approval.clone();
        assert!(s.defer_request("only", &mut events));
        assert_eq!(s.pending_approval, shown);
        assert_eq!(s.snapshot()["deferredCount"], json!(0));
        force_past_deadline(&mut s, "only");
        assert!(s.expire_approvals(&mut events).is_empty());
    }

    #[test]
    fn approval_queue_cap_and_duplicate_rule_unchanged() {
        // 32-request cap and duplicate-id fail-closed rule are untouched by
        // the deferral change.
        let mut s = journal_state();
        let mut events = Vec::new();
        for i in 0..32 {
            s.handle_ui_request(&approval_value(&format!("q-{i:02}")), &mut events);
        }
        assert_eq!(s.pending_requests.len(), 32);
        s.handle_ui_request(&approval_value("q-overflow"), &mut events);
        assert_eq!(s.pending_requests.len(), 32);
        assert!(!s.pending_requests.contains_key("q-overflow"));
        let before = events.len();
        s.handle_ui_request(
            &json!({"type":"extension_ui_request","id":"q-00","method":"confirm",
                "message":"EVIL-SWAP","timeout":60000}),
            &mut events,
        );
        assert_eq!(events.len(), before);
        assert_eq!(s.pending_requests["q-00"].request["message"], json!("approve q-00?"));
    }

    #[test]
    fn queue_overflow_answers_cancelled_and_leaves_queue_intact() {
        // Fill the queue to the 32-request cap, then submit a 33rd request:
        // the overflow must be TOLD (cancelled line for the 33rd id), never
        // silently swallowed, and the first 32 must be untouched.
        let mut s = journal_state();
        let mut events = Vec::new();
        for i in 0..32 {
            let lines = s.handle_ui_request(&approval_value(&format!("q-{i:02}")), &mut events);
            assert!(lines.is_empty(), "inserted requests owe no child line");
        }
        assert_eq!(s.pending_requests.len(), 32);
        let approval_before = s.pending_approval.clone();
        let snapshot_before = s.snapshot();
        let events_before = events.len();

        let overflow = approval_value("q-overflow");
        let lines = s.handle_ui_request(&overflow, &mut events);
        assert_eq!(lines.len(), 1);
        assert_eq!(
            lines[0],
            json!({"type": "extension_ui_response", "id": "q-overflow", "cancelled": true})
        );
        // The refused id is never inserted: expiry can never touch it.
        assert_eq!(s.pending_requests.len(), 32);
        assert!(!s.pending_requests.contains_key("q-overflow"));
        assert_eq!(s.pending_approval, approval_before);
        assert_eq!(s.snapshot()["pendingRequests"].as_object().unwrap().len(), 32);
        assert_eq!(
            s.snapshot()["pendingRequests"],
            snapshot_before["pendingRequests"]
        );
        assert_eq!(s.snapshot()["pendingApproval"], snapshot_before["pendingApproval"]);
        assert_eq!(s.snapshot()["deferredCount"], json!(0));
        // No new dialog is surfaced for the refused request.
        assert_eq!(events.len(), events_before);
        // The refusal is recorded through the bounded diagnostic tail.
        assert!(s.diagnostic.contains("q-overflow"));
    }

    #[test]
    fn parked_overflow_queue_keeps_deferral_round_robin() {
        // The 32 parked requests are unaffected by a refused 33rd: defer one
        // and advance a round with existing round-robin behavior intact.
        let mut s = journal_state();
        let mut events = Vec::new();
        for i in 0..32 {
            s.handle_ui_request(&approval_value(&format!("q-{i:02}")), &mut events);
        }
        let refused = s.handle_ui_request(&approval_value("q-overflow"), &mut events);
        assert_eq!(refused.len(), 1);
        // Defer the displayed head; the next request surfaces instead.
        assert!(s.defer_request("q-00", &mut events));
        assert_eq!(
            s.pending_approval.as_ref().and_then(|v| v.get("id")),
            Some(&json!("q-01"))
        );
        assert_eq!(s.snapshot()["deferredCount"], json!(1));
        assert_eq!(s.pending_requests.len(), 32);
        assert!(!s.pending_requests.contains_key("q-overflow"));
        // Answering everything else starts a new round: parked q-00
        // re-surfaces with its deferred flag cleared.
        for i in 1..32 {
            s.pending_requests.remove(&format!("q-{i:02}"));
        }
        s.refresh_pending_approval(&mut events);
        assert_eq!(
            s.pending_approval.as_ref().and_then(|v| v.get("id")),
            Some(&json!("q-00"))
        );
        assert_eq!(s.snapshot()["deferredCount"], json!(0));
    }

    #[test]
    fn overflow_cancelled_line_matches_expiry_shape() {
        // The overflow response must have exactly the same keys as the
        // cancelled line expire_approvals produces (byte-for-byte shape).
        let mut s = journal_state();
        let mut events = Vec::new();
        for i in 0..32 {
            s.handle_ui_request(&approval_value(&format!("q-{i:02}")), &mut events);
        }
        let overflow = s.handle_ui_request(&approval_value("q-overflow"), &mut events);
        assert_eq!(overflow.len(), 1);
        // Reference shape from the fail-closed expiry valve.
        let mut r = journal_state();
        let mut revents = Vec::new();
        r.handle_ui_request(&approval_value("ref"), &mut revents);
        force_past_deadline(&mut r, "ref");
        let expired = r.expire_approvals(&mut revents);
        assert_eq!(expired.len(), 1);
        let o_keys: Vec<&String> = overflow[0].as_object().unwrap().keys().collect();
        let e_keys: Vec<&String> = expired[0].as_object().unwrap().keys().collect();
        assert_eq!(o_keys, e_keys);
        assert_eq!(overflow[0]["type"], expired[0]["type"]);
        assert_eq!(overflow[0]["cancelled"], expired[0]["cancelled"]);
        assert_eq!(overflow[0]["type"], json!("extension_ui_response"));
        // Same construction as reject_all_requests: identical key order.
        let mut j = journal_state();
        let mut jevents = Vec::new();
        j.handle_ui_request(&approval_value("rej"), &mut jevents);
        let rejected = j.reject_all_requests(&mut jevents);
        assert_eq!(rejected.len(), 1);
        let r_keys: Vec<&String> = rejected[0].as_object().unwrap().keys().collect();
        assert_eq!(o_keys, r_keys);
    }

    // -- palette (general) mode: same fencing, never scoped graph --

    fn palette_state() -> AgentState {
        let mut s = AgentState::new(
            String::new(),
            false,
            "/pal/a.jsonl".into(),
            "A".into(),
            false,
        );
        s.process_started = true;
        s.desired_running = true;
        s.state_ok = true;
        s.extension_ok = true;
        s.status = "Ready".to_string();
        assert!(!s.scoped_mode(), "palette must not be scoped graph");
        s
    }

    fn palette_begin_new(s: &mut AgentState) -> String {
        // Mirror Bridge::op_new_session (now mode-agnostic).
        s.control_pending = true;
        s.fresh_session = true;
        s.session_file.clear();
        s.begin_session_refresh();
        let id = s.next_rpc_id();
        s.request_kinds
            .insert(id.clone(), RequestKind::simple("new_session"));
        s.begin_session_change("new", &id);
        id
    }

    #[test]
    fn palette_startup_defers_history_until_authoritative_state() {
        // Fresh palette bridge: startup marks refresh + awaits state, so an
        // early get_messages frame cannot populate stale history.
        let mut s = AgentState::new(String::new(), false, String::new(), String::new(), false);
        s.process_started = true;
        s.desired_running = true;
        let writes = s.start_ready_writes();
        assert!(s.session_refresh_pending, "palette startup must gate");
        assert!(s.messages_awaiting_session_state);
        assert!(!s.ready(), "palette ready gates on refresh");
        assert!(!s.scoped_mode());
        let auth_id = writes
            .iter()
            .find(|w| w.get("type").and_then(|v| v.as_str()) == Some("get_state"))
            .and_then(|w| w.get("id"))
            .and_then(|v| v.as_str())
            .expect("startup get_state")
            .to_string();
        assert_eq!(
            s.request_kinds
                .get(&auth_id)
                .and_then(|r| r.refresh_generation),
            Some(s.session_refresh_generation),
            "startup poll must be authoritative"
        );
        // Early history for the (still unknown) identity is fenced.
        let early_id = writes
            .iter()
            .find(|w| w.get("type").and_then(|v| v.as_str()) == Some("get_messages"))
            .and_then(|w| w.get("id"))
            .and_then(|v| v.as_str())
            .expect("startup get_messages")
            .to_string();
        let mut events = Vec::new();
        let mut w = Vec::new();
        s.handle_pi_response(
            &json!({"type":"response","id":early_id,"command":"get_messages","success":true,
                "data":{"messages":[{"role":"user","content":"EARLY"}]}}),
            &mut events,
            &mut w,
        );
        assert!(s.messages.is_empty(), "early history must not apply");
        assert!(
            !events.iter().any(|e| e.name == "historyLoaded"),
            "no historyLoaded before authoritative state"
        );
        // Authoritative state installs identity and rerequests history.
        let mut w2 = Vec::new();
        s.handle_pi_response(
            &json!({"type":"response","id":auth_id,"command":"get_state","success":true,
                "data":{"sessionFile":"/pal/a.jsonl","sessionName":"A"}}),
            &mut events,
            &mut w2,
        );
        assert!(!s.session_refresh_pending);
        assert_eq!(s.session_file, "/pal/a.jsonl");
        assert!(w2
            .iter()
            .any(|v| v.get("type").and_then(|x| x.as_str()) == Some("get_messages")));
        // The rerequested generation now loads and emits historyLoaded.
        let rereq = w2
            .iter()
            .find(|v| v.get("type").and_then(|x| x.as_str()) == Some("get_messages"))
            .and_then(|v| v.get("id"))
            .and_then(|v| v.as_str())
            .expect("rerequest")
            .to_string();
        s.handle_pi_response(
            &json!({"type":"response","id":rereq,"command":"get_messages","success":true,
                "data":{"messages":[{"role":"user","content":"hello"}]}}),
            &mut events,
            &mut w,
        );
        assert_eq!(s.messages.len(), 1);
        assert_eq!(events.last().unwrap().name, "historyLoaded");
    }

    #[test]
    fn palette_new_retains_gate_until_ack_and_authoritative_state() {
        let mut s = palette_state();
        let change_id = palette_begin_new(&mut s);
        let gen = s.session_refresh_generation;
        assert!(s.session_refresh_pending);
        assert!(!s.ready(), "palette New must gate ready");
        let mut events = Vec::new();
        // Settled old-state poll stays untagged and cannot release.
        let writes = s.handle_pi_value(&json!({"type":"agent_settled"}), &mut events);
        let settled_id = settled_get_state_id(&writes);
        assert!(!settled_id.is_empty());
        assert_eq!(
            s.request_kinds
                .get(&settled_id)
                .and_then(|r| r.refresh_generation),
            None
        );
        let mut w2 = Vec::new();
        s.handle_pi_response(
            &json!({"type":"response","id":settled_id,"command":"get_state","success":true,
                "data":{"sessionFile":"/pal/a.jsonl"}}),
            &mut events,
            &mut w2,
        );
        assert!(
            s.session_refresh_pending,
            "settled old state must not release"
        );
        assert!(
            s.session_file.is_empty(),
            "New cleared file; old must not restore"
        );
        // Stop/abort before ACK only marks cancel, never releases.
        let cancelled = s.fail_session_refresh("cancel?", false, &mut events);
        assert!(!cancelled);
        assert!(s.session_change_cancel_requested);
        assert!(s.session_refresh_pending);
        // Real ACK authorizes a tagged poll (not a release).
        let mut w3 = Vec::new();
        s.handle_pi_response(
            &json!({"type":"response","id":change_id,"command":"new_session","success":true,
                "data":{}}),
            &mut events,
            &mut w3,
        );
        assert!(s.session_refresh_pending);
        let auth_id = settled_get_state_id(&w3);
        assert_eq!(
            s.request_kinds
                .get(&auth_id)
                .and_then(|r| r.refresh_generation),
            Some(gen)
        );
        // Authoritative state releases with the new identity.
        let mut w4 = Vec::new();
        s.handle_pi_response(
            &json!({"type":"response","id":auth_id,"command":"get_state","success":true,
                "data":{"sessionFile":"/pal/b.jsonl","sessionName":"B"}}),
            &mut events,
            &mut w4,
        );
        assert!(!s.session_refresh_pending);
        assert!(!s.control_pending);
        assert_eq!(s.session_file, "/pal/b.jsonl");
        assert!(s.ready());
        assert!(!s.scoped_mode(), "palette stays unscoped after New");
    }

    #[test]
    fn palette_history_failure_is_correlated_not_generic() {
        let mut s = palette_state();
        s.messages_generation = 2;
        s.messages_session_file = s.session_file.clone();
        s.request_kinds.insert(
            "cur".to_string(),
            RequestKind {
                kind: "get_messages".to_string(),
                message: None,
                session_file: Some(s.session_file.clone()),
                generation: Some(2),
                refresh_generation: None,
            },
        );
        let mut events = Vec::new();
        let mut writes = Vec::new();
        s.handle_pi_response(
            &json!({"type":"response","id":"cur","command":"get_messages","success":false,"error":"down"}),
            &mut events,
            &mut writes,
        );
        assert_eq!(events.len(), 2, "historyFailed + failed (non-journal)");
        assert_eq!(events[0].name, "historyFailed");
        assert_eq!(events[0].args[1], json!("/pal/a.jsonl"));
        // Stale generation failure is ignored entirely.
        s.request_kinds.insert(
            "old".to_string(),
            RequestKind {
                kind: "get_messages".to_string(),
                message: None,
                session_file: Some(s.session_file.clone()),
                generation: Some(1),
                refresh_generation: None,
            },
        );
        let before = events.len();
        s.handle_pi_response(
            &json!({"type":"response","id":"old","command":"get_messages","success":false,"error":"old"}),
            &mut events,
            &mut writes,
        );
        assert_eq!(events.len(), before);
    }

    #[test]
    fn palette_restore_picker_gates_until_authoritative() {
        let mut s = palette_state();
        // switchSession equivalent for palette: prompt picker + refresh gate.
        let picker_id = {
            let id = s.next_rpc_id();
            s.request_kinds.insert(
                id.clone(),
                RequestKind {
                    kind: "prompt".to_string(),
                    message: Some("/desktop-sessions".to_string()),
                    session_file: None,
                    generation: None,
                    refresh_generation: None,
                },
            );
            s.last_request_id = id.clone();
            s.begin_session_refresh();
            let rid = s.last_request_id.clone();
            s.begin_session_change("restore", &rid);
            s.session_switching = true;
            s.busy = true;
            id
        };
        let gen = s.session_refresh_generation;
        assert!(s.session_refresh_pending);
        let mut events = Vec::new();
        let mut w = Vec::new();
        // Foreign prompt ACK with another id cannot release.
        s.request_kinds
            .insert("qs-plain".to_string(), RequestKind::simple("prompt"));
        s.handle_pi_response(
            &json!({"type":"response","id":"qs-plain","command":"prompt","success":true,"data":{}}),
            &mut events,
            &mut w,
        );
        assert!(s.session_refresh_pending);
        // Picker ACK authorizes the tagged poll.
        let mut w2 = Vec::new();
        s.handle_pi_response(
            &json!({"type":"response","id":picker_id,"command":"prompt","success":true,"data":{}}),
            &mut events,
            &mut w2,
        );
        let auth_id = settled_get_state_id(&w2);
        assert_eq!(
            s.request_kinds
                .get(&auth_id)
                .and_then(|r| r.refresh_generation),
            Some(gen)
        );
        let mut w3 = Vec::new();
        s.handle_pi_response(
            &json!({"type":"response","id":auth_id,"command":"get_state","success":true,
                "data":{"sessionFile":"/pal/c.jsonl","sessionName":"C"}}),
            &mut events,
            &mut w3,
        );
        assert!(!s.session_refresh_pending);
        assert_eq!(s.session_file, "/pal/c.jsonl");
    }

    #[test]
    fn palette_busy_controls_and_extension_safety_match_scoped() {
        let mut s = palette_state();
        // Busy prompt path: agent_start sets busy, settled clears + refreshes.
        let mut events = Vec::new();
        s.handle_pi_value(&json!({"type":"agent_start"}), &mut events);
        assert!(s.busy);
        // Compact + model + commands paths stay live for palette.
        s.request_kinds.insert(
            "m1".to_string(),
            RequestKind::simple("get_available_models"),
        );
        let mut w = Vec::new();
        s.handle_pi_response(
            &json!({"type":"response","id":"m1","command":"get_available_models","success":true,
                "data":{"models":[{"id":"x"}]}}),
            &mut events,
            &mut w,
        );
        assert_eq!(s.models.len(), 1);
        s.request_kinds
            .insert("c1".to_string(), RequestKind::simple("get_commands"));
        s.handle_pi_response(
            &json!({"type":"response","id":"c1","command":"get_commands","success":true,
                "data":{"commands":[{"name":"desktop-sessions"}]}}),
            &mut events,
            &mut w,
        );
        assert!(s.extension_ok);
        // Extension error without refresh surfaces failed + get_state.
        let writes = s.handle_pi_value(
            &json!({"type":"extension_error","error":"boom"}),
            &mut events,
        );
        assert_eq!(events.last().unwrap().name, "failed");
        assert!(writes
            .iter()
            .any(|v| v.get("type").and_then(|x| x.as_str()) == Some("get_state")));
    }
}
