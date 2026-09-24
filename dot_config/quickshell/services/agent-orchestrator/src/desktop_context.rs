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
/// Bounds for optional application resource fields.
pub const MAX_RESOURCE_ADAPTER_CHARS: usize = 64;
pub const MAX_RESOURCE_PATH_CHARS: usize = 4096;
pub const MAX_RESOURCE_BRANCH_CHARS: usize = 256;
pub const MAX_RESOURCE_URL_CHARS: usize = 2048;
pub const MAX_RESOURCE_PAGE_CHARS: usize = 1024;

/// Focused window: `id` is opaque (compositor-assigned), never interpreted.
/// `process_id` is the optional OS PID of the focused client (Hyprland `pid`
/// field). It is correlation metadata for application enrichment only and is
/// deliberately ignored by semantic equality / classification so PID-only
/// differences never produce history rows on their own.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct FocusedWindow {
    pub id: String,
    pub application: String,
    pub title: String,
    #[serde(default)]
    pub process_id: Option<u32>,
}

impl FocusedWindow {
    pub fn new(id: &str, application: &str, title: &str) -> Self {
        Self {
            id: bound_chars(id, MAX_ID_CHARS),
            application: bound_chars(application, MAX_APP_CHARS),
            title: bound_chars(title, MAX_TITLE_CHARS),
            process_id: None,
        }
    }

    pub fn new_with_pid(id: &str, application: &str, title: &str, process_id: Option<u32>) -> Self {
        Self {
            id: bound_chars(id, MAX_ID_CHARS),
            application: bound_chars(application, MAX_APP_CHARS),
            title: bound_chars(title, MAX_TITLE_CHARS),
            process_id,
        }
    }

    /// Semantic equality for history/dedup: opaque id + human strings only.
    /// `process_id` is correlation metadata and never meaningful alone.
    pub fn semantic_eq(&self, other: &Self) -> bool {
        self.id == other.id && self.application == other.application && self.title == other.title
    }
}

/// Optional typed application resource attached to a desktop snapshot.
///
/// Generic and additive: Phase 1 JSON without `resource` deserializes to
/// `None`. `adapter` records provenance (e.g. `neovim`, `kitty`,
/// `zen-title`, `logseq`, `logseq-title`, `zotero`) so fallbacks stay honest
/// and labeled. Only meaningful location fields participate in semantic
/// equality; provider timestamps/diagnostics must never live here (they
/// would defeat dedup).
///
/// `git_remote` is an additive project-resolution field: a normalized safe
/// GitHub repository URL (`https://github.com/owner/repo`, no credentials)
/// when already known by a provider. Phase 2 JSON without `git_remote`
/// reads as `None`. The project resolver may also discover the remote from
/// the local git config when this is absent; it never stores credentials.
///
/// `zotero` is the additive typed Zotero active-reader identity (Phase 6):
/// server/library/item/attachment plus current parent memberships and
/// ancestor collection keys, version, and a stable `zotero://` URI. Older
/// JSON without `zotero` reads as `None`. Only the stable document identity
/// (server+library+item/attachment+collections) participates in matching;
/// page turns, titles, and versions never affect identity or project
/// resolution.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct ResourceContext {
    #[serde(default)]
    pub adapter: String,
    #[serde(default)]
    pub file: Option<String>,
    #[serde(default)]
    pub cwd: Option<String>,
    #[serde(default)]
    pub git_root: Option<String>,
    #[serde(default)]
    pub git_branch: Option<String>,
    #[serde(default)]
    pub git_remote: Option<String>,
    #[serde(default)]
    pub url: Option<String>,
    #[serde(default)]
    pub page: Option<String>,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub zotero: Option<ZoteroContext>,
}

/// Typed Zotero active-reader identity (metadata only, never content).
///
/// - `server_id`: the `Zotero-Server-ID` header value (Zotero 10+ local API;
///   earlier servers report `""` and never match a pinned registry entry).
/// - `library_type`/`library_id`: `user`|`group` plus a digit string. The
///   registry alias `user/"0"` is server-bound (matches any user library on
///   the same server); all other values match exactly.
/// - `item_key`: stable parent (regular-item) key; `attachment_key` is the
///   opened attachment key when it differs (`None` when the reader opened
///   the parent directly).
/// - `collections`/`ancestor_collections`: current direct parent memberships
///   plus ancestor collection keys (each 8 uppercase alnum). Subcollection
///   matching checks ancestors only when the registry entry opts in.
/// - `version`: local object version (freshness/diagnostics only, never
///   identity or matching).
/// - `uri`: stable `zotero://select/...` URI for display/search only.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct ZoteroContext {
    #[serde(default)]
    pub server_id: String,
    #[serde(default)]
    pub library_type: String,
    #[serde(default)]
    pub library_id: String,
    #[serde(default)]
    pub item_key: String,
    #[serde(default)]
    pub attachment_key: Option<String>,
    #[serde(default)]
    pub collections: Vec<String>,
    #[serde(default)]
    pub ancestor_collections: Vec<String>,
    #[serde(default)]
    pub version: Option<i64>,
    #[serde(default)]
    pub uri: Option<String>,
}

