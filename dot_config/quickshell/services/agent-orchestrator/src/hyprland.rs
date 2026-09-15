//! Hyprland adapter: socket discovery, event parsing, snapshot queries.
//!
//! Sockets (no `hyprctl` subprocess):
//! - events: `.socket2.sock`, lines `EVENT>>DATA\n`.
//! - requests: `.socket.sock`, one-shot `j/activewindow` / `j/activeworkspace`
//!   queries; each connection is opened, used once, and closed immediately
//!   (Hyprland stalls on unclosed request connections).
//!
//! Path discovery supports the modern layout
//! `$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/` and the legacy
//! `/tmp/hypr/$HYPRLAND_INSTANCE_SIGNATURE/` fallback.
//!
//! Event handling rules:
//! - subscribe (connect `.socket2.sock`) BEFORE the initial authoritative
//!   snapshot so nothing is missed;
//! - only whitelisted meaningful events trigger a bounded snapshot
//!   reconciliation (never continuous polling); paired events
//!   (`activewindow`+`activewindowv2`) dedup via semantic equality;
//! - malformed lines never corrupt context (ignored);
//! - commas in titles preserved (payloads split on the FIRST commas only).

use crate::desktop_context::{now_ms, DesktopContext, FocusedWindow, Source, Workspace};
use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

/// Max bytes for one event line (larger lines are discarded to newline).
pub const MAX_EVENT_LINE_BYTES: usize = 64 * 1024;
/// Max bytes for one request response.
pub const MAX_RESPONSE_BYTES: usize = 1024 * 1024;
/// Per-read/write socket timeout (upper bound; each op is further capped by
/// the remaining total budget in [`query_socket`]).
pub const SOCKET_RW_TIMEOUT: Duration = Duration::from_secs(2);
/// Upper bound for a blocking connect. Used directly by
/// [`connect_bounded`] and as the cap for the connect phase inside
/// [`query_socket`] (which is additionally capped by the remaining total
/// budget). Collector should use [`connect_bounded`] for its event socket
/// too so connects never block outside deadlines.
pub const CONNECT_TIMEOUT: Duration = Duration::from_secs(2);
/// Total deadline for one request/response (trickle cannot hang forever;
/// must stay under Hyprland's ~5s stall timeout). Covers connect + write +
/// read; every blocking wait is capped by the remaining budget so the actual
/// total stays bounded (no trickle stalls).
pub const TOTAL_RESPONSE_DEADLINE: Duration = Duration::from_secs(4);
/// Idle event-read timeout: shutdown responsiveness only, not desktop polling.
pub const EVENT_IDLE_TIMEOUT: Duration = Duration::from_secs(1);
/// Reader chunk size.
pub const READ_CHUNK: usize = 8192;
/// Bounded stability retries for [`fetch_snapshot_at`]. Each attempt is
/// 4 fast queries when the compositor is live; query-level timeouts fail
/// fast (no retry on I/O) so the worst case stays bounded.
pub const SNAPSHOT_MAX_ATTEMPTS: usize = 3;
/// Liveness probe timeout for socket discovery (short: only distinguishes
/// a listening socket from a stale socket file).
pub const DISCOVERY_PROBE_TIMEOUT: Duration = Duration::from_millis(200);

#[derive(Debug)]
pub enum HyprError {
    NoSockets(String),
    Io(String),
    Timeout(String),
    TooLarge(String),
    BadData(String),
}

impl std::fmt::Display for HyprError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            HyprError::NoSockets(m) => write!(f, "no hyprland sockets: {m}"),
            HyprError::Io(m) => write!(f, "hyprland I/O: {m}"),
            HyprError::Timeout(m) => write!(f, "hyprland timeout: {m}"),
            HyprError::TooLarge(m) => write!(f, "hyprland oversize: {m}"),
            HyprError::BadData(m) => write!(f, "hyprland bad data: {m}"),
        }
    }
}

impl std::error::Error for HyprError {}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SocketPaths {
    pub event_socket: PathBuf,
    pub request_socket: PathBuf,
}

/// Discover sockets from the process environment.
pub fn discover_sockets() -> Option<SocketPaths> {
    let runtime = std::env::var_os("XDG_RUNTIME_DIR").map(PathBuf::from);
    let sig = std::env::var("HYPRLAND_INSTANCE_SIGNATURE").ok();
    discover_sockets_with(runtime.as_deref(), sig.as_deref(), Path::new("/tmp/hypr"))
}

/// Testable discovery with explicit bases.
///
/// Safety rules (no silent foreign/stale choice):
/// - explicit signatures must be safe single path segments, and both
///   endpoints must be owned non-symlink Unix sockets of the current euid;
/// - without a signature, only owned socket endpoints are eligible; stale
///   entries are skipped in favour of live ones, and multiple *live*
///   instances resolve to `None` (caller must supply an explicit
///   signature) instead of silently picking the lexicographic first.
/// Modern (`$XDG_RUNTIME_DIR/hypr/`) is still preferred over legacy
/// (`/tmp/hypr/`) for explicit signatures; for implicit discovery all
/// healthy candidates across both bases are considered for liveness.
pub fn discover_sockets_with(
    runtime_dir: Option<&Path>,
    signature: Option<&str>,
    tmp_base: &Path,
) -> Option<SocketPaths> {
    // 1. Explicit signature: modern runtime dir first, legacy /tmp second.
    if let Some(sig) = signature.filter(|s| !s.is_empty()) {
        if !is_safe_signature(sig) {
            return None;
        }
        if let Some(rt) = runtime_dir {
            let dir = rt.join("hypr").join(sig);
            if let Some(p) = both_present_healthy(&dir) {
                return Some(p);
            }
        }
        let dir = tmp_base.join(sig);
        if let Some(p) = both_present_healthy(&dir) {
            return Some(p);
        }
        return None;
    }
    // 2. No signature: gather healthy candidates across modern + legacy,
    //    then disambiguate by liveness. Never return a stale first entry
    //    while a live second exists, and never silently pick among several
    //    live instances.
    let mut healthy: Vec<SocketPaths> = Vec::new();
    if let Some(rt) = runtime_dir {
        healthy.extend(collect_healthy(&rt.join("hypr")));
    }
    healthy.extend(collect_healthy(tmp_base));
    if healthy.is_empty() {
        return None;
    }
    if healthy.len() == 1 {
        let p = &healthy[0];
        return is_live_socket(&p.request_socket).then(|| p.clone());
    }
    let live: Vec<SocketPaths> = healthy
        .into_iter()
        .filter(|p| is_live_socket(&p.request_socket))
        .collect();
    if live.len() == 1 {
        // Stale-first / live-second: the single live instance wins
        // regardless of lexicographic order.
        return Some(live.into_iter().next().expect("single live"));
    }
    // 0 live (all stale) or >1 live (ambiguous): require explicit signature.
    None
}

/// Safe signature = single non-empty path segment without separators,
/// NUL bytes, or parent/current markers. Hyprland instance signatures are
/// hex-like; accept `[A-Za-z0-9_.-]` up to 128 chars to block traversal
/// (`../x`), absolute paths, and control garbage while staying compatible.
fn is_safe_signature(sig: &str) -> bool {
    if sig.is_empty() || sig.len() > 128 {
        return false;
    }
    if sig == "." || sig == ".." {
        return false;
    }
    if sig.contains('/') || sig.contains('\\') || sig.contains('\0') {
        return false;
    }
    if Path::new(sig).components().count() != 1 {
        return false;
    }
    sig.chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-' || c == '.')
}

