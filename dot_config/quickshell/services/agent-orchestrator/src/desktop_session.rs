//! Phase 4 deterministic work sessions.
//!
//! Public session config, resource-identity normalization, and online
//! sessionization rules. SQL/schema integration lives in
//! [`crate::desktop_store`]; this module stays dependency-free beyond the
//! current crate.
//!
//! ## Session model
//!
//! - Raw activity snapshots stay append-only. Each v3 activity carries a
//!   stable 128-bit hex `event_id`, a durable `device_id`, and exactly one
//!   `session_id`.
//! - Sessions aggregate without copying full snapshots: project UUID/name,
//!   start/end bounds, first/last activity IDs, event count, open/closed
//!   status plus `ended_reason`, per-session thresholds, device provenance,
//!   and a distinct-application list derived from associated snapshots.
//! - Session resources are compact aggregates keyed by a stable identity
//!   (see [`resource_identity`]); occurrence counts increment per associated
//!   event. The raw event retains the full [`crate::desktop_context::ResourceContext`].
//!
//! ## Online / no-lookahead rule
//!
//! Sessionization is online: each new event is assigned using only the
//! persisted open session plus the new event. Earlier interruption events
//! that turn out (at a later event) to have lasted `>= grace` are NOT
//! retroactively reassigned — they remain in the prior session so stable
//! session IDs never shift. See [`decide_session_action`] docs.

use crate::desktop_context::{DesktopContext, ResourceContext};

/// Default inactivity gap: 30 minutes.
pub const DEFAULT_SESSION_GAP_MS: i64 = 30 * 60 * 1000;
/// Default unrelated/unresolved interruption grace: 2 minutes.
pub const DEFAULT_SESSION_INTERRUPTION_MS: i64 = 2 * 60 * 1000;
/// Sensible cap for the inactivity gap: 24 hours.
pub const MAX_SESSION_GAP_MS: i64 = 24 * 60 * 60 * 1000;
/// Minimum positive gap (tests may use small positive milliseconds).
pub const MIN_SESSION_GAP_MS: i64 = 1;
/// Env overrides (precedence: CLI > env > defaults; collect path only).
pub const SESSION_GAP_ENV: &str = "QS_DESKTOP_SESSION_GAP_MS";
pub const SESSION_INTERRUPTION_ENV: &str = "QS_DESKTOP_SESSION_INTERRUPTION_MS";

/// Per-session thresholds. Stored on each session row so historical
/// boundaries never change when the config changes.
///
/// Fields are private so every value passes through [`SessionConfig::new`]
/// validation; read them via the accessors.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SessionConfig {
    inactivity_gap_ms: i64,
    interruption_grace_ms: i64,
}

impl Default for SessionConfig {
    fn default() -> Self {
        Self {
            inactivity_gap_ms: DEFAULT_SESSION_GAP_MS,
            interruption_grace_ms: DEFAULT_SESSION_INTERRUPTION_MS,
        }
    }
}

impl SessionConfig {
    /// Validate bounded values.
    pub fn new(gap_ms: i64, interruption_ms: i64) -> Result<Self, String> {
        if gap_ms < MIN_SESSION_GAP_MS || gap_ms > MAX_SESSION_GAP_MS {
            return Err(format!(
                "session gap must be {MIN_SESSION_GAP_MS}..={MAX_SESSION_GAP_MS} ms, got {gap_ms}"
            ));
        }
        if interruption_ms < 0 || interruption_ms > gap_ms {
            return Err(format!(
                "session interruption grace must be 0..=gap ({gap_ms}) ms, got {interruption_ms}"
            ));
        }
        Ok(Self {
            inactivity_gap_ms: gap_ms,
            interruption_grace_ms: interruption_ms,
        })
    }

    /// Inactivity gap: a gap `>= gap` before the next event finalizes the
    /// open session as `inactivity`.
    pub fn inactivity_gap_ms(&self) -> i64 {
        self.inactivity_gap_ms
    }

    /// Unresolved/unavailable interruption grace. May be `0` (any second
    /// unresolved/return event splits); never exceeds the gap.
    pub fn interruption_grace_ms(&self) -> i64 {
        self.interruption_grace_ms
    }

