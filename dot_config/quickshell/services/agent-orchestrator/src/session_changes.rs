//! Bounded private sidecar store for per-desktop-session repository changes.
//!
//! The strict activity DB schema (v4) is never touched. This module owns a
//! separate SQLite file next to the activity DB
//! (`<dbname>.changes.db`, e.g. `activity.db.changes.db`) keyed by the
//! immutable desktop `session_id` (32 lowercase hex) plus its project UUID.
//! Sidecar schema v2 adds the commit-range patch, completeness metadata, and
//! repository binding; v1 files migrate forward with data preserved.
//!
//! Capture contract (change-only, never window activity):
//! - Baseline (`HEAD` + bounded worktree status/diff) is captured at the
//!   actual session-creation observation, i.e. when the writer reports a NEW
//!   session. It is never recomputed afterwards.
//! - `latest` is refreshed only while that same session stays the assigned
//!   session, rate-limited to bound collector overhead. When the next session
//!   begins the old row freezes: it is never compared against today's mutable
//!   `HEAD` again.
//! - Committed work is captured as a frozen `baseline_commit..latest_commit`
//!   patch plus commit metadata at refresh time: a clean edit/commit/clean
//!   session therefore still shows actual changes, not just hashes.
//! - Evidence carries the baseline worktree patch, the latest worktree patch,
//!   and the commit-range patch as separately bounded sections with an
//!   explicit attribution limitation: same-file hunks across sections cannot
//!   be mechanically attributed, and a baseline-dirty edit that is later
//!   committed appears in both the baseline and the commit sections.
//! - Sessions first encountered after an upgrade (row missing but the session
//!   is NOT new) are marked `has_baseline = 0` with an explicit reason. The
//!   current `HEAD` is never backfilled as a fabricated start, and no git
//!   date guessing happens.
//! - `local_folder` from the project registry is authoritative. No linked
//!   repo (or no project) means no capture (`available = false` with a clear
//!   reason), never a fabricated empty diff. The row binds the repository
//!   identity (toplevel + git dir); a later linked-folder switch freezes the
//!   row as mismatched instead of comparing across repositories.
//! - Baseline dirty state is persisted alongside the latest state so evidence
//!   can distinguish "already dirty at session start" from "changed during
//!   this session". Pre-existing dirty edits are never attributed as new.
//! - Only repository change metadata/diff is snapshotted, bounded, with the
//!   `project_files` exclusion policy applied as an allowlist BEFORE any
//!   content is obtained (sensitive/protected paths travel as pathspec
//!   exclusions and never reach diff argv as inclusions). No commits are
//!   created and the index is never modified (read-only `git` only).
//! - No hooks, filters, or monitors ever execute: the runner uses a minimal
//!   environment (no global/system config, `GIT_OPTIONAL_LOCKS=0`), flags
//!   that outrank repository config (`--no-optional-locks`,
//!   `-c core.fsmonitor=false`, `-c core.hooksPath=/dev/null`,
//!   `--no-ext-diff`, `--no-textconv`), a fail-closed local-config screen
//!   mirroring `project_files`, its own process group with group kill, and
//!   one aggregate budget per capture. Failures preserve the prior snapshot
//!   and persist explicit partial/unavailable metadata with completeness
//!   flags; repeated failures back off.
//! - The shutdown drain skips capture entirely: an honest missing latest is
//!   preferable to a dropped activity row.
//! - Captures are best-effort and must never break activity persistence:
//!   every entry point catches its own errors, logs one line to stderr, and
//!   returns normally. Subprocesses use argv form (no shell), stdin null.
//! - Capture timestamps are honest last-observed state: `observed_at_ms`
//!   comes from the activity observation, `updated_at_ms` from wall clock at
//!   capture. An offline gap means the frozen `latest` is the last observed
//!   state, not the exact session end.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock, RwLock};
use std::time::{Duration, Instant};

/// Current sidecar schema version. v1 files migrate to v2 with data kept.
pub const SIDECAR_SCHEMA_VERSION: i64 = 2;
/// Minimum wall-clock gap between two git `latest` refreshes of one session.
pub const MIN_UPDATE_INTERVAL_MS: i64 = 30_000;
/// Consecutive capture failures before git attempts back off per session.
pub const FAIL_THRESHOLD: u32 = 3;
/// Backoff window after the threshold is reached.
pub const FAIL_COOLDOWN_MS: i64 = 300_000;
/// Aggregate wall-clock budget for one observe call across ALL git spawns.
pub const CAPTURE_BUDGET_TOTAL: Duration = Duration::from_millis(1200);
/// Bounded git output caps (per invocation).
pub const MAX_GIT_OUTPUT_BYTES: usize = 32 * 1024;
pub const MAX_STATUS_BYTES: usize = 16 * 1024;
/// Bounded persisted evidence caps.
pub const MAX_STORED_DIFF_CHARS: usize = 8_000;
pub const MAX_STORED_COMMIT_PATCH_CHARS: usize = 8_000;
pub const MAX_STORED_STATUS_CHARS: usize = 3_000;
pub const MAX_STORED_META_CHARS: usize = 2_000;
/// Max status entries / allowlisted diff paths / log commits per capture.
pub const MAX_TRACKED_FILES: usize = 200;
pub const MAX_ALLOWLIST_PATHS: usize = 500;
pub const MAX_LOG_COMMITS: usize = 50;

/// Derive the sidecar path for an activity DB path.
///
/// `<dir>/<file>` -> `<dir>/<file>.changes.db` (e.g.
/// `activity.db` -> `activity.db.changes.db`). Keeps per-DB isolation so
/// tests with tmp DBs never share a sidecar. No filesystem access.
pub fn sidecar_path_for(db_path: &Path) -> PathBuf {
    let file_name = db_path
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| "activity.db".to_string());
    let sidecar_name = format!("{file_name}.changes.db");
    match db_path.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => parent.join(sidecar_name),
        _ => PathBuf::from(sidecar_name),
    }
}

// ---------------------------------------------------------------------------
// Collector wiring: process-wide optional sidecar path.
// ---------------------------------------------------------------------------

static SIDECAR_PATH: RwLock<Option<PathBuf>> = RwLock::new(None);

/// Set (or clear with `None`) the sidecar path for collector best-effort
/// captures. Called once by the `collect` binary from its `--db` path.
/// Tests set/clear explicitly. Never fails.
pub fn set_sidecar_path(path: Option<PathBuf>) {
    if let Ok(mut guard) = SIDECAR_PATH.write() {
        *guard = path;
    }
}

/// Current sidecar path, if the collector binary configured one.
pub fn sidecar_path() -> Option<PathBuf> {
    SIDECAR_PATH.read().ok().and_then(|g| g.clone())
}

// ---------------------------------------------------------------------------
// Path exclusion policy (equivalent to scripts/project_files.py).
// ---------------------------------------------------------------------------
//
// Canonical sets mirror `_SENSITIVE_DIRS`, `SENSITIVE_NAMES`, `_PI_PROTECTED`
// and `_HELPER_NAMES` exactly. `is_excluded` answers for a repo-relative
// path; callers enumerate candidate paths FIRST (metadata only) and only
// allowed paths ever reach content-producing diff argv as inclusions.

const SENSITIVE_NAMES: &[&str] = &[
    ".env",
    ".credentials",
    ".netrc",
    "credentials",
    "secrets",
];
const SENSITIVE_DIRS: &[&str] = &[".ssh", ".gnupg", ".aws"];
const PI_PROTECTED: &[&str] = &[
    "auth",
    "credentials",
    "config",
    "agent",
    "extensions",
    "skills",
    "SYSTEM.md",
    "settings.json",
    "trust.json",
    "APPEND_SYSTEM.md",
    "prompts",
    "themes",
];
const HELPER_NAMES: &[&str] = &[
    "daily_agenda.py",
    "desktop_projects.py",
    "desktop_resume.py",
    "journal_assistant.py",
    "journal_sessions.py",
    "logseq_common.py",
    "logseq_graph.py",
    "logseq_todos.py",
    "palette_files.py",
    "project_files.py",
    "project_folder.py",
    "project_overview.py",
    "project_planner.py",
    "project_session_changes.py",
    "project_sessions.py",
    "projects.py",
    "quickshell_settings.py",
    "screen_capture.py",
    "zotero.py",
];

/// Sensitive-name check on ONE path component (case-insensitive), mirroring
/// `project_files._sensitive_name`: full name or stem in the set, or
/// `.env*` / `.credentials.*` prefixes.
fn sensitive_name(part: &str) -> bool {
    let folded = part.to_lowercase();
    if SENSITIVE_NAMES.contains(&folded.as_str()) {
        return true;
    }
    // Stem check: `secrets.tar.gz` has stem `secrets.tar`.
    let stem = part
        .rsplit_once('.')
        .map(|(s, _)| s)
        .unwrap_or(part)
        .to_lowercase();
    if SENSITIVE_NAMES.contains(&stem.as_str()) {
        return true;
    }
    folded == ".env" || folded.starts_with(".env.") || folded.starts_with(".credentials.")
}

/// Compile-time trusted scripts dir (`<manifest>/../../scripts`), resolved
/// once. Only the actual helper import location is protected, never an
/// unrelated project's `scripts/` directory.
fn trusted_scripts_dir() -> Option<PathBuf> {
    static DIR: OnceLock<Option<PathBuf>> = OnceLock::new();
    DIR.get_or_init(|| {
        let lexical = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("..")
            .join("scripts");
        std::fs::canonicalize(&lexical).ok()
    })
    .clone()
}

/// Active registry file + lock (authoritative scope files, never diffed).
fn active_registry_targets() -> Vec<PathBuf> {
    let reg = crate::project_context::resolve_registry_file(None);
    let lock = PathBuf::from(format!("{}.lock", reg.to_string_lossy()));
    let mut out = Vec::with_capacity(2);
    for cand in [reg, lock] {
        // Canonical when possible, lexical otherwise (absent files stay
        // blocked, mirroring project_files).
        match std::fs::canonicalize(&cand) {
            Ok(c) => out.push(c),
            Err(_) => out.push(cand),
        }
    }
    out
}

/// Per-repository exclusion policy. Built once per capture.
struct PathPolicy {
    /// Canonical repository toplevel (absolute paths anchor here).
    root: PathBuf,
    /// Canonical trusted scripts dir, when it resolves.
    trusted: Option<PathBuf>,
    /// Canonical-or-lexical active registry targets.
    registry: Vec<PathBuf>,
}

impl PathPolicy {
    fn for_repo(root: &Path) -> Self {
        Self {
            root: root.to_path_buf(),
            trusted: trusted_scripts_dir(),
            registry: active_registry_targets(),
        }
    }

    fn absolute_of(&self, rel: &str) -> PathBuf {
        self.root.join(rel)
    }

    fn under_trusted(&self, absolute: &Path) -> bool {
        let Some(t) = self.trusted.as_ref() else {
            return false;
        };
        // Resolved spelling first, lexical fallback for missing ancestors.
        if let Ok(resolved) = std::fs::canonicalize(absolute) {
            if resolved.starts_with(t) {
                return true;
            }
        }
        absolute.starts_with(t)
    }

    fn is_registry_target(&self, absolute: &Path) -> bool {
        // Fast path on the basename before any stat, like project_files.
        let name = absolute.file_name().map(|s| s.to_string_lossy());
        let mut basename_hit = false;
        for cand in &self.registry {
            if let (Some(a), Some(b)) = (
                absolute.file_name().map(|s| s.to_string_lossy()),
                cand.file_name().map(|s| s.to_string_lossy()),
            ) {
                if a == b {
                    basename_hit = true;
                    break;
                }
            }
        }
        if !basename_hit {
            let _ = name;
            return false;
        }
        let canonical = std::fs::canonicalize(absolute).unwrap_or_else(|_| absolute.to_path_buf());
        self.registry.contains(&canonical)
    }

