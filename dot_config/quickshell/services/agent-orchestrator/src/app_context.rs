//! Phase 2 application-aware resource enrichment.
//!
//! Generic [`ResourceProvider`] interface (this module) is separate from the
//! concrete adapters below. Providers attach an optional typed
//! [`ResourceContext`](crate::desktop_context::ResourceContext) to a
//! compositor snapshot; `source` stays compositor-only and provenance lives
//! in `resource.adapter`.
//!
//! Correlation rule (never an unrelated global): a provider result is
//! attached ONLY when it demonstrably belongs to the focused window.
//! Kitty instance identity is established via the Unix socket peer credential
//! ([`kitty_peer_pid`], bounded connect, bare or `unix:`-prefixed paths
//! normalized): the socket peer PID must equal the focused compositor client
//! PID — a present but unequal peer is a confirmed mismatch that evicts
//! rather than serving last-good; the selected OS window/tab/pane must each
//! be the unique `is_focused` entry (no positional or first-element guesses,
//! no numeric window-id==pid conflation). Kitty socket discovery honors the
//! configured env socket (`QS_KITTY_SOCKET` / `KITTY_LISTEN_ON`) as a
//! preference; when it is absent or belongs to a foreign instance, the
//! focused instance's default socket (`/tmp/kitty-<focused_pid>`, plus
//! `$TMPDIR/kitty-<pid>` when different) is probed and still validated by
//! peer PID — no other opt-in is required for cwd-based project resolution
//! (the nvim Lua publisher remains optional for per-file context). Neovim wins only as the unique
//! foreground `nvim` with a fresh correlated record. Zen explicit records
//! require the opaque compositor `window_id` PLUS `pid`. Zotero explicit
//! active-reader records require the same verified `window_id` PLUS `pid`
//! binding supplied by the external bridge (the in-process plugin cannot
//! observe native window handles: the local library API exposes no
//! focus/reader state and `Zotero.Reader` exposes no OS handle, so a global
//! last-active reader is never attributed and there is no title fallback;
//! closed/stale/mismatched readers evict). Logseq is
//! title-only (no HTTP API: the global page cannot be window-bound without
//! polling): the full title plus an explicit ` - Logseq` suffix-strip page
//! extraction. Multi-target ambiguity resolves to `None`, never a guess.
//! Slow/malformed/disconnected apps return `None` and never fail the base
//! collector. All subprocess time/output and file input is bounded
//! (`O_NONBLOCK | O_NOFOLLOW` opens, descriptor mtime); no shell
//! interpolation; file *contents* are never read (only published structured
//! records and `git rev-parse` metadata).
//!
//! Oscillation guard: transient failures must not flap resources. A bounded
//! thread-local last-good cache ([`LAST_GOOD_TTL_MS`]) serves the previous
//! resource for the same focus key while a provider is merely *unavailable*
//! (returns `None`), and fills transiently-missing `git_branch` when the
//! file/cwd anchor is unchanged. Kitty-path serves additionally require the
//! exact verified pane + editor PID recorded alongside the cached neovim
//! value; anything else clears, evicts, and falls back to the verified pane
//! cwd. Thread-locality keeps each collector session worker (one thread per
//! session) on a fresh cache, so reconnects invalidate automatically and
//! sessions never share stale entries.
//! Health/timestamps/identity never enter the persisted resource, so serving
//! last-good never creates semantic activity by itself. A confirmed mismatch
//! (peer identity, explicit binding, ambiguous pane focus) evicts instead of
//! serving: it must never return a foreign last-good entry.

use crate::desktop_context::{now_ms, DesktopContext, FocusedWindow, ResourceContext};
use std::cell::RefCell;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::Duration;

/// Low-frequency application-only refresh interval used by the collector.
/// Resource/branch changes without compositor events are captured on this
/// cadence without restarting generic desktop polling.
pub const APP_REFRESH_INTERVAL: Duration = Duration::from_secs(5);
/// Freshness bound for published app records (nvim/zen explicit files).
pub const RECORD_FRESHNESS_MS: i64 = 30_000;
/// Bounded last-good window for the same focus while a provider is
/// transiently unavailable. Prevents failure noise from oscillating history.
pub const LAST_GOOD_TTL_MS: i64 = 60_000;
const LAST_GOOD_MAX_ENTRIES: usize = 64;
/// Bounded subprocess / socket timeouts.
pub const GIT_TIMEOUT: Duration = Duration::from_millis(1000);
pub const KITTY_TIMEOUT: Duration = Duration::from_millis(1500);
/// Bounded input sizes.
pub const MAX_RECORD_FILE_BYTES: u64 = 16 * 1024;
pub const MAX_KITTY_OUTPUT_BYTES: usize = 256 * 1024;
pub const MAX_GIT_OUTPUT_BYTES: usize = 8 * 1024;
pub const MAX_RECORD_FILES: usize = 128;
pub const MAX_FOREGROUND_ENTRIES: usize = 32;

/// Generic enrichment interface. Concrete adapters implement this; the
/// collector only depends on the trait + [`enrich_desktop_context`].
pub trait ResourceProvider {
    fn name(&self) -> &'static str;
    fn enrich(&self, ctx: &DesktopContext) -> Option<ResourceContext>;
}

/// Environment-driven opt-in configuration. All fields optional; absent
/// means that integration is disabled (honest fallback or no resource, never
/// a base failure).
#[derive(Clone, Debug, Default)]
pub struct EnrichmentEnv {
    pub kitty_socket: Option<PathBuf>,
    pub nvim_dir: Option<PathBuf>,
    pub zen_file: Option<PathBuf>,
    pub zotero_file: Option<PathBuf>,
}

impl EnrichmentEnv {
    pub fn from_env() -> Self {
        let kitty_socket = std::env::var_os("QS_KITTY_SOCKET")
            .map(PathBuf::from)
            .or_else(|| {
                std::env::var_os("KITTY_LISTEN_ON").map(|v| {
                    // KITTY_LISTEN_ON is `unix:/path`; strip to a filesystem path.
                    let s = v.to_string_lossy().to_string();
                    match s.strip_prefix("unix:") {
                        Some(p) => PathBuf::from(p),
                        None => PathBuf::from(s),
                    }
                })
            });
        let nvim_dir = std::env::var_os("QS_NVIM_CONTEXT_DIR").map(PathBuf::from);
        let zen_file = std::env::var_os("QS_ZEN_CONTEXT_FILE").map(PathBuf::from);
        let zotero_file = std::env::var_os("QS_ZOTERO_CONTEXT_FILE").map(PathBuf::from);
        Self {
            kitty_socket,
            nvim_dir,
            zen_file,
            zotero_file,
        }
    }
}

/// Default composite enrichment reading opt-in state from the environment.
/// Never panics, never blocks unboundedly, never reads file contents.
/// Runs application enrichment first, then deterministic project resolution
/// (thread-local cached registry projection). Unavailable / focusless /
/// resourceless snapshots never carry a project.
pub fn enrich_desktop_context(base: DesktopContext) -> DesktopContext {
    let env = EnrichmentEnv::from_env();
    let now = now_ms();
    let apped = enrich_with_env(base, &env, now);
    crate::project_context::enrich_desktop_with_project(apped, now)
}

/// Provider outcome: a bound value, a transient unavailability (serve
/// last-good within TTL), or a confirmed mismatch (evict, never serve).
#[derive(Clone, Debug)]
enum ProviderFresh {
    Value(ResourceContext),
    Unavailable,
    Mismatch,
}

/// Testable composite with explicit env + clock.
pub fn enrich_with_env(base: DesktopContext, env: &EnrichmentEnv, now: i64) -> DesktopContext {
    if !base.available {
        return base.with_project(None);
    }
    let Some(win) = base.focused_window.as_ref() else {
        return base.with_project(None);
    };
    let key = focus_key(win);
    let app = win.application.to_lowercase();
    // Git outcome is tracked so a *confirmed* non-repo clears metadata
    // instead of being back-filled from the cache as if transient.
    let mut git_confirmed_absent = false;
    // Verified pane/editor scope for kitty-path cache service. Other
    // providers use the focus key alone (their PIDs already scope them).
    let mut scope: Option<EditorIdentity> = None;
    let fresh: ProviderFresh = if app.contains("kitty") {
        let (fresh, kitty_scope) = kitty_fresh(win, env, now);
        scope = kitty_scope;
        match fresh {
            ProviderFresh::Value(mut r) => {
                if attach_git(&mut r) == GitOutcome::NonRepo {
                    git_confirmed_absent = true;
                }
                ProviderFresh::Value(r)
            }
            other => other,
        }
    } else if app.contains("nvim") || app.contains("neovim") {
        match nvim_direct_resource(win.process_id, env, now) {
            None => ProviderFresh::Unavailable,
            Some(mut r) => {
                if attach_git(&mut r) == GitOutcome::NonRepo {
                    git_confirmed_absent = true;
                }
                ProviderFresh::Value(r)
            }
        }
    } else if app.contains("zen") {
        if env.zen_file.is_none() {
            // No explicit integration configured: honest title fallback.
            match zen_fallback(&win.title) {
                Some(r) => ProviderFresh::Value(r),
                None => return base,
            }
        } else {
            match zen_fresh(win, env, now) {
                ProviderFresh::Value(r) => ProviderFresh::Value(r),
                ProviderFresh::Unavailable => ProviderFresh::Unavailable,
                ProviderFresh::Mismatch => {
                    // Confirmed mismatch carries the honest title fallback
                    // as a value (evicting any stale primary for this focus).
                    match zen_fallback(&win.title) {
                        Some(r) => ProviderFresh::Value(r),
                        None => ProviderFresh::Mismatch,
                    }
                }
            }
        }
    } else if app.contains("logseq") {
        match logseq_resource(&win.title) {
            Some(r) => ProviderFresh::Value(r),
            // Empty title: nothing honest to attach (not worth caching).
            None => return base,
        }
    } else if app.contains("zotero") {
        // No title-based attribution for Zotero: without the opt-in explicit
        // file there is nothing honest to attach (a window title cannot prove
        // which reader tab is active, and the local library API exposes no
        // focus/reader state). Unconfigured Zotero windows carry no resource.
        if env.zotero_file.is_none() {
            return base;
        }
        match zotero_fresh(win, env, now) {
            ProviderFresh::Value(r) => ProviderFresh::Value(r),
            // Closed readers and binding mismatches evict (never serve stale
            // documents); missing/unreadable files stay transient.
            ProviderFresh::Unavailable => ProviderFresh::Unavailable,
            ProviderFresh::Mismatch => ProviderFresh::Mismatch,
        }
    } else {
        return base;
    };
    let fresh = match fresh {
        ProviderFresh::Value(r) if r.is_empty() => ProviderFresh::Unavailable,
        other => other,
    };
    match apply_last_good(&key, fresh, now, git_confirmed_absent, scope) {
        Some(r) => base.with_resource(Some(r)),
        None => base,
    }
}

/// Stable focus key for the last-good cache: opaque window id + app class +
/// client PID. The PID is included so a reused compositor address for a new
/// client never inherits the previous occupant's resource.
pub fn focus_key(win: &FocusedWindow) -> String {
    format!(
        "{}\0{}\0{}",
        win.application.to_lowercase(),
        win.id,
        win.process_id.map(|p| p.to_string()).unwrap_or_default()
    )
}

/// One cache entry: the resource, when it was stored, when git fields were
/// last successfully resolved, and — for kitty-path values — the verified
/// pane/editor identity the value was resolved for. The git timestamp is
/// tracked separately so carrying old git fields forward never renews the
/// entry TTL.
#[derive(Clone, Debug)]
struct LastGoodEntry {
    resource: ResourceContext,
    stored_at_ms: i64,
    git_at_ms: Option<i64>,
    editor: Option<EditorIdentity>,
}

/// Verified pane/editor identity recorded alongside cached kitty-path
/// values. Nonsemantic metadata only: it scopes cache service, never
/// enters [`ResourceContext`], snapshots, kinds, or dedup — activity
/// semantics are unchanged by its presence.
#[derive(Clone, Debug, PartialEq, Eq)]
struct EditorIdentity {
    kitty_window_id: String,
    nvim_pid: Option<u32>,
}

thread_local! {
    static LAST_GOOD: RefCell<HashMap<String, LastGoodEntry>> = RefCell::new(HashMap::new());
}

/// Clear the last-good oscillation guard (tests only; production never calls).
pub fn clear_last_good_cache() {
    LAST_GOOD.with(|c| c.borrow_mut().clear());
}

/// Drop one focus key from the last-good cache.
fn evict_last_good(key: &str) {
    LAST_GOOD.with(|c| {
        c.borrow_mut().remove(key);
    });
}

