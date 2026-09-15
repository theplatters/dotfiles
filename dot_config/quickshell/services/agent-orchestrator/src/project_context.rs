//! Deterministic project resolution over the canonical registry.
//!
//! Canonical source of truth is the validated Python registry
//! (`scripts/projects.py list_projects(registry_file=None)`; CLI
//! `python3 scripts/projects.py list` emitting
//! `{projects:[{id,name,logseq_path,local_folder,github_url,path,page}],revision,file}`).
//! This module NEVER parses `projects.toml` and NEVER duplicates registry
//! validation: it only projects/caches that validated JSON output.
//!
//! Registry location honors `QUICKSHELL_PROJECTS_FILE` when non-empty,
//! else `<repo>/projects.toml` (the same precedence as the Python API).
//! The registry path is used lexically and never canonicalized: a symlinked
//! registry file is rejected by the helper itself (`O_NOFOLLOW`), so this
//! module fails closed on it instead of following the link.
//!
//! Cache contract (enrichment worker thread; thread-local default is fine):
//! - The bounded `list` subprocess runs ONLY when registry metadata changes
//!   (resolved path string, device, inode, mtime, ctime, length). An
//!   unchanged mapping never spawns Python per refresh.
//! - Bounded entries (5000) and bounded stdout (2 MiB), bounded wall time.
//! - Failures (missing helper, spawn/timeout/oversize/malformed JSON) fail
//!   closed: no association (`None`), stale mappings are never served, and a
//!   brief retry TTL (5 s) avoids hot-looping Python.
//! - Missing registry file reads as an empty mapping (no projects), still
//!   cached by metadata so deletion/creation invalidates.
//! - Mapped-folder filesystem projections are NOT frozen at registry load:
//!   the raw `local_folder` string is stored and each needed mapping is
//!   resolved lazily per precedence level with a short TTL (5 s, bounded to
//!   256 entries). A symlink retarget while the registry is unchanged
//!   converges after at most the TTL without spawning Python (bounded
//!   staleness <= 5 s; the old location stops matching once refreshed).
//!
//! Match contract:
//! - `ProjectContext { id, name, matched_by }` only (`file`/`cwd`/`git_root`/
//!   `git_remote`), serde-default on `DesktopContext.project`. Semantic
//!   equality includes these three (explainability) but never the registry
//!   `revision`.
//! - Path precedence `FILE > CWD > GIT_ROOT > GIT_REMOTE`, resolved lazily in
//!   order: the `FILE` candidate is canonicalized and matched first; weaker
//!   levels touch the filesystem only when no stronger level hit or tied.
//!   Each candidate is tilde-expanded then canonicalized with symlink-aware
//!   `..` semantics (the OS resolves `..` AFTER symlinks: `/work/link/../f`
//!   with `link -> /repos/B/sub` is `/repos/B/f`, never `/work/f`); a
//!   nonexistent trailing suffix keeps the resolved existing prefix's
//!   semantics, else fails closed. Descendant matching honors component
//!   boundaries (`/a/b` does not match `/a/bc/d`), longest mapped folder
//!   wins. A tie between different projects at the same strength is
//!   ambiguous: no association and NO fallback to a weaker strength. Titles
//!   are never inferred from.
//! - Remote fallback runs `git -C <root> config --local --includes --null
//!   --get-regexp ^remote\..*\.url$` (repository-local config only: no
//!   network, no shell, no global/system config, bounded time/output; ambient
//!   `GIT_DIR`/`GIT_WORK_TREE`/`GIT_CONFIG_*` hijacks are stripped from the
//!   child environment; raw URLs are never logged or persisted) and normalizes
//!   every URL: HTTPS/SSH/scp GitHub forms to `https://github.com/owner/repo`
//!   (single trailing slash / single `.git` stripped, host must be
//!   `github.com`/`www.github.com`, owner/repo `[A-Za-z0-9_.-]+`,
//!   case-insensitive compare, credentials stripped and never stored).
//!   Deterministic remote rule: normalize the repo's URLs, then compute the
//!   DISTINCT REGISTERED PROJECTS they claim. Exactly one distinct claimed
//!   project wins — even when the repo has other unregistered remotes (a fork
//!   with an unmapped upstream still matches the mapped origin). Zero claims,
//!   or claims on more than one distinct project, mean no association (never
//!   an arbitrary `origin` pick). This uses git's own config semantics, so
//!   comments, quoting, and linked worktrees (`commondir`) behave correctly.
//!   Discovery (including negative/empty) is cached with a 30 s TTL plus
//!   best-effort git-config/commondir metadata invalidation, bounded to 64
//!   repos. No subprocess runs for pure path matches.
//!
//! Filesystem limitation (documented, not retried): `stat`/`canonicalize`
//! are blocking syscalls with no timeout, so a broken network mount can stall
//! the calling worker thread. Exposure is limited by lazy per-level matching
//! (only needed candidates/mappings touch the filesystem); no wall-time
//! guarantee is attempted here.

use crate::desktop_context::{DesktopContext, ProjectContext, ResourceContext};
use std::cell::RefCell;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

/// Override env var honored exactly like the Python API.
pub const PROJECTS_FILE_ENV: &str = "QUICKSHELL_PROJECTS_FILE";
/// Bounded caches / IO.
pub const MAX_REGISTRY_ENTRIES: usize = 5000;
pub const MAX_REGISTRY_OUTPUT_BYTES: usize = 2 * 1024 * 1024;
pub const REGISTRY_TIMEOUT: Duration = Duration::from_millis(2000);
/// Brief failure retry TTL (fail closed, no hot-loop).
pub const REGISTRY_RETRY_TTL_MS: i64 = 5_000;
/// Remote discovery cache TTL + bound.
pub const REMOTE_TTL_MS: i64 = 30_000;
const REMOTE_CACHE_MAX: usize = 64;
/// Mapped-folder projection cache: short TTL so symlink retargets converge
/// without spawning Python (bounded staleness, see module docs).
pub const FOLDER_TTL_MS: i64 = 5_000;
const FOLDER_CACHE_MAX: usize = 256;
/// Bounded `git config` discovery.
pub const GIT_REMOTE_TIMEOUT: Duration = Duration::from_millis(1000);
const MAX_GIT_REMOTE_OUTPUT_BYTES: usize = 32 * 1024;
const MAX_GIT_REMOTES: usize = 64;
/// Cap for symlink-pointer file reads (worktree `gitdir:` / `commondir`).
const POINTER_READ_CAP: u64 = 4096;
const MAX_PATH_CHARS: usize = 4096;

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------

/// Resolve the registry file honoring `--projects-file > env > default`,
/// mirroring `scripts/projects.py resolve_registry_file` (explicit param
/// wins, else `QUICKSHELL_PROJECTS_FILE`, else `<repo>/projects.toml`).
pub fn resolve_registry_file(explicit: Option<&str>) -> PathBuf {
    if let Some(raw) = explicit {
        let t = raw.trim();
        if !t.is_empty() {
            return absolutize_lexical(&expand_tilde_lexical(t));
        }
    }
    if let Ok(raw) = std::env::var(PROJECTS_FILE_ENV) {
        let t = raw.trim().to_string();
        if !t.is_empty() {
            return absolutize_lexical(&expand_tilde_lexical(&t));
        }
    }
    default_registry_file()
}

