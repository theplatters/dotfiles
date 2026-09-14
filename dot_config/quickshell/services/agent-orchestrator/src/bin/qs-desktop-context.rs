//! `qs-desktop-context`: singleton Hyprland desktop-activity collector.
//!
//! Modes:
//! - `qs-desktop-context [--db PATH] [collect]` (default): hold the per-DB
//!   advisory lock for life, maintain live context via the library, and
//!   append meaningful snapshots to the private SQLite DB.
//! - `qs-desktop-context [--db PATH] history [--limit N]` /
//!   `history --from START_MS --to END_MS [--limit N]`: read-only history
//!   query printing JSON to stdout (manual validation). Never claims the
//!   persisted latest row as live `current`.
//!
//! All diagnostics go to stderr. Stdout carries ONLY history JSON in
//! `history` mode (no Pi protocol frames).

use qs_agent_orchestrator::{
    acquire_lock, default_db_path, discover_sockets, ensure_parent_dir, lock_path_for,
    run_collector_forever, ActivityStore, StoreError, Tracker, MAX_QUERY_LIMIT,
};
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
    },
}

fn usage() -> String {
    "usage: qs-desktop-context [--db PATH] [collect] | history [--limit N] [--from START_MS --to END_MS [--limit N]]".to_string()
}

fn parse_ms(s: &str, what: &str) -> Result<i64, String> {
    s.parse::<i64>()
        .map_err(|_| format!("{what} must be an integer (UTC epoch-ms), got {s:?}"))
}

fn parse_args(argv: &[String]) -> Result<(PathBuf, Mode), String> {
    let mut db_override: Option<String> = None;
    let mut subcommand: Option<String> = None;
    let mut limit: Option<i64> = None;
    let mut from: Option<i64> = None;
    let mut to: Option<i64> = None;
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
            "--help" | "-h" => return Err(usage()),
            "collect" | "history" => {
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
    let db = match db_override {
        Some(p) => PathBuf::from(p),
        None => default_db_path().map_err(|e| format!("{e}\n{}", usage()))?,
    };
    let mode = match subcommand.as_deref().unwrap_or("collect") {
        "collect" => {
            if limit.is_some() || from.is_some() {
                return Err(format!("--limit/--from/--to are history-only\n{}", usage()));
            }
            Mode::Collect
        }
        "history" => Mode::History {
            limit: limit.unwrap_or(20),
            from,
            to,
        },
        other => return Err(format!("unknown subcommand: {other}\n{}", usage())),
    };
    Ok((db, mode))
}

fn run_history(db: &PathBuf, limit: i64, from: Option<i64>, to: Option<i64>) -> i32 {
    // Validate before the missing-DB fast path so bad limits/ranges fail
    // closed even when the collector has never run.
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
    let rows = if let (Some(f), Some(t)) = (from, to) {
        store.activity_in_range(f, t, limit)
    } else {
        store.recent_activity(limit)
    };
    match rows {
        Ok(rows) => {
            let values: Vec<serde_json::Value> = rows
                .iter()
                .map(|r| {
                    let snapshot: serde_json::Value =
                        serde_json::from_str(&r.snapshot_json).unwrap_or(serde_json::Value::Null);
                    serde_json::json!({
                        "id": r.id,
                        "observed_at_ms": r.observed_at_ms,
                        "kind": r.kind,
                        "source": r.source,
                        "snapshot": snapshot,
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
            eprintln!("qs-desktop-context: history query failed: {e}");
            1
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
        Mode::Collect => run_collect(&db),
        Mode::History { limit, from, to } => run_history(&db, limit, from, to),
    };
    std::process::exit(code);
}