    /// Parse one env field independently: absent/blank reads as `None`
    /// (caller falls back); present-but-malformed is `Err` (fail closed).
    fn parse_env_field(var: &str) -> Result<Option<i64>, String> {
        match std::env::var(var) {
            Err(_) => Ok(None),
            Ok(raw) => {
                let t = raw.trim();
                if t.is_empty() {
                    Ok(None)
                } else {
                    t.parse::<i64>()
                        .map(Some)
                        .map_err(|_| format!("{var} must be an integer (ms), got {raw:?}"))
                }
            }
        }
    }

    /// Defaults with env overrides applied (`QS_DESKTOP_SESSION_GAP_MS`,
    /// `QS_DESKTOP_SESSION_INTERRUPTION_MS`). Absent env reads as defaults;
    /// present-but-malformed is `Err` (fail closed). Only the final pair is
    /// bounds-validated.
    pub fn from_env() -> Result<Self, String> {
        let gap = Self::parse_env_field(SESSION_GAP_ENV)?.unwrap_or(DEFAULT_SESSION_GAP_MS);
        let interrupt = Self::parse_env_field(SESSION_INTERRUPTION_ENV)?
            .unwrap_or(DEFAULT_SESSION_INTERRUPTION_MS);
        Self::new(gap, interrupt)
    }

    /// Resolve `CLI > env > defaults` independently per field. `None` means
    /// "not overridden on the CLI"; a CLI override bypasses its env var
    /// entirely (even a malformed one). Only the final pair is
    /// bounds-validated, so an intermediate-invalid env pair combined with a
    /// valid CLI override still resolves.
    pub fn resolve(cli_gap: Option<i64>, cli_interruption: Option<i64>) -> Result<Self, String> {
        let gap = match cli_gap {
            Some(v) => v,
            None => Self::parse_env_field(SESSION_GAP_ENV)?.unwrap_or(DEFAULT_SESSION_GAP_MS),
        };
        let interrupt = match cli_interruption {
            Some(v) => v,
            None => Self::parse_env_field(SESSION_INTERRUPTION_ENV)?
                .unwrap_or(DEFAULT_SESSION_INTERRUPTION_MS),
        };
        Self::new(gap, interrupt)
    }
}

/// Open/closed status stored on sessions.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SessionStatus {
    Open,
    Closed,
}

impl SessionStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            SessionStatus::Open => "open",
            SessionStatus::Closed => "closed",
        }
    }
    pub fn from_str(s: &str) -> Option<Self> {
        match s {
            "open" => Some(SessionStatus::Open),
            "closed" => Some(SessionStatus::Closed),
            _ => None,
        }
    }
}

/// Finalization reason stored on closed sessions.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EndedReason {
    /// Gap `>=` the session's stored inactivity gap.
    Inactivity,
    /// Resolved `A -> B` (or unresolved `None -> B`) project arrival.
    ProjectSwitch,
    /// A short unresolved run proved long (`>=` stored grace) at a later
    /// unresolved event or return-to-same-project event.
    Interruption,
}

impl EndedReason {
    pub fn as_str(&self) -> &'static str {
        match self {
            EndedReason::Inactivity => "inactivity",
            EndedReason::ProjectSwitch => "project_switch",
            EndedReason::Interruption => "interruption",
        }
    }
    pub fn from_str(s: &str) -> Option<Self> {
        match s {
            "inactivity" => Some(EndedReason::Inactivity),
            "project_switch" => Some(EndedReason::ProjectSwitch),
            "interruption" => Some(EndedReason::Interruption),
            _ => None,
        }
    }
}

/// Whether a resource identity is portable (project-relative file/dir, URL,
/// page) or explicitly machine-local (absolute file/cwd with no portable
/// anchor).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ResourceKind {
    Portable,
    Local,
}

impl ResourceKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            ResourceKind::Portable => "portable",
            ResourceKind::Local => "local",
        }
    }
    pub fn from_str(s: &str) -> Option<Self> {
        match s {
            "portable" => Some(ResourceKind::Portable),
            "local" => Some(ResourceKind::Local),
            _ => None,
        }
    }
}

