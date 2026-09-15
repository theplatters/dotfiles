//! `qs-desktop-context`: singleton Hyprland desktop-activity collector +
//! read-only project history queries.
//!
//! Modes:
//! - `qs-desktop-context [--db PATH] [collect]` (default): hold the per-DB
//!   advisory lock for life, maintain live context via the library, and
//!   append meaningful snapshots to the private SQLite DB.
//! - `qs-desktop-context [--db PATH] history [--project UUID] [--limit N] [--from START_MS --to END_MS]`:
//!   read-only history query printing JSON to stdout. Never claims the
//!   persisted latest row as live `current`.
//! - `qs-desktop-context current`: fresh on-demand snapshot with default
//!   enrichment (`enrich_desktop_context`: app + project), rechecked for
//!   focus races. No DB, no latest-row fallback. Prints one `DesktopContext`
//!   object (unavailable object when no compositor or on race/failure).
//! - `qs-desktop-context current-project`: same fresh path, prints the
//!   `ProjectContext` object or `null`.
//! - `qs-desktop-context [--db PATH] last-activity --project UUID`: latest row
//!   for one project or `null`.
//! - `qs-desktop-context [--db PATH] resources --project UUID [--limit N]`:
//!   deduped recently-used resources for one project.
//!
//! All diagnostics go to stderr. Stdout carries ONLY JSON (no Pi frames).
//! `current`/`current-project` are fresh snapshots, not collector-tracker
//! reads and not historical rows. Nothing here installs helpers or spawns
//! installers; enrichment reuses the existing library path.

use qs_agent_orchestrator::{
    acquire_lock, default_db_path, discover_sockets, ensure_parent_dir, lock_path_for,
    run_collector_forever, ActivityStore, StoreError, Tracker, MAX_QUERY_LIMIT,
};
use qs_agent_orchestrator::desktop_context::{now_ms, DesktopContext, Source};
use qs_agent_orchestrator::desktop_store::normalize_project_id;
use qs_agent_orchestrator::hyprland::fetch_snapshot;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};

static SHUTDOWN: AtomicBool = AtomicBool::new(false);

extern "C" fn shutdown_handler(_sig: i32) {
    SHUTDOWN.store(true, Ordering::SeqCst);
}

fn install_signal_handlers() {
    unsafe {
        libc::signal(libc::SIGTERM, shutdown_handler as *const () as usize);
        libc::signal(libc::SIGINT, shutdown_handler as *const () as usize);
        libc::signal(libc::SIGPIPE, libc::SIG_IGN);
    }
}

enum Mode {
    Collect,
    History {
        limit: i64,
        from: Option<i64>,
        to: Option<i64>,
        project: Option<String>,
    },
    Current,
    CurrentProject,
    LastActivity {
        project: String,
    },
    Resources {
        project: String,
        limit: i64,
    },
}

fn usage() -> String {
    "usage: qs-desktop-context [--db PATH] [collect]\n       qs-desktop-context [--db PATH] history [--project UUID] [--limit N] [--from START_MS --to END_MS [--limit N]]\n       qs-desktop-context current\n       qs-desktop-context current-project\n       qs-desktop-context [--db PATH] last-activity --project UUID\n       qs-desktop-context [--db PATH] resources --project UUID [--limit N]".to_string()
}

fn parse_ms(s: &str, what: &str) -> Result<i64, String> {
    s.parse::<i64>()
        .map_err(|_| format!("{what} must be an integer (UTC epoch-ms), got {s:?}"))
}

