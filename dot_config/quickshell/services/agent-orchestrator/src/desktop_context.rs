//! Generic desktop context types and in-memory tracking.
//!
//! No compositor-specific fields here (no Hyprland addresses/classes as
//! protocol concepts): a focused window is an opaque `id` plus human
//! `application`/`title` strings, and a workspace is an opaque `id`/`name`.

use serde::{Deserialize, Serialize};
use std::time::{SystemTime, UNIX_EPOCH};

/// Max chars retained per field (char-boundary safe truncation).
pub const MAX_ID_CHARS: usize = 256;
pub const MAX_APP_CHARS: usize = 256;
pub const MAX_TITLE_CHARS: usize = 1024;
pub const MAX_WORKSPACE_CHARS: usize = 256;

/// Focused window: `id` is opaque (compositor-assigned), never interpreted.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct FocusedWindow {
    pub id: String,
    pub application: String,
    pub title: String,
}

impl FocusedWindow {
    pub fn new(id: &str, application: &str, title: &str) -> Self {
        Self {
            id: bound_chars(id, MAX_ID_CHARS),
            application: bound_chars(application, MAX_APP_CHARS),
            title: bound_chars(title, MAX_TITLE_CHARS),
        }
    }
}

/// Active workspace: both fields opaque strings.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Workspace {
    pub id: String,
    pub name: String,
}

impl Workspace {
    pub fn new(id: &str, name: &str) -> Self {
        Self {
            id: bound_chars(id, MAX_WORKSPACE_CHARS),
            name: bound_chars(name, MAX_WORKSPACE_CHARS),
        }
    }
}

/// Observation source. Generic: only the compositor family is recorded.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Source {
    Hyprland,
    Unknown,
}

impl Source {
    pub fn as_str(&self) -> &'static str {
        match self {
            Source::Hyprland => "hyprland",
            Source::Unknown => "unknown",
        }
    }

    pub fn from_str(s: &str) -> Self {
        match s {
            "hyprland" => Source::Hyprland,
            _ => Source::Unknown,
        }
    }
}

/// Availability is explicit so disconnects clear live state instead of
/// retaining a stale snapshot.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Availability {
    Available,
    Unavailable,
}

/// Live desktop context snapshot (serializable).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct DesktopContext {
    pub focused_window: Option<FocusedWindow>,
    pub workspace: Option<Workspace>,
    pub available: bool,
    pub source: Source,
    /// UTC epoch milliseconds of observation.
    pub observed_at_ms: i64,
}

impl DesktopContext {
    pub fn available(
        source: Source,
        focused_window: Option<FocusedWindow>,
        workspace: Option<Workspace>,
        observed_at_ms: i64,
    ) -> Self {
        Self {
            focused_window,
            workspace,
            available: true,
            source,
            observed_at_ms,
        }
    }

    pub fn unavailable(source: Source, observed_at_ms: i64) -> Self {
        Self {
            focused_window: None,
            workspace: None,
            available: false,
            source,
            observed_at_ms,
        }
    }

    /// Semantic equality: everything except the observation timestamp.
    /// Used for dedup so paired/duplicate events do not append history.
    pub fn semantic_eq(&self, other: &Self) -> bool {
        self.focused_window == other.focused_window
            && self.workspace == other.workspace
            && self.available == other.available
            && self.source == other.source
    }
}

/// Persisted activity kind. Stored as its `as_str()` string.
///
/// Phase 1 honesty: snapshot diffs never infer lifecycle. `WindowOpen` /
/// `WindowClose` are RESERVED for future explicit lifecycle evidence only
/// (e.g. compositor open/close notifications correlated with a snapshot);
/// the Phase 1 classifier maps `Some <-> None` focused-window changes to
/// `Focus`, since losing workspace focus is not lifecycle evidence.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ActivityKind {
    Focus,
    Title,
    Workspace,
    /// Reserved: never produced by [`classify_transition`] in Phase 1.
    WindowOpen,
    /// Reserved: never produced by [`classify_transition`] in Phase 1.
    WindowClose,
    Availability,
    Snapshot,
}

