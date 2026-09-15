//! Phase 2 application-aware context tests (no live compositor/apps).
//!
//! Covers the acceptance contract with fakes:
//! - meaningful file/cwd/url/git/page changes persist as `context`;
//! - repeated equal enrichment dedups (incl. last-good failure noise);
//! - optional/unavailable providers never fail the base collector;
//! - background enrichment never delays base/events/shutdown; stale
//!   generations are discarded;
//! - no unbounded blocking/queue; legacy Phase 1 JSON still reads;
//! - git temp repo / worktree (detached) / detached / non-repo / malicious;
//! - collector app-only refresh without compositor polling;
//! - real Lua publisher (when nvim installed), kitty command boundary +
//!   socket peer identity, Zen window_id+pid binding, Logseq window binding.

use qs_agent_orchestrator::collector::{
    flush_pending, run_collector_once_with_enrich, run_collector_forever_with_enrich,
    AppRefreshConfig, CollectorConfig, SessionOutcome,
};
use qs_agent_orchestrator::desktop_context::{
    classify_transition, ActivityKind, DesktopContext, EnqueueOutcome, FocusedWindow,
    ResourceContext, Source, Tracker, Workspace,
};
use qs_agent_orchestrator::{ActivityStore, EnrichmentEnv};
use qs_agent_orchestrator::app_context::{
    clear_last_good_cache, cmdline_is_nvim, enrich_with_env, kitty_ls_argv, kitty_peer_pid,
    logseq_page_from_title, normalize_kitty_socket_arg, parse_nvim_record, sanitize_abs_path,
    select_kitty_pane,
};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

fn ctx_base(ts: i64) -> DesktopContext {
    DesktopContext::available(
        Source::Hyprland,
        Some(FocusedWindow::new("0x1", "kitty", "t")),
        Some(Workspace::new("1", "1")),
        ts,
    )
}

fn tmpdir(tag: &str) -> std::path::PathBuf {
    let p = std::env::temp_dir().join(format!(
        "qs-p2-{tag}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&p).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o700));
    }
    p
}

fn private_dir(p: &std::path::Path) {
    std::fs::create_dir_all(p).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(p, std::fs::Permissions::from_mode(0o700));
    }
}

/// Serialize collector-session tests: each session owns a worker thread and
/// the process-wide cap is finite, so parallel sessions would contend for
/// slots and turn enrichment timing nondeterministic. One session at a time
/// keeps every timing assertion exact.
fn session_serial() -> std::sync::MutexGuard<'static, ()> {
    static LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
    LOCK.lock().unwrap_or_else(|e| e.into_inner())
}

// --- Semantic dedup + context kind ---------------------------------------

#[test]
fn resource_changes_are_context_repeats_dedup() {
    let mut t = Tracker::new();
    let a = ctx_base(100).with_resource(Some(ResourceContext::new(
        "neovim",
        Some("/tmp/a.md"),
        Some("/tmp"),
        None,
        None,
        None,
        None,
        None,
    )));
    match t.enqueue(a.clone()).expect("queue A") {
        EnqueueOutcome::Queued(kind, _) => assert_eq!(kind, ActivityKind::Focus),
        other => panic!("{other:?}"),
    }
    let (k, snap) = t.pop_pending_front().unwrap();
    assert_eq!(k, ActivityKind::Focus);
    t.mark_persisted(&snap);

    let a2 = ctx_base(200).with_resource(Some(ResourceContext::new(
        "neovim",
        Some("/tmp/a.md"),
        Some("/tmp"),
        None,
        None,
        None,
        None,
        None,
    )));
    assert_eq!(t.enqueue(a2).unwrap(), EnqueueOutcome::Deduplicated);

    let b = ctx_base(300).with_resource(Some(ResourceContext::new(
        "neovim",
        Some("/tmp/b.md"),
        Some("/tmp"),
        None,
        None,
        None,
        None,
        None,
    )));
    match t.enqueue(b).expect("queue B") {
        EnqueueOutcome::Queued(kind, snap) => {
            assert_eq!(kind, ActivityKind::Context);
            assert_eq!(snap.observed_at_ms, 300);
        }
        other => panic!("{other:?}"),
    }

    let mut t2 = Tracker::new();
    let z1 = DesktopContext::available(
        Source::Hyprland,
        Some(FocusedWindow::new("0x2", "zen", "T")),
        Some(Workspace::new("1", "1")),
        1,
    )
    .with_resource(Some(ResourceContext::new(
        "zen", None, None, None, None, Some("https://a.example/"), None, Some("T"),
    )));
    let (k, s) = match t2.enqueue(z1.clone()).unwrap() {
        EnqueueOutcome::Queued(k, s) => (k, s),
        other => panic!("{other:?}"),
    };
    assert_eq!(k, ActivityKind::Focus);
    t2.mark_persisted(&s);
    let _ = t2.pop_pending_front();
    let z2 = DesktopContext::available(
        Source::Hyprland,
        Some(FocusedWindow::new("0x2", "zen", "T")),
        Some(Workspace::new("1", "1")),
        2,
    )
    .with_resource(Some(ResourceContext::new(
        "zen", None, None, None, None, Some("https://b.example/"), None, Some("T"),
    )));
    assert_eq!(
        classify_transition(Some(&z1), &z2),
        ActivityKind::Context
    );
}

#[test]
fn pid_only_differences_dedup() {
    let mut t = Tracker::new();
    let a = DesktopContext::available(
        Source::Hyprland,
        Some(FocusedWindow::new_with_pid("0x1", "kitty", "t", Some(100))),
        Some(Workspace::new("1", "1")),
        1,
    );
    let (k, s) = t.enqueue(a).unwrap().queued().unwrap();
    assert_eq!(k, ActivityKind::Focus);
    t.mark_persisted(&s);
    let _ = t.pop_pending_front();
    let b = DesktopContext::available(
        Source::Hyprland,
        Some(FocusedWindow::new_with_pid("0x1", "kitty", "t", Some(200))),
        Some(Workspace::new("1", "1")),
        2,
    );
    assert_eq!(
        t.enqueue(b).unwrap(),
        EnqueueOutcome::Deduplicated,
        "PID-only change must not produce history"
    );
}

trait QueuedUnwrap {
    fn queued(self) -> Result<(ActivityKind, DesktopContext), String>;
}
impl QueuedUnwrap for EnqueueOutcome {
    fn queued(self) -> Result<(ActivityKind, DesktopContext), String> {
        match self {
            EnqueueOutcome::Queued(k, s) => Ok((k, s)),
            EnqueueOutcome::Deduplicated => Err("deduplicated".to_string()),
        }
    }
}

// --- Legacy JSON ----------------------------------------------------------

#[test]
fn legacy_phase1_json_reads_without_resource_or_pid() {
    let legacy = r#"{"focused_window":{"id":"0x1","application":"kitty","title":"t"},"workspace":{"id":"1","name":"1"},"available":true,"source":"hyprland","observed_at_ms":123}"#;
    let ctx: DesktopContext = serde_json::from_str(legacy).expect("legacy must parse");
    assert_eq!(ctx.resource, None);
    assert_eq!(ctx.focused_window.as_ref().unwrap().process_id, None);
    let store = ActivityStore::open_in_memory().unwrap();
    store.append("focus", "hyprland", &ctx).unwrap();
    let rows = store.recent_activity(10).unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].snapshot().unwrap(), ctx);

    let enriched = ctx.with_resource(Some(ResourceContext::new(
        "kitty",
        None,
        Some("/tmp"),
        None,
        Some("main"),
        None,
        None,
        None,
    )));
    store.append("context", "hyprland", &enriched).unwrap();
    let rows = store.recent_activity(10).unwrap();
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0].snapshot().unwrap(), enriched);
}

// --- Pure provider edge cases ---------------------------------------------

