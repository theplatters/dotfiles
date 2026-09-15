//! Phase 1 local desktop context/history library.
//!
//! Generic (compositor-agnostic) desktop context types and SQLite history
//! live here. Compositor specifics (Hyprland socket paths, event names,
//! snapshot JSON shapes) live in [`hyprland`]. The collector executable
//! (`src/bin/qs-desktop-context.rs`) wires these together and owns the
//! singleton lock/database path.
//!
//! Design notes for future workers:
//! - `current_context()` is in-memory live state only. It starts as `None`
//!   (no observation yet) and is never backfilled from persisted rows, so a
//!   historical row is never mistaken for live state.
//! - Persistence is append-only with semantic-equality dedup (timestamps
//!   ignored). Reopening the database does not restore `current`.

pub mod app_context;
pub mod collector;
pub mod desktop_context;
pub mod desktop_lock;
pub mod desktop_paths;
pub mod desktop_store;
pub mod hyprland;
pub mod project_context;

pub use app_context::{
    enrich_desktop_context, enrich_with_env, EnrichmentEnv, ResourceProvider, APP_REFRESH_INTERVAL,
};
pub use collector::{
    enrich_workers_live, final_drain, flush_pending, run_collector_forever, run_collector_once,
    run_collector_forever_with_enrich, run_collector_once_with_enrich, AppRefreshConfig,
    CollectorConfig, SessionOutcome, BACKOFF_INITIAL, BACKOFF_MAX, SHUTDOWN_DRAIN_DEADLINE,
};
pub use desktop_context::{
    now_ms, ActivityKind, Availability, DesktopContext, FocusedWindow, ProjectContext,
    ResourceContext, Source, Tracker,
};
pub use project_context::{
    clear_project_cache, enrich_desktop_with_project, resolve_project_for_resource,
    resolve_registry_file, ProjectResolver, PROJECTS_FILE_ENV,
};
pub use desktop_lock::{acquire_lock, DesktopLock, LockError};
pub use desktop_paths::{default_db_path, ensure_parent_dir, lock_path_for};
pub use desktop_store::{
    ActivityRecord, ActivityStore, StoreError, MAX_QUERY_LIMIT, SCHEMA_VERSION,
};
pub use hyprland::{
    discover_sockets, discover_sockets_with, is_meaningful_event, parse_event_line, HyprError,
    HyprEvent, SocketPaths,
};