/// Scoped peek: the cached neovim value for this exact pane + editor PID,
/// if fresh. Read-only (never renews TTL); lets the kitty path tell a
/// transiently-missing record for the SAME publisher apart from a pane or
/// editor switch, which must clear and fall back instead of serving old.
fn peek_scoped_neovim(
    key: &str,
    now: i64,
    kitty_window_id: &str,
    nvim_pid: u32,
) -> Option<ResourceContext> {
    LAST_GOOD.with(|c| {
        let store = c.borrow();
        let e = store.get(key)?;
        if e.resource.adapter != "neovim" {
            return None;
        }
        if !(e.stored_at_ms > 0 && now >= e.stored_at_ms && now - e.stored_at_ms <= LAST_GOOD_TTL_MS)
        {
            return None;
        }
        match &e.editor {
            Some(id)
                if id.kitty_window_id == kitty_window_id && id.nvim_pid == Some(nvim_pid) =>
            {
                Some(e.resource.clone())
            }
            _ => None,
        }
    })
}

/// Oscillation guard core.
/// - `Value` fresh: fill a transiently-missing `git_branch` from the cache
///   when the location anchor is unchanged, then store (recording `editor`
///   for kitty-path values); the entry timestamp is renewed, but the *git*
///   timestamp is renewed only when git was actually resolved now
///   (carried-forward git never extends its own TTL). A confirmed
///   non-repo (`git_confirmed_absent`) stores as-is with no fill.
/// - `Unavailable` with no scope: serve the cached resource for the same
///   focus key while within [`LAST_GOOD_TTL_MS`]; else `None`.
/// - `Unavailable` with a scope: serve only when the cached entry carries
///   the exact same pane/editor identity; a present-but-foreign entry is
///   evicted (confirmed foreign), never served.
/// - `Mismatch`: evict the entry and return `None` (never serve foreign data).
fn apply_last_good(
    key: &str,
    fresh: ProviderFresh,
    now: i64,
    git_confirmed_absent: bool,
    editor: Option<EditorIdentity>,
) -> Option<ResourceContext> {
    LAST_GOOD.with(|c| {
        let mut store = c.borrow_mut();
        match fresh {
            ProviderFresh::Mismatch => {
                store.remove(key);
                None
            }
            ProviderFresh::Unavailable => match &editor {
                None => match store.get(key) {
                    Some(e)
                        if e.stored_at_ms > 0
                            && now >= e.stored_at_ms
                            && now - e.stored_at_ms <= LAST_GOOD_TTL_MS =>
                    {
                        Some(e.resource.clone())
                    }
                    _ => None,
                },
                Some(scope) => {
                    let hit = matches!(store.get(key), Some(e)
                        if e.resource.adapter == "neovim"
                            && e.stored_at_ms > 0
                            && now >= e.stored_at_ms
                            && now - e.stored_at_ms <= LAST_GOOD_TTL_MS
                            && e.editor.as_ref() == Some(scope));
                    if hit {
                        Some(store.get(key).expect("hit").resource.clone())
                    } else {
                        // Foreign or absent: evict so nothing stale survives
                        // for a later unscoped serve.
                        store.remove(key);
                        None
                    }
                }
            },
            ProviderFresh::Value(mut r) => {
                // `carried` tracks whether any git field below was copied
                // from the cache rather than resolved now. Carried fields
                // never renew timestamps (no indefinite TTL refresh).
                let mut carried = false;
                let mut git_at = if r.git_branch.is_some() { Some(now) } else { None };
                if !git_confirmed_absent && r.git_branch.is_none() {
                    if let Some(cached) = store.get(key) {
                        let git_ok = cached.git_at_ms.map(|g| {
                            g > 0 && now >= g && now - g <= LAST_GOOD_TTL_MS
                        }) == Some(true);
                        let anchor_same = cached.resource.file == r.file
                            && cached.resource.cwd == r.cwd
                            && cached.resource.url == r.url
                            && cached.resource.page == r.page;
                        if git_ok && anchor_same {
                            if r.git_root.is_none() {
                                r.git_root = cached.resource.git_root.clone();
                                carried = true;
                            }
                            if r.git_branch.is_none() && r.git_root == cached.resource.git_root
                            {
                                r.git_branch = cached.resource.git_branch.clone();
                                carried = true;
                            }
                            if carried {
                                git_at = cached.git_at_ms;
                            }
                        }
                    }
                }
                if store.len() >= LAST_GOOD_MAX_ENTRIES && !store.contains_key(key) {
                    if let Some(oldest) = store
                        .iter()
                        .min_by_key(|(_, e)| e.stored_at_ms)
                        .map(|(k, _)| k.clone())
                    {
                        store.remove(&oldest);
                    }
                }
                // Carried-forward git keeps the ORIGINAL entry timestamp, so
                // repeated partial resolutions cannot extend the window.
                let stored_at = if carried {
                    store.get(key).map(|e| e.stored_at_ms).unwrap_or(now)
                } else {
                    now
                };
                store.insert(
                    key.to_string(),
                    LastGoodEntry {
                        resource: r.clone(),
                        stored_at_ms: stored_at,
                        git_at_ms: git_at,
                        editor,
                    },
                );
                Some(r)
            }
        }
    })
}

// ---------------------------------------------------------------------------
// Private file validation (no symlinks, owned, private, regular)
// ---------------------------------------------------------------------------

/// Validate that `dir` is a safe private record directory: exists, not a
/// symlink, a real directory owned by the euid with no group/other bits.
pub fn validate_private_dir(dir: &Path) -> bool {
    let md = match std::fs::symlink_metadata(dir) {
        Ok(m) => m,
        Err(_) => return false,
    };
    if md.file_type().is_symlink() || !md.is_dir() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        // SAFETY: geteuid has no failure mode.
        let euid = unsafe { libc::geteuid() };
        if md.uid() != euid {
            return false;
        }
        if md.mode() & 0o077 != 0 {
            return false;
        }
    }
    true
}

/// Open a private record file with `O_NOFOLLOW | O_NONBLOCK` and validate the
/// opened descriptor: regular file, euid-owned, no group/other permission
/// bits. `O_NONBLOCK` keeps a FIFO swap from blocking the open; non-regular
/// files are rejected by the descriptor check below. Returns the validated
/// file handle.
#[cfg(unix)]
fn open_private_file(path: &Path) -> Option<std::fs::File> {
    use std::os::unix::fs::OpenOptionsExt;
    let f = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .ok()?;
    let md = f.metadata().ok()?;
    if !md.is_file() {
        return None;
    }
    {
        use std::os::unix::fs::MetadataExt;
        // SAFETY: geteuid has no failure mode.
        let euid = unsafe { libc::geteuid() };
        if md.uid() != euid {
            return None;
        }
        if md.mode() & 0o077 != 0 {
            return None;
        }
        if md.size() > MAX_RECORD_FILE_BYTES {
            return None;
        }
    }
    Some(f)
}

#[cfg(not(unix))]
fn open_private_file(path: &Path) -> Option<std::fs::File> {
    // Non-Unix fallback: symlink + size checks without owner/mode semantics.
    if let Ok(md) = std::fs::symlink_metadata(path) {
        if md.file_type().is_symlink() || !md.is_file() {
            return None;
        }
        if md.len() > MAX_RECORD_FILE_BYTES {
            return None;
        }
    } else {
        return None;
    }
    std::fs::File::open(path).ok()
}

/// Validated private read: content plus mtime (taken from the opened
/// descriptor, not a re-stat, to avoid symlink-swap races).
fn read_private_file(path: &Path) -> Option<(String, Option<i64>)> {
    use std::io::Read;
    // File name must be a single safe segment (records) or any safe leaf for
    // explicit single-file integrations; the caller constrains the shape.
    let f = open_private_file(path)?;
    let mtime_ms = f
        .metadata()
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_millis() as i64);
    let mut buf = Vec::new();
    let mut take = f.take(MAX_RECORD_FILE_BYTES + 1);
    if take.read_to_end(&mut buf).is_err() {
        return None;
    }
    if buf.len() as u64 > MAX_RECORD_FILE_BYTES {
        return None;
    }
    String::from_utf8(buf).ok().map(|s| (s, mtime_ms))
}

fn record_file_name_ok(path: &Path) -> bool {
    let Some(name) = path.file_name().and_then(|n| n.to_str()) else {
        return false;
    };
    if name.len() > 32 || !name.ends_with(".json") {
        return false;
    }
    let stem = &name[..name.len() - 5];
    !stem.is_empty() && stem.len() <= 10 && stem.chars().all(|c| c.is_ascii_digit())
}

// ---------------------------------------------------------------------------
// Kitty + Neovim
// ---------------------------------------------------------------------------

/// Published Neovim record (written atomically by the opt-in Lua integration).
#[derive(Clone, Debug)]
pub struct NvimRecord {
    pub pid: u32,
    pub kitty_window_id: Option<u64>,
    pub file: Option<String>,
    pub cwd: Option<String>,
    pub updated_at_ms: Option<i64>,
}

pub fn parse_nvim_record(raw: &str) -> Option<NvimRecord> {
    let v: serde_json::Value = serde_json::from_str(raw).ok()?;
    let obj = v.as_object()?;
    let pid = match obj.get("pid") {
        Some(serde_json::Value::Number(n)) => n.as_u64().and_then(|x| u32::try_from(x).ok())?,
        Some(serde_json::Value::String(s)) => s.parse::<u32>().ok()?,
        _ => return None,
    };
    let kitty_window_id = match obj.get("kitty_window_id") {
        None | Some(serde_json::Value::Null) => None,
        Some(serde_json::Value::Number(n)) => n.as_u64(),
        Some(serde_json::Value::String(s)) => s.parse::<u64>().ok(),
        Some(_) => None,
    };
    let file = obj
        .get("file")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string());
    let cwd = obj
        .get("cwd")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string());
    let updated_at_ms = match obj.get("updated_at_ms") {
        None | Some(serde_json::Value::Null) => None,
        Some(serde_json::Value::Number(n)) => n.as_i64(),
        Some(_) => None,
    };
    let bound = |o: Option<String>, m: usize| {
        o.map(|s| {
            if s.chars().count() <= m {
                s
            } else {
                s.chars().take(m).collect()
            }
        })
    };
    Some(NvimRecord {
        pid,
        kitty_window_id,
        file: bound(file, 4096),
        cwd: bound(cwd, 4096),
        updated_at_ms,
    })
}

fn nvim_runtime_dir(env: &EnrichmentEnv) -> Option<PathBuf> {
    if let Some(d) = env.nvim_dir.clone() {
        return Some(d);
    }
    std::env::var_os("XDG_RUNTIME_DIR").map(|r| {
        PathBuf::from(r).join("quickshell").join("nvim-context")
    })
}

/// Read + parse at most MAX_RECORD_FILES validated private records.
/// Malformed/unowned/over-permissive/symlink files are skipped. Returns
/// records with file mtimes; freshness is checked at selection.
pub fn read_nvim_records(dir: &Path) -> Vec<(NvimRecord, Option<i64>)> {
    let mut out = Vec::new();
    if !validate_private_dir(dir) {
        return out;
    }
    let entries = match std::fs::read_dir(dir) {
        Ok(e) => e,
        Err(_) => return out,
    };
    for e in entries.flatten().take(MAX_RECORD_FILES) {
        let p = e.path();
        if !record_file_name_ok(&p) {
            continue;
        }
        let Some((content, mtime_ms)) = read_private_file(&p) else {
            continue;
        };
        if let Some(rec) = parse_nvim_record(&content) {
            // Cross-check: file name PID should match the record PID.
            // Mismatches are ignored (stale/renamed junk, never trusted).
            let stem = p
                .file_stem()
                .and_then(|s| s.to_str())
                .and_then(|s| s.parse::<u32>().ok());
            if stem != Some(rec.pid) {
                continue;
            }
            out.push((rec, mtime_ms));
        }
    }
    out
}

fn record_is_fresh(updated_at: Option<i64>, mtime: Option<i64>, now: i64) -> bool {
    if let Some(t) = updated_at {
        if t > 0 && now >= t && now - t <= RECORD_FRESHNESS_MS {
            return true;
        }
    }
    if updated_at.is_none() {
        if let Some(m) = mtime {
            if m > 0 && now >= m && now - m <= RECORD_FRESHNESS_MS + 5000 {
                return true;
            }
        }
    }
    false
}

/// Normalize a kitty remote-control socket to the `unix:` address form kitty
/// requires (`kitty @ --to unix:/path ls`). Accepts a bare filesystem path
/// (`QS_KITTY_SOCKET`) or a `unix:/path` address (`KITTY_LISTEN_ON`).
/// Returns `None` for empty/NUL/overlong or non-absolute paths.
pub fn normalize_kitty_socket_arg(socket: &Path) -> Option<String> {
    let s = socket.to_string_lossy().to_string();
    if s.is_empty() || s.len() > 4096 || s.contains('\0') {
        return None;
    }
    let path = s.strip_prefix("unix:").unwrap_or(&s);
    if !path.starts_with('/') || path.contains('\0') {
        return None;
    }
    Some(format!("unix:{path}"))
}