impl ActivityKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            ActivityKind::Focus => "focus",
            ActivityKind::Title => "title",
            ActivityKind::Workspace => "workspace",
            ActivityKind::WindowOpen => "window_open",
            ActivityKind::WindowClose => "window_close",
            ActivityKind::Availability => "availability",
            ActivityKind::Snapshot => "snapshot",
        }
    }

    pub fn from_str(s: &str) -> Self {
        match s {
            "focus" => ActivityKind::Focus,
            "title" => ActivityKind::Title,
            "workspace" => ActivityKind::Workspace,
            "window_open" => ActivityKind::WindowOpen,
            "window_close" => ActivityKind::WindowClose,
            "availability" => ActivityKind::Availability,
            _ => ActivityKind::Snapshot,
        }
    }
}

/// Classify a transition for persistence. `prev=None` means first observation
/// (startup): the kind describes what the snapshot contains, not a diff.
///
/// Honesty rule: a focused-window `Some <-> None` change is classified as
/// `Focus`, NOT `WindowOpen`/`WindowClose`. A snapshot alone cannot prove a
/// window was created/destroyed (focus can move to another workspace while
/// the window still exists); Phase 1 tracks focused open/close changes
/// honestly as focus changes. `WindowOpen`/`WindowClose` remain reserved for
/// future explicit lifecycle evidence.
pub fn classify_transition(prev: Option<&DesktopContext>, next: &DesktopContext) -> ActivityKind {
    let Some(prev) = prev else {
        if !next.available {
            return ActivityKind::Availability;
        }
        // First live observation: describe it usefully.
        if next.focused_window.is_some() {
            return ActivityKind::Focus;
        }
        if next.workspace.is_some() {
            return ActivityKind::Workspace;
        }
        return ActivityKind::Snapshot;
    };
    if prev.available != next.available || prev.source != next.source {
        return ActivityKind::Availability;
    }
    if !next.available {
        return ActivityKind::Availability;
    }
    let prev_win = prev.focused_window.as_ref();
    let next_win = next.focused_window.as_ref();
    match (prev_win, next_win) {
        (None, None) => {}
        // Focused-window appeared/disappeared: focus change, not proof of
        // lifecycle. Reserved WindowOpen/WindowClose are never inferred here.
        (None, Some(_)) | (Some(_), None) => return ActivityKind::Focus,
        (Some(p), Some(n)) => {
            if p.id != n.id {
                return ActivityKind::Focus;
            }
            if p.title != n.title || p.application != n.application {
                return ActivityKind::Title;
            }
        }
    }
    if prev.workspace != next.workspace {
        return ActivityKind::Workspace;
    }
    // Same window id/title and same workspace but something else changed
    // (should be rare given semantic_eq gate): generic snapshot.
    ActivityKind::Snapshot
}

/// Bounded FIFO capacity for durable pending observations.
///
/// Overflow policy (explicit gap, finite memory): [`Tracker::enqueue`]
/// returns `Err(PendingFull)` when full. The pending backlog is retained in
/// the `Tracker` across reconnect/backoff so a later flush can preserve
/// original timestamps and `A -> B -> A` order, but the *new* overflowing
/// observation is NOT retained (no reserved slot): the caller must log an
/// explicit `observation gap` to stderr and stop the session (see collector
/// `BacklogFull`). History therefore has an explicit gap on overflow — never
/// a lossless-recovery claim. While no subscription is held (session ended,
/// storage-blocked) live `current` must read unavailable via
/// [`Tracker::mark_current_unavailable_local`], not stale available.
pub const MAX_PENDING_OBSERVATIONS: usize = 128;

/// Explicit bounded-overflow error: the pending FIFO is full.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PendingFull {
    pub capacity: usize,
}

impl std::fmt::Display for PendingFull {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "history backlog full (capacity {})", self.capacity)
    }
}

impl std::error::Error for PendingFull {}

/// Outcome of [`Tracker::enqueue`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EnqueueOutcome {
    /// Semantically equal to the latest queued/persisted state; not queued.
    Deduplicated,
    /// Queued with its original `observed_at_ms`; caller should flush.
    Queued(ActivityKind, DesktopContext),
}