fn parse_args(argv: &[String]) -> Result<(Option<PathBuf>, Mode), String> {
    let mut db_override: Option<String> = None;
    let mut subcommand: Option<String> = None;
    let mut limit: Option<i64> = None;
    let mut from: Option<i64> = None;
    let mut to: Option<i64> = None;
    let mut project: Option<String> = None;
    let mut i = 1;
    while i < argv.len() {
        match argv[i].as_str() {
            "--db" => {
                i += 1;
                if i >= argv.len() {
                    return Err(format!("--db requires a value\n{}", usage()));
                }
                db_override = Some(argv[i].clone());
            }
            "--limit" => {
                i += 1;
                if i >= argv.len() {
                    return Err(format!("--limit requires a value\n{}", usage()));
                }
                limit = Some(
                    argv[i]
                        .parse::<i64>()
                        .map_err(|_| format!("--limit must be an integer\n{}", usage()))?,
                );
            }
            "--from" => {
                i += 1;
                if i >= argv.len() {
                    return Err(format!("--from requires a value\n{}", usage()));
                }
                from = Some(
                    parse_ms(&argv[i].clone(), "--from")
                        .map_err(|e| format!("{e}\n{}", usage()))?,
                );
            }
            "--to" => {
                i += 1;
                if i >= argv.len() {
                    return Err(format!("--to requires a value\n{}", usage()));
                }
                to = Some(
                    parse_ms(&argv[i].clone(), "--to").map_err(|e| format!("{e}\n{}", usage()))?,
                );
            }
            "--project" => {
                i += 1;
                if i >= argv.len() {
                    return Err(format!("--project requires a value\n{}", usage()));
                }
                if project.is_some() {
                    return Err(format!("duplicate --project\n{}", usage()));
                }
                project = Some(argv[i].clone());
            }
            "--help" | "-h" => return Err(usage()),
            "collect" | "history" | "current" | "current-project" | "last-activity" | "resources" => {
                if subcommand.is_some() {
                    return Err(format!("duplicate subcommand\n{}", usage()));
                }
                subcommand = Some(argv[i].clone());
            }
            other => return Err(format!("unknown argument: {other}\n{}", usage())),
        }
        i += 1;
    }
    if (from.is_some()) != (to.is_some()) {
        return Err(format!(
            "--from and --to must be given together\n{}",
            usage()
        ));
    }
    let mode = match subcommand.as_deref().unwrap_or("collect") {
        "collect" => {
            if limit.is_some() || from.is_some() || project.is_some() {
                return Err(format!(
                    "--limit/--from/--to/--project are query-only (not for collect)\n{}",
                    usage()
                ));
            }
            Mode::Collect
        }
        "history" => Mode::History {
            limit: limit.unwrap_or(20),
            from,
            to,
            project,
        },
        "current" => {
            if limit.is_some() || from.is_some() || project.is_some() {
                return Err(format!(
                    "--limit/--from/--to/--project are not for current\n{}",
                    usage()
                ));
            }
            if db_override.is_some() {
                eprintln!("qs-desktop-context: warning: --db ignored for current (fresh snapshot, no DB)");
            }
            Mode::Current
        }
        "current-project" => {
            if limit.is_some() || from.is_some() || project.is_some() {
                return Err(format!(
                    "--limit/--from/--to/--project are not for current-project\n{}",
                    usage()
                ));
            }
            if db_override.is_some() {
                eprintln!("qs-desktop-context: warning: --db ignored for current-project (fresh snapshot, no DB)");
            }
            Mode::CurrentProject
        }
        "last-activity" => {
            let p = project.ok_or_else(|| format!("--project UUID is required for last-activity\n{}", usage()))?;
            if limit.is_some() || from.is_some() {
                return Err(format!(
                    "--limit/--from/--to are not for last-activity\n{}",
                    usage()
                ));
            }
            Mode::LastActivity { project: p }
        }
        "resources" => {
            let p = project.ok_or_else(|| format!("--project UUID is required for resources\n{}", usage()))?;
            if from.is_some() {
                return Err(format!("--from/--to are not for resources\n{}", usage()));
            }
            Mode::Resources {
                project: p,
                limit: limit.unwrap_or(20),
            }
        }
        other => return Err(format!("unknown subcommand: {other}\n{}", usage())),
    };
    // `current` modes never resolve the default DB path (no HOME requirement,
    // no initialization). Query/collect modes resolve it.
    let db = match mode {
        Mode::Current | Mode::CurrentProject => None,
        _ => {
            let db = match db_override {
                Some(p) => PathBuf::from(p),
                None => default_db_path().map_err(|e| format!("{e}\n{}", usage()))?,
            };
            Some(db)
        }
    };
    Ok((db, mode))
}