impl ZoteroContext {
    /// True when no stable identity is present (nothing to match or dedup).
    pub fn is_empty(&self) -> bool {
        self.server_id.is_empty()
            || self.item_key.is_empty()
            || self.library_type.is_empty()
            || self.library_id.is_empty()
    }
}

impl ResourceContext {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        adapter: &str,
        file: Option<&str>,
        cwd: Option<&str>,
        git_root: Option<&str>,
        git_branch: Option<&str>,
        url: Option<&str>,
        page: Option<&str>,
        title: Option<&str>,
    ) -> Self {
        Self {
            adapter: bound_chars(adapter, MAX_RESOURCE_ADAPTER_CHARS),
            file: opt_bound(file, MAX_RESOURCE_PATH_CHARS),
            cwd: opt_bound(cwd, MAX_RESOURCE_PATH_CHARS),
            git_root: opt_bound(git_root, MAX_RESOURCE_PATH_CHARS),
            git_branch: opt_bound(git_branch, MAX_RESOURCE_BRANCH_CHARS),
            git_remote: None,
            url: opt_bound(url, MAX_RESOURCE_URL_CHARS),
            page: opt_bound(page, MAX_RESOURCE_PAGE_CHARS),
            title: opt_bound(title, MAX_TITLE_CHARS),
            zotero: None,
        }
    }

    /// Attach a normalized safe GitHub remote URL (no credentials).
    /// Malformed/untrusted values clear to `None`; overlong values truncate
    /// at a char boundary via the same bound as `url`.
    pub fn with_git_remote(mut self, remote: Option<&str>) -> Self {
        self.git_remote = remote
            .filter(|s| !s.is_empty())
            .and_then(|s| crate::project_context::normalize_github_remote(s))
            .and_then(|n| opt_bound(Some(n.as_str()), MAX_RESOURCE_URL_CHARS));
        self
    }

    /// Attach a validated typed Zotero identity. `None` or empty identities
    /// clear to `None` so explicit struct literals keep compiling.
    pub fn with_zotero(mut self, zotero: Option<ZoteroContext>) -> Self {
        self.zotero = zotero.filter(|z| !z.is_empty());
        self
    }

    /// True when every meaningful location field is empty. Adapter-only
    /// resources carry no signal and are treated as absent by callers.
    /// A present non-empty typed Zotero identity counts as meaningful even
    /// when all path/URL fields are absent.
    pub fn is_empty(&self) -> bool {
        self.file.is_none()
            && self.cwd.is_none()
            && self.git_root.is_none()
            && self.git_branch.is_none()
            && self.git_remote.is_none()
            && self.url.is_none()
            && self.page.is_none()
            && self.title.is_none()
            && self.zotero.as_ref().map(|z| z.is_empty()).unwrap_or(true)
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

/// Deterministic project association for a desktop snapshot.
///
/// Additive and explainable: only the stable project identity (`id`, `name`)
/// plus the match strength (`matched_by`: `zotero_collection`/`file`/`cwd`/
/// `git_root`/`logseq_page`/`git_remote` ONLY) participate in semantic
/// equality. The registry `revision` is
/// deliberately excluded so identical mappings at different revisions do not
/// flap history. Old JSON without `project` reads as `None`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProjectContext {
    pub id: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub matched_by: String,
}

impl ProjectContext {
    pub fn new(id: &str, name: &str, matched_by: &str) -> Self {
        Self {
            id: bound_chars(id, MAX_ID_CHARS),
            name: bound_chars(name, MAX_APP_CHARS),
            matched_by: bound_chars(matched_by, MAX_RESOURCE_ADAPTER_CHARS),
        }
    }
}