/// Stable dedup identity for one meaningful resource location.
///
/// - `key` is the dedup key (`portable:<typed-identity>` or
///   `local:<typed-identity>`, typed so a file `a` never collides with a URL
///   `a`).
/// - `portable_identity` / `local_identity` keep the two families distinct
///   in the public/database model: exactly one is `Some`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ResourceIdentity {
    pub kind: ResourceKind,
    pub key: String,
    pub portable_identity: Option<String>,
    pub local_identity: Option<String>,
}

/// Normalize a git root for component-boundary checks: trailing slashes
/// trimmed except the filesystem root.
fn normalize_root(root: &str) -> String {
    if root == "/" {
        return "/".to_string();
    }
    let t = root.trim_end_matches('/');
    if t.is_empty() {
        "/".to_string()
    } else {
        t.to_string()
    }
}

/// Relative path of `path` under `root`, or `None` when outside.
/// Component boundaries honored (`/a/b` does not match `/a/bc/d`);
/// the root itself maps to `"."`.
fn relative_under(path: &str, root: &str) -> Option<String> {
    if path.is_empty() || root.is_empty() {
        return None;
    }
    let r = normalize_root(root);
    if r == "/" {
        if !path.starts_with('/') {
            return None;
        }
        let rel = path.trim_start_matches('/');
        // Trim trailing slashes for stability.
        let rel = rel.trim_end_matches('/');
        if rel.is_empty() {
            return Some(".".to_string());
        }
        return Some(rel.to_string());
    }
    if path == r {
        return Some(".".to_string());
    }
    let prefix = format!("{r}/");
    if let Some(rest) = path.strip_prefix(&prefix) {
        if rest.is_empty() {
            return Some(".".to_string());
        }
        // Stability: no trailing slashes in the portable form.
        let rel = rest.trim_end_matches('/');
        if rel.is_empty() {
            return Some(".".to_string());
        }
        return Some(rel.to_string());
    }
    None
}