fn row_to_json(r: &qs_agent_orchestrator::desktop_store::ActivityRecord) -> serde_json::Value {
    let snapshot: serde_json::Value =
        serde_json::from_str(&r.snapshot_json).unwrap_or(serde_json::Value::Null);
    serde_json::json!({
        "id": r.id,
        "observed_at_ms": r.observed_at_ms,
        "kind": r.kind,
        "source": r.source,
        "snapshot": snapshot,
    })
}

fn run_history(
    db: &PathBuf,
    limit: i64,
    from: Option<i64>,
    to: Option<i64>,
    project: Option<String>,
) -> i32 {
    // Validate before the missing-DB fast path so bad args fail closed even
    // when the collector has never run.
    if limit < 1 || limit > MAX_QUERY_LIMIT {
        eprintln!("qs-desktop-context: history query failed: invalid argument: limit must be 1..={MAX_QUERY_LIMIT}, got {limit}");
        return 1;
    }
    if let (Some(f), Some(t)) = (from, to) {
        if f < 0 || t < 0 || f > t {
            eprintln!("qs-desktop-context: history query failed: invalid argument: invalid range {f}..{t} (start inclusive, end exclusive, >= 0)");
            return 1;
        }
    }
    let norm_project: Option<String> = match project {
        Some(p) => match normalize_project_id(&p) {
            Ok(n) => Some(n),
            Err(e) => {
                eprintln!("qs-desktop-context: history query failed: {e}");
                return 1;
            }
        },
        None => None,
    };
    // Strictly read-only: never create files, schemas, or journals and
    // never claim the lock. Missing DB reads as empty only when the file
    // simply does not exist (clear message, not fake rows). Permission and
    // foreign-schema failures are distinct errors.
    let store = match ActivityStore::open_read_only(db) {
        Ok(s) => s,
        Err(StoreError::NotFound(msg)) => {
            eprintln!("qs-desktop-context: {msg}");
            println!("[]");
            return 0;
        }
        Err(StoreError::PermissionDenied(msg)) => {
            eprintln!(
                "qs-desktop-context: permission denied opening {}: {msg}",
                db.display()
            );
            return 1;
        }
        Err(e) => {
            eprintln!("qs-desktop-context: cannot open {}: {e}", db.display());
            return 1;
        }
    };
    let rows = match (norm_project, from, to) {
        (Some(pid), Some(f), Some(t)) => store.activity_in_range_for_project(&pid, f, t, limit),
        (Some(pid), _, _) => {
            if from.is_some() || to.is_some() {
                eprintln!("qs-desktop-context: history query failed: --from/--to must be given together");
                return 1;
            }
            store.recent_activity_for_project(&pid, limit)
        }
        (None, Some(f), Some(t)) => store.activity_in_range(f, t, limit),
        (None, _, _) => store.recent_activity(limit),
    };
    match rows {
        Ok(rows) => {
            let values: Vec<serde_json::Value> = rows.iter().map(row_to_json).collect();
            println!(
                "{}",
                serde_json::to_string_pretty(&values).unwrap_or_else(|_| "[]".to_string())
            );
            0
        }
        Err(e) => {
            eprintln!("qs-desktop-context: history query failed: {e}");
            1
        }
    }
}