/// Peer-connection timeout for kitty instance identity (fast local check).
pub const KITTY_PEER_TIMEOUT: Duration = Duration::from_millis(500);

/// Unix socket peer PID via `SO_PEERCRED` (Linux). Establishes the kitty
/// instance identity: the listening kitty process PID. Connects with the
/// same bounded connector as the compositor sockets (never unbounded).
/// Accepts both bare paths and `unix:`-prefixed addresses. The collector
/// requires the peer to equal the focused compositor client PID; otherwise
/// the `ls` output belongs to an unrelated instance and is discarded as a
/// confirmed mismatch (never served from last-good).
#[cfg(target_os = "linux")]
pub fn kitty_peer_pid(socket: &Path) -> Option<u32> {
    let fs_path = kitty_fs_path(socket)?;
    let stream = crate::hyprland::connect_bounded(&fs_path, KITTY_PEER_TIMEOUT).ok()?;
    use std::os::unix::io::AsRawFd;
    let fd = stream.as_raw_fd();
    let mut cred: libc::ucred = unsafe { std::mem::zeroed() };
    let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    let ret = unsafe {
        libc::getsockopt(
            fd,
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            &mut cred as *mut _ as *mut libc::c_void,
            &mut len,
        )
    };
    if ret != 0 {
        return None;
    }
    u32::try_from(cred.pid).ok()
}

#[cfg(not(target_os = "linux"))]
pub fn kitty_peer_pid(_socket: &Path) -> Option<u32> {
    None
}

/// Candidate kitty remote-control sockets for the focused client PID, in
/// probe priority order: the configured env socket first (when it is a
/// usable absolute path: non-empty, no NUL, `<=4096` bytes, bare or
/// `unix:`-prefixed form normalized via [`kitty_fs_path`]), then the
/// focused instance's default socket `/tmp/kitty-<focused_pid>` (kitty's
/// default when no explicit listen address is configured), then
/// `$TMPDIR/kitty-<focused_pid>` when it differs from the `/tmp` form
/// (hermetic temp dirs in tests, custom `TMPDIR` in production).
/// Duplicates are removed preserving order. All candidates are absolute
/// paths only; no shell is ever involved and each candidate is still
/// validated by [`kitty_peer_pid`] before use.
pub fn kitty_candidate_sockets(configured: Option<&Path>, focused_pid: u32) -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = Vec::new();
    let mut push = |p: PathBuf| {
        if !p.is_absolute() {
            return;
        }
        if !out.contains(&p) {
            out.push(p);
        }
    };
    if let Some(cfg) = configured {
        if let Some(fs) = kitty_fs_path(cfg) {
            push(fs);
        }
    }
    push(PathBuf::from(format!("/tmp/kitty-{focused_pid}")));
    let tmp = std::env::temp_dir().join(format!("kitty-{focused_pid}"));
    if tmp.is_absolute() {
        push(tmp);
    }
    out
}

/// Outcome of [`select_kitty_socket`]: the validated socket plus its peer
/// PID, a confirmed foreign instance, or nothing reachable.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum KittySocketChoice {
    Matched(PathBuf, u32),
    Mismatch,
    Unavailable,
}

/// Probe each candidate with the bounded [`kitty_peer_pid`] (500 ms) and
/// return the FIRST candidate whose peer equals `focused_pid`, scanning all
/// candidates (a foreign first candidate never stops a later match). When
/// no candidate matches but at least one probe returned a (foreign) peer,
/// the focused instance is confirmed absent on every reachable socket:
/// `Mismatch`. When nothing was reachable at all: `Unavailable`.
pub fn select_kitty_socket(candidates: &[PathBuf], focused_pid: u32) -> KittySocketChoice {
    let mut saw_foreign = false;
    for cand in candidates {
        match kitty_peer_pid(cand) {
            Some(peer) if peer == focused_pid => {
                return KittySocketChoice::Matched(cand.clone(), peer);
            }
            Some(_) => {
                saw_foreign = true;
            }
            None => {}
        }
    }
    if saw_foreign {
        KittySocketChoice::Mismatch
    } else {
        KittySocketChoice::Unavailable
    }
}

/// Focused kitty pane info extracted from `kitty @ ls` JSON.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct KittyPaneInfo {
    pub os_window_id: String,
    pub tab_id: String,
    pub window_id: String,
    pub cwd: Option<String>,
    pub title: Option<String>,
}

/// One foreground process entry of a kitty pane.
#[derive(Clone, Debug)]
struct ForegroundEntry {
    pid: Option<u32>,
    cmdline: String,
    cwd: Option<String>,
}

pub fn cmdline_is_nvim(cmdline: &str) -> bool {
    let first = cmdline.split_whitespace().next().unwrap_or("");
    let base = first.rsplit('/').next().unwrap_or(first);
    base == "nvim" || base.starts_with("nvim-") || base == "neovim"
}

/// Parse `kitty @ ls` output and select the uniquely-focused OS window, tab
/// and pane. Instance identity must already be established: `focused_pid`
/// (Hyprland client PID) must equal `peer_pid` (socket `SO_PEERCRED` PID).
/// Real `kitty ls` carries NO OS-window pid; pane `pid` is the shell and
/// foreground pids are the editors — numeric id==pid conflation is rejected.
/// Any ambiguity (zero or multiple focused entries at any level) is `None`.
pub fn select_kitty_pane(
    raw: &str,
    focused_pid: Option<u32>,
    peer_pid: Option<u32>,
) -> Option<KittyPaneInfo> {
    let (focused, peer) = match (focused_pid, peer_pid) {
        (Some(f), Some(p)) if f == p => (f, p),
        _ => return None,
    };
    let _ = (focused, peer);
    let v: serde_json::Value = serde_json::from_str(raw).ok()?;
    let arr = v.as_array()?;
    if arr.is_empty() || arr.len() > 128 {
        return None;
    }
    let os_idx = unique_focused_index(arr)?;
    let os = arr.get(os_idx)?;
    let os_id = os
        .get("id")
        .map(|x| match x {
            serde_json::Value::Number(n) => n.to_string(),
            serde_json::Value::String(s) => s.clone(),
            _ => String::new(),
        })
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| os_idx.to_string());
    let tabs = os.get("tabs").and_then(|t| t.as_array())?;
    if tabs.is_empty() || tabs.len() > 128 {
        return None;
    }
    let tab_idx = unique_focused_index(tabs)?;
    let tab = tabs.get(tab_idx)?;
    let tab_id = tab
        .get("id")
        .map(|x| match x {
            serde_json::Value::Number(n) => n.to_string(),
            serde_json::Value::String(s) => s.clone(),
            _ => String::new(),
        })
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| tab_idx.to_string());
    let wins = tab.get("windows").and_then(|w| w.as_array())?;
    if wins.is_empty() || wins.len() > 128 {
        return None;
    }
    let win_idx = unique_focused_index(wins)?;
    let win = wins.get(win_idx)?;
    let window_id = win
        .get("id")
        .map(|x| match x {
            serde_json::Value::Number(n) => n.to_string(),
            serde_json::Value::String(s) => s.clone(),
            _ => String::new(),
        })
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| win_idx.to_string());
    let title = win
        .get("title")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .map(|s| {
            if s.chars().count() <= 1024 {
                s.to_string()
            } else {
                s.chars().take(1024).collect()
            }
        });
    let cwd = resolve_pane_cwd(win);
    Some(KittyPaneInfo {
        os_window_id: os_id,
        tab_id,
        window_id,
        cwd,
        title,
    })
}

/// Exactly-one `is_focused==true` index, else `None` (no positional guess).
fn unique_focused_index(items: &[serde_json::Value]) -> Option<usize> {
    let mut found: Option<usize> = None;
    for (i, item) in items.iter().enumerate() {
        if item
            .get("is_focused")
            .and_then(|b| b.as_bool())
            .unwrap_or(false)
        {
            if found.is_some() {
                return None;
            }
            found = Some(i);
        }
    }
    found
}

fn foreground_entries(win: &serde_json::Value) -> Vec<ForegroundEntry> {
    let mut out = Vec::new();
    let Some(fps) = win.get("foreground_processes").and_then(|f| f.as_array()) else {
        return out;
    };
    for fp in fps.iter().take(MAX_FOREGROUND_ENTRIES) {
        let pid = fp
            .get("pid")
            .and_then(|v| v.as_u64())
            .and_then(|x| u32::try_from(x).ok());
        let mut cmdline = String::new();
        if let Some(cl) = fp.get("cmdline").and_then(|v| v.as_array()) {
            let parts: Vec<String> = cl
                .iter()
                .filter_map(|x| x.as_str().map(|s| s.to_string()))
                .take(8)
                .collect();
            cmdline = parts.join(" ");
        } else if let Some(cl) = fp.get("cmdline").and_then(|v| v.as_str()) {
            cmdline = cl.chars().take(1024).collect();
        } else if let Some(exe) = fp.get("exe").and_then(|v| v.as_str()) {
            cmdline = exe.to_string();
        }
        if cmdline.chars().count() > 1024 {
            cmdline = cmdline.chars().take(1024).collect();
        }
        let cwd = fp
            .get("cwd")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .map(|s| s.to_string());
        // Skip fully-empty entries (bounded noise, no signal).
        if pid.is_none() && cmdline.is_empty() && cwd.is_none() {
            continue;
        }
        out.push(ForegroundEntry { pid, cmdline, cwd });
    }
    out
}

/// Pane cwd without first-element guesses: when the foreground group agrees
/// on exactly one distinct absolute cwd, use it; otherwise fall back to the
/// confidently-focused pane `cwd`; otherwise `None`.
fn resolve_pane_cwd(win: &serde_json::Value) -> Option<String> {
    let entries = foreground_entries(win);
    let mut distinct: Vec<String> = Vec::new();
    for e in &entries {
        if let Some(c) = e.cwd.as_deref().and_then(sanitize_abs_path) {
            if !distinct.contains(&c) {
                distinct.push(c);
            }
        }
    }
    if distinct.len() == 1 {
        return Some(distinct.into_iter().next().expect("single"));
    }
    if distinct.len() > 1 {
        // Disagreeing group: fall through to the pane-level cwd rather than
        // picking one member arbitrarily.
    }
    win.get("cwd")
        .and_then(|v| v.as_str())
        .and_then(sanitize_abs_path)
}