/// Stable resource identity for aggregation.
///
/// `event_project_id` is the resolved (lowercase) project UUID of the event
/// being aggregated, or `None` for unresolved events.
///
/// Priority:
/// 0. Bound Zotero reader (stable document, never page turns): qualified
///    server/library plus parent item and opened attachment
///    (`portable:zotero:<server>:<libtype>:<libid>:item:<item>:att:<att>`).
///    Titles, versions, collections, and URIs never affect the key, so tab
///    switches between attachments of the same parent stay distinct while
///    page turns within one attachment dedup.
/// 1. File/directory under `git_root` (component boundaries; root itself is
///    `"."`). `file` wins over `cwd` when both anchor. The portable key is
///    anchored to avoid cross-repository collisions: the event project UUID
///    first (`portable:proj:<uuid>:file:<rel>`), otherwise the normalized
///    `git_remote` (`portable:remote:<remote>:file:<rel>`). When neither
///    anchor exists the location is explicitly machine-local and the
///    `git_root` is embedded (`local:file:<root>:<rel>`), so the same
///    relative path in different repositories never collides — even inside
///    one absorbed unresolved run.
/// 2. `url`, then `page` (globally meaningful portable identities).
/// 3. Otherwise explicitly machine-local absolute `file`, then `cwd`
///    (`local:file:<abs>` / `local:cwd:<abs>`).
///
/// Adapter/title/branch never affect identity, so the same location with a
/// different title still dedups. Empty/absent locations yield `None` (caller
/// skips aggregation). The raw event always retains the full
/// [`ResourceContext`]; only this compact identity plus the latest compact
/// metadata is aggregated per session.
pub fn resource_identity(
    res: &ResourceContext,
    event_project_id: Option<&str>,
) -> Option<ResourceIdentity> {
    // Stable Zotero document identity first: page turns, titles, versions,
    // collections, and URIs never affect the key. Different attachments of
    // the same parent stay distinct; the same attachment across title/version
    // changes dedups.
    if let Some(z) = res.zotero.as_ref().filter(|x| !x.is_empty()) {
        if crate::project_context::is_zotero_server_id(&z.server_id)
            && (z.library_type == "user" || z.library_type == "group")
            && crate::project_context::is_zotero_library_id(&z.library_id)
            && !z.item_key.is_empty()
            && !z.item_key.contains('\0')
        {
            let att = z
                .attachment_key
                .as_deref()
                .filter(|s| !s.is_empty())
                .unwrap_or("-");
            // Canonical portable form: `zotero:<server>:<type>/<lib>:item:<item>:att:<att>`.
            let portable = format!(
                "zotero:{}:{}/{}:item:{}:att:{}",
                z.server_id, z.library_type, z.library_id, z.item_key, att
            );
            return Some(ResourceIdentity {
                kind: ResourceKind::Portable,
                key: format!("portable:{portable}"),
                portable_identity: Some(portable),
                local_identity: None,
            });
        }
    }
    let file = res.file.as_deref().filter(|s| !s.is_empty());
    let cwd = res.cwd.as_deref().filter(|s| !s.is_empty());
    let git_root = res.git_root.as_deref().filter(|s| !s.is_empty());
    let url = res.url.as_deref().filter(|s| !s.is_empty());
    let page = res.page.as_deref().filter(|s| !s.is_empty());

    if let Some(root) = git_root {
        let anchored = |typed: &str| -> ResourceIdentity {
            let portable = typed.to_string();
            if let Some(pid) = event_project_id.filter(|s| !s.is_empty()) {
                return ResourceIdentity {
                    kind: ResourceKind::Portable,
                    key: format!("portable:proj:{}:{typed}", pid.to_lowercase()),
                    portable_identity: Some(portable),
                    local_identity: None,
                };
            }
            if let Some(remote) = res
                .git_remote
                .as_deref()
                .filter(|s| !s.is_empty())
                .and_then(|r| crate::project_context::normalize_github_remote(r))
            {
                return ResourceIdentity {
                    kind: ResourceKind::Portable,
                    key: format!("portable:remote:{}:{typed}", remote.to_lowercase()),
                    portable_identity: Some(portable),
                    local_identity: None,
                };
            }
            // No project and no remote: explicitly machine-local, with the
            // git root embedded so identical relative paths in different
            // repositories stay distinct.
            let local = format!("{typed}@{}", normalize_root(root));
            ResourceIdentity {
                kind: ResourceKind::Local,
                key: format!("local:{local}"),
                portable_identity: None,
                local_identity: Some(local),
            }
        };
        if let Some(f) = file {
            if let Some(rel) = relative_under(f, root) {
                return Some(anchored(&format!("file:{rel}")));
            }
        }
        if let Some(c) = cwd {
            if let Some(rel) = relative_under(c, root) {
                return Some(anchored(&format!("cwd:{rel}")));
            }
        }
    }
    if let Some(u) = url {
        let portable = format!("url:{u}");
        return Some(ResourceIdentity {
            kind: ResourceKind::Portable,
            key: format!("portable:{portable}"),
            portable_identity: Some(portable),
            local_identity: None,
        });
    }
    if let Some(p) = page {
        let portable = format!("page:{p}");
        return Some(ResourceIdentity {
            kind: ResourceKind::Portable,
            key: format!("portable:{portable}"),
            portable_identity: Some(portable),
            local_identity: None,
        });
    }
    if let Some(f) = file {
        let local = format!("file:{f}");
        return Some(ResourceIdentity {
            kind: ResourceKind::Local,
            key: format!("local:{local}"),
            portable_identity: None,
            local_identity: Some(local),
        });
    }
    if let Some(c) = cwd {
        let local = format!("cwd:{c}");
        return Some(ResourceIdentity {
            kind: ResourceKind::Local,
            key: format!("local:{local}"),
            portable_identity: None,
            local_identity: Some(local),
        });
    }
    None
}

/// Resolved project id for sessionization: `Some(lowercase-id)` when the
/// snapshot is available and carries a non-empty project id, else `None`.
/// Unavailability is unresolved activity, never discarded.
pub fn resolved_project_id(ctx: &DesktopContext) -> Option<String> {
    if !ctx.available {
        return None;
    }
    ctx.project
        .as_ref()
        .map(|p| p.id.clone())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_lowercase())
}

/// Minimal view of the persisted open session needed for the pure decision.
#[derive(Clone, Debug)]
pub struct OpenSessionView {
    pub project_id: Option<String>,
    pub end_ms: i64,
    pub unresolved_start_ms: Option<i64>,
    pub gap_ms: i64,
    pub interruption_ms: i64,
}

