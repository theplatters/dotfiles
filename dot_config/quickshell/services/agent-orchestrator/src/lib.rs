//! Phase 5 local desktop context/history/sessions/search library.
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
//! - Phase 4 adds deterministic work sessions (`desktop_session` for
//!   config/types/resource identity plus `desktop_store` for SQL): raw
//!   snapshots stay append-only with stable 128-bit `event_id`/`session_id`
//!   plus a durable `device_id`; each raw append plus its session
//!   assignment/update/resource aggregation is one SQLite transaction so a
//!   failure means the raw event was not persisted. Sessionization is online
//!   with no retroactive reassignment (earlier interruption events stay in
//!   the prior session when a later event proves the run lasted `>= grace`).
//! - Phase 5 adds session-centric searchable history (`desktop_store` FTS5
//!   `activity_fts` plus `SessionSearchQuery`/`SessionSearchResult` and
//!   `SessionDetail`): schema v4, transactional v3→v4 migration with FTS
//!   backfill, atomic FTS maintenance on append, and compact CLI output.

pub mod app_context;
pub mod collector;
pub mod desktop_context;
pub mod desktop_lock;
pub mod desktop_paths;
pub mod desktop_session;
pub mod desktop_store;
pub mod hyprland;
pub mod project_context;
pub mod session_changes;

pub use app_context::{
    enrich_desktop_context, enrich_with_env, EnrichmentEnv, ResourceProvider, APP_REFRESH_INTERVAL,
};
pub use collector::{
    apply_focusless_retention, enrich_workers_live, final_drain, flush_pending,
    run_collector_forever, run_collector_forever_with_enrich, run_collector_once,
    run_collector_once_with_enrich, AppRefreshConfig, CollectorConfig, SessionOutcome,
    BACKOFF_INITIAL, BACKOFF_MAX, SHUTDOWN_DRAIN_DEADLINE,
};
pub use desktop_context::{
    now_ms, retain_focusless, retain_focusless_with_anchor, ActivityKind, Availability,
    DesktopContext, FocusedWindow, ProjectContext, ResourceContext, Source, Tracker,
};
pub use desktop_lock::{acquire_lock, DesktopLock, LockError};
pub use desktop_paths::{default_db_path, ensure_parent_dir, lock_path_for};
pub use desktop_session::{
    decide_session_action, normalize_hex_id, resource_identity, session_id_for, EndedReason,
    OpenSessionView, ResourceIdentity, ResourceKind, SessionAction, SessionConfig, SessionStatus,
    DEFAULT_SESSION_GAP_MS, DEFAULT_SESSION_INTERRUPTION_MS, MAX_SESSION_GAP_MS, SESSION_GAP_ENV,
    SESSION_INTERRUPTION_ENV,
};
pub use desktop_store::{
    normalize_project_id, normalize_session_id, ActivityRecord, ActivityStore, AppendOutcome,
    ProjectResource, SessionDetail, SessionRecord, SessionResourceRecord, SessionSearchQuery,
    SessionSearchResult, StoreError, MAX_QUERY_LIMIT, MAX_SEARCH_RESOURCES_PER_SESSION,
    MAX_SEARCH_TEXT_CHARS, SCHEMA_VERSION,
};
pub use hyprland::{
    discover_sockets, discover_sockets_with, is_meaningful_event, parse_event_line, HyprError,
    HyprEvent, SocketPaths,
};
pub use project_context::{
    clear_project_cache, enrich_desktop_with_project, folder_for_project_id,
    resolve_project_for_resource, resolve_registry_file, ProjectResolver, PROJECTS_FILE_ENV,
};