/// Compile-time repo default: `<manifest>/../../projects.toml`.
pub fn default_registry_file() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("projects.toml")
}

/// Helper script sibling: `<manifest>/../../scripts/projects.py`.
pub fn helper_script_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("scripts")
        .join("projects.py")
}

fn expand_tilde_lexical(raw: &str) -> String {
    if raw == "~" || raw.starts_with("~/") {
        if let Ok(home) = std::env::var("HOME") {
            if !home.is_empty() {
                return format!("{home}{}", &raw[1..]);
            }
        }
    }
    raw.to_string()
}

fn absolutize_lexical(p: &str) -> PathBuf {
    let path = PathBuf::from(p);
    if path.is_absolute() {
        return path;
    }
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("/"));
    cwd.join(path)
}

// ---------------------------------------------------------------------------
// Normalization helpers (pub for tests)
// ---------------------------------------------------------------------------

/// Normalize a GitHub remote URL to canonical `https://github.com/o/r`.
///
/// Accepts HTTPS (`https://github.com/o/r[.git][/]`, `www.` normalized,
/// userinfo credentials stripped) and SSH/scp forms
/// (`git@github.com:o/r.git`, `ssh://git@github.com/o/r[.git]` with optional
/// `:port` and userinfo). Owner/repo must match `[A-Za-z0-9_.-]+`.
/// Anything else (other hosts/schemes, malformed paths, NUL/controls,
/// overlong) is `None`. No credentials are ever echoed back.
pub fn normalize_github_remote(raw: &str) -> Option<String> {
    let t = raw.trim();
    if t.is_empty() || t.len() > 2048 || t.contains('\0') {
        return None;
    }
    if t.chars().any(|c| c.is_control()) {
        return None;
    }
    let stripped = strip_surrounding_quotes(t);
    // scp-like: [user@]host:path
    if let Some(canonical) = normalize_scp_like(stripped) {
        return Some(canonical);
    }
    normalize_url_like(stripped)
}

fn strip_surrounding_quotes(s: &str) -> &str {
    let t = s.trim();
    if t.len() >= 2 {
        let b = t.as_bytes();
        let (f, l) = (b[0], b[t.len() - 1]);
        if (f == b'\'' && l == b'\'') || (f == b'"' && l == b'"') {
            return t[1..t.len() - 1].trim();
        }
    }
    t
}

fn owner_repo_ok(owner: &str, repo: &str) -> bool {
    if owner.is_empty() || repo.is_empty() {
        return false;
    }
    if owner.len() > 256 || repo.len() > 256 {
        return false;
    }
    let ok = |s: &str| {
        !s.is_empty()
            && s.len() <= 256
            && s.chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-'))
            && s != "."
            && s != ".."
    };
    ok(owner) && ok(repo)
}

fn strip_git_suffix(path: &str) -> &str {
    let no_slash = path.trim_end_matches('/');
    if no_slash.len() > 4 && no_slash.as_bytes()[no_slash.len() - 4..].eq_ignore_ascii_case(b".git") {
        no_slash[..no_slash.len() - 4].trim_end_matches('/')
    } else {
        no_slash
    }
}

fn normalize_scp_like(s: &str) -> Option<String> {
    // Reject anything with "://" (URL-like handled elsewhere) or whitespace.
    if s.contains("://") || s.chars().any(|c| c.is_whitespace()) {
        return None;
    }
    let colon = s.find(':')?;
    let (host_part, path_part) = s.split_at(colon);
    let path_part = &path_part[1..];
    if path_part.is_empty() || path_part.starts_with('/') {
        return None;
    }
    // host_part may carry userinfo: [user@]host
    let host = host_part.rsplit('@').next().unwrap_or(host_part);
    if !host.eq_ignore_ascii_case("github.com") && !host.eq_ignore_ascii_case("www.github.com") {
        return None;
    }
    let cleaned = strip_git_suffix(path_part.trim());
    let mut parts = cleaned.split('/').filter(|p| !p.is_empty());
    let owner = parts.next()?;
    let repo = parts.next()?;
    if parts.next().is_some() {
        return None;
    }
    if !owner_repo_ok(owner, repo) {
        return None;
    }
    Some(format!("https://github.com/{owner}/{repo}"))
}

fn normalize_url_like(s: &str) -> Option<String> {
    let (scheme, rest) = s.split_once("://")?;
    if !scheme.eq_ignore_ascii_case("https") && !scheme.eq_ignore_ascii_case("ssh") {
        return None;
    }
    // authority = up to next '/'
    let slash = rest.find('/')?;
    let (authority, path) = rest.split_at(slash);
    if authority.is_empty() {
        return None;
    }
    // Strip userinfo (last '@') — credentials never echoed.
    let hostport = authority.rsplit('@').next().unwrap_or(authority);
    // Strip :port if present (ssh may carry one).
    let host = match hostport.rsplit_once(':') {
        Some((h, port)) if !h.is_empty() && !port.contains(']') && port.chars().all(|c| c.is_ascii_digit()) && !port.is_empty() => h,
        _ => hostport,
    };
    if !host.eq_ignore_ascii_case("github.com") && !host.eq_ignore_ascii_case("www.github.com") {
        return None;
    }
    let cleaned = strip_git_suffix(path.trim());
    let mut parts = cleaned.split('/').filter(|p| !p.is_empty());
    let owner = parts.next()?;
    let repo = parts.next()?;
    if parts.next().is_some() {
        return None;
    }
    if !owner_repo_ok(owner, repo) {
        return None;
    }
    Some(format!("https://github.com/{owner}/{repo}"))
}

/// Canonicalize an absolute path string: tilde-expand, then resolve via the
/// filesystem with symlink-aware `..` semantics. The ORIGINAL expanded path
/// is canonicalized first (the OS resolves `..` AFTER symlinks, so
/// `/work/link/../f` with `link -> /repos/B/sub` is `/repos/B/f` — a prior
/// lexical `..` clean would wrongly yield `/work/f`). A nonexistent trailing
/// suffix keeps the resolved existing prefix's semantics (remainder appended);
/// fully unresolvable paths fail closed (`None`). Returns the canonical
/// absolute string (no trailing slash except root) or `None` for
/// empty/overlong/NUL/control/relative/URI-like values.
///
/// NOTE: `stat`/`canonicalize` are blocking with no timeout (see module docs
/// for the network-mount limitation); callers limit exposure via lazy
/// per-level matching.
pub fn canonicalize_path_str(raw: &str) -> Option<String> {
    if raw.is_empty() || raw.len() > MAX_PATH_CHARS || raw.contains('\0') {
        return None;
    }
    if raw.chars().any(|c| c.is_control()) {
        return None;
    }
    if raw.contains("://") {
        return None;
    }
    let expanded = expand_tilde_lexical(raw.trim());
    if expanded.is_empty() || expanded.len() > MAX_PATH_CHARS {
        return None;
    }
    if !(expanded == "~" || expanded.starts_with("~/") || expanded.starts_with('/')) {
        // expand_tilde_lexical leaves "~user/..." untouched: ambiguous, reject.
        // Plain relative paths are never mapping candidates.
        return None;
    }
    // A lone "~" with missing HOME stays "~": reject (no guessing).
    if expanded == "~" {
        return None;
    }
    if !expanded.starts_with('/') {
        return None;
    }
    canonicalize_raw(&expanded)
}