/// Both endpoints present AND valid: owned non-symlink Unix sockets of the
/// current euid. Regular files, foreign-owned sockets, and symlinks (which
/// could point at foreign endpoints) are rejected.
fn both_present_healthy(dir: &Path) -> Option<SocketPaths> {
    let event = dir.join(".socket2.sock");
    let req = dir.join(".socket.sock");
    if owned_socket(&event) && owned_socket(&req) {
        Some(SocketPaths {
            event_socket: event,
            request_socket: req,
        })
    } else {
        None
    }
}

fn owned_socket(path: &Path) -> bool {
    // Reject symlinks outright (avoid foreign endpoints via link).
    if let Ok(meta) = std::fs::symlink_metadata(path) {
        if meta.file_type().is_symlink() {
            return false;
        }
    } else {
        return false;
    }
    let md = match std::fs::metadata(path) {
        Ok(m) => m,
        Err(_) => return false,
    };
    use std::os::unix::fs::{FileTypeExt, MetadataExt};
    if !md.file_type().is_socket() {
        return false;
    }
    let uid = md.uid();
    // SAFETY: geteuid has no failure mode.
    let euid = unsafe { libc::geteuid() };
    uid == euid
}

fn collect_healthy(base: &Path) -> Vec<SocketPaths> {
    let entries = match std::fs::read_dir(base) {
        Ok(e) => e,
        Err(_) => return Vec::new(),
    };
    let mut dirs: Vec<PathBuf> = Vec::new();
    for e in entries.flatten() {
        let p = e.path();
        // Only directories with safe signature names participate.
        let name_ok = p
            .file_name()
            .and_then(|n| n.to_str())
            .map(is_safe_signature)
            .unwrap_or(false);
        if !name_ok {
            continue;
        }
        // Followed dir check (is_dir follows symlinks); combined with the
        // symlink rejection on the sockets themselves this is sufficient.
        if p.is_dir() {
            dirs.push(p);
        }
    }
    dirs.sort();
    let mut out = Vec::new();
    for dir in dirs {
        if let Some(p) = both_present_healthy(&dir) {
            out.push(p);
        }
    }
    out
}

fn is_live_socket(request_socket: &Path) -> bool {
    // Short bounded probe: a listening socket accepts (queues) the connect
    // even without an acceptor; a stale socket file fails fast. The probe
    // connection is dropped immediately.
    connect_bounded(request_socket, DISCOVERY_PROBE_TIMEOUT).is_ok()
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------

/// One parsed event line. `payload` is kept verbatim (commas preserved).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HyprEvent {
    pub name: String,
    pub payload: String,
}

/// Parse `EVENT>>DATA`. Returns `None` for malformed lines (missing `>>` or
/// empty event name). Payload may be empty (valid for some events).
pub fn parse_event_line(line: &str) -> Option<HyprEvent> {
    let line = line.strip_suffix('\r').unwrap_or(line);
    let (name, payload) = line.split_once(">>")?;
    if name.is_empty() {
        return None;
    }
    // Event names are simple tokens; reject control garbage without
    // rejecting future unknown-but-well-formed names.
    if !name
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
    {
        return None;
    }
    if name.len() > 128 || payload.len() > MAX_EVENT_LINE_BYTES {
        return None;
    }
    Some(HyprEvent {
        name: name.to_string(),
        payload: payload.to_string(),
    })
}

/// True for events that can change focus/title/workspace and therefore
/// trigger a bounded snapshot reconciliation. Unknown or purely cosmetic
/// events (layers, config reload, fullscreen pings) return false.
pub fn is_meaningful_event(name: &str) -> bool {
    matches!(
        name,
        "activewindow"
            | "activewindowv2"
            | "windowtitle"
            | "windowtitlev2"
            | "workspace"
            | "workspacev2"
            | "focusedmon"
            | "focusedmonv2"
            | "openwindow"
            | "closewindow"
            | "movewindow"
            | "movewindowv2"
            | "activespecial"
            | "activespecialv2"
            | "moveworkspace"
            | "moveworkspacev2"
            | "createworkspace"
            | "createworkspacev2"
            | "destroyworkspace"
            | "destroyworkspacev2"
            | "renameworkspace"
    )
}

/// Split payload on the FIRST comma: `(head, rest)`. Preserves commas in
/// titles (`activewindow` = `CLASS,TITLE...`).
pub fn split_first_comma(payload: &str) -> (String, String) {
    match payload.find(',') {
        Some(i) => (payload[..i].to_string(), payload[i + 1..].to_string()),
        None => (payload.to_string(), String::new()),
    }
}

/// Split `openwindow` payload `ADDRESS,WORKSPACE,CLASS,TITLE...` into its
/// four parts, preserving commas inside the title.
pub fn split_openwindow(payload: &str) -> Option<(String, String, String, String)> {
    let mut parts: Vec<String> = Vec::new();
    let mut rest = payload;
    for _ in 0..3 {
        match rest.find(',') {
            Some(i) => {
                parts.push(rest[..i].to_string());
                rest = &rest[i + 1..];
            }
            None => return None,
        }
    }
    parts.push(rest.to_string());
    if parts.len() != 4 {
        return None;
    }
    Some((
        parts[0].clone(),
        parts[1].clone(),
        parts[2].clone(),
        parts[3].clone(),
    ))
}

// ---------------------------------------------------------------------------
// Snapshot queries
// ---------------------------------------------------------------------------