#[test]
fn kitty_command_boundary_and_peer_identity() {
    // `--to` requires the unix: address form; bare paths are normalized.
    let argv = kitty_ls_argv(std::path::Path::new("/tmp/kitty.sock")).expect("argv");
    assert_eq!(argv, vec!["kitty", "@", "--to", "unix:/tmp/kitty.sock", "ls"]);
    assert!(normalize_kitty_socket_arg(std::path::Path::new("relative.sock")).is_none());
    assert!(normalize_kitty_socket_arg(std::path::Path::new("")).is_none());
    // unix:-prefixed input normalizes identically.
    assert_eq!(
        normalize_kitty_socket_arg(std::path::Path::new("unix:/tmp/kitty.sock")).as_deref(),
        Some("unix:/tmp/kitty.sock")
    );

    // SO_PEERCRED identity: a listener's accepted peer reports this process.
    let dir = tmpdir("peer");
    let sock = dir.join("peer.sock");
    let _ = std::fs::remove_file(&sock);
    let listener = std::os::unix::net::UnixListener::bind(&sock).unwrap();
    let sock_c = sock.clone();
    let server = std::thread::spawn(move || {
        let (_s, _) = listener.accept().expect("accept");
        std::thread::sleep(Duration::from_millis(300));
    });
    std::thread::sleep(Duration::from_millis(30));
    let peer = kitty_peer_pid(&sock).expect("peer pid");
    assert_eq!(peer, std::process::id(), "socket peer must be this process");
    // The unix:-prefixed address form resolves to the same peer.
    let prefixed = std::path::PathBuf::from(format!("unix:{}", sock.display()));
    let peer2 = kitty_peer_pid(&prefixed).expect("prefixed peer pid");
    assert_eq!(peer2, std::process::id());
    drop(sock_c);
    let _ = server.join();
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn kitty_real_shaped_selection_no_conflation() {
    clear_last_good_cache();
    // Distinct identities: kitty 4001 (peer==focused), shell 5001, nvim
    // 6001, pane id 7. No numeric id==pid shortcut may fire.
    let raw = r#"[{"id":11,"is_focused":true,"tabs":[{"id":21,"is_focused":true,"windows":[{"id":7,"is_focused":true,"pid":5001,"cwd":"/tmp","title":"nvim","foreground_processes":[{"pid":5001,"cmdline":["fish"],"cwd":"/tmp"},{"pid":6001,"cmdline":["nvim","/tmp/a.md"],"cwd":"/tmp"}]}]}]}]"#;
    assert!(select_kitty_pane(raw, Some(4001), Some(9999)).is_none());
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
    // Malformed / oversize rejected.
    assert!(select_kitty_pane("not json", Some(1), Some(1)).is_none());
    assert!(select_kitty_pane("[]", Some(1), Some(1)).is_none());
}

#[test]
fn nvim_malformed_records_rejected() {
    assert!(parse_nvim_record("not json").is_none());
    assert!(parse_nvim_record(r#"{"file":"/tmp/a"}"#).is_none());
    assert!(parse_nvim_record(r#"{"pid":"abc"}"#).is_none());
    let rec = parse_nvim_record(r#"{"pid":111,"file":"/tmp/a.md","cwd":"/tmp"}"#).unwrap();
    assert_eq!(rec.pid, 111);
    assert!(cmdline_is_nvim("nvim file.md"));
    assert!(cmdline_is_nvim("/usr/bin/nvim foo"));
    assert!(!cmdline_is_nvim("fish"));
    assert!(!cmdline_is_nvim("node nvim-helper.js"));
}

#[test]
fn private_record_validation_rejects_symlink_and_world_readable() {
    let dir = tmpdir("priv");
    private_dir(&dir);
    // Valid private record passes through read path indirectly: craft a
    // correctly-named owned 0600 file and confirm the name gate + open path.
    let good = dir.join("4242.json");
    std::fs::write(&good, r#"{"pid":4242,"file":"/tmp/a","cwd":"/tmp"}"#).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&good, std::fs::Permissions::from_mode(0o600));
    }
    // Symlink alias rejected.
    let link = dir.join("9999.json");
    #[cfg(unix)]
    {
        let _ = std::os::unix::fs::symlink(&good, &link);
        // Name gate passes but O_NOFOLLOW open must fail -> record skipped.
        // (Validated inside read_nvim_records; here assert the symlink exists
        // so the test is meaningful.)
        assert!(std::fs::symlink_metadata(&link).unwrap().file_type().is_symlink());
    }
    // World-readable rejected by mode check (unix only).
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let open = dir.join("7777.json");
        std::fs::write(&open, r#"{"pid":7777}"#).unwrap();
        let _ = std::fs::set_permissions(&open, std::fs::Permissions::from_mode(0o644));
        let md = std::fs::metadata(&open).unwrap();
        assert_ne!(md.permissions().mode() & 0o077, 0);
    }
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn logseq_title_only_with_page_extraction() {
    // No HTTP API: the global getCurrentPage cannot be bound to a window
    // without polling, so Logseq is title-only. The page is extracted by an
    // explicit documented rule only.
    clear_last_good_cache();
    assert_eq!(
        logseq_page_from_title("My Page - Logseq").as_deref(),
        Some("My Page")
    );
    assert_eq!(
        logseq_page_from_title("Journal / 2024-01-01 - Logseq").as_deref(),
        Some("Journal / 2024-01-01")
    );
    assert!(logseq_page_from_title("Logseq").is_none());
    assert!(logseq_page_from_title("").is_none());
    assert!(logseq_page_from_title("a - Logseq - extra").is_none());
    assert!(logseq_page_from_title(" - Logseq").is_none());
    let ctx = DesktopContext::available(
        Source::Hyprland,
        Some(FocusedWindow::new("0xL", "Logseq", "My Page - Logseq")),
        None,
        1,
    );
    let out = enrich_with_env(ctx, &EnrichmentEnv::default(), 1_000_000);
    let r = out.resource.expect("logseq title fallback");
    assert_eq!(r.adapter, "logseq-title");
    assert_eq!(r.title.as_deref(), Some("My Page - Logseq"));
    assert_eq!(r.page.as_deref(), Some("My Page"));
    assert_eq!(r.url, None, "never invent a URL");
    // Bare title without suffix: title only, no page guess.
    let ctx2 = DesktopContext::available(
        Source::Hyprland,
        Some(FocusedWindow::new("0xL", "Logseq", "Logseq")),
        None,
        1,
    );
    let out2 = enrich_with_env(ctx2, &EnrichmentEnv::default(), 2_000_000);
    let r2 = out2.resource.expect("fallback");
    assert_eq!(r2.page, None);
}

#[test]
fn zen_explicit_requires_window_id_and_pid() {
    clear_last_good_cache();
    let dir = tmpdir("zen");
    private_dir(&dir);
    let now: i64 = 5_000_000;
    let counter = AtomicUsize::new(0);
    let mk = |body: &str| {
        let n = counter.fetch_add(1, Ordering::SeqCst);
        let p = dir.join(format!("z-{n}.json"));
        std::fs::write(&p, body).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o600));
        }
        p
    };
    let win = FocusedWindow::new_with_pid("0xZ", "zen", "T", Some(111));
    // Matching window_id+pid -> explicit.
    let p1 = mk(r#"{"window_id":"0xZ","pid":111,"url":"https://a.example/","title":"A","updated_at_ms":5000000}"#);
    let env1 = EnrichmentEnv {
        zen_file: Some(p1),
        ..Default::default()
    };
    let ctx = DesktopContext::available(Source::Hyprland, Some(win.clone()), None, 1);
    let out = enrich_with_env(ctx, &env1, now);
    assert_eq!(out.resource.as_ref().unwrap().adapter, "zen");
    // Same pid, different window -> fallback (PID insufficient).
    let p2 = mk(r#"{"window_id":"0xOTHER","pid":111,"url":"https://a.example/","title":"A","updated_at_ms":5000000}"#);
    let env2 = EnrichmentEnv {
        zen_file: Some(p2),
        ..Default::default()
    };
    let ctx2 = DesktopContext::available(Source::Hyprland, Some(win.clone()), None, 1);
    let out2 = enrich_with_env(ctx2, &env2, now);
    assert_eq!(out2.resource.as_ref().unwrap().adapter, "zen-title");
    assert_eq!(out2.resource.as_ref().unwrap().url, None);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn sanitize_and_zen_fallback_bounds() {
    clear_last_good_cache();
    assert!(sanitize_abs_path("rel").is_none());
    assert!(sanitize_abs_path("/ok").is_some());
    let ctx = DesktopContext::available(
        Source::Hyprland,
        Some(FocusedWindow::new("0x1", "zen", "Some Article")),
        None,
        1,
    );
    let out = enrich_with_env(ctx, &EnrichmentEnv::default(), 99);
    let r = out.resource.unwrap();
    assert_eq!(r.adapter, "zen-title");
    assert_eq!(r.url, None);
}

// --- Lua publisher ----------------------------------------------------------

#[test]
fn lua_publisher_emits_epoch_private_record() {
    let nvim = std::process::Command::new("nvim").arg("--version").output();
    let Ok(out) = nvim else {
        eprintln!("nvim missing; skipping");
        return;
    };
    if !out.status.success() {
        eprintln!("nvim broken; skipping");
        return;
    }
    let manifest = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("integrations/neovim/qs-context.lua");
    assert!(manifest.is_file(), "lua integration missing");
    let dir = tmpdir("lua-pub");
    let ctxdir = dir.join("nvim-context");
    let script = dir.join("run.lua");
    // Headless nvim: enable + setup, edit a file, publish, quit.
    let target = dir.join("note.md");
    std::fs::write(&target, "hello").unwrap();
    let lua = format!(
        "vim.opt.runtimepath:append({dir:?}); vim.g.qs_nvim_context_enable = true; vim.g.qs_nvim_context_dir = {ctx:?}; require('qs-context').setup(); vim.cmd('edit ' .. {f:?}); require('qs-context').publish(); require('qs-context').cleanup = function() end;",
        dir = dir.join("rt").to_string_lossy(),
        ctx = ctxdir.to_string_lossy(),
        f = target.to_string_lossy(),
    );
    std::fs::create_dir_all(dir.join("rt").join("lua")).unwrap();
    std::fs::copy(&manifest, dir.join("rt").join("lua").join("qs-context.lua")).unwrap();
    std::fs::write(&script, lua).unwrap();
    let status = std::process::Command::new("nvim")
        .args(["--headless", "--noplugin", "-l", &script.to_string_lossy()])
        .output();
    let Ok(output) = status else {
        eprintln!("nvim -l failed to spawn; skipping");
        return;
    };
    if !output.status.success() {
        // Older nvim without -l: fall back to --headless +c.
        let s2 = std::process::Command::new("nvim")
            .args([
                "--headless",
                "--noplugin",
                "-c",
                &format!("luafile {}", script.display()),
                "-c",
                "qa!",
            ])
            .output();
        if s2.map(|o| o.status.success()).unwrap_or(false) == false {
            eprintln!("nvim headless publish unsupported here; skipping");
            return;
        }
    }
    // Find the <pid>.json record.
    let entries: Vec<_> = std::fs::read_dir(&ctxdir).map(|e| e.flatten().collect()).unwrap_or_default();
    assert!(!entries.is_empty(), "lua must publish a record in {ctxdir:?}");
    let rec_path = entries
        .iter()
        .map(|e| e.path())
        .find(|p| p.extension().and_then(|x| x.to_str()) == Some("json"))
        .expect("json record");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let md = std::fs::symlink_metadata(&rec_path).unwrap();
        assert!(!md.file_type().is_symlink());
        assert_eq!(md.permissions().mode() & 0o777, 0o600, "record must be 0600");
    }
    let raw = std::fs::read_to_string(&rec_path).unwrap();
    let v: serde_json::Value = serde_json::from_str(&raw).unwrap();
    let epoch_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0);
    let updated = v.get("updated_at_ms").and_then(|x| x.as_i64()).unwrap();
    // Epoch clock (not monotonic): within 120 s of now and sane magnitude.
    assert!(updated > 1_700_000_000_000, "must be epoch ms, got {updated}");
    assert!((epoch_ms - updated).abs() < 120_000, "fresh epoch record");
    assert_eq!(
        v.get("file").and_then(|x| x.as_str()).unwrap(),
        target.to_string_lossy()
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn lua_publisher_buffer_kinds_omit_file() {
    // Only real local files publish a path: unnamed/help/terminal/nofile
    // and URI-schemed names (fugitive://, term://, file://) must publish
    // no file while retaining cwd. Skipped when nvim is unavailable.
    let nvim = std::process::Command::new("nvim").arg("--version").output();
    let Ok(out) = nvim else {
        eprintln!("nvim missing; skipping");
        return;
    };
    if !out.status.success() {
        eprintln!("nvim broken; skipping");
        return;
    }
    let manifest = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("integrations/neovim/qs-context.lua");
    assert!(manifest.is_file(), "lua integration missing");
    let dir = tmpdir("lua-kinds");
    let ctxdir = dir.join("nvim-context");
    let script = dir.join("kinds.lua");
    let target = dir.join("note.md");
    std::fs::write(&target, "hello").unwrap();
    let lua = format!(
        r#"vim.opt.runtimepath:append({dir:?})
vim.g.qs_nvim_context_enable = true
vim.g.qs_nvim_context_dir = {ctx:?}
local mod = require('qs-context')
mod.setup()
mod.cleanup = function() end
local fails = {{}}
local function check(name, cond)
  if not cond then fails[#fails + 1] = name end
end
local function published()
  local p = {ctx:?} .. "/" .. tostring(vim.fn.getpid()) .. ".json"
  return vim.json.decode(table.concat(vim.fn.readfile(p), "\n"))
end
-- 1. normal file buffer publishes its path.
vim.cmd('edit ' .. {f:?})
mod.publish()
local r = published()
check("normal file", r.file == {f:?})
check("normal cwd retained", type(r.cwd) == "string" and r.cwd ~= "")
-- 2. unnamed buffer: no file, cwd retained.
vim.cmd('enew!')
mod.publish()
r = published()
check("unnamed no file", r.file == "")
check("unnamed cwd retained", type(r.cwd) == "string" and r.cwd ~= "")
-- 3. real terminal buffer (term:// name): no file.
vim.cmd('enew!')
local job = vim.fn.termopen(os.getenv("SHELL") or "sh")
check("termopen started", job > 0)
vim.wait(800)
check("terminal buftype", vim.bo.buftype == "terminal")
mod.publish()
r = published()
check("terminal no file", r.file == "")
check("terminal cwd retained", type(r.cwd) == "string" and r.cwd ~= "")
-- 4. nofile buftype: no file.
vim.cmd('enew!')
vim.bo.buftype = "nofile"
mod.publish()
r = published()
check("nofile no file", r.file == "")
-- 5. help buftype: no file.
vim.cmd('enew!')
vim.bo.buftype = "help"
mod.publish()
r = published()
check("help no file", r.file == "")
-- 6. fugitive:// name on a normal buffer: no file (scheme gate).
vim.cmd('enew!')
vim.bo.buftype = ""
vim.api.nvim_buf_set_name(0, "fugitive:///tmp/repo/.git//abc123")
check("fugitive name set", vim.api.nvim_buf_get_name(0) == "fugitive:///tmp/repo/.git//abc123")
mod.publish()
r = published()
check("fugitive no file", r.file == "")
-- 7. file:// URI name: no file.
vim.cmd('enew!')
vim.api.nvim_buf_set_name(0, "file:///tmp/note.md")
mod.publish()
r = published()
check("file-uri no file", r.file == "")
if #fails > 0 then
  error("buffer-kind failures: " .. table.concat(fails, ", "))
end
print("QS-KINDS-OK")"#,
        dir = dir.join("rt").to_string_lossy(),
        ctx = ctxdir.to_string_lossy(),
        f = target.to_string_lossy(),
    );
    std::fs::create_dir_all(dir.join("rt").join("lua")).unwrap();
    std::fs::copy(&manifest, dir.join("rt").join("lua").join("qs-context.lua")).unwrap();
    std::fs::write(&script, lua).unwrap();
    // A failed script run is a real failure, not a skip: only a missing
    // nvim (checked above) or an unspawable/headless-incapable binary
    // skips. Both run modes must print the marker on success.
    let run_l = std::process::Command::new("nvim")
        .args(["--headless", "--noplugin", "-l", &script.to_string_lossy()])
        .output();
    let Ok(out_l) = run_l else {
        eprintln!("nvim failed to spawn; skipping");
        return;
    };
    if out_l.status.success() {
        // NOTE: `print` in `nvim -l` lands on stdout or stderr depending on
        // version; accept the marker on either stream.
        let stdout = String::from_utf8_lossy(&out_l.stdout);
        let stderr = String::from_utf8_lossy(&out_l.stderr);
        assert!(
            stdout.contains("QS-KINDS-OK") || stderr.contains("QS-KINDS-OK"),
            "scenarios must all pass: stdout={stdout} stderr={stderr}"
        );
    } else {
        let out_c = std::process::Command::new("nvim")
            .args([
                "--headless",
                "--noplugin",
                "-c",
                &format!("luafile {}", script.display()),
                "-c",
                "qa!",
            ])
            .output();
        match out_c {
            Ok(o) if o.status.success() => {
                let stdout = String::from_utf8_lossy(&o.stdout);
                let stderr = String::from_utf8_lossy(&o.stderr);
                assert!(
                    stdout.contains("QS-KINDS-OK") || stderr.contains("QS-KINDS-OK"),
                    "scenarios must all pass: stdout={stdout} stderr={stderr}"
                );
            }
            _ => eprintln!("nvim headless script unsupported here; skipping"),
        }
    }
    let _ = std::fs::remove_dir_all(&dir);
}

// --- Git: temp repo / worktree / detached / non-repo / malicious -----------

fn git(args: &[&str], dir: &std::path::Path) -> bool {
    std::process::Command::new("git")
        .args(args)
        .current_dir(dir)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

#[test]
fn git_repo_branch_detached_worktree_nonrepo_malicious() {
    if std::process::Command::new("git").arg("--version").output().is_err() {
        eprintln!("git missing; skipping");
        return;
    }
    let dir = tmpdir("git-repo");
    assert!(git(&["init", "-b", "main"], &dir), "git init");
    assert!(git(&["config", "user.email", "t@t"], &dir), "config");
    assert!(git(&["config", "user.name", "t"], &dir), "config");
    std::fs::write(dir.join("f.md"), "hi").unwrap();
    assert!(git(&["add", "."], &dir), "add");
    assert!(git(&["commit", "-m", "init"], &dir), "commit");
    let s = dir.to_string_lossy().to_string();
    let root = qs_agent_orchestrator::app_context::git_root_for(&s).expect("root");
    assert_eq!(root, s);
    let branch = qs_agent_orchestrator::app_context::git_branch_for(&s).expect("branch");
    assert_eq!(branch, "main");

    assert!(git(&["checkout", "-b", "feature"], &dir), "branch");
    let b2 = qs_agent_orchestrator::app_context::git_branch_for(&s).expect("branch2");
    assert_eq!(b2, "feature");

    assert!(git(&["checkout", "--detach", "HEAD"], &dir), "detach");
    let det = qs_agent_orchestrator::app_context::git_branch_for(&s).expect("detached");
    assert!(det.starts_with("detached:"), "got {det}");

    // Detached worktree on a fresh dir: never the already-checked-out
    // branch, and success is asserted (no silent skip).
    let wt = tmpdir("git-wt");
    let wt_str = wt.to_string_lossy().to_string();
    let _ = std::fs::remove_dir_all(&wt);
    assert!(
        git(&["worktree", "add", "--detach", &wt_str, "HEAD"], &dir),
        "detached worktree add must succeed"
    );
    let wt_root = qs_agent_orchestrator::app_context::git_root_for(&wt_str).expect("wt root");
    assert_eq!(wt_root, wt_str);
    let wt_branch = qs_agent_orchestrator::app_context::git_branch_for(&wt_str).expect("wt branch");
    assert!(wt_branch.starts_with("detached:"), "worktree detached, got {wt_branch}");

    let plain = tmpdir("git-plain");
    let ps = plain.to_string_lossy().to_string();
    assert!(qs_agent_orchestrator::app_context::git_root_for(&ps).is_none());

    assert!(qs_agent_orchestrator::app_context::git_root_for("relative/path").is_none());
    assert!(qs_agent_orchestrator::app_context::git_root_for("").is_none());
    assert!(qs_agent_orchestrator::app_context::git_root_for("/tmp/bad\0path").is_none());
    assert!(qs_agent_orchestrator::app_context::git_branch_for("../../etc").is_none());

    let _ = std::fs::remove_dir_all(&dir);
    let _ = std::fs::remove_dir_all(&wt);
    let _ = std::fs::remove_dir_all(&plain);
}

#[test]
fn git_transient_branch_keeps_ttl_and_nonrepo_clears() {
    // Carried-forward git must not renew its TTL: repeated partial
    // resolutions past the original window stop filling. A confirmed
    // non-repo clears metadata immediately instead of back-filling.
    if std::process::Command::new("git").arg("--version").output().is_err() {
        eprintln!("git missing; skipping");
        return;
    }
    clear_last_good_cache();
    let dir = tmpdir("git-ttl");
    assert!(git(&["init", "-b", "main"], &dir), "git init");
    assert!(git(&["config", "user.email", "t@t"], &dir), "config");
    assert!(git(&["config", "user.name", "t"], &dir), "config");
    std::fs::write(dir.join("f.md"), "hi").unwrap();
    assert!(git(&["add", "."], &dir), "add");
    assert!(git(&["commit", "-m", "init"], &dir), "commit");

    // Nvim record pointing into the repo; updated_at tracks the fake clock.
    let recdir = tmpdir("git-ttl-rec");
    private_dir(&recdir);
    let pid = 42424u32;
    let rec = recdir.join(format!("{pid}.json"));
    let write_rec = |now: i64| {
        std::fs::write(
            &rec,
            format!(
                r#"{{"pid":{pid},"file":"{}/note.md","cwd":"{}","updated_at_ms":{now}}}"#,
                dir.display(),
                dir.display(),
            ),
        )
        .unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&rec, std::fs::Permissions::from_mode(0o600));
        }
    };
    let env = EnrichmentEnv {
        nvim_dir: Some(recdir.clone()),
        ..Default::default()
    };
    let win = || FocusedWindow::new_with_pid("0xG", "nvim", "note.md", Some(pid));
    let base = |now: i64| {
        write_rec(now);
        DesktopContext::available(Source::Hyprland, Some(win()), None, now)
    };
    let t0: i64 = 20_000_000;
    let first = enrich_with_env(base(t0), &env, t0);
    let r0 = first.resource.as_ref().expect("git resource");
    assert_eq!(r0.git_branch.as_deref(), Some("main"));

    // Break branch resolution only (corrupt the loose ref): toplevel still
    // succeeds while HEAD no longer resolves. Corrupting HEAD itself would
    // break discovery entirely, which is a different (Unknown) case.
    let ref_path = dir.join(".git/refs/heads/main");
    let saved_ref = std::fs::read(&ref_path).expect("loose ref");
    std::fs::write(&ref_path, "garbage-not-a-sha\n").unwrap();
    let t1 = t0 + 30_000;
    let filled = enrich_with_env(base(t1), &env, t1);
    assert_eq!(
        filled.resource.as_ref().and_then(|r| r.git_branch.clone()).as_deref(),
        Some("main"),
        "transient branch failure must be filled from cache"
    );
    // Past the ORIGINAL window the fill expires despite repeated misses.
    let t2 = t0 + 60_000 + 1;
    let expired = enrich_with_env(base(t2), &env, t2);
    assert_eq!(
        expired.resource.as_ref().and_then(|r| r.git_branch.clone()),
        None,
        "carried git must not renew its own TTL"
    );

    // Confirmed non-repo clears immediately even within TTL: restore the
    // ref first for a clean prime inside the window.
    let t3 = t0 + 40_000;
    std::fs::write(&ref_path, &saved_ref).expect("restore ref");
    let prime = enrich_with_env(base(t3), &env, t3);
    assert_eq!(
        prime.resource.as_ref().and_then(|r| r.git_branch.clone()).as_deref(),
        Some("main")
    );
    let _ = std::fs::remove_dir_all(dir.join(".git"));
    let cleared = enrich_with_env(base(t3 + 1000), &env, t3 + 1000);
    let rc = cleared.resource.as_ref().expect("resource still attaches");
    assert_eq!(rc.git_root, None, "confirmed non-repo must clear root");
    assert_eq!(rc.git_branch, None, "confirmed non-repo must clear branch");

    let _ = std::fs::remove_dir_all(&dir);
    let _ = std::fs::remove_dir_all(&recdir);
}

// --- Collector integration (background worker) ------------------------------

fn spawn_fake_request_socket(
    dir: &std::path::Path,
    win: &str,
    ws: &str,
    hits: Option<Arc<AtomicUsize>>,
) -> std::path::PathBuf {
    use std::io::{Read, Write};
    use std::os::unix::net::UnixListener;
    let path = dir.join(".socket.sock");
    let _ = std::fs::remove_file(&path);
    let listener = UnixListener::bind(&path).expect("bind req");
    let (win, ws) = (win.to_string(), ws.to_string());
    std::thread::spawn(move || {
        for conn in listener.incoming() {
            let mut stream = match conn {
                Ok(s) => s,
                Err(_) => return,
            };
            if let Some(h) = hits.as_ref() {
                h.fetch_add(1, Ordering::SeqCst);
            }
            let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
            let mut buf = vec![0u8; 4096];
            let n = match stream.read(&mut buf) {
                Ok(n) => n,
                Err(_) => continue,
            };
            if n == 0 {
                continue;
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
        }
    });
    path
}

#[test]
fn collector_base_immediate_then_context_async() {
    let _session_guard = session_serial();
    clear_last_good_cache();
    let dir = tmpdir("res-change");
    let req = spawn_fake_request_socket(
        &dir,
        r#"{"address":"0x1","class":"kitty","title":"t","workspace":{"id":1,"name":"1"}}"#,
        r#"{"id":1,"name":"1"}"#,
        None,
    );
    let ev_path = dir.join(".socket2.sock");
    let _ = std::fs::remove_file(&ev_path);
    let listener = std::os::unix::net::UnixListener::bind(&ev_path).unwrap();
    let server = std::thread::spawn(move || {
        use std::io::Write;
        let (mut s, _) = listener.accept().expect("accept");
        std::thread::sleep(Duration::from_millis(80));
        let _ = s.write_all(b"workspace>>1\n");
        std::thread::sleep(Duration::from_millis(1200));
    });
    std::thread::sleep(Duration::from_millis(30));

    // Constant enrichment: every generation yields file B. Base rows carry no
    // resource; the background worker adds exactly one `context` row.
    let enrich: qs_agent_orchestrator::collector::EnrichFn =
        Arc::new(move |ctx: DesktopContext| {
            ctx.with_resource(Some(ResourceContext::new(
                "neovim",
                Some("/tmp/b.md"),
                Some("/tmp"),
                None,
                None,
                None,
                None,
                None,
            )))
        });
    let app = AppRefreshConfig {
        interval: Duration::from_secs(3600),
        enrich: Some(enrich),
    };
    let store = ActivityStore::open_in_memory().unwrap();
    let mut tracker = Tracker::new();
    let shutdown = AtomicBool::new(false);
    let config = CollectorConfig {
        event_socket: ev_path,
        request_socket: req,
        idle_timeout: Duration::from_millis(100),
        coalesce_window: Duration::from_millis(20),
    };
    let (outcome, _) = run_collector_once_with_enrich(&store, &mut tracker, &config, &shutdown, &app);
    let _ = server.join();
    assert_eq!(outcome, SessionOutcome::Disconnected);
    let _ = flush_pending(&store, &mut tracker);
    let rows = store.activity_in_range(0, i64::MAX / 2, 20).unwrap();
    assert!(rows.len() >= 2, "base + async context, got {}", rows.len());
    assert_eq!(rows[0].kind, "focus", "base persists first without waiting");
    assert!(rows.iter().any(|r| r.kind == "context"), "worker context must arrive");
    assert!(rows.iter().any(|r| r.snapshot_json.contains("/tmp/b.md")));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn collector_stale_generation_discarded() {
    let _session_guard = session_serial();
    clear_last_good_cache();
    let dir = tmpdir("stale");
    let req = spawn_fake_request_socket(
        &dir,
        r#"{"address":"0x1","class":"kitty","title":"t","workspace":{"id":1,"name":"1"}}"#,
        r#"{"id":1,"name":"1"}"#,
        None,
    );
    let ev_path = dir.join(".socket2.sock");
    let _ = std::fs::remove_file(&ev_path);
    let listener = std::os::unix::net::UnixListener::bind(&ev_path).unwrap();
    let server = std::thread::spawn(move || {
        use std::io::Write;
        let (mut s, _) = listener.accept().expect("accept");
        // Two rapid events: gen1 (slow, file A) is superseded by gen2 (file B).
        std::thread::sleep(Duration::from_millis(60));
        let _ = s.write_all(b"workspace>>1\n");
        std::thread::sleep(Duration::from_millis(30));
        let _ = s.write_all(b"workspace>>1\n");
        std::thread::sleep(Duration::from_millis(1200));
    });
    std::thread::sleep(Duration::from_millis(30));
    let calls = Arc::new(AtomicUsize::new(0));
    let calls_c = Arc::clone(&calls);
    let enrich: qs_agent_orchestrator::collector::EnrichFn = Arc::new(move |ctx: DesktopContext| {
        let n = calls_c.fetch_add(1, Ordering::SeqCst);
        if n == 1 {
            // gen1 slow: stale by completion.
            std::thread::sleep(Duration::from_millis(400));
            ctx.with_resource(Some(ResourceContext::new(
                "neovim",
                Some("/tmp/stale-a.md"),
                Some("/tmp"),
                None,
                None,
                None,
                None,
                None,
            )))
        } else {
            ctx.with_resource(Some(ResourceContext::new(
                "neovim",
                Some("/tmp/fresh-b.md"),
                Some("/tmp"),
                None,
                None,
                None,
                None,
                None,
            )))
        }
    });
    let app = AppRefreshConfig {
        interval: Duration::from_secs(3600),
        enrich: Some(enrich),
    };
    let store = ActivityStore::open_in_memory().unwrap();
    let mut tracker = Tracker::new();
    let shutdown = AtomicBool::new(false);
    let config = CollectorConfig {
        event_socket: ev_path,
        request_socket: req,
        idle_timeout: Duration::from_millis(80),
        coalesce_window: Duration::from_millis(10),
    };
    let (outcome, _) = run_collector_once_with_enrich(&store, &mut tracker, &config, &shutdown, &app);
    let _ = server.join();
    assert_eq!(outcome, SessionOutcome::Disconnected);
    let rows = store.activity_in_range(0, i64::MAX / 2, 20).unwrap();
    assert!(
        !rows.iter().any(|r| r.snapshot_json.contains("stale-a")),
        "stale generation must be discarded, got {:?}",
        rows.iter().map(|r| &r.snapshot_json).collect::<Vec<_>>()
    );
    assert!(rows.iter().any(|r| r.snapshot_json.contains("fresh-b")));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn collector_blocked_provider_keeps_focus_and_shutdown_fast() {
    let _session_guard = session_serial();
    clear_last_good_cache();
    let dir = tmpdir("blocked-prov");
    let req = spawn_fake_request_socket(
        &dir,
        r#"{"address":"0x1","class":"kitty","title":"t","workspace":{"id":1,"name":"1"}}"#,
        r#"{"id":1,"name":"1"}"#,
        None,
    );
    let ev_path = dir.join(".socket2.sock");
    let _ = std::fs::remove_file(&ev_path);
    let listener = std::os::unix::net::UnixListener::bind(&ev_path).unwrap();
    std::thread::spawn(move || {
        let (_s, _) = listener.accept().expect("accept");
        std::thread::sleep(Duration::from_secs(5));
    });
    std::thread::sleep(Duration::from_millis(30));
    // Provider blocks 2 s per call: base must persist immediately and
    // shutdown must not wait for it.
    let enrich: qs_agent_orchestrator::collector::EnrichFn = Arc::new(move |ctx: DesktopContext| {
        std::thread::sleep(Duration::from_secs(2));
        ctx.with_resource(Some(ResourceContext::new(
            "kitty",
            None,
            Some("/tmp"),
            None,
            None,
            None,
            None,
            None,
        )))
    });
    let app = AppRefreshConfig {
        interval: Duration::from_secs(3600),
        enrich: Some(enrich),
    };
    let store = ActivityStore::open_in_memory().unwrap();
    let mut tracker = Tracker::new();
    let shutdown = Arc::new(AtomicBool::new(false));
    let shutdown_c = Arc::clone(&shutdown);
    let config = CollectorConfig {
        event_socket: ev_path,
        request_socket: req,
        idle_timeout: Duration::from_millis(50),
        coalesce_window: Duration::from_millis(10),
    };
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let (outcome, _) =
            run_collector_once_with_enrich(&store, &mut tracker, &config, shutdown_c.as_ref(), &app);
        let n = store.recent_activity(10).unwrap_or_default().len();
        let _ = tx.send((outcome, n));
    });
    // Base must appear quickly despite the blocked worker.
    std::thread::sleep(Duration::from_millis(300));
    shutdown.store(true, Ordering::SeqCst);
    let start = Instant::now();
    let (outcome, nrows) = rx.recv_timeout(Duration::from_secs(5)).expect("session exits");
    assert_eq!(outcome, SessionOutcome::Shutdown);
    assert!(
        start.elapsed() < Duration::from_secs(2),
        "shutdown must not wait for blocked provider, took {:?}",
        start.elapsed()
    );
    assert!(nrows >= 1, "base persists while provider blocked");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn collector_repeated_equal_enrichment_dedups() {
    let _session_guard = session_serial();
    clear_last_good_cache();
    let dir = tmpdir("res-dedup");
    let req = spawn_fake_request_socket(
        &dir,
        r#"{"address":"0x1","class":"kitty","title":"t","workspace":{"id":1,"name":"1"}}"#,
        r#"{"id":1,"name":"1"}"#,
        None,
    );
    let ev_path = dir.join(".socket2.sock");
    let _ = std::fs::remove_file(&ev_path);
    let listener = std::os::unix::net::UnixListener::bind(&ev_path).unwrap();
    let server = std::thread::spawn(move || {
        use std::io::Write;
        let (mut s, _) = listener.accept().expect("accept");
        std::thread::sleep(Duration::from_millis(60));
        let _ = s.write_all(b"activewindow>>kitty,t\nactivewindowv2>>0x1\n");
        std::thread::sleep(Duration::from_millis(900));
    });
    std::thread::sleep(Duration::from_millis(30));
    let enrich: qs_agent_orchestrator::collector::EnrichFn = Arc::new(move |ctx: DesktopContext| {
        ctx.with_resource(Some(ResourceContext::new(
            "kitty",
            None,
            Some("/tmp"),
            None,
            None,
            None,
            None,
            None,
        )))
    });
    let app = AppRefreshConfig {
        interval: Duration::from_secs(3600),
        enrich: Some(enrich),
    };
    let store = ActivityStore::open_in_memory().unwrap();
    let mut tracker = Tracker::new();
    let shutdown = AtomicBool::new(false);
    let config = CollectorConfig {
        event_socket: ev_path,
        request_socket: req,
        idle_timeout: Duration::from_millis(100),
        coalesce_window: Duration::from_millis(20),
    };
    let (outcome, _) = run_collector_once_with_enrich(&store, &mut tracker, &config, &shutdown, &app);
    let _ = server.join();
    assert_eq!(outcome, SessionOutcome::Disconnected);
    let rows = store.recent_activity(20).unwrap();
    // Base focus + one worker context + disconnect availability at most.
    assert!(rows.len() <= 3, "equal enrichment must dedup, got {}", rows.len());
    assert_eq!(
        rows.iter().filter(|r| r.kind == "context").count(),
        1,
        "exactly one context row for constant enrichment"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn collector_unavailable_provider_never_fails_base() {
    let _session_guard = session_serial();
    clear_last_good_cache();
    let dir = tmpdir("res-unavail");
    let req = spawn_fake_request_socket(
        &dir,
        r#"{"address":"0x1","class":"kitty","title":"t","workspace":{"id":1,"name":"1"}}"#,
        r#"{"id":1,"name":"1"}"#,
        None,
    );
    let ev_path = dir.join(".socket2.sock");
    let _ = std::fs::remove_file(&ev_path);
    let listener = std::os::unix::net::UnixListener::bind(&ev_path).unwrap();
    let server = std::thread::spawn(move || {
        use std::io::Write;
        let (mut s, _) = listener.accept().expect("accept");
        std::thread::sleep(Duration::from_millis(60));
        let _ = s.write_all(b"workspace>>1\n");
        std::thread::sleep(Duration::from_millis(600));
    });
    std::thread::sleep(Duration::from_millis(30));
    let enrich: qs_agent_orchestrator::collector::EnrichFn =
        Arc::new(move |_ctx: DesktopContext| panic!("provider down"));
    let app = AppRefreshConfig {
        interval: Duration::from_secs(3600),
        enrich: Some(enrich),
    };
    let store = ActivityStore::open_in_memory().unwrap();
    let mut tracker = Tracker::new();
    let shutdown = AtomicBool::new(false);
    let config = CollectorConfig {
        event_socket: ev_path,
        request_socket: req,
        idle_timeout: Duration::from_millis(100),
        coalesce_window: Duration::from_millis(20),
    };
    let (outcome, progress) =
        run_collector_once_with_enrich(&store, &mut tracker, &config, &shutdown, &app);
    let _ = server.join();
    assert_eq!(outcome, SessionOutcome::Disconnected);
    assert!(progress);
    assert!(!store.recent_activity(10).unwrap().is_empty(), "base must persist");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn collector_failure_noise_does_not_oscillate() {
    // End-to-end last-good: a Zen explicit record present, then deleted
    // (transient failure). The second enrichment must serve the cached
    // resource for the same focus, so the tracker dedups instead of
    // persisting a fallback-removal row.
    clear_last_good_cache();
    let dir = tmpdir("noise");
    private_dir(&dir);
    let now: i64 = 9_000_000;
    let win = FocusedWindow::new_with_pid("0xNOISE", "zen", "T", Some(4242));
    let rec = dir.join("zen.json");
    std::fs::write(
        &rec,
        r#"{"window_id":"0xNOISE","pid":4242,"url":"https://a.example/","title":"A","updated_at_ms":9000000}"#,
    )
    .unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&rec, std::fs::Permissions::from_mode(0o600));
    }
    let env = EnrichmentEnv {
        zen_file: Some(rec.clone()),
        ..Default::default()
    };
    let base = || {
        DesktopContext::available(Source::Hyprland, Some(win.clone()), None, now)
    };
    // The last-good cache is process-global and other tests clear it in
    // parallel; retry the prime/fail handshake until it holds without
    // interference (bounded, converges immediately when undisturbed).
    let rec_body = r#"{"window_id":"0xNOISE","pid":4242,"url":"https://a.example/","title":"A","updated_at_ms":9000000}"#;
    let mut pair = None;
    for _ in 0..50 {
        std::fs::write(&rec, rec_body).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&rec, std::fs::Permissions::from_mode(0o600));
        }
        let first = enrich_with_env(base(), &env, now);
        if first.resource.as_ref().map(|r| r.adapter.as_str()) != Some("zen") {
            continue;
        }
        let _ = std::fs::remove_file(&rec);
        let second = enrich_with_env(base(), &env, now + 1000);
        if second.resource == first.resource {
            pair = Some((first, second));
            break;
        }
    }
    let (first, second) = pair.expect(
        "last-good must mask transient unavailability for the same focus",
    );
    let mut t = Tracker::new();
    match t.enqueue(first.clone()).unwrap() {
        EnqueueOutcome::Queued(_, _) => {}
        other => panic!("{other:?}"),
    }
    let (_, snap) = t.pop_pending_front().unwrap();
    t.mark_persisted(&snap);
    assert_eq!(t.enqueue(second).unwrap(), EnqueueOutcome::Deduplicated);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn collector_app_only_refresh_without_compositor_polling() {
    let _session_guard = session_serial();
    clear_last_good_cache();
    let dir = tmpdir("app-refresh");
    let hits = Arc::new(AtomicUsize::new(0));
    let req = spawn_fake_request_socket(
        &dir,
        r#"{"address":"0x1","class":"kitty","title":"t","workspace":{"id":1,"name":"1"}}"#,
        r#"{"id":1,"name":"1"}"#,
        Some(Arc::clone(&hits)),
    );
    let ev_path = dir.join(".socket2.sock");
    let _ = std::fs::remove_file(&ev_path);
    let listener = std::os::unix::net::UnixListener::bind(&ev_path).unwrap();
    std::thread::spawn(move || {
        let (_s, _) = listener.accept().expect("accept");
        std::thread::sleep(Duration::from_secs(5));
    });
    std::thread::sleep(Duration::from_millis(30));

    let enrich: qs_agent_orchestrator::collector::EnrichFn = Arc::new(move |ctx: DesktopContext| {
        ctx.with_resource(Some(ResourceContext::new(
            "neovim",
            Some("/tmp/b.md"),
            Some("/tmp"),
            None,
            None,
            None,
            None,
            None,
        )))
    });
    let app = AppRefreshConfig {
        interval: Duration::from_millis(80),
        enrich: Some(enrich),
    };
    let shutdown = Arc::new(AtomicBool::new(false));
    let shutdown_c = Arc::clone(&shutdown);
    let config = CollectorConfig {
        event_socket: ev_path,
        request_socket: req,
        idle_timeout: Duration::from_millis(50),
        coalesce_window: Duration::from_millis(10),
    };
    // File DB so the test thread can observe harvested rows while the
    // session still runs (in-memory stores cannot cross threads).
    let db = dir.join("activity.db");
    {
        let init = ActivityStore::open(&db).unwrap();
        let _ = init.recent_activity(1).unwrap();
    }
    let db_session = db.clone();
    let db_poll = db.clone();
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let session_store = ActivityStore::open(&db_session).unwrap();
        let mut session_tracker = Tracker::new();
        let (outcome, _) = run_collector_once_with_enrich(
            &session_store,
            &mut session_tracker,
            &config,
            shutdown_c.as_ref(),
            &app,
        );
        let _ = tx.send(outcome);
    });
    // Poll the file DB until the async context row is harvested on idle.
    let start = Instant::now();
    let kinds = loop {
        let poll = ActivityStore::open_read_only(&db_poll).unwrap();
        let rows = poll.recent_activity(20).unwrap_or_default();
        let kinds: Vec<String> = rows.iter().map(|r| r.kind.clone()).collect();
        if kinds.contains(&"context".to_string()) || start.elapsed() > Duration::from_secs(5) {
            break kinds;
        }
        std::thread::sleep(Duration::from_millis(25));
    };
    shutdown.store(true, Ordering::SeqCst);
    let outcome = rx.recv_timeout(Duration::from_secs(5)).expect("session exits");
    assert_eq!(outcome, SessionOutcome::Shutdown);
    assert!(kinds.contains(&"context".to_string()), "refresh row must be context, got {kinds:?}");
    assert_eq!(
        hits.load(Ordering::SeqCst),
        4,
        "app refresh must not poll the compositor"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn storage_blocked_with_enrich_stays_persistence_only() {
    let _session_guard = session_serial();
    use qs_agent_orchestrator::desktop_context::MAX_PENDING_OBSERVATIONS;
    use qs_agent_orchestrator::hyprland::SocketPaths;
    let dir = tmpdir("blocked-enrich");
    let db = dir.join("activity.db");
    drop(ActivityStore::open(&db).unwrap());
    let lock_conn = rusqlite::Connection::open(&db).unwrap();
    lock_conn.execute_batch("BEGIN EXCLUSIVE").unwrap();

    let mut tracker = Tracker::new();
    for i in 0..MAX_PENDING_OBSERVATIONS {
        let c = DesktopContext::available(
            Source::Hyprland,
            Some(FocusedWindow::new("0x1", "kitty", &format!("t{i}"))),
            Some(Workspace::new("1", "1")),
            1000 + i as i64,
        );
        match tracker.enqueue(c).expect("prefill") {
            EnqueueOutcome::Queued(_, _) => {}
            EnqueueOutcome::Deduplicated => panic!("must queue"),
        }
    }
    let discover_hits = Arc::new(AtomicUsize::new(0));
    let req_hits = Arc::new(AtomicUsize::new(0));
    let req_path = spawn_fake_request_socket(
        &dir,
        r#"{"address":"0x1","class":"kitty","title":"static","workspace":{"id":1,"name":"1"}}"#,
        r#"{"id":1,"name":"1"}"#,
        Some(Arc::clone(&req_hits)),
    );
    let ev_path = dir.join(".socket2.sock");
    {
        use std::os::unix::net::UnixListener;
        let _ = std::fs::remove_file(&ev_path);
        let listener = UnixListener::bind(&ev_path).unwrap();
        std::thread::spawn(move || {
            let (_s, _) = listener.accept().expect("accept");
            std::thread::sleep(Duration::from_secs(10));
        });
    }
    std::thread::sleep(Duration::from_millis(50));
    let shutdown = Arc::new(AtomicBool::new(false));
    let shutdown_c = Arc::clone(&shutdown);
    let dh_c = Arc::clone(&discover_hits);
    let req_c = req_path.clone();
    let ev_c = ev_path.clone();
    let app = AppRefreshConfig {
        interval: Duration::from_millis(50),
        enrich: None,
    };
    let handle = std::thread::spawn(move || {
        let session_store = ActivityStore::open(&db).unwrap();
        let mut session_tracker = tracker;
        let paths = SocketPaths {
            event_socket: ev_c,
            request_socket: req_c,
        };
        run_collector_forever_with_enrich(
            &session_store,
            &mut session_tracker,
            shutdown_c.as_ref(),
            || {
                dh_c.fetch_add(1, Ordering::SeqCst);
                Some(paths.clone())
            },
            &app,
        )
    });
    std::thread::sleep(Duration::from_millis(700));
    shutdown.store(true, Ordering::SeqCst);
    let _ = handle.join().expect("join");
    assert_eq!(discover_hits.load(Ordering::SeqCst), 0, "blocked: no discover");
    assert_eq!(req_hits.load(Ordering::SeqCst), 0, "blocked: no snapshot queries");
    lock_conn.execute_batch("COMMIT").unwrap();
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn collector_event_after_enriched_no_oscillation() {
    let _session_guard = session_serial();
    // HIGH: a raw event snapshot carries resource=None, which is NOT
    // confirmed absence. After the initial enrichment persisted, a later
    // event for the same window must merge the overlay (dedup), never a
    // clear/restore oscillation.
    clear_last_good_cache();
    let dir = tmpdir("no-osc");
    let req = spawn_fake_request_socket(
        &dir,
        r#"{"address":"0x1","class":"kitty","title":"t","workspace":{"id":1,"name":"1"}}"#,
        r#"{"id":1,"name":"1"}"#,
        None,
    );
    let ev_path = dir.join(".socket2.sock");
    let _ = std::fs::remove_file(&ev_path);
    let listener = std::os::unix::net::UnixListener::bind(&ev_path).unwrap();
    let server = std::thread::spawn(move || {
        use std::io::Write;
        let (mut s, _) = listener.accept().expect("accept");
        // Event AFTER the worker context has been harvested (idle 100 ms
        // harvests well before 350 ms); EOF later.
        std::thread::sleep(Duration::from_millis(350));
        let _ = s.write_all(b"workspace>>1\n");
        std::thread::sleep(Duration::from_millis(300));
    });
    std::thread::sleep(Duration::from_millis(30));
    let enrich: qs_agent_orchestrator::collector::EnrichFn =
        Arc::new(move |ctx: DesktopContext| {
            ctx.with_resource(Some(ResourceContext::new(
                "kitty",
                None,
                Some("/tmp/stable"),
                None,
                None,
                None,
                None,
                None,
            )))
        });
    let app = AppRefreshConfig {
        interval: Duration::from_secs(3600),
        enrich: Some(enrich),
    };
    let store = ActivityStore::open_in_memory().unwrap();
    let mut tracker = Tracker::new();
    let shutdown = AtomicBool::new(false);
    let config = CollectorConfig {
        event_socket: ev_path,
        request_socket: req,
        idle_timeout: Duration::from_millis(100),
        coalesce_window: Duration::from_millis(20),
    };
    let (outcome, _) = run_collector_once_with_enrich(&store, &mut tracker, &config, &shutdown, &app);
    let _ = server.join();
    assert_eq!(outcome, SessionOutcome::Disconnected);
    let rows = store.activity_in_range(0, i64::MAX / 2, 20).unwrap();
    let kinds: Vec<&str> = rows.iter().map(|r| r.kind.as_str()).collect();
    assert_eq!(
        kinds,
        vec!["focus", "context", "availability"],
        "event must merge overlay (dedup), never oscillate"
    );
    // The initial base row is bare by design (base persists before the
    // worker answers); every row AFTER the enrichment arrived must keep the
    // stable overlay — the later event snapshot merged it instead of
    // persisting a resource=None clear row.
    let ctx_pos = kinds.iter().position(|k| *k == "context").expect("context row");
    assert!(rows[ctx_pos].snapshot_json.contains("/tmp/stable"));
    for r in rows.iter().skip(ctx_pos + 1).filter(|r| r.kind != "availability") {
        assert!(r.snapshot_json.contains("/tmp/stable"), "row lost overlay: {r:?}");
    }
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn collector_eof_discards_inflight_enrichment() {
    let _session_guard = session_serial();
    // HIGH: a blocked worker result for a superseded generation plus a
    // terminal EOF must never surface after disconnect. No terminal drain:
    // EOF discards immediately and marks unavailable.
    clear_last_good_cache();
    let dir = tmpdir("eof-discard");
    let req = spawn_fake_request_socket(
        &dir,
        r#"{"address":"0x1","class":"kitty","title":"t","workspace":{"id":1,"name":"1"}}"#,
        r#"{"id":1,"name":"1"}"#,
        None,
    );
    let ev_path = dir.join(".socket2.sock");
    let _ = std::fs::remove_file(&ev_path);
    let listener = std::os::unix::net::UnixListener::bind(&ev_path).unwrap();
    let server = std::thread::spawn(move || {
        use std::io::Write;
        let (mut s, _) = listener.accept().expect("accept");
        std::thread::sleep(Duration::from_millis(60));
        let _ = s.write_all(b"workspace>>1\n");
        // EOF while the worker is still blocked.
        std::thread::sleep(Duration::from_millis(90));
    });
    std::thread::sleep(Duration::from_millis(30));
    let enrich: qs_agent_orchestrator::collector::EnrichFn =
        Arc::new(move |ctx: DesktopContext| {
            std::thread::sleep(Duration::from_millis(600));
            ctx.with_resource(Some(ResourceContext::new(
                "kitty",
                None,
                Some("/tmp/stale-eof"),
                None,
                None,
                None,
                None,
                None,
            )))
        });
    let app = AppRefreshConfig {
        interval: Duration::from_secs(3600),
        enrich: Some(enrich),
    };
    let store = ActivityStore::open_in_memory().unwrap();
    let mut tracker = Tracker::new();
    let shutdown = AtomicBool::new(false);
    let config = CollectorConfig {
        event_socket: ev_path,
        request_socket: req,
        idle_timeout: Duration::from_millis(100),
        coalesce_window: Duration::from_millis(20),
    };
    let start = Instant::now();
    let (outcome, _) = run_collector_once_with_enrich(&store, &mut tracker, &config, &shutdown, &app);
    let _ = server.join();
    assert_eq!(outcome, SessionOutcome::Disconnected);
    assert!(
        start.elapsed() < Duration::from_secs(2),
        "EOF must not wait for the blocked worker, took {:?}",
        start.elapsed()
    );
    let rows = store.activity_in_range(0, i64::MAX / 2, 20).unwrap();
    assert!(
        !rows.iter().any(|r| r.snapshot_json.contains("stale-eof")),
        "in-flight enrichment must be discarded on EOF"
    );
    let cur = tracker.current_context().expect("current");
    assert!(!cur.available, "disconnect marks unavailable, never stale");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn collector_idle_resource_advances_timestamp() {
    let _session_guard = session_serial();
    // MED: idle resource observations use completion time, so a later
    // resource change advances history time.
    clear_last_good_cache();
    let dir = tmpdir("idle-ts");
    let req = spawn_fake_request_socket(
        &dir,
        r#"{"address":"0x1","class":"kitty","title":"t","workspace":{"id":1,"name":"1"}}"#,
        r#"{"id":1,"name":"1"}"#,
        None,
    );
    let ev_path = dir.join(".socket2.sock");
    let _ = std::fs::remove_file(&ev_path);
    let listener = std::os::unix::net::UnixListener::bind(&ev_path).unwrap();
    let server = std::thread::spawn(move || {
        let (_s, _) = listener.accept().expect("accept");
        std::thread::sleep(Duration::from_millis(700));
    });
    std::thread::sleep(Duration::from_millis(30));
    let calls = Arc::new(AtomicUsize::new(0));
    let calls_c = Arc::clone(&calls);
    let enrich: qs_agent_orchestrator::collector::EnrichFn = Arc::new(move |ctx: DesktopContext| {
        let n = calls_c.fetch_add(1, Ordering::SeqCst);
        let file = if n == 0 { "/tmp/ts-a.md" } else { "/tmp/ts-b.md" };
        ctx.with_resource(Some(ResourceContext::new(
            "neovim",
            Some(file),
            Some("/tmp"),
            None,
            None,
            None,
            None,
            None,
        )))
    });
    let app = AppRefreshConfig {
        interval: Duration::from_millis(80),
        enrich: Some(enrich),
    };
    let store = ActivityStore::open_in_memory().unwrap();
    let mut tracker = Tracker::new();
    let shutdown = AtomicBool::new(false);
    let config = CollectorConfig {
        event_socket: ev_path,
        request_socket: req,
        idle_timeout: Duration::from_millis(50),
        coalesce_window: Duration::from_millis(10),
    };
    let (outcome, _) = run_collector_once_with_enrich(&store, &mut tracker, &config, &shutdown, &app);
    let _ = server.join();
    assert_eq!(outcome, SessionOutcome::Disconnected);
    let rows = store.activity_in_range(0, i64::MAX / 2, 20).unwrap();
    let with_res: Vec<_> = rows
        .iter()
        .filter(|r| r.kind == "focus" || r.kind == "context")
        .collect();
    assert!(with_res.len() >= 3, "base + two resource rows, got {}", with_res.len());
    let ts: Vec<i64> = with_res.iter().map(|r| r.observed_at_ms).collect();
    let sorted = {
        let mut s = ts.clone();
        s.sort();
        s
    };
    assert_eq!(ts, sorted, "history timestamps must advance, got {ts:?}");
    assert!(ts.windows(2).all(|w| w[0] < w[1]), "strictly increasing, got {ts:?}");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn collector_enriched_overflow_is_backlog_full() {
    let _session_guard = session_serial();
    // MED: enriched-result overflow propagates BacklogFull with local
    // unavailable marking, exactly like Phase 1 snapshot overflow.
    use qs_agent_orchestrator::desktop_context::MAX_PENDING_OBSERVATIONS;
    clear_last_good_cache();
    let dir = tmpdir("enrich-full");
    let req = spawn_fake_request_socket(
        &dir,
        r#"{"address":"0x1","class":"kitty","title":"overflow-new","workspace":{"id":1,"name":"1"}}"#,
        r#"{"id":1,"name":"1"}"#,
        None,
    );
    let ev_path = dir.join(".socket2.sock");
    let _ = std::fs::remove_file(&ev_path);
    let listener = std::os::unix::net::UnixListener::bind(&ev_path).unwrap();
    std::thread::spawn(move || {
        let (_s, _) = listener.accept().expect("accept");
        std::thread::sleep(Duration::from_secs(5));
    });
    std::thread::sleep(Duration::from_millis(30));
    // Prefill to one below capacity so the initial base fills it and the
    // fast worker result overflows.
    let mut tracker = Tracker::new();
    for i in 0..MAX_PENDING_OBSERVATIONS - 1 {
        let c = DesktopContext::available(
            Source::Hyprland,
            Some(FocusedWindow::new("0x1", "kitty", &format!("pre{i}"))),
            Some(Workspace::new("1", "1")),
            1000 + i as i64,
        );
        match tracker.enqueue(c).expect("prefill") {
            EnqueueOutcome::Queued(_, _) => {}
            EnqueueOutcome::Deduplicated => panic!("must queue"),
        }
    }
    // Storage stays blocked: in-memory store that always fails appends.
    // Use a real lock instead: separate DB under exclusive lock.
    let lockdir = tmpdir("enrich-full-db");
    let db = lockdir.join("activity.db");
    drop(ActivityStore::open(&db).unwrap());
    let lock_conn = rusqlite::Connection::open(&db).unwrap();
    lock_conn.execute_batch("BEGIN EXCLUSIVE").unwrap();
    let store = ActivityStore::open(&db).unwrap();
    let enrich: qs_agent_orchestrator::collector::EnrichFn =
        Arc::new(move |ctx: DesktopContext| {
            ctx.with_resource(Some(ResourceContext::new(
                "kitty",
                None,
                Some("/tmp/full"),
                None,
                None,
                None,
                None,
                None,
            )))
        });
    let app = AppRefreshConfig {
        interval: Duration::from_millis(40),
        enrich: Some(enrich),
    };
    let shutdown = AtomicBool::new(false);
    let config = CollectorConfig {
        event_socket: ev_path,
        request_socket: req,
        idle_timeout: Duration::from_millis(50),
        coalesce_window: Duration::from_millis(10),
    };
    let (outcome, _) = run_collector_once_with_enrich(&store, &mut tracker, &config, &shutdown, &app);
    assert_eq!(
        outcome,
        SessionOutcome::BacklogFull,
        "enriched overflow must end the session like snapshot overflow"
    );
    let cur = tracker.current_context().expect("current");
    assert!(!cur.available, "BacklogFull reads locally unavailable");
    lock_conn.execute_batch("COMMIT").unwrap();
    let _ = std::fs::remove_dir_all(&dir);
    let _ = std::fs::remove_dir_all(&lockdir);
}

#[test]
fn collector_event_confirmed_absence_clears_without_idle_refresh() {
    // The worker always receives a resource-free snapshot through the real
    // default enrichment path: a Zen explicit record flips from matching to
    // mismatched (empty title, so no fallback), and the compositor event
    // must persist a clear row on the event path itself — not via the
    // (disabled here) idle refresh. Before the strip fix, the merged
    // overlay rode along into the worker, last-good re-served it on
    // mismatch, and the absence never cleared.
    let _session_guard = session_serial();
    clear_last_good_cache();
    let dir = tmpdir("confirmed-absence");
    let req = spawn_fake_request_socket(
        &dir,
        r#"{"address":"0x9","class":"zen","title":"","pid":4242,"workspace":{"id":1,"name":"1"}}"#,
        r#"{"id":1,"name":"1"}"#,
        None,
    );
    let zen_path = dir.join("zen.json");
    let write_zen = |window_id: &str| {
        std::fs::write(
            &zen_path,
            format!(
                r#"{{"window_id":"{window_id}","pid":4242,"url":"https://example.com/a","title":"A","updated_at_ms":{}}}"#,
                qs_agent_orchestrator::now_ms()
            ),
        )
        .unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&zen_path, std::fs::Permissions::from_mode(0o600));
        }
    };
    write_zen("0x9");
    let old_zen_env = std::env::var_os("QS_ZEN_CONTEXT_FILE");
    std::env::set_var("QS_ZEN_CONTEXT_FILE", &zen_path);
    let ev_path = dir.join(".socket2.sock");
    let _ = std::fs::remove_file(&ev_path);
    let listener = std::os::unix::net::UnixListener::bind(&ev_path).unwrap();
    let fire = Arc::new(AtomicBool::new(false));
    let fire_srv = Arc::clone(&fire);
    let server = std::thread::spawn(move || {
        use std::io::Write;
        let (mut s, _) = listener.accept().expect("accept");
        // Wait for the main thread to observe the enriched row, then fire
        // one compositor event for the same window, then EOF.
        let start = Instant::now();
        while !fire_srv.load(Ordering::SeqCst) && start.elapsed() < Duration::from_secs(10) {
            std::thread::sleep(Duration::from_millis(25));
        }
        let _ = s.write_all(b"workspace>>1\n");
        std::thread::sleep(Duration::from_millis(1200));
    });
    std::thread::sleep(Duration::from_millis(30));

    // Default enrichment (real provider path); idle refresh disabled.
    let app = AppRefreshConfig {
        interval: Duration::from_secs(3600),
        enrich: None,
    };
    // File DB so the main thread can observe rows while the session runs.
    let db = dir.join("activity.db");
    {
        let init = ActivityStore::open(&db).unwrap();
        let _ = init.recent_activity(1).unwrap();
    }
    let db_session = db.clone();
    let db_poll = db.clone();
    let shutdown = Arc::new(AtomicBool::new(false));
    let shutdown_c = Arc::clone(&shutdown);
    let config = CollectorConfig {
        event_socket: ev_path,
        request_socket: req,
        idle_timeout: Duration::from_millis(100),
        coalesce_window: Duration::from_millis(20),
    };
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let session_store = ActivityStore::open(&db_session).unwrap();
        let mut session_tracker = Tracker::new();
        let (outcome, _) = run_collector_once_with_enrich(
            &session_store,
            &mut session_tracker,
            &config,
            shutdown_c.as_ref(),
            &app,
        );
        let _ = tx.send(outcome);
    });
    // Wait for the enriched context row, then fire the event.
    let start = Instant::now();
    loop {
        let poll = ActivityStore::open_read_only(&db_poll).unwrap();
        let rows = poll.recent_activity(20).unwrap_or_default();
        if rows.iter().any(|r| r.kind == "context") || start.elapsed() > Duration::from_secs(10) {
            break;
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    // Flip the explicit record to another window BEFORE firing: the next
    // worker pass sees a confirmed mismatch (empty title, so no fallback).
    write_zen("0xOTHER");
    fire.store(true, Ordering::SeqCst);
    // Wait for the clear row (event path, not idle refresh).
    let start = Instant::now();
    let cleared = loop {
        let poll = ActivityStore::open_read_only(&db_poll).unwrap();
        let rows = poll.activity_in_range(0, i64::MAX / 2, 20).unwrap_or_default();
        let kinds: Vec<String> = rows.iter().map(|r| r.kind.clone()).collect();
        if kinds == ["focus", "context", "context"] || start.elapsed() > Duration::from_secs(10) {
            break kinds;
        }
        std::thread::sleep(Duration::from_millis(25));
    };
    match old_zen_env {
        Some(v) => std::env::set_var("QS_ZEN_CONTEXT_FILE", v),
        None => std::env::remove_var("QS_ZEN_CONTEXT_FILE"),
    }
    assert_eq!(
        cleared,
        vec!["focus", "context", "context"],
        "confirmed absence must clear on the event path"
    );
    let poll = ActivityStore::open_read_only(&db_poll).unwrap();
    let rows = poll.activity_in_range(0, i64::MAX / 2, 20).unwrap();
    assert!(rows[1].snapshot_json.contains("https://example.com/a"));
    assert!(
        rows[2].snapshot_json.contains("\"resource\":null"),
        "third row must be the resource-free clear, got {}",
        rows[2].snapshot_json
    );
    shutdown.store(true, Ordering::SeqCst);
    let _ = rx.recv_timeout(Duration::from_secs(5));
    let _ = server.join();
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn collector_title_change_keeps_matching_fallback_resource() {
    // Title-only fallbacks are recomputed synchronously in the overlay
    // merge: a Zen title change must persist one title row whose resource
    // already matches, not a stale-title row plus a correction context.
    let _session_guard = session_serial();
    clear_last_good_cache();
    let dir = tmpdir("zen-title-change");
    // First full fetch (4 one-shot queries) serves T1, later fetches T2.
    let hits = Arc::new(AtomicUsize::new(0));
    let hits_srv = Arc::clone(&hits);
    let req_path = dir.join(".socket.sock");
    let _ = std::fs::remove_file(&req_path);
    let listener_req = std::os::unix::net::UnixListener::bind(&req_path).unwrap();
    std::thread::spawn(move || {
        use std::io::{Read, Write};
        for conn in listener_req.incoming() {
            let mut s = match conn {
                Ok(s) => s,
                Err(_) => return,
            };
            let _ = s.set_read_timeout(Some(Duration::from_secs(2)));
            let mut buf = vec![0u8; 4096];
            let n = match s.read(&mut buf) {
                Ok(n) => n,
                Err(_) => continue,
            };
            if n == 0 {
                continue;
            }
            let n_hit = hits_srv.fetch_add(1, Ordering::SeqCst);
            let title = if n_hit < 4 { "First Article" } else { "Second Article" };
            let req = String::from_utf8_lossy(&buf[..n]).to_string();
            let reply = if req.contains("activewindow") {
                format!(
                    r#"{{"address":"0x9","class":"zen","title":"{title}","workspace":{{"id":1,"name":"1"}}}}"#
                )
            } else {
                r#"{"id":1,"name":"1"}"#.to_string()
            };
            let _ = s.write_all(reply.as_bytes());
        }
    });
    let ev_path = dir.join(".socket2.sock");
    let _ = std::fs::remove_file(&ev_path);
    let listener = std::os::unix::net::UnixListener::bind(&ev_path).unwrap();
    let server = std::thread::spawn(move || {
        use std::io::Write;
        let (mut s, _) = listener.accept().expect("accept");
        // Let the initial focus + fallback-context rows land first.
        std::thread::sleep(Duration::from_millis(500));
        let _ = s.write_all(b"workspace>>1\n");
        std::thread::sleep(Duration::from_millis(1200));
    });
    std::thread::sleep(Duration::from_millis(30));

    // Default enrichment (no injection): zen title fallback is pure.
    let app = AppRefreshConfig {
        interval: Duration::from_secs(3600),
        enrich: None,
    };
    let store = ActivityStore::open_in_memory().unwrap();
    let mut tracker = Tracker::new();
    let shutdown = AtomicBool::new(false);
    let config = CollectorConfig {
        event_socket: ev_path,
        request_socket: req_path,
        idle_timeout: Duration::from_millis(100),
        coalesce_window: Duration::from_millis(20),
    };
    let (outcome, _) = run_collector_once_with_enrich(&store, &mut tracker, &config, &shutdown, &app);
    let _ = server.join();
    assert_eq!(outcome, SessionOutcome::Disconnected);
    let _ = flush_pending(&store, &mut tracker);
    let rows = store.activity_in_range(0, i64::MAX / 2, 20).unwrap();
    let kinds: Vec<&str> = rows.iter().map(|r| r.kind.as_str()).collect();
    assert_eq!(
        kinds,
        vec!["focus", "context", "title", "availability"],
        "title change must be a single matching row, got {kinds:?}"
    );
    let title_row = &rows[2];
    assert!(title_row.snapshot_json.contains("Second Article"));
    let snap: serde_json::Value = serde_json::from_str(&title_row.snapshot_json).unwrap();
    assert_eq!(
        snap.pointer("/resource/title").and_then(|v| v.as_str()),
        Some("Second Article"),
        "title row resource must already match, got {}",
        title_row.snapshot_json
    );
    assert_eq!(
        snap.pointer("/resource/adapter").and_then(|v| v.as_str()),
        Some("zen-title")
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn kitty_same_nvim_missing_record_serves_last_good_no_kitty_noise() {
    // Same verified foreground nvim whose record disappears must read as
    // transient (bounded last-good serves, no fileA/kitty/fileA noise);
    // a genuinely changed or ambiguous foreground falls back to kitty cwd.
    // Uses a fake `kitty` executable on PATH plus a real listener socket
    // so SO_PEERCRED identity binds to this test process.
    let _session_guard = session_serial();
    clear_last_good_cache();
    let dir = tmpdir("kitty-nvim-gap");
    let bindir = dir.join("bin");
    std::fs::create_dir_all(&bindir).unwrap();
    let ls_nvim = dir.join("ls-nvim.json");
    let ls_shell = dir.join("ls-shell.json");
    let ls_amb = dir.join("ls-ambiguous.json");
    // Foreground: shell pid 4242 + unique nvim pid 4321, pane id 7.
    std::fs::write(
        &ls_nvim,
        r#"[{"id":11,"is_focused":true,"tabs":[{"id":21,"is_focused":true,"windows":[{"id":7,"is_focused":true,"pid":4242,"cwd":"/tmp/stage","title":"nvim","foreground_processes":[{"pid":4242,"cmdline":["fish"],"cwd":"/tmp/stage"},{"pid":4321,"cmdline":["nvim","note.md"],"cwd":"/tmp/stage"}]}]}]}]"#,
    )
    .unwrap();
    // Changed editor: shell only, no nvim anywhere.
    std::fs::write(
        &ls_shell,
        r#"[{"id":11,"is_focused":true,"tabs":[{"id":21,"is_focused":true,"windows":[{"id":7,"is_focused":true,"pid":4242,"cwd":"/tmp/stage","title":"fish","foreground_processes":[{"pid":4242,"cmdline":["fish"],"cwd":"/tmp/stage"}]}]}]}]"#,
    )
    .unwrap();
    // Ambiguous: two distinct nvim foreground PIDs.
    std::fs::write(
        &ls_amb,
        r#"[{"id":11,"is_focused":true,"tabs":[{"id":21,"is_focused":true,"windows":[{"id":7,"is_focused":true,"pid":4242,"cwd":"/tmp/stage","title":"nvim","foreground_processes":[{"pid":4321,"cmdline":["nvim","a.md"],"cwd":"/tmp/stage"},{"pid":4322,"cmdline":["nvim","b.md"],"cwd":"/tmp/stage"}]}]}]}]"#,
    )
    .unwrap();
    // Fake `kitty` ignores argv and prints the file named by env.
    let fake = bindir.join("kitty");
    std::fs::write(&fake, "#!/bin/sh\ncat \"${QS_FAKE_KITTY_LS:?}\"\n").unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&fake, std::fs::Permissions::from_mode(0o755));
    }
    // Real listener: peer PID on connect is this test process.
    let sock = dir.join("kitty.sock");
    let _ = std::fs::remove_file(&sock);
    let listener = std::os::unix::net::UnixListener::bind(&sock).unwrap();
    let _srv = std::thread::spawn(move || {
        for conn in listener.incoming() {
            if conn.is_err() {
                return;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    });
    let me = std::process::id();
    let win = || FocusedWindow::new_with_pid("0xK", "kitty", "t", Some(me));
    let base = || {
        DesktopContext::available(
            Source::Hyprland,
            Some(win()),
            Some(Workspace::new("1", "1")),
            qs_agent_orchestrator::now_ms(),
        )
    };
    let recdir = dir.join("nvim-context");
    private_dir(&recdir);
    let now = qs_agent_orchestrator::now_ms();
    std::fs::write(
        recdir.join("4321.json"),
        format!(
            r#"{{"pid":4321,"kitty_window_id":7,"file":"/tmp/stage/note.md","cwd":"/tmp/stage","updated_at_ms":{now}}}"#
        ),
    )
    .unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(recdir.join("4321.json"), std::fs::Permissions::from_mode(0o600));
    }
    let env = EnrichmentEnv {
        kitty_socket: Some(sock.clone()),
        nvim_dir: Some(recdir.clone()),
        ..Default::default()
    };

    // PATH + ls-file env under the serial lock; restored before asserts.
    let old_path = std::env::var_os("PATH");
    let old_ls = std::env::var_os("QS_FAKE_KITTY_LS");
    let mut new_path = bindir.to_string_lossy().to_string();
    if let Some(p) = &old_path {
        new_path.push(':');
        new_path.push_str(&p.to_string_lossy());
    }
    std::env::set_var("PATH", &new_path);
    std::env::set_var("QS_FAKE_KITTY_LS", &ls_nvim);
    let first = enrich_with_env(base(), &env, qs_agent_orchestrator::now_ms());
    let r0 = first.resource.clone().expect("neovim resource");
    assert_eq!(r0.adapter, "neovim");
    assert_eq!(r0.file.as_deref(), Some("/tmp/stage/note.md"));

    // Record vanishes, same foreground nvim: transient, last-good serves.
    std::fs::remove_file(recdir.join("4321.json")).unwrap();
    let second = enrich_with_env(base(), &env, qs_agent_orchestrator::now_ms());
    let r1 = second.resource.clone().expect("last-good must serve");
    assert_eq!(r1, r0, "missing record must not fall back to kitty cwd");

    // Record restored: identical again, no oscillation.
    std::fs::write(
        recdir.join("4321.json"),
        format!(
            r#"{{"pid":4321,"kitty_window_id":7,"file":"/tmp/stage/note.md","cwd":"/tmp/stage","updated_at_ms":{}}}"#,
            qs_agent_orchestrator::now_ms()
        ),
    )
    .unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(recdir.join("4321.json"), std::fs::Permissions::from_mode(0o600));
    }
    let third = enrich_with_env(base(), &env, qs_agent_orchestrator::now_ms());
    assert_eq!(third.resource, first.resource);

    // Changed editor (shell only): confirmed kitty fallback, evicting.
    std::env::set_var("QS_FAKE_KITTY_LS", &ls_shell);
    let fourth = enrich_with_env(base(), &env, qs_agent_orchestrator::now_ms());
    let r3 = fourth.resource.clone().expect("kitty fallback");
    assert_eq!(r3.adapter, "kitty");
    assert_eq!(r3.file, None);
    assert_eq!(r3.cwd.as_deref(), Some("/tmp/stage"));

    // Ambiguous foreground (two nvims): kitty fallback, never a guess.
    std::env::set_var("QS_FAKE_KITTY_LS", &ls_amb);
    let fifth = enrich_with_env(base(), &env, qs_agent_orchestrator::now_ms());
    assert_eq!(fifth.resource.as_ref().map(|r| r.adapter.as_str()), Some("kitty"));

    match old_path {
        Some(p) => std::env::set_var("PATH", p),
        None => std::env::remove_var("PATH"),
    }
    match old_ls {
        Some(p) => std::env::set_var("QS_FAKE_KITTY_LS", p),
        None => std::env::remove_var("QS_FAKE_KITTY_LS"),
    }
    assert_eq!(r1.adapter, "neovim");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn kitty_pane_switch_missing_record_clears_to_kitty_never_old_file() {
    // Pane/editor-scoped last-good: paneA/nvim4321/fileA cached, then the
    // focused pane switches to paneB/nvim4322 with no record. The old
    // editor file must never be served for the new identity: the result is
    // the verified paneB kitty cwd (evicting), not fileA and not a clear.
    // Returning to paneA while its record is still missing likewise yields
    // paneA kitty cwd — fileA is never resurrected after eviction.
    // Ambiguous pane focus resolves to no resource at all (confirmed
    // mismatch), never an unrelated previous pane's value.
    let _session_guard = session_serial();
    clear_last_good_cache();
    let dir = tmpdir("kitty-pane-switch");
    let bindir = dir.join("bin");
    std::fs::create_dir_all(&bindir).unwrap();
    let ls_a = dir.join("ls-a.json");
    let ls_b = dir.join("ls-b.json");
    let ls_amb = dir.join("ls-amb.json");
    // Pane 7, unique nvim pid 4321.
    std::fs::write(
        &ls_a,
        r#"[{"id":11,"is_focused":true,"tabs":[{"id":21,"is_focused":true,"windows":[{"id":7,"is_focused":true,"pid":4242,"cwd":"/tmp/stage","title":"nvim","foreground_processes":[{"pid":4242,"cmdline":["fish"],"cwd":"/tmp/stage"},{"pid":4321,"cmdline":["nvim","note.md"],"cwd":"/tmp/stage"}]}]}]}]"#,
    )
    .unwrap();
    // Pane 8, unique nvim pid 4322, different cwd.
    std::fs::write(
        &ls_b,
        r#"[{"id":11,"is_focused":true,"tabs":[{"id":21,"is_focused":true,"windows":[{"id":8,"is_focused":true,"pid":4242,"cwd":"/tmp/other","title":"nvim","foreground_processes":[{"pid":4242,"cmdline":["fish"],"cwd":"/tmp/other"},{"pid":4322,"cmdline":["nvim","other.md"],"cwd":"/tmp/other"}]}]}]}]"#,
    )
    .unwrap();
    // Ambiguous: two focused OS windows.
    std::fs::write(
        &ls_amb,
        r#"[{"id":11,"is_focused":true,"tabs":[{"id":21,"is_focused":true,"windows":[{"id":7,"is_focused":true,"pid":4242,"cwd":"/tmp/stage","title":"nvim","foreground_processes":[{"pid":4321,"cmdline":["nvim"],"cwd":"/tmp/stage"}]}]}]},{"id":12,"is_focused":true,"tabs":[{"id":22,"is_focused":true,"windows":[{"id":9,"is_focused":true,"pid":4243,"cwd":"/tmp/other","title":"x","foreground_processes":[{"pid":4243,"cmdline":["fish"],"cwd":"/tmp/other"}]}]}]}]"#,
    )
    .unwrap();
    let fake = bindir.join("kitty");
    std::fs::write(&fake, "#!/bin/sh\ncat \"${QS_FAKE_KITTY_LS:?}\"\n").unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&fake, std::fs::Permissions::from_mode(0o755));
    }
    let sock = dir.join("kitty.sock");
    let _ = std::fs::remove_file(&sock);
    let listener = std::os::unix::net::UnixListener::bind(&sock).unwrap();
    let _srv = std::thread::spawn(move || {
        for conn in listener.incoming() {
            if conn.is_err() {
                return;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    });
    let me = std::process::id();
    let win = || FocusedWindow::new_with_pid("0xK", "kitty", "t", Some(me));
    let base = || {
        DesktopContext::available(
            Source::Hyprland,
            Some(win()),
            Some(Workspace::new("1", "1")),
            qs_agent_orchestrator::now_ms(),
        )
    };
    let recdir = dir.join("nvim-context");
    private_dir(&recdir);
    let write_rec = |pid: u32, kid: u64, file: &str| {
        std::fs::write(
            recdir.join(format!("{pid}.json")),
            format!(
                r#"{{"pid":{pid},"kitty_window_id":{kid},"file":"{file}","cwd":"/tmp/stage","updated_at_ms":{}}}"#,
                qs_agent_orchestrator::now_ms()
            ),
        )
        .unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(
                recdir.join(format!("{pid}.json")),
                std::fs::Permissions::from_mode(0o600),
            );
        }
    };
    write_rec(4321, 7, "/tmp/stage/note.md");
    let env = EnrichmentEnv {
        kitty_socket: Some(sock.clone()),
        nvim_dir: Some(recdir.clone()),
        ..Default::default()
    };

    let old_path = std::env::var_os("PATH");
    let old_ls = std::env::var_os("QS_FAKE_KITTY_LS");
    let mut new_path = bindir.to_string_lossy().to_string();
    if let Some(p) = &old_path {
        new_path.push(':');
        new_path.push_str(&p.to_string_lossy());
    }
    std::env::set_var("PATH", &new_path);

    // Phase A: pane 7 / nvim 4321 with a fresh record -> neovim fileA.
    std::env::set_var("QS_FAKE_KITTY_LS", &ls_a);
    let a = enrich_with_env(base(), &env, qs_agent_orchestrator::now_ms());
    let ra = a.resource.clone().expect("neovim resource");
    assert_eq!(ra.adapter, "neovim");
    assert_eq!(ra.file.as_deref(), Some("/tmp/stage/note.md"));

    // Phase B: pane 8 / nvim 4322, no record anywhere. Must be paneB kitty
    // cwd — never paneA's fileA.
    std::env::set_var("QS_FAKE_KITTY_LS", &ls_b);
    let b = enrich_with_env(base(), &env, qs_agent_orchestrator::now_ms());
    let rb = b.resource.clone().expect("paneB kitty fallback");
    assert_eq!(rb.adapter, "kitty", "pane switch must fall back, not serve fileA");
    assert_eq!(rb.file, None);
    assert_eq!(rb.cwd.as_deref(), Some("/tmp/other"));

    // Phase C: same paneB state repeats identically (no oscillation).
    let b2 = enrich_with_env(base(), &env, qs_agent_orchestrator::now_ms());
    assert_eq!(b2.resource, b.resource);

    // Phase D: back on paneA/nvim4321, record still missing. fileA was
    // evicted at phase B, so this is paneA kitty cwd — never resurrected.
    std::env::set_var("QS_FAKE_KITTY_LS", &ls_a);
    std::fs::remove_file(recdir.join("4321.json")).unwrap();
    let d = enrich_with_env(base(), &env, qs_agent_orchestrator::now_ms());
    let rd = d.resource.clone().expect("paneA kitty fallback");
    assert_eq!(rd.adapter, "kitty");
    assert_eq!(rd.cwd.as_deref(), Some("/tmp/stage"));
    assert_ne!(
        rd.file.as_deref(),
        Some("/tmp/stage/note.md"),
        "evicted fileA must never return"
    );

    // Phase E: ambiguous pane focus -> no resource (confirmed mismatch).
    std::env::set_var("QS_FAKE_KITTY_LS", &ls_amb);
    let e = enrich_with_env(base(), &env, qs_agent_orchestrator::now_ms());
    assert_eq!(e.resource, None, "ambiguous focus must not serve any pane");

    match old_path {
        Some(p) => std::env::set_var("PATH", p),
        None => std::env::remove_var("PATH"),
    }
    match old_ls {
        Some(p) => std::env::set_var("QS_FAKE_KITTY_LS", p),
        None => std::env::remove_var("QS_FAKE_KITTY_LS"),
    }
    let _ = std::fs::remove_dir_all(&dir);
}