/// Canonicalize WITHOUT pre-cleaning `..`: try the OS on the original path
/// first (correct symlink + `..` interaction), else resolve the nearest
/// existing ancestor of the original and re-append the remainder onto the
/// resolved prefix. `None` when nothing usable resolves.
fn canonicalize_raw(expanded: &str) -> Option<String> {
    let candidate = Path::new(expanded);
    // Fast path: the full path exists (symlinks + `..` resolved by the OS).
    if std::fs::symlink_metadata(candidate).is_ok() {
        if let Some(s) = checked_canonical_str(candidate) {
            return Some(s);
        }
        return None;
    }
    // Ancestor walk on the ORIGINAL component sequence (bounded): pop
    // trailing components until an existing prefix is found, canonicalize it
    // with the OS, then re-append the remainder. Because the prefix is fully
    // resolved, the remainder's `..` applies to real directories and a final
    // lexical clean is sound.
    let mut ancestor = candidate;
    let mut remainder: Vec<String> = Vec::new();
    for _ in 0..128 {
        let Some(parent) = ancestor.parent() else {
            break;
        };
        let Some(name) = ancestor.file_name() else {
            break;
        };
        let Some(name_str) = name.to_str() else {
            return None;
        };
        if name_str.len() > MAX_PATH_CHARS {
            return None;
        }
        remainder.push(name_str.to_string());
        ancestor = parent;
        if std::fs::symlink_metadata(ancestor).is_ok() {
            let Some(base) = checked_canonical_str(ancestor) else {
                return None;
            };
            remainder.reverse();
            let mut s = base;
            for comp in &remainder {
                if comp.is_empty() || comp == "." {
                    continue;
                }
                s.push('/');
                s.push_str(comp);
            }
            let cleaned = lexical_clean(&s);
            if cleaned.len() > MAX_PATH_CHARS || !cleaned.starts_with('/') {
                return None;
            }
            if cleaned.contains('\0') || cleaned.chars().any(|c| c.is_control()) {
                return None;
            }
            return Some(trim_trailing_slash(&cleaned).to_string());
        }
    }
    None
}

/// `std::fs::canonicalize` plus output validation (absolute, bounded,
/// no NUL/controls). Symlink loops / races yield `None` (fail closed).
fn checked_canonical_str(path: &Path) -> Option<String> {
    let real = std::fs::canonicalize(path).ok()?;
    let s = real.to_string_lossy().to_string();
    if s.is_empty()
        || s.len() > MAX_PATH_CHARS
        || !s.starts_with('/')
        || s.contains('\0')
        || s.chars().any(|c| c.is_control())
    {
        return None;
    }
    Some(trim_trailing_slash(&s).to_string())
}

fn trim_trailing_slash(s: &str) -> &str {
    if s.len() > 1 {
        let t = s.trim_end_matches('/');
        if t.is_empty() {
            "/"
        } else {
            t
        }
    } else {
        s
    }
}