/// In-memory live state plus persistence watermark and durable backlog.
///
/// - `current`: live state only. `None` until the first observation (a fresh
///   process never backfills this from SQLite rows).
/// - `last_persisted`: last semantically-distinct context successfully
///   appended. Compared ignoring timestamps.
/// - `pending`: bounded FIFO of meaningful observations not yet persisted,
///   each with its ORIGINAL `observed_at_ms`. Semantic dedup for new
///   observations is against the queue tail when non-empty (else
///   `last_persisted`), so `A -> B -> A` across failures preserves `B`
///   instead of losing it to watermark dedup. Flush in FIFO order to
///   preserve history order; retry on idle/backoff without new compositor
///   queries (never desktop polling).
#[derive(Debug, Default)]
pub struct Tracker {
    current: Option<DesktopContext>,
    last_persisted: Option<DesktopContext>,
    pending: std::collections::VecDeque<(ActivityKind, DesktopContext)>,
}

impl Tracker {
    pub fn new() -> Self {
        Self {
            current: None,
            last_persisted: None,
            pending: std::collections::VecDeque::new(),
        }
    }

    /// Live context only. `None` = no observation yet in this process.
    /// Never derived from persisted rows.
    pub fn current_context(&self) -> Option<DesktopContext> {
        self.current.clone()
    }

    /// Observe a new snapshot. Updates live `current` unconditionally and
    /// returns `(kind, snapshot)` when the snapshot is semantically new
    /// versus the last *persisted* context (so failed appends retry).
    ///
    /// NOTE: the durable collector path uses [`Tracker::enqueue`] (queue
    /// tail dedup, bounded FIFO, order-preserving) instead of this helper.
    /// This method is retained for simple immediate-persist callers and
    /// backward compatibility; it does not consult the pending backlog.
    pub fn observe(&mut self, ctx: DesktopContext) -> Option<(ActivityKind, DesktopContext)> {
        let changed = match self.last_persisted.as_ref() {
            None => true,
            Some(last) => !last.semantic_eq(&ctx),
        };
        // Live state always tracks the latest observation, including
        // duplicates (timestamp refresh) and unavailable markers.
        self.current = Some(ctx.clone());
        if !changed {
            return None;
        }
        let prev_persisted = self.last_persisted.as_ref();
        let kind = classify_transition(prev_persisted, &ctx);
        Some((kind, ctx))
    }

    /// Durable enqueue: updates live `current` unconditionally, dedups
    /// semantically against the pending tail when non-empty (else
    /// `last_persisted`), and retains meaningful observations in a bounded
    /// FIFO with ORIGINAL timestamps.
    ///
    /// - `Ok(EnqueueOutcome::Deduplicated)` — duplicate of the latest
    ///   queued/persisted state; nothing queued.
    /// - `Ok(EnqueueOutcome::Queued(kind, snap))` — queued; caller should
    ///   flush FIFO in order via `pending_front` + store append +
    ///   `mark_persisted` + `pop_pending_front`.
    /// - `Err(PendingFull)` — backlog full; live `current` already advanced
    ///   but nothing was queued or dropped silently. Caller must surface the
    ///   error and stop the session (collector `BacklogFull`).
    pub fn enqueue(
        &mut self,
        ctx: DesktopContext,
    ) -> Result<EnqueueOutcome, PendingFull> {
        // Predecessor for both dedup and kind: latest queued state wins so
        // A -> B -> A across failures preserves B.
        let prev_owned: Option<DesktopContext> = if let Some((_, back)) = self.pending.back() {
            Some(back.clone())
        } else {
            self.last_persisted.clone()
        };
        let dominated = match prev_owned.as_ref() {
            None => false, // first observation: always meaningful
            Some(prev) => prev.semantic_eq(&ctx),
        };
        // Live state always tracks latest, even on dedup/overflow.
        self.current = Some(ctx.clone());
        if dominated {
            return Ok(EnqueueOutcome::Deduplicated);
        }
        let kind = classify_transition(prev_owned.as_ref(), &ctx);
        if self.pending.len() >= MAX_PENDING_OBSERVATIONS {
            return Err(PendingFull {
                capacity: MAX_PENDING_OBSERVATIONS,
            });
        }
        self.pending.push_back((kind.clone(), ctx.clone()));
        Ok(EnqueueOutcome::Queued(kind, ctx))
    }