/// Live desktop context snapshot (serializable).
///
/// `resource` is an additive Phase 2 field: missing in Phase 1 JSON reads as
/// `None`, and `Some` values round-trip through the same `activity` table
/// without any schema change. `source` stays compositor-only (Hyprland);
/// per-application provenance lives in `resource.adapter`.
///
/// `project` is the deterministic project resolution overlay: missing in
/// older JSON reads as `None` and participates in semantic equality
/// (id/name/matched_by only, never the registry revision).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct DesktopContext {
    pub focused_window: Option<FocusedWindow>,
    pub workspace: Option<Workspace>,
    pub available: bool,
    pub source: Source,
    /// UTC epoch milliseconds of observation.
    pub observed_at_ms: i64,
    #[serde(default)]
    pub resource: Option<ResourceContext>,
    #[serde(default)]
    pub project: Option<ProjectContext>,
    /// Focusless-retention marker: `Some(first_focusless_ms)` where retention
    /// was applied against a previous context with signal (a focused window
    /// or a non-empty resource/project — typically carrying that previous
    /// context's `resource`/`project` onto this focusless snapshot inside
    /// the bounded window; see [`retain_focusless`]). `None` on real window
    /// observations and on honest (expired/unretained) focusless rows.
    ///
    /// Additive Phase 2 field: missing in older JSON reads as `None`, and it
    /// round-trips through the same `activity` table without any schema
    /// change. Intentionally excluded from [`DesktopContext::semantic_eq`]
    /// so repeated retained rows dedup against the tail.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub retained_since_ms: Option<i64>,
    /// Focusless-streak clock: `Some(streak_start_ms)` on EVERY available
    /// focusless observation, whether a project/resource is currently being
    /// carried or not. It records the UTC-ms start of the current
    /// uninterrupted focusless streak so expiry stays durable once the
    /// lookback skips project-less rows (see [`retain_focusless`]).
    ///
    /// `None` on real window observations and unavailable snapshots (either
    /// ends the streak and clears both markers). Additive: missing in older
    /// JSON reads as `None`, no schema/SQL change, and excluded from
    /// [`DesktopContext::semantic_eq`] so streak bookkeeping never defeats
    /// dedup.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub focusless_since_ms: Option<i64>,
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
            resource: None,
            project: None,
            retained_since_ms: None,
            focusless_since_ms: None,
        }
    }

    pub fn available_with_resource(
        source: Source,
        focused_window: Option<FocusedWindow>,
        workspace: Option<Workspace>,
        observed_at_ms: i64,
        resource: Option<ResourceContext>,
    ) -> Self {
        let resource = resource.filter(|r| !r.is_empty());
        Self {
            focused_window,
            workspace,
            available: true,
            source,
            observed_at_ms,
            resource,
            project: None,
            retained_since_ms: None,
            focusless_since_ms: None,
        }
    }

    pub fn unavailable(source: Source, observed_at_ms: i64) -> Self {
        Self {
            focused_window: None,
            workspace: None,
            available: false,
            source,
            observed_at_ms,
            resource: None,
            project: None,
            retained_since_ms: None,
            focusless_since_ms: None,
        }
    }

    /// Semantic equality: everything except the observation timestamp,
    /// the focused-window PID, and the focusless-retention markers.
    /// Focused windows compare on opaque
    /// id/application/title only; resource compares on meaningful location
    /// fields; project compares on stable identity (id/name/matched_by
    /// only, never the registry revision) so identical mappings do not flap.
    /// `retained_since_ms` and `focusless_since_ms` are deliberately ignored
    /// so a retained focusless row dedups against the tail instead of
    /// appending on every event, and so a real window event (which clears
    /// both markers) still compares on user meaning alone.
    /// Provider timestamps/diagnostics must never be stored here.
    /// Used for dedup so paired/duplicate events do not append history.
    pub fn semantic_eq(&self, other: &Self) -> bool {
        windows_opt_semantic_eq(self.focused_window.as_ref(), other.focused_window.as_ref())
            && self.workspace == other.workspace
            && self.available == other.available
            && self.source == other.source
            && self.resource == other.resource
            && self.project == other.project
    }

    /// Attach (or clear) enrichment, normalizing adapter-only shells to None.
    /// Any resource change clears a possibly-stale project: the project
    /// resolver re-derives it from the new resource, and stripped worker
    /// inputs (`None`) must never carry a previous focus's project.
    /// It also clears both focusless markers (`retained_since_ms` and
    /// `focusless_since_ms`): persistence re-derives retention via
    /// [`retain_focusless`], so a stripped or refreshed resource never
    /// carries a stale window.
    pub fn with_resource(mut self, resource: Option<ResourceContext>) -> Self {
        self.resource = resource.filter(|r| !r.is_empty());
        self.project = None;
        self.retained_since_ms = None;
        self.focusless_since_ms = None;
        self
    }

    /// Attach (or clear) the deterministic project overlay.
    pub fn with_project(mut self, project: Option<ProjectContext>) -> Self {
        self.project = project;
        self
    }
}