/// Bounded connect to a Unix socket: nonblocking `connect(2)` + `poll(2)`
/// with `timeout`, so a stalled listener/backlog can never block outside
/// the deadline.
///
/// Public so other workers (notably the collector's event-socket connect)
/// reuse the same bound. Returns a blocking `UnixStream` on success.
///
/// **Collector integration:** the collector calls
/// `connect_bounded(&config.event_socket, CONNECT_TIMEOUT)` (or the
/// remaining session budget). No other signature change is needed.
pub fn connect_bounded(socket: &Path, timeout: Duration) -> Result<UnixStream, HyprError> {
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::io::FromRawFd;

    let bytes = socket.as_os_str().as_bytes();
    if bytes.is_empty() || bytes.contains(&0) {
        return Err(HyprError::BadData("socket path out of bounds".to_string()));
    }
    // sockaddr_un sun_path capacity (108 incl. NUL on Linux).
    if bytes.len() >= 108 {
        return Err(HyprError::BadData("socket path too long".to_string()));
    }
    let fd = unsafe { libc::socket(libc::AF_UNIX, libc::SOCK_STREAM, 0) };
    if fd < 0 {
        return Err(HyprError::Io(format!(
            "socket(): {}",
            std::io::Error::last_os_error()
        )));
    }
    struct FdGuard(std::os::unix::io::RawFd);
    impl Drop for FdGuard {
        fn drop(&mut self) {
            unsafe { libc::close(self.0) };
        }
    }
    let guard = FdGuard(fd);
    // Nonblocking for the connect phase.
    let orig_flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if orig_flags < 0 {
        return Err(HyprError::Io(format!(
            "fcntl GETFL: {}",
            std::io::Error::last_os_error()
        )));
    }
    if unsafe { libc::fcntl(fd, libc::F_SETFL, orig_flags | libc::O_NONBLOCK) } < 0 {
        return Err(HyprError::Io(format!(
            "fcntl O_NONBLOCK: {}",
            std::io::Error::last_os_error()
        )));
    }
    // Build sockaddr_un.
    let mut addr: libc::sockaddr_un = unsafe { std::mem::zeroed() };
    addr.sun_family = libc::AF_UNIX as libc::sa_family_t;
    for (i, b) in bytes.iter().enumerate() {
        addr.sun_path[i] = *b as libc::c_char;
    }
    let addr_len =
        (std::mem::size_of::<libc::sa_family_t>() + bytes.len() + 1) as libc::socklen_t;
    // SAFETY: fd is a valid socket, addr is a valid sockaddr_un of addr_len.
    let ret = unsafe {
        libc::connect(
            fd,
            &addr as *const _ as *const libc::sockaddr,
            addr_len,
        )
    };
    if ret != 0 {
        let errno = std::io::Error::last_os_error()
            .raw_os_error()
            .unwrap_or(libc::EIO);
        if errno != libc::EINPROGRESS {
            let e = std::io::Error::from_raw_os_error(errno);
            return Err(HyprError::Io(format!(
                "connect {}: {e}",
                socket.display()
            )));
        }
        // EINPROGRESS: poll for writability within the deadline, looping
        // across EINTR with the remaining budget.
        let deadline = Instant::now() + timeout;
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err(HyprError::Timeout(format!(
                    "connect {} exceeded {:?}",
                    socket.display(),
                    timeout
                )));
            }
            let ms = remaining.as_millis().min(i32::MAX as u128) as i32;
            let mut pfd = libc::pollfd {
                fd,
                events: libc::POLLOUT,
                revents: 0,
            };
            // SAFETY: single valid pollfd.
            let pr = unsafe { libc::poll(&mut pfd, 1, ms) };
            if pr == 0 {
                return Err(HyprError::Timeout(format!(
                    "connect {} exceeded {:?}",
                    socket.display(),
                    timeout
                )));
            }
            if pr < 0 {
                let pe = std::io::Error::last_os_error()
                    .raw_os_error()
                    .unwrap_or(libc::EIO);
                if pe == libc::EINTR {
                    continue;
                }
                let e = std::io::Error::from_raw_os_error(pe);
                return Err(HyprError::Io(format!(
                    "poll connect {}: {e}",
                    socket.display()
                )));
            }
            // Writable (or error): check SO_ERROR.
            let mut errcode: libc::c_int = 0;
            let mut len: libc::socklen_t =
                std::mem::size_of::<libc::c_int>() as libc::socklen_t;
            let so = unsafe {
                libc::getsockopt(
                    fd,
                    libc::SOL_SOCKET,
                    libc::SO_ERROR,
                    &mut errcode as *mut _ as *mut libc::c_void,
                    &mut len,
                )
            };
            if so != 0 {
                return Err(HyprError::Io(format!(
                    "getsockopt {}: {}",
                    socket.display(),
                    std::io::Error::last_os_error()
                )));
            }
            if errcode != 0 {
                // ECONNREFUSED etc: stale socket file, no listener.
                if errcode == libc::ETIMEDOUT {
                    return Err(HyprError::Timeout(format!(
                        "connect {}: {}",
                        socket.display(),
                        std::io::Error::from_raw_os_error(errcode)
                    )));
                }
                return Err(HyprError::Io(format!(
                    "connect {}: {}",
                    socket.display(),
                    std::io::Error::from_raw_os_error(errcode)
                )));
            }
            break;
        }
    }
    // Back to blocking; per-op timeouts are applied by the caller from the
    // remaining total budget.
    if unsafe { libc::fcntl(fd, libc::F_SETFL, orig_flags) } < 0 {
        return Err(HyprError::Io(format!(
            "fcntl blocking restore: {}",
            std::io::Error::last_os_error()
        )));
    }
    let fd_raw = guard.0;
    std::mem::forget(guard);
    // SAFETY: fd is a connected blocking socket owned by us now.
    let stream = unsafe { UnixStream::from_raw_fd(fd_raw) };
    Ok(stream)
}

fn capped_budget(deadline: Instant) -> Option<Duration> {
    let remaining = deadline.saturating_duration_since(Instant::now());
    if remaining.is_zero() {
        return None;
    }
    Some(remaining.min(SOCKET_RW_TIMEOUT))
}

/// One-shot request over `.socket.sock`. Opens (bounded connect), writes
/// `request`, reads to EOF within [`TOTAL_RESPONSE_DEADLINE`] and
/// [`MAX_RESPONSE_BYTES`], then closes (drops) immediately so Hyprland never
/// stalls.
///
/// The total covers connect + write + read: every wait uses the remaining
/// budget, so trickle senders (a byte at a time under the per-read timeout)
/// still hit the total deadline instead of stalling forever.
pub fn query_socket(request_socket: &Path, request: &str) -> Result<String, HyprError> {
    if request.is_empty() || request.len() > 4096 {
        return Err(HyprError::BadData("request out of bounds".to_string()));
    }
    let deadline = Instant::now() + TOTAL_RESPONSE_DEADLINE;
    let connect_budget = deadline
        .saturating_duration_since(Instant::now())
        .min(CONNECT_TIMEOUT);
    if connect_budget.is_zero() {
        return Err(HyprError::Timeout(format!(
            "response for {request} exceeded total deadline"
        )));
    }
    let mut stream = connect_bounded(request_socket, connect_budget)?;
    let write_budget = match capped_budget(deadline) {
        Some(b) => b,
        None => {
            return Err(HyprError::Timeout(format!(
                "response for {request} exceeded total deadline"
            )))
        }
    };
    stream
        .set_read_timeout(Some(write_budget))
        .map_err(|e| HyprError::Io(format!("read timeout set: {e}")))?;
    stream
        .set_write_timeout(Some(write_budget))
        .map_err(|e| HyprError::Io(format!("write timeout set: {e}")))?;
    match stream.write_all(request.as_bytes()) {
        Ok(()) => {}
        Err(e)
            if e.kind() == std::io::ErrorKind::TimedOut
                || e.kind() == std::io::ErrorKind::WouldBlock =>
        {
            return Err(HyprError::Timeout(format!(
                "write for {request} timed out: {e}"
            )))
        }
        Err(e) => return Err(HyprError::Io(format!("write request: {e}"))),
    }
    // Signal EOF to the compositor; ignore shutdown errors (best-effort).
    let _ = stream.shutdown(std::net::Shutdown::Write);

    let mut out: Vec<u8> = Vec::new();
    let mut chunk = [0u8; READ_CHUNK];
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(HyprError::Timeout(format!(
                "response for {request} exceeded total deadline"
            )));
        }
        // Re-cap the read wait to the remaining total so a slow trickle
        // cannot extend past the deadline via repeated per-read timeouts.
        let _ = stream.set_read_timeout(Some(remaining.min(SOCKET_RW_TIMEOUT)));
        match stream.read(&mut chunk) {
            Ok(0) => break, // EOF: complete response.
            Ok(n) => {
                if out.len() + n > MAX_RESPONSE_BYTES {
                    return Err(HyprError::TooLarge(format!(
                        "response for {request} exceeds {MAX_RESPONSE_BYTES} bytes"
                    )));
                }
                out.extend_from_slice(&chunk[..n]);
            }
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                return Err(HyprError::Timeout(format!(
                    "response for {request} read timeout: {e}"
                )));
            }
            Err(e) if e.kind() == std::io::ErrorKind::TimedOut => {
                // Distinguish trickle-over-deadline from a single slow read:
                // either way it is a timeout, and the loop bound above keeps
                // the actual total within TOTAL_RESPONSE_DEADLINE + one cap.
                return Err(HyprError::Timeout(format!(
                    "response for {request} timed out: {e}"
                )));
            }
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(HyprError::Io(format!("read response: {e}"))),
        }
    }
    String::from_utf8(out).map_err(|e| HyprError::BadData(format!("response not UTF-8: {e}")))
}