    /// True when a repo-relative path must never be listed, read, or diffed.
    /// Mirrors `project_files.is_excluded` + `_protected_child`.
    pub fn is_excluded(&self, rel: &str) -> bool {
        if rel.is_empty() || rel == "." {
            return true;
        }
        let parts: Vec<&str> = rel.split('/').collect();
        if parts.iter().any(|p| p.is_empty() || *p == "." || *p == "..") {
            return true;
        }
        for part in &parts {
            if *part == ".git" || SENSITIVE_DIRS.contains(part) {
                return true;
            }
            // Every part counts (mirrors project_files.is_excluded): a file
            // below a secrets/.env/credentials directory is excluded even
            // when its own basename is innocent.
            if sensitive_name(part) {
                return true;
            }
            if *part == ".project-folder.lock" {
                return true;
            }
            if part.starts_with(".project-folder-") && part.ends_with(".tmp") {
                return true;
            }
        }
        if let Some(name) = parts.last() {
            if *name == "ScopedAgent.qml" {
                return true;
            }
        }
        // Every `.pi` occurrence counts.
        for (pos, part) in parts.iter().enumerate() {
            if *part == ".pi"
                && pos + 1 < parts.len()
                && PI_PROTECTED.contains(&parts[pos + 1])
            {
                return true;
            }
        }
        if parts.contains(&"scripts") {
            if let Some(name) = parts.last() {
                if HELPER_NAMES.contains(name) {
                    return true;
                }
            }
        }
        let absolute = self.absolute_of(rel);
        // Resolved + lexical spellings both count.
        let resolved = std::fs::canonicalize(&absolute).unwrap_or_else(|_| absolute.clone());
        if self.under_trusted(&resolved) || self.under_trusted(&absolute) {
            return true;
        }
        if self.is_registry_target(&resolved) || self.is_registry_target(&absolute) {
            return true;
        }
        false
    }
}