/// Kitty (+ Neovim-inside-kitty precedence) resource for a focused kitty
/// window, plus the verified pane/editor scope for cache service.
/// Socket discovery honors the configured env socket as a preference and
/// falls back to the focused instance's default socket
/// (`/tmp/kitty-<focused_pid>`, plus `$TMPDIR/kitty-<pid>`); every
/// candidate is still validated by peer PID before use, so no extra opt-in
/// beyond the running kitty instance is required for cwd resolution.
/// Scoping rules (never an unrelated global):
/// - no focused PID: `Mismatch` (identity cannot be established).
/// - no reachable candidate: `Unavailable` with no scope (transient;
///   generic focus-keyed serve may apply).
/// - reachable foreign peer(s) but no candidate matching the focused PID:
///   `Mismatch` (confirmed foreign; evicts, no serve).
/// - failed query after a match: `Unavailable` with no scope (transient).
/// - unresolvable / ambiguous pane focus: `Mismatch` (confirmed foreign;
/// - unique foreground nvim with a fresh record: neovim `Value` scoped to
///   that pane + editor PID.
/// - same unique foreground nvim but the record is missing/stale: the
///   scoped peek decides — a fresh cached neovim value for the SAME pane
///   and PID reads as transient (`Unavailable`, bounded serve, no renew);
///   any other identity (or none usable) clears, evicts, and falls back
///   to the verified pane cwd instead of serving old.
/// - changed or ambiguous foreground, or no usable record dir: kitty cwd
///   fallback scoped to the pane (editor unknown).
fn kitty_fresh(
    win: &FocusedWindow,
    env: &EnrichmentEnv,
    now: i64,
) -> (ProviderFresh, Option<EditorIdentity>) {
    let scope = |pane: &KittyPaneInfo, nvim_pid: Option<u32>| EditorIdentity {
        kitty_window_id: pane.window_id.clone(),
        nvim_pid,
    };
    let Some(focused_pid) = win.process_id else {
        // No focused PID: instance identity cannot be established.
        return (ProviderFresh::Mismatch, None);
    };
    // Socket discovery BEFORE spawning `kitty`: the configured env socket
    // is the preference, but when it is absent or belongs to a foreign
    // instance the focused instance's own default socket is tried. Every
    // candidate is probed with the bounded peer check; the first whose
    // `SO_PEERCRED` peer equals the focused client PID wins (all
    // candidates are scanned — a foreign first candidate never stops a
    // later match). A reachable foreign socket with no matching candidate
    // is a confirmed mismatch (never last-good); nothing reachable is
    // merely unavailable.
    let candidates = kitty_candidate_sockets(env.kitty_socket.as_deref(), focused_pid);
    let (socket, peer) = match select_kitty_socket(&candidates, focused_pid) {
        KittySocketChoice::Matched(socket, peer) => (socket, peer),
        KittySocketChoice::Mismatch => return (ProviderFresh::Mismatch, None),
        KittySocketChoice::Unavailable => return (ProviderFresh::Unavailable, None),
    };
    let Some(raw) = kitty_ls_via_socket(&socket) else {
        return (ProviderFresh::Unavailable, None);
    };
    // Unresolvable or ambiguous pane focus: confirmed mismatch, never an
    // unrelated previous pane's value.
    let Some(pane) = select_kitty_pane(&raw, win.process_id, Some(peer)) else {
        return (ProviderFresh::Mismatch, None);
    };
    // Resolve pane entries once for both presence detection and record
    // matching.
    let entries: Option<Vec<ForegroundEntry>> = (|| {
        let v = serde_json::from_str::<serde_json::Value>(&raw).ok()?;
        let arr = v.as_array()?;
        selected_pane_entries(arr, &pane)
    })();
    if let Some(entries) = entries.as_deref() {
        if let NvimPresence::Unique(pid) = unique_nvim_presence(entries) {
            if let Some(dir) = nvim_runtime_dir(env) {
                if validate_private_dir(&dir) {
                    if let Some(rec) = select_unique_nvim_record(
                        entries,
                        &dir,
                        Some(&pane.window_id),
                        now,
                        Some(pid),
                    ) {
                        let file = rec
                            .file
                            .and_then(|f| resolve_nvim_file(&f, rec.cwd.as_deref()));
                        let cwd = rec
                            .cwd
                            .and_then(|c| sanitize_abs_path(&c))
                            .or(pane.cwd.clone());
                        let r = ResourceContext::new(
                            "neovim",
                            file.as_deref(),
                            cwd.as_deref(),
                            None,
                            None,
                            None,
                            None,
                            None,
                        );
                        if !r.is_empty() {
                            return (
                                ProviderFresh::Value(r),
                                Some(scope(&pane, Some(pid))),
                            );
                        }
                    }
                    // Same verified pane + editor PID, record missing/stale:
                    // serve the old value only on an exact scoped hit;
                    // anything else clears, evicts, and falls back to the
                    // verified pane cwd below (never fileA/kitty/fileA
                    // noise, never a foreign pane's file).
                    let key = focus_key(win);
                    if peek_scoped_neovim(&key, now, &pane.window_id, pid).is_some() {
                        return (
                            ProviderFresh::Unavailable,
                            Some(scope(&pane, Some(pid))),
                        );
                    }
                    evict_last_good(&key);
                }
            }
            // No usable record dir, or scoped miss above: fall through to
            // the verified pane cwd below.
        }
    }
    let r = ResourceContext::new(
        "kitty",
        None,
        pane.cwd.as_deref(),
        None,
        None,
        None,
        None,
        None,
    );
    if r.is_empty() {
        return (ProviderFresh::Unavailable, None);
    }
    (ProviderFresh::Value(r), Some(scope(&pane, None)))
}

/// Foreground editor presence for the nvim-precedence decision, regardless
/// of `foreground_processes` ordering. `Unique(pid)` means exactly one
/// distinct nvim foreground PID, so a verified publisher is expected and a
/// missing record is transient; `Absent` (editor changed or never nvim)
/// and `Ambiguous` (several distinct nvim PIDs, or an nvim entry without a
/// correlatable PID) fall back to kitty cwd.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum NvimPresence {
    Unique(u32),
    Absent,
    Ambiguous,
}

fn unique_nvim_presence(entries: &[ForegroundEntry]) -> NvimPresence {
    let mut pids: Vec<u32> = Vec::new();
    let mut pidless_nvim = false;
    for e in entries {
        if !cmdline_is_nvim(&e.cmdline) {
            continue;
        }
        match e.pid {
            Some(pid) => {
                if !pids.contains(&pid) {
                    pids.push(pid);
                }
            }
            None => pidless_nvim = true,
        }
    }
    match (pids.as_slice(), pidless_nvim) {
        ([pid], false) => NvimPresence::Unique(*pid),
        ([], false) => NvimPresence::Absent,
        _ => NvimPresence::Ambiguous,
    }
}

/// Raw foreground entries of the already-selected pane (re-resolved from the
/// parsed payload to keep ordering-independent analysis in one place).
fn selected_pane_entries(
    arr: &[serde_json::Value],
    pane: &KittyPaneInfo,
) -> Option<Vec<ForegroundEntry>> {
    for os in arr {
        let os_id = os
            .get("id")
            .map(|x| match x {
                serde_json::Value::Number(n) => n.to_string(),
                serde_json::Value::String(s) => s.clone(),
                _ => String::new(),
            })
            .unwrap_or_default();
        if os_id != pane.os_window_id {
            // Fall back to focused-position resolution when ids are absent?
            // No: ids were normalized at selection; compare via focused flags.
            // To stay exact, re-find the unique focused chain.
            continue;
        }
        let tabs = os.get("tabs").and_then(|t| t.as_array())?;
        let tab_idx = unique_focused_index(tabs)?;
        let wins = tabs.get(tab_idx)?.get("windows").and_then(|w| w.as_array())?;
        let win_idx = unique_focused_index(wins)?;
        return Some(foreground_entries(&wins[win_idx]));
    }
    // IDs may be missing in minimal fixtures: resolve via the unique focused
    // chain directly (still no positional guess).
    let os_idx = unique_focused_index(arr)?;
    let tabs = arr.get(os_idx)?.get("tabs").and_then(|t| t.as_array())?;
    let tab_idx = unique_focused_index(tabs)?;
    let wins = tabs
        .get(tab_idx)?
        .get("windows")
        .and_then(|w| w.as_array())?;
    let win_idx = unique_focused_index(wins)?;
    Some(foreground_entries(&wins[win_idx]))
}

/// Unique foreground nvim with a fresh correlated record, regardless of
/// `foreground_processes` ordering. Zero or multiple distinct nvim matches
/// resolve to `None` (no first-element guess, no background attribution).
/// When `only_pid` is `Some`, only that verified foreground PID is eligible
/// (callers that already established uniqueness pass it through).
fn select_unique_nvim_record(
    entries: &[ForegroundEntry],
    dir: &Path,
    kitty_window_id: Option<&str>,
    now: i64,
    only_pid: Option<u32>,
) -> Option<NvimRecord> {
    let records = read_nvim_records(dir);
    if records.is_empty() {
        return None;
    }
    let kitty_id = kitty_window_id.and_then(|s| s.parse::<u64>().ok());
    let mut matched: Vec<NvimRecord> = Vec::new();
    for e in entries {
        if !cmdline_is_nvim(&e.cmdline) {
            continue;
        }
        let Some(pid) = e.pid else { continue };
        if let Some(required) = only_pid {
            if pid != required {
                continue;
            }
        }
        for (rec, mtime) in &records {
            if rec.pid != pid {
                continue;
            }
            if let (Some(a), Some(b)) = (rec.kitty_window_id, kitty_id) {
                if a != b {
                    continue;
                }
            }
            if !record_is_fresh(rec.updated_at_ms, *mtime, now) {
                continue;
            }
            let file_ok = rec
                .file
                .as_deref()
                .map(|f| f.starts_with('/') && is_safe_path_str(f));
            let cwd_ok = rec
                .cwd
                .as_deref()
                .map(|c| sanitize_abs_path(c).is_some());
            let relative_ok = rec
                .file
                .as_deref()
                .map(|f| !f.starts_with('/') && !f.is_empty() && !f.contains('\0'))
                .unwrap_or(false)
                && cwd_ok == Some(true);
            if file_ok == Some(true) || cwd_ok == Some(true) || relative_ok {
                matched.push(rec.clone());
            }
        }
    }
    // Deduplicate by PID (same nvim listed twice is one candidate).
    matched.sort_by_key(|r| r.pid);
    matched.dedup_by_key(|r| r.pid);
    if matched.len() == 1 {
        matched.into_iter().next()
    } else {
        None
    }
}

fn nvim_direct_resource(
    focused_pid: Option<u32>,
    env: &EnrichmentEnv,
    now: i64,
) -> Option<ResourceContext> {
    let dir = nvim_runtime_dir(env)?;
    if !validate_private_dir(&dir) {
        return None;
    }
    let pid = focused_pid?;
    let records = read_nvim_records(&dir);
    let mut matched: Vec<NvimRecord> = Vec::new();
    for (rec, mtime) in records {
        if rec.pid != pid {
            continue;
        }
        if !record_is_fresh(rec.updated_at_ms, mtime, now) {
            continue;
        }
        matched.push(rec);
    }
    matched.sort_by_key(|r| r.pid);
    matched.dedup_by_key(|r| r.pid);
    if matched.len() != 1 {
        return None;
    }
    let rec = matched.into_iter().next().expect("single");
    let file = rec.file.and_then(|f| resolve_nvim_file(&f, rec.cwd.as_deref()));
    let cwd = rec.cwd.and_then(|c| sanitize_abs_path(&c));
    let r = ResourceContext::new(
        "neovim",
        file.as_deref(),
        cwd.as_deref(),
        None,
        None,
        None,
        None,
        None,
    );
    if r.is_empty() {
        return None;
    }
    Some(r)
}

/// True for URI-like values (`scheme://…`) which are never local paths.
/// Defensive: buffers like `fugitive://…` or `term://…` must not be joined
/// onto a cwd even if a publisher ever emits them.
fn is_uri_like(s: &str) -> bool {
    let Some((scheme, _)) = s.split_once("://") else {
        return false;
    };
    let mut chars = scheme.chars();
    match chars.next() {
        Some(c) if c.is_ascii_alphabetic() => {}
        _ => return false,
    }
    chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'))
}

fn resolve_nvim_file(file: &str, cwd: Option<&str>) -> Option<String> {
    if file.is_empty() || file.contains('\0') || file.len() > 4096 {
        return None;
    }
    if is_uri_like(file) {
        return None;
    }
    if file.starts_with('/') {
        return sanitize_abs_path(file);
    }
    let base = cwd?;
    let abs_base = sanitize_abs_path(base)?;
    if file.contains('\0') {
        return None;
    }
    let joined = lexical_clean(&format!("{abs_base}/{file}"));
    sanitize_abs_path(&joined)
}

fn lexical_clean(p: &str) -> String {
    let mut parts: Vec<&str> = Vec::new();
    let absolute = p.starts_with('/');
    for comp in p.split('/') {
        match comp {
            "" | "." => {}
            ".." => {
                parts.pop();
            }
            c => parts.push(c),
        }
    }
    if absolute {
        format!("/{}", parts.join("/"))
    } else {
        parts.join("/")
    }
}

/// Absolute-path sanitizer: must start with '/', no NUL, bounded, no
/// control characters. Does NOT touch the filesystem and never reads file
/// contents. Returns the bounded path or None.
pub fn sanitize_abs_path(p: &str) -> Option<String> {
    if p.is_empty() || p.len() > 4096 || !p.starts_with('/') || p.contains('\0') {
        return None;
    }
    if p.chars().any(|c| c.is_control()) {
        return None;
    }
    Some(if p.chars().count() <= 4096 {
        p.to_string()
    } else {
        p.chars().take(4096).collect()
    })
}

fn is_safe_path_str(p: &str) -> bool {
    sanitize_abs_path(p).is_some()
}

/// Filesystem path for a kitty socket value that may carry the `unix:`
/// address prefix (`KITTY_LISTEN_ON` form) or be a bare path
/// (`QS_KITTY_SOCKET` form). Returns `None` for empty/NUL/overlong or
/// non-absolute values.
pub fn kitty_fs_path(socket: &Path) -> Option<PathBuf> {
    let raw = socket.to_string_lossy().to_string();
    if raw.is_empty() || raw.len() > 4096 || raw.contains('\0') {
        return None;
    }
    let fs_part = raw.strip_prefix("unix:").unwrap_or(&raw);
    if !fs_part.starts_with('/') || fs_part.contains('\0') {
        return None;
    }
    Some(PathBuf::from(fs_part))
}

/// Query kitty remote control: `kitty @ --to unix:<socket> ls`. Bounded time
/// and output, no shell. Returns stdout on success.
fn kitty_ls_via_socket(socket: &Path) -> Option<String> {
    let fs_path = kitty_fs_path(socket)?;
    let arg = normalize_kitty_socket_arg(&fs_path)?;
    let out = run_bounded("kitty", &["@", "--to", &arg, "ls"], KITTY_TIMEOUT, MAX_KITTY_OUTPUT_BYTES)?;
    if out.trim().is_empty() {
        return None;
    }
    Some(out)
}

/// Build the exact argv used for the kitty query (testable command boundary).
pub fn kitty_ls_argv(socket: &Path) -> Option<Vec<String>> {
    let arg = normalize_kitty_socket_arg(socket)?;
    Some(vec![
        "kitty".to_string(),
        "@".to_string(),
        "--to".to_string(),
        arg,
        "ls".to_string(),
    ])
}