/// Authoritative snapshot from two bounded queries. Strict shape validation:
/// non-JSON or wrong-typed JSON is `Err` (never a fake empty state); a JSON
/// *object* without window/workspace fields is a valid empty focus/workspace.
pub fn fetch_snapshot(request_socket: &Path) -> Result<DesktopContext, HyprError> {
    fetch_snapshot_at(request_socket, now_ms())
}

pub fn fetch_snapshot_at(
    request_socket: &Path,
    observed_at_ms: i64,
) -> Result<DesktopContext, HyprError> {
    // Two separate queries can straddle a window/workspace switch and yield
    // an impossible combination (window from desk A + workspace B). Validate
    // with bounded rereads: require consecutive paired reads to agree
    // SEMANTICALLY (parsed focus/workspace fields + embedded workspace id,
    // ignoring JSON formatting, geometry, counts, and other irrelevant
    // metadata) AND the window's embedded workspace to agree with the
    // monitor's activeworkspace unless the window is on a special workspace
    // (special windows legitimately mismatch the monitor workspace, so never
    // equate the two blindly). After bounded attempts return Timeout rather
    // than persisting a torn snapshot. Query-level I/O/timeout errors fail
    // fast with no retry; shape errors (non-JSON/wrong-typed) fail fast as
    // BadData without retry.
    let mut last_err: Option<HyprError> = None;
    for _ in 0..SNAPSHOT_MAX_ATTEMPTS {
        let win_a = query_socket(request_socket, "j/activewindow")?;
        let ws_a = query_socket(request_socket, "j/activeworkspace")?;
        let win_b = query_socket(request_socket, "j/activewindow")?;
        let ws_b = query_socket(request_socket, "j/activeworkspace")?;
        // Semantic stability: parse first so formatting/geometry/count/extra
        // keys never force a retry. Parse failures are shape errors (fail
        // fast, no retry).
        let focused_a = parse_activewindow(&win_a)?;
        let focused_b = parse_activewindow(&win_b)?;
        let workspace_a = parse_activeworkspace(&ws_a)?;
        let workspace_b = parse_activeworkspace(&ws_b)?;
        // PID-only differences are correlation metadata, never instability:
        // compare focused windows on id/application/title only.
        let windows_stable = match (&focused_a, &focused_b) {
            (None, None) => true,
            (Some(a), Some(b)) => a.semantic_eq(b),
            _ => false,
        };
        if !windows_stable || workspace_a != workspace_b {
            last_err = Some(HyprError::Timeout(
                "snapshot unstable across rereads (switch in flight)".to_string(),
            ));
            continue;
        }
        // Embedded workspace stability (same semantic rule).
        if extract_window_workspace(&win_a) != extract_window_workspace(&win_b) {
            last_err = Some(HyprError::Timeout(
                "snapshot unstable across rereads (switch in flight)".to_string(),
            ));
            continue;
        }
        if !workspaces_consistent(&win_a, &ws_a) {
            last_err = Some(HyprError::Timeout(
                "snapshot window/workspace mismatch (switch in flight)".to_string(),
            ));
            continue;
        }
        return Ok(DesktopContext::available(
            Source::Hyprland,
            focused_a,
            workspace_a,
            observed_at_ms,
        ));
    }
    Err(last_err.unwrap_or_else(|| {
        HyprError::Timeout("snapshot unstable: retries exhausted".to_string())
    }))
}

/// Window's embedded `workspace` object, if present: `(id, name)` as
/// strings. `None` when absent/unparseable (caller treats as consistent:
/// nothing to validate against).
fn extract_window_workspace(raw: &str) -> Option<(String, String)> {
    let v: serde_json::Value = serde_json::from_str(raw.trim()).ok()?;
    let ws = v.get("workspace")?;
    if ws.is_null() {
        return None;
    }
    // Some versions flatten as `workspaceID` on the window; accept it.
    if let Some(obj) = ws.as_object() {
        let id = match obj.get("id") {
            None | Some(serde_json::Value::Null) => String::new(),
            Some(serde_json::Value::String(s)) => s.clone(),
            Some(serde_json::Value::Number(n)) => n.to_string(),
            Some(_) => return None,
        };
        let name = match obj.get("name") {
            None | Some(serde_json::Value::Null) => String::new(),
            Some(serde_json::Value::String(s)) => s.clone(),
            Some(_) => return None,
        };
        if id.is_empty() && name.is_empty() {
            return None;
        }
        return Some((id, name));
    }
    if let Some(s) = ws.as_str() {
        if s.is_empty() {
            return None;
        }
        return Some((s.to_string(), String::new()));
    }
    if let Some(n) = ws.as_i64() {
        return Some((n.to_string(), String::new()));
    }
    if let Some(n) = ws.as_u64() {
        return Some((n.to_string(), String::new()));
    }
    None
}

/// True for Hyprland special workspaces, which live above monitors: a
/// focused special window legitimately mismatches the monitor's
/// activeworkspace, so mismatch there is consistent.
fn is_special_workspace(id: &str, name: &str) -> bool {
    if name.starts_with("special") {
        return true;
    }
    if id.starts_with("special") {
        return true;
    }
    if id.starts_with('-') {
        return true;
    }
    if let Ok(n) = id.parse::<i64>() {
        if n < 0 {
            return true;
        }
    }
    false
}

/// Cross-check the window's embedded workspace against the activeworkspace
/// response. Lenient by design: missing fields, missing embedded workspace,
/// empty focus, or special workspaces all count as consistent. Only a
/// definite non-special disagreement is inconsistent (torn read).
fn workspaces_consistent(win_raw: &str, ws_raw: &str) -> bool {
    let Some((win_id, win_name)) = extract_window_workspace(win_raw) else {
        return true;
    };
    if is_special_workspace(&win_id, &win_name) {
        return true;
    }
    // Parse the activeworkspace the same way parse_activeworkspace does,
    // but without failing the whole snapshot on shape issues here (shape
    // errors are reported by the strict parser afterwards).
    let Ok(v) = serde_json::from_str::<serde_json::Value>(ws_raw.trim()) else {
        return true;
    };
    let Some(obj) = v.as_object() else {
        return true;
    };
    let ws_id = match obj.get("id") {
        None | Some(serde_json::Value::Null) => String::new(),
        Some(serde_json::Value::String(s)) => s.clone(),
        Some(serde_json::Value::Number(n)) => n.to_string(),
        Some(_) => return true,
    };
    let ws_id = if ws_id.is_empty() {
        match obj.get("workspaceID") {
            Some(serde_json::Value::Number(n)) => n.to_string(),
            Some(serde_json::Value::String(s)) => s.clone(),
            _ => String::new(),
        }
    } else {
        ws_id
    };
    let ws_name = match obj.get("name") {
        None | Some(serde_json::Value::Null) => String::new(),
        Some(serde_json::Value::String(s)) => s.clone(),
        Some(_) => return true,
    };
    // Empty active workspace with a present window: nothing definite to
    // contradict (strict parser decides validity).
    if ws_id.is_empty() && ws_name.is_empty() {
        return true;
    }
    if !win_id.is_empty() && !ws_id.is_empty() {
        return win_id == ws_id;
    }
    if !win_name.is_empty() && !ws_name.is_empty() {
        return win_name == ws_name;
    }
    true
}

