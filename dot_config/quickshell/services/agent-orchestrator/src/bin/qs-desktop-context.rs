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

use qs_agent_orchestrator::desktop_context::{now_ms, DesktopContext, Source};
use qs_agent_orchestrator::desktop_store::normalize_project_id;
use qs_agent_orchestrator::hyprland::fetch_snapshot;
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

#[derive(Debug)]
enum Mode {
    Collect {
        gap_ms: Option<i64>,
        interruption_ms: Option<i64>,
    },
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
    CurrentSession,
    Sessions {
        limit: i64,
        from: Option<i64>,
        to: Option<i64>,
        project: Option<String>,
    },
    LastSession {
        project: String,
    },
    SessionResources {
        session: String,
        limit: i64,
    },
    SessionEvents {
        session: String,
        limit: i64,
    },
    Search {
        limit: i64,
        from: Option<i64>,
        to: Option<i64>,
        project: Option<String>,
        application: Option<String>,
        resource: Option<String>,
        device: Option<String>,
        query: Option<String>,
    },
    SessionDetail {
        session: String,
        resource_limit: i64,
        include_events: bool,
        event_limit: i64,
    },
    DeviceId,
}

fn usage() -> String {
    "usage: qs-desktop-context [--db PATH] [collect [--session-gap-ms MS --session-interruption-ms MS]]\n       qs-desktop-context [--db PATH] history [--project UUID] [--limit N] [--from START_MS --to END_MS [--limit N]]\n       qs-desktop-context current\n       qs-desktop-context current-project\n       qs-desktop-context [--db PATH] last-activity --project UUID\n       qs-desktop-context [--db PATH] resources --project UUID [--limit N]\n       qs-desktop-context [--db PATH] current-session\n       qs-desktop-context [--db PATH] sessions [--project UUID] [--limit N] [--from START_MS --to END_MS]\n       qs-desktop-context [--db PATH] last-session --project UUID\n       qs-desktop-context [--db PATH] session-resources --session SESSION_ID [--limit N]\n       qs-desktop-context [--db PATH] session-events --session SESSION_ID [--limit N]\n       qs-desktop-context [--db PATH] search [--project UUID] [--application TEXT] [--resource TEXT] [--device 32HEX] [--query TEXT] [--from START_MS --to END_MS] [--limit N]\n       qs-desktop-context [--db PATH] session-detail --session SESSION_ID [--resource-limit N] [--include-events [--event-limit N]]\n       qs-desktop-context [--db PATH] device-id".to_string()
}

fn parse_ms(s: &str, what: &str) -> Result<i64, String> {
    s.parse::<i64>()
        .map_err(|_| format!("{what} must be an integer (UTC epoch-ms), got {s:?}"))
}

fn parse_session_ms(s: &str, what: &str) -> Result<i64, String> {
    s.parse::<i64>()
        .map_err(|_| format!("{what} must be an integer (ms), got {s:?}"))
}