// ---------------------------------------------------------------------------
// Zen
// ---------------------------------------------------------------------------

/// Zen: honest title fallback (never invent a URL) plus an opt-in explicit
/// record correlated by opaque compositor `window_id` PLUS `pid`.
/// There is no bundled Zen producer: the publisher contract (see
/// `integrations/zen/README.md`) requires the helper to write
/// `{window_id, pid, url, title, updated_at_ms}`. PID alone is insufficient
/// with multiple Zen windows. Session databases are never read.
///
/// Unavailable (missing/unreadable) explicit files yield `None` so the
/// last-good cache masks transient helper restarts; a readable but
/// non-matching record is a confirmed mismatch and yields the honest title
/// fallback for the focused window.
fn zen_resource(
    win: &FocusedWindow,
    focused_title: &str,
    env: &EnrichmentEnv,
    now: i64,
) -> Option<ResourceContext> {
    let fallback = || {
        let title = focused_title.trim();
        if title.is_empty() {
            None
        } else {
            Some(ResourceContext::new(
                "zen-title",
                None,
                None,
                None,
                None,
                None,
                None,
                Some(title),
            ))
        }
    };
    let Some(path) = env.zen_file.as_ref() else {
        return fallback();
    };
    match read_zen_explicit(path, win, now) {
        ZenRead::Matched(r) => Some(r),
        ZenRead::ConfirmedMismatch => fallback(),
        ZenRead::Unavailable => None,
    }
}

/// Tri-state Zen read for the cache-aware pipeline: an explicit record that
/// matches binds as a value; a missing/unreadable file is transient
/// (`Unavailable`, serve last-good); a readable but non-matching record is a
/// confirmed mismatch (honest fallback, evict any stale primary).
/// Requires a configured file; unconfigured callers use [`zen_fallback`].
fn zen_fresh(win: &FocusedWindow, env: &EnrichmentEnv, now: i64) -> ProviderFresh {
    let Some(path) = env.zen_file.as_ref() else {
        return ProviderFresh::Unavailable;
    };
    match read_zen_explicit(path, win, now) {
        ZenRead::Matched(r) => ProviderFresh::Value(r),
        ZenRead::ConfirmedMismatch => ProviderFresh::Mismatch,
        ZenRead::Unavailable => ProviderFresh::Unavailable,
    }
}

/// Recompute a title-only fallback resource (`*-title` adapters) from the
/// current window title. Pure and synchronous (no I/O): safe to call on the
/// event path inside the overlay merge, so a title change persists one
/// title row whose resource already matches instead of a stale-title row
/// plus a correction `context` row. The collector stays generic: it calls
/// this helper and carries whatever comes back.
///
/// Returns `None` when `resource` is not a title fallback (the caller
/// carries the stored overlay); otherwise the recomputed fallback — which
/// may itself be `None` for an empty title, meaning nothing honest carries
/// forward.
pub fn refresh_title_fallback(
    resource: &ResourceContext,
    application: &str,
    title: &str,
) -> Option<Option<ResourceContext>> {
    let app = application.to_lowercase();
    if resource.adapter == "zen-title" && app.contains("zen") {
        Some(zen_fallback(title))
    } else if resource.adapter == "logseq-title" && app.contains("logseq") {
        Some(logseq_resource(title))
    } else {
        None
    }
}

/// Honest Zen title fallback (never invents a URL).
fn zen_fallback(focused_title: &str) -> Option<ResourceContext> {
    let title = focused_title.trim();
    if title.is_empty() {
        return None;
    }
    Some(ResourceContext::new(
        "zen-title",
        None,
        None,
        None,
        None,
        None,
        None,
        Some(title),
    ))
}

/// Tri-state explicit read: `Matched` (bound, use it), `ConfirmedMismatch`
/// (readable record for another window/stale content — the focused window
/// honestly has no bound URL), `Unavailable` (missing/unreadable — transient,
/// serve last-good).
enum ZenRead {
    Matched(ResourceContext),
    ConfirmedMismatch,
    Unavailable,
}

fn read_zen_explicit(path: &Path, win: &FocusedWindow, now: i64) -> ZenRead {
    let Some((raw, mtime)) = read_private_file(path) else {
        return ZenRead::Unavailable;
    };
    let Ok(v) = serde_json::from_str::<serde_json::Value>(&raw) else {
        return ZenRead::Unavailable;
    };
    let Some(obj) = v.as_object() else {
        return ZenRead::Unavailable;
    };
    // Exact binding: opaque compositor window id AND pid must both match.
    // A readable record for another window (or without binding fields) is a
    // confirmed mismatch, not a transient failure.
    let rec_window = obj.get("window_id").and_then(|v| v.as_str()).unwrap_or("");
    if rec_window.is_empty() || rec_window != win.id {
        return ZenRead::ConfirmedMismatch;
    }
    let Some(focused_pid) = win.process_id else {
        return ZenRead::ConfirmedMismatch;
    };
    let rec_pid = match obj.get("pid") {
        Some(serde_json::Value::Number(n)) => n.as_u64().and_then(|x| u32::try_from(x).ok()),
        Some(serde_json::Value::String(s)) => s.parse::<u32>().ok(),
        _ => None,
    };
    if rec_pid != Some(focused_pid) {
        return ZenRead::ConfirmedMismatch;
    }
    let updated = obj.get("updated_at_ms").and_then(|v| v.as_i64());
    if !record_is_fresh(updated, mtime, now) {
        return ZenRead::ConfirmedMismatch;
    }
    let url = obj.get("url").and_then(|v| v.as_str()).filter(|s| {
        let t = s.trim();
        t.starts_with("http://") || t.starts_with("https://")
    });
    let title = obj
        .get("title")
        .and_then(|v| v.as_str())
        .filter(|s| !s.trim().is_empty());
    if url.is_none() && title.is_none() {
        return ZenRead::ConfirmedMismatch;
    }
    let r = ResourceContext::new("zen", None, None, None, None, url, None, title);
    if r.is_empty() {
        return ZenRead::ConfirmedMismatch;
    }
    ZenRead::Matched(r)
}

// ---------------------------------------------------------------------------
// Zotero (opt-in explicit active-reader only, no title attribution)
// ---------------------------------------------------------------------------
//
// The Zotero local library API (`http://localhost:23119/api/`, Zotero docs
// "Local API": read-only library data, `Zotero-Server-ID` header on Zotero
// 10+, no focus/reader state) cannot tell which reader tab is active, and
// the in-process JS API (`Zotero.Reader` tab/window manager,
// `Zotero.getActiveZoteroPane().getSelectedItems()`, `Zotero.Items`,
// `Zotero.Collections` per the JS-API docs and `zotero/reader` source)
// exposes no native OS window handle that maps to a Hyprland window
// address/PID. A plugin therefore cannot fabricate `window_id`/`pid`, and
// a process-global "last active reader" must never be attributed to the
// focused window (multi-window/tab switches and detached readers would
// misattribute).
//
// Fail-closed contract (documented in `integrations/zotero/README.md`):
// the plugin exposes a local endpoint with the *Zotero-internal* active
// reader (server/library/item/attachment/title/memberships+ancestors,
// version, stable URI), and a tiny external bridge samples the compositor
// (`hyprctl activewindow -j`) in the SAME tick, then writes the verified
// private JSON (`QS_ZOTERO_CONTEXT_FILE`, `0600`, atomic rename, bounded)
// carrying the opaque `window_id` PLUS `pid` binding. The collector binds
// ONLY on an exact `window_id`+`pid` match with freshness (30 s, mtime
// fallback); anything else evicts. Closed readers are explicit tombstones
// (`state:"closed"` or missing item identity) and evict rather than serving
// last-good. There is no title fallback: unconfigured Zotero windows carry
// no resource, and no SQLite, cloud, title, or content reads ever happen.

/// True for an 8-char Zotero key (`[A-Z0-9]{8}`: item/attachment/collection).
pub fn is_zotero_key(s: &str) -> bool {
    s.len() == 8 && s.bytes().all(|c| c.is_ascii_uppercase() || c.is_ascii_digit())
}

/// True for a Zotero library id (digit string, `user/"0"` alias allowed).
pub fn is_zotero_library_id(s: &str) -> bool {
    !s.is_empty() && s.len() <= 32 && s.bytes().all(|c| c.is_ascii_digit())
}

/// Bounded server-id check (non-blank, no NUL/controls/whitespace, ≤128).
pub fn is_zotero_server_id(s: &str) -> bool {
    let t = s.trim();
    !t.is_empty()
        && t.len() <= 128
        && !t.contains('\0')
        && !t.chars().any(|c| c.is_control() || c.is_whitespace())
}

/// Stable `zotero://select/...` URI check (display/search only, never identity).
fn is_zotero_uri(s: &str) -> bool {
    let t = s.trim();
    !t.is_empty()
        && t.len() <= 2048
        && !t.contains('\0')
        && (t.starts_with("zotero://select/") || t.starts_with("zotero://open-pdf/"))
}

/// Tri-state Zotero read: `Matched` (bound active reader), `ConfirmedMismatch`
/// (readable but for another window/stale/closed — evict), `Unavailable`
/// (missing/unreadable — transient, serve last-good).
enum ZoteroRead {
    Matched(ResourceContext),
    ConfirmedMismatch,
    Unavailable,
}

fn zotero_fresh(win: &FocusedWindow, env: &EnrichmentEnv, now: i64) -> ProviderFresh {
    let Some(path) = env.zotero_file.as_ref() else {
        return ProviderFresh::Unavailable;
    };
    match read_zotero_explicit(path, win, now) {
        ZoteroRead::Matched(r) => ProviderFresh::Value(r),
        ZoteroRead::ConfirmedMismatch => ProviderFresh::Mismatch,
        ZoteroRead::Unavailable => ProviderFresh::Unavailable,
    }
}

fn read_zotero_explicit(path: &Path, win: &FocusedWindow, now: i64) -> ZoteroRead {
    let Some((raw, mtime)) = read_private_file(path) else {
        return ZoteroRead::Unavailable;
    };
    let Ok(v) = serde_json::from_str::<serde_json::Value>(&raw) else {
        return ZoteroRead::Unavailable;
    };
    let Some(obj) = v.as_object() else {
        return ZoteroRead::Unavailable;
    };
    // Exact binding first: opaque compositor window id AND pid must both match.
    // A readable record for another window (or without binding) is a confirmed
    // mismatch, never a transient. This is the verified bridge binding — the
    // plugin itself never invents it (see module docs).
    let rec_window = obj.get("window_id").and_then(|x| x.as_str()).unwrap_or("");
    if rec_window.is_empty() || rec_window != win.id {
        return ZoteroRead::ConfirmedMismatch;
    }
    let Some(focused_pid) = win.process_id else {
        return ZoteroRead::ConfirmedMismatch;
    };
    let rec_pid = match obj.get("pid") {
        Some(serde_json::Value::Number(n)) => n.as_u64().and_then(|x| u32::try_from(x).ok()),
        Some(serde_json::Value::String(s)) => s.parse::<u32>().ok(),
        _ => None,
    };
    if rec_pid != Some(focused_pid) {
        return ZoteroRead::ConfirmedMismatch;
    }
    let updated = obj.get("updated_at_ms").and_then(|x| x.as_i64());
    if !record_is_fresh(updated, mtime, now) {
        return ZoteroRead::ConfirmedMismatch;
    }
    // Closed reader tombstone: explicit closed state or missing item identity
    // evicts (never serves the previous document as last-good).
    if obj.get("state").and_then(|x| x.as_str()) == Some("closed") {
        return ZoteroRead::ConfirmedMismatch;
    }
    let Some(zc) = parse_zotero_context(obj) else {
        return ZoteroRead::ConfirmedMismatch;
    };
    let title = obj
        .get("title")
        .and_then(|x| x.as_str())
        .filter(|s| !s.trim().is_empty())
        .map(|s| {
            let t = s.trim();
            if t.chars().count() <= 1024 {
                t.to_string()
            } else {
                t.chars().take(1024).collect()
            }
        });
    let uri = zc.uri.clone();
    let r = ResourceContext::new(
        "zotero",
        None,
        None,
        None,
        None,
        uri.as_deref(),
        None,
        title.as_deref(),
    )
    .with_zotero(Some(zc));
    if r.is_empty() {
        return ZoteroRead::ConfirmedMismatch;
    }
    ZoteroRead::Matched(r)
}