/// Focusless retention: keep the current project across brief compositor
/// focus gaps (e.g. a Quickshell layer-surface stealing focus), instead of
/// clearing it the moment `focused_window` reads `None`.
///
/// A focusless candidate (`available`, no focused window) carries the
/// previous context's `resource` + `project` while the bounded retention
/// window is active. The persisted row keeps `focused_window: None`
/// (honest: no window is focused) but the retained project keeps the
/// session open (`classify_transition` emits `focus`, `resolved_project_id`
/// stays the project).
///
/// Retention applies only when all of these hold:
/// - the candidate is available and has no focused window;
/// - a previous context exists and is available;
/// - the previous workspace equals the candidate workspace;
/// - the previous context has something to retain (a focused window, or a
///   non-empty resource/project);
/// - the total focusless period has not exceeded `retention_ms`.
///
/// Two markers are maintained:
/// - `retained_since_ms`: `Some(first_focusless_ms)` where retention was
///   applied against a previous context with signal (a focused window or a
///   non-empty resource/project — typically carrying that `resource` /
///   `project`). `None` on real window observations, unavailable snapshots,
///   and honestly-cleared focusless rows (expired, unretained,
///   workspace-mismatched).
/// - `focusless_since_ms`: the UTC-ms start of the current uninterrupted
///   focusless streak, set on EVERY available focusless observation —
///   retained or honestly cleared. Windowed and unavailable observations
///   clear both markers. This is what makes expiry durable: once the
///   lookback skips project-less rows, an expired project-less focusless
///   row stops being a latch only if its streak clock survives on the row
///   itself; the next observation re-reads that clock and stays expired
///   instead of re-anchoring on an older signal row and resurrecting the
///   project forever.
///
/// The window bounds the *total* focusless period, not each event. The
/// effective anchor is, in precedence order: the explicit `streak_anchor_ms`
/// passed to [`retain_focusless_with_anchor`] (the live path threads the
/// newest available focusless row's `focusless_since_ms` here, even when
/// that row carries no project), else the previous row's
/// `focusless_since_ms`, else its legacy `retained_since_ms`, else — for a
/// legacy bare focusless previous with no markers at all — its
/// `observed_at_ms`, else (windowed previous / fresh streak) the
/// candidate's own `observed_at_ms`. Retention expires once
/// `candidate.observed_at_ms - anchor > retention_ms`, so repeated
/// focusless events never extend the window.
///
/// Re-anchoring a stale windowed row at the candidate time is intentional.
/// The newest persisted activity row is written only on semantically distinct
/// observations, so while a window stays focused for a long time (no
/// title/resource changes) the newest row can be minutes old even though the
/// collector's live `current_context()` stayed fresh via timestamp refreshes.
/// Anchoring at `previous.observed_at_ms` would therefore expire immediately
/// on the first live-path focusless poll and drop the current project — the
/// bug this retention exists to fix. When the collector is running it
/// promptly persists the retained row with both markers, so the *second*
/// focusless event already reads the streak clock and the total period stays
/// bounded; when the collector is not running the live path (`current` /
/// `current-project` via [`crate::collector::apply_focusless_retention`])
/// retains the last observed project while focusless by paging back past
/// un-enriched windowed rows to a carry-capable signal row (see that
/// function's docs for the window-change boundary). While the collector
/// process is NOT running the live path is read-only, so for a windowed
/// carry source there is no persisted streak clock and every poll re-anchors
/// fresh (the grace effectively does not expire until the collector
/// persists a row).
///
/// Every other case keeps the candidate's own resource/project as observed
/// with `retained_since_ms` cleared but `focusless_since_ms` set to the
/// streak anchor (fresh streaks anchor at the candidate time): unavailable
/// snapshots and windowed candidates (a real window event ends retention)
/// clear BOTH markers; workspace changes, previous contexts with nothing to
/// retain, and expired windows persist the honest focusless context with the
/// streak clock preserved exactly as observed.
///
/// Both markers are excluded from [`DesktopContext::semantic_eq`] so
/// bookkeeping never defeats dedup.
pub fn retain_focusless(
    previous: Option<&DesktopContext>,
    candidate: DesktopContext,
    retention_ms: i64,
) -> DesktopContext {
    retain_focusless_with_anchor(previous, candidate, retention_ms, None)
}

