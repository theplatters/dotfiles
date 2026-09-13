use crate::cli::Config;
use std::collections::HashMap;
use std::os::unix::io::AsRawFd;
use std::os::unix::process::CommandExt;
use std::process::{Child, ChildStdin, Command, ExitStatus, Stdio};
use std::time::{Duration, Instant};

pub struct PiHandles {
    pub child: Child,
    pub stdin: ChildStdin,
    #[allow(dead_code)]
    pub pid: u32,
    pub pgid: i32,
}

/// Bound a pending session name like `scripts/project_sessions.py`:
/// NUL-tolerant whitespace collapse, capped at 120 chars (QML rename bound).
pub fn bounded_name(value: &str) -> String {
    let cleaned: String = value.replace('\0', " ");
    let collapsed: String = cleaned.split_whitespace().collect::<Vec<_>>().join(" ");
    collapsed.chars().take(120).collect()
}

pub fn build_pi_command(
    cfg: &Config,
    session_file: &str,
    session_name: &str,
    fresh: bool,
) -> (Vec<String>, HashMap<String, String>) {
    // Capture the process environment once; the pure helper below takes it
    // as an explicit map so unit tests stay hermetic (no global set_var).
    let base: HashMap<String, String> = std::env::vars().collect();
    build_pi_command_with_env(cfg, session_file, session_name, fresh, &base)
}

fn build_pi_command_with_env(
    cfg: &Config,
    session_file: &str,
    session_name: &str,
    fresh: bool,
    base_env: &HashMap<String, String>,
) -> (Vec<String>, HashMap<String, String>) {
    let root = cfg.root.to_string_lossy().to_string();
    let mut cmd: Vec<String> = Vec::new();
    if cfg.mode == "palette" {
        // General palette worker: direct `pi --mode rpc --approve`, never the
        // scoped wrappers/private pools, never --continue. Legacy palette
        // command parity: inherited PI_CODING_AGENT_SESSION_DIR
        // selects the pool, cached --session is passed verbatim when present,
        // and a pending name becomes --name only when there is no file to
        // resume (fresh or empty), so a New session can never revive stale state.
        cmd.push("pi".to_string());
        cmd.push("--mode".to_string());
        cmd.push("rpc".to_string());
        cmd.push("--approve".to_string());
        let inherited_dir = base_env
            .get("PI_CODING_AGENT_SESSION_DIR")
            .map(|s| s.trim().to_string())
            .unwrap_or_default();
        if !inherited_dir.is_empty() {
            cmd.push("--session-dir".to_string());
            cmd.push(inherited_dir);
        }
        if fresh {
            let name = bounded_name(session_name.trim());
            if !name.is_empty() {
                cmd.push("--name".to_string());
                cmd.push(name);
            }
        } else if !session_file.trim().is_empty() {
            cmd.push("--session".to_string());
            cmd.push(session_file.trim().to_string());
        } else {
            let name = bounded_name(session_name.trim());
            if !name.is_empty() {
                cmd.push("--name".to_string());
                cmd.push(name);
            }
        }
    } else if cfg.mode == "project" {
        cmd.push("python3".to_string());
        cmd.push(format!("{root}/scripts/project_sessions.py"));
        cmd.push("--project".to_string());
        cmd.push(cfg.project.clone().unwrap_or_default());
        if !session_file.is_empty() {
            cmd.push("--session".to_string());
            cmd.push(session_file.to_string());
            if !session_name.trim().is_empty() {
                cmd.push("--pending-name".to_string());
                cmd.push(session_name.trim().to_string());
            }
        }
        if fresh {
            cmd.push("--new-session".to_string());
        }
    } else {
        cmd.push("python3".to_string());
        cmd.push(format!("{root}/scripts/journal_sessions.py"));
        if !session_file.is_empty() {
            cmd.push("--session".to_string());
            cmd.push(session_file.to_string());
            if !session_name.trim().is_empty() {
                cmd.push("--pending-name".to_string());
                cmd.push(session_name.trim().to_string());
            }
        }
        if fresh {
            cmd.push("--new-session".to_string());
        }
    }
    // Environment parity: same workingDirectory, project mode clears journal
    // vars, journal mode sets QS_JOURNAL_MODE and clears project path.
    // Palette (general) mode must never impose a graph tool allowlist: clear
    // all four scope vars so the existing TypeScript extension stays on its
    // general tools. LOGSEQ_GRAPH (and PI_CODING_AGENT_SESSION_DIR) are
    // inherited untouched.
    let mut env: HashMap<String, String> = base_env.clone();
    if cfg.mode == "palette" {
        env.remove("QS_PROJECT_PATH");
        env.remove("QS_PROJECT_SESSION_SCOPE");
        env.remove("QS_JOURNAL_MODE");
        env.remove("QS_JOURNAL_SESSION_SCOPE");
    } else if cfg.mode == "journal" {
        env.insert("QS_JOURNAL_MODE".to_string(), "1".to_string());
        env.insert("QS_PROJECT_PATH".to_string(), String::new());
        env.remove("QS_PROJECT_SESSION_SCOPE");
    } else {
        env.insert(
            "QS_PROJECT_PATH".to_string(),
            cfg.project.clone().unwrap_or_default(),
        );
        env.remove("QS_JOURNAL_MODE");
        env.remove("QS_JOURNAL_SESSION_SCOPE");
    }
    (cmd, env)
}