/// Online assignment of one new event.
///
/// - Gap `>=` the open session's stored gap finalizes as `Inactivity`
///   (checked first, so a gapped project switch reports `inactivity`).
/// - Resolved `A -> B` (`B != A`) finalizes immediately as `ProjectSwitch`;
///   app/window/workspace/resource changes never split while the project id
///   stays `A`.
/// - A known-project session absorbs a short unresolved run: the first
///   unresolved event records `unresolved_start` and continues. While the run
///   is open, a switch to another known project still splits immediately as
///   `ProjectSwitch`. When a later unresolved event — or a return to the same
///   project — proves `new_ts - unresolved_start >= grace`, the open session
///   finalizes as `Interruption` and the later event starts the new session.
///   Earlier interruption events stay in the prior session (online,
///   no-lookahead: stable IDs never shift retroactively).
/// - Unresolved session `->` resolved project starts a new project session
///   (`ProjectSwitch`); unresolved `->` unresolved continues unless gapped.
/// - Timestamps drive gaps; equal timestamps order by activity ID upstream
///   (gap `0` continues). Clock rollback (`new_ts < end`) never splits and
///   never corrupts ranges (callers clamp `end` to `max`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SessionAction {
    /// Continue the open session. Carries the unresolved-run start to persist
    /// (`None` = no open run; `Some(t)` = run began at `t`).
    Continue { new_unresolved_start: Option<i64> },
    /// Finalize the open session with this reason and start a new session at
    /// the new event.
    Split { ended_reason: EndedReason },
}

pub fn decide_session_action(
    open: &OpenSessionView,
    new_project_id: Option<&str>,
    new_ts: i64,
) -> SessionAction {
    // Gap first (rollback-safe: negative gaps never split).
    let gap = new_ts.saturating_sub(open.end_ms);
    if gap >= open.gap_ms {
        return SessionAction::Split {
            ended_reason: EndedReason::Inactivity,
        };
    }
    let open_proj = open.project_id.as_deref().filter(|s| !s.is_empty());
    match (open_proj, new_project_id.filter(|s| !s.is_empty())) {
        (Some(a), Some(b)) => {
            if a.to_lowercase() != b.to_lowercase() {
                return SessionAction::Split {
                    ended_reason: EndedReason::ProjectSwitch,
                };
            }
            // Same project: grace applies only while an unresolved run is open.
            if let Some(start) = open.unresolved_start_ms {
                let run = new_ts.saturating_sub(start);
                if run >= open.interruption_ms {
                    return SessionAction::Split {
                        ended_reason: EndedReason::Interruption,
                    };
                }
                // Returned before grace expired: run ends, session continues.
                return SessionAction::Continue {
                    new_unresolved_start: None,
                };
            }
            SessionAction::Continue {
                new_unresolved_start: None,
            }
        }
        (Some(_), None) => {
            // First unresolved after resolved: open the run.
            if let Some(start) = open.unresolved_start_ms {
                let run = new_ts.saturating_sub(start);
                if run >= open.interruption_ms {
                    return SessionAction::Split {
                        ended_reason: EndedReason::Interruption,
                    };
                }
                return SessionAction::Continue {
                    new_unresolved_start: Some(start),
                };
            }
            SessionAction::Continue {
                new_unresolved_start: Some(new_ts),
            }
        }
        (None, Some(_)) => SessionAction::Split {
            ended_reason: EndedReason::ProjectSwitch,
        },
        (None, None) => SessionAction::Continue {
            new_unresolved_start: None,
        },
    }
}

/// Generate a random 128-bit hex identity (32 lowercase hex chars).
///
/// Uses `/dev/urandom` on Unix; falls back to a time/pid/counter mix hashed
/// through deterministic FNV when unavailable. Collisions are retry-checked
/// by callers via the UNIQUE index.
pub fn generate_128bit_hex() -> String {
    let mut buf = [0u8; 16];
    if read_urandom(&mut buf) {
        return hex32(&buf);
    }
    // Fallback: hash time + pid + counter (two FNV streams => 128 bits).
    use std::sync::atomic::{AtomicU64, Ordering};
    static CTR: AtomicU64 = AtomicU64::new(0);
    let n = CTR.fetch_add(1, Ordering::SeqCst);
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let pid = std::process::id() as u128;
    let mut data = Vec::with_capacity(48);
    data.extend_from_slice(&now.to_le_bytes());
    data.extend_from_slice(&pid.to_le_bytes());
    data.extend_from_slice(&n.to_le_bytes());
    data.extend_from_slice(b"qs-desktop-session-fallback");
    hash_128bit_hex(&data)
}