fn run_last_activity(db: &PathBuf, project: &str) -> i32 {
    let pid = match normalize_project_id(project) {
        Ok(n) => n,
        Err(e) => {
            eprintln!("qs-desktop-context: last-activity query failed: {e}");
            return 1;
        }
    };
    let store = match ActivityStore::open_read_only(db) {
        Ok(s) => s,
        Err(StoreError::NotFound(msg)) => {
            eprintln!("qs-desktop-context: {msg}");
            println!("null");
            return 0;
        }
        Err(StoreError::PermissionDenied(msg)) => {
            eprintln!(
                "qs-desktop-context: permission denied opening {}: {msg}",
                db.display()
            );
            return 1;
        }
        Err(e) => {
            eprintln!("qs-desktop-context: cannot open {}: {e}", db.display());
            return 1;
        }
    };
    match store.last_activity_for_project(&pid) {
        Ok(Some(row)) => {
            println!(
                "{}",
                serde_json::to_string_pretty(&row_to_json(&row))
                    .unwrap_or_else(|_| "null".to_string())
            );
            0
        }
        Ok(None) => {
            println!("null");
            0
        }
        Err(e) => {
            eprintln!("qs-desktop-context: last-activity query failed: {e}");
            1
        }
    }
}

fn run_resources(db: &PathBuf, project: &str, limit: i64) -> i32 {
    if limit < 1 || limit > MAX_QUERY_LIMIT {
        eprintln!("qs-desktop-context: resources query failed: invalid argument: limit must be 1..={MAX_QUERY_LIMIT}, got {limit}");
        return 1;
    }
    let pid = match normalize_project_id(project) {
        Ok(n) => n,
        Err(e) => {
            eprintln!("qs-desktop-context: resources query failed: {e}");
            return 1;
        }
    };
    let store = match ActivityStore::open_read_only(db) {
        Ok(s) => s,
        Err(StoreError::NotFound(msg)) => {
            eprintln!("qs-desktop-context: {msg}");
            println!("[]");
            return 0;
        }
        Err(StoreError::PermissionDenied(msg)) => {
            eprintln!(
                "qs-desktop-context: permission denied opening {}: {msg}",
                db.display()
            );
            return 1;
        }
        Err(e) => {
            eprintln!("qs-desktop-context: cannot open {}: {e}", db.display());
            return 1;
        }
    };
    match store.recently_used_resources_for_project(&pid, limit) {
        Ok(items) => {
            let values: Vec<serde_json::Value> = items
                .iter()
                .map(|it| {
                    serde_json::json!({
                        "resource": it.resource,
                        "observed_at_ms": it.observed_at_ms,
                        "activity_id": it.activity_id,
                    })
                })
                .collect();
            println!(
                "{}",
                serde_json::to_string_pretty(&values).unwrap_or_else(|_| "[]".to_string())
            );
            0
        }
        Err(e) => {
            eprintln!("qs-desktop-context: resources query failed: {e}");
            1
        }
    }
}

/// Fresh on-demand snapshot with default enrichment (app + project).
///
/// Intentionally a new snapshot, not a collector-tracker read and not a
/// historical row: discovers sockets, fetches, enriches, then re-fetches to
/// reject a snapshot that changed mid-enrichment (focus/window/app/PID/
/// title/workspace). Returns an unavailable object (no project) when there
/// is no compositor, a fetch fails, or the race check trips. Never touches
/// the DB.
fn fresh_current_context() -> DesktopContext {
    let now_unavailable = || DesktopContext::unavailable(Source::Hyprland, now_ms());
    let Some(paths) = discover_sockets() else {
        eprintln!("qs-desktop-context: no hyprland sockets; reporting unavailable");
        return now_unavailable();
    };
    let base = match fetch_snapshot(&paths.request_socket) {
        Ok(ctx) => ctx,
        Err(e) => {
            eprintln!("qs-desktop-context: snapshot failed: {e}; reporting unavailable");
            return now_unavailable();
        }
    };
    // Default enrichment: application providers + deterministic project
    // resolution (thread-local cached registry). May spawn the validated
    // Python helper; never installs anything.
    let enriched = qs_agent_orchestrator::enrich_desktop_context(base.clone());
    // Recheck after enrichment: reject a stale snapshot when focus moved.
    let recheck = match fetch_snapshot(&paths.request_socket) {
        Ok(ctx) => ctx,
        Err(e) => {
            eprintln!("qs-desktop-context: recheck snapshot failed: {e}; reporting unavailable");
            return now_unavailable();
        }
    };
    if !same_focus_for_current(&base, &recheck) {
        eprintln!("qs-desktop-context: focus changed during enrichment; discarding stale snapshot");
        return now_unavailable();
    }
    // Stable: refresh the timestamp to completion time (like the collector's
    // enriched rows) so the result is fresh, not the pre-enrich time.
    let mut out = enriched;
    out.observed_at_ms = now_ms();
    out
}