pub fn spawn_pi(
    cfg: &Config,
    session_file: &str,
    session_name: &str,
    fresh: bool,
) -> Result<PiHandles, String> {
    let (cmd, env) = build_pi_command(cfg, session_file, session_name, fresh);
    if cmd.len() < 2 {
        return Err("pi command is empty".to_string());
    }
    let mut command = Command::new(&cmd[0]);
    command.args(&cmd[1..]);
    command.current_dir(&cfg.root);
    command.env_clear();
    for (k, v) in env {
        command.env(k, v);
    }
    command.stdin(Stdio::piped());
    command.stdout(Stdio::piped());
    command.stderr(Stdio::piped());
    unsafe {
        command.pre_exec(|| {
            // Own process group so SIGTERM/SIGKILL escalation never leaks
            // grandchildren when Quickshell SIGTERMs the bridge.
            libc::setpgid(0, 0);
            Ok(())
        });
    }
    let mut child = command
        .spawn()
        .map_err(|e| format!("pi process could not start: {e}"))?;
    let stdin = child
        .stdin
        .take()
        .ok_or("pi stdin unavailable".to_string())?;
    // Writers run on the state loop, so Pi stdin must be nonblocking: a
    // blocked peer fails closed via deadline instead of wedging Stop,
    // expiry and EOF handling. Reader fds stay blocking on their dedicated
    // threads (bounded framer + bounded channel there).
    crate::transport::set_nonblocking(stdin.as_raw_fd())
        .map_err(|e| format!("pi stdin nonblocking failed: {e}"))?;
    let pid = child.id();
    Ok(PiHandles {
        child,
        stdin,
        pid,
        pgid: pid as i32,
    })
}