fn lexical_clean(p: &str) -> String {
    let absolute = p.starts_with('/');
    let mut parts: Vec<&str> = Vec::new();
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

/// Component-boundary descendant check: `candidate == folder` or
/// `candidate` starts with `folder + "/"` (root folder matches all).
pub fn is_descendant_of(candidate: &str, folder: &str) -> bool {
    if candidate == folder {
        return true;
    }
    if folder == "/" {
        return candidate.starts_with('/');
    }
    candidate.len() > folder.len()
        && candidate.starts_with(folder)
        && candidate.as_bytes().get(folder.len()) == Some(&b'/')
}

// ---------------------------------------------------------------------------
// Registry projection
// ---------------------------------------------------------------------------

/// One validated registry row projected for matching.
///
/// `folder_raw` is the validated registry string, stored RAW: filesystem
/// projection (symlink resolution) happens lazily per precedence level with
/// a short TTL (see `ProjectResolver::project_folder`), so a symlink retarget
/// while the registry is unchanged converges without spawning Python.
#[derive(Clone, Debug)]
pub struct ProjectEntry {
    pub id: String,
    pub name: String,
    pub folder_raw: Option<String>,
    pub remote: Option<String>,
}

#[derive(Clone, Debug)]
struct RegistryMeta {
    path: String,
    dev: u64,
    ino: u64,
    mtime_sec: i64,
    mtime_nsec: i64,
    ctime_sec: i64,
    ctime_nsec: i64,
    len: u64,
    missing: bool,
}

#[derive(Clone, Debug)]
struct RemoteCacheEntry {
    remotes: Vec<String>,
    cached_at_ms: i64,
    fingerprint: GitFingerprint,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct GitFingerprint {
    dotgit_kind: u8,
    dotgit_mtime_sec: i64,
    dotgit_mtime_nsec: i64,
    dotgit_len: u64,
    config_mtime_sec: i64,
    config_mtime_nsec: i64,
    config_len: u64,
    config_present: bool,
    common_mtime_sec: i64,
    common_mtime_nsec: i64,
    common_len: u64,
    common_present: bool,
}

#[derive(Clone, Debug)]
struct FolderCacheEntry {
    canonical: Option<String>,
    cached_at_ms: i64,
}

/// Cached deterministic resolver. Own one per enrichment worker thread;
/// the process default lives in a thread-local (see free functions below).
pub struct ProjectResolver {
    registry: PathBuf,
    helper: PathBuf,
    meta: Option<RegistryMeta>,
    entries: Vec<ProjectEntry>,
    /// Earliest `now_ms` at which a failed load may retry (brief TTL).
    retry_after_ms: i64,
    remote_cache: HashMap<String, RemoteCacheEntry>,
    folder_cache: HashMap<String, FolderCacheEntry>,
}

impl ProjectResolver {
    pub fn new() -> Self {
        Self::with_paths(resolve_registry_file(None), helper_script_path())
    }

    pub fn with_paths(registry: PathBuf, helper: PathBuf) -> Self {
        Self {
            registry,
            helper,
            meta: None,
            entries: Vec::new(),
            retry_after_ms: 0,
            remote_cache: HashMap::new(),
            folder_cache: HashMap::new(),
        }
    }

    /// Override the registry path (tests). Drops cached mapping.
    pub fn set_registry(&mut self, registry: PathBuf) {
        if registry != self.registry {
            self.registry = registry;
            self.meta = None;
            self.entries.clear();
            self.retry_after_ms = 0;
        }
    }

    /// Drop cached mapping + remote/folder discovery (tests).
    pub fn clear_cache(&mut self) {
        self.meta = None;
        self.entries.clear();
        self.retry_after_ms = 0;
        self.remote_cache.clear();
        self.folder_cache.clear();
    }

    fn current_meta(&self) -> RegistryMeta {
        let path = self.registry.to_string_lossy().to_string();
        match std::fs::symlink_metadata(&self.registry) {
            Err(_) => RegistryMeta {
                path,
                dev: 0,
                ino: 0,
                mtime_sec: 0,
                mtime_nsec: 0,
                ctime_sec: 0,
                ctime_nsec: 0,
                len: 0,
                missing: true,
            },
            Ok(md) => {
                #[cfg(unix)]
                {
                    use std::os::unix::fs::MetadataExt;
                    return RegistryMeta {
                        path,
                        dev: md.dev(),
                        ino: md.ino(),
                        mtime_sec: md.mtime(),
                        mtime_nsec: md.mtime_nsec(),
                        ctime_sec: md.ctime(),
                        ctime_nsec: md.ctime_nsec(),
                        len: md.len(),
                        missing: false,
                    };
                }
                #[cfg(not(unix))]
                {
                    let mtime = md
                        .modified()
                        .ok()
                        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                        .map(|d| (d.as_secs() as i64, d.subsec_nanos() as i64))
                        .unwrap_or((0, 0));
                    return RegistryMeta {
                        path,
                        dev: 0,
                        ino: 0,
                        mtime_sec: mtime.0,
                        mtime_nsec: mtime.1,
                        ctime_sec: 0,
                        ctime_nsec: 0,
                        len: md.len(),
                        missing: false,
                    };
                }
            }
        }
    }

    fn meta_same(a: &RegistryMeta, b: &RegistryMeta) -> bool {
        a.path == b.path
            && a.missing == b.missing
            && (a.missing
                || (a.dev == b.dev
                    && a.ino == b.ino
                    && a.mtime_sec == b.mtime_sec
                    && a.mtime_nsec == b.mtime_nsec
                    && a.ctime_sec == b.ctime_sec
                    && a.ctime_nsec == b.ctime_nsec
                    && a.len == b.len))
    }

    /// Ensure the mapping is fresh for `now_ms`. Spawns Python ONLY on
    /// metadata change (or past the brief failure retry TTL).
    fn ensure_loaded(&mut self, now_ms: i64) {
        // Honor a changed QUICKSHELL_PROJECTS_FILE without a restart.
        let live = resolve_registry_file(None);
        if live != self.registry && self.meta.is_some() {
            // Only auto-follow the env default when this resolver was built
            // from it (explicit test paths stay pinned). Heuristic: follow
            // when the current path equals the last resolved live path or
            // the compiled default; otherwise a test-set path is pinned.
            // Simplest safe rule: follow only when the resolver path is the
            // compiled default or already equals live. Pinned test paths
            // (tmp files) never equal the default, so they stay pinned.
            if self.registry == default_registry_file() || self.registry == live {
                self.set_registry(live);
            }
        }
        let cur = self.current_meta();
        if let Some(prev) = self.meta.as_ref() {
            if Self::meta_same(prev, &cur) {
                if self.entries.is_empty() && self.retry_after_ms > 0 && now_ms < self.retry_after_ms {
                    return; // brief failure TTL: no respawn
                }
                if !self.entries.is_empty() || self.retry_after_ms == 0 {
                    return; // unchanged mapping: no subprocess
                }
                if now_ms < self.retry_after_ms {
                    return;
                }
            }
        }
        // Missing file: empty mapping, no subprocess (Python would agree).
        if cur.missing {
            self.meta = Some(cur);
            self.entries.clear();
            self.retry_after_ms = 0;
            return;
        }
        match self.load_via_helper() {
            Ok(entries) => {
                self.meta = Some(cur);
                self.entries = entries;
                self.retry_after_ms = 0;
            }
            Err(_) => {
                // Fail closed: never serve stale; brief retry TTL only.
                self.meta = Some(cur);
                self.entries.clear();
                self.retry_after_ms = now_ms.saturating_add(REGISTRY_RETRY_TTL_MS);
            }
        }
    }

    fn load_via_helper(&self) -> Result<Vec<ProjectEntry>, String> {
        let out = run_helper_list(&self.helper, &self.registry).ok_or("helper failed")?;
        parse_registry_output(&out)
    }

    /// Deterministic resolve for one resource. `None` = unknown/ambiguous/
    /// unavailable (fail closed, never a guess).
    ///
    /// Path levels resolve LAZILY in precedence order: the `FILE` candidate
    /// is canonicalized and matched first, and weaker levels touch the
    /// filesystem only when no stronger level hit or tied. The git remote
    /// subprocess runs only when every path level missed.
    pub fn resolve_resource(
        &mut self,
        resource: &ResourceContext,
        now_ms: i64,
    ) -> Option<ProjectContext> {
        self.ensure_loaded(now_ms);
        if self.entries.is_empty() {
            return None;
        }
        // Path precedence: FILE > CWD > GIT_ROOT. Each level matches on its
        // own longest folder; a tie between different projects at the same
        // level is ambiguous (no fallback to weaker levels).
        let levels: [(&Option<String>, &str); 3] = [
            (&resource.file, "file"),
            (&resource.cwd, "cwd"),
            (&resource.git_root, "git_root"),
        ];
        let mut root_canonical: Option<String> = None;
        for (raw, matched_by) in levels {
            let Some(raw) = raw.as_deref() else {
                continue;
            };
            let Some(cand) = canonicalize_path_str(raw) else {
                continue;
            };
            if matched_by == "git_root" {
                root_canonical = Some(cand.clone());
            }
            match self.match_path_level(&cand, now_ms) {
                PathMatchOwned::None => continue,
                PathMatchOwned::Ambiguous => return None,
                PathMatchOwned::Hit(id, name) => {
                    return Some(ProjectContext::new(&id, &name, matched_by));
                }
            }
        }
        // Remote fallback (only when no path matched at any level).
        let remotes = self.remote_candidates(resource, root_canonical.as_deref(), now_ms);
        if remotes.is_empty() {
            return None;
        }
        match match_remote_claims(&self.entries, &remotes) {
            RemoteMatch::Hit(id, name) => Some(ProjectContext::new(&id, &name, "git_remote")),
            _ => None,
        }
    }

    /// Filesystem projection of one raw mapped folder, cached with a short
    /// TTL so symlink retargets converge without spawning Python. `None`
    /// means the mapping carries no path signal right now (invalid raw or
    /// currently unresolvable); the registry row itself stays valid.
    fn project_folder(&mut self, raw: &str, now_ms: i64) -> Option<String> {
        if let Some(hit) = self.folder_cache.get(raw) {
            if hit.cached_at_ms > 0 && now_ms >= hit.cached_at_ms && now_ms - hit.cached_at_ms <= FOLDER_TTL_MS
            {
                return hit.canonical.clone();
            }
        }
        let canonical = canonicalize_path_str(raw);
        if self.folder_cache.len() >= FOLDER_CACHE_MAX && !self.folder_cache.contains_key(raw) {
            if let Some(oldest) = self
                .folder_cache
                .iter()
                .min_by_key(|(_, e)| e.cached_at_ms)
                .map(|(k, _)| k.clone())
            {
                self.folder_cache.remove(&oldest);
            }
        }
        self.folder_cache.insert(
            raw.to_string(),
            FolderCacheEntry {
                canonical: canonical.clone(),
                cached_at_ms: now_ms,
            },
        );
        canonical
    }

    /// Longest mapped-folder match at one precedence level. Folder
    /// projections resolve lazily through the short-TTL cache. Ties between
    /// different projects (same longest length, different ids) are ambiguous.
    fn match_path_level(&mut self, candidate: &str, now_ms: i64) -> PathMatchOwned {
        let mut best_len: Option<usize> = None;
        let mut best: Vec<(String, String, usize)> = Vec::new();
        // Clone the raws first so the folder-cache borrow does not overlap
        // the entries borrow.
        let raws: Vec<(String, String, Option<String>)> = self
            .entries
            .iter()
            .map(|e| (e.id.clone(), e.name.clone(), e.folder_raw.clone()))
            .collect();
        for (id, name, raw) in raws {
            let Some(raw) = raw.as_deref() else {
                continue;
            };
            let Some(folder) = self.project_folder(raw, now_ms) else {
                continue;
            };
            if !is_descendant_of(candidate, &folder) {
                continue;
            }
            let len = folder.len();
            match best_len {
                None => {
                    best_len = Some(len);
                    best = vec![(id, name, len)];
                }
                Some(b) if len > b => {
                    best_len = Some(len);
                    best = vec![(id, name, len)];
                }
                Some(b) if len == b => {
                    best.push((id, name, len));
                }
                _ => {}
            }
        }
        match best_len {
            None => PathMatchOwned::None,
            Some(_) => {
                let first = best[0].0.as_str();
                if best.iter().all(|(id, _, _)| id == first) {
                    let (id, name, _) = best.into_iter().next().expect("best");
                    PathMatchOwned::Hit(id, name)
                } else {
                    PathMatchOwned::Ambiguous
                }
            }
        }
    }

    /// Explicit remote candidates: a normalized `resource.git_remote` wins;
    /// otherwise cached `git config` discovery from the canonical git root.
    /// The root is canonicalized by the caller during the `GIT_ROOT` level so
    /// discovery never re-stats purely for probing.
    fn remote_candidates(
        &mut self,
        resource: &ResourceContext,
        root_canonical: Option<&str>,
        now_ms: i64,
    ) -> Vec<String> {
        if let Some(raw) = resource.git_remote.as_deref() {
            if let Some(n) = normalize_github_remote(raw) {
                return vec![n];
            }
            return Vec::new();
        }
        let Some(root) = root_canonical else {
            // The GIT_ROOT level already attempted canonicalization; if it
            // failed there is no trustworthy anchor for discovery.
            // (Deliberately no second canonicalization pass here.)
            return Vec::new();
        };
        self.discover_remotes(root, now_ms)
    }

    /// Cached `git config` remote discovery (local only, bounded subprocess).
    /// All outcomes — including empty/negative — are cached with a 30 s TTL
    /// plus best-effort git-config/commondir metadata invalidation.
    fn discover_remotes(&mut self, git_root: &str, now_ms: i64) -> Vec<String> {
        let key = git_root.to_string();
        let fingerprint = git_config_fingerprint(git_root);
        if let Some(hit) = self.remote_cache.get(&key) {
            let fresh = hit.cached_at_ms > 0
                && now_ms >= hit.cached_at_ms
                && now_ms - hit.cached_at_ms <= REMOTE_TTL_MS;
            if fresh && hit.fingerprint == fingerprint {
                return hit.remotes.clone();
            }
        }
        let remotes = git_remote_urls(git_root).unwrap_or_default();
        if self.remote_cache.len() >= REMOTE_CACHE_MAX && !self.remote_cache.contains_key(&key) {
            if let Some(oldest) = self
                .remote_cache
                .iter()
                .min_by_key(|(_, e)| e.cached_at_ms)
                .map(|(k, _)| k.clone())
            {
                self.remote_cache.remove(&oldest);
            }
        }
        self.remote_cache.insert(
            key,
            RemoteCacheEntry {
                remotes: remotes.clone(),
                cached_at_ms: now_ms,
                fingerprint,
            },
        );
        remotes
    }

    /// Attach (or clear) the project overlay for a snapshot. Unavailable,
    /// focusless or resourceless snapshots never carry a project.
    pub fn enrich(&mut self, base: DesktopContext, now_ms: i64) -> DesktopContext {
        if !base.available || base.focused_window.is_none() {
            return base.with_project(None);
        }
        let Some(resource) = base.resource.as_ref() else {
            return base.with_project(None);
        };
        if resource.is_empty() {
            return base.with_project(None);
        }
        match self.resolve_resource(resource, now_ms) {
            Some(p) => base.with_project(Some(p)),
            None => base.with_project(None),
        }
    }
}

impl Default for ProjectResolver {
    fn default() -> Self {
        Self::new()
    }
}

/// Owned path-match outcome (ids cloned so the folder-cache borrow can end
/// before the caller uses the result).
enum PathMatchOwned {
    None,
    Ambiguous,
    Hit(String, String),
}

/// Remote-claim outcome: exactly one DISTINCT registered project claimed by
/// any of the repo's normalized remotes wins — even when the repo has other
/// unregistered remotes (fork with an unmapped upstream). Zero claims, or
/// claims spanning more than one distinct project, mean no association.
enum RemoteMatch {
    None,
    Ambiguous,
    Hit(String, String),
}

/// Compute the distinct registered projects claimed by the repo remotes.
/// Unregistered repo remotes are ignored (they do not force ambiguity); two
/// repo remotes claiming the SAME project still yield that project.
fn match_remote_claims(entries: &[ProjectEntry], remotes: &[String]) -> RemoteMatch {
    if remotes.is_empty() {
        return RemoteMatch::None;
    }
    // Distinct normalized repo remotes (case-insensitive dedup).
    let mut distinct: Vec<String> = Vec::new();
    for r in remotes {
        let low = r.to_lowercase();
        if !distinct.contains(&low) {
            distinct.push(low);
        }
    }
    // Distinct registered projects claimed by any repo remote.
    let mut claimed: Vec<(String, String)> = Vec::new();
    for want in &distinct {
        for e in entries {
            let Some(reg) = e.remote.as_deref() else {
                continue;
            };
            if reg.to_lowercase() == *want && !claimed.iter().any(|(id, _)| id == &e.id) {
                claimed.push((e.id.clone(), e.name.clone()));
            }
        }
    }
    match claimed.len() {
        0 => RemoteMatch::None,
        1 => {
            let (id, name) = claimed.into_iter().next().expect("one claim");
            RemoteMatch::Hit(id, name)
        }
        _ => RemoteMatch::Ambiguous,
    }
}

// ---------------------------------------------------------------------------
// Registry subprocess (bounded)
// ---------------------------------------------------------------------------

fn run_helper_list(helper: &Path, registry: &Path) -> Option<Vec<u8>> {
    if helper.to_string_lossy().len() > 4096 || registry.to_string_lossy().len() > 4096 {
        return None;
    }
    let mut child = std::process::Command::new("python3")
        .arg(helper)
        .arg("--projects-file")
        .arg(registry)
        .arg("list")
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .spawn()
        .ok()?;
    use std::io::Read;
    let mut stdout = child.stdout.take()?;
    let start = Instant::now();
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut buf = Vec::new();
        let mut chunk = [0u8; 4096];
        loop {
            match stdout.read(&mut chunk) {
                Ok(0) => break,
                Ok(n) => {
                    buf.extend_from_slice(&chunk[..n]);
                    if buf.len() > MAX_REGISTRY_OUTPUT_BYTES + 1 {
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
                if buf.len() > MAX_REGISTRY_OUTPUT_BYTES {
                    return None;
                }
                return Some(buf);
            }
            Ok(None) => {
                if start.elapsed() >= REGISTRY_TIMEOUT {
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

/// Parse + project the validated `list` JSON. Unknown fields ignored;
/// top-level or per-row shape violations fail closed (`Err`).
fn parse_registry_output(raw: &[u8]) -> Result<Vec<ProjectEntry>, String> {
    if raw.len() > MAX_REGISTRY_OUTPUT_BYTES {
        return Err("output too large".to_string());
    }
    let v: serde_json::Value =
        serde_json::from_slice(raw).map_err(|_| "invalid json".to_string())?;
    let arr = v
        .get("projects")
        .and_then(|p| p.as_array())
        .ok_or("missing projects".to_string())?;
    if arr.len() > MAX_REGISTRY_ENTRIES {
        return Err("too many projects".to_string());
    }
    let mut out = Vec::with_capacity(arr.len().min(256));
    for item in arr {
        let obj = item.as_object().ok_or("project must be object".to_string())?;
        let id = obj
            .get("id")
            .and_then(|x| x.as_str())
            .ok_or("project id missing".to_string())?;
        let name = obj
            .get("name")
            .and_then(|x| x.as_str())
            .ok_or("project name missing".to_string())?;
        if id.trim().is_empty() || id.len() > 128 {
            return Err("bad project id".to_string());
        }
        if name.trim().is_empty() || name.len() > 1024 {
            return Err("bad project name".to_string());
        }
        let folder_raw = obj
            .get("local_folder")
            .and_then(|x| x.as_str())
            .unwrap_or("");
        let url_raw = obj
            .get("github_url")
            .and_then(|x| x.as_str())
            .unwrap_or("");
        if folder_raw.len() > 4096 || url_raw.len() > 2048 {
            return Err("field too long".to_string());
        }
        // Store the raw folder string: filesystem projection happens lazily
        // per precedence level (short TTL), so symlink retargets converge
        // without a registry reload. Light validation only (NUL/controls
        // carry no signal but the row itself stays valid).
        let folder_raw = if folder_raw.trim().is_empty() {
            None
        } else {
            let t = folder_raw.trim();
            if t.contains('\0') || t.chars().any(|c| c.is_control()) {
                None
            } else {
                Some(t.to_string())
            }
        };
        let remote = if url_raw.trim().is_empty() {
            None
        } else {
            normalize_github_remote(url_raw.trim())
        };
        out.push(ProjectEntry {
            id: id.trim().to_string(),
            name: name.trim().to_string(),
            folder_raw,
            remote,
        });
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Git remote discovery: bounded local `git config` (no network, no shell)
// ---------------------------------------------------------------------------

/// List normalized GitHub remote URLs for a canonical git root via git's own
/// config machinery (handles comments, quoting, includes, and linked
/// worktrees through `commondir`). Repository-local only:
/// `git -C <root> config --local --includes --null --get-regexp
/// ^remote\..*\.url$` with a bounded timeout and bounded output; argv only,
/// stdin null, `LC_ALL=C`. `--local` keeps global/system config out and
/// neutralizes environment-injected config values; the ambient environment
/// is additionally sanitized (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_CONFIG_*`
/// removed) so it can never hijack which repo `-C <root>` inspects. Raw URLs
/// are never logged or persisted — only normalized
/// `https://github.com/owner/repo` values are returned (deduped
/// case-insensitively, git's output order preserved). Overflow past the
/// remote cap fails the whole discovery closed (`None`).
fn git_remote_urls(git_root: &str) -> Option<Vec<String>> {
    if git_root.is_empty() || git_root.len() > MAX_PATH_CHARS || git_root.contains('\0') {
        return None;
    }
    let out = run_git_config(git_root)?;
    parse_git_null_config(&out)
}

/// Parse `git config --null --get-regexp` output: NUL-separated
/// `key\nvalue` records. Keys are sanity-checked to `remote.*.url`; values
/// are normalized (invalid skipped) and deduped case-insensitively.
/// More than `MAX_GIT_REMOTES` normalized remotes fails the whole parse
/// closed (`None`): silently keeping the first 64 could falsely report a
/// unique claim while the 65th claims another project.
fn parse_git_null_config(raw: &[u8]) -> Option<Vec<String>> {
    let mut out: Vec<String> = Vec::new();
    let mut seen: Vec<String> = Vec::new();
    for record in raw.split(|b| *b == 0) {
        if record.is_empty() {
            continue;
        }
        let value = match record.iter().position(|b| *b == b'\n') {
            Some(idx) => &record[idx + 1..],
            None => continue,
        };
        let key = &record[..record.len() - value.len() - 1];
        if !is_remote_url_key(key) {
            continue;
        }
        let Ok(text) = std::str::from_utf8(value) else {
            continue;
        };
        let text = text.trim();
        if text.is_empty() || text.len() > 2048 {
            continue;
        }
        if let Some(n) = normalize_github_remote(text) {
            let low = n.to_lowercase();
            if !seen.contains(&low) {
                if out.len() >= MAX_GIT_REMOTES {
                    return None;
                }
                seen.push(low);
                out.push(n);
            }
        }
    }
    Some(out)
}

fn is_remote_url_key(key: &[u8]) -> bool {
    // `remote.<name>.url`, case-insensitive section/field, non-empty name.
    const PREFIX: &[u8] = b"remote.";
    const SUFFIX: &[u8] = b".url";
    if key.len() <= PREFIX.len() + SUFFIX.len() {
        return false;
    }
    if !key[..PREFIX.len()].eq_ignore_ascii_case(PREFIX) {
        return false;
    }
    if !key[key.len() - SUFFIX.len()..].eq_ignore_ascii_case(SUFFIX) {
        return false;
    }
    let name = &key[PREFIX.len()..key.len() - SUFFIX.len()];
    !name.is_empty() && name.len() <= 256 && !name.contains(&b'\0') && !name.contains(&b'\n')
}

/// Exact argv (after `git`) for repository-local remote discovery.
/// `--local` confines reads to the repository config (linked worktrees
/// resolve through their common dir; global/system config never applies),
/// `--includes` honors repository `include.*` directives with git's own
/// semantics, and `--null` gives unambiguous record framing.
pub(crate) fn git_config_argv(git_root: &str) -> Vec<String> {
    vec![
        "-C".to_string(),
        git_root.to_string(),
        "config".to_string(),
        "--local".to_string(),
        "--includes".to_string(),
        "--null".to_string(),
        "--get-regexp".to_string(),
        r"^remote\..*\.url$".to_string(),
    ]
}

/// Environment variables stripped from the git child environment.
/// Ambient `GIT_DIR`/`GIT_WORK_TREE` would hijack WHICH repository `-C`
/// inspects (verified: `GIT_DIR=<evil>/.git git -C <real> config` reports the
/// evil repo even with `--local`); the `GIT_CONFIG_*` family would inject or
/// redirect config scope. All are removed so discovery inspects exactly
/// `<root>` and nothing else. (`GIT_CONFIG_KEY_*`/`GIT_CONFIG_VALUE_*`
/// suffixed pairs are enumerated dynamically in
/// [`apply_git_env_sanitize`].)
pub(crate) fn git_env_blocklist() -> Vec<&'static str> {
    vec![
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_CONFIG",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
    ]
}

/// Strip ambient git environment hijacks from a child command (see
/// [`git_env_blocklist`]). Read-only on our own environment (enumerates the
/// `GIT_CONFIG_KEY_*`/`GIT_CONFIG_VALUE_*` pair pattern); never mutates it.
fn apply_git_env_sanitize(cmd: &mut std::process::Command) {
    for key in git_env_blocklist() {
        cmd.env_remove(key);
    }
    for (key, _) in std::env::vars_os() {
        let Some(key) = key.to_str() else {
            continue;
        };
        if key.starts_with("GIT_CONFIG_KEY_") || key.starts_with("GIT_CONFIG_VALUE_") {
            cmd.env_remove(key);
        }
    }
}

/// Bounded `git -C <root> config --local --includes --null --get-regexp
/// ^remote\..*\.url$` (see [`git_config_argv`]/[`git_env_blocklist`]).
/// Returns the raw stdout on exit 0 (matches) or exit 1 (no matches:
/// negative result, still cacheable). Anything else (spawn failure, timeout,
/// oversize, non-repo exit 128) is `None` — fail closed, cached as empty by
/// the caller.
fn run_git_config(git_root: &str) -> Option<Vec<u8>> {
    if git_root.contains('\0') || git_root.len() > MAX_PATH_CHARS {
        return None;
    }
    let argv = git_config_argv(git_root);
    let mut cmd = std::process::Command::new("git");
    cmd.args(&argv);
    cmd.env("LC_ALL", "C").env("LANG", "C");
    apply_git_env_sanitize(&mut cmd);
    cmd.stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null());
    let mut child = cmd.spawn().ok()?;
    use std::io::Read;
    let mut stdout = child.stdout.take()?;
    let start = Instant::now();
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut buf = Vec::new();
        let mut chunk = [0u8; 4096];
        loop {
            match stdout.read(&mut chunk) {
                Ok(0) => break,
                Ok(n) => {
                    buf.extend_from_slice(&chunk[..n]);
                    if buf.len() > MAX_GIT_REMOTE_OUTPUT_BYTES + 1 {
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
                if buf.len() > MAX_GIT_REMOTE_OUTPUT_BYTES {
                    return None;
                }
                match status.code() {
                    Some(0) | Some(1) => return Some(buf),
                    _ => return None,
                }
            }
            Ok(None) => {
                if start.elapsed() >= GIT_REMOTE_TIMEOUT {
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

/// Best-effort fingerprint of the git config inputs git would read for
/// `<root>`: the `.git` entry itself, `<root>/.git/config` for plain repos,
/// plus the worktree `gitdir:` pointer target config and its `commondir`
/// config for linked worktrees. Pointer-file reads are capped, symlink-safe
/// (`O_NOFOLLOW`), and `O_NONBLOCK` (FIFO-safe: fifos are rejected after
/// `fstat` without ever blocking). Missing/unreadable inputs fingerprint as
/// absent; any change forces a rediscovery even within the TTL. This is
/// best-effort (not a full reimplementation of git's config lookup); the TTL
/// bounds staleness regardless.
fn git_config_fingerprint(git_root: &str) -> GitFingerprint {
    let mut fp = GitFingerprint::default();
    let root = Path::new(git_root);
    let dotgit = root.join(".git");
    let Ok(md) = std::fs::symlink_metadata(&dotgit) else {
        return fp;
    };
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        fp.dotgit_mtime_sec = md.mtime();
        fp.dotgit_mtime_nsec = md.mtime_nsec();
        fp.dotgit_len = md.len();
    }
    #[cfg(not(unix))]
    {
        fp.dotgit_len = md.len();
    }
    let ft = md.file_type();
    if ft.is_symlink() {
        fp.dotgit_kind = 3;
        return fp;
    }
    if ft.is_dir() {
        fp.dotgit_kind = 2;
        let cfg = dotgit.join("config");
        let (present, sec, nsec, len) = file_meta(&cfg);
        fp.config_present = present;
        fp.config_mtime_sec = sec;
        fp.config_mtime_nsec = nsec;
        fp.config_len = len;
        return fp;
    }
    if ft.is_file() {
        fp.dotgit_kind = 1;
        let Some(content) = read_small_file_capped(&dotgit, POINTER_READ_CAP) else {
            return fp;
        };
        let first = content.lines().next().unwrap_or("").trim();
        let gitdir_raw = first.strip_prefix("gitdir:").map(|s| s.trim()).unwrap_or("");
        if gitdir_raw.is_empty() || gitdir_raw.len() > MAX_PATH_CHARS || gitdir_raw.contains('\0') {
            return fp;
        }
        let gitdir = if Path::new(gitdir_raw).is_absolute() {
            PathBuf::from(gitdir_raw)
        } else {
            root.join(gitdir_raw)
        };
        let (present, sec, nsec, len) = file_meta(&gitdir.join("config"));
        fp.config_present = present;
        fp.config_mtime_sec = sec;
        fp.config_mtime_nsec = nsec;
        fp.config_len = len;
        // Common dir for linked worktrees (`commondir` file inside gitdir).
        if let Some(common_rel) = read_small_file_capped(&gitdir.join("commondir"), POINTER_READ_CAP) {
            let common_rel = common_rel.lines().next().unwrap_or("").trim();
            if !common_rel.is_empty()
                && common_rel.len() <= MAX_PATH_CHARS
                && !common_rel.contains('\0')
            {
                let common = if Path::new(common_rel).is_absolute() {
                    PathBuf::from(common_rel)
                } else {
                    gitdir.join(common_rel)
                };
                let (p, s, ns, l) = file_meta(&common.join("config"));
                fp.common_present = p;
                fp.common_mtime_sec = s;
                fp.common_mtime_nsec = ns;
                fp.common_len = l;
            }
        }
        return fp;
    }
    fp.dotgit_kind = 3;
    fp
}

fn file_meta(path: &Path) -> (bool, i64, i64, u64) {
    let Ok(md) = std::fs::symlink_metadata(path) else {
        return (false, 0, 0, 0);
    };
    if md.file_type().is_symlink() || !md.is_file() {
        return (false, 0, 0, 0);
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        return (true, md.mtime(), md.mtime_nsec(), md.len());
    }
    #[cfg(not(unix))]
    {
        let mtime = md
            .modified()
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| (d.as_secs() as i64, d.subsec_nanos() as i64))
            .unwrap_or((0, 0));
        return (true, mtime.0, mtime.1, md.len());
    }
}

/// Capped, symlink-safe, FIFO-safe small file read. Rejects symlinks and
/// non-regular files (post-open `fstat` verified, so a FIFO swap can never
/// block: the open is `O_NONBLOCK` and reads happen only on confirmed
/// regular files with a hard byte cap). Returns the UTF-8-lossy-trimmed
/// content as `String`.
#[cfg(unix)]
fn read_small_file_capped(path: &Path, cap: u64) -> Option<String> {
    use std::os::unix::fs::OpenOptionsExt;
    let md = std::fs::symlink_metadata(path).ok()?;
    if md.file_type().is_symlink() || !md.is_file() {
        return None;
    }
    if md.len() > cap {
        return None;
    }
    let f = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(path)
        .ok()?;
    let st = f.metadata().ok()?;
    if !st.is_file() {
        return None;
    }
    {
        use std::os::unix::fs::MetadataExt;
        if st.size() > cap {
            return None;
        }
    }
    use std::io::Read;
    let mut buf = Vec::new();
    let mut take = f.take(cap + 1);
    if take.read_to_end(&mut buf).is_err() {
        return None;
    }
    if buf.len() as u64 > cap {
        return None;
    }
    String::from_utf8(buf).ok()
}

#[cfg(not(unix))]
fn read_small_file_capped(path: &Path, cap: u64) -> Option<String> {
    let md = std::fs::symlink_metadata(path).ok()?;
    if md.file_type().is_symlink() || !md.is_file() {
        return None;
    }
    if md.len() > cap {
        return None;
    }
    let raw = std::fs::read(path).ok()?;
    if raw.len() as u64 > cap {
        return None;
    }
    String::from_utf8(raw).ok()
}

// ---------------------------------------------------------------------------
// Thread-local default (enrichment worker thread)
// ---------------------------------------------------------------------------

thread_local! {
    static DEFAULT_RESOLVER: RefCell<ProjectResolver> = RefCell::new(ProjectResolver::new());
}

/// Clear the thread-local project + remote caches (tests).
pub fn clear_project_cache() {
    DEFAULT_RESOLVER.with(|c| c.borrow_mut().clear_cache());
}

/// Resolve one resource via the thread-local cache.
pub fn resolve_project_for_resource(
    resource: &ResourceContext,
    now_ms: i64,
) -> Option<ProjectContext> {
    DEFAULT_RESOLVER.with(|c| c.borrow_mut().resolve_resource(resource, now_ms))
}

/// Attach (or clear) the project overlay via the thread-local cache.
/// Unavailable / focusless / resourceless inputs clear the project.
pub fn enrich_desktop_with_project(base: DesktopContext, now_ms: i64) -> DesktopContext {
    DEFAULT_RESOLVER.with(|c| c.borrow_mut().enrich(base, now_ms))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn remote_forms_normalize() {
        assert_eq!(
            normalize_github_remote("https://github.com/Owner/Repo").as_deref(),
            Some("https://github.com/Owner/Repo")
        );
        assert_eq!(
            normalize_github_remote("https://github.com/Owner/Repo.git").as_deref(),
            Some("https://github.com/Owner/Repo")
        );
        assert_eq!(
            normalize_github_remote("https://www.github.com/o/r/").as_deref(),
            Some("https://github.com/o/r")
        );
        assert_eq!(
            normalize_github_remote("git@github.com:o/r.git").as_deref(),
            Some("https://github.com/o/r")
        );
        assert_eq!(
            normalize_github_remote("ssh://git@github.com/o/r.git").as_deref(),
            Some("https://github.com/o/r")
        );
        assert_eq!(
            normalize_github_remote("https://token@github.com/o/r").as_deref(),
            Some("https://github.com/o/r")
        );
        assert!(normalize_github_remote("https://gitlab.com/o/r").is_none());
        assert!(normalize_github_remote("https://github.com/o").is_none());
        assert!(normalize_github_remote("https://github.com/o/r/extra").is_none());
        assert!(normalize_github_remote("git@github.com:o/r; rm -rf").is_none());
    }

    #[test]
    fn descendant_boundaries() {
        assert!(is_descendant_of("/a/b/c", "/a/b"));
        assert!(is_descendant_of("/a/b", "/a/b"));
        assert!(!is_descendant_of("/a/bc/d", "/a/b"));
        assert!(is_descendant_of("/x", "/"));
    }

    #[test]
    fn canonicalize_rejects_relative_and_uri() {
        assert!(canonicalize_path_str("relative/x").is_none());
        assert!(canonicalize_path_str("file:///tmp/x").is_none());
        assert!(canonicalize_path_str("/tmp/x").is_some());
    }

    #[test]
    fn git_argv_is_repo_local_and_framed() {
        assert_eq!(
            git_config_argv("/repo/root"),
            vec![
                "-C",
                "/repo/root",
                "config",
                "--local",
                "--includes",
                "--null",
                "--get-regexp",
                r"^remote\..*\.url$",
            ]
        );
    }

    #[test]
    fn git_env_blocklist_covers_hijacks() {
        let blocked = git_env_blocklist();
        for var in [
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_PARAMETERS",
        ] {
            assert!(blocked.contains(&var), "{var} must be stripped");
        }
    }

    #[test]
    fn null_config_overflow_fails_whole_parse_closed() {
        fn record(name: &str, url: &str) -> Vec<u8> {
            format!("remote.{name}.url\n{url}\0").into_bytes()
        }
        // 64 distinct normalized remotes parse fine.
        let mut raw = Vec::new();
        for i in 0..64u32 {
            raw.extend(record(&format!("r{i:02}"), &format!("https://github.com/acme/p{i:03}")));
        }
        let parsed = parse_git_null_config(&raw).expect("64 must parse");
        assert_eq!(parsed.len(), 64);
        // The 65th distinct remote fails the WHOLE parse closed — keeping the
        // first 64 could falsely report a unique claim while the 65th claims
        // another project.
        raw.extend(record("zz", "https://github.com/acme/other"));
        assert_eq!(
            parse_git_null_config(&raw),
            None,
            "65 distinct remotes must fail closed, not truncate"
        );
        // Duplicates never consume the cap: 65 identical records stay one.
        let mut dup = Vec::new();
        for _ in 0..65 {
            dup.extend(record("origin", "https://github.com/acme/same"));
        }
        assert_eq!(parse_git_null_config(&dup).unwrap().len(), 1);
    }
}