/// [`retain_focusless`] with an explicit streak anchor.
///
/// `streak_anchor_ms` is the start of the current uninterrupted focusless
/// streak as seen by the caller. The live DB path
/// ([`crate::collector::apply_focusless_retention`]) passes the newest
/// available focusless row's `focusless_since_ms` here (falling back to that
/// row's `observed_at_ms` for legacy rows), because the carry source it
/// passes as `previous` may be an older enriched row while the streak clock
/// lives on a newer project-less focusless row. In-memory collector paths
/// pass `None`, which derives the anchor from `previous` alone and behaves
/// exactly like the original [`retain_focusless`], plus streak-clock
/// bookkeeping. An explicit anchor takes precedence over every fallback;
/// legacy rows without either marker still behave sanely (fresh anchor at
/// the candidate time, or at the legacy focusless row's own time — see
/// body).
pub fn retain_focusless_with_anchor(
    previous: Option<&DesktopContext>,
    mut candidate: DesktopContext,
    retention_ms: i64,
    streak_anchor_ms: Option<i64>,
) -> DesktopContext {
    // Only available focusless snapshots are eligible; anything else ends
    // the streak and passes through with both markers cleared.
    if !candidate.available || candidate.focused_window.is_some() {
        candidate.retained_since_ms = None;
        candidate.focusless_since_ms = None;
        return candidate;
    }
    let Some(prev) = previous else {
        // Fresh streak: no previous to continue from.
        candidate.retained_since_ms = None;
        candidate.focusless_since_ms = Some(candidate.observed_at_ms);
        return candidate;
    };
    if !prev.available || prev.workspace != candidate.workspace {
        // Workspace change / unavailable previous: no carry, fresh streak.
        candidate.retained_since_ms = None;
        candidate.focusless_since_ms = Some(candidate.observed_at_ms);
        return candidate;
    }
    // Streak anchor: explicit (live-path newest focusless) wins, then the
    // previous row's own streak clock, then its legacy retain marker, then
    // — for a legacy bare focusless row with no markers — its own
    // observation time so the streak still advances instead of restarting.
    // A windowed previous with no explicit anchor is a fresh streak.
    let anchor = streak_anchor_ms
        .or(prev.focusless_since_ms)
        .or(prev.retained_since_ms)
        .or(if prev.focused_window.is_none() {
            Some(prev.observed_at_ms)
        } else {
            None
        })
        .unwrap_or(candidate.observed_at_ms);
    if candidate.observed_at_ms.saturating_sub(anchor) > retention_ms {
        // Expired: persist the honest focusless context (resource/project as
        // observed), carry marker cleared, streak clock preserved so the
        // next observation stays expired instead of resurrecting.
        candidate.retained_since_ms = None;
        candidate.focusless_since_ms = Some(anchor);
        return candidate;
    }
    let has_signal = prev.focused_window.is_some()
        || prev
            .resource
            .as_ref()
            .map(|r| !r.is_empty())
            .unwrap_or(false)
        || prev.project.is_some();
    if !has_signal {
        // Project-less focusless previous: nothing to carry, but the streak
        // continues — record its clock so a later observation expires at
        // `anchor + retention_ms`, not later.
        candidate.retained_since_ms = None;
        candidate.focusless_since_ms = Some(anchor);
        return candidate;
    }
    candidate.resource.clone_from(&prev.resource);
    candidate.project.clone_from(&prev.project);
    candidate.retained_since_ms = Some(anchor);
    candidate.focusless_since_ms = Some(anchor);
    candidate
}

fn windows_opt_semantic_eq(a: Option<&FocusedWindow>, b: Option<&FocusedWindow>) -> bool {
    match (a, b) {
        (None, None) => true,
        (Some(x), Some(y)) => x.semantic_eq(y),
        _ => false,
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
    /// Phase 2: same focused window/workspace, but the application resource
    /// (file/cwd/git root/branch/url/page/title) changed.
    Context,
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
            ActivityKind::Context => "context",
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
            "context" => ActivityKind::Context,
            "resource" => ActivityKind::Context,
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
            // Deliberately ignore process_id-only differences here and in
            // semantic_eq: PID is correlation metadata, not user meaning.
        }
    }
    if prev.workspace != next.workspace {
        return ActivityKind::Workspace;
    }
    if prev.resource != next.resource || prev.project != next.project {
        return ActivityKind::Context;
    }
    // Same window id/title, same workspace, same resource/project but
    // something else changed (should be rare given semantic_eq gate):
    // generic snapshot.
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