    /// Number of observations awaiting persistence.
    pub fn pending_len(&self) -> usize {
        self.pending.len()
    }

    /// True when no observation awaits persistence.
    pub fn pending_is_empty(&self) -> bool {
        self.pending.is_empty()
    }

    /// Clone the FIFO front (oldest unpersisted) without removing.
    pub fn pending_front(&self) -> Option<(ActivityKind, DesktopContext)> {
        self.pending.front().cloned()
    }

    /// Remove and return the FIFO front after its append succeeded (call
    /// [`Tracker::mark_persisted`] for the same snapshot first).
    pub fn pop_pending_front(&mut self) -> Option<(ActivityKind, DesktopContext)> {
        self.pending.pop_front()
    }

    /// Durable unavailable enqueue (same backlog contract as `enqueue`).
    pub fn enqueue_unavailable(
        &mut self,
        source: Source,
        observed_at_ms: i64,
    ) -> Result<EnqueueOutcome, PendingFull> {
        self.enqueue(DesktopContext::unavailable(source, observed_at_ms))
    }

    /// Local-only unavailable marking while no subscription is held.
    ///
    /// Sets live `current` to unavailable WITHOUT touching `pending` or
    /// `last_persisted`. Used when the collector holds no live subscription
    /// (disconnected session end, storage-blocked with a full backlog where
    /// an unavailable marker cannot be enqueued). Never affects durability:
    /// no row is queued, no watermark advances.
    pub fn mark_current_unavailable_local(&mut self, source: Source, observed_at_ms: i64) {
        self.current = Some(DesktopContext::unavailable(source, observed_at_ms));
    }

    /// True when the bounded backlog is at capacity (storage-blocked).
    pub fn backlog_is_full(&self) -> bool {
        self.pending.len() >= MAX_PENDING_OBSERVATIONS
    }

    /// Mark a context as successfully persisted. Call only after the store
    /// append succeeded; on transient SQLite failure skip this so the next
    /// observation retries the same semantic state.
    pub fn mark_persisted(&mut self, ctx: &DesktopContext) {
        self.last_persisted = Some(ctx.clone());
    }

    /// Mark the source unavailable: clears focused window/workspace and
    /// records an unavailable live context. Returns the persistence payload
    /// when semantically new (same retry contract as [`Tracker::observe`]).
    pub fn mark_unavailable(
        &mut self,
        source: Source,
        observed_at_ms: i64,
    ) -> Option<(ActivityKind, DesktopContext)> {
        self.observe(DesktopContext::unavailable(source, observed_at_ms))
    }

    #[cfg(test)]
    pub fn last_persisted(&self) -> Option<DesktopContext> {
        self.last_persisted.clone()
    }
}

/// Current UTC epoch milliseconds. Non-negative; 0 on clock failure.
pub fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