fn read_urandom(buf: &mut [u8]) -> bool {
    #[cfg(unix)]
    {
        use std::io::Read;
        if let Ok(mut f) = std::fs::File::open("/dev/urandom") {
            let mut filled = 0;
            while filled < buf.len() {
                match f.read(&mut buf[filled..]) {
                    Ok(0) => break,
                    Ok(n) => filled += n,
                    Err(_) => return false,
                }
            }
            return filled == buf.len();
        }
        false
    }
    #[cfg(not(unix))]
    {
        let _ = buf;
        false
    }
}

fn hex32(bytes: &[u8; 16]) -> String {
    const H: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(32);
    for b in bytes.iter() {
        s.push(H[(b >> 4) as usize] as char);
        s.push(H[(b & 0xf) as usize] as char);
    }
    s
}

/// Deterministic 128-bit hex from arbitrary bytes (dual FNV-1a 64).
/// Used for stable session IDs derived from `device_id:event_id`.
pub fn hash_128bit_hex(data: &[u8]) -> String {
    let h1 = fnv1a64(data, 0xcbf29ce484222325, 0x100000001b3);
    let h2 = fnv1a64(data, 0x84222325cbf29ce4, 0x100000001b3 ^ 0x9e3779b97f4a7c15);
    let mut buf = [0u8; 16];
    buf[..8].copy_from_slice(&h1.to_le_bytes());
    buf[8..].copy_from_slice(&h2.to_le_bytes());
    hex32(&buf)
}

fn fnv1a64(data: &[u8], mut h: u64, p: u64) -> u64 {
    for b in data {
        h ^= *b as u64;
        h = h.wrapping_mul(p);
    }
    // Final avalanche for short inputs.
    h ^= h >> 29;
    h = h.wrapping_mul(0xbf58476d1ce4e5b9);
    h ^= h >> 32;
    h
}

/// Deterministic stable session id for a session starting at `event_id` on
/// `device_id`. Survives restart/extension because the first event never
/// changes; later events never recompute it.
pub fn session_id_for(device_id: &str, event_id: &str) -> String {
    let mut data = Vec::with_capacity(device_id.len() + event_id.len() + 1);
    data.extend_from_slice(device_id.as_bytes());
    data.push(b':');
    data.extend_from_slice(event_id.as_bytes());
    hash_128bit_hex(&data)
}