/// Refuse a linked folder itself when it is sensitive/protected, mirroring
/// `project_files._validate_root` (components screened, not just the leaf).
fn validate_capture_root(canonical: &Path, policy_trusted: &Option<PathBuf>) -> Result<(), String> {
    if canonical == Path::new("/") || !canonical.is_dir() {
        return Err("linked folder is not a directory".to_string());
    }
    for sys in ["/etc", "/proc", "/sys", "/dev", "/boot"] {
        if canonical == Path::new(sys) || canonical.starts_with(format!("{sys}/")) {
            return Err("linked folder is sensitive".to_string());
        }
    }
    let text = canonical.to_string_lossy().to_string();
    let parts: Vec<&str> = text.split('/').filter(|p| !p.is_empty()).collect();
    for part in &parts {
        if *part == ".git" || SENSITIVE_DIRS.contains(part) || sensitive_name(part) {
            return Err("linked folder is sensitive".to_string());
        }
    }
    // `.pi` protected subtree.
    for (pos, part) in parts.iter().enumerate() {
        if *part == ".pi" && pos + 1 < parts.len() && PI_PROTECTED.contains(&parts[pos + 1]) {
            return Err("linked folder is protected".to_string());
        }
    }
    if let Some(t) = policy_trusted {
        if canonical.starts_with(t) {
            return Err("linked folder is protected".to_string());
        }
    }
    if let Some(name) = canonical.file_name().map(|s| s.to_string_lossy()) {
        if HELPER_NAMES.contains(&name.as_ref()) || name == "ScopedAgent.qml" {
            return Err("linked folder is protected".to_string());
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Hardened git runner (no hooks/filters/monitors, own process group,
// aggregate budget).
// ---------------------------------------------------------------------------

/// Outcome of one git spawn. Timeouts/overflows kill the whole process
/// group, so a hostile `core.fsmonitor` (or any other spawned helper) can
/// never outlive the call.
enum GitOut {
    Ok(i32, Vec<u8>),
    Timeout,
    Overflow,
    Error,
}

/// Shared deadline across every git spawn of one capture.
struct GitBudget {
    deadline: Instant,
}

impl GitBudget {
    fn new(total: Duration) -> Self {
        Self {
            deadline: Instant::now() + total,
        }
    }

    fn remaining(&self) -> Option<Duration> {
        let now = Instant::now();
        if now >= self.deadline {
            None
        } else {
            Some(self.deadline - now)
        }
    }
}

fn sanitize_git_cmd(cmd: &mut std::process::Command) {
    // Minimal environment: no global/system config, no optional locks, no
    // lazy fetch, no pager. Ambient GIT_* hijacks cannot exist because the
    // environment is cleared first.
    cmd.env_clear();
    cmd.env("PATH", "/usr/bin:/bin");
    cmd.env("LANG", "C");
    cmd.env("LC_ALL", "C");
    cmd.env("GIT_PAGER", "cat");
    cmd.env("GIT_EDITOR", "true");
    cmd.env("GIT_TERMINAL_PROMPT", "0");
    cmd.env("GIT_CONFIG_NOSYSTEM", "1");
    cmd.env("GIT_CONFIG_GLOBAL", "/dev/null");
    cmd.env("GIT_CONFIG_SYSTEM", "/dev/null");
    cmd.env("GIT_OPTIONAL_LOCKS", "0");
    cmd.env("GIT_NO_LAZY_FETCH", "1");
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // Own session/process group so timeout/overflow kills the whole
        // group (never our own pgid).
        unsafe {
            cmd.pre_exec(|| {
                libc::setsid();
                Ok(())
            });
        }
    }
}

#[cfg(unix)]
fn kill_git_group(pid: u32) {
    // Group kill first (children share the child's fresh pgid), direct kill
    // as fallback. Errors mean already gone.
    unsafe {
        libc::killpg(pid as i32, libc::SIGKILL);
    }
    // No direct kill here: the caller waits/reaps next. If killpg failed
    // (already exited) the wait below reaps promptly.
}

/// Run one read-only git argv with the shared budget + output cap.
///
/// `sub` starts with the subcommand. Flags outranking repository config are
/// prepended (`--no-optional-locks`, `core.fsmonitor=false`, ...), so even a
/// hostile local config cannot enable monitors, pagers, or recurse into
/// submodules for these commands.
fn run_git(
    cwd: &Path,
    sub: &[&str],
    allow_codes: &[i32],
    budget: &mut GitBudget,
    cap: usize,
) -> GitOut {
    if cwd.as_os_str().is_empty() {
        return GitOut::Error;
    }
    let mut argv: Vec<&str> = vec![
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "core.pager=cat",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "submodule.recurse=false",
    ];
    argv.extend_from_slice(sub);
    let mut cmd = std::process::Command::new("git");
    cmd.args(&argv);
    sanitize_git_cmd(&mut cmd);
    cmd.current_dir(cwd);
    cmd.stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null());
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(_) => return GitOut::Error,
    };
    let pid = child.id();
    use std::io::Read;
    let stdout = match child.stdout.take() {
        Some(s) => s,
        None => return GitOut::Error,
    };
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut buf = Vec::new();
        let mut chunk = [0u8; 4096];
        let mut src = stdout;
        loop {
            match src.read(&mut chunk) {
                Ok(0) => break,
                Ok(n) => {
                    buf.extend_from_slice(&chunk[..n]);
                    if buf.len() > cap + 1 {
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
                if buf.len() > cap + 1 {
                    #[cfg(unix)]
                    {
                        kill_git_group(pid);
                    }
                    let _ = child.wait();
                    return GitOut::Overflow;
                }
                let code = status.code().unwrap_or(-1);
                if allow_codes.contains(&code) {
                    // Truncate defensively at the cap (byte-exact is fine;
                    // text truncation happens on chars later).
                    let mut buf = buf;
                    buf.truncate(cap);
                    return GitOut::Ok(code, buf);
                }
                return GitOut::Error;
            }
            Ok(None) => {
                if budget.remaining().is_none() {
                    #[cfg(unix)]
                    {
                        kill_git_group(pid);
                    }
                    #[cfg(not(unix))]
                    {
                        let _ = child.kill();
                    }
                    let _ = child.wait();
                    return GitOut::Timeout;
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            Err(_) => {
                #[cfg(unix)]
                {
                    kill_git_group(pid);
                }
                #[cfg(not(unix))]
                {
                    let _ = child.kill();
                }
                return GitOut::Error;
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Fail-closed repository config screen (mirrors project_files._EXEC_CONFIG_RES).
// ---------------------------------------------------------------------------

/// True when a casefolded local-config key can execute commands. Dotted
/// subsections may themselves contain dots, so the middle matches `.+`
/// (non-empty, dots allowed), mirroring the Python patterns.
fn exec_config_key(key: &str) -> bool {
    // Keys fold first: git matches sections case-insensitively and the
    // Python policy casefolds before matching.
    let folded = key.trim().to_lowercase();
    let folded = folded.as_str();
    if let Some(rest) = folded.strip_prefix("core.") {
        return matches!(
            rest,
            "fsmonitor"
                | "fsmonitorhook"
                | "pager"
                | "editor"
                | "askpass"
                | "sshcommand"
                | "hookspath"
        );
    }
    if folded == "sequence.editor" || folded == "diff.external" {
        return true;
    }
    for prefix in ["diff.", "filter."] {
        if let Some(rest) = folded.strip_prefix(prefix) {
            for suffix in ["command", "textconv", "cachetextconv", "clean", "smudge", "process"] {
                if rest.len() > suffix.len() + 1
                    && rest.ends_with(suffix)
                    && rest.as_bytes()[rest.len() - suffix.len() - 1] == b'.'
                {
                    let middle = &rest[..rest.len() - suffix.len() - 1];
                    if !middle.is_empty() {
                        // `diff.*` only owns command/textconv/cachetextconv;
                        // `filter.*` only owns clean/smudge/process.
                        let diff_ok = prefix == "diff."
                            && matches!(suffix, "command" | "textconv" | "cachetextconv");
                        let filter_ok = prefix == "filter."
                            && matches!(suffix, "clean" | "smudge" | "process");
                        if diff_ok || filter_ok {
                            return true;
                        }
                    }
                }
            }
        }
    }
    if let Some(rest) = folded.strip_prefix("merge.") {
        if rest.len() > "driver".len() + 1
            && rest.ends_with("driver")
            && rest.as_bytes()[rest.len() - "driver".len() - 1] == b'.'
            && !rest[..rest.len() - "driver".len() - 1].is_empty()
        {
            return true;
        }
    }
    if folded == "credential.helper" {
        return true;
    }
    if let Some(rest) = folded.strip_prefix("credential.") {
        if rest.ends_with(".helper") && rest.len() > ".helper".len() {
            return true;
        }
    }
    if folded == "include.path" {
        return true;
    }
    if let Some(rest) = folded.strip_prefix("includeif.") {
        if rest.ends_with(".path") {
            return true;
        }
    }
    if folded == "extensions.worktreeconfig" {
        return true;
    }
    false
}

/// Refuse content-producing git when the repository-local config can execute
/// commands. Reads config only (never executes anything). Returns the
/// offending key on refusal.
fn check_repo_config(cwd: &Path, budget: &mut GitBudget) -> Result<(), String> {
    let (code, out) = match run_git(
        cwd,
        &["config", "--local", "--list", "-z"],
        &[0, 1],
        budget,
        MAX_GIT_OUTPUT_BYTES,
    ) {
        GitOut::Ok(code, buf) => (code, buf),
        // Timeouts/overflows/spawn failures reading config fail closed:
        // the screen itself could not prove safety.
        GitOut::Timeout | GitOut::Overflow | GitOut::Error => {
            return Err("cannot inspect repository git config (bounded read failed)".to_string())
        }
    };
    if code == 1 && out.is_empty() {
        return Ok(()); // No local config entries: nothing to screen.
    }
    if code != 0 {
        return Err("cannot inspect repository git config".to_string());
    }
    for record in out.split(|b| *b == 0) {
        if record.is_empty() {
            continue;
        }
        let nl = record.iter().position(|b| *b == b'\n').unwrap_or(record.len());
        let key = String::from_utf8_lossy(&record[..nl]).trim().to_lowercase();
        if exec_config_key(&key) {
            // Report the raw key spelling for honesty.
            let raw = String::from_utf8_lossy(&record[..nl]).trim().to_string();
            return Err(format!("refusing helper execution ({raw})"));
        }
        if key.starts_with("submodule.") && key.ends_with(".update") {
            let value = if nl < record.len() {
                String::from_utf8_lossy(&record[nl + 1..]).trim().to_lowercase()
            } else {
                String::new()
            };
            if !matches!(value.as_str(), "checkout" | "rebase" | "merge" | "none" | "")
                || value.starts_with('!')
            {
                let raw = String::from_utf8_lossy(&record[..nl]).trim().to_string();
                return Err(format!("refusing submodule command ({raw})"));
            }
        }
        if key == "protocol.allow" {
            let value = if nl < record.len() {
                String::from_utf8_lossy(&record[nl + 1..]).trim().to_lowercase()
            } else {
                String::new()
            };
            if value != "never" {
                return Err("refusing widened fetch protocols (protocol.allow)".to_string());
            }
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Capture.
// ---------------------------------------------------------------------------

/// Bounded worktree snapshot at one observation (change metadata only).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RepoState {
    /// Full 40-hex `HEAD`, or `None` when unborn/unresolvable.
    pub commit: Option<String>,
    /// Bounded human-readable status lines for allowed paths only.
    pub status: String,
    /// Bounded unified worktree-vs-`HEAD` diff over allowed paths only.
    pub diff: String,
    /// Count of status entries observed before truncation.
    pub file_count: usize,
    /// True when any bound truncated the snapshot.
    pub truncated: bool,
    /// False when a partial failure produced this snapshot.
    pub complete: bool,
    /// Honest machine note (truncations, refusals) persisted for evidence.
    pub note: String,
}

impl RepoState {
    pub fn empty_unavailable() -> Self {
        Self {
            commit: None,
            status: String::new(),
            diff: String::new(),
            file_count: 0,
            truncated: false,
            complete: false,
            note: "unavailable".to_string(),
        }
    }
}

/// Bounded committed-work snapshot for `baseline..latest` (commit objects
/// only — never worktree filters).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CommitRange {
    /// Bounded `hash time author subject` lines, newest first.
    pub meta: String,
    /// Total commits in range (may exceed the listed ones).
    pub count: u64,
    /// Bounded unified `base..head` diff over allowed paths only.
    pub patch: String,
    pub complete: bool,
    pub note: String,
}

/// Resolved + bound repository identity for one capture.
struct ResolvedRepo {
    toplevel: PathBuf,
    gitdir: PathBuf,
}

fn parse_hex40(bytes: &[u8]) -> Option<String> {
    let text = String::from_utf8_lossy(bytes).trim().to_string();
    if text.len() == 40 && text.bytes().all(|c| c.is_ascii_hexdigit()) {
        Some(text.to_lowercase())
    } else {
        None
    }
}

fn validate_repo_dir(repo: &Path) -> bool {
    if repo.as_os_str().is_empty() {
        return false;
    }
    let s = repo.to_string_lossy();
    if s.len() > 4096 || s.contains('\0') {
        return false;
    }
    if !repo.is_absolute() {
        return false;
    }
    matches!(
        std::fs::symlink_metadata(repo),
        Ok(md) if !md.file_type().is_symlink() && md.is_dir()
    )
}

/// Resolve the linked folder to its repository identity (toplevel + git dir)
/// with the fail-closed config screen. Never produces content.
fn resolve_repo(folder: &Path, budget: &mut GitBudget) -> Result<ResolvedRepo, String> {
    if !validate_repo_dir(folder) {
        return Err("linked folder is not a usable directory".to_string());
    }
    let canonical = std::fs::canonicalize(folder)
        .map_err(|_| "linked folder is not accessible".to_string())?;
    validate_capture_root(&canonical, &trusted_scripts_dir())?;
    let toplevel_raw = match run_git(
        &canonical,
        &["rev-parse", "--show-toplevel"],
        &[0],
        budget,
        4096,
    ) {
        GitOut::Ok(_, out) => out,
        GitOut::Timeout => return Err("git timed out resolving repository".to_string()),
        _ => return Err("not a git repository".to_string()),
    };
    let toplevel_str = String::from_utf8_lossy(&toplevel_raw).trim().to_string();
    let toplevel = std::fs::canonicalize(&toplevel_str).unwrap_or_else(|_| PathBuf::from(&toplevel_str));
    if toplevel_str.is_empty() || !canonical.starts_with(&toplevel) {
        return Err("linked folder is outside its repository".to_string());
    }
    // Screen exec-capable local config BEFORE any content command.
    if let Err(key) = check_repo_config(&toplevel, budget) {
        return Err(format!("repository git config {key}"));
    }
    let gitdir_raw = match run_git(
        &toplevel,
        &["rev-parse", "--absolute-git-dir"],
        &[0],
        budget,
        4096,
    ) {
        GitOut::Ok(_, out) => out,
        GitOut::Timeout => return Err("git timed out resolving repository".to_string()),
        _ => return Err("cannot resolve repository git directory".to_string()),
    };
    let gitdir_str = String::from_utf8_lossy(&gitdir_raw).trim().to_string();
    let gitdir = std::fs::canonicalize(&gitdir_str).unwrap_or_else(|_| PathBuf::from(&gitdir_str));
    Ok(ResolvedRepo { toplevel, gitdir })
}

/// Parse NUL-framed porcelain v1 (`-z`) into validated repo-relative paths.
/// Returns `(tracked_lines, untracked_names, total_records)`. Skips renames
/// (disabled upstream), invalid UTF-8, and unsafe spellings.
fn parse_status_nul(raw: &[u8]) -> (Vec<(String, String)>, Vec<String>, usize) {
    let mut tracked: Vec<(String, String)> = Vec::new();
    let mut untracked: Vec<String> = Vec::new();
    let mut total = 0usize;
    for token in raw.split(|b| *b == 0) {
        if token.is_empty() {
            continue;
        }
        let Ok(text) = std::str::from_utf8(token) else {
            continue;
        };
        if text.len() < 4 || text.as_bytes()[2] != b' ' {
            continue;
        }
        total += 1;
        let (xy, path) = (&text[..2], &text[3..]);
        if path.is_empty()
            || path == "."
            || path.starts_with('/')
            || path.split('/').any(|p| p.is_empty() || p == "." || p == "..")
        {
            continue;
        }
        if xy == "??" {
            untracked.push(path.to_string());
        } else {
            tracked.push((xy.to_string(), path.to_string()));
        }
    }
    (tracked, untracked, total)
}

/// Parse NUL-framed `--name-only` output into validated repo-relative paths.
fn parse_names_nul(raw: &[u8]) -> Vec<String> {
    let mut out = Vec::new();
    for token in raw.split(|b| *b == 0) {
        if token.is_empty() {
            continue;
        }
        let Ok(text) = std::str::from_utf8(token) else {
            continue;
        };
        if text.is_empty()
            || text == "."
            || text.starts_with('/')
            || text.split('/').any(|p| p.is_empty() || p == "." || p == "..")
        {
            continue;
        }
        out.push(text.to_string());
    }
    out
}

fn clip_chars(text: &str, limit: usize, notice: &str) -> (String, bool) {
    if text.chars().count() <= limit {
        return (text.to_string(), false);
    }
    let clipped: String = text.chars().take(limit).collect();
    (format!("{}{notice}", clipped.trim_end()), true)
}

/// Build literal allowlist pathspecs plus forbidden counterpart exclusions
/// (a literal inclusion also matches descendants, so a replaced directory
/// would leak a deleted secret without the exclusion — same rule as
/// `project_files._scoped_diff`).
fn allowlist_spec(allowed: &[String], forbidden: &[String]) -> Vec<String> {    let mut seen = std::collections::HashSet::new();
    let mut include: Vec<String> = Vec::new();
    for p in allowed {
        if seen.insert(p.clone()) {
            include.push(format!(":(literal){p}"));
        }
    }
    seen.clear();
    let mut spec = include;
    for p in forbidden {
        if seen.insert(p.clone()) {
            spec.push(format!(":(exclude,literal){p}"));
        }
    }
    spec
}

/// Split enumerated change names into allowlist inclusions and forbidden
/// exclusions. Single pass over the WHOLE input: the allowlist cap never
/// stops the scan for forbidden descendants (a file replacing a directory
/// must still exclude the deleted secret beneath it). Returns
/// `(allowed, forbidden, capped)`.
fn split_names(
    names: Vec<String>,
    policy: &PathPolicy,
    cap: usize,
) -> (Vec<String>, Vec<String>, bool) {
    let mut allowed = Vec::new();
    let mut forbidden = Vec::new();
    let mut skipped = 0usize;
    for p in names {
        if policy.is_excluded(&p) {
            forbidden.push(p);
        } else if allowed.len() < cap {
            allowed.push(p);
        } else {
            skipped += 1;
        }
    }
    (allowed, forbidden, skipped > 0)
}

/// Capture the bounded worktree state. `None` only when no honest snapshot
/// is possible (not a repo, refused config, timeout, failure); partial
/// output degrades to `complete = false` with an explicit note instead.
pub fn capture_repo_state(repo: &Path) -> Option<RepoState> {
    let mut budget = GitBudget::new(CAPTURE_BUDGET_TOTAL);
    let resolved = resolve_repo(repo, &mut budget).ok()?;
    capture_worktree(&resolved, &mut budget)
}

fn capture_worktree(resolved: &ResolvedRepo, budget: &mut GitBudget) -> Option<RepoState> {
    let top = &resolved.toplevel;
    let policy = PathPolicy::for_repo(top);
    // HEAD (unborn is fine: worktree status still applies).
    let commit = match run_git(top, &["rev-parse", "--verify", "HEAD"], &[0], budget, 128) {
        GitOut::Ok(_, out) => parse_hex40(&out),
        GitOut::Timeout => return None,
        _ => None,
    };
    // Status first: candidate paths are enumerated as metadata BEFORE any
    // content command, and only allowed paths reach the diff.
    let status_raw = match run_git(
        top,
        &[
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--no-renames",
            "--ignore-submodules=all",
            "--",
            ".",
        ],
        &[0],
        budget,
        MAX_GIT_OUTPUT_BYTES,
    ) {
        GitOut::Ok(_, out) => out,
        GitOut::Overflow => {
            return Some(RepoState {
                commit,
                status: format!(
                    "… [status exceeded {} bytes; listing withheld]",
                    MAX_STATUS_BYTES
                ),
                diff: String::new(),
                file_count: 0,
                truncated: true,
                complete: false,
                note: "status listing withheld (output cap)".to_string(),
            })
        }
        _ => return None,
    };
    if status_raw.len() > MAX_STATUS_BYTES + 1024 {
        return Some(RepoState {
            commit,
            status: format!(
                "… [status exceeded {} bytes; listing withheld]",
                MAX_STATUS_BYTES
            ),
            diff: String::new(),
            file_count: 0,
            truncated: true,
            complete: false,
            note: "status listing withheld (output cap)".to_string(),
        });
    }
    let (tracked, untracked, _total) = parse_status_nul(&status_raw);
    let mut allowed: Vec<String> = Vec::new();
    let mut withheld = 0usize;
    for (xy, path) in &tracked {
        if allowed.len() >= MAX_TRACKED_FILES {
            withheld += 1;
            continue;
        }
        if policy.is_excluded(path) {
            withheld += 1;
            continue;
        }
        allowed.push(path.clone());
        let _ = xy;
    }
    let mut untracked_shown: Vec<String> = Vec::new();
    for path in &untracked {
        if allowed.len() + untracked_shown.len() >= MAX_TRACKED_FILES {
            withheld += 1;
            continue;
        }
        if policy.is_excluded(path) {
            withheld += 1;
            continue;
        }
        untracked_shown.push(path.clone());
    }
    let file_count = allowed.len() + untracked_shown.len();
    let mut status_lines: Vec<String> = tracked
        .iter()
        .filter_map(|(xy, p)| {
            if allowed.contains(p) {
                Some(format!("{xy} {p}"))
            } else {
                None
            }
        })
        .collect();
    for p in &untracked_shown {
        status_lines.push(format!("?? {p}"));
    }
    let mut status = status_lines.join("\n");
    let mut truncated = withheld > 0;
    if withheld > 0 {
        status.push_str(&format!("\n… [{withheld} path(s) withheld by privacy policy or entry cap]"));
    }
    let mut notes: Vec<String> = Vec::new();
    if withheld > 0 {
        notes.push("status truncated (privacy exclusions or entry cap)".to_string());
    }
    // Changed-name enumeration (metadata only) finds forbidden counterparts.
    // A failed enumeration withholds ALL content: without the counterpart
    // list the exclusions cannot be proven, so no diff may run.
    let changed: Option<Vec<String>> = match run_git(
        top,
        &[
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--ignore-submodules=all",
            "HEAD",
            "--",
            ".",
        ],
        &[0],
        budget,
        MAX_GIT_OUTPUT_BYTES,
    ) {
        GitOut::Ok(_, out) => Some(parse_names_nul(&out)),
        GitOut::Timeout => {
            notes.push("changed-name enumeration timed out; diff withheld".to_string());
            None
        }
        GitOut::Overflow => {
            notes.push("changed-name enumeration overflowed; diff withheld".to_string());
            None
        }
        GitOut::Error => {
            notes.push("changed-name enumeration failed; diff withheld".to_string());
            None
        }
    };
    let allowed_set: std::collections::HashSet<&str> =
        allowed.iter().map(|s| s.as_str()).collect();
    // Forbidden counterparts are scanned across the whole enumeration even
    // when the allowlist is capped (see `split_names`).
    let names_ok = changed.is_some();
    let forbidden: Vec<String> = match changed {
        Some(names) => {
            let (_, forbidden, _) = split_names(names, &policy, usize::MAX);
            forbidden
                .into_iter()
                .filter(|p| !allowed_set.contains(p.as_str()))
                .collect()
        }
        None => Vec::new(),
    };
    // Content diff strictly over the allowlist, and only when the
    // enumeration succeeded: an empty allowlist with no exclusions means a
    // clean tree (skip the spawn); a failed enumeration withholds content.
    let mut diff = String::new();
    let mut diff_complete = true;
    if !names_ok {
        diff_complete = false;
    } else if !allowed.is_empty() || !forbidden.is_empty() {
        let mut inc: Vec<String> = allowed.clone();
        if inc.len() > MAX_ALLOWLIST_PATHS {
            inc.truncate(MAX_ALLOWLIST_PATHS);
            truncated = true;
            notes.push("diff allowlist truncated (path cap)".to_string());
        }
        let spec = allowlist_spec(&inc, &forbidden);
        let mut full: Vec<String> = vec![
            "diff".to_string(),
            "--no-renames".to_string(),
            "--no-ext-diff".to_string(),
            "--no-textconv".to_string(),
            "--no-color".to_string(),
            "--unified=1".to_string(),
            "--ignore-submodules=all".to_string(),
            "HEAD".to_string(),
            "--".to_string(),
        ];
        full.extend(spec);
        let arg_refs: Vec<&str> = full.iter().map(|s| s.as_str()).collect();
        match run_git(top, &arg_refs, &[0, 1], budget, MAX_GIT_OUTPUT_BYTES) {
            GitOut::Ok(_, out) => {
                diff = String::from_utf8_lossy(&out).to_string();
            }
            GitOut::Overflow => {
                diff_complete = false;
                notes.push("diff withheld (output cap)".to_string());
            }
            GitOut::Timeout => {
                diff_complete = false;
                notes.push("diff timed out (aggregate budget)".to_string());
            }
            GitOut::Error => {
                diff_complete = false;
                notes.push("diff failed".to_string());
            }
        }
    }
    let (diff, diff_cut) = clip_chars(&diff, MAX_STORED_DIFF_CHARS, "\n… [diff truncated]");
    if diff_cut {
        truncated = true;
        notes.push(format!("diff truncated at {MAX_STORED_DIFF_CHARS} chars"));
    }
    let (status, status_cut) = clip_chars(&status, MAX_STORED_STATUS_CHARS, "… [truncated]");
    if status_cut {
        truncated = true;
        notes.push(format!("status truncated at {MAX_STORED_STATUS_CHARS} chars"));
    }
    Some(RepoState {
        commit,
        status,
        diff,
        file_count,
        truncated,
        complete: diff_complete,
        note: notes.join("; "),
    })
}

/// Capture the bounded `base..head` commit range (commit objects only).
/// Infallible by construction: failures yield `complete = false` with an
/// explicit note instead of `None`.
pub fn capture_commit_range(repo: &Path, base: &str, head: &str) -> CommitRange {
    let mut budget = GitBudget::new(CAPTURE_BUDGET_TOTAL);
    let resolved = match resolve_repo(repo, &mut budget) {
        Ok(r) => r,
        Err(e) => {
            return CommitRange {
                meta: String::new(),
                count: 0,
                patch: String::new(),
                complete: false,
                note: format!("commit range unavailable ({e})"),
            }
        }
    };
    capture_commit_range_in(&resolved, &mut budget, base, head)
}

/// Shared-budget commit-range capture used by the observe path so one
/// aggregate deadline governs resolution, worktree, and range together
/// (never a fresh budget per phase).
fn capture_commit_range_in(
    resolved: &ResolvedRepo,
    mut budget: &mut GitBudget,
    base: &str,
    head: &str,
) -> CommitRange {
    let empty = |note: &str| CommitRange {
        meta: String::new(),
        count: 0,
        patch: String::new(),
        complete: false,
        note: note.to_string(),
    };
    if base.len() != 40
        || head.len() != 40
        || !base.bytes().all(|c| c.is_ascii_hexdigit())
        || !head.bytes().all(|c| c.is_ascii_hexdigit())
        || base == head
    {
        return CommitRange {
            meta: String::new(),
            count: 0,
            patch: String::new(),
            complete: true,
            note: String::new(),
        };
    }
    let top = &resolved.toplevel;
    let policy = PathPolicy::for_repo(top);
    let range = format!("{base}..{head}");
    let count: u64 = match run_git(
        top,
        &["rev-list", "--count", &range],
        &[0],
        &mut budget,
        64,
    ) {
        GitOut::Ok(_, out) => String::from_utf8_lossy(&out)
            .trim()
            .parse()
            .unwrap_or(0),
        _ => return empty("commit range unavailable (count failed)"),
    };
    // Commit metadata, newest first, bounded. Records terminate with
    // \x1e (subjects may contain newlines but never NUL, so \x00 stays the
    // field separator and \x1e the record separator).
    let max_count = format!("--max-count={}", MAX_LOG_COMMITS + 1);
    let log_args = vec![
        "log",
        "--no-show-signature",
        "--format=%H%x00%ct%x00%an%x00%s%x1e",
        max_count.as_str(),
        range.as_str(),
    ];
    let meta_raw = match run_git(top, &log_args, &[0], &mut budget, MAX_GIT_OUTPUT_BYTES) {
        GitOut::Ok(_, out) => out,
        _ => {
            return CommitRange {
                meta: String::new(),
                count,
                patch: String::new(),
                complete: false,
                note: "commit metadata unavailable".to_string(),
            }
        }
    };
    let mut metas: Vec<String> = Vec::new();
    for record in meta_raw.split(|b| *b == 0x1e) {
        if record.is_empty() {
            continue;
        }
        let text = String::from_utf8_lossy(record);
        let mut fields = text.splitn(4, '\0');
        let (h, t, a, s) = (
            fields.next().unwrap_or(""),
            fields.next().unwrap_or(""),
            fields.next().unwrap_or(""),
            fields.next().unwrap_or(""),
        );
        if h.len() != 40 || !h.bytes().all(|c| c.is_ascii_hexdigit()) {
            continue;
        }
        metas.push(format!("{} {} {} {}", &h[..12], t.trim(), a.trim(), s.trim()));
        if metas.len() > MAX_LOG_COMMITS {
            break;
        }
    }
    let mut note_parts: Vec<String> = Vec::new();
    let mut meta = metas.join("\n");
    if (count as usize) > MAX_LOG_COMMITS {
        meta.push_str(&format!("\n… [showing {MAX_LOG_COMMITS} of {count} commits]"));
        note_parts.push("commit list truncated".to_string());
    }
    let (meta, _) = clip_chars(&meta, MAX_STORED_META_CHARS, "… [truncated]");
    // Names in range (metadata only), then allowlisted content patch.
    let names_raw = match run_git(
        top,
        &[
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--ignore-submodules=all",
            &range,
            "--",
            ".",
        ],
        &[0],
        &mut budget,
        MAX_GIT_OUTPUT_BYTES,
    ) {
        GitOut::Ok(_, out) => out,
        _ => {
            return CommitRange {
                meta,
                count,
                patch: String::new(),
                complete: false,
                note: "commit patch withheld (name enumeration failed)".to_string(),
            }
        }
    };
    let names = parse_names_nul(&names_raw);
    // Single pass over ALL names: the allowlist cap never stops the scan
    // for forbidden descendants (see `split_names`).
    let (allowed, forbidden, capped) = split_names(names, &policy, MAX_ALLOWLIST_PATHS);
    if capped {
        note_parts.push("commit patch allowlist truncated (path cap)".to_string());
    }
    if allowed.is_empty() {
        let mut note = String::new();
        if !forbidden.is_empty() {
            note = "commit patch withheld by privacy policy".to_string();
        }
        return CommitRange {
            meta,
            count,
            patch: String::new(),
            complete: true,
            note,
        };
    }
    let spec = allowlist_spec(&allowed, &forbidden);
    let mut full: Vec<String> = vec![
        "diff".to_string(),
        "--no-renames".to_string(),
        "--no-ext-diff".to_string(),
        "--no-textconv".to_string(),
        "--no-color".to_string(),
        "--unified=1".to_string(),
        "--ignore-submodules=all".to_string(),
        range.clone(),
        "--".to_string(),
    ];
    full.extend(spec);
    let arg_refs: Vec<&str> = full.iter().map(|s| s.as_str()).collect();
    let (patch, complete, patch_note) = match run_git(top, &arg_refs, &[0, 1], &mut budget, MAX_GIT_OUTPUT_BYTES) {
        GitOut::Ok(_, out) => {
            let text = String::from_utf8_lossy(&out).to_string();
            let (clipped, cut) = clip_chars(&text, MAX_STORED_COMMIT_PATCH_CHARS, "\n… [patch truncated]");
            let note = cut.then(|| format!("commit patch truncated at {MAX_STORED_COMMIT_PATCH_CHARS} chars"));
            (clipped, true, note)
        }
        GitOut::Overflow => (String::new(), false, Some("commit patch withheld (output cap)".to_string())),
        GitOut::Timeout => (String::new(), false, Some("commit patch timed out (aggregate budget)".to_string())),
        GitOut::Error => (String::new(), false, Some("commit patch failed".to_string())),
    };
    if let Some(n) = patch_note {
        note_parts.push(n);
    }
    CommitRange {
        meta,
        count,
        patch,
        complete,
        note: note_parts.join("; "),
    }
}

/// Whether a refreshed commit range may replace the prior one.
///
/// A failed/empty refresh with prior data preserves it (explicit failure,
/// never conflated with no-range-needed, which bypasses this helper);
/// a complete range, fresh content, or a first-ever range advances
/// (the last honestly marked incomplete by the caller).
fn range_refresh_advances(prior_patch: &str, fresh: &CommitRange) -> bool {
    fresh.complete || !fresh.patch.is_empty() || prior_patch.is_empty()
}

// ---------------------------------------------------------------------------
// Sidecar SQLite store (private file, separate from activity v4).
// ---------------------------------------------------------------------------

/// One sidecar row (schema v2).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ChangeRow {
    pub session_id: String,
    pub project_id: String,
    pub repo_path: String,
    pub repo_gitdir: String,
    /// `ok` or `mismatch` (linked folder switched repos: frozen, unavailable).
    pub repo_status: String,
    pub baseline_commit: Option<String>,
    pub baseline_status: String,
    pub baseline_diff: String,
    pub baseline_complete: bool,
    pub latest_commit: Option<String>,
    pub latest_status: String,
    pub latest_diff: String,
    pub latest_complete: bool,
    pub commit_meta: String,
    pub commit_patch: String,
    pub commit_complete: bool,
    pub capture_note: String,
    pub has_baseline: bool,
    pub reason: String,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
    pub observed_at_ms: i64,
}

/// v2 columns added onto a v1 table (name, DDL fragment).
const V2_COLUMNS: &[(&str, &str)] = &[
    ("repo_gitdir", "TEXT NOT NULL DEFAULT ''"),
    ("repo_status", "TEXT NOT NULL DEFAULT 'ok'"),
    ("commit_meta", "TEXT NOT NULL DEFAULT ''"),
    ("commit_patch", "TEXT NOT NULL DEFAULT ''"),
    ("baseline_complete", "INTEGER NOT NULL DEFAULT 1"),
    ("latest_complete", "INTEGER NOT NULL DEFAULT 1"),
    ("commit_complete", "INTEGER NOT NULL DEFAULT 1"),
    ("capture_note", "TEXT NOT NULL DEFAULT ''"),
];

/// Private sidecar store. Synchronous, bounded, best-effort.
pub struct SessionChangeStore {
    conn: rusqlite::Connection,
}

impl SessionChangeStore {
    fn init_schema(conn: &rusqlite::Connection) -> Result<(), String> {
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
             CREATE TABLE IF NOT EXISTS session_changes (
               session_id TEXT PRIMARY KEY,
               project_id TEXT NOT NULL,
               repo_path TEXT NOT NULL,
               repo_gitdir TEXT NOT NULL DEFAULT '',
               repo_status TEXT NOT NULL DEFAULT 'ok',
               baseline_commit TEXT,
               baseline_status TEXT NOT NULL DEFAULT '',
               baseline_diff TEXT NOT NULL DEFAULT '',
               baseline_complete INTEGER NOT NULL DEFAULT 1,
               latest_commit TEXT,
               latest_status TEXT NOT NULL DEFAULT '',
               latest_diff TEXT NOT NULL DEFAULT '',
               latest_complete INTEGER NOT NULL DEFAULT 1,
               commit_meta TEXT NOT NULL DEFAULT '',
               commit_patch TEXT NOT NULL DEFAULT '',
               commit_complete INTEGER NOT NULL DEFAULT 1,
               capture_note TEXT NOT NULL DEFAULT '',
               has_baseline INTEGER NOT NULL DEFAULT 1,
               reason TEXT NOT NULL DEFAULT '',
               created_at_ms INTEGER NOT NULL,
               updated_at_ms INTEGER NOT NULL,
               observed_at_ms INTEGER NOT NULL
             );",
        )
        .map_err(|e| format!("init sidecar schema: {e}"))?;
        // Migrate a v1 table forward: add missing v2 columns, keep all rows.
        let mut existing = std::collections::HashSet::new();
        {
            let mut stmt = conn
                .prepare("PRAGMA table_info(session_changes)")
                .map_err(|e| format!("sidecar pragma: {e}"))?;
            let rows = stmt
                .query_map([], |r| r.get::<_, String>(1))
                .map_err(|e| format!("sidecar pragma rows: {e}"))?;
            for name in rows.filter_map(|r| r.ok()) {
                existing.insert(name);
            }
        }
        for (col, ddl) in V2_COLUMNS {
            if !existing.contains(*col) {
                conn.execute_batch(&format!("ALTER TABLE session_changes ADD COLUMN {col} {ddl}"))
                    .map_err(|e| format!("sidecar migrate {col}: {e}"))?;
            }
        }
        // Singleton version row moves to v2 (best-effort, ignore races).
        let n: i64 = conn
            .query_row("SELECT COUNT(*) FROM schema_version", [], |r| r.get(0))
            .map_err(|e| format!("sidecar version count: {e}"))?;
        let prev: i64 = conn
            .query_row(
                "SELECT COALESCE(MAX(version), 0) FROM schema_version",
                [],
                |r| r.get(0),
            )
            .unwrap_or(0);
        if prev < SIDECAR_SCHEMA_VERSION {
            // v1 snapshots lack completeness provenance entirely: mark all
            // three components incomplete with an explicit historical
            // limitation. Data stays frozen as stored (history preserved,
            // never refabricated).
            conn.execute(
                "UPDATE session_changes SET baseline_complete = 0, latest_complete = 0,
                 commit_complete = 0,
                 capture_note = 'migrated from sidecar v1: pre-v2 snapshots lack completeness provenance, treated as historical partial; baseline/latest/commit frozen as stored'",
                [],
            )
            .map_err(|e| format!("sidecar migrate completeness: {e}"))?;
        }
        if n == 0 {
            let _ = conn.execute(
                "INSERT OR IGNORE INTO schema_version (version) VALUES (?1)",
                rusqlite::params![SIDECAR_SCHEMA_VERSION],
            );
        } else {
            let _ = conn.execute(
                "UPDATE schema_version SET version = ?1",
                rusqlite::params![SIDECAR_SCHEMA_VERSION],
            );
        }
        Ok(())
    }

    /// Open (creating `0600` when new) the sidecar at `path`. Migrates v1.
    pub fn open(path: &Path) -> Result<Self, String> {
        if path.as_os_str().is_empty() {
            return Err("sidecar path is empty".to_string());
        }
        let s = path.to_string_lossy();
        if s.len() > 4096 || s.contains('\0') {
            return Err("sidecar path is unsafe".to_string());
        }
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                crate::desktop_paths::ensure_parent_for_file(path)
                    .map_err(|e| format!("sidecar parent: {e}"))?;
            }
        }
        // Refuse symlinked sidecar files; create 0600 when missing.
        let is_new = std::fs::symlink_metadata(path)
            .map(|_| false)
            .unwrap_or(true);
        if !is_new {
            crate::desktop_paths::validate_file_private(path)
                .map_err(|e| format!("sidecar file: {e}"))?;
        } else {
            #[cfg(unix)]
            {
                crate::desktop_paths::create_private_file(path)
                    .map_err(|e| format!("sidecar create: {e}"))?;
            }
            #[cfg(not(unix))]
            {
                std::fs::File::create(path).map_err(|e| format!("sidecar create: {e}"))?;
            }
        }
        let conn = rusqlite::Connection::open(path).map_err(|e| format!("open sidecar: {e}"))?;
        conn.busy_timeout(std::time::Duration::from_millis(100))
            .map_err(|e| format!("sidecar busy timeout: {e}"))?;
        Self::init_schema(&conn)?;
        Ok(Self { conn })
    }

    /// Open with a fresh in-memory connection (tests).
    pub fn open_in_memory() -> Result<Self, String> {
        let conn =
            rusqlite::Connection::open_in_memory().map_err(|e| format!("open memory: {e}"))?;
        Self::init_schema(&conn)?;
        Ok(Self { conn })
    }

    pub fn get(&self, session_id: &str) -> Result<Option<ChangeRow>, String> {
        let sid = normalize_session(session_id)?;
        let mut stmt = self
            .conn
            .prepare(
                "SELECT session_id, project_id, repo_path, repo_gitdir, repo_status,
                 baseline_commit, baseline_status, baseline_diff, baseline_complete,
                 latest_commit, latest_status, latest_diff, latest_complete,
                 commit_meta, commit_patch, commit_complete, capture_note,
                 has_baseline, reason, created_at_ms, updated_at_ms, observed_at_ms
                 FROM session_changes WHERE session_id = ?1",
            )
            .map_err(|e| format!("sidecar get prepare: {e}"))?;
        let row = stmt
            .query_row(rusqlite::params![sid], |r| {
                let flag = |v: rusqlite::Result<i64>| v.map(|x| x != 0).unwrap_or(false);
                Ok(ChangeRow {
                    session_id: r.get(0)?,
                    project_id: r.get(1)?,
                    repo_path: r.get(2)?,
                    repo_gitdir: r.get(3)?,
                    repo_status: r.get(4)?,
                    baseline_commit: r.get(5)?,
                    baseline_status: r.get(6)?,
                    baseline_diff: r.get(7)?,
                    baseline_complete: flag(r.get(8)),
                    latest_commit: r.get(9)?,
                    latest_status: r.get(10)?,
                    latest_diff: r.get(11)?,
                    latest_complete: flag(r.get(12)),
                    commit_meta: r.get(13)?,
                    commit_patch: r.get(14)?,
                    commit_complete: flag(r.get(15)),
                    capture_note: r.get(16)?,
                    has_baseline: flag(r.get(17)),
                    reason: r.get(18)?,
                    created_at_ms: r.get(19)?,
                    updated_at_ms: r.get(20)?,
                    observed_at_ms: r.get(21)?,
                })
            })
            .optional()
            .map_err(|e| format!("sidecar get: {e}"))?;
        Ok(row)
    }

    /// Insert a fresh baseline row (new session). Overwrites nothing: when a
    /// row already exists this is a no-op returning `Ok(false)`.
    #[allow(clippy::too_many_arguments)]
    pub fn insert_baseline(
        &self,
        session_id: &str,
        project_id: &str,
        repo_path: &str,
        repo_gitdir: &str,
        observed_at_ms: i64,
        state: &RepoState,
        now_ms: i64,
    ) -> Result<bool, String> {
        let sid = normalize_session(session_id)?;
        let pid = normalize_project(project_id)?;
        if self.get(&sid)?.is_some() {
            return Ok(false);
        }
        // No commit range exists yet at session creation by construction.
        self.conn
            .execute(
                "INSERT OR IGNORE INTO session_changes
                 (session_id, project_id, repo_path, repo_gitdir, repo_status,
                  baseline_commit, baseline_status, baseline_diff, baseline_complete,
                  latest_commit, latest_status, latest_diff, latest_complete,
                  commit_meta, commit_patch, commit_complete, capture_note,
                  has_baseline, reason, created_at_ms, updated_at_ms, observed_at_ms)
                 VALUES (?1,?2,?3,?4,'ok',?5,?6,?7,?8,?5,?6,?7,?8,'','',1,?9,1,'',?10,?10,?11)",
                rusqlite::params![
                    sid,
                    pid,
                    repo_path,
                    repo_gitdir,
                    state.commit,
                    state.status,
                    state.diff,
                    state.complete as i64,
                    state.note,
                    now_ms,
                    observed_at_ms,
                ],
            )
            .map_err(|e| format!("sidecar insert baseline: {e}"))?;
        Ok(true)
    }

    /// Insert an upgrade-marker row: the session already existed before
    /// capture, so `has_baseline = 0` and the baseline is NEVER backfilled
    /// with the current `HEAD`. `latest` may still record the current state.
    #[allow(clippy::too_many_arguments)]
    pub fn insert_missing_baseline(
        &self,
        session_id: &str,
        project_id: &str,
        repo_path: &str,
        repo_gitdir: &str,
        observed_at_ms: i64,
        latest: Option<&RepoState>,
        now_ms: i64,
    ) -> Result<bool, String> {
        let sid = normalize_session(session_id)?;
        let pid = normalize_project(project_id)?;
        if self.get(&sid)?.is_some() {
            return Ok(false);
        }
        let reason = "baseline unavailable: session started before change capture; start commit not fabricated"
            .to_string();
        let complete = latest.map(|s| s.complete as i64).unwrap_or(0);
        let note = latest.map(|s| s.note.clone()).unwrap_or_default();
        self.conn
            .execute(
                "INSERT OR IGNORE INTO session_changes
                 (session_id, project_id, repo_path, repo_gitdir, repo_status,
                  baseline_commit, baseline_status, baseline_diff, baseline_complete,
                  latest_commit, latest_status, latest_diff, latest_complete,
                  commit_meta, commit_patch, commit_complete, capture_note,
                  has_baseline, reason, created_at_ms, updated_at_ms, observed_at_ms)
                 VALUES (?1,?2,?3,?4,'ok',NULL,'','',1,?5,?6,?7,?8,'','',1,?9,0,?10,?11,?11,?12)",
                rusqlite::params![
                    sid,
                    pid,
                    repo_path,
                    repo_gitdir,
                    latest.and_then(|s| s.commit.clone()),
                    latest.map(|s| s.status.clone()).unwrap_or_default(),
                    latest.map(|s| s.diff.clone()).unwrap_or_default(),
                    complete,
                    note,
                    reason,
                    now_ms,
                    observed_at_ms,
                ],
            )
            .map_err(|e| format!("sidecar insert missing: {e}"))?;
        Ok(true)
    }

    /// Refresh `latest` (plus the frozen commit range) for the live session.
    /// Never touches the baseline. `commit = None` preserves the prior
    /// commit range (failed refresh keeps prior data, never empty success).
    #[allow(clippy::too_many_arguments)]
    pub fn update_latest(
        &self,
        session_id: &str,
        observed_at_ms: i64,
        state: &RepoState,
        commit: Option<&CommitRange>,
        note: &str,
        now_ms: i64,
    ) -> Result<(), String> {
        let sid = normalize_session(session_id)?;
        self.conn
            .execute(
                "UPDATE session_changes SET latest_commit=?1, latest_status=?2,
                 latest_diff=?3, latest_complete=?4,
                 updated_at_ms=?5, observed_at_ms=?6, capture_note=?7
                 WHERE session_id=?8",
                rusqlite::params![
                    state.commit,
                    state.status,
                    state.diff,
                    state.complete as i64,
                    now_ms,
                    observed_at_ms,
                    note,
                    sid
                ],
            )
            .map_err(|e| format!("sidecar update latest: {e}"))?;
        if let Some(c) = commit {
            self.conn
                .execute(
                    "UPDATE session_changes SET commit_meta=?1, commit_patch=?2,
                     commit_complete=?3 WHERE session_id=?4",
                    rusqlite::params![c.meta, c.patch, c.complete as i64, sid],
                )
                .map_err(|e| format!("sidecar update commit range: {e}"))?;
        }
        Ok(())
    }

    /// Record a failed refresh honestly: prior snapshots stay, the note says
    /// so. The observed timestamp still advances (last-observed honesty).
    pub fn note_refresh_failed(
        &self,
        session_id: &str,
        observed_at_ms: i64,
        note: &str,
    ) -> Result<(), String> {
        let sid = normalize_session(session_id)?;
        self.conn
            .execute(
                "UPDATE session_changes SET observed_at_ms=?1, capture_note=?2
                 WHERE session_id=?3",
                rusqlite::params![observed_at_ms, note, sid],
            )
            .map_err(|e| format!("sidecar note failure: {e}"))?;
        Ok(())
    }

    /// Freeze a row whose linked folder switched repositories: never compare
    /// across repos. Data stays for forensics; reads report unavailable.
    pub fn mark_repo_mismatch(
        &self,
        session_id: &str,
        observed_at_ms: i64,
        detail: &str,
    ) -> Result<(), String> {
        let sid = normalize_session(session_id)?;
        self.conn
            .execute(
                "UPDATE session_changes SET repo_status='mismatch',
                 observed_at_ms=?1, capture_note=?2 WHERE session_id=?3",
                rusqlite::params![observed_at_ms, detail, sid],
            )
            .map_err(|e| format!("sidecar mark mismatch: {e}"))?;
        Ok(())
    }

    /// Backfill the repository identity for pre-v2 rows bound to the same
    /// toplevel (migration-era rows carry an empty git dir).
    pub fn backfill_gitdir(&self, session_id: &str, gitdir: &str) -> Result<(), String> {
        let sid = normalize_session(session_id)?;
        self.conn
            .execute(
                "UPDATE session_changes SET repo_gitdir=?1 WHERE session_id=?2",
                rusqlite::params![gitdir, sid],
            )
            .map_err(|e| format!("sidecar backfill gitdir: {e}"))?;
        Ok(())
    }

    /// Cheap liveness bump when the diff refresh is rate-limited: keeps the
    /// honest last-observed timestamp without a git subprocess.
    pub fn touch_observed(&self, session_id: &str, observed_at_ms: i64) -> Result<(), String> {
        let sid = normalize_session(session_id)?;
        self.conn
            .execute(
                "UPDATE session_changes SET observed_at_ms=?1 WHERE session_id=?2",
                rusqlite::params![observed_at_ms, sid],
            )
            .map_err(|e| format!("sidecar touch: {e}"))?;
        Ok(())
    }

    /// Test-only: backdate `updated_at_ms` so rate-limit tests can force a
    /// refresh without sleeping 30 s. Never used in production paths.
    pub fn conn_execute_backdate(&self, session_id: &str, updated_at_ms: i64) -> bool {
        let Ok(sid) = normalize_session(session_id) else {
            return false;
        };
        self.conn
            .execute(
                "UPDATE session_changes SET updated_at_ms=?1 WHERE session_id=?2",
                rusqlite::params![updated_at_ms, sid],
            )
            .is_ok()
    }

    /// Mark a session unavailable (no linked repo). Idempotent.
    pub fn insert_unavailable(
        &self,
        session_id: &str,
        project_id: &str,
        reason: &str,
        observed_at_ms: i64,
        now_ms: i64,
    ) -> Result<bool, String> {
        let sid = normalize_session(session_id)?;
        let pid = normalize_project(project_id)?;
        if self.get(&sid)?.is_some() {
            return Ok(false);
        }
        self.conn
            .execute(
                "INSERT OR IGNORE INTO session_changes
                 (session_id, project_id, repo_path, repo_gitdir, repo_status,
                  baseline_commit, baseline_status, baseline_diff, baseline_complete,
                  latest_commit, latest_status, latest_diff, latest_complete,
                  commit_meta, commit_patch, commit_complete, capture_note,
                  has_baseline, reason, created_at_ms, updated_at_ms, observed_at_ms)
                 VALUES (?1,?2,'','','ok',NULL,'','',1,NULL,'','',1,'','',1,'',0,?3,?4,?4,?5)",
                rusqlite::params![sid, pid, reason, now_ms, observed_at_ms],
            )
            .map_err(|e| format!("sidecar insert unavailable: {e}"))?;
        Ok(true)
    }
}

fn normalize_session(id: &str) -> Result<String, String> {
    crate::desktop_session::normalize_hex_id(id.trim(), "session_id")
}

fn normalize_project(id: &str) -> Result<String, String> {
    crate::desktop_store::normalize_project_id(id.trim())
        .map_err(|e| format!("project id: {e}"))
}

use rusqlite::OptionalExtension;

// ---------------------------------------------------------------------------
// Failure backoff (per session, in-memory): repeated capture failures skip
// git for a cooldown instead of burning the aggregate budget every event.
// ---------------------------------------------------------------------------

static FAILURES: std::sync::LazyLock<Mutex<HashMap<String, (u32, i64)>>> =
    std::sync::LazyLock::new(|| Mutex::new(HashMap::new()));

fn wall_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

fn failure_backed_off(session_id: &str) -> bool {
    let now = wall_ms();
    FAILURES
        .lock()
        .ok()
        .and_then(|m| m.get(session_id).cloned())
        .is_some_and(|(count, last)| count >= FAIL_THRESHOLD && now - last < FAIL_COOLDOWN_MS)
}

fn record_failure(session_id: &str) {
    if let Ok(mut m) = FAILURES.lock() {
        let e = m.entry(session_id.to_string()).or_insert((0, 0));
        e.0 = e.0.saturating_add(1);
        e.1 = wall_ms();
    }
}

fn clear_failure(session_id: &str) {
    if let Ok(mut m) = FAILURES.lock() {
        m.remove(session_id);
    }
}

// ---------------------------------------------------------------------------
// Best-effort observation hook (called by the collector AFTER a successful
// activity append, never inside the activity transaction; never during the
// shutdown drain).
// ---------------------------------------------------------------------------

/// Observe one successfully persisted activity event for change capture.
///
/// - `session_id`: immutable desktop session assigned by the writer.
/// - `project_id`: `None` for unresolved sessions (skipped: no repo).
/// - `observed_at_ms`: the activity observation timestamp.
/// - `is_new_session`: true exactly when the writer created this session for
///   this event (baseline immediate). False means the session already
///   existed: a missing sidecar row becomes an upgrade marker, never a
///   fabricated baseline.
/// - `repo_override`: `Some(path)` in tests to bypass the registry; `None`
///   in production (registry `local_folder` authoritative).
///
/// Never returns an error to the caller: failures are one stderr line.
pub fn observe_session_change(
    session_id: &str,
    project_id: Option<&str>,
    observed_at_ms: i64,
    is_new_session: bool,
    repo_override: Option<&Path>,
) {
    if let Err(e) =
        observe_session_change_inner(session_id, project_id, observed_at_ms, is_new_session, repo_override)
    {
        eprintln!("qs-desktop-context: session-change capture skipped: {e}");
    }
}

fn observe_session_change_inner(
    session_id: &str,
    project_id: Option<&str>,
    observed_at_ms: i64,
    is_new_session: bool,
    repo_override: Option<&Path>,
) -> Result<(), String> {
    let Some(pid_raw) = project_id else {
        return Ok(()); // Unresolved sessions carry no repo.
    };
    let sid = normalize_session(session_id)?;
    let pid = normalize_project(pid_raw)?;
    let sidecar = sidecar_path().ok_or("no sidecar configured".to_string())?;
    // Resolve the linked folder BEFORE opening the sidecar (cheap registry
    // read; no git yet).
    let folder: Option<PathBuf> = match repo_override {
        Some(p) => Some(p.to_path_buf()),
        None => crate::project_context::folder_for_project_id(&pid),
    };
    let Some(folder) = folder else {
        // No linked repo: record unavailable once (best-effort), never fake.
        let store = SessionChangeStore::open(&sidecar)?;
        let now = crate::desktop_context::now_ms();
        if store.get(&sid)?.is_none() {
            let _ = store.insert_unavailable(
                &sid,
                &pid,
                "no linked repository for project (local_folder empty or unresolvable)",
                observed_at_ms,
                now,
            );
        }
        return Ok(());
    };
    let store = SessionChangeStore::open(&sidecar)?;
    let now = crate::desktop_context::now_ms();
    let existing = store.get(&sid)?;
    if let Some(row) = existing.as_ref() {
        if row.project_id.to_lowercase() != pid.to_lowercase() {
            return Err("session project mismatch; refusing to rewrite".to_string());
        }
        if !row.has_baseline && row.repo_path.is_empty() {
            return Ok(()); // Unavailable marker stays.
        }
        if row.repo_status == "mismatch" {
            return Ok(()); // Already frozen on a folder switch.
        }
        // Rate-limit git work; cheaply bump the observed timestamp.
        if now.saturating_sub(row.updated_at_ms) < MIN_UPDATE_INTERVAL_MS && !is_new_session {
            let _ = store.touch_observed(&sid, observed_at_ms);
            return Ok(());
        }
        // Back off after repeated failures instead of burning budget.
        if failure_backed_off(&sid) {
            let _ = store.note_refresh_failed(
                &sid,
                observed_at_ms,
                "refresh deferred after repeated failures; showing prior state",
            );
            return Ok(());
        }
    }
    // One aggregate deadline governs resolution, worktree, and commit
    // range together from here on.
    let mut budget = GitBudget::new(CAPTURE_BUDGET_TOTAL);
    let resolved = match resolve_repo(&folder, &mut budget) {
        Ok(r) => r,
        Err(e) => {
            // Unresolvable/refused repository: unavailable marker once, never
            // a fabricated empty diff.
            record_failure(&sid);
            let now = crate::desktop_context::now_ms();
            if existing.is_none() {
                let _ = store.insert_unavailable(
                    &sid,
                    &pid,
                    &format!("repository unavailable at session start ({e})"),
                    observed_at_ms,
                    now,
                );
            } else {
                let _ = store.note_refresh_failed(
                    &sid,
                    observed_at_ms,
                    &format!("refresh failed ({e}); showing prior state"),
                );
            }
            return Ok(());
        }
    };
    let repo_path = resolved.toplevel.to_string_lossy().to_string();
    let repo_gitdir = resolved.gitdir.to_string_lossy().to_string();
    let now = crate::desktop_context::now_ms();
    match existing {
        None if is_new_session => {
            // Immediate baseline at session creation observation. An
            // incomplete baseline is still recorded honestly (partial,
            // no prior to preserve) and counts toward backoff.
            match capture_worktree(&resolved, &mut budget) {
                Some(state) => {
                    if state.complete {
                        clear_failure(&sid);
                    } else {
                        record_failure(&sid);
                    }
                    let _ = store.insert_baseline(
                        &sid,
                        &pid,
                        &repo_path,
                        &repo_gitdir,
                        observed_at_ms,
                        &state,
                        now,
                    );
                }
                None => {
                    record_failure(&sid);
                    let _ = store.insert_unavailable(
                        &sid,
                        &pid,
                        "repository state unavailable at session start (refused or git failed)",
                        observed_at_ms,
                        now,
                    );
                }
            }
            Ok(())
        }
        None => {
            // Pre-existing session first seen after upgrade: missing baseline.
            match capture_worktree(&resolved, &mut budget) {
                Some(state) => {
                    if state.complete {
                        clear_failure(&sid);
                    } else {
                        record_failure(&sid);
                    }
                    let _ = store.insert_missing_baseline(
                        &sid,
                        &pid,
                        &repo_path,
                        &repo_gitdir,
                        observed_at_ms,
                        Some(&state),
                        now,
                    );
                }
                None => {
                    record_failure(&sid);
                    let _ = store.insert_missing_baseline(
                        &sid,
                        &pid,
                        &repo_path,
                        &repo_gitdir,
                        observed_at_ms,
                        None,
                        now,
                    );
                }
            }
            Ok(())
        }
        Some(row) => {
            // Frozen sessions never refresh: only the currently assigned
            // session reaches this hook, so any row update here is for the
            // live session by construction. (Project/unavailable/mismatch/
            // rate/backoff fast checks already ran git-free above; only the
            // repository binding below needs the resolved identity.)
            // Repository binding: a switched linked folder must never be
            // compared against this row's baseline. Legacy rows (empty
            // git dir from v1) backfill when the toplevel still matches.
            if row.repo_path != repo_path {
                let _ = store.mark_repo_mismatch(
                    &sid,
                    observed_at_ms,
                    "linked folder changed repositories; row frozen, never compared across repos",
                );
                return Ok(());
            }
            if row.repo_gitdir.is_empty() {
                let _ = store.backfill_gitdir(&sid, &repo_gitdir);
            } else if row.repo_gitdir != repo_gitdir {
                let _ = store.mark_repo_mismatch(
                    &sid,
                    observed_at_ms,
                    "repository identity changed; row frozen, never compared across repos",
                );
                return Ok(());
            }
            match capture_worktree(&resolved, &mut budget) {
                // Coherent snapshot rule: ONLY a fully complete worktree
                // snapshot may advance the row. Anything partial retains
                // the ENTIRE prior snapshot (HEAD, diffs, range, capture
                // timestamp); only the failure note, observed time, and
                // backoff state move.
                Some(state) if state.complete => {
                    // Frozen commit range for this refresh: only when a real
                    // baseline commit and a different HEAD exist. Shares the
                    // aggregate budget (no fresh deadline per phase).
                    let commit: Option<CommitRange> =
                        match (&row.baseline_commit, &state.commit) {
                            (Some(base), Some(head)) if base != head => {
                                Some(capture_commit_range_in(
                                    &resolved, &mut budget, base, head,
                                ))
                            }
                            // No range needed (HEAD unchanged or unborn):
                            // vacuous, distinct from a failed refresh.
                            _ => None,
                        };
                    match commit {
                        // Explicit range failure with prior data: retain
                        // everything and record the failure for backoff.
                        // Never conflated with no-range-needed above.
                        Some(c)
                            if !range_refresh_advances(&row.commit_patch, &c) =>
                        {
                            record_failure(&sid);
                            let _ = store.note_refresh_failed(
                                &sid,
                                observed_at_ms,
                                &format!(
                                    "commit range refresh failed ({}); showing prior snapshot at {}",
                                    if c.note.is_empty() {
                                        "range unavailable".to_string()
                                    } else {
                                        c.note.clone()
                                    },
                                    row.latest_commit.as_deref().unwrap_or("unknown"),
                                ),
                            );
                        }
                        _ => {
                            let commit_ok =
                                commit.as_ref().map(|c| c.complete).unwrap_or(true);
                            if commit_ok {
                                clear_failure(&sid);
                            } else {
                                // First-ever range, honestly incomplete:
                                // advance with complete=false marked.
                                record_failure(&sid);
                            }
                            let mut note = state.note.clone();
                            if let Some(c) = commit.as_ref() {
                                if !c.note.is_empty() {
                                    if !note.is_empty() {
                                        note.push_str("; ");
                                    }
                                    note.push_str(&c.note);
                                }
                            }
                            let _ = store.update_latest(
                                &sid,
                                observed_at_ms,
                                &state,
                                commit.as_ref(),
                                &note,
                                now,
                            );
                        }
                    }
                }
                Some(state) => {
                    // Incomplete worktree: retain the entire prior snapshot.
                    record_failure(&sid);
                    let _ = store.note_refresh_failed(
                        &sid,
                        observed_at_ms,
                        &format!(
                            "worktree refresh incomplete ({}); showing prior snapshot at {}",
                            if state.note.is_empty() {
                                "partial capture".to_string()
                            } else {
                                state.note.clone()
                            },
                            row.latest_commit.as_deref().unwrap_or("unknown"),
                        ),
                    );
                }
                None => {
                    record_failure(&sid);
                    let _ = store.note_refresh_failed(
                        &sid,
                        observed_at_ms,
                        "refresh failed (refused or git failed); showing prior state",
                    );
                }
            }
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn unique_path(tag: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "qs-changes-{}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0),
            tag
        ))
    }

    fn git(cmd: &[&str], cwd: &Path) {
        let st = std::process::Command::new("git")
            .args(cmd)
            .current_dir(cwd)
            .env("LC_ALL", "C")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_AUTHOR_NAME", "t")
            .env("GIT_AUTHOR_EMAIL", "t@t")
            .env("GIT_COMMITTER_NAME", "t")
            .env("GIT_COMMITTER_EMAIL", "t@t")
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status()
            .expect("spawn git");
        assert!(st.success(), "git {cmd:?} failed");
    }

    fn init_repo(tag: &str) -> PathBuf {
        let dir = unique_path(tag);
        std::fs::create_dir_all(&dir).unwrap();
        git(&["init", "-q"], &dir);
        git(&["config", "user.name", "t"], &dir);
        git(&["config", "user.email", "t@t"], &dir);
        std::fs::write(dir.join("note.md"), "one\n").unwrap();
        git(&["add", "note.md"], &dir);
        git(&["commit", "-qm", "init"], &dir);
        dir
    }

    #[test]
    fn sidecar_path_is_sibling() {
        let db = PathBuf::from("/tmp/x/activity.db");
        assert_eq!(
            sidecar_path_for(&db),
            PathBuf::from("/tmp/x/activity.db.changes.db")
        );
    }

    #[test]
    fn exec_config_keys_refused() {
        for key in [
            "core.fsmonitor",
            "core.fsmOnitorHook",
            "core.pager",
            "core.hookspath",
            "sequence.editor",
            "diff.external",
            "diff.foo.command",
            "diff.safe.driver.textconv",
            "filter.evil.clean",
            "filter.a.b.process",
            "merge.bin.driver",
            "credential.helper",
            "credential.https://example.com.helper",
            "include.path",
            "includeif.gitdir:/x.path",
            "extensions.worktreeconfig",
        ] {
            assert!(exec_config_key(key), "should refuse {key}");
        }
        for key in [
            "core.repositoryformatversion",
            "core.bare",
            "user.name",
            "diff.algorithm",
            "filter.lfs.required",
            "credential.usehttppath",
            "includeif.onbranch",
        ] {
            assert!(!exec_config_key(key), "should allow {key}");
        }
        // submodule.update + protocol.allow are handled separately; the
        // helper must not treat them as plain exec keys here.
        assert!(!exec_config_key("submodule.foo.update"));
        assert!(!exec_config_key("protocol.allow"));
    }

    #[test]
    fn policy_mirrors_project_files() {
        let policy = PathPolicy::for_repo(Path::new("/repo"));
        // Sensitive names (tracked or not, any depth).
        for p in [
            ".env",
            "a/.env",
            ".ENV.local",
            "x/credentials",
            "secrets/keys.txt",
            "a/b/.credentials.json",
            "id_stuff/.netrc",
        ] {
            assert!(policy.is_excluded(p), "should exclude {p}");
        }
        // Sensitive/special dirs.
        for p in [".git/config", "a/.ssh/id_rsa", ".aws/creds", ".gnupg/x"] {
            assert!(policy.is_excluded(p), "should exclude {p}");
        }
        // .pi protected subtrees (every occurrence counts).
        for p in [
            ".pi/auth/x",
            "a/.pi/settings.json",
            ".pi/extensions/pkg/.pi/public.txt",
        ] {
            assert!(policy.is_excluded(p), "should exclude {p}");
        }
        // Trusted helper names under any scripts/ dir.
        for p in [
            "scripts/project_files.py",
            "sub/scripts/zotero.py",
            "scripts/project_session_changes.py",
        ] {
            assert!(policy.is_excluded(p), "should exclude {p}");
        }
        assert!(policy.is_excluded("ScopedAgent.qml"));
        assert!(policy.is_excluded("a/ScopedAgent.qml"));
        // Folder locks.
        assert!(policy.is_excluded(".project-folder.lock"));
        assert!(policy.is_excluded("a/.project-folder-9.tmp"));
        // Ordinary work files stay allowed.
        for p in ["note.md", "src/main.rs", "docs/guide.md", "scripts/run.sh"] {
            assert!(!policy.is_excluded(p), "should allow {p}");
        }
        // Unsafe spellings never reach git.
        for p in ["", ".", "../x", "/abs", "a//b", "a/./b"] {
            assert!(policy.is_excluded(p), "should exclude unsafe {p:?}");
        }
    }

    #[test]
    fn split_names_scans_all_for_forbidden() {
        let policy = PathPolicy::for_repo(Path::new("/repo"));
        // Cap of 1: the allowed cap must not hide the forbidden descendant
        // at the end (file `bundle` replacing directory `bundle/` holding
        // a secret).
        let (allowed, forbidden, capped) = split_names(
            vec![
                "notes.md".to_string(),
                "extra.md".to_string(),
                "bundle/.env".to_string(),
            ],
            &policy,
            1,
        );
        assert_eq!(allowed, vec!["notes.md".to_string()]);
        assert!(
            forbidden.contains(&"bundle/.env".to_string()),
            "forbidden: {forbidden:?}"
        );
        assert!(capped);
        // Clean input: nothing capped, nothing forbidden.
        let (allowed, forbidden, capped) = split_names(
            vec!["a.md".to_string(), "b.md".to_string()],
            &policy,
            500,
        );
        assert_eq!(allowed.len(), 2);
        assert!(forbidden.is_empty());
        assert!(!capped);
    }

    #[test]
    fn range_refresh_advances_only_on_complete_content_or_first() {
        let fresh = |complete: bool, patch: &str| CommitRange {
            meta: String::new(),
            count: 0,
            patch: patch.to_string(),
            complete,
            note: String::new(),
        };
        // Failed/empty refresh with prior data: preserve (explicit
        // failure, never conflated with no-range-needed).
        assert!(!range_refresh_advances("old-patch", &fresh(false, "")));
        // Complete, fresh content, or first-ever range: advance.
        assert!(range_refresh_advances("old-patch", &fresh(true, "")));
        assert!(range_refresh_advances("old-patch", &fresh(false, "p")));
        assert!(range_refresh_advances("", &fresh(false, "")));
    }

    #[test]
    fn expired_budget_fails_fast_and_honest() {
        let repo = init_repo("budget");
        // Resolve with a live budget, then capture with an expired one: the
        // shared deadline must fail the whole phase immediately.
        let mut live = GitBudget::new(Duration::from_secs(5));
        let resolved = resolve_repo(&repo, &mut live).expect("resolve");
        let mut dead = GitBudget::new(Duration::from_millis(0));
        std::thread::sleep(Duration::from_millis(5));
        let range = capture_commit_range_in(&resolved, &mut dead, &"ab".repeat(20), &"cd".repeat(20));
        assert!(!range.complete, "expired budget must not succeed");
        let _ = std::fs::remove_dir_all(&repo);
    }

    #[test]
    fn baseline_and_latest_roundtrip() {
        let repo = init_repo("roundtrip");
        let state = capture_repo_state(&repo).expect("capture");
        assert!(state.commit.is_some());
        assert!(state.complete);
        let store = SessionChangeStore::open_in_memory().unwrap();
        let sid = "a".repeat(32);
        let pid = "11111111-1111-1111-1111-111111111111";
        assert!(store
            .insert_baseline(&sid, pid, &repo.to_string_lossy(), "gitdir", 1000, &state, 1001)
            .unwrap());
        // Second insert is a no-op (baseline immutable).
        assert!(!store
            .insert_baseline(&sid, pid, &repo.to_string_lossy(), "gitdir", 2000, &state, 2001)
            .unwrap());
        let row = store.get(&sid).unwrap().expect("row");
        assert!(row.has_baseline);
        assert_eq!(row.baseline_commit, row.latest_commit);
        assert_eq!(row.repo_status, "ok");
        let _ = std::fs::remove_dir_all(&repo);
    }

    #[test]
    fn missing_baseline_never_backfills() {
        let store = SessionChangeStore::open_in_memory().unwrap();
        let sid = "b".repeat(32);
        let pid = "11111111-1111-1111-1111-111111111111";
        assert!(store
            .insert_missing_baseline(&sid, pid, "/repo", "gitdir", 1000, None, 1001)
            .unwrap());
        let row = store.get(&sid).unwrap().expect("row");
        assert!(!row.has_baseline);
        assert!(row.baseline_commit.is_none());
        assert!(row.reason.contains("before change capture"));
    }

    #[test]
    fn clean_commit_range_captured() {
        // Clean baseline, one commit during the session, clean latest:
        // the frozen commit range must carry actual changes, not hashes.
        let repo = init_repo("range");
        let base = capture_repo_state(&repo).expect("baseline");
        assert!(base.diff.is_empty());
        std::fs::write(repo.join("note.md"), "one\nsession work\n").unwrap();
        git(&["add", "note.md"], &repo);
        git(&["commit", "-qm", "session work"], &repo);
        let latest = capture_repo_state(&repo).expect("latest");
        assert!(latest.diff.is_empty());
        let (Some(b), Some(h)) = (base.commit.clone(), latest.commit.clone()) else {
            panic!("need commits");
        };
        assert_ne!(b, h);
        let range = capture_commit_range(&repo, &b, &h);
        assert!(range.complete, "note: {}", range.note);
        assert_eq!(range.count, 1);
        assert!(range.patch.contains("session work"), "patch: {}", range.patch);
        assert!(range.meta.contains("session work"), "meta: {}", range.meta);
        let _ = std::fs::remove_dir_all(&repo);
    }

    #[test]
    fn preexisting_edit_committed_in_both() {
        // Baseline-dirty edit later committed: it appears in BOTH the
        // baseline worktree patch and the commit-range patch (attribution
        // limits are stated in evidence, not hidden here).
        let repo = init_repo("both");
        std::fs::write(repo.join("note.md"), "one\ndirty-before\n").unwrap();
        let base = capture_repo_state(&repo).expect("baseline");
        assert!(base.diff.contains("dirty-before"));
        git(&["add", "note.md"], &repo);
        git(&["commit", "-qm", "commit dirty"], &repo);
        let latest = capture_repo_state(&repo).expect("latest");
        let (Some(b), Some(h)) = (base.commit.clone(), latest.commit.clone()) else {
            panic!("need commits");
        };
        let range = capture_commit_range(&repo, &b, &h);
        assert!(range.complete, "note: {}", range.note);
        assert!(range.patch.contains("dirty-before"), "patch: {}", range.patch);
        let _ = std::fs::remove_dir_all(&repo);
    }

    #[test]
    fn tracked_protected_edits_withheld() {
        // Tracked protected files edited: content never reaches the diff,
        // while ordinary edits in the same capture do.
        let repo = init_repo("prot");
        std::fs::create_dir_all(repo.join("sub/scripts")).unwrap();
        std::fs::write(repo.join("sub/scripts/zotero.py"), "helper\n").unwrap();
        std::fs::write(repo.join("bundle.env"), "public\n").unwrap();
        std::fs::create_dir_all(repo.join(".pi/auth")).unwrap();
        std::fs::write(repo.join(".pi/auth/keys.txt"), "sekrit\n").unwrap();
        std::fs::write(repo.join("notes.md"), "hello\n").unwrap();
        git(&["add", "."], &repo);
        git(&["commit", "-qm", "add files"], &repo);
        std::fs::write(repo.join("sub/scripts/zotero.py"), "helper changed TOKEN=abc\n").unwrap();
        std::fs::write(repo.join("bundle.env"), "still public\n").unwrap();
        std::fs::write(repo.join(".pi/auth/keys.txt"), "sekrit2\n").unwrap();
        std::fs::write(repo.join("notes.md"), "hello world\n").unwrap();
        let state = capture_repo_state(&repo).expect("capture");
        assert!(state.complete, "note: {}", state.note);
        // Ordinary content present…
        assert!(state.diff.contains("hello world"), "diff: {}", state.diff);
        // …protected content absent everywhere (status + diff).
        assert!(!state.diff.contains("TOKEN=abc"), "diff: {}", state.diff);
        assert!(!state.diff.contains("sekrit"), "diff: {}", state.diff);
        assert!(!state.status.contains("zotero.py"), "status: {}", state.status);
        assert!(!state.status.contains("keys.txt"), "status: {}", state.status);
        // bundle.env is NOT sensitive (only exact .env / .env.* are).
        assert!(state.status.contains("bundle.env"), "status: {}", state.status);
        let _ = std::fs::remove_dir_all(&repo);
    }

    #[test]
    fn hostile_config_never_executes() {
        // Hostile monitors/filters/pagers configured locally: capture must
        // refuse fast without running anything.
        let repo = init_repo("hostile");
        let marker = repo.join("PWNED");
        let hook = "#!/bin/sh\ntouch \"$1\"\n";
        let _ = (marker.clone(), hook.clone());
        git(&["config", "core.fsmonitor", "touch PWNED-fsmonitor"], &repo);
        git(&["config", "filter.evil.clean", "touch PWNED-clean"], &repo);
        git(&["config", "filter.evil.smudge", "touch PWNED-smudge"], &repo);
        git(&["config", "filter.evil.process", "touch PWNED-process"], &repo);
        git(&["config", "diff.evil.command", "touch PWNED-diff"], &repo);
        git(&["config", "core.pager", "touch PWNED-pager"], &repo);
        let start = Instant::now();
        let state = capture_repo_state(&repo);
        let elapsed = start.elapsed();
        assert!(elapsed < Duration::from_secs(10), "took {elapsed:?}");
        // Refused honestly: no snapshot, no execution.
        assert!(state.is_none());
        for name in [
            "PWNED-fsmonitor",
            "PWNED-clean",
            "PWNED-smudge",
            "PWNED-process",
            "PWNED-diff",
            "PWNED-pager",
        ] {
            assert!(
                !repo.join(name).exists(),
                "{name} was executed!"
            );
        }
        assert!(!marker.exists());
        let _ = std::fs::remove_dir_all(&repo);
    }

    #[test]
    fn capture_never_touches_index() {
        let repo = init_repo("index");
        let before = std::process::Command::new("git")
            .args(["status", "--porcelain=v1"])
            .current_dir(&repo)
            .output()
            .unwrap();
        let _ = capture_repo_state(&repo).expect("capture");
        let after = std::process::Command::new("git")
            .args(["status", "--porcelain=v1"])
            .current_dir(&repo)
            .output()
            .unwrap();
        assert_eq!(before.stdout, after.stdout);
        // No commits created by capture.
        let count = std::process::Command::new("git")
            .args(["rev-list", "--count", "HEAD"])
            .current_dir(&repo)
            .output()
            .unwrap();
        assert_eq!(String::from_utf8_lossy(&count.stdout).trim(), "1");
        let _ = std::fs::remove_dir_all(&repo);
    }

    #[test]
    fn unborn_repo_withholds_diff_but_keeps_status() {
        // No HEAD yet: status enumerates (untracked allowed files shown)
        // but name enumeration against HEAD fails, so content is withheld
        // rather than diffed without proven exclusions.
        let dir = unique_path("unborn");
        std::fs::create_dir_all(&dir).unwrap();
        git(&["init", "-q"], &dir);
        git(&["config", "user.name", "t"], &dir);
        git(&["config", "user.email", "t@t"], &dir);
        std::fs::write(dir.join("notes.md"), "hello\n").unwrap();
        let state = capture_repo_state(&dir).expect("status snapshot");
        assert!(state.commit.is_none());
        assert!(state.status.contains("notes.md"), "status: {}", state.status);
        assert!(state.diff.is_empty(), "withheld, got: {}", state.diff);
        assert!(!state.complete);
        assert!(state.note.contains("withheld"), "note: {}", state.note);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn non_repo_returns_none() {
        let dir = unique_path("nonrepo");
        std::fs::create_dir_all(&dir).unwrap();
        assert!(capture_repo_state(&dir).is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn v1_sidecar_migrates_with_data() {
        let dir = unique_path("migrate");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("activity.db.changes.db");
        // Hand-built v1 schema with one row.
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                "CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
                 CREATE TABLE session_changes (
                   session_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                   repo_path TEXT NOT NULL, baseline_commit TEXT,
                   baseline_status TEXT NOT NULL DEFAULT '',
                   baseline_diff TEXT NOT NULL DEFAULT '',
                   latest_commit TEXT, latest_status TEXT NOT NULL DEFAULT '',
                   latest_diff TEXT NOT NULL DEFAULT '',
                   has_baseline INTEGER NOT NULL DEFAULT 1,
                   reason TEXT NOT NULL DEFAULT '',
                   created_at_ms INTEGER NOT NULL,
                   updated_at_ms INTEGER NOT NULL,
                   observed_at_ms INTEGER NOT NULL);",
            )
            .unwrap();
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (1)",
                [],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO session_changes VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14)",
                rusqlite::params![
                    "d".repeat(32),
                    "11111111-1111-1111-1111-111111111111",
                    "/repo",
                    "ab".repeat(20),
                    "M x",
                    "diff-x",
                    "ab".repeat(20),
                    "M x",
                    "diff-x",
                    1,
                    "",
                    1000,
                    1001,
                    1001,
                ],
            )
            .unwrap();
        }
        // Work around 0600 validation: rusqlite creates 0644; lock it down.
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        }
        // Work around 0600 validation: file created by us is already 0600.
        let store = SessionChangeStore::open(&path).unwrap();
        let row = store.get(&"d".repeat(32)).unwrap().expect("row");
        assert_eq!(row.baseline_diff, "diff-x");
        assert_eq!(row.repo_gitdir, "");
        assert_eq!(row.repo_status, "ok");
        // v1 snapshots lack completeness provenance: all three mark
        // incomplete with the historical limitation; data frozen as stored.
        assert!(!row.baseline_complete && !row.latest_complete && !row.commit_complete);
        assert!(row.commit_patch.is_empty());
        assert!(row.capture_note.contains("migrated from sidecar v1"), "note: {}", row.capture_note);
        let v: i64 = store
            .conn
            .query_row("SELECT version FROM schema_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, SIDECAR_SCHEMA_VERSION);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