/// Parse + bound the typed Zotero identity from a verified record object.
/// Returns `None` on any missing/invalid stable field (fail closed). Only
/// metadata is read: server/library/item/attachment keys, current direct
/// memberships + ancestor collection keys, version, stable URI. Page numbers,
/// annotations, and content are never read.
pub fn parse_zotero_context(obj: &serde_json::Map<String, serde_json::Value>) -> Option<crate::desktop_context::ZoteroContext> {
    let server_id = obj.get("server_id")?.as_str()?;
    if !is_zotero_server_id(server_id) {
        return None;
    }
    let library_type = obj.get("library_type")?.as_str()?;
    if library_type != "user" && library_type != "group" {
        return None;
    }
    let library_id = obj.get("library_id")?.as_str()?;
    if !is_zotero_library_id(library_id) {
        return None;
    }
    if library_type == "group" && library_id == "0" {
        return None;
    }
    let item_key = obj.get("item_key")?.as_str()?;
    if !is_zotero_key(item_key) {
        return None;
    }
    let attachment_key = match obj.get("attachment_key") {
        None | Some(serde_json::Value::Null) => None,
        Some(serde_json::Value::String(s)) if s.is_empty() => None,
        Some(serde_json::Value::String(s)) if is_zotero_key(s) => Some(s.clone()),
        _ => return None,
    };
    let str_list = |key: &str| -> Option<Vec<String>> {
        match obj.get(key) {
            None | Some(serde_json::Value::Null) => Some(Vec::new()),
            Some(serde_json::Value::Array(arr)) => {
                if arr.len() > 256 {
                    return None;
                }
                let mut out = Vec::new();
                for x in arr {
                    let s = x.as_str()?;
                    if !is_zotero_key(s) {
                        return None;
                    }
                    if !out.contains(&s.to_string()) {
                        out.push(s.to_string());
                    }
                }
                Some(out)
            }
            _ => None,
        }
    };
    let collections = str_list("collections")?;
    let ancestor_collections = str_list("ancestor_collections")?;
    // Back-compat: older publishers wrote `ancestors`.
    let ancestor_collections = if ancestor_collections.is_empty() {
        str_list("ancestors").unwrap_or_default()
    } else {
        ancestor_collections
    };
    let version = match obj.get("version") {
        None | Some(serde_json::Value::Null) => None,
        Some(serde_json::Value::Number(n)) => {
            let x = n.as_i64()?;
            if x < 0 {
                return None;
            }
            Some(x)
        }
        _ => return None,
    };
    let uri = match obj.get("zotero_uri").or_else(|| obj.get("uri")) {
        None | Some(serde_json::Value::Null) => None,
        Some(serde_json::Value::String(s)) if s.trim().is_empty() => None,
        Some(serde_json::Value::String(s)) if is_zotero_uri(s) => {
            Some(s.trim().to_string())
        }
        // A malformed URI never fails the whole record: identity still binds,
        // display/search just omit the link.
        Some(serde_json::Value::String(_)) => None,
        _ => return None,
    };
    Some(crate::desktop_context::ZoteroContext {
        server_id: server_id.trim().to_string(),
        library_type: library_type.to_string(),
        library_id: library_id.to_string(),
        item_key: item_key.to_string(),
        attachment_key,
        collections,
        ancestor_collections,
        version,
        uri,
    })
}

// ---------------------------------------------------------------------------
// Logseq (title-only)
// ---------------------------------------------------------------------------
//
// The local HTTP `getCurrentPage` API was removed: it reports a process-
// global page with no verifiable per-window binding (a configured window id
// cannot prove the global value belongs to the focused window without
// compositor polling), so any attributed page would be deceptive. Logseq is
// title-only: the full window title plus an explicit, documented page
// extraction from the window title. Two shapes are accepted: the
// browser-tab form `<page> - Logseq` (exact suffix stripped, then trailing
// `-`/whitespace trimmed) and the Electron-app bare page name (the trimmed
// title used as-is). Validation here is structural only (non-empty,
// <=1024 chars, no control chars, not the app name itself, no trailing
// loading ellipsis); project association is exact registry matching in the
// resolver, never a guess here.
fn logseq_resource(focused_title: &str) -> Option<ResourceContext> {
    let title = focused_title.trim();
    if title.is_empty() {
        return None;
    }
    Some(ResourceContext::new(
        "logseq-title",
        None,
        None,
        None,
        None,
        None,
        logseq_page_from_title(title).as_deref(),
        Some(title),
    ))
}

/// Explicit page extraction from a Logseq window title. Accepts both the
/// browser-tab form `<page> - Logseq` (exact suffix stripped, then trailing
/// `-`/whitespace trimmed exactly as before) and the Electron-app bare page
/// name (the trimmed title used as-is). Validation is structural only:
/// non-empty, `chars().count() <= 1024`, no `'\0'` or other control chars
/// (`char::is_control`), not the app window name (`"logseq"`,
/// case-insensitive), and no trailing loading-ellipsis placeholder (`"..."` or
/// `"…"`). Returns `None` for anything that fails validation (never a guess);
/// project association is exact-match in the resolver.
pub fn logseq_page_from_title(title: &str) -> Option<String> {
    let trimmed = title.trim();
    if trimmed.is_empty() {
        return None;
    }
    let candidate = if let Some(stripped) = trimmed.strip_suffix("- Logseq") {
        let page = stripped.trim_end();
        let page = page.strip_suffix('-').unwrap_or(page).trim();
        page
    } else {
        trimmed
    };
    if candidate.is_empty() || candidate.chars().count() > 1024 {
        return None;
    }
    if candidate.chars().any(|c| c.is_control()) {
        return None;
    }
    if candidate.eq_ignore_ascii_case("logseq") {
        return None;
    }
    if candidate.ends_with("...") || candidate.ends_with('…') {
        return None;
    }
    Some(candidate.to_string())
}

// ---------------------------------------------------------------------------
// Git (bounded, no shell, no file-content reads)
// ---------------------------------------------------------------------------

/// Outcome of [`attach_git`]: whether the anchor resolved to a repository.
/// `NonRepo` is explicit (confirmed by git's own error text) and tells the
/// caller to clear metadata rather than back-fill it as if transient;
/// `Unknown` (timeout, missing binary) leaves the fill path available.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GitOutcome {
    NoAnchor,
    Repo,
    NonRepo,
    Unknown,
}

/// Attach `git_root`/`git_branch` derived from the resource's file parent or
/// cwd. A transient branch failure leaves `git_branch` unset rather than
/// inventing a removal (the last-good cache in [`apply_last_good`] restores
/// it when the anchor is unchanged). A confirmed non-repo clears both
/// fields.
pub fn attach_git(r: &mut ResourceContext) -> GitOutcome {
    let anchor = r
        .file
        .as_deref()
        .and_then(parent_dir_of)
        .or_else(|| r.cwd.clone());
    let Some(dir) = anchor.and_then(|d| sanitize_abs_path(&d)) else {
        return GitOutcome::NoAnchor;
    };
    match git_toplevel_status(&dir) {
        GitStatus::Unknown => GitOutcome::Unknown,
        GitStatus::NonRepo => {
            r.git_root = None;
            r.git_branch = None;
            GitOutcome::NonRepo
        }
        GitStatus::Repo(root) => {
            r.git_root = Some(root);
            // Partial failure (root ok, branch flaky): keep branch UNSET;
            // the caller-side last-good fill restores the previous branch
            // for the same anchor instead of persisting a removal row.
            if let Some(branch) = git_branch_for(&dir) {
                r.git_branch = Some(branch);
            }
            GitOutcome::Repo
        }
    }
}

/// Toplevel lookup with failure classification.
#[derive(Debug, PartialEq, Eq)]
enum GitStatus {
    Repo(String),
    NonRepo,
    Unknown,
}

fn git_toplevel_status(dir: &str) -> GitStatus {
    if sanitize_abs_path(dir).is_none() {
        return GitStatus::Unknown;
    }
    match run_git_captured(&["-C", dir, "rev-parse", "--show-toplevel"]) {
        None => GitStatus::Unknown,
        Some((false, _, stderr)) if is_not_a_repo(&stderr) => GitStatus::NonRepo,
        Some((false, _, _)) => GitStatus::Unknown,
        Some((true, stdout, _)) => match sanitize_abs_path(stdout.trim()) {
            Some(root) if !root.is_empty() => GitStatus::Repo(root),
            _ => GitStatus::Unknown,
        },
    }
}

fn is_not_a_repo(stderr: &str) -> bool {
    // git's own stable phrasing (covers worktree and submodule variants).
    stderr.contains("not a git repository")
}

/// Bounded `git` run capturing stdout + a bounded stderr snippet for
/// failure classification. `None` on spawn failure/timeout/oversize.
/// `LC_ALL=C` pins git's stderr phrasing so "not a git repository" matches
/// regardless of the process locale.
fn run_git_captured(args: &[&str]) -> Option<(bool, String, String)> {
    use std::io::Read;
    for a in args {
        if a.contains('\0') || a.len() > 4096 {
            return None;
        }
    }
    let mut child = std::process::Command::new("git")
        .args(args)
        .env("LC_ALL", "C")
        .env("LANG", "C")
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .ok()?;
    let mut out = child.stdout.take()?;
    let mut err = child.stderr.take()?;
    let start = std::time::Instant::now();
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut obuf = Vec::new();
        let mut ebuf = Vec::new();
        let mut chunk = [0u8; 4096];
        let mut over = false;
        loop {
            let mut progressed = false;
            match out.read(&mut chunk) {
                Ok(0) => {}
                Ok(n) => {
                    progressed = true;
                    obuf.extend_from_slice(&chunk[..n]);
                    if obuf.len() > MAX_GIT_OUTPUT_BYTES + 1 {
                        over = true;
                        break;
                    }
                }
                Err(_) => break,
            }
            match err.read(&mut chunk) {
                Ok(0) => {}
                Ok(n) => {
                    progressed = true;
                    ebuf.extend_from_slice(&chunk[..n]);
                    if ebuf.len() > 2048 + 1 {
                        over = true;
                        break;
                    }
                }
                Err(_) => break,
            }
            if !progressed {
                break;
            }
        }
        let _ = tx.send((obuf, ebuf, over));
    });
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let (obuf, ebuf, over) =
                    rx.recv_timeout(Duration::from_millis(200)).unwrap_or_default();
                if over
                    || obuf.len() > MAX_GIT_OUTPUT_BYTES
                    || ebuf.len() > 2048
                {
                    return None;
                }
                let stdout = String::from_utf8(obuf).unwrap_or_default();
                let stderr = String::from_utf8_lossy(&ebuf).to_string();
                return Some((status.success(), stdout, stderr));
            }
            Ok(None) => {
                if start.elapsed() >= GIT_TIMEOUT {
                    let _ = child.kill();
                    let _ = child.wait();
                    return None;
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            Err(_) => {
                let _ = child.kill();
                return None;
            }
        }
    }
}

fn parent_dir_of(file: &str) -> Option<String> {
    let p = Path::new(file);
    p.parent().map(|d| {
        let s = d.to_string_lossy().to_string();
        if s.is_empty() {
            "/".to_string()
        } else {
            s
        }
    })
}

pub fn git_root_for(dir: &str) -> Option<String> {
    sanitize_abs_path(dir)?;
    let out = run_bounded(
        "git",
        &["-C", dir, "rev-parse", "--show-toplevel"],
        GIT_TIMEOUT,
        MAX_GIT_OUTPUT_BYTES,
    )?;
    let t = out.trim();
    if t.is_empty() {
        return None;
    }
    sanitize_abs_path(t)
}

pub fn git_branch_for(dir: &str) -> Option<String> {
    sanitize_abs_path(dir)?;
    let out = run_bounded(
        "git",
        &["-C", dir, "rev-parse", "--abbrev-ref", "HEAD"],
        GIT_TIMEOUT,
        MAX_GIT_OUTPUT_BYTES,
    )?;
    let t = out.trim();
    if t.is_empty() {
        return None;
    }
    if t == "HEAD" {
        let sha = run_bounded(
            "git",
            &["-C", dir, "rev-parse", "--short", "HEAD"],
            GIT_TIMEOUT,
            MAX_GIT_OUTPUT_BYTES,
        )?;
        let sha = sha.trim();
        if sha.is_empty() || sha.len() > 64 || sha.contains(|c: char| c.is_whitespace()) {
            return None;
        }
        let label = format!("detached:{sha}");
        if label.chars().count() > 256 {
            return None;
        }
        return Some(label);
    }
    if t.chars().count() > 256 || t.contains('\0') || t.contains(|c: char| c.is_control()) {
        return None;
    }
    Some(t.to_string())
}