/// Validate a 128-bit hex identity (32 hex chars, any case); returns the
/// lowercase canonical form.
pub fn normalize_hex_id(id: &str, what: &str) -> Result<String, String> {
    if id.len() != 32 {
        return Err(format!(
            "{what} must be 32 hex chars (128-bit), got {} chars",
            id.len()
        ));
    }
    if !id.bytes().all(|c| c.is_ascii_hexdigit()) {
        return Err(format!("{what} must be [0-9a-fA-F] only"));
    }
    Ok(id.to_lowercase())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn config_bounds() {
        assert!(SessionConfig::new(1, 0).is_ok());
        assert!(SessionConfig::new(1000, 1000).is_ok());
        assert!(SessionConfig::new(0, 0).is_err());
        assert!(SessionConfig::new(MAX_SESSION_GAP_MS + 1, 0).is_err());
        assert!(SessionConfig::new(1000, 1001).is_err());
        assert!(SessionConfig::new(1000, -1).is_err());
    }

    #[test]
    fn resource_identity_portable_file() {
        let r = ResourceContext::new(
            "neovim",
            Some("/repo/note.md"),
            Some("/repo"),
            Some("/repo"),
            Some("main"),
            None,
            None,
            Some("T"),
        );
        let id = resource_identity(&r, Some("proj-1")).unwrap();
        assert_eq!(id.kind, ResourceKind::Portable);
        assert_eq!(id.portable_identity.as_deref(), Some("file:note.md"));
        assert!(id.local_identity.is_none());
        // Adapter/title/branch ignored: same key.
        let r2 = ResourceContext::new(
            "kitty",
            Some("/repo/note.md"),
            Some("/other"),
            Some("/repo"),
            Some("feature"),
            None,
            None,
            Some("Other"),
        );
        assert_eq!(resource_identity(&r2, Some("proj-1")).unwrap().key, id.key);
    }

    #[test]
    fn resource_identity_project_anchor_distinguishes_repos() {
        let mk = |file: &str, root: &str| {
            ResourceContext::new(
                "nvim",
                Some(file),
                Some(root),
                Some(root),
                None,
                None,
                None,
                None,
            )
        };
        let a = resource_identity(&mk("/repo-a/note.md", "/repo-a"), Some("proj-a")).unwrap();
        let b = resource_identity(&mk("/repo-b/note.md", "/repo-b"), Some("proj-b")).unwrap();
        assert_eq!(a.kind, ResourceKind::Portable);
        assert_ne!(
            a.key, b.key,
            "same rel path in different projects must not collide"
        );
        // Same repo + same project still dedups.
        let a2 = resource_identity(&mk("/repo-a/note.md", "/repo-a"), Some("proj-a")).unwrap();
        assert_eq!(a.key, a2.key);
    }

    #[test]
    fn resource_identity_remote_anchor_without_project() {
        let mut r = ResourceContext::new(
            "nvim",
            Some("/repo/note.md"),
            Some("/repo"),
            Some("/repo"),
            None,
            None,
            None,
            None,
        );
        r = r.with_git_remote(Some("https://github.com/o/r"));
        let id = resource_identity(&r, None).unwrap();
        assert_eq!(id.kind, ResourceKind::Portable);
        assert_eq!(id.portable_identity.as_deref(), Some("file:note.md"));
        assert!(id.key.contains("https://github.com/o/r"));
    }

    #[test]
    fn resource_identity_no_anchor_is_local_with_root() {
        let r = ResourceContext::new(
            "nvim",
            Some("/repo-a/note.md"),
            Some("/repo-a"),
            Some("/repo-a"),
            None,
            None,
            None,
            None,
        );
        let a = resource_identity(&r, None).unwrap();
        assert_eq!(a.kind, ResourceKind::Local);
        assert!(a.local_identity.as_deref().unwrap().contains("/repo-a"));
        let r2 = ResourceContext::new(
            "nvim",
            Some("/repo-b/note.md"),
            Some("/repo-b"),
            Some("/repo-b"),
            None,
            None,
            None,
            None,
        );
        let b = resource_identity(&r2, None).unwrap();
        assert_ne!(
            a.key, b.key,
            "same rel path without anchor must stay distinct"
        );
    }

    #[test]
    fn resource_identity_root_dot() {
        let r = ResourceContext::new(
            "kitty",
            None,
            Some("/repo"),
            Some("/repo"),
            None,
            None,
            None,
            None,
        );
        let id = resource_identity(&r, Some("proj-1")).unwrap();
        assert_eq!(id.portable_identity.as_deref(), Some("cwd:."));
    }

    #[test]
    fn resource_identity_component_boundary() {
        let r = ResourceContext::new(
            "x",
            Some("/repo2/a.md"),
            None,
            Some("/repo"),
            None,
            None,
            None,
            None,
        );
        // Not under /repo (boundary) and no URL/page => machine-local file.
        let id = resource_identity(&r, Some("proj-1")).unwrap();
        assert_eq!(id.kind, ResourceKind::Local);
        assert_eq!(id.local_identity.as_deref(), Some("file:/repo2/a.md"));
    }

    /// Serial env access: the process environment is global, so env tests
    /// must not run concurrently with each other.
    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    struct EnvGuard {
        saved_gap: Option<String>,
        saved_int: Option<String>,
    }

    impl EnvGuard {
        fn hold() -> (std::sync::MutexGuard<'static, ()>, Self) {
            let guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
            let saved = Self {
                saved_gap: std::env::var(SESSION_GAP_ENV).ok(),
                saved_int: std::env::var(SESSION_INTERRUPTION_ENV).ok(),
            };
            std::env::remove_var(SESSION_GAP_ENV);
            std::env::remove_var(SESSION_INTERRUPTION_ENV);
            (guard, saved)
        }
    }

    impl Drop for EnvGuard {
        fn drop(&mut self) {
            match self.saved_gap.as_deref() {
                Some(v) => std::env::set_var(SESSION_GAP_ENV, v),
                None => std::env::remove_var(SESSION_GAP_ENV),
            }
            match self.saved_int.as_deref() {
                Some(v) => std::env::set_var(SESSION_INTERRUPTION_ENV, v),
                None => std::env::remove_var(SESSION_INTERRUPTION_ENV),
            }
        }
    }

    #[test]
    fn resolve_defaults_without_env() {
        let (_lock, _restore) = EnvGuard::hold();
        let cfg = SessionConfig::resolve(None, None).unwrap();
        assert_eq!(cfg.inactivity_gap_ms(), DEFAULT_SESSION_GAP_MS);
        assert_eq!(cfg.interruption_grace_ms(), DEFAULT_SESSION_INTERRUPTION_MS);
    }

    #[test]
    fn resolve_cli_overrides_malformed_env() {
        let (_lock, _restore) = EnvGuard::hold();
        std::env::set_var(SESSION_GAP_ENV, "not-a-number");
        std::env::set_var(SESSION_INTERRUPTION_ENV, "also-bad");
        // Malformed env without overrides fails.
        assert!(SessionConfig::resolve(None, None).is_err());
        assert!(SessionConfig::resolve(None, Some(100)).is_err());
        // CLI overrides bypass their malformed env field entirely.
        let cfg = SessionConfig::resolve(Some(5000), Some(100)).unwrap();
        assert_eq!(cfg.inactivity_gap_ms(), 5000);
        assert_eq!(cfg.interruption_grace_ms(), 100);
    }

    #[test]
    fn resolve_intermediate_invalid_final_valid() {
        let (_lock, _restore) = EnvGuard::hold();
        // Env pair is intermediate-invalid (grace exceeds gap)...
        std::env::set_var(SESSION_GAP_ENV, "5000");
        std::env::set_var(SESSION_INTERRUPTION_ENV, "6000");
        assert!(SessionConfig::resolve(None, None).is_err());
        // ...but a CLI gap making the final pair valid must resolve.
        let cfg = SessionConfig::resolve(Some(10000), None).unwrap();
        assert_eq!(cfg.inactivity_gap_ms(), 10000);
        assert_eq!(cfg.interruption_grace_ms(), 6000);
        // And a CLI grace making the final pair valid must resolve.
        let cfg = SessionConfig::resolve(None, Some(1000)).unwrap();
        assert_eq!(cfg.inactivity_gap_ms(), 5000);
        assert_eq!(cfg.interruption_grace_ms(), 1000);
    }

    #[test]
    fn resolve_env_applies_per_field() {
        let (_lock, _restore) = EnvGuard::hold();
        std::env::set_var(SESSION_GAP_ENV, "300000");
        let cfg = SessionConfig::resolve(None, None).unwrap();
        assert_eq!(cfg.inactivity_gap_ms(), 300000);
        assert_eq!(cfg.interruption_grace_ms(), DEFAULT_SESSION_INTERRUPTION_MS);
        let cfg = SessionConfig::resolve(None, Some(5000)).unwrap();
        assert_eq!(cfg.inactivity_gap_ms(), 300000);
        assert_eq!(cfg.interruption_grace_ms(), 5000);
        // Env gap below the default grace is intermediate-invalid.
        std::env::set_var(SESSION_GAP_ENV, "60000");
        assert!(SessionConfig::resolve(None, None).is_err());
    }

    #[test]
    fn session_rules_gap_boundary() {
        let open = OpenSessionView {
            project_id: Some("a".to_string()),
            end_ms: 1000,
            unresolved_start_ms: None,
            gap_ms: 100,
            interruption_ms: 50,
        };
        // Gap exactly at threshold splits.
        assert_eq!(
            decide_session_action(&open, Some("a"), 1100),
            SessionAction::Split {
                ended_reason: EndedReason::Inactivity
            }
        );
        assert_eq!(
            decide_session_action(&open, Some("a"), 1099),
            SessionAction::Continue {
                new_unresolved_start: None
            }
        );
    }
}