fn bound_chars(s: &str, max_chars: usize) -> String {
    if s.chars().count() <= max_chars {
        return s.to_string();
    }
    s.chars().take(max_chars).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ctx_with(win_id: Option<&str>, title: &str, ws: Option<&str>, ts: i64) -> DesktopContext {
        DesktopContext::available(
            Source::Hyprland,
            win_id.map(|id| FocusedWindow::new(id, "app", title)),
            ws.map(|w| Workspace::new(w, w)),
            ts,
        )
    }

    #[test]
    fn semantic_eq_ignores_timestamp() {
        let a = ctx_with(Some("0x1"), "t", Some("1"), 1000);
        let b = ctx_with(Some("0x1"), "t", Some("1"), 2000);
        assert!(a.semantic_eq(&b));
        assert_ne!(a, b); // timestamps differ
    }

    #[test]
    fn semantic_eq_detects_focus_title_workspace_availability() {
        let base = ctx_with(Some("0x1"), "t", Some("1"), 1);
        assert!(!base.semantic_eq(&ctx_with(Some("0x2"), "t", Some("1"), 1)));
        assert!(!base.semantic_eq(&ctx_with(Some("0x1"), "t2", Some("1"), 1)));
        assert!(!base.semantic_eq(&ctx_with(Some("0x1"), "t", Some("2"), 1)));
        let un = DesktopContext::unavailable(Source::Hyprland, 1);
        assert!(!base.semantic_eq(&un));
    }

    #[test]
    fn tracker_dedups_identical_semantics() {
        let mut t = Tracker::new();
        assert!(t.current_context().is_none());
        let a = ctx_with(Some("0x1"), "t", Some("1"), 100);
        let b = ctx_with(Some("0x1"), "t", Some("1"), 200);
        let first = t.observe(a.clone()).expect("first persists");
        assert_eq!(first.0, ActivityKind::Focus);
        t.mark_persisted(&first.1);
        assert!(t.observe(b).is_none(), "same semantics dedup");
        // Live current still refreshes timestamp.
        assert_eq!(t.current_context().unwrap().observed_at_ms, 200);
    }

    #[test]
    fn tracker_retries_after_failed_append() {
        let mut t = Tracker::new();
        let a = ctx_with(Some("0x1"), "t", Some("1"), 100);
        let (k, snap) = t.observe(a).expect("persist");
        assert_eq!(k, ActivityKind::Focus);
        // Simulate transient SQLite failure: do NOT mark_persisted.
        let _ = snap;
        // Same semantics observed again must still request persistence.
        let b = ctx_with(Some("0x1"), "t", Some("1"), 200);
        assert!(t.observe(b).is_some(), "retry after failure");
    }

    #[test]
    fn classify_focus_title_workspace_focus_not_lifecycle() {
        let none_ws =
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 1);
        assert_eq!(classify_transition(None, &none_ws), ActivityKind::Workspace);
        let w1 = ctx_with(Some("0x1"), "t", Some("1"), 1);
        assert_eq!(classify_transition(None, &w1), ActivityKind::Focus);
        // Title change, same window id.
        let w1t = ctx_with(Some("0x1"), "t2", Some("1"), 2);
        assert_eq!(classify_transition(Some(&w1), &w1t), ActivityKind::Title);
        // Workspace change, same window.
        let w1ws = ctx_with(Some("0x1"), "t", Some("2"), 3);
        assert_eq!(
            classify_transition(Some(&w1), &w1ws),
            ActivityKind::Workspace
        );
        // Focus change.
        let w2 = ctx_with(Some("0x2"), "t", Some("1"), 4);
        assert_eq!(classify_transition(Some(&w1), &w2), ActivityKind::Focus);
        // Focused window disappeared: focus loss, NOT lifecycle proof.
        let empty =
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 5);
        assert_eq!(
            classify_transition(Some(&w1), &empty),
            ActivityKind::Focus,
            "Some->None is focus loss, not window_close"
        );
        // Focused window appeared: focus gain, NOT lifecycle proof.
        assert_eq!(
            classify_transition(Some(&empty), &w1),
            ActivityKind::Focus,
            "None->Some is focus gain, not window_open"
        );
        // Reserved lifecycle kinds are never inferred by the classifier.
        assert_ne!(
            classify_transition(Some(&w1), &empty),
            ActivityKind::WindowClose
        );
        assert_ne!(
            classify_transition(Some(&empty), &w1),
            ActivityKind::WindowOpen
        );
        // Availability flip.
        let un = DesktopContext::unavailable(Source::Hyprland, 6);
        assert_eq!(
            classify_transition(Some(&w1), &un),
            ActivityKind::Availability
        );
    }

    #[test]
    fn empty_focused_window_and_workspace_valid() {
        let empty = DesktopContext::available(Source::Hyprland, None, None, 1);
        assert_eq!(classify_transition(None, &empty), ActivityKind::Snapshot);
        let mut t = Tracker::new();
        let (k, _) = t.observe(empty).expect("empty snapshot persists once");
        assert_eq!(k, ActivityKind::Snapshot);
    }

    #[test]
    fn mark_unavailable_clears_live_state() {
        let mut t = Tracker::new();
        t.observe(ctx_with(Some("0x1"), "t", Some("1"), 1));
        let cur = t.current_context().unwrap();
        assert!(cur.available);
        assert!(cur.focused_window.is_some());
        let payload = t.mark_unavailable(Source::Hyprland, 2);
        assert!(payload.is_some());
        let cur = t.current_context().unwrap();
        assert!(!cur.available);
        assert!(cur.focused_window.is_none());
        assert!(cur.workspace.is_none());
    }

    #[test]
    fn fresh_tracker_has_no_current_no_fake_restore() {
        let t = Tracker::new();
        assert!(t.current_context().is_none());
    }

    #[test]
    fn enqueue_preserves_a_b_a_across_failures() {
        // Regression: append failure only logged + watermark dedup lost B
        // when A arrived again. Queue-tail dedup must preserve A->B->A.
        let mut t = Tracker::new();
        let a1 = ctx_with(Some("0x1"), "t", Some("1"), 100);
        let b = ctx_with(Some("0x2"), "t", Some("1"), 200);
        let a2 = ctx_with(Some("0x1"), "t", Some("1"), 300);
        // Simulate A already persisted.
        match t.enqueue(a1.clone()).expect("queue A") {
            EnqueueOutcome::Queued(_, _) => {}
            other => panic!("expected queued, got {other:?}"),
        }
        // Pretend the flush of A succeeded (pop + watermark) so the backlog
        // starts empty with A persisted — mirrors collector flush success.
        let (k, snap) = t.pop_pending_front().expect("pop A");
        assert_eq!(k, ActivityKind::Focus);
        t.mark_persisted(&snap);
        assert!(t.pending_is_empty());
        // B arrives while storage is down: queued, not yet persisted.
        match t.enqueue(b.clone()).expect("queue B") {
            EnqueueOutcome::Queued(_, _) => {}
            other => panic!("expected queued B, got {other:?}"),
        }
        assert_eq!(t.pending_len(), 1);
        // A arrives again: distinct from queued tail B, so must queue even
        // though it equals last_persisted A. Old watermark dedup dropped B.
        match t.enqueue(a2.clone()).expect("queue A2") {
            EnqueueOutcome::Queued(kind, snap) => {
                assert_eq!(kind, ActivityKind::Focus);
                assert_eq!(snap.observed_at_ms, 300, "original timestamp kept");
            }
            other => panic!("A2 must queue behind B, got {other:?}"),
        }
        assert_eq!(t.pending_len(), 2);
        // FIFO order preserved with original timestamps.
        let first = t.pending_front().expect("front");
        assert_eq!(first.1.observed_at_ms, 200);
        let _ = t.pop_pending_front();
        let second = t.pending_front().expect("second");
        assert_eq!(second.1.observed_at_ms, 300);
    }

    #[test]
    fn enqueue_dedups_against_tail_not_watermark() {
        let mut t = Tracker::new();
        let a = ctx_with(Some("0x1"), "t", Some("1"), 100);
        let b = ctx_with(Some("0x1"), "t", Some("1"), 200); // same semantics
        match t.enqueue(a).expect("queue") {
            EnqueueOutcome::Queued(_, _) => {}
            other => panic!("{other:?}"),
        }
        // Duplicate of queued tail: dedup, no growth.
        assert_eq!(
            t.enqueue(b).expect("dedup"),
            EnqueueOutcome::Deduplicated
        );
        assert_eq!(t.pending_len(), 1);
        // Live current still refreshes to latest timestamp.
        assert_eq!(t.current_context().unwrap().observed_at_ms, 200);
    }

    #[test]
    fn enqueue_overflow_is_explicit_no_silent_drop() {
        let mut t = Tracker::new();
        for i in 0..MAX_PENDING_OBSERVATIONS {
            let c = ctx_with(Some("0x1"), &format!("t{i}"), Some("1"), i as i64);
            // Titles differ so each is meaningful; queue must grow.
            match t.enqueue(c).expect("queue") {
                EnqueueOutcome::Queued(_, _) => {}
                EnqueueOutcome::Deduplicated => panic!("titles differ, must queue"),
            }
        }
        assert_eq!(t.pending_len(), MAX_PENDING_OBSERVATIONS);
        let overflow = ctx_with(Some("0x9"), "overflow", Some("1"), 9999);
        let err = t.enqueue(overflow).expect_err("must surface overflow");
        assert_eq!(err.capacity, MAX_PENDING_OBSERVATIONS);
        // Nothing silently dropped: length unchanged, live advanced.
        assert_eq!(t.pending_len(), MAX_PENDING_OBSERVATIONS);
        assert_eq!(t.current_context().unwrap().observed_at_ms, 9999);
    }
}