/// SIGTERM the process group, wait up to `timeout`, then SIGKILL. Always
/// reaps the child to avoid zombies/orphans.
#[allow(dead_code)]
pub fn stop_child(handles: &mut PiHandles, timeout: Duration) {
    unsafe {
        libc::killpg(handles.pgid, libc::SIGTERM);
    }
    let start = Instant::now();
    loop {
        match handles.child.try_wait() {
            Ok(Some(_)) => return,
            Ok(None) => {
                if start.elapsed() >= timeout {
                    break;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(_) => return,
        }
    }
    unsafe {
        libc::killpg(handles.pgid, libc::SIGKILL);
    }
    let _ = handles.child.wait();
}

#[allow(dead_code)]
pub fn kill_child_group(pgid: i32) {
    if pgid > 0 {
        unsafe {
            libc::killpg(pgid, libc::SIGTERM);
        }
    }
}

/// Whether any process still holds `pgid` (probes with signal 0). Used to
/// detect a surviving group after the direct child was reaped: a resistant
/// descendant keeps the group alive even though the leader exited.
pub fn group_alive(pgid: i32) -> bool {
    if pgid <= 0 {
        return false;
    }
    let rc = unsafe { libc::killpg(pgid, 0) };
    if rc == 0 {
        return true;
    }
    unsafe { *libc::__errno_location() != libc::ESRCH }
}

fn signal_group(pgid: i32, sig: libc::c_int) {
    if pgid > 0 {
        unsafe {
            libc::killpg(pgid, sig);
        }
    }
}

/// Nonblocking reap attempt; `Ok(None)` means still running, `Err` means the
/// handle is unusable (treated as dead by callers).
#[allow(dead_code)]
pub fn reap_nonblocking(child: &mut Child) -> Result<Option<ExitStatus>, std::io::Error> {
    child.try_wait()
}

/// Supervised teardown of one process group: TERM, finite wait, KILL, reap.
///
/// `child` is `Some` while the direct child is unreaped and `None` once it
/// was already reaped (leader-exited case with a possibly surviving group).
/// The group is only reported dead once `group_alive(pgid)` is false, so a
/// stubborn descendant cannot orphan. Returns the direct child's exit code
/// when this call reaped it, else `None`.
pub fn teardown_group(
    pgid: i32,
    mut child: Option<&mut Child>,
    term_wait: Duration,
) -> Option<i32> {
    if pgid <= 0 {
        if let Some(c) = child {
            return c.try_wait().ok().flatten().map(|s| s.code().unwrap_or(1));
        }
        return None;
    }
    signal_group(pgid, libc::SIGTERM);
    let start = Instant::now();
    let mut code: Option<i32> = None;
    if let Some(c) = child.as_mut() {
        loop {
            match c.try_wait() {
                Ok(Some(status)) => {
                    code = Some(status.code().unwrap_or(1));
                    break;
                }
                Ok(None) => {
                    if start.elapsed() >= term_wait {
                        break;
                    }
                    std::thread::sleep(Duration::from_millis(10));
                }
                Err(_) => break,
            }
        }
    } else if !group_alive(pgid) {
        return None;
    }
    // Even with the direct child reaped, a resistant descendant may keep the
    // group alive: escalate and wait finitely for the whole group.
    let escalate_at = start + term_wait;
    loop {
        if !group_alive(pgid) {
            return code;
        }
        if Instant::now() >= escalate_at {
            break;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    signal_group(pgid, libc::SIGKILL);
    let kill_at = Instant::now() + term_wait.max(Duration::from_millis(200));
    // Reap the direct child once it is gone: without this the escalated
    // child stays a zombie even though the group is dead. `child` is only a
    // borrow so the owner still drops the handle afterwards.
    loop {
        if let Some(c) = child.as_mut() {
            match c.try_wait() {
                Ok(Some(status)) => {
                    code = Some(status.code().unwrap_or(1));
                }
                Ok(None) => {}
                Err(_) => {}
            }
        }
        if !group_alive(pgid) {
            break;
        }
        if Instant::now() >= kill_at {
            break;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    // Final nonblocking reap: the direct child may have exited with the
    // group already gone; never leave a zombie behind.
    if let Some(c) = child.as_mut() {
        if let Ok(Some(status)) = c.try_wait() {
            code = Some(status.code().unwrap_or(1));
        }
    }
    code
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::process::CommandExt;

    /// Spawn a group whose leader exits immediately while a TERM-resistant
    /// descendant survives it: `sh -c 'trap "" TERM; sleep 30 &'`. The `sh`
    /// exits at once; the `sleep` inherits the ignored TERM disposition and
    /// keeps the pgid alive until KILL.
    fn spawn_resistant_group() -> (Option<Child>, i32) {
        let mut cmd = Command::new("sh");
        cmd.args(["-c", "trap \"\" TERM; sleep 30 &"]);
        cmd.stdin(Stdio::null());
        cmd.stdout(Stdio::null());
        cmd.stderr(Stdio::null());
        unsafe {
            cmd.pre_exec(|| {
                libc::setpgid(0, 0);
                Ok(())
            });
        }
        let mut child = cmd.spawn().expect("sh spawn");
        let pgid = child.id() as i32;
        let status = child.wait().expect("leader reaped");
        assert!(status.success(), "leader must exit cleanly on its own");
        assert!(
            group_alive(pgid),
            "resistant descendant must outlive leader"
        );
        (None, pgid)
    }

    #[test]
    fn teardown_kills_group_after_leader_exit() {
        let (mut child_opt, pgid) = spawn_resistant_group();
        let code = teardown_group(pgid, child_opt.as_mut(), Duration::from_millis(200));
        assert_eq!(code, None, "already-reaped leader yields no new code");
        assert!(
            !group_alive(pgid),
            "TERM-resistant descendant must be KILLed, no orphan"
        );
    }

    #[test]
    fn teardown_kills_term_resistant_direct_child_and_reaps_no_zombie() {
        // The DIRECT child itself ignores TERM (`trap "" TERM` survives the
        // exec), so escalation must KILL it and the handle must be reaped:
        // otherwise the escalated child is left as a zombie.
        let mut cmd = Command::new("sh");
        cmd.args(["-c", "trap \"\" TERM; exec sleep 30"]);
        cmd.stdin(Stdio::null());
        cmd.stdout(Stdio::null());
        cmd.stderr(Stdio::null());
        unsafe {
            cmd.pre_exec(|| {
                libc::setpgid(0, 0);
                Ok(())
            });
        }
        let mut child = cmd.spawn().expect("sh spawn");
        let pgid = child.id() as i32;
        // Give the exec a moment so the resistant disposition is in place.
        std::thread::sleep(Duration::from_millis(200));
        assert!(group_alive(pgid), "test child must be running");
        teardown_group(pgid, Some(&mut child), Duration::from_millis(200));
        assert!(
            !group_alive(pgid),
            "TERM-resistant direct child must be KILLed, no orphan"
        );
        assert!(
            child.try_wait().expect("reapable").is_some(),
            "escalated direct child must be reaped, no zombie"
        );
    }

    #[test]
    fn teardown_reaps_live_child_and_group() {
        let mut cmd = Command::new("sleep");
        cmd.arg("30");
        cmd.stdin(Stdio::null());
        cmd.stdout(Stdio::null());
        cmd.stderr(Stdio::null());
        unsafe {
            cmd.pre_exec(|| {
                libc::setpgid(0, 0);
                Ok(())
            });
        }
        let mut child = cmd.spawn().expect("sleep spawn");
        let pgid = child.id() as i32;
        teardown_group(pgid, Some(&mut child), Duration::from_millis(200));
        assert!(
            !group_alive(pgid),
            "no surviving group after supervised teardown"
        );
    }

    fn palette_cfg() -> Config {
        Config {
            root: std::path::PathBuf::from("/tmp"),
            mode: "palette".to_string(),
            project: None,
            session: None,
            pending_name: None,
            new_session: false,
        }
    }

    fn isolated_env(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
            .collect()
    }

    #[test]
    fn palette_spawns_direct_pi_without_wrapper_or_continue() {
        // Explicit isolated env; never touches process globals, never
        // launches real pi here.
        let base = isolated_env(&[("PI_CODING_AGENT_SESSION_DIR", "/tmp/fake-pool")]);
        let (cmd, _env) =
            build_pi_command_with_env(&palette_cfg(), "/tmp/s.jsonl", "draft", false, &base);
        assert_eq!(&cmd[0..4], ["pi", "--mode", "rpc", "--approve"]);
        assert!(
            !cmd.iter().any(|a| a.contains("sessions.py")),
            "palette must not use scoped wrappers"
        );
        assert!(!cmd.contains(&"--continue".to_string()));
        assert!(!cmd.contains(&"--new-session".to_string()));
        assert!(!cmd.contains(&"--pending-name".to_string()));
        let dir_pos = cmd
            .iter()
            .position(|a| a == "--session-dir")
            .expect("session-dir");
        assert_eq!(cmd[dir_pos + 1], "/tmp/fake-pool");
        let sess_pos = cmd
            .iter()
            .position(|a| a == "--session")
            .expect("--session");
        assert_eq!(cmd[sess_pos + 1], "/tmp/s.jsonl");
    }

    #[test]
    fn palette_omits_session_dir_when_unset_and_names_empty_file() {
        let base = isolated_env(&[]);
        let (cmd, _) =
            build_pi_command_with_env(&palette_cfg(), "", "  New   draft  ", false, &base);
        assert!(!cmd.iter().any(|a| a == "--session-dir"));
        assert!(!cmd.iter().any(|a| a == "--session"));
        let name_pos = cmd.iter().position(|a| a == "--name").expect("--name");
        assert_eq!(cmd[name_pos + 1], "New draft");
    }

    #[test]
    fn palette_fresh_never_resumes_stale_session() {
        let base = isolated_env(&[]);
        // Fresh New with a stale cached path must not pass --session.
        let (cmd, _) =
            build_pi_command_with_env(&palette_cfg(), "/tmp/stale.jsonl", "", true, &base);
        assert!(
            !cmd.iter().any(|a| a == "--session"),
            "fresh must not resume"
        );
        assert!(!cmd.iter().any(|a| a == "--continue"));
        // Fresh with a pending name carries it as --name (bounded).
        let (cmd2, _) =
            build_pi_command_with_env(&palette_cfg(), "/tmp/stale.jsonl", "hello", true, &base);
        assert!(!cmd2.iter().any(|a| a == "--session"));
        let pos = cmd2.iter().position(|a| a == "--name").expect("--name");
        assert_eq!(cmd2[pos + 1], "hello");
    }

    #[test]
    fn palette_env_clears_scope_but_retains_graph_choice() {
        let base = isolated_env(&[
            ("QS_PROJECT_PATH", "pages/P.md"),
            ("QS_PROJECT_SESSION_SCOPE", "/tmp/scope"),
            ("QS_JOURNAL_MODE", "1"),
            ("QS_JOURNAL_SESSION_SCOPE", "/tmp/jscope"),
            ("LOGSEQ_GRAPH", "/tmp/graph"),
            ("PI_CODING_AGENT_SESSION_DIR", "/tmp/pool"),
        ]);
        let (_, env) = build_pi_command_with_env(&palette_cfg(), "", "", false, &base);
        assert!(!env.contains_key("QS_PROJECT_PATH"));
        assert!(!env.contains_key("QS_PROJECT_SESSION_SCOPE"));
        assert!(!env.contains_key("QS_JOURNAL_MODE"));
        assert!(!env.contains_key("QS_JOURNAL_SESSION_SCOPE"));
        assert_eq!(
            env.get("LOGSEQ_GRAPH").map(|s| s.as_str()),
            Some("/tmp/graph")
        );
        assert_eq!(
            env.get("PI_CODING_AGENT_SESSION_DIR").map(|s| s.as_str()),
            Some("/tmp/pool")
        );
    }

    #[test]
    fn bounded_name_collapses_and_caps_like_wrapper() {
        assert_eq!(bounded_name("  New   draft  "), "New draft");
        assert_eq!(bounded_name(""), "");
        assert_eq!(bounded_name("   "), "");
        let long = "x".repeat(500);
        assert_eq!(bounded_name(&long).chars().count(), 120);
    }
}