/// Focus equality for the `current` race check: opaque window id +
/// application + title + client PID plus workspace. PID is included here
/// (unlike history dedup) so a reused address for a new client rejects.
fn same_focus_for_current(a: &DesktopContext, b: &DesktopContext) -> bool {
    match (a.focused_window.as_ref(), b.focused_window.as_ref()) {
        (None, None) => {}
        (Some(x), Some(y)) => {
            if x.id != y.id
                || x.application != y.application
                || x.title != y.title
                || x.process_id != y.process_id
            {
                return false;
            }
        }
        _ => return false,
    }
    if a.workspace != b.workspace {
        return false;
    }
    true
}

fn run_current() -> i32 {
    let ctx = fresh_current_context();
    match serde_json::to_string_pretty(&ctx) {
        Ok(s) => {
            println!("{s}");
            0
        }
        Err(e) => {
            eprintln!("qs-desktop-context: current serialize failed: {e}");
            1
        }
    }
}

fn run_current_project() -> i32 {
    let ctx = fresh_current_context();
    match &ctx.project {
        Some(p) => match serde_json::to_string_pretty(p) {
            Ok(s) => {
                println!("{s}");
                0
            }
            Err(e) => {
                eprintln!("qs-desktop-context: current-project serialize failed: {e}");
                1
            }
        },
        None => {
            println!("null");
            0
        }
    }
}

fn run_collect(db: &PathBuf) -> i32 {
    if let Err(e) = ensure_parent_dir(db) {
        eprintln!("qs-desktop-context: cannot prepare {}: {e}", db.display());
        return 1;
    }
    let lock_path = lock_path_for(db);
    let _lock = match acquire_lock(&lock_path) {
        Ok(g) => g,
        Err(e) => {
            eprintln!("qs-desktop-context: cannot start (singleton lock): {e}");
            return 1;
        }
    };
    let store = match ActivityStore::open(db) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("qs-desktop-context: cannot open {}: {e}", db.display());
            return 1;
        }
    };
    eprintln!("qs-desktop-context: collecting to {}", db.display());
    install_signal_handlers();
    // Live context only: a fresh Tracker (never backfilled from rows).
    let mut tracker = Tracker::new();
    let remaining = run_collector_forever(&store, &mut tracker, &SHUTDOWN, discover_sockets);
    if remaining > 0 {
        eprintln!(
            "qs-desktop-context: shutting down with {remaining} pending observations unpersisted (in-memory backlog lost; no durable spool)"
        );
        1
    } else {
        eprintln!("qs-desktop-context: shutting down");
        0
    }
}

fn main() {
    let argv: Vec<String> = std::env::args().collect();
    let (db, mode) = match parse_args(&argv) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("qs-desktop-context: {e}");
            std::process::exit(2);
        }
    };
    let code = match mode {
        Mode::Collect => run_collect(db.as_ref().expect("collect needs db")),
        Mode::History { limit, from, to, project } => {
            run_history(db.as_ref().expect("history needs db"), limit, from, to, project)
        }
        Mode::Current => run_current(),
        Mode::CurrentProject => run_current_project(),
        Mode::LastActivity { project } => {
            run_last_activity(db.as_ref().expect("last-activity needs db"), &project)
        }
        Mode::Resources { project, limit } => {
            run_resources(db.as_ref().expect("resources needs db"), &project, limit)
        }
    };
    std::process::exit(code);
}