/// Strict activewindow parse. `None` = valid empty focus.
pub fn parse_activewindow(raw: &str) -> Result<Option<FocusedWindow>, HyprError> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Err(HyprError::BadData(
            "empty activewindow response".to_string(),
        ));
    }
    let v: serde_json::Value = serde_json::from_str(trimmed)
        .map_err(|e| HyprError::BadData(format!("activewindow not JSON: {e}")))?;
    let obj = match v.as_object() {
        Some(o) => o,
        None => {
            return Err(HyprError::BadData(
                "activewindow must be a JSON object".to_string(),
            ))
        }
    };
    let addr = obj.get("address").and_then(|v| v.as_str()).unwrap_or("");
    let class = obj.get("class").and_then(|v| v.as_str()).unwrap_or("");
    let title = obj.get("title").and_then(|v| v.as_str()).unwrap_or("");
    // Wrong-typed present fields are invalid, not empty.
    for (key, want_str) in [("address", true), ("class", true), ("title", true)] {
        if let Some(val) = obj.get(key) {
            if !val.is_null() && !val.is_string() {
                let _ = want_str;
                return Err(HyprError::BadData(format!(
                    "activewindow.{key} must be a string"
                )));
            }
        }
    }
    // Optional correlation PID: Hyprland reports the client PID as a JSON
    // number under `pid`. Lenient by design (missing/null/wrong-typed reads
    // as None) so older compositors and fake sockets without `pid` keep
    // working; PID-only differences never affect semantic stability.
    let process_id: Option<u32> = match obj.get("pid") {
        None | Some(serde_json::Value::Null) => None,
        Some(serde_json::Value::Number(n)) => n.as_u64().and_then(|v| u32::try_from(v).ok()),
        Some(serde_json::Value::String(s)) => s.parse::<u32>().ok(),
        Some(_) => None,
    };
    // Valid empty: no address (or null address) and no class/title content.
    if addr.is_empty() || addr == "0x0" {
        if class.is_empty() && title.is_empty() {
            return Ok(None);
        }
        // Address-less but with content: still treat as a window with an
        // empty opaque id (valid, not fake-empty).
    }
    if addr.is_empty() && class.is_empty() && title.is_empty() {
        return Ok(None);
    }
    Ok(Some(FocusedWindow::new_with_pid(
        addr,
        class,
        title,
        process_id,
    )))
}