/// Bounded subprocess runner: no shell, argv only, wall-time timeout, output
/// cap (stdout only, stderr discarded). Returns trimmed stdout on exit 0.
fn run_bounded(cmd: &str, args: &[&str], timeout: Duration, max_bytes: usize) -> Option<String> {
    use std::io::Read;
    if cmd.is_empty() || cmd.contains('/') || cmd.contains('\0') {
        if cmd != "git" && cmd != "kitty" {
            return None;
        }
    }
    for a in args {
        if a.contains('\0') || a.len() > 4096 {
            return None;
        }
    }
    let mut child = std::process::Command::new(cmd)
        .args(args)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .spawn()
        .ok()?;
    let mut stdout = child.stdout.take()?;
    let start = std::time::Instant::now();
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut buf = Vec::new();
        let mut chunk = [0u8; 4096];
        loop {
            match stdout.read(&mut chunk) {
                Ok(0) => break,
                Ok(n) => {
                    buf.extend_from_slice(&chunk[..n]);
                    if buf.len() > max_bytes + 1 {
                        break;
                    }
                }
                Err(_) => break,
            }
        }
        let _ = tx.send(buf);
    });
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let buf = rx.recv_timeout(Duration::from_millis(200)).unwrap_or_default();
                if !status.success() {
                    return None;
                }
                if buf.len() > max_bytes {
                    return None;
                }
                return String::from_utf8(buf).ok();
            }
            Ok(None) => {
                if start.elapsed() >= timeout {
                    let _ = child.kill();
                    let _ = child.wait();
                    return None;
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            Err(_) => {
                let _ = child.kill();
                return None;
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Generic provider impls
// ---------------------------------------------------------------------------

/// Kitty provider: focused kitty windows only.
pub struct KittyProvider {
    pub env: EnrichmentEnv,
    pub now: i64,
}

impl ResourceProvider for KittyProvider {
    fn name(&self) -> &'static str {
        "kitty"
    }
    fn enrich(&self, ctx: &DesktopContext) -> Option<ResourceContext> {
        let win = ctx.focused_window.as_ref()?;
        if !ctx.available || !win.application.to_lowercase().contains("kitty") {
            return None;
        }
        match kitty_fresh(win, &self.env, self.now) {
            (ProviderFresh::Value(r), _) => Some(r),
            _ => None,
        }
    }
}

/// Honest Zen title fallback provider.
pub struct ZenProvider {
    pub env: EnrichmentEnv,
    pub now: i64,
}

impl ResourceProvider for ZenProvider {
    fn name(&self) -> &'static str {
        "zen"
    }
    fn enrich(&self, ctx: &DesktopContext) -> Option<ResourceContext> {
        let win = ctx.focused_window.as_ref()?;
        if !ctx.available || !win.application.to_lowercase().contains("zen") {
            return None;
        }
        zen_resource(win, &win.title, &self.env, self.now)
    }
}

/// Logseq provider (bound API else title fallback).
pub struct LogseqProvider {
    pub env: EnrichmentEnv,
}

impl ResourceProvider for LogseqProvider {
    fn name(&self) -> &'static str {
        "logseq"
    }
    fn enrich(&self, ctx: &DesktopContext) -> Option<ResourceContext> {
        let win = ctx.focused_window.as_ref()?;
        if !ctx.available || !win.application.to_lowercase().contains("logseq") {
            return None;
        }
        logseq_resource(&win.title)
    }
}

/// Zotero provider: explicit verified active-reader only, never title-based.
/// Unconfigured or unbound Zotero windows yield `None` (fail closed).
pub struct ZoteroProvider {
    pub env: EnrichmentEnv,
    pub now: i64,
}