fn parse_args(argv: &[String]) -> Result<(Option<PathBuf>, Mode), String> {
    let mut db_override: Option<String> = None;
    let mut subcommand: Option<String> = None;
    let mut limit: Option<i64> = None;
    let mut from: Option<i64> = None;
    let mut to: Option<i64> = None;
    let mut project: Option<String> = None;
    let mut session: Option<String> = None;
    let mut gap_ms: Option<i64> = None;
    let mut interruption_ms: Option<i64> = None;
    let mut application: Option<String> = None;
    let mut resource: Option<String> = None;
    let mut device: Option<String> = None;
    let mut query: Option<String> = None;
    let mut resource_limit: Option<i64> = None;
    let mut event_limit: Option<i64> = None;
    let mut include_events = false;
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
            "--session" => {
                i += 1;
                if i >= argv.len() {
                    return Err(format!("--session requires a value\n{}", usage()));
                }
                if session.is_some() {
                    return Err(format!("duplicate --session\n{}", usage()));
                }
                session = Some(argv[i].clone());
            }
            "--session-gap-ms" => {
                i += 1;
                if i >= argv.len() {
                    return Err(format!("--session-gap-ms requires a value\n{}", usage()));
                }
                gap_ms = Some(
                    parse_session_ms(&argv[i].clone(), "--session-gap-ms")
                        .map_err(|e| format!("{e}\n{}", usage()))?,
                );
            }
            "--session-interruption-ms" => {
                i += 1;
                if i >= argv.len() {
                    return Err(format!(
                        "--session-interruption-ms requires a value\n{}",
                        usage()
                    ))?;
                }
                interruption_ms = Some(
                    parse_session_ms(&argv[i].clone(), "--session-interruption-ms")
                        .map_err(|e| format!("{e}\n{}", usage()))?,
                );
            }
            "--application" => {
                i += 1;
                if i >= argv.len() {
                    return Err(format!("--application requires a value\n{}", usage()));
                }
                if application.is_some() {
                    return Err(format!("duplicate --application\n{}", usage()));
                }
                application = Some(argv[i].clone());
            }
            "--resource" => {
                i += 1;
                if i >= argv.len() {
                    return Err(format!("--resource requires a value\n{}", usage()));
                }
                if resource.is_some() {
                    return Err(format!("duplicate --resource\n{}", usage()));
                }
                resource = Some(argv[i].clone());
            }
            "--device" => {
                i += 1;
                if i >= argv.len() {
                    return Err(format!("--device requires a value\n{}", usage()));
                }
                if device.is_some() {
                    return Err(format!("duplicate --device\n{}", usage()));
                }
                device = Some(argv[i].clone());
            }
            "--query" => {
                i += 1;
                if i >= argv.len() {
                    return Err(format!("--query requires a value\n{}", usage()));
                }
                if query.is_some() {
                    return Err(format!("duplicate --query\n{}", usage()));
                }
                query = Some(argv[i].clone());
            }
            "--resource-limit" => {
                i += 1;
                if i >= argv.len() {
                    return Err(format!("--resource-limit requires a value\n{}", usage()));
                }
                resource_limit =
                    Some(argv[i].parse::<i64>().map_err(|_| {
                        format!("--resource-limit must be an integer\n{}", usage())
                    })?);
            }
            "--event-limit" => {
                i += 1;
                if i >= argv.len() {
                    return Err(format!("--event-limit requires a value\n{}", usage()));
                }
                event_limit = Some(
                    argv[i]
                        .parse::<i64>()
                        .map_err(|_| format!("--event-limit must be an integer\n{}", usage()))?,
                );
            }
            "--include-events" => {
                include_events = true;
            }
            "--help" | "-h" => return Err(usage()),
            "collect" | "history" | "current" | "current-project" | "last-activity"
            | "resources" | "current-session" | "sessions" | "last-session"
            | "session-resources" | "session-events" | "search" | "session-detail"
            | "device-id" => {
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
    let collect_only = |what: &str| -> Result<(), String> {
        if subcommand.as_deref().unwrap_or("collect") != "collect" {
            return Err(format!(
                "{what} is collect-only (not for queries)\n{}",
                usage()
            ));
        }
        Ok(())
    };
    if gap_ms.is_some() || interruption_ms.is_some() {
        // Session thresholds are collect-only overrides.
        if subcommand.as_deref().unwrap_or("collect") != "collect" {
            return Err(format!(
                "--session-gap-ms/--session-interruption-ms are collect-only\n{}",
                usage()
            ));
        }
    }
    if session.is_some() {
        match subcommand.as_deref() {
            Some("session-resources") | Some("session-events") | Some("session-detail") => {}
            _ => {
                return Err(format!(
                    "--session is only for session-resources/session-events/session-detail\n{}",
                    usage()
                ))
            }
        }
    }
    // Search-only text/device flags reject on other subcommands below per-mode.
    let is_search = subcommand.as_deref() == Some("search");
    let is_detail = subcommand.as_deref() == Some("session-detail");
    if (application.is_some() || resource.is_some() || device.is_some() || query.is_some())
        && !is_search
    {
        return Err(format!(
            "--application/--resource/--device/--query are only for search\n{}",
            usage()
        ));
    }
    if (resource_limit.is_some() || event_limit.is_some() || include_events) && !is_detail {
        return Err(format!(
            "--resource-limit/--event-limit/--include-events are only for session-detail\n{}",
            usage()
        ));
    }
    if event_limit.is_some() && !include_events {
        return Err(format!(
            "--event-limit requires --include-events\n{}",
            usage()
        ));
    }
    let mode = match subcommand.as_deref().unwrap_or("collect") {
        "collect" => {
            if limit.is_some()
                || from.is_some()
                || project.is_some()
                || session.is_some()
                || application.is_some()
                || resource.is_some()
                || device.is_some()
                || query.is_some()
                || resource_limit.is_some()
                || event_limit.is_some()
                || include_events
            {
                return Err(format!(
                    "--limit/--from/--to/--project/--session/--application/--resource/--device/--query/--resource-limit/--event-limit/--include-events are query-only (not for collect)\n{}",
                    usage()
                ));
            }
            Mode::Collect {
                gap_ms,
                interruption_ms,
            }
        }
        "history" => {
            if session.is_some() || gap_ms.is_some() || interruption_ms.is_some() {
                return Err(format!(
                    "--session/session-gap are not for history\n{}",
                    usage()
                ));
            }
            Mode::History {
                limit: limit.unwrap_or(20),
                from,
                to,
                project,
            }
        }
        "current" => {
            if limit.is_some() || from.is_some() || project.is_some() || session.is_some() {
                return Err(format!(
                    "--limit/--from/--to/--project/--session are not for current\n{}",
                    usage()
                ));
            }
            let _ = collect_only("--session-gap-ms");
            if db_override.is_some() {
                eprintln!(
                    "qs-desktop-context: warning: --db ignored for current (fresh snapshot, no DB)"
                );
            }
            Mode::Current
        }
        "current-project" => {
            if limit.is_some() || from.is_some() || project.is_some() || session.is_some() {
                return Err(format!(
                    "--limit/--from/--to/--project/--session are not for current-project\n{}",
                    usage()
                ));
            }
            if db_override.is_some() {
                eprintln!("qs-desktop-context: warning: --db ignored for current-project (fresh snapshot, no DB)");
            }
            Mode::CurrentProject
        }
        "last-activity" => {
            let p = project.ok_or_else(|| {
                format!("--project UUID is required for last-activity\n{}", usage())
            })?;
            if limit.is_some() || from.is_some() || session.is_some() {
                return Err(format!(
                    "--limit/--from/--to/--session are not for last-activity\n{}",
                    usage()
                ));
            }
            Mode::LastActivity { project: p }
        }
        "resources" => {
            let p = project
                .ok_or_else(|| format!("--project UUID is required for resources\n{}", usage()))?;
            if from.is_some() || session.is_some() {
                return Err(format!(
                    "--from/--to/--session are not for resources\n{}",
                    usage()
                ));
            }
            Mode::Resources {
                project: p,
                limit: limit.unwrap_or(20),
            }
        }
        "current-session" => {
            if limit.is_some() || from.is_some() || project.is_some() || session.is_some() {
                return Err(format!(
                    "--limit/--from/--to/--project/--session are not for current-session\n{}",
                    usage()
                ));
            }
            Mode::CurrentSession
        }
        "sessions" => {
            if session.is_some() {
                return Err(format!("--session is not for sessions\n{}", usage()));
            }
            Mode::Sessions {
                limit: limit.unwrap_or(20),
                from,
                to,
                project,
            }
        }
        "last-session" => {
            let p = project.ok_or_else(|| {
                format!("--project UUID is required for last-session\n{}", usage())
            })?;
            if limit.is_some() || from.is_some() || session.is_some() {
                return Err(format!(
                    "--limit/--from/--to/--session are not for last-session\n{}",
                    usage()
                ));
            }
            Mode::LastSession { project: p }
        }
        "session-resources" => {
            let s = session.ok_or_else(|| {
                format!(
                    "--session SESSION_ID is required for session-resources\n{}",
                    usage()
                )
            })?;
            if project.is_some() || from.is_some() {
                return Err(format!(
                    "--project/--from/--to are not for session-resources\n{}",
                    usage()
                ));
            }
            Mode::SessionResources {
                session: s,
                limit: limit.unwrap_or(20),
            }
        }
        "session-events" => {
            let s = session.ok_or_else(|| {
                format!(
                    "--session SESSION_ID is required for session-events\n{}",
                    usage()
                )
            })?;
            if project.is_some() || from.is_some() {
                return Err(format!(
                    "--project/--from/--to are not for session-events\n{}",
                    usage()
                ));
            }
            Mode::SessionEvents {
                session: s,
                limit: limit.unwrap_or(20),
            }
        }
        "search" => {
            if session.is_some()
                || gap_ms.is_some()
                || interruption_ms.is_some()
                || resource_limit.is_some()
                || event_limit.is_some()
                || include_events
            {
                return Err(format!(
                    "--session/session-gap/--resource-limit/--event-limit/--include-events are not for search\n{}",
                    usage()
                ));
            }
            Mode::Search {
                limit: limit.unwrap_or(20),
                from,
                to,
                project,
                application,
                resource,
                device,
                query,
            }
        }
        "session-detail" => {
            let s = session.ok_or_else(|| {
                format!(
                    "--session SESSION_ID is required for session-detail\n{}",
                    usage()
                )
            })?;
            if project.is_some()
                || from.is_some()
                || limit.is_some()
                || application.is_some()
                || resource.is_some()
                || device.is_some()
                || query.is_some()
            {
                return Err(format!(
                    "--project/--from/--to/--limit/--application/--resource/--device/--query are not for session-detail\n{}",
                    usage()
                ));
            }
            Mode::SessionDetail {
                session: s,
                resource_limit: resource_limit.unwrap_or(20),
                include_events,
                event_limit: event_limit.unwrap_or(20),
            }
        }
        "device-id" => {
            if limit.is_some()
                || from.is_some()
                || project.is_some()
                || session.is_some()
                || application.is_some()
                || resource.is_some()
                || device.is_some()
                || query.is_some()
                || resource_limit.is_some()
                || event_limit.is_some()
                || include_events
            {
                return Err(format!(
                    "--limit/--from/--to/--project/--session/--application/--resource/--device/--query/--resource-limit/--event-limit/--include-events are not for device-id\n{}",
                    usage()
                ));
            }
            Mode::DeviceId
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
    let mut v = serde_json::json!({
        "id": r.id,
        "observed_at_ms": r.observed_at_ms,
        "kind": r.kind,
        "source": r.source,
        "snapshot": snapshot,
    });
    // Phase 4 provenance (always populated on v3 rows).
    v["event_id"] = r.event_id.clone().into();
    v["device_id"] = r.device_id.clone().into();
    v["session_id"] = r.session_id.clone().into();
    v
}

/// Session JSON with query-time effective state computed at one captured
/// `now` per response (see [`SessionRecord::effective_status_at`];
/// read-only queries never mutate).
fn session_to_json(
    r: &qs_agent_orchestrator::desktop_store::SessionRecord,
    now_ms: i64,
) -> serde_json::Value {
    let project = match (&r.project_id, &r.project_name) {
        (Some(id), name) => {
            let mut o = serde_json::json!({"id": id});
            if let Some(n) = name {
                o["name"] = serde_json::Value::String(n.clone());
            }
            Some(o)
        }
        (None, _) => None,
    };
    serde_json::json!({
        "session_id": r.session_id,
        "device_id": r.device_id,
        "project": project,
        "start_ms": r.start_ms,
        "end_ms": r.end_ms,
        "first_activity_id": r.first_activity_id,
        "last_activity_id": r.last_activity_id,
        "event_count": r.event_count,
        "status": r.status,
        "ended_reason": r.ended_reason,
        "unresolved_start_ms": r.unresolved_start_ms,
        "active": r.is_active_at(now_ms),
        "effective_status": r.effective_status_at(now_ms),
        "applications": r.applications,
        "gap_ms": r.gap_ms,
        "interruption_ms": r.interruption_ms,
    })
}

fn session_resource_to_json(
    r: &qs_agent_orchestrator::desktop_store::SessionResourceRecord,
) -> serde_json::Value {
    serde_json::json!({
        "session_id": r.session_id,
        "resource_key": r.resource_key,
        "kind": r.kind,
        "portable_identity": r.portable_identity,
        "local_identity": r.local_identity,
        "resource": r.resource,
        "occurrence_count": r.occurrence_count,
        "first_seen_ms": r.first_seen_ms,
        "last_seen_ms": r.last_seen_ms,
        "first_activity_id": r.first_activity_id,
        "last_activity_id": r.last_activity_id,
    })
}

/// Compact session summary for `search`: stable IDs, bounds, counts, status,
/// effective status, newest matching observation, apps, and matched
/// resources. Omits low-value thresholds (`gap_ms`/`interruption_ms`) and
/// bound IDs (`first/last_activity_id`, `unresolved_start_ms`).
fn search_result_to_compact(
    r: &qs_agent_orchestrator::desktop_store::SessionRecord,
    matched_at_ms: Option<i64>,
    resources: &[qs_agent_orchestrator::desktop_store::SessionResourceRecord],
    now_ms: i64,
) -> serde_json::Value {
    let project = match (&r.project_id, &r.project_name) {
        (Some(id), name) => {
            let mut o = serde_json::json!({"id": id});
            if let Some(n) = name {
                o["name"] = serde_json::Value::String(n.clone());
            }
            Some(o)
        }
        (None, _) => None,
    };
    serde_json::json!({
        "session_id": r.session_id,
        "device_id": r.device_id,
        "project": project,
        "start_ms": r.start_ms,
        "end_ms": r.end_ms,
        "event_count": r.event_count,
        "status": r.status,
        "ended_reason": r.ended_reason,
        "active": r.is_active_at(now_ms),
        "effective_status": r.effective_status_at(now_ms),
        "matched_at_ms": matched_at_ms,
        "applications": r.applications,
        "resources": resources.iter().map(session_resource_to_json).collect::<Vec<_>>(),
    })
}

fn validate_search_text_cli(field: &str, v: &str) -> Result<String, String> {
    let t = v.trim();
    if t.is_empty() {
        return Err(format!("--{field} must be nonempty (non-whitespace)"));
    }
    if t.chars().count() > qs_agent_orchestrator::desktop_store::MAX_SEARCH_TEXT_CHARS {
        return Err(format!(
            "--{field} must be 1..={} chars",
            qs_agent_orchestrator::desktop_store::MAX_SEARCH_TEXT_CHARS
        ));
    }
    Ok(t.to_string())
}

#[allow(clippy::too_many_arguments)]
fn run_search(
    db: &PathBuf,
    limit: i64,
    from: Option<i64>,
    to: Option<i64>,
    project: Option<String>,
    application: Option<String>,
    resource: Option<String>,
    device: Option<String>,
    query: Option<String>,
) -> i32 {
    // Validate before the missing-DB fast path so bad args fail closed even
    // when the collector has never run.
    if limit < 1 || limit > MAX_QUERY_LIMIT {
        eprintln!("qs-desktop-context: search query failed: invalid argument: limit must be 1..={MAX_QUERY_LIMIT}, got {limit}");
        return 1;
    }
    if let (Some(f), Some(t)) = (from, to) {
        if f < 0 || t < 0 || f > t {
            eprintln!("qs-desktop-context: search query failed: invalid argument: invalid range {f}..{t} (start inclusive, end exclusive, UTC epoch-ms, >= 0)");
            return 1;
        }
    }
    let norm_project: Option<String> = match project {
        Some(p) => match normalize_project_id(&p) {
            Ok(n) => Some(n),
            Err(e) => {
                eprintln!("qs-desktop-context: search query failed: {e}");
                return 1;
            }
        },
        None => None,
    };
    let norm_device: Option<String> = match device {
        Some(d) => match qs_agent_orchestrator::normalize_hex_id(&d, "device_id") {
            Ok(n) => Some(n),
            Err(e) => {
                eprintln!("qs-desktop-context: search query failed: invalid argument: {e}");
                return 1;
            }
        },
        None => None,
    };
    for (field, v) in [
        ("application", application.as_deref()),
        ("resource", resource.as_deref()),
        ("query", query.as_deref()),
    ] {
        if let Some(text) = v {
            if let Err(e) = validate_search_text_cli(field, text) {
                eprintln!("qs-desktop-context: search query failed: invalid argument: {e}");
                return 1;
            }
        }
    }
    // Echoable normalized query (explicit UTC/range semantics).
    let query_json = serde_json::json!({
        "project": norm_project,
        "application": application,
        "resource": resource,
        "device": norm_device,
        "query": query,
        "from_ms": from,
        "to_ms": to,
        "range_semantics": if from.is_some() { Some("start inclusive, end exclusive, UTC epoch-ms") } else { None },
        "limit": limit,
    });
    let store = match ActivityStore::open_read_only(db) {
        Ok(s) => s,
        Err(StoreError::NotFound(msg)) => {
            eprintln!("qs-desktop-context: {msg}");
            println!(
                "{}",
                serde_json::to_string_pretty(&serde_json::json!({
                    "query": query_json,
                    "sessions": [],
                    "count": 0,
                }))
                .unwrap_or_else(|_| "{\"sessions\":[],\"count\":0}".to_string())
            );
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
    let q = qs_agent_orchestrator::desktop_store::SessionSearchQuery {
        start_ms: from,
        end_ms: to,
        project_id: norm_project,
        application,
        resource,
        device_id: norm_device,
        query,
        limit,
    };
    match store.search_sessions(&q) {
        Ok(hits) => {
            let now = qs_agent_orchestrator::now_ms();
            let sessions: Vec<serde_json::Value> = hits
                .iter()
                .map(|h| search_result_to_compact(&h.session, h.matched_at_ms, &h.resources, now))
                .collect();
            let count = sessions.len();
            println!(
                "{}",
                serde_json::to_string_pretty(&serde_json::json!({
                    "query": query_json,
                    "sessions": sessions,
                    "count": count,
                }))
                .unwrap_or_else(|_| "{\"sessions\":[],\"count\":0}".to_string())
            );
            0
        }
        Err(e) => {
            eprintln!("qs-desktop-context: search query failed: {e}");
            1
        }
    }
}

fn run_session_detail(
    db: &PathBuf,
    session: &str,
    resource_limit: i64,
    include_events: bool,
    event_limit: i64,
) -> i32 {
    if resource_limit < 1 || resource_limit > MAX_QUERY_LIMIT {
        eprintln!("qs-desktop-context: session-detail query failed: invalid argument: --resource-limit must be 1..={MAX_QUERY_LIMIT}, got {resource_limit}");
        return 1;
    }
    if include_events && (event_limit < 1 || event_limit > MAX_QUERY_LIMIT) {
        eprintln!("qs-desktop-context: session-detail query failed: invalid argument: --event-limit must be 1..={MAX_QUERY_LIMIT}, got {event_limit}");
        return 1;
    }
    if qs_agent_orchestrator::normalize_session_id(session).is_err() {
        eprintln!("qs-desktop-context: session-detail query failed: invalid argument: session id must be 32 hex chars (128-bit)");
        return 1;
    }
    let store = match ActivityStore::open_read_only(db) {
        Ok(s) => s,
        Err(StoreError::NotFound(msg)) => {
            eprintln!("qs-desktop-context: {msg}");
            let mut v = serde_json::json!({
                "session": serde_json::Value::Null,
                "resources": [],
                "events_included": include_events,
            });
            if include_events {
                v["events"] = serde_json::json!([]);
            }
            println!(
                "{}",
                serde_json::to_string_pretty(&v)
                    .unwrap_or_else(|_| "{\"session\":null}".to_string())
            );
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
    match store.session_detail(session, resource_limit, include_events, event_limit) {
        Ok(detail) => {
            let now = qs_agent_orchestrator::now_ms();
            let session_json = match &detail.session {
                Some(s) => session_to_json(s, now),
                None => serde_json::Value::Null,
            };
            let mut v = serde_json::json!({
                "session": session_json,
                "resources": detail.resources.iter().map(session_resource_to_json).collect::<Vec<_>>(),
                "events_included": detail.events_included,
            });
            if detail.events_included {
                v["events"] = detail
                    .events
                    .iter()
                    .map(row_to_json)
                    .collect::<Vec<_>>()
                    .into();
            }
            println!(
                "{}",
                serde_json::to_string_pretty(&v)
                    .unwrap_or_else(|_| "{\"session\":null}".to_string())
            );
            0
        }
        Err(e) => {
            eprintln!("qs-desktop-context: session-detail query failed: {e}");
            1
        }
    }
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
                eprintln!(
                    "qs-desktop-context: history query failed: --from/--to must be given together"
                );
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

fn run_current_session(db: &PathBuf) -> i32 {
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
    let now = now_ms();
    match store.current_session(now) {
        Ok(Some(s)) => {
            println!(
                "{}",
                serde_json::to_string_pretty(&session_to_json(&s, now))
                    .unwrap_or_else(|_| "null".to_string())
            );
            0
        }
        Ok(None) => {
            println!("null");
            0
        }
        Err(e) => {
            eprintln!("qs-desktop-context: current-session query failed: {e}");
            1
        }
    }
}

fn run_sessions(
    db: &PathBuf,
    limit: i64,
    from: Option<i64>,
    to: Option<i64>,
    project: Option<String>,
) -> i32 {
    if limit < 1 || limit > MAX_QUERY_LIMIT {
        eprintln!("qs-desktop-context: sessions query failed: invalid argument: limit must be 1..={MAX_QUERY_LIMIT}, got {limit}");
        return 1;
    }
    if let (Some(f), Some(t)) = (from, to) {
        if f < 0 || t < 0 || f > t {
            eprintln!("qs-desktop-context: sessions query failed: invalid argument: invalid range {f}..{t} (start inclusive, end exclusive, >= 0)");
            return 1;
        }
    }
    let norm_project: Option<String> = match project {
        Some(p) => match normalize_project_id(&p) {
            Ok(n) => Some(n),
            Err(e) => {
                eprintln!("qs-desktop-context: sessions query failed: {e}");
                return 1;
            }
        },
        None => None,
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
            // Foreign DBs error here via open validation.
            eprintln!("qs-desktop-context: cannot open {}: {e}", db.display());
            return 1;
        }
    };
    let rows = match (norm_project, from, to) {
        (Some(pid), Some(f), Some(t)) => store.sessions_in_range_for_project(&pid, f, t, limit),
        (Some(pid), _, _) => {
            if from.is_some() || to.is_some() {
                eprintln!(
                    "qs-desktop-context: sessions query failed: --from/--to must be given together"
                );
                return 1;
            }
            store.recent_sessions_for_project(&pid, limit)
        }
        (None, Some(f), Some(t)) => store.sessions_in_range(f, t, limit),
        (None, _, _) => store.recent_sessions(limit),
    };
    match rows {
        Ok(rows) => {
            let now = now_ms();
            let values: Vec<serde_json::Value> =
                rows.iter().map(|r| session_to_json(r, now)).collect();
            println!(
                "{}",
                serde_json::to_string_pretty(&values).unwrap_or_else(|_| "[]".to_string())
            );
            0
        }
        Err(e) => {
            eprintln!("qs-desktop-context: sessions query failed: {e}");
            1
        }
    }
}

fn run_last_session(db: &PathBuf, project: &str) -> i32 {
    let pid = match normalize_project_id(project) {
        Ok(n) => n,
        Err(e) => {
            eprintln!("qs-desktop-context: last-session query failed: {e}");
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
    match store.last_session_for_project(&pid) {
        Ok(Some(s)) => {
            let now = now_ms();
            println!(
                "{}",
                serde_json::to_string_pretty(&session_to_json(&s, now))
                    .unwrap_or_else(|_| "null".to_string())
            );
            0
        }
        Ok(None) => {
            println!("null");
            0
        }
        Err(e) => {
            eprintln!("qs-desktop-context: last-session query failed: {e}");
            1
        }
    }
}

fn run_session_resources(db: &PathBuf, session: &str, limit: i64) -> i32 {
    if limit < 1 || limit > MAX_QUERY_LIMIT {
        eprintln!("qs-desktop-context: session-resources query failed: invalid argument: limit must be 1..={MAX_QUERY_LIMIT}, got {limit}");
        return 1;
    }
    if qs_agent_orchestrator::normalize_session_id(session).is_err() {
        eprintln!("qs-desktop-context: session-resources query failed: invalid argument: session id must be 32 hex chars (128-bit)");
        return 1;
    }
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
    match store.session_resources(session, limit) {
        Ok(items) => {
            let values: Vec<serde_json::Value> =
                items.iter().map(session_resource_to_json).collect();
            println!(
                "{}",
                serde_json::to_string_pretty(&values).unwrap_or_else(|_| "[]".to_string())
            );
            0
        }
        Err(e) => {
            eprintln!("qs-desktop-context: session-resources query failed: {e}");
            1
        }
    }
}

fn run_session_events(db: &PathBuf, session: &str, limit: i64) -> i32 {
    if limit < 1 || limit > MAX_QUERY_LIMIT {
        eprintln!("qs-desktop-context: session-events query failed: invalid argument: limit must be 1..={MAX_QUERY_LIMIT}, got {limit}");
        return 1;
    }
    if qs_agent_orchestrator::normalize_session_id(session).is_err() {
        eprintln!("qs-desktop-context: session-events query failed: invalid argument: session id must be 32 hex chars (128-bit)");
        return 1;
    }
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
    match store.session_events(session, limit) {
        Ok(rows) => {
            let values: Vec<serde_json::Value> = rows.iter().map(row_to_json).collect();
            println!(
                "{}",
                serde_json::to_string_pretty(&values).unwrap_or_else(|_| "[]".to_string())
            );
            0
        }
        Err(e) => {
            eprintln!("qs-desktop-context: session-events query failed: {e}");
            1
        }
    }
}

/// Durable local device identity for this database (`device_info` singleton).
///
/// Strictly read-only like the other query modes: a missing DB reads as
/// `null` (exit 0, collector never ran) instead of creating anything;
/// validation of the singleton shape happens in the store.
fn run_device_id(db: &PathBuf) -> i32 {
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
    match store.device_id() {
        Ok(dev) => {
            println!(
                "{}",
                serde_json::to_string_pretty(&dev).unwrap_or_else(|_| "null".to_string())
            );
            0
        }
        Err(e) => {
            eprintln!("qs-desktop-context: device-id query failed: {e}");
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

fn run_collect(db: &PathBuf, gap_ms: Option<i64>, interruption_ms: Option<i64>) -> i32 {
    // Precedence CLI > env > defaults; validated before ANY filesystem or
    // database mutation so bad thresholds fail fast (exit 1 with a clear
    // message, nothing created).
    let cfg = match qs_agent_orchestrator::SessionConfig::resolve(gap_ms, interruption_ms) {
        Ok(c) => c,
        Err(e) => {
            eprintln!(
                "qs-desktop-context: invalid session config: {e}\n{}",
                usage()
            );
            return 1;
        }
    };
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
    let store = match ActivityStore::open_with_session_config_and_lock(db, cfg, &_lock) {
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
        Mode::Collect {
            gap_ms,
            interruption_ms,
        } => run_collect(
            db.as_ref().expect("collect needs db"),
            gap_ms,
            interruption_ms,
        ),
        Mode::History {
            limit,
            from,
            to,
            project,
        } => run_history(
            db.as_ref().expect("history needs db"),
            limit,
            from,
            to,
            project,
        ),
        Mode::Current => run_current(),
        Mode::CurrentProject => run_current_project(),
        Mode::LastActivity { project } => {
            run_last_activity(db.as_ref().expect("last-activity needs db"), &project)
        }
        Mode::Resources { project, limit } => {
            run_resources(db.as_ref().expect("resources needs db"), &project, limit)
        }
        Mode::CurrentSession => run_current_session(db.as_ref().expect("current-session needs db")),
        Mode::Sessions {
            limit,
            from,
            to,
            project,
        } => run_sessions(
            db.as_ref().expect("sessions needs db"),
            limit,
            from,
            to,
            project,
        ),
        Mode::LastSession { project } => {
            run_last_session(db.as_ref().expect("last-session needs db"), &project)
        }
        Mode::SessionResources { session, limit } => run_session_resources(
            db.as_ref().expect("session-resources needs db"),
            &session,
            limit,
        ),
        Mode::SessionEvents { session, limit } => run_session_events(
            db.as_ref().expect("session-events needs db"),
            &session,
            limit,
        ),
        Mode::Search {
            limit,
            from,
            to,
            project,
            application,
            resource,
            device,
            query,
        } => run_search(
            db.as_ref().expect("search needs db"),
            limit,
            from,
            to,
            project,
            application,
            resource,
            device,
            query,
        ),
        Mode::SessionDetail {
            session,
            resource_limit,
            include_events,
            event_limit,
        } => run_session_detail(
            db.as_ref().expect("session-detail needs db"),
            &session,
            resource_limit,
            include_events,
            event_limit,
        ),
        Mode::DeviceId => run_device_id(db.as_ref().expect("device-id needs db")),
    };
    std::process::exit(code);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn argv(parts: &[&str]) -> Vec<String> {
        parts.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn device_id_parses_without_flags() {
        let (_db, mode) =
            parse_args(&argv(&["qs-desktop-context", "device-id"])).expect("device-id parses");
        assert!(matches!(mode, Mode::DeviceId));
    }

    #[test]
    fn device_id_rejects_query_flags() {
        // Valid values per flag so each case reaches the device-id gate
        // (rather than failing generic value parsing first).
        let hex32 = "a".repeat(32);
        let cases: Vec<(Vec<String>, &str)> = vec![
            (
                argv(&["qs-desktop-context", "device-id", "--limit", "5"]),
                "not for device-id",
            ),
            (
                argv(&[
                    "qs-desktop-context",
                    "device-id",
                    "--project",
                    "00000000-0000-4000-8000-000000000000",
                ]),
                "not for device-id",
            ),
            // --session is gated before the per-mode check, like elsewhere.
            (
                argv(&["qs-desktop-context", "device-id", "--session", &hex32]),
                "--session is only for",
            ),
            // Text/device filters are gated before the per-mode check,
            // like for every other non-search subcommand.
            (
                argv(&["qs-desktop-context", "device-id", "--application", "kitty"]),
                "are only for search",
            ),
            (
                argv(&["qs-desktop-context", "device-id", "--resource", "x"]),
                "are only for search",
            ),
            (
                argv(&["qs-desktop-context", "device-id", "--device", &hex32]),
                "are only for search",
            ),
            (
                argv(&["qs-desktop-context", "device-id", "--query", "x"]),
                "are only for search",
            ),
            // Detail flags are likewise gated before the per-mode check.
            (
                argv(&["qs-desktop-context", "device-id", "--resource-limit", "5"]),
                "are only for session-detail",
            ),
            (
                argv(&[
                    "qs-desktop-context",
                    "device-id",
                    "--include-events",
                    "--event-limit",
                    "5",
                ]),
                "are only for session-detail",
            ),
        ];
        for (args, want) in &cases {
            let err = parse_args(args).expect_err("device-id must reject query flags");
            assert!(err.contains(want), "unexpected: {err}");
        }
        let err = parse_args(&argv(&[
            "qs-desktop-context",
            "device-id",
            "--include-events",
        ]))
        .expect_err("device-id must reject --include-events");
        assert!(
            err.contains("are only for session-detail"),
            "unexpected: {err}"
        );
    }
}