fn opt_bound(v: Option<&str>, max_chars: usize) -> Option<String> {
    match v {
        None => None,
        Some(s) if s.is_empty() => None,
        Some(s) => Some(bound_chars(s, max_chars)),
    }
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
    fn retain_focusless_within_ttl_carries_resource_and_project() {
        let proj = ProjectContext::new("proj-1", "P", "file");
        let res = ResourceContext::new(
            "neovim",
            Some("/repo/note.md"),
            None,
            None,
            None,
            None,
            None,
            None,
        );
        let prev = DesktopContext::available(
            Source::Hyprland,
            Some(FocusedWindow::new("0x1", "kitty", "t")),
            Some(Workspace::new("1", "1")),
            1000,
        )
        .with_resource(Some(res))
        .with_project(Some(proj));
        let candidate =
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 1050);
        let out = retain_focusless(Some(&prev), candidate, 120_000);
        assert!(
            out.focused_window.is_none(),
            "retained rows stay honestly focusless"
        );
        assert!(out.available);
        assert_eq!(out.project.as_ref().map(|p| p.id.as_str()), Some("proj-1"));
        assert!(out.resource.is_some());
        // A windowed previous row means focuslessness is first observed now:
        // the window anchors at the candidate time, not the stale row time.
        assert_eq!(out.retained_since_ms, Some(1050));
    }

    #[test]
    fn retain_focusless_expiry_uses_first_focusless_time_not_each_event() {
        let proj = ProjectContext::new("proj-1", "P", "file");
        let prev_windowed = DesktopContext::available(
            Source::Hyprland,
            Some(FocusedWindow::new("0x1", "kitty", "t")),
            Some(Workspace::new("1", "1")),
            1000,
        )
        .with_project(Some(proj.clone()));
        // First focusless event at t=1010 retains, anchored at the first
        // focusless observation (t=1010), NOT at the stale windowed row time.
        let first = retain_focusless(
            Some(&prev_windowed),
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 1010),
            100,
        );
        assert_eq!(first.retained_since_ms, Some(1010));
        assert!(first.project.is_some());
        // Second focusless at t=1050: only 40 ms after the anchor, so it
        // still retains with the same anchor — repeated events never extend
        // the window.
        let second = retain_focusless(
            Some(&first),
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 1050),
            100,
        );
        assert_eq!(second.retained_since_ms, Some(1010));
        assert!(second.project.is_some());
        // ...but t=1120 is 110 ms past the anchor and must expire even
        // though it is recent relative to the previous retained row.
        let expired = retain_focusless(
            Some(&second),
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 1120),
            100,
        );
        assert!(
            expired.project.is_none(),
            "expired retention clears the project"
        );
        assert!(expired.resource.is_none());
        assert_eq!(expired.retained_since_ms, None);
    }

    #[test]
    fn retain_focusless_stale_windowed_previous_anchors_at_candidate() {
        // Live-path/DB-row regression: the newest persisted windowed row is
        // written only on semantically distinct observations, so it can be
        // minutes old while the window stayed focused. The first focusless
        // observation after it must still retain (anchored at the candidate
        // time), not expire immediately against the stale row timestamp.
        let proj = ProjectContext::new("proj-1", "P", "file");
        let res = ResourceContext::new(
            "neovim",
            Some("/repo/note.md"),
            None,
            None,
            None,
            None,
            None,
            None,
        );
        let stale = DesktopContext::available(
            Source::Hyprland,
            Some(FocusedWindow::new("0x1", "kitty", "t")),
            Some(Workspace::new("1", "1")),
            1_000,
        )
        .with_resource(Some(res))
        .with_project(Some(proj));
        let candidate_time = 1_000 + 600_000;
        let candidate = DesktopContext::available(
            Source::Hyprland,
            None,
            Some(Workspace::new("1", "1")),
            candidate_time,
        );
        let out = retain_focusless(Some(&stale), candidate, 120_000);
        assert!(
            out.focused_window.is_none(),
            "retained rows stay honestly focusless"
        );
        assert_eq!(out.project.as_ref().map(|p| p.id.as_str()), Some("proj-1"));
        assert!(out.resource.is_some());
        assert_eq!(out.retained_since_ms, Some(candidate_time));
    }

    #[test]
    fn retain_focusless_expiry_boundary() {
        // The boundary is measured from the first focusless time: build the
        // retained row first (anchor = first candidate time), then probe the
        // edge against that marker. A windowed previous always anchors at the
        // candidate time itself (delta 0), so it can never be past-boundary.
        let prev = DesktopContext::available(
            Source::Hyprland,
            Some(FocusedWindow::new("0x1", "kitty", "t")),
            Some(Workspace::new("1", "1")),
            1000,
        )
        .with_project(Some(ProjectContext::new("proj-1", "P", "file")));
        let first = retain_focusless(
            Some(&prev),
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 1000),
            100,
        );
        assert_eq!(first.retained_since_ms, Some(1000));
        // Exactly at the window: `now - since > retention` is false, retain.
        let at = retain_focusless(
            Some(&first),
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 1100),
            100,
        );
        assert!(at.project.is_some());
        assert_eq!(at.retained_since_ms, Some(1000));
        // One ms past: expire.
        let past = retain_focusless(
            Some(&first),
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 1101),
            100,
        );
        assert!(past.project.is_none());
        assert_eq!(past.retained_since_ms, None);
    }

    #[test]
    fn retain_focusless_rejects_workspace_change_empty_previous_and_unavailable() {
        let prev = DesktopContext::available(
            Source::Hyprland,
            Some(FocusedWindow::new("0x1", "kitty", "t")),
            Some(Workspace::new("1", "1")),
            1000,
        )
        .with_project(Some(ProjectContext::new("proj-1", "P", "file")));
        // Workspace change: no retention.
        let moved = retain_focusless(
            Some(&prev),
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("2", "2")), 1010),
            120_000,
        );
        assert!(moved.project.is_none());
        assert_eq!(moved.retained_since_ms, None);
        // Previous with nothing to retain (no window, no resource/project).
        let bare =
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 1000);
        let none = retain_focusless(
            Some(&bare),
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 1010),
            120_000,
        );
        assert!(none.project.is_none());
        assert_eq!(none.retained_since_ms, None);
        // No previous at all.
        let fresh = retain_focusless(
            None,
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 1010),
            120_000,
        );
        assert!(fresh.project.is_none());
        assert_eq!(fresh.retained_since_ms, None);
        // Unavailable previous.
        let un_prev = DesktopContext::unavailable(Source::Hyprland, 1000);
        let un = retain_focusless(
            Some(&un_prev),
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 1010),
            120_000,
        );
        assert!(un.project.is_none());
        assert_eq!(un.retained_since_ms, None);
    }

    #[test]
    fn retain_focusless_never_touches_unavailable_or_windowed_candidates() {
        let prev = DesktopContext::available(
            Source::Hyprland,
            Some(FocusedWindow::new("0x1", "kitty", "t")),
            Some(Workspace::new("1", "1")),
            1000,
        )
        .with_project(Some(ProjectContext::new("proj-1", "P", "file")));
        // Unavailable snapshots must NOT retain.
        let un = retain_focusless(
            Some(&prev),
            DesktopContext::unavailable(Source::Hyprland, 1010),
            120_000,
        );
        assert!(!un.available);
        assert!(un.project.is_none());
        assert_eq!(un.retained_since_ms, None);
        // Windowed candidates pass through with the marker cleared (a real
        // window event ends retention), keeping their own project.
        let mut windowed = DesktopContext::available(
            Source::Hyprland,
            Some(FocusedWindow::new("0x2", "kitty", "t2")),
            Some(Workspace::new("1", "1")),
            1010,
        )
        .with_project(Some(ProjectContext::new("proj-2", "Q", "file")));
        windowed.retained_since_ms = Some(500);
        let out = retain_focusless(Some(&prev), windowed, 120_000);
        assert_eq!(out.project.as_ref().map(|p| p.id.as_str()), Some("proj-2"));
        assert_eq!(out.retained_since_ms, None);
    }

    #[test]
    fn retain_focusless_marks_dedup_against_tail() {
        // A retained row is semantically equal to the previous windowed row
        // modulo the (ignored) marker... except focused_window differs, so a
        // first focusless event is a real `focus` transition; repeats dedup.
        let prev = DesktopContext::available(
            Source::Hyprland,
            Some(FocusedWindow::new("0x1", "kitty", "t")),
            Some(Workspace::new("1", "1")),
            1000,
        )
        .with_project(Some(ProjectContext::new("proj-1", "P", "file")));
        let first = retain_focusless(
            Some(&prev),
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 1010),
            120_000,
        );
        assert!(
            !prev.semantic_eq(&first),
            "window -> focusless is a real transition"
        );
        let repeat = retain_focusless(
            Some(&first),
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 1020),
            120_000,
        );
        assert!(first.semantic_eq(&repeat), "repeated retained rows dedup");
        assert_eq!(repeat.retained_since_ms, Some(1010));
    }

    #[test]
    fn retain_focusless_sets_streak_clock_on_every_focusless_observation() {
        // `focusless_since_ms` is the streak start on retained AND on
        // honestly-cleared rows; windowed/unavailable clear both.
        let prev = DesktopContext::available(
            Source::Hyprland,
            Some(FocusedWindow::new("0x1", "kitty", "t")),
            Some(Workspace::new("1", "1")),
            1000,
        )
        .with_project(Some(ProjectContext::new("proj-1", "P", "file")));
        // Retained: both markers equal the anchor.
        let kept = retain_focusless(
            Some(&prev),
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 1010),
            120_000,
        );
        assert_eq!(kept.retained_since_ms, Some(1010));
        assert_eq!(kept.focusless_since_ms, Some(1010));
        // Expired: carry cleared, streak clock preserved.
        let expired = retain_focusless(
            Some(&kept),
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 1010 + 120_001),
            120_000,
        );
        assert_eq!(expired.retained_since_ms, None);
        assert_eq!(expired.focusless_since_ms, Some(1010));
        assert!(expired.project.is_none());
        // Bare previous (no signal): nothing carried, streak continues from
        // the previous observation time.
        let bare =
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 2000);
        let continued = retain_focusless(
            Some(&bare),
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 2010),
            120_000,
        );
        assert_eq!(continued.retained_since_ms, None);
        assert_eq!(continued.focusless_since_ms, Some(2000));
        assert!(continued.project.is_none());
        // Fresh streak (no previous) anchors at the candidate time.
        let fresh = retain_focusless(
            None,
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 3000),
            120_000,
        );
        assert_eq!(fresh.retained_since_ms, None);
        assert_eq!(fresh.focusless_since_ms, Some(3000));
        // Windowed and unavailable candidates clear both markers.
        let mut windowed = ctx_with(Some("0x2"), "t2", Some("1"), 3010);
        windowed.retained_since_ms = Some(1);
        windowed.focusless_since_ms = Some(1);
        let out = retain_focusless(Some(&prev), windowed, 120_000);
        assert_eq!(out.retained_since_ms, None);
        assert_eq!(out.focusless_since_ms, None);
        let un = retain_focusless(
            Some(&prev),
            DesktopContext::unavailable(Source::Hyprland, 3020),
            120_000,
        );
        assert_eq!(un.retained_since_ms, None);
        assert_eq!(un.focusless_since_ms, None);
    }

    #[test]
    fn retain_focusless_streak_anchor_survives_projectless_row() {
        // Two consecutive focusless observations where the first is
        // persisted WITHOUT a carry still expire at `anchor + retention`,
        // not later: the first bare row records the streak clock, the
        // second re-reads it.
        let bare =
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 1000);
        let first = retain_focusless(
            Some(&bare),
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 1010),
            100,
        );
        // Nothing carried, but the streak started at the bare row's time.
        assert!(first.project.is_none());
        assert_eq!(first.retained_since_ms, None);
        assert_eq!(first.focusless_since_ms, Some(1000));
        // Inside the window anchored at 1000: still no carry, same anchor.
        let inside = retain_focusless(
            Some(&first),
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 1050),
            100,
        );
        assert_eq!(inside.focusless_since_ms, Some(1000));
        assert_eq!(inside.retained_since_ms, None);
        // Past `anchor + retention`: still project-less with the SAME anchor
        // (never extended by the project-less intermediate row).
        let expired = retain_focusless(
            Some(&inside),
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 1101),
            100,
        );
        assert!(expired.project.is_none());
        assert_eq!(expired.retained_since_ms, None);
        assert_eq!(
            expired.focusless_since_ms,
            Some(1000),
            "project-less rows must not extend the window"
        );
    }

    #[test]
    fn retain_focusless_with_anchor_explicit_wins_for_live_path() {
        // The live path threads the newest focusless row's clock explicitly
        // while the carry source is an older enriched row: expiry is judged
        // against the streak, not the source.
        let mut source = ctx_with(Some("0x1"), "t", Some("1"), 1000);
        source.resource = Some(ResourceContext::new(
            "neovim",
            Some("/repo/note.md"),
            None,
            None,
            None,
            None,
            None,
            None,
        ));
        source.project = Some(ProjectContext::new("proj-1", "P", "file"));
        // Streak started at 5000 (newer project-less focusless row); the
        // candidate at 5050 is inside a 100 ms window anchored at 5000.
        let kept = retain_focusless_with_anchor(
            Some(&source),
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 5050),
            100,
            Some(5000),
        );
        assert_eq!(kept.project.as_ref().map(|p| p.id.as_str()), Some("proj-1"));
        assert_eq!(kept.retained_since_ms, Some(5000));
        assert_eq!(kept.focusless_since_ms, Some(5000));
        // Same streak, candidate past the window: expired even though the
        // source row itself is fresh relative to the candidate.
        let expired = retain_focusless_with_anchor(
            Some(&source),
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 5200),
            100,
            Some(5000),
        );
        assert!(expired.project.is_none());
        assert_eq!(expired.retained_since_ms, None);
        assert_eq!(expired.focusless_since_ms, Some(5000));
    }

    #[test]
    fn retain_focusless_legacy_rows_without_markers() {
        // Older JSON without either marker must still deserialize (additive
        // fields default to None) and behave sanely with a fresh anchor.
        let legacy_json = r#"{"focused_window":{"id":"0x1","application":"kitty","title":"t"},"workspace":{"id":"1","name":"1"},"available":true,"source":"hyprland","observed_at_ms":1000,"resource":null,"project":null}"#;
        let legacy: DesktopContext = serde_json::from_str(legacy_json).expect("legacy deserializes");
        assert_eq!(legacy.retained_since_ms, None);
        assert_eq!(legacy.focusless_since_ms, None);
        let legacy_focusless_json = r#"{"focused_window":null,"workspace":{"id":"1","name":"1"},"available":true,"source":"hyprland","observed_at_ms":1000}"#;
        let legacy_focusless: DesktopContext =
            serde_json::from_str(legacy_focusless_json).expect("legacy focusless deserializes");
        assert_eq!(legacy_focusless.focusless_since_ms, None);
        // A legacy windowed previous anchors a first focusless observation
        // at the candidate time (fresh streak).
        let out = retain_focusless(
            Some(&legacy),
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 600_000),
            120_000,
        );
        // Legacy windowed row names a window (signal) but carries no
        // resource/project, so the markers anchor fresh at the candidate
        // with an empty carry.
        assert_eq!(out.retained_since_ms, Some(600_000));
        assert_eq!(out.focusless_since_ms, Some(600_000));
        // A legacy bare focusless previous continues from its own time.
        let cont = retain_focusless(
            Some(&legacy_focusless),
            DesktopContext::available(Source::Hyprland, None, Some(Workspace::new("1", "1")), 1010),
            120_000,
        );
        assert_eq!(cont.focusless_since_ms, Some(1000));
    }

    #[test]
    fn focusless_markers_ignored_by_semantic_eq_and_cleared_by_with_resource() {
        let mut a = ctx_with(Some("0x1"), "t", Some("1"), 1000);
        let mut b = ctx_with(Some("0x1"), "t", Some("1"), 2000);
        a.retained_since_ms = Some(1000);
        a.focusless_since_ms = Some(1000);
        b.retained_since_ms = Some(1500);
        b.focusless_since_ms = Some(1500);
        assert!(a.semantic_eq(&b), "streak bookkeeping must never defeat dedup");
        // Resource refresh clears both markers so stale windows never ride.
        let cleared = a.with_resource(None);
        assert_eq!(cleared.retained_since_ms, None);
        assert_eq!(cleared.focusless_since_ms, None);
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