impl ResourceProvider for ZoteroProvider {
    fn name(&self) -> &'static str {
        "zotero"
    }
    fn enrich(&self, ctx: &DesktopContext) -> Option<ResourceContext> {
        let win = ctx.focused_window.as_ref()?;
        if !ctx.available || !win.application.to_lowercase().contains("zotero") {
            return None;
        }
        match zotero_fresh(win, &self.env, self.now) {
            ProviderFresh::Value(r) => Some(r),
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env_empty() -> EnrichmentEnv {
        EnrichmentEnv::default()
    }

    #[test]
    fn unknown_app_no_resource() {
        clear_last_good_cache();
        let ctx = DesktopContext::available(
            crate::desktop_context::Source::Hyprland,
            Some(crate::desktop_context::FocusedWindow::new("0x1", "foot", "shell")),
            None,
            1,
        );
        let out = enrich_with_env(ctx.clone(), &env_empty(), 1_000_000);
        assert_eq!(out.resource, None);
        assert!(out.semantic_eq(&ctx));
    }

    #[test]
    fn zen_fallback_title_no_url() {
        clear_last_good_cache();
        let ctx = DesktopContext::available(
            crate::desktop_context::Source::Hyprland,
            Some(crate::desktop_context::FocusedWindow::new("0x1", "zen", "Example Page — Zen")),
            None,
            1,
        );
        let out = enrich_with_env(ctx, &env_empty(), 1_000_000);
        let r = out.resource.expect("zen fallback");
        assert_eq!(r.adapter, "zen-title");
        assert_eq!(r.url, None, "never invent a URL");
        assert_eq!(r.title.as_deref(), Some("Example Page — Zen"));
    }

    #[test]
    fn logseq_title_only_with_page_extraction() {
        clear_last_good_cache();
        let ctx = DesktopContext::available(
            crate::desktop_context::Source::Hyprland,
            Some(crate::desktop_context::FocusedWindow::new("0x1", "Logseq", "My Page - Logseq")),
            None,
            1,
        );
        let out = enrich_with_env(ctx, &env_empty(), 1_000_000);
        let r = out.resource.expect("logseq fallback");
        assert_eq!(r.adapter, "logseq-title");
        assert_eq!(r.title.as_deref(), Some("My Page - Logseq"));
        assert_eq!(r.page.as_deref(), Some("My Page"));
        // No API leftovers: env carries no endpoint config.
        assert!(env_empty().kitty_socket.is_none());
    }

    #[test]
    fn logseq_page_extraction_rules() {
        assert_eq!(
            logseq_page_from_title("My Page - Logseq").as_deref(),
            Some("My Page")
        );
        assert_eq!(
            logseq_page_from_title("  Journal / 2024 - Logseq  ").as_deref(),
            Some("Journal / 2024")
        );
        assert!(logseq_page_from_title("Logseq").is_none());
        assert!(logseq_page_from_title("").is_none());
        assert_eq!(
            logseq_page_from_title("a - Logseq - extra").as_deref(),
            Some("a - Logseq - extra")
        );
        // Bare Electron-app titles are page names.
        assert_eq!(
            logseq_page_from_title("Asset-Prices").as_deref(),
            Some("Asset-Prices")
        );
        assert_eq!(
            logseq_page_from_title("Person/Grace").as_deref(),
            Some("Person/Grace")
        );
        // Loading-placeholder transients are rejected.
        assert!(logseq_page_from_title("Lädt...").is_none());
        assert!(logseq_page_from_title("Loading…").is_none());
        // Whitespace is trimmed.
        assert_eq!(
            logseq_page_from_title("  Asset-Prices  ").as_deref(),
            Some("Asset-Prices")
        );
        // Embedded control chars are rejected.
        assert!(logseq_page_from_title("a\tb").is_none());
        assert!(logseq_page_from_title("a\0b").is_none());
        // Overlong candidates are rejected.
        assert!(logseq_page_from_title(&"a".repeat(1025)).is_none());
        assert_eq!(
            logseq_page_from_title(&"a".repeat(1024)).as_deref(),
            Some("a".repeat(1024).as_str())
        );
    }

    #[test]
    fn logseq_bare_title_enriches_page() {
        clear_last_good_cache();
        let ctx = DesktopContext::available(
            crate::desktop_context::Source::Hyprland,
            Some(crate::desktop_context::FocusedWindow::new(
                "0x1",
                "Logseq",
                "Asset-Prices",
            )),
            None,
            1,
        );
        let out = enrich_with_env(ctx, &env_empty(), 1_000_000);
        let r = out.resource.expect("logseq bare-title fallback");
        assert_eq!(r.adapter, "logseq-title");
        assert_eq!(r.title.as_deref(), Some("Asset-Prices"));
        assert_eq!(r.page.as_deref(), Some("Asset-Prices"));
    }

    #[test]
    fn nvim_record_parse_and_freshness() {
        let raw = r#"{"pid":123,"kitty_window_id":5,"file":"/tmp/a.md","cwd":"/tmp","updated_at_ms":1000000}"#;
        let rec = parse_nvim_record(raw).expect("parse");
        assert_eq!(rec.pid, 123);
        assert_eq!(rec.kitty_window_id, Some(5));
        assert!(record_is_fresh(rec.updated_at_ms, None, 1_000_010));
        assert!(!record_is_fresh(rec.updated_at_ms, None, 2_000_000));
    }

    #[test]
    fn kitty_socket_arg_requires_unix_prefix() {
        let argv = kitty_ls_argv(Path::new("/tmp/kitty.sock")).expect("argv");
        assert_eq!(argv, vec!["kitty", "@", "--to", "unix:/tmp/kitty.sock", "ls"]);
        assert!(normalize_kitty_socket_arg(Path::new("relative.sock")).is_none());
        assert!(normalize_kitty_socket_arg(Path::new("")).is_none());
    }

    #[test]
    fn kitty_unique_focus_required_and_peer_bound() {
        clear_last_good_cache();
        // Real-shaped: kitty pid 4001 (socket peer == focused), shell 5001,
        // nvim 6001, pane id 7 — all distinct, no id==pid conflation.
        let raw = r#"[{"id":11,"is_focused":true,"tabs":[{"id":21,"is_focused":true,"windows":[{"id":7,"is_focused":true,"pid":5001,"cwd":"/tmp","title":"nvim","foreground_processes":[{"pid":5001,"cmdline":["fish"],"cwd":"/tmp"},{"pid":6001,"cmdline":["nvim","/tmp/a.md"],"cwd":"/tmp"}]}]}]}]"#;
        // Peer mismatch -> None even though a focused pane exists.
        assert!(select_kitty_pane(raw, Some(4001), Some(9999)).is_none());
        // Missing peer -> None (no guessing).
        assert!(select_kitty_pane(raw, Some(4001), None).is_none());
        let pane = select_kitty_pane(raw, Some(4001), Some(4001)).expect("bound select");
        assert_eq!(pane.window_id, "7");
        assert_eq!(pane.cwd.as_deref(), Some("/tmp"));
        // Ambiguous: two focused OS windows -> None.
        let amb = r#"[{"id":1,"is_focused":true,"tabs":[{"id":1,"is_focused":true,"windows":[{"id":1,"is_focused":true,"pid":1,"cwd":"/tmp","title":"a"}]}]},{"id":2,"is_focused":true,"tabs":[{"id":2,"is_focused":true,"windows":[{"id":2,"is_focused":true,"pid":2,"cwd":"/tmp","title":"b"}]}]}]"#;
        assert!(select_kitty_pane(amb, Some(4001), Some(4001)).is_none());
        // No focused pane -> None (no positional fallback).
        let nofocus = r#"[{"id":1,"is_focused":true,"tabs":[{"id":1,"is_focused":true,"windows":[{"id":9,"is_focused":false,"pid":1,"cwd":"/tmp","title":"a"}]}]}]"#;
        assert!(select_kitty_pane(nofocus, Some(4001), Some(4001)).is_none());
    }

    #[test]
    fn kitty_fs_path_accepts_both_forms() {
        assert_eq!(
            kitty_fs_path(Path::new("unix:/tmp/k.sock")),
            Some(PathBuf::from("/tmp/k.sock"))
        );
        assert_eq!(
            kitty_fs_path(Path::new("/tmp/k.sock")),
            Some(PathBuf::from("/tmp/k.sock"))
        );
        assert!(kitty_fs_path(Path::new("relative.sock")).is_none());
    }

    #[test]
    fn peer_mismatch_evicts_without_serving_last_good() {
        clear_last_good_cache();
        // Prime the cache for a kitty focus key through the tri-state path
        // is impossible without `ls`; prime directly at the guard level,
        // then verify a confirmed mismatch evicts instead of serving.
        let key = focus_key(&crate::desktop_context::FocusedWindow::new_with_pid(
            "0xM", "kitty", "t", Some(111),
        ));
        let primed = ResourceContext::new(
            "kitty",
            None,
            Some("/tmp"),
            Some("/tmp"),
            Some("main"),
            None,
            None,
            None,
        );
        assert_eq!(
            apply_last_good(&key, ProviderFresh::Value(primed.clone()), 1000, false, None),
            Some(primed.clone())
        );
        // Mismatch (peer belongs to another instance) evicts, returns None.
        assert_eq!(
            apply_last_good(&key, ProviderFresh::Mismatch, 2000, false, None),
            None
        );
        // A later Unavailable finds nothing (evicted, not served).
        assert_eq!(
            apply_last_good(&key, ProviderFresh::Unavailable, 3000, false, None),
            None
        );
    }

    #[test]
    fn git_nonrepo_is_none() {
        let dir = std::env::temp_dir().join(format!("qs-norepo-{}-{}", std::process::id(), now_ms()));
        std::fs::create_dir_all(&dir).unwrap();
        let s = dir.to_string_lossy().to_string();
        assert!(git_root_for(&s).is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn sanitize_rejects_relative_and_nul() {
        assert!(sanitize_abs_path("relative/path").is_none());
        assert!(sanitize_abs_path("/ok/path").is_some());
        assert!(sanitize_abs_path("/bad\0path").is_none());
    }

    #[test]
    fn last_good_serves_transient_none_and_fills_git() {
        clear_last_good_cache();
        let key = "kitty\00x1\0111".to_string();
        let full = ResourceContext::new(
            "kitty",
            None,
            Some("/tmp"),
            Some("/tmp"),
            Some("main"),
            None,
            None,
            None,
        );
        let v = |r: ResourceContext| ProviderFresh::Value(r);
        assert_eq!(
            apply_last_good(&key, v(full.clone()), 1000, false, None),
            Some(full.clone())
        );
        // Transient unavailable -> cached served, no oscillation.
        assert_eq!(
            apply_last_good(&key, ProviderFresh::Unavailable, 2000, false, None),
            Some(full.clone())
        );
        // Expired -> None.
        assert_eq!(
            apply_last_good(&key, ProviderFresh::Unavailable, 1000 + LAST_GOOD_TTL_MS + 1, false, None),
            None
        );
        // Partial git fill: fresh missing branch, same anchor -> restored.
        let partial = ResourceContext::new(
            "kitty",
            None,
            Some("/tmp"),
            Some("/tmp"),
            None,
            None,
            None,
            None,
        );
        // Re-store full first (within TTL).
        assert_eq!(
            apply_last_good(&key, v(full.clone()), 3000, false, None),
            Some(full.clone())
        );
        assert_eq!(
            apply_last_good(&key, v(partial.clone()), 3100, false, None),
            Some(full.clone()),
            "transient branch loss must be filled, not persisted as removal"
        );
        // Carried-forward git must NOT renew the window: past the original
        // TTL the partial no longer fills, even with repeated misses.
        assert_eq!(
            apply_last_good(&key, v(partial), 3000 + LAST_GOOD_TTL_MS + 1, false, None)
                .unwrap()
                .git_branch,
            None,
            "git TTL must expire despite repeated partial resolutions"
        );
        // Confirmed non-repo stores as-is (evicts old git, no fill).
        let norepo = ResourceContext::new(
            "kitty", None, Some("/tmp"), None, None, None, None, None,
        );
        assert_eq!(
            apply_last_good(&key, v(norepo.clone()), 4000, true, None),
            Some(norepo),
            "confirmed non-repo must clear metadata, not back-fill"
        );
    }

    #[test]
    fn scoped_serve_requires_same_pane_and_pid() {
        clear_last_good_cache();
        let key = "kitty\00xK\0999".to_string();
        let scope_a = || {
            Some(EditorIdentity {
                kitty_window_id: "7".to_string(),
                nvim_pid: Some(4321),
            })
        };
        let scope_b = || {
            Some(EditorIdentity {
                kitty_window_id: "8".to_string(),
                nvim_pid: Some(4322),
            })
        };
        let file_a = || {
            ProviderFresh::Value(ResourceContext::new(
                "neovim",
                Some("/tmp/stage/note.md"),
                Some("/tmp/stage"),
                None,
                None,
                None,
                None,
                None,
            ))
        };
        // Prime pane 7 / editor 4321.
        assert_eq!(
            apply_last_good(&key, file_a(), 1000, false, scope_a()),
            Some(ResourceContext::new(
                "neovim",
                Some("/tmp/stage/note.md"),
                Some("/tmp/stage"),
                None,
                None,
                None,
                None,
                None,
            ))
        );
        // Same pane + PID, record missing: scoped serve, no renew.
        assert_eq!(
            apply_last_good(&key, ProviderFresh::Unavailable, 2000, false, scope_a()),
            Some(ResourceContext::new(
                "neovim",
                Some("/tmp/stage/note.md"),
                Some("/tmp/stage"),
                None,
                None,
                None,
                None,
                None,
            )),
            "same pane+pid must serve the old editor file"
        );
        // Different pane/editor: evict, never serve.
        assert_eq!(
            apply_last_good(&key, ProviderFresh::Unavailable, 3000, false, scope_b()),
            None,
            "pane switch must not serve the previous pane's file"
        );
        // Evicted: even the original scope finds nothing now.
        assert_eq!(
            apply_last_good(&key, ProviderFresh::Unavailable, 3100, false, scope_a()),
            None
        );
    }

    #[test]
    fn focus_key_includes_pid() {
        use crate::desktop_context::FocusedWindow;
        let a = FocusedWindow::new_with_pid("0x1", "kitty", "t", Some(100));
        let b = FocusedWindow::new_with_pid("0x1", "kitty", "t", Some(200));
        assert_ne!(focus_key(&a), focus_key(&b));
    }

    #[test]
    fn refresh_title_fallback_recomputes_only_fallbacks() {
        let zen_old = ResourceContext::new(
            "zen-title", None, None, None, None, None, None, Some("Old Title"),
        );
        // Same fallback kind, new title: recomputed synchronously.
        assert_eq!(
            refresh_title_fallback(&zen_old, "zen", "New Title"),
            Some(ResourceContext::new(
                "zen-title", None, None, None, None, None, None, Some("New Title")
            )
            .into()),
            "stale fallback title must refresh, not correct later"
        );
        let log_old = ResourceContext::new(
            "logseq-title", None, None, None, None, None, Some("Old"), Some("Old - Logseq"),
        );
        assert_eq!(
            refresh_title_fallback(&log_old, "Logseq", "New - Logseq")
                .unwrap()
                .unwrap()
                .page
                .as_deref(),
            Some("New")
        );
        // Empty title: honestly absent (nothing carries forward).
        assert_eq!(refresh_title_fallback(&zen_old, "zen", "   "), Some(None));
        // Non-fallback adapters are not touched: caller carries the overlay.
        let nvim = ResourceContext::new(
            "neovim", Some("/tmp/a.md"), Some("/tmp"), None, None, None, None, None,
        );
        assert_eq!(refresh_title_fallback(&nvim, "kitty", "t"), None);
        // Adapter/app mismatch is not a fallback either.
        assert_eq!(refresh_title_fallback(&zen_old, "logseq", "New - Logseq"), None);
    }

    #[test]
    fn resolve_rejects_uri_like_paths() {
        assert!(resolve_nvim_file("fugitive:///tmp/.git//abc", Some("/tmp")).is_none());
        assert!(resolve_nvim_file("term://12345:6789", Some("/tmp")).is_none());
        assert!(resolve_nvim_file("file:///tmp/x.md", Some("/tmp")).is_none());
        assert!(resolve_nvim_file("https://example.com/x.md", Some("/tmp")).is_none());
        // Ordinary paths still resolve (relative joins cwd, absolute passes).
        assert_eq!(
            resolve_nvim_file("note.md", Some("/tmp")).as_deref(),
            Some("/tmp/note.md")
        );
        assert_eq!(
            resolve_nvim_file("/tmp/note.md", Some("/other")).as_deref(),
            Some("/tmp/note.md")
        );
    }

    #[test]
    fn kitty_candidate_sockets_order_dedup_and_filtering() {
        let pid = 424242u32;
        let default_tmp = PathBuf::from(format!("/tmp/kitty-{pid}"));
        // Configured socket is the preference, then the focused default.
        let cfg = PathBuf::from("/run/user/1000/kitty.sock");
        let c = kitty_candidate_sockets(Some(cfg.as_path()), pid);
        assert!(!c.is_empty(), "even empty env yields a derived default");
        assert_eq!(c[0], cfg, "configured socket stays first");
        assert!(c.contains(&default_tmp), "must contain /tmp/kitty-<pid>");
        assert!(c.iter().all(|p| p.is_absolute()), "absolute only");
        let mut seen = std::collections::HashSet::new();
        for p in &c {
            assert!(seen.insert(p.clone()), "no duplicates: {c:?}");
        }
        // No configured socket: derived default is still yielded.
        let none = kitty_candidate_sockets(None, pid);
        assert_eq!(none[0], default_tmp);
        assert!(none.iter().all(|p| p.is_absolute()));
        // Relative / empty / NUL configured values are filtered out.
        let rel = kitty_candidate_sockets(Some(Path::new("relative.sock")), pid);
        assert_eq!(rel[0], default_tmp, "relative configured must be skipped");
        let empty = kitty_candidate_sockets(Some(Path::new("")), pid);
        assert_eq!(empty[0], default_tmp, "empty configured must be skipped");
        // Configured socket equal to the default dedupes (no repeat).
        let dup = kitty_candidate_sockets(Some(default_tmp.as_path()), pid);
        assert_eq!(dup.iter().filter(|p| *p == &default_tmp).count(), 1);
    }

    #[test]
    fn select_kitty_socket_matched_skips_unreachable_first() {
        // Hermetic temp dir only: never bind literal /tmp/kitty-<pid>.
        let dir = std::env::temp_dir().join(format!(
            "qs-kitty-sel-{}-{}",
            std::process::id(),
            now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let sock = dir.join("second.sock");
        let _ = std::fs::remove_file(&sock);
        let listener = std::os::unix::net::UnixListener::bind(&sock).unwrap();
        let accept = std::thread::spawn(move || {
            // Accept the single probe connection; the peer credential is
            // the connecting process (this test binary) either way.
            if let Ok((s, _)) = listener.accept() {
                std::mem::forget(s);
            }
        });
        let missing = dir.join("first-missing.sock");
        let candidates = vec![missing.clone(), sock.clone()];
        let me = std::process::id();
        match select_kitty_socket(&candidates, me) {
            KittySocketChoice::Matched(path, peer) => {
                assert_eq!(path, sock, "first matching candidate wins");
                assert_eq!(peer, me);
            }
            other => panic!("expected Matched, got {other:?}"),
        }
        let _ = std::fs::remove_dir_all(&dir);
        let _ = accept.join();
    }

    #[test]
    fn select_kitty_socket_mismatch_and_unavailable() {
        let dir = std::env::temp_dir().join(format!(
            "qs-kitty-mis-{}-{}",
            std::process::id(),
            now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let sock = dir.join("only.sock");
        let _ = std::fs::remove_file(&sock);
        let listener = std::os::unix::net::UnixListener::bind(&sock).unwrap();
        let accept = std::thread::spawn(move || {
            if let Ok((s, _)) = listener.accept() {
                std::mem::forget(s);
            }
        });
        // Reachable socket whose peer (this process) differs from the
        // requested pid: confirmed foreign, never last-good.
        let foreign = std::process::id().wrapping_add(1);
        assert_ne!(foreign, std::process::id());
        assert_eq!(
            select_kitty_socket(std::slice::from_ref(&sock), foreign),
            KittySocketChoice::Mismatch
        );
        let _ = std::fs::remove_dir_all(&dir);
        let _ = accept.join();
        // Nothing reachable at all: transient unavailability.
        let gone = vec![
            std::env::temp_dir().join(format!("qs-kitty-gone-a-{}-{}", std::process::id(), now_ms())),
            std::env::temp_dir().join(format!("qs-kitty-gone-b-{}-{}", std::process::id(), now_ms())),
        ];
        assert_eq!(
            select_kitty_socket(&gone, std::process::id()),
            KittySocketChoice::Unavailable
        );
        assert_eq!(
            select_kitty_socket(&[], std::process::id()),
            KittySocketChoice::Unavailable
        );
    }

    #[test]
    fn unique_nvim_presence_classes() {
        let entry = |pid: Option<u32>, cmd: &str| ForegroundEntry {
            pid,
            cmdline: cmd.to_string(),
            cwd: None,
        };
        assert_eq!(unique_nvim_presence(&[]), NvimPresence::Absent);
        assert_eq!(
            unique_nvim_presence(&[entry(Some(1), "fish"), entry(Some(2), "nvim a.md")]),
            NvimPresence::Unique(2)
        );
        // Order-independent.
        assert_eq!(
            unique_nvim_presence(&[entry(Some(2), "/usr/bin/nvim a.md"), entry(Some(1), "fish")]),
            NvimPresence::Unique(2)
        );
        // Same pid twice is still one publisher.
        assert_eq!(
            unique_nvim_presence(&[entry(Some(2), "nvim a.md"), entry(Some(2), "nvim")]),
            NvimPresence::Unique(2)
        );
        // Two distinct nvim PIDs: ambiguous, never a guess.
        assert_eq!(
            unique_nvim_presence(&[entry(Some(2), "nvim a.md"), entry(Some(3), "nvim b.md")]),
            NvimPresence::Ambiguous
        );
        // Pid-less nvim entry: cannot verify the publisher, ambiguous.
        assert_eq!(
            unique_nvim_presence(&[entry(None, "nvim a.md")]),
            NvimPresence::Ambiguous
        );
        assert_eq!(
            unique_nvim_presence(&[entry(Some(2), "nvim a.md"), entry(None, "nvim")]),
            NvimPresence::Ambiguous
        );
        // Non-nvim only.
        assert_eq!(
            unique_nvim_presence(&[entry(Some(1), "fish -l")]),
            NvimPresence::Absent
        );
    }
}