/// Strict activeworkspace parse. `None` = valid empty workspace.
pub fn parse_activeworkspace(raw: &str) -> Result<Option<Workspace>, HyprError> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Err(HyprError::BadData(
            "empty activeworkspace response".to_string(),
        ));
    }
    let v: serde_json::Value = serde_json::from_str(trimmed)
        .map_err(|e| HyprError::BadData(format!("activeworkspace not JSON: {e}")))?;
    let obj = match v.as_object() {
        Some(o) => o,
        None => {
            return Err(HyprError::BadData(
                "activeworkspace must be a JSON object".to_string(),
            ))
        }
    };
    let name = match obj.get("name") {
        None | Some(serde_json::Value::Null) => String::new(),
        Some(serde_json::Value::String(s)) => s.clone(),
        Some(_) => {
            return Err(HyprError::BadData(
                "activeworkspace.name must be a string".to_string(),
            ))
        }
    };
    let id = match obj.get("id") {
        None | Some(serde_json::Value::Null) => String::new(),
        Some(serde_json::Value::String(s)) => s.clone(),
        Some(serde_json::Value::Number(n)) => n.to_string(),
        Some(_) => {
            return Err(HyprError::BadData(
                "activeworkspace.id must be a string or number".to_string(),
            ))
        }
    };
    // Also accept `workspaceID` legacy alias if `id` absent.
    let id = if id.is_empty() {
        match obj.get("workspaceID") {
            Some(serde_json::Value::Number(n)) => n.to_string(),
            Some(serde_json::Value::String(s)) => s.clone(),
            Some(_) => {
                return Err(HyprError::BadData(
                    "activeworkspace.workspaceID must be a string or number".to_string(),
                ))
            }
            None => String::new(),
        }
    } else {
        id
    };
    if id.is_empty() && name.is_empty() {
        return Ok(None);
    }
    Ok(Some(Workspace::new(&id, &name)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::net::UnixListener;

    #[test]
    fn parse_lines_valid_and_malformed() {
        let e = parse_event_line("activewindow>>kitty,hello, world").unwrap();
        assert_eq!(e.name, "activewindow");
        // Commas in titles preserved verbatim.
        assert_eq!(e.payload, "kitty,hello, world");
        assert!(parse_event_line("no-separator-here").is_none());
        assert!(parse_event_line(">>payload").is_none());
        assert!(parse_event_line("").is_none());
        assert!(parse_event_line("bad name>>x").is_none());
        // Empty payload is valid (e.g. configreloaded>>).
        let e = parse_event_line("configreloaded>>").unwrap();
        assert_eq!(e.payload, "");
    }

    #[test]
    fn meaningful_filter() {
        for name in [
            "activewindow",
            "activewindowv2",
            "windowtitle",
            "windowtitlev2",
            "workspace",
            "workspacev2",
            "focusedmon",
            "openwindow",
            "closewindow",
            "movewindow",
        ] {
            assert!(is_meaningful_event(name), "{name}");
        }
        for name in [
            "openlayer",
            "closelayer",
            "configreloaded",
            "fullscreen",
            "submap",
            "screencast",
            "unknownfutureevent",
            "",
        ] {
            assert!(!is_meaningful_event(name), "{name}");
        }
    }

    #[test]
    fn first_comma_split_preserves_title_commas() {
        let (class, title) = split_first_comma("kitty,a, b, c");
        assert_eq!(class, "kitty");
        assert_eq!(title, "a, b, c");
        let (a, b) = split_first_comma("onlyclass");
        assert_eq!(a, "onlyclass");
        assert_eq!(b, "");
    }

    #[test]
    fn openwindow_split_preserves_title() {
        let (addr, ws, class, title) = split_openwindow("0xabc,1,kitty,hello, world, ok").unwrap();
        assert_eq!(addr, "0xabc");
        assert_eq!(ws, "1");
        assert_eq!(class, "kitty");
        assert_eq!(title, "hello, world, ok");
        assert!(split_openwindow("only,two").is_none());
    }

    #[test]
    fn snapshot_parsing_empty_valid_bad_invalid() {
        // Valid empty focus.
        assert_eq!(parse_activewindow("{}").unwrap(), None);
        assert_eq!(parse_activewindow(r#"{"address":"0x0"}"#).unwrap(), None);
        // Present window.
        let w = parse_activewindow(r#"{"address":"0x1","class":"kitty","title":"a, b"}"#)
            .unwrap()
            .unwrap();
        assert_eq!(w.title, "a, b");
        // Bad JSON is an error, never fake-empty.
        assert!(parse_activewindow("not json").is_err());
        assert!(parse_activewindow("[1,2]").is_err());
        assert!(parse_activewindow(r#"{"address": 123}"#).is_err());
        // Workspace variants.
        assert_eq!(parse_activeworkspace("{}").unwrap(), None);
        let ws = parse_activeworkspace(r#"{"id": 3, "name": "code"}"#)
            .unwrap()
            .unwrap();
        assert_eq!(ws.id, "3");
        assert_eq!(ws.name, "code");
        assert!(parse_activeworkspace("garbage").is_err());
        assert!(parse_activeworkspace("[1]").is_err());
    }

    fn unique_tmp(tag: &str) -> PathBuf {
        use std::sync::atomic::{AtomicU64, Ordering};
        static CTR: AtomicU64 = AtomicU64::new(0);
        let n = CTR.fetch_add(1, Ordering::SeqCst);
        std::env::temp_dir().join(format!(
            "qs-hypr-{tag}-{}-{}-{}",
            std::process::id(),
            n,
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ))
    }

    /// Bind both real Unix sockets in `dir` and hold the listeners alive.
    /// Returns the guards (must be kept alive for the sockets to stay live).
    fn bind_live_pair(dir: &Path) -> (UnixListener, UnixListener) {
        std::fs::create_dir_all(dir).unwrap();
        let req_path = dir.join(".socket.sock");
        let ev_path = dir.join(".socket2.sock");
        let _ = std::fs::remove_file(&req_path);
        let _ = std::fs::remove_file(&ev_path);
        let req = UnixListener::bind(&req_path).expect("bind req");
        let ev = UnixListener::bind(&ev_path).expect("bind ev");
        (req, ev)
    }

    #[test]
    fn discover_prefers_modern_over_legacy() {
        let base = unique_tmp("disc");
        let rt = base.join("run");
        let tmp = base.join("tmp");
        let sig = "abc123";
        std::fs::create_dir_all(rt.join("hypr").join(sig)).unwrap();
        std::fs::create_dir_all(tmp.join(sig)).unwrap();
        // Only legacy has sockets: legacy wins.
        let _legacy = bind_live_pair(&tmp.join(sig));
        let found = discover_sockets_with(Some(rt.as_path()), Some(sig), &tmp).unwrap();
        assert_eq!(found.request_socket, tmp.join(sig).join(".socket.sock"));
        // Add modern sockets: modern wins.
        let _modern = bind_live_pair(&rt.join("hypr").join(sig));
        let found2 = discover_sockets_with(Some(rt.as_path()), Some(sig), &tmp).unwrap();
        assert_eq!(
            found2.request_socket,
            rt.join("hypr").join(sig).join(".socket.sock")
        );
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn discover_rejects_unsafe_signatures() {
        let base = unique_tmp("disc-unsafe");
        std::fs::create_dir_all(&base).unwrap();
        for bad in ["../evil", "/abs", "a/b", "a\0b", ".", "..", "", "has space"] {
            assert!(
                discover_sockets_with(Some(base.as_path()), Some(bad), &base).is_none(),
                "bad sig {bad:?} must be rejected"
            );
        }
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn discover_skips_non_socket_files() {
        let base = unique_tmp("disc-nonsock");
        let rt = base.join("run");
        let tmp = base.join("tmp");
        let sig = "plainfiles";
        std::fs::create_dir_all(rt.join("hypr").join(sig)).unwrap();
        std::fs::create_dir_all(&tmp).unwrap();
        // Regular files where sockets should be: not eligible.
        std::fs::write(rt.join("hypr").join(sig).join(".socket.sock"), b"x").unwrap();
        std::fs::write(rt.join("hypr").join(sig).join(".socket2.sock"), b"x").unwrap();
        // Explicit signature with non-sockets -> None (no silent regular-file pick).
        assert!(discover_sockets_with(Some(rt.as_path()), Some(sig), &tmp).is_none());
        // Implicit scan with only non-sockets -> None.
        assert!(discover_sockets_with(Some(rt.as_path()), None, &tmp).is_none());
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn discover_symlink_endpoints_rejected() {
        let base = unique_tmp("disc-symlink");
        let rt = base.join("run");
        let tmp = base.join("tmp");
        std::fs::create_dir_all(&tmp).unwrap();
        // Real live target elsewhere.
        let target_dir = base.join("target");
        let _target = bind_live_pair(&target_dir);
        let sig = "linkedsig";
        let link_dir = rt.join("hypr").join(sig);
        std::fs::create_dir_all(&link_dir).unwrap();
        // Symlink endpoints pointing at the live target: must be rejected.
        #[cfg(unix)]
        {
            use std::os::unix::fs::symlink;
            let _ = std::fs::remove_file(link_dir.join(".socket.sock"));
            let _ = std::fs::remove_file(link_dir.join(".socket2.sock"));
            symlink(target_dir.join(".socket.sock"), link_dir.join(".socket.sock")).unwrap();
            symlink(
                target_dir.join(".socket2.sock"),
                link_dir.join(".socket2.sock"),
            )
            .unwrap();
        }
        assert!(discover_sockets_with(Some(rt.as_path()), Some(sig), &tmp).is_none());
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn discover_stale_first_live_second() {
        // Lexicographically-first entry is stale (regular files), second is live.
        let base = unique_tmp("disc-stale");
        let hypr = base.join("hypr");
        std::fs::create_dir_all(hypr.join("aaa")).unwrap();
        std::fs::create_dir_all(hypr.join("zzz")).unwrap();
        std::fs::write(hypr.join("aaa").join(".socket.sock"), b"stale").unwrap();
        std::fs::write(hypr.join("aaa").join(".socket2.sock"), b"stale").unwrap();
        let _live = bind_live_pair(&hypr.join("zzz"));
        // Re-run via runtime layout for clarity: modern base scan.
        let rt = base.join("rt");
        std::fs::create_dir_all(rt.join("hypr").join("aaa")).unwrap();
        std::fs::create_dir_all(rt.join("hypr").join("zzz")).unwrap();
        std::fs::write(rt.join("hypr").join("aaa").join(".socket.sock"), b"s").unwrap();
        std::fs::write(rt.join("hypr").join("aaa").join(".socket2.sock"), b"s").unwrap();
        let _live2 = bind_live_pair(&rt.join("hypr").join("zzz"));
        let empty_tmp = base.join("no-legacy");
        std::fs::create_dir_all(&empty_tmp).unwrap();
        let found = discover_sockets_with(Some(rt.as_path()), None, &empty_tmp)
            .expect("live second must win over stale first");
        assert_eq!(
            found.request_socket,
            rt.join("hypr").join("zzz").join(".socket.sock")
        );
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn discover_dead_socket_file_skipped_for_live() {
        // Stale Unix-socket file (listener gone, file left) first, live second.
        let base = unique_tmp("disc-dead");
        let rt = base.join("rt");
        let aaa = rt.join("hypr").join("aaa");
        let zzz = rt.join("hypr").join("zzz");
        std::fs::create_dir_all(&aaa).unwrap();
        std::fs::create_dir_all(&zzz).unwrap();
        // Create a real socket file then drop the listener without unlinking:
        // the path remains a socket inode but connect fails (stale).
        {
            let req_path = aaa.join(".socket.sock");
            let ev_path = aaa.join(".socket2.sock");
            let req = UnixListener::bind(&req_path).unwrap();
            let ev = UnixListener::bind(&ev_path).unwrap();
            // Keep file existence: drop listeners, files stay on disk.
            drop(req);
            drop(ev);
        }
        // Sanity: stale paths are sockets but not connectable.
        assert!(connect_bounded(&aaa.join(".socket.sock"), Duration::from_millis(200)).is_err());
        let _live = bind_live_pair(&zzz);
        let empty_tmp = base.join("no-legacy");
        std::fs::create_dir_all(&empty_tmp).unwrap();
        let found = discover_sockets_with(Some(rt.as_path()), None, &empty_tmp)
            .expect("live must beat dead socket file");
        assert_eq!(found.request_socket, zzz.join(".socket.sock"));
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn discover_ambiguous_multiple_live_requires_explicit() {
        let base = unique_tmp("disc-amb");
        let rt = base.join("rt");
        std::fs::create_dir_all(rt.join("hypr").join("aaa")).unwrap();
        std::fs::create_dir_all(rt.join("hypr").join("bbb")).unwrap();
        let _a = bind_live_pair(&rt.join("hypr").join("aaa"));
        let _b = bind_live_pair(&rt.join("hypr").join("bbb"));
        let empty_tmp = base.join("no-legacy");
        std::fs::create_dir_all(&empty_tmp).unwrap();
        // Ambiguous: must not silently pick one.
        assert!(discover_sockets_with(Some(rt.as_path()), None, &empty_tmp).is_none());
        // Explicit signature disambiguates.
        let fa = discover_sockets_with(Some(rt.as_path()), Some("aaa"), &empty_tmp).unwrap();
        assert_eq!(fa.request_socket, rt.join("hypr").join("aaa").join(".socket.sock"));
        let fb = discover_sockets_with(Some(rt.as_path()), Some("bbb"), &empty_tmp).unwrap();
        assert_eq!(fb.request_socket, rt.join("hypr").join("bbb").join(".socket.sock"));
        let _ = std::fs::remove_dir_all(&base);
    }

    /// Fake request socket: replies with canned JSON per request string.
    pub fn spawn_fake_request_socket(
        dir: &Path,
        activewindow: &str,
        activeworkspace: &str,
    ) -> PathBuf {
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
                if n == 0 {
                    continue; // liveness probe without payload.
                }
                let req = String::from_utf8_lossy(&buf[..n]).to_string();
                let reply = if req.contains("activewindow") {
                    win.clone()
                } else if req.contains("activeworkspace") {
                    ws.clone()
                } else {
                    "{}".to_string()
                };
                let _ = stream.write_all(reply.as_bytes());
                // Drop = EOF for the client.
            }
        });
        path
    }

    /// Mutating fake: first `switch_after` connections serve (win_a, ws_a),
    /// later connections serve (win_b, ws_b). Simulates a switch landing
    /// between the two snapshot queries.
    fn spawn_switch_once_socket(
        dir: &Path,
        win_a: &str,
        ws_a: &str,
        win_b: &str,
        ws_b: &str,
        switch_after: usize,
    ) -> PathBuf {
        use std::sync::atomic::{AtomicUsize, Ordering};
        use std::sync::Arc;
        let path = dir.join(".socket.sock");
        let _ = std::fs::remove_file(&path);
        let listener = UnixListener::bind(&path).expect("bind switch socket");
        let (wa, sa, wb, sb) = (
            win_a.to_string(),
            ws_a.to_string(),
            win_b.to_string(),
            ws_b.to_string(),
        );
        let count = Arc::new(AtomicUsize::new(0));
        let c2 = Arc::clone(&count);
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
                if n == 0 {
                    continue;
                }
                let idx = c2.fetch_add(1, Ordering::SeqCst);
                let req = String::from_utf8_lossy(&buf[..n]).to_string();
                let is_win = req.contains("activewindow");
                let reply = if idx < switch_after {
                    if is_win { wa.clone() } else { sa.clone() }
                } else if is_win {
                    wb.clone()
                } else {
                    sb.clone()
                };
                let _ = stream.write_all(reply.as_bytes());
            }
        });
        path
    }

    /// Flapping fake: alternates states every connection, so any torn pair
    /// stays torn but rereads can look superficially stable. The snapshot
    /// must never return the impossible mix.
    fn spawn_flapping_socket(dir: &Path, win_a: &str, ws_a: &str, win_b: &str, ws_b: &str) -> PathBuf {
        use std::sync::atomic::{AtomicUsize, Ordering};
        use std::sync::Arc;
        let path = dir.join(".socket.sock");
        let _ = std::fs::remove_file(&path);
        let listener = UnixListener::bind(&path).expect("bind flap socket");
        let (wa, sa, wb, sb) = (
            win_a.to_string(),
            ws_a.to_string(),
            win_b.to_string(),
            ws_b.to_string(),
        );
        let count = Arc::new(AtomicUsize::new(0));
        let c2 = Arc::clone(&count);
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
                if n == 0 {
                    continue;
                }
                let idx = c2.fetch_add(1, Ordering::SeqCst);
                let req = String::from_utf8_lossy(&buf[..n]).to_string();
                // Even connections serve state A, odd serve state B.
                // win queries at even idx -> W1, ws queries at odd idx -> WS2:
                // a naive two-query snapshot would return the mixed W1+WS2.
                let use_a = idx % 2 == 0;
                let reply = if req.contains("activewindow") {
                    if use_a { wa.clone() } else { wb.clone() }
                } else if use_a {
                    sa.clone()
                } else {
                    sb.clone()
                };
                let _ = stream.write_all(reply.as_bytes());
            }
        });
        path
    }

    #[test]
    fn query_and_snapshot_against_fake_socket() {
        let dir = unique_tmp("fake");
        std::fs::create_dir_all(&dir).unwrap();
        let req = spawn_fake_request_socket(
            &dir,
            r#"{"address":"0x1","class":"kitty","title":"hi, there","workspace":{"id":1,"name":"1"}}"#,
            r#"{"id":1,"name":"1"}"#,
        );
        std::thread::sleep(Duration::from_millis(50));
        let raw = query_socket(&req, "j/activewindow").unwrap();
        assert!(raw.contains("kitty"));
        let snap = fetch_snapshot_at(&req, 1234).unwrap();
        assert!(snap.available);
        assert_eq!(snap.focused_window.unwrap().title, "hi, there");
        assert_eq!(snap.workspace.unwrap().name, "1");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn snapshot_switch_mid_read_never_returns_mixed() {
        let dir = unique_tmp("switch");
        std::fs::create_dir_all(&dir).unwrap();
        let win1 = r#"{"address":"0x1","class":"kitty","title":"one","workspace":{"id":1,"name":"1"}}"#;
        let ws1 = r#"{"id":1,"name":"1"}"#;
        let win2 = r#"{"address":"0x2","class":"foot","title":"two","workspace":{"id":2,"name":"2"}}"#;
        let ws2 = r#"{"id":2,"name":"2"}"#;
        // Switch lands after the first query: win_a=W1, everything after=W2/WS2.
        let req = spawn_switch_once_socket(&dir, win1, ws1, win2, ws2, 1);
        std::thread::sleep(Duration::from_millis(50));
        let snap = fetch_snapshot_at(&req, 7).expect("must resolve to a stable state");
        let win_id = snap.focused_window.clone().map(|w| w.id).unwrap_or_default();
        let ws_id = snap.workspace.clone().map(|w| w.id).unwrap_or_default();
        // Never the impossible W1 + WS2 mix.
        assert!(
            !(win_id == "0x1" && ws_id == "2"),
            "torn snapshot returned: win={win_id} ws={ws_id}"
        );
        // Must be one of the two real states (here: the new one).
        assert_eq!(win_id, "0x2");
        assert_eq!(ws_id, "2");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn snapshot_flapping_never_returns_mixed() {
        let dir = unique_tmp("flap");
        std::fs::create_dir_all(&dir).unwrap();
        let win1 = r#"{"address":"0x1","class":"kitty","title":"one","workspace":{"id":1,"name":"1"}}"#;
        let ws1 = r#"{"id":1,"name":"1"}"#;
        let win2 = r#"{"address":"0x2","class":"foot","title":"two","workspace":{"id":2,"name":"2"}}"#;
        let ws2 = r#"{"id":2,"name":"2"}"#;
        let req = spawn_flapping_socket(&dir, win1, ws1, win2, ws2);
        std::thread::sleep(Duration::from_millis(50));
        match fetch_snapshot_at(&req, 9) {
            Ok(snap) => {
                let win_id = snap.focused_window.clone().map(|w| w.id).unwrap_or_default();
                let ws_id = snap.workspace.clone().map(|w| w.id).unwrap_or_default();
                let mixed = (win_id == "0x1" && ws_id == "2")
                    || (win_id == "0x2" && ws_id == "1");
                assert!(!mixed, "torn snapshot returned: win={win_id} ws={ws_id}");
                // If it did return, it must be a fully consistent pair.
                assert!(
                    (win_id == "0x1" && ws_id == "1")
                        || (win_id == "0x2" && ws_id == "2"),
                    "unexpected combo win={win_id} ws={ws_id}"
                );
            }
            Err(_) => {} // Bounded failure is acceptable; torn data is not.
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn snapshot_special_workspace_mismatch_accepted() {
        let dir = unique_tmp("special");
        std::fs::create_dir_all(&dir).unwrap();
        // Focused window lives on special: monitor workspace legitimately differs.
        let win = r#"{"address":"0x9","class":"kitty","title":"spec","workspace":{"id":-99,"name":"special:magic"}}"#;
        let ws = r#"{"id":1,"name":"1"}"#;
        let req = spawn_fake_request_socket(&dir, win, ws);
        std::thread::sleep(Duration::from_millis(50));
        let snap = fetch_snapshot_at(&req, 11).expect("special mismatch must be accepted");
        assert_eq!(snap.focused_window.unwrap().id, "0x9");
        assert_eq!(snap.workspace.unwrap().id, "1");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn workspace_consistency_rules() {
        // No embedded workspace -> consistent.
        assert!(workspaces_consistent(r#"{"address":"0x1"}"#, r#"{"id":1,"name":"1"}"#));
        // Matching ids -> consistent.
        assert!(workspaces_consistent(
            r#"{"address":"0x1","workspace":{"id":2,"name":"2"}}"#,
            r#"{"id":2,"name":"2"}"#
        ));
        // Non-special mismatch -> inconsistent.
        assert!(!workspaces_consistent(
            r#"{"address":"0x1","workspace":{"id":1,"name":"1"}}"#,
            r#"{"id":2,"name":"2"}"#
        ));
        // Special mismatch -> consistent (never equate blindly).
        assert!(workspaces_consistent(
            r#"{"address":"0x1","workspace":{"id":-99,"name":"special:magic"}}"#,
            r#"{"id":1,"name":"1"}"#
        ));
        assert!(workspaces_consistent(
            r#"{"address":"0x1","workspace":{"id":99,"name":"special"}}"#,
            r#"{"id":1,"name":"1"}"#
        ));
    }

    #[test]
    fn connect_bounded_success_and_missing() {
        let dir = unique_tmp("conn");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("live.sock");
        let _listener = UnixListener::bind(&path).unwrap();
        let s = connect_bounded(&path, Duration::from_secs(2)).expect("live connect");
        drop(s);
        let missing = dir.join("missing.sock");
        let start = Instant::now();
        let err = connect_bounded(&missing, Duration::from_secs(2)).expect_err("missing must fail");
        // Fast failure, bounded well under the 2s budget.
        assert!(start.elapsed() < Duration::from_secs(2), "missing connect took {err:?}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn query_stall_times_out_within_total() {
        let dir = unique_tmp("stall");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(".socket.sock");
        let _ = std::fs::remove_file(&path);
        let listener = UnixListener::bind(&path).unwrap();
        std::thread::spawn(move || {
            for conn in listener.incoming() {
                let mut stream = match conn {
                    Ok(s) => s,
                    Err(_) => return,
                };
                let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
                let mut buf = vec![0u8; 4096];
                let _ = stream.read(&mut buf);
                // Stall: never reply.
                std::thread::sleep(TOTAL_RESPONSE_DEADLINE + Duration::from_secs(3));
            }
        });
        std::thread::sleep(Duration::from_millis(50));
        let start = Instant::now();
        let err = query_socket(&path, "j/activewindow").expect_err("stall must timeout");
        let elapsed = start.elapsed();
        assert!(
            matches!(err, HyprError::Timeout(_)),
            "stall must be Timeout, got {err:?}"
        );
        assert!(
            elapsed < TOTAL_RESPONSE_DEADLINE + Duration::from_secs(2),
            "stall total must stay bounded, took {elapsed:?}"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn query_trickle_times_out_within_total() {
        let dir = unique_tmp("trickle");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(".socket.sock");
        let _ = std::fs::remove_file(&path);
        let listener = UnixListener::bind(&path).unwrap();
        std::thread::spawn(move || {
            for conn in listener.incoming() {
                let mut stream = match conn {
                    Ok(s) => s,
                    Err(_) => return,
                };
                let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
                let mut buf = vec![0u8; 4096];
                let _ = stream.read(&mut buf);
                // Trickle: a byte at a time, each under the per-read timeout,
                // but the total would run past the deadline.
                for _ in 0..40 {
                    let _ = stream.write_all(b"x");
                    std::thread::sleep(Duration::from_millis(300));
                }
            }
        });
        std::thread::sleep(Duration::from_millis(50));
        let start = Instant::now();
        let err = query_socket(&path, "j/activewindow").expect_err("trickle must timeout");
        let elapsed = start.elapsed();
        assert!(
            matches!(err, HyprError::Timeout(_)),
            "trickle must be Timeout, got {err:?}"
        );
        assert!(
            elapsed < TOTAL_RESPONSE_DEADLINE + Duration::from_secs(2),
            "trickle total must stay bounded, took {elapsed:?}"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn query_oversize_rejected_with_limit() {
        let dir = unique_tmp("oversize");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(".socket.sock");
        let _ = std::fs::remove_file(&path);
        let listener = UnixListener::bind(&path).unwrap();
        std::thread::spawn(move || {
            for conn in listener.incoming() {
                let mut stream = match conn {
                    Ok(s) => s,
                    Err(_) => return,
                };
                let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
                let mut buf = vec![0u8; 4096];
                let _ = stream.read(&mut buf);
                // Blast past MAX_RESPONSE_BYTES as fast as possible.
                let chunk = vec![b'y'; 64 * 1024];
                for _ in 0..((MAX_RESPONSE_BYTES / chunk.len()) + 2) {
                    if stream.write_all(&chunk).is_err() {
                        break;
                    }
                }
            }
        });
        std::thread::sleep(Duration::from_millis(50));
        let err = query_socket(&path, "j/activewindow").expect_err("oversize must fail");
        assert!(
            matches!(err, HyprError::TooLarge(_)),
            "oversize must be TooLarge, got {err:?}"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn connect_backlog_stays_bounded() {
        // Listener that never accepts: fill the backlog, then verify the
        // bounded connect never blocks past its deadline.
        let dir = unique_tmp("backlog");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("backlog.sock");
        let _ = std::fs::remove_file(&path);
        let listener = UnixListener::bind(&path).unwrap();
        // Hold many pending connections without accepting.
        let mut held = Vec::new();
        for _ in 0..128 {
            match UnixStream::connect(&path) {
                Ok(s) => held.push(s),
                Err(_) => break,
            }
        }
        let start = Instant::now();
        let timeout = Duration::from_millis(500);
        let _ = connect_bounded(&path, timeout);
        let elapsed = start.elapsed();
        assert!(
            elapsed < timeout + Duration::from_secs(2),
            "backlog connect must stay bounded, took {elapsed:?}"
        );
        drop(held);
        drop(listener);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
