#!/usr/bin/env python3
"""Read-only desktop-project integration over the existing project registry.

Resolves the *current* desktop project reported by the fresh compositor
query (``qs-desktop-context current``) to the canonical registry
(``scripts/projects.py list_projects``) by stable ``id``, then serves
read-only Logseq and activity views. No inference, no writes, no fallback
to a selected project.

Registry source of truth (never re-parsed here):

- ``projects.list_projects(registry_file=None)`` returning
  ``{projects:[{id,name,logseq_path,local_folder,github_url,path,page}],
  revision,file}``. ``--projects-file`` / ``QUICKSHELL_PROJECTS_FILE``
  precedence is preserved by delegating to that API.

Logseq source of truth (no duplicate page/task parser):

- ``project_planner.read_page(graph, path)`` for page content and its
  ``todos`` list. ``--graph`` / ``LOGSEQ_GRAPH`` / ``settings.json``
  precedence is preserved via ``logseq_common.resolve_graph``. Graph
  resolution happens ONLY when a registry entry supplies a non-empty
  ``logseq_path``; unknown / stale / name-only projects return explicit
  empty results with no graph requirement.

Desktop CLI contract (implemented by the Rust ``qs-desktop-context``
binary; mocked in tests while it builds):

- ``qs-desktop-context [--db PATH] current`` -> full ``DesktopContext``
  dict with ``project:{id,name,matched_by}|null``. Fresh compositor query,
  never a persisted last row.
- ``qs-desktop-context [--db PATH] current-project`` -> ``project|null``.
  This backend calls ``current`` once per request and derives the project
  locally so the full context stays available for debugging.
- ``qs-desktop-context [--db PATH] history --project UUID [--limit N]``
  -> ``[{id,observed_at_ms,kind,source,snapshot}, ...]``.
- ``qs-desktop-context [--db PATH] last-activity --project UUID``
  -> row ``{id,observed_at_ms,kind,source,snapshot}|null``.
- ``qs-desktop-context [--db PATH] resources --project UUID [--limit N]``
  -> ``[{resource:{adapter,file,cwd,git_root,git_branch,git_remote,url,
  page,title},observed_at_ms,activity_id}, ...]``.
- ``qs-desktop-context [--db PATH] current-session``
  -> deterministic work ``session|null`` from the persisted DB
  (``session_id``, ``project:{id,name}|null``, ``start_ms``/``end_ms``,
  ``status``/``effective_status``, ``event_count``, ``applications``, ...).
  Freshness is query-time ``now`` computed by Rust; no compositor call.
- ``qs-desktop-context [--db PATH] sessions [--project UUID] [--limit N]
  [--from START_MS --to END_MS]`` -> ``[session, ...]`` (same shape).
  No-project lists all sessions; explicit project scopes; ``--from/--to``
  must be paired ``0..=i64::MAX`` integers with ``from <= to``.
- ``qs-desktop-context [--db PATH] last-session --project UUID``
  -> ``session|null`` for that project.
- ``qs-desktop-context [--db PATH] session-resources --session 32HEX
  [--limit N]`` -> per-session resource rollups.
- ``qs-desktop-context [--db PATH] session-events --session 32HEX
  [--limit N]`` -> activity rows for that session (history shape).
- ``qs-desktop-context [--db PATH] search [--project UUID]
  [--application TEXT] [--resource TEXT] [--device 32HEX] [--query TEXT]
  [--from START_MS --to END_MS] [--limit N]``
  -> ``{query:{project,application,resource,device,query,from_ms,to_ms,
  range_semantics,limit},sessions:[compact session summaries with
  resources],count}``. Empty filters allowed (recent sessions, newest-first);
  ``--from/--to`` must pair (``0..=i64::MAX``, ``from <= to``,
  start-inclusive/end-exclusive, UTC epoch-ms); texts are trimmed
  ``1..256`` chars; device is 32-hex (canonical lowercase); limit
  ``1..1000`` (default 20). Compact summaries carry ``matched_at_ms``
  (newest matching observation, ``None`` for project/device-only or
  no-filter searches) plus resources, and no snapshots/raw events.
- ``qs-desktop-context [--db PATH] session-detail --session 32HEX
  [--resource-limit N] [--include-events [--event-limit N]]``
  -> ``{session:object|null,resources:[...],events_included:bool,
  events?:[...]}``. Events are only included when ``--include-events`` is
  given; ``--event-limit`` requires ``--include-events``. Limits
  ``1..1000`` (defaults 20).

Deterministic work sessions vs Pi agent sessions (do not confuse):

- "Work sessions" here are deterministic, DB-derived activity clusters
  (gap/interruption thresholds, ``session_id`` = 32 lowercase hex) served
  read-only by the commands below. They describe what the desktop did.
- "Pi agent sessions" (``SessionManager``/``session_before_switch``/
  ``desktop-sessions`` command in the Pi extension) are conversational
  agent transcripts managed by Pi itself. The new work-session tools never
  list, switch, resume, or mutate Pi sessions.

Binary resolution (no automatic build/start):

- explicit ``--desktop-bin`` > ``QS_DESKTOP_CONTEXT_BIN`` env > default
  ``<repo>/services/agent-orchestrator/target/release/qs-desktop-context``.
  A missing binary is a clear error naming the resolved path and the manual
  cargo build command; nothing is built or started automatically.

DB override:

- explicit ``--db`` > ``QS_DESKTOP_DB`` (fallback ``QS_DESKTOP_CONTEXT_DB``)
  > Rust default (the binary decides). Passed as ``--db PATH`` before the
  subcommand.

Request semantics:

- ``current`` is captured ONCE per request. The reported ``project.id`` is
  looked up in the fresh registry by stable id; the CURRENT registry
  ``name``/``logseq_path`` mapping wins over any historical name.
- Stale (reported id absent from the registry) => unknown: no Logseq data,
  no history query for the default path, explicit ``stale-removed`` status.
  No project is ever mapped to a "selected project" fallback; unassociated
  stays unassociated.
- Identity and Logseq linkage are separate. Any present registry entry
  (``associated`` or ``name-only-no-linkage``) is a valid identity for
  history/last-activity/resources. Only todos/logseq-context additionally
  require a non-empty ``logseq_path``.
- Name-only registry entries (``logseq_path == ""``) return explicit
  ``has_logseq_linkage:false`` with reason ``name-only-no-linkage`` rather
  than guessing a name/path for Logseq views. No graph is required, but
  default history queries still run.
- Explicit ``--project UUID`` is allowed for historical queries. Unknown or
  deleted UUIDs still query history/resources/last-activity (history never
  requires a present registry entry). For Logseq views an unknown explicit
  UUID returns explicit no-linkage with no graph requirement.
- ``unknown`` / ``null`` / empty results never require a graph.

Backend commands (all read-only, JSON to stdout):

- ``current-project``: ``{project,registry,logseq_path,has_logseq_linkage,
  status,reason,context}``.
- ``todos [--project UUID]``: ``{project,registry,requested_project_id,
  logseq_path,has_logseq_linkage,status,reason,page,path,graphName,
  revision,todos}``. ``todos`` comes verbatim from ``read_page``.
- ``logseq-context [--project UUID]``: same identity plus
  ``{page,path,graphName,revision,content,todos}``.
- ``recent-activity [--project UUID] [--limit N]``: ``{project,registry,
  requested_project_id,history,count,reason}``. Defaults to current unless
  ``--project`` is given. No current project => empty history, no DB query.
- ``last-activity [--project UUID]``: ``{project,registry,
  requested_project_id,activity,reason}``. Same defaulting rule.
- ``resources [--project UUID] [--limit N]``: ``{project,registry,
  requested_project_id,resources,count,reason}``. Same defaulting rule.
- ``current-session``: ``{session,reason}``. Direct persisted-DB query;
  no compositor/current-project call and no registry requirement.
- ``device-id``: ``{device_id,reason}``. Durable local device identity
  (``device_info`` singleton, canonical lowercase 32-hex) or null when
  the database is missing. Direct DB query; no compositor/registry call.
- ``sessions [--project UUID] [--limit N] [--from START_MS --to END_MS]``:
  ``{project,registry,requested_project_id,sessions,count,reason}``. No
  project => all sessions directly (no compositor/registry call).
  Explicit project => UUID-validated, registry metadata resolved for
  display (unknown/deleted UUIDs still query). Paired ``0..=i64::MAX``
  range is validated here and passed to Rust; limit stays 1..1000.
- ``last-session --project UUID``: ``{project,registry,
  requested_project_id,session,reason}``. Explicit project required at
  the CLI; unknown/deleted UUIDs still query.
- ``session-resources --session 32HEX [--limit N]``:
  ``{session_id,resources,count}``. No compositor/registry call.
- ``session-events --session 32HEX [--limit N]``:
  ``{session_id,events,count}``. No compositor/registry call.
- ``current-context``: alias of ``current-project`` (same
  ``{project,reported_project,registry,logseq_path,has_logseq_linkage,
  status,reason,context}`` with full context plus project/registry linkage).
- ``search-activity [--project UUID] [--application TEXT]
  [--resource TEXT] [--device 32HEX] [--query TEXT]
  [--from START_MS --to END_MS] [--limit N]``:
  ``{query,sessions,count}`` exactly as Rust ``search`` returns. Direct DB
  query; no compositor/registry call. Empty filters allowed (recent
  sessions). All filters validated before spawn.
- ``get-session --session 32HEX [--resource-limit N] [--include-events
  [--event-limit N]]``: Rust ``session-detail`` object
  (``{session,resources,events_included,events?}``) verbatim. Direct DB
  query; no compositor/registry call. Events excluded by default; only
  ``--include-events`` includes them; ``--event-limit`` requires
  ``--include-events``.
- ``project-activity [--project UUID] [--application TEXT]
  [--resource TEXT] [--device 32HEX] [--query TEXT]
  [--from START_MS --to END_MS] [--limit N]``:
  ``{project,reported_project?,registry,requested_project_id,query,
  sessions,count,reason}``. Same Rust ``search`` but defaults to the fresh
  current stable project identity; explicit ``--project`` skips the
  compositor. Registry is resolved for display only (unknown explicit UUIDs
  still query). All non-project filters/range/limit thread through. The
  default-current two-call path shares one ``REQUEST_TIMEOUT`` deadline.

Subprocess safety: list-form argv (no shell), one request deadline
(``REQUEST_TIMEOUT`` 8s shared by the at-most-two Rust calls per request,
fitting the extension 10s timeout plus kill-escalation grace), incremental
streaming stdout+stderr cap (``MAX_OUTPUT`` enforced while reading via
selectors/non-blocking fds, never buffering unbounded output before the
limit), ``--db``/``--limit`` validated before spawn. The Rust child stays in
our process group (no new session) so an outer group kill reaches it;
direct-CLI SIGTERM forwards to the active child (TERM, grace, KILL, reap).
All user-facing failures are single-line ``error: ...`` on stderr with
exit 1 (no traceback).
"""

from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import NoReturn

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    import projects as _projects
except ImportError:  # pragma: no cover - tests always have scripts on path
    _projects = None  # type: ignore


class DesktopError(ValueError):
    pass


def _error(message: str) -> NoReturn:
    raise DesktopError(message)


DEFAULT_BIN = (
    Path(__file__).resolve().parent.parent
    / "services"
    / "agent-orchestrator"
    / "target"
    / "release"
    / "qs-desktop-context"
)
BIN_ENV = "QS_DESKTOP_CONTEXT_BIN"
DB_ENV_PRIMARY = "QS_DESKTOP_DB"
DB_ENV_FALLBACK = "QS_DESKTOP_CONTEXT_DB"

CLI_TIMEOUT = 8.0
# Overall Python request budget (fits inside the extension 10s timeout plus
# bounded kill-escalation grace). Two-call requests (current + history)
# share this budget via per-call remaining-time computation.
REQUEST_TIMEOUT = 8.0
MAX_OUTPUT = 2 * 1024 * 1024
PATH_LIMIT = 4096
MAX_LIMIT = 1000
DEFAULT_LIMIT = 20
DEFAULT_RESOURCE_LIMIT = 20
DEFAULT_EVENT_LIMIT = 20
MAX_SEARCH_TEXT_CHARS = 256
# Grace between TERM and KILL when reaping a runaway Rust child.
_KILL_GRACE = 0.5

_MANUAL_BUILD_HINT = (
    "cargo build --locked --release "
    "--manifest-path services/agent-orchestrator/Cargo.toml"
)


# ---------------------------------------------------------------------------
# Path / arg resolution
# ---------------------------------------------------------------------------

def resolve_desktop_bin(explicit=None) -> Path:
    """Resolve the helper binary: --desktop-bin > env > repo default."""
    raw = None
    if explicit is not None and str(explicit).strip():
        raw = str(explicit).strip()
    elif isinstance(os.environ.get(BIN_ENV, ""), str) and os.environ.get(
        BIN_ENV, ""
    ).strip():
        raw = os.environ.get(BIN_ENV, "").strip()  # type: ignore[union-attr]
    else:
        return DEFAULT_BIN
    assert raw is not None
    if len(raw) > PATH_LIMIT or "\x00" in raw:
        _error("desktop helper path is too long or contains NUL")
    expanded = os.path.expanduser(raw)
    candidate = Path(expanded)
    if not candidate.is_absolute():
        try:
            lexical = os.path.abspath(os.path.join(os.getcwd(), expanded))
        except OSError as exc:
            raise DesktopError("desktop helper path is not accessible") from exc
        candidate = Path(lexical)
    return candidate


def resolve_db_path(explicit=None) -> str | None:
    """Resolve the activity DB: --db > QS_DESKTOP_DB > QS_DESKTOP_CONTEXT_DB."""
    raw = None
    if explicit is not None and str(explicit).strip():
        raw = str(explicit).strip()
    else:
        for key in (DB_ENV_PRIMARY, DB_ENV_FALLBACK):
            value = os.environ.get(key, "")
            if isinstance(value, str) and value.strip():
                raw = value.strip()
                break
    if raw is None:
        return None
    if len(raw) > PATH_LIMIT or "\x00" in raw:
        _error("desktop db path is too long or contains NUL")
    return raw


def _validate_project_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("project must be a UUID string")
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError as exc:
        raise DesktopError("project must be a UUID string") from exc


def _validate_limit(value: object, default: int = DEFAULT_LIMIT) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        _error("limit must be an integer 1..1000")
    if isinstance(value, int):
        limit = value
    elif isinstance(value, str) and value.strip():
        try:
            limit = int(value.strip(), 10)
        except ValueError as exc:
            raise DesktopError("limit must be an integer 1..1000") from exc
    else:
        _error("limit must be an integer 1..1000")
        raise AssertionError("unreachable")
    if limit < 1 or limit > MAX_LIMIT:
        _error(f"limit must be 1..{MAX_LIMIT}")
    return limit


def _validate_session_id(value: object) -> str:
    """Validate a 128-bit session identity; return canonical lowercase."""
    if not isinstance(value, str) or not value.strip():
        _error("session must be 32 hex chars (128-bit)")
    text = value.strip()
    if len(text) != 32 or any(
        ch not in "0123456789abcdefABCDEF" for ch in text
    ):
        _error("session must be 32 hex chars (128-bit)")
    return text.lower()


def _validate_device_id(value: object) -> str:
    """Validate a 128-bit device identity; return canonical lowercase."""
    if not isinstance(value, str) or not value.strip():
        _error("device must be 32 hex chars (128-bit)")
    text = value.strip()
    if len(text) != 32 or any(
        ch not in "0123456789abcdefABCDEF" for ch in text
    ):
        _error("device must be 32 hex chars (128-bit)")
    return text.lower()


def _validate_search_text(field: str, value: object) -> str:
    """Validate bounded nonempty search text; return the trimmed form.

    Mirrors Rust ``MAX_SEARCH_TEXT_CHARS`` (256 chars): trimmed,
    ``1..=256`` chars, no NUL. ``field`` is one of
    ``application``/``resource``/``query`` so errors name the flag.
    """
    if not isinstance(value, str) or not value.strip():
        _error(f"--{field} must be nonempty (1..{MAX_SEARCH_TEXT_CHARS} chars)")
    trimmed = value.strip()
    if "\x00" in trimmed:
        _error(f"--{field} must not contain NUL")
    if len(trimmed) > PATH_LIMIT:
        _error(f"--{field} is too long")
    # Char count (code points) consistently with Rust ``chars().count()``.
    if len(trimmed) < 1 or len(trimmed) > MAX_SEARCH_TEXT_CHARS:
        _error(f"--{field} must be 1..{MAX_SEARCH_TEXT_CHARS} chars")
    return trimmed


def _optional_search_text(field: str, value: object) -> str | None:
    """Return None when absent, else the validated trimmed text.

    Absent means ``None`` only; an explicitly supplied empty/whitespace
    string is an error (Rust rejects it as nonempty-required).
    """
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        # Explicit empty flag value: fail closed like Rust instead of
        # silently dropping the filter.
        _error(f"--{field} must be nonempty (1..{MAX_SEARCH_TEXT_CHARS} chars)")
    return _validate_search_text(field, value)


def _optional_project_filter(value: object) -> str | None:
    """Return None when absent (None or blank), else canonical UUID."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return _validate_project_id(value)


def _optional_device_filter(value: object) -> str | None:
    """Return None when absent (None only), else canonical 32-hex."""
    if value is None:
        return None
    return _validate_device_id(value)


def _validate_resource_limit(value: object) -> int:
    """Validate ``--resource-limit`` (1..1000, default 20)."""
    try:
        return _validate_limit(value, DEFAULT_RESOURCE_LIMIT)
    except DesktopError as exc:
        raise DesktopError(str(exc).replace("limit must be", "resource-limit must be")) from exc


def _validate_event_limit(value: object) -> int:
    """Validate ``--event-limit`` (1..1000, default 20)."""
    try:
        return _validate_limit(value, DEFAULT_EVENT_LIMIT)
    except DesktopError as exc:
        raise DesktopError(str(exc).replace("limit must be", "event-limit must be")) from exc


def _parse_include_events(value: object) -> bool:
    """Parse the ``--include-events`` flag (boolean, default False)."""
    if value is None or value is False:
        return False
    if value is True:
        return True
    _error("--include-events must be a boolean")
    raise AssertionError("unreachable")


# Max signed 64-bit value (Rust ``i64::MAX``): backend timestamps are
# representable only in ``0..=I64_MAX``. Python ints are unbounded, so the
# bound is enforced here, fail closed, before spawn.
I64_MAX = 9223372036854775807


def _parse_session_ms(value: object, what: str) -> int:
    if isinstance(value, bool):
        _error(f"{what} must be 0..{I64_MAX} (UTC epoch-ms)")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = int(value.strip(), 10)
        except ValueError as exc:
            raise DesktopError(
                f"{what} must be 0..{I64_MAX} (UTC epoch-ms)"
            ) from exc
    else:
        raise DesktopError(f"{what} must be 0..{I64_MAX} (UTC epoch-ms)")
    if parsed < 0 or parsed > I64_MAX:
        _error(f"{what} must be 0..{I64_MAX} (UTC epoch-ms)")
    return parsed


def _validate_session_range(
    from_ms: object, to_ms: object
) -> tuple[int | None, int | None]:
    """Validate the paired --from/--to window for ``sessions``.

    Only omitted (``None``) timestamps mean absent: an explicit
    empty/whitespace value rejects, fail closed, and never becomes an
    unrestricted search. Both absent => (None, None) (no range filter).
    Exactly one present is an error (Rust requires the pair together).
    Present values must be ``0..=i64::MAX`` with ``from <= to``.
    """
    from_present = from_ms is not None
    to_present = to_ms is not None
    if not from_present and not to_present:
        return None, None
    if from_present != to_present:
        _error("--from and --to must be given together")
    parsed_from = _parse_session_ms(from_ms, "--from")
    parsed_to = _parse_session_ms(to_ms, "--to")
    if parsed_from > parsed_to:
        _error(
            f"invalid range {parsed_from}..{parsed_to} "
            "(start inclusive, end exclusive, >= 0)"
        )
    return parsed_from, parsed_to


# ---------------------------------------------------------------------------
# Bounded subprocess (no shell, no auto build/start)
# ---------------------------------------------------------------------------

def _missing_binary_error(binary: Path) -> DesktopError:
    return DesktopError(
        f"desktop context helper not found at {binary}; "
        f"build it manually ({_MANUAL_BUILD_HINT}); "
        "no automatic build/start"
    )


# Active Rust children for direct-CLI SIGTERM forwarding. The extension
# kills the whole process group (Python + Rust share one group: Python never
# uses start_new_session for Rust), but a direct `kill <python-pid>` must
# still reap the Rust child instead of orphaning it.
_ACTIVE: set = set()
_ACTIVE_LOCK = threading.Lock()
_ORIG_TERM = None


def _register_child(proc) -> None:
    with _ACTIVE_LOCK:
        global _ORIG_TERM
        if _ORIG_TERM is None and threading.current_thread() is threading.main_thread():
            try:
                _ORIG_TERM = signal.getsignal(signal.SIGTERM)
                signal.signal(signal.SIGTERM, _term_handler)
            except (OSError, ValueError, RuntimeError):
                _ORIG_TERM = None
        _ACTIVE.add(proc)


def _unregister_child(proc) -> None:
    global _ORIG_TERM
    with _ACTIVE_LOCK:
        _ACTIVE.discard(proc)
        if not _ACTIVE and _ORIG_TERM is not None and threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGTERM, _ORIG_TERM)
            except (OSError, ValueError, RuntimeError):
                pass
            _ORIG_TERM = None


def _term_handler(signum, frame) -> None:
    global _ORIG_TERM
    with _ACTIVE_LOCK:
        victims = list(_ACTIVE)
        orig = _ORIG_TERM
    for proc in victims:
        try:
            proc.terminate()
        except OSError:
            pass
    # Bounded grace, then escalate; always reap so no zombies remain.
    deadline = time.monotonic() + _KILL_GRACE
    for proc in victims:
        try:
            remaining = deadline - time.monotonic()
            proc.wait(timeout=max(0.0, remaining))
        except Exception:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=_KILL_GRACE)
            except Exception:
                pass
    if callable(orig) and orig not in (signal.SIG_DFL, signal.SIG_IGN):
        try:
            orig(signum, frame)
            return
        except Exception:
            pass
    # Preserve default TERM semantics when no Python handler was installed.
    try:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
    except (OSError, ValueError):
        pass
    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except OSError:
        pass
    raise SystemExit(143)


def _kill_and_reap(proc) -> None:
    """TERM, bounded grace, KILL, reap. Never raises; never leaves zombies."""
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=_KILL_GRACE)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=_KILL_GRACE)
    except Exception:
        pass


def _child_env_for_registry(registry_file=None) -> dict | None:
    """Child-only registry override for the Rust detector subprocess.

    Returns ``{QUICKSHELL_PROJECTS_FILE: <resolved>}`` when an explicit
    ``--projects-file`` override was supplied, else ``None`` (inherit the
    parent environment unchanged). Path semantics reuse
    ``projects.resolve_registry_file`` so detection and lookup share one
    stable contract. Never mutates ``os.environ``.
    """
    if registry_file is None:
        return None
    if isinstance(registry_file, Path):
        text = str(registry_file)
    else:
        text = str(registry_file)
    if not text.strip():
        return None
    if _projects is None:
        _error("project registry helper is unavailable")
    assert _projects is not None
    try:
        resolved = _projects.resolve_registry_file(registry_file)
    except Exception as exc:
        raise DesktopError(f"project registry file is unusable: {exc}") from exc
    key = getattr(_projects, "ENV_VAR", "QUICKSHELL_PROJECTS_FILE")
    return {key: str(resolved)}


def _run_desktop_cli(
    binary: Path, db: str | None, args: list[str], timeout: float | None = None,
    extra_env: dict | None = None,
) -> bytes:
    """Run ``qs-desktop-context [--db DB] <args>`` bounded; return stdout bytes.

    Incremental streaming reads (selectors, non-blocking fds) enforce one
    request deadline and a combined stdout+stderr cap *while* reading, so a
    huge/slow child can never fill memory before the limit is noticed. No
    shell; the Rust child stays in our process group (no new session) so an
    outer group kill reaches it. Overflow/timeout kills and reaps the child
    (TERM, grace, KILL, wait).
    """
    if len(str(binary)) > PATH_LIMIT or "\x00" in str(binary):
        _error("desktop helper path is unsafe")
    if db is not None and (len(db) > PATH_LIMIT or "\x00" in db):
        _error("desktop db path is unsafe")
    for item in args:
        if not isinstance(item, str) or len(item) > PATH_LIMIT or "\x00" in item:
            _error("desktop helper argument is unsafe")
    argv = [str(binary)]
    if db:
        argv += ["--db", db]
    argv += args
    budget = REQUEST_TIMEOUT if timeout is None else timeout
    try:
        budget = float(budget)
    except (TypeError, ValueError) as exc:
        raise DesktopError("desktop helper timeout is invalid") from exc
    if not (budget > 0 and budget <= 60):
        _error("desktop helper timeout is out of range")
    deadline = time.monotonic() + budget
    if extra_env:
        for key, value in extra_env.items():
            if not isinstance(key, str) or not isinstance(value, str):
                _error("desktop helper environment override is invalid")
            if len(key) > 4096 or len(value) > PATH_LIMIT or "\x00" in key or "\x00" in value:
                _error("desktop helper environment override is unsafe")
        child_env: dict | None = dict(os.environ)
        child_env.update(extra_env)
    else:
        child_env = None
    try:
        proc = subprocess.Popen(
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=False,
            env=child_env,
        )
    except FileNotFoundError as exc:
        raise _missing_binary_error(binary) from exc
    except OSError as exc:
        raise DesktopError(f"desktop context helper failed: {exc}") from exc
    assert proc.stdout is not None and proc.stderr is not None
    _register_child(proc)
    try:
        for stream in (proc.stdout, proc.stderr):
            try:
                os.set_blocking(stream.fileno(), False)
            except (OSError, ValueError):
                pass
        sel = selectors.DefaultSelector()
        try:
            sel.register(proc.stdout, selectors.EVENT_READ, "out")
            sel.register(proc.stderr, selectors.EVENT_READ, "err")
        except (OSError, ValueError, KeyError) as exc:
            _kill_and_reap(proc)
            raise DesktopError(f"desktop context helper failed: {exc}") from exc
        out_parts: list[bytes] = []
        err_parts: list[bytes] = []
        total = 0
        overflow = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _kill_and_reap(proc)
                    raise DesktopError("desktop context helper timed out")
                if proc.poll() is not None:
                    # Drained exit: pull whatever remains without blocking.
                    for stream, store in ((proc.stdout, out_parts), (proc.stderr, err_parts)):
                        while True:
                            try:
                                chunk = stream.read(65536)
                            except (BlockingIOError, OSError):
                                break
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > MAX_OUTPUT:
                                overflow = True
                                break
                            store.append(chunk)
                            if total > MAX_OUTPUT:
                                overflow = True
                                break
                        if overflow:
                            break
                    break
                try:
                    events = sel.select(timeout=min(0.05, max(0.0, remaining)))
                except OSError as exc:
                    _kill_and_reap(proc)
                    raise DesktopError(f"desktop context helper failed: {exc}") from exc
                if not events:
                    continue
                for key, _mask in events:
                    try:
                        chunk = key.fileobj.read(65536)  # type: ignore[union-attr]
                    except (BlockingIOError, OSError):
                        continue
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_OUTPUT:
                        overflow = True
                        break
                    if key.data == "out":
                        out_parts.append(chunk)
                    else:
                        err_parts.append(chunk)
                if overflow:
                    break
                if total > MAX_OUTPUT:
                    overflow = True
                    break
        finally:
            try:
                sel.close()
            except Exception:
                pass
        if overflow:
            _kill_and_reap(proc)
            _error(f"desktop context helper output exceeded {MAX_OUTPUT} bytes")
        # Reap with the remaining budget (bounded); runaway gets killed.
        remaining = deadline - time.monotonic()
        if proc.poll() is None:
            if remaining <= 0:
                _kill_and_reap(proc)
                raise DesktopError("desktop context helper timed out")
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                _kill_and_reap(proc)
                raise DesktopError("desktop context helper timed out") from exc
            except OSError as exc:
                _kill_and_reap(proc)
                raise DesktopError(f"desktop context helper failed: {exc}") from exc
        code = proc.returncode if proc.returncode is not None else 1
        out = b"".join(out_parts)
        err = b"".join(err_parts)
        if code != 0:
            try:
                detail = err.decode("utf-8", errors="replace").strip()
            except Exception:
                detail = ""
            if not detail:
                detail = f"desktop helper exited {code}"
            if len(detail) > 4096:
                detail = detail[:4096]
            _error(detail)
        return out
    finally:
        _unregister_child(proc)
        # Belt-and-braces: never return with a live child; always close
        # pipes so callers never see ResourceWarnings.
        try:
            if proc.poll() is None:
                _kill_and_reap(proc)
        except Exception:
            pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass


def _parse_json_output(raw: bytes, what: str):
    if len(raw) > MAX_OUTPUT:
        _error(f"desktop helper {what} output exceeded {MAX_OUTPUT} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DesktopError(f"desktop helper {what} is not valid UTF-8") from exc
    if not text.strip():
        _error(f"desktop helper {what} returned empty output")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DesktopError(
            f"desktop helper {what} returned invalid JSON"
        ) from exc


def _fetch_current(binary: Path, db: str | None, timeout: float | None = None,
                 registry_file=None) -> dict:
    raw = _run_desktop_cli(
        binary, db, ["current"],
        timeout=REQUEST_TIMEOUT if timeout is None else timeout,
        extra_env=_child_env_for_registry(registry_file),
    )
    value = _parse_json_output(raw, "current")
    if not isinstance(value, dict):
        _error("desktop helper current must be an object")
    return value


def _fetch_history(
    binary: Path, db: str | None, project_id: str, limit: int, timeout: float | None = None
) -> list:
    raw = _run_desktop_cli(
        binary, db, ["history", "--project", project_id, "--limit", str(limit)],
        timeout=REQUEST_TIMEOUT if timeout is None else timeout,
    )
    value = _parse_json_output(raw, "history")
    if not isinstance(value, list):
        _error("desktop helper history must be a list")
    return value


def _fetch_last_activity(
    binary: Path, db: str | None, project_id: str, timeout: float | None = None
):
    raw = _run_desktop_cli(
        binary, db, ["last-activity", "--project", project_id],
        timeout=REQUEST_TIMEOUT if timeout is None else timeout,
    )
    value = _parse_json_output(raw, "last-activity")
    if value is not None and not isinstance(value, dict):
        _error("desktop helper last-activity must be an object or null")
    return value


def _fetch_resources(
    binary: Path, db: str | None, project_id: str, limit: int, timeout: float | None = None
) -> list:
    raw = _run_desktop_cli(
        binary, db, ["resources", "--project", project_id, "--limit", str(limit)],
        timeout=REQUEST_TIMEOUT if timeout is None else timeout,
    )
    value = _parse_json_output(raw, "resources")
    if not isinstance(value, list):
        _error("desktop helper resources must be a list")
    return value


def _fetch_current_session(
    binary: Path, db: str | None, timeout: float | None = None
):
    raw = _run_desktop_cli(
        binary, db, ["current-session"],
        timeout=REQUEST_TIMEOUT if timeout is None else timeout,
    )
    value = _parse_json_output(raw, "current-session")
    if value is not None and not isinstance(value, dict):
        _error("desktop helper current-session must be an object or null")
    return value


def _fetch_sessions(
    binary: Path,
    db: str | None,
    project_id: str | None,
    limit: int,
    from_ms: int | None = None,
    to_ms: int | None = None,
    timeout: float | None = None,
) -> list:
    argv = ["sessions"]
    if project_id is not None:
        argv += ["--project", project_id]
    argv += ["--limit", str(limit)]
    if from_ms is not None and to_ms is not None:
        argv += ["--from", str(from_ms), "--to", str(to_ms)]
    raw = _run_desktop_cli(
        binary, db, argv,
        timeout=REQUEST_TIMEOUT if timeout is None else timeout,
    )
    value = _parse_json_output(raw, "sessions")
    if not isinstance(value, list):
        _error("desktop helper sessions must be a list")
    return value


def _fetch_last_session(
    binary: Path, db: str | None, project_id: str, timeout: float | None = None
):
    raw = _run_desktop_cli(
        binary, db, ["last-session", "--project", project_id],
        timeout=REQUEST_TIMEOUT if timeout is None else timeout,
    )
    value = _parse_json_output(raw, "last-session")
    if value is not None and not isinstance(value, dict):
        _error("desktop helper last-session must be an object or null")
    return value


def _fetch_session_resources(
    binary: Path, db: str | None, session_id: str, limit: int, timeout: float | None = None
) -> list:
    raw = _run_desktop_cli(
        binary, db,
        ["session-resources", "--session", session_id, "--limit", str(limit)],
        timeout=REQUEST_TIMEOUT if timeout is None else timeout,
    )
    value = _parse_json_output(raw, "session-resources")
    if not isinstance(value, list):
        _error("desktop helper session-resources must be a list")
    return value


def _fetch_device_id(
    binary: Path, db: str | None, timeout: float | None = None
) -> str | None:
    """Return the durable local device id (canonical lowercase) or None.

    ``null`` (missing DB: the collector never ran) reads as ``None`` with
    no creation, mirroring the other read-only query commands. A present
    value is validated as 32-hex and canonicalized, fail closed.
    """
    raw = _run_desktop_cli(
        binary, db, ["device-id"],
        timeout=REQUEST_TIMEOUT if timeout is None else timeout,
    )
    value = _parse_json_output(raw, "device-id")
    if value is None:
        return None
    if not isinstance(value, str):
        _error("desktop helper device-id must be a string or null")
    return _validate_device_id(value)


def _fetch_session_events(
    binary: Path, db: str | None, session_id: str, limit: int, timeout: float | None = None
) -> list:
    raw = _run_desktop_cli(
        binary, db,
        ["session-events", "--session", session_id, "--limit", str(limit)],
        timeout=REQUEST_TIMEOUT if timeout is None else timeout,
    )
    value = _parse_json_output(raw, "session-events")
    if not isinstance(value, list):
        _error("desktop helper session-events must be a list")
    return value


def _fetch_search(
    binary: Path,
    db: str | None,
    project_id: str | None,
    application: str | None,
    resource: str | None,
    device: str | None,
    query: str | None,
    from_ms: int | None,
    to_ms: int | None,
    limit: int,
    timeout: float | None = None,
) -> dict:
    """Run Rust ``search`` with validated filters; return the exact object.

    Separate ``["--query", value]`` argv items stay safe toward Rust: the
    Rust parser consumes the token following a filter flag explicitly as
    its value, so even leading-hyphen text (``--help``, ``-draft``) arrives
    as data. (Pi→Python uses ``--query=<value>`` instead because Python
    argparse would otherwise interpret a leading-hyphen value as a flag.)
    """
    argv = ["search"]
    if project_id is not None:
        argv += ["--project", project_id]
    if application is not None:
        argv += ["--application", application]
    if resource is not None:
        argv += ["--resource", resource]
    if device is not None:
        argv += ["--device", device]
    if query is not None:
        argv += ["--query", query]
    if from_ms is not None and to_ms is not None:
        argv += ["--from", str(from_ms), "--to", str(to_ms)]
    argv += ["--limit", str(limit)]
    raw = _run_desktop_cli(
        binary, db, argv,
        timeout=REQUEST_TIMEOUT if timeout is None else timeout,
    )
    value = _parse_json_output(raw, "search")
    if not isinstance(value, dict):
        _error("desktop helper search must be an object")
    query_obj = value.get("query")
    sessions = value.get("sessions")
    count = value.get("count")
    if not isinstance(query_obj, dict):
        _error("desktop helper search query must be an object")
    if not isinstance(sessions, list):
        _error("desktop helper search sessions must be a list")
    if not isinstance(count, int) or isinstance(count, bool):
        _error("desktop helper search count must be an integer")
    if count != len(sessions):
        _error("desktop helper search count must match sessions length")
    return value


def _fetch_session_detail(
    binary: Path,
    db: str | None,
    session_id: str,
    resource_limit: int,
    include_events: bool,
    event_limit: int | None,
    timeout: float | None = None,
) -> dict:
    """Run Rust ``session-detail``; validate the exact object shape."""
    argv = ["session-detail", "--session", session_id,
            "--resource-limit", str(resource_limit)]
    if include_events:
        argv += ["--include-events"]
        if event_limit is not None:
            argv += ["--event-limit", str(event_limit)]
    raw = _run_desktop_cli(
        binary, db, argv,
        timeout=REQUEST_TIMEOUT if timeout is None else timeout,
    )
    value = _parse_json_output(raw, "session-detail")
    if not isinstance(value, dict):
        _error("desktop helper session-detail must be an object")
    session = value.get("session")
    resources = value.get("resources")
    events_included = value.get("events_included")
    if session is not None and not isinstance(session, dict):
        _error("desktop helper session-detail session must be an object or null")
    if not isinstance(resources, list):
        _error("desktop helper session-detail resources must be a list")
    if not isinstance(events_included, bool):
        _error("desktop helper session-detail events_included must be a boolean")
    if events_included != include_events:
        _error("desktop helper session-detail events_included mismatch")
    if include_events:
        events = value.get("events")
        if not isinstance(events, list):
            _error("desktop helper session-detail events must be a list")
    else:
        if "events" in value:
            _error("desktop helper session-detail must not include events")
    return value


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise DesktopError("desktop context helper timed out")
    return left


# ---------------------------------------------------------------------------
# Registry resolution (stable id lookup; current mapping wins)
# ---------------------------------------------------------------------------

def _load_registry(registry_file=None) -> tuple[dict, dict]:
    if _projects is None:
        _error("project registry helper is unavailable")
    assert _projects is not None
    try:
        response = _projects.list_projects(registry_file)
    except Exception as exc:
        raise DesktopError(f"project registry is unusable: {exc}") from exc
    by_id: dict[str, dict] = {}
    for entry in response.get("projects", []):
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            by_id[entry["id"]] = entry
    return by_id, response


def _extract_reported_project(context: dict):
    project = context.get("project")
    if project is None:
        return None
    if not isinstance(project, dict):
        _error("desktop helper current project must be an object or null")
    raw_id = project.get("id")
    if raw_id is None or (isinstance(raw_id, str) and not raw_id.strip()):
        return None
    try:
        pid = str(uuid.UUID(str(raw_id).strip()))
    except ValueError as exc:
        raise DesktopError("desktop helper project id is not a UUID") from exc
    name = project.get("name", "")
    matched_by = project.get("matched_by", "")
    if not isinstance(name, str):
        name = str(name)
    if not isinstance(matched_by, str):
        matched_by = str(matched_by)
    return {"id": pid, "name": name, "matched_by": matched_by}


def _resolve_current_identity(
    binary: Path, db: str | None, registry_file=None, timeout: float | None = None
) -> tuple[dict, dict | None, dict | None, str, str]:
    """Return (context, reported_project|None, registry_entry|None, status, reason).

    Statuses: ``associated`` (linked), ``name-only-no-linkage``,
    ``unassociated`` (no current project), ``stale-removed`` (id absent).
    Identity (registry entry present) and Logseq linkage are separate:
    history/last-activity/resources need only identity, while todos and
    logseq-context additionally need a non-empty ``logseq_path``.
    """
    # Same registry for detection and lookup: the explicit override (if
    # any) is forwarded child-only to the Rust detector; otherwise the
    # child inherits the parent environment unchanged.
    context = _fetch_current(binary, db, timeout=timeout, registry_file=registry_file)
    reported = _extract_reported_project(context)
    by_id, _response = _load_registry(registry_file)
    if reported is None:
        return context, None, None, "unassociated", "no current desktop project"
    entry = by_id.get(reported["id"])
    if entry is None:
        return (
            context,
            reported,
            None,
            "stale-removed",
            "current project is no longer in the registry",
        )
    if not isinstance(entry.get("logseq_path", ""), str) or not entry.get(
        "logseq_path", ""
    ).strip():
        return (
            context,
            reported,
            entry,
            "name-only-no-linkage",
            "project has no Logseq linkage (logseq_path is empty); "
            "not guessing a name/path",
        )
    return context, reported, entry, "associated", ""


def _resolve_explicit_identity(
    project_id: str, registry_file=None
) -> tuple[dict | None, dict | None, str, str]:
    """Return (display_project, registry_entry|None, status, reason) for --project."""
    canonical = _validate_project_id(project_id)
    by_id, _response = _load_registry(registry_file)
    entry = by_id.get(canonical)
    if entry is None:
        display = {"id": canonical, "name": "", "matched_by": ""}
        return (
            display,
            None,
            "unknown-explicit",
            "project id is unknown or deleted; historical activity still queryable",
        )
    if not entry.get("logseq_path", ""):
        display = {
            "id": canonical,
            "name": entry.get("name", ""),
            "matched_by": "",
        }
        return (
            display,
            entry,
            "name-only-no-linkage",
            "project has no Logseq linkage (logseq_path is empty); "
            "not guessing a name/path",
        )
    display = {
        "id": canonical,
        "name": entry.get("name", ""),
        "matched_by": "",
    }
    return display, entry, "associated", ""


def _display_for_entry(
    reported: dict | None, entry: dict | None
) -> dict | None:
    """Prefer the current registry name; keep reported matched_by for honesty."""
    if entry is not None:
        matched = ""
        if isinstance(reported, dict) and isinstance(
            reported.get("matched_by"), str
        ):
            matched = reported["matched_by"]
        return {
            "id": entry["id"],
            "name": entry.get("name", ""),
            "matched_by": matched,
        }
    return reported


def _default_history_target(
    reported: dict | None, entry: dict | None, status: str
) -> tuple[dict | None, bool]:
    """Split identity from Logseq linkage for history-family defaults.

    Returns (project_out, should_query): any present registry entry
    (``associated`` or ``name-only-no-linkage``) is a valid identity for
    history/last-activity/resources even without a Logseq page. Only
    ``unassociated``/``stale-removed`` (entry None) stay empty without a DB
    query. Todos/logseq-context keep the stricter linkage gate.
    """
    if entry is None:
        return None, False
    return _display_for_entry(reported, entry), True


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------

def current_project(
    registry_file=None, desktop_bin=None, db=None
) -> dict:
    """Return the current desktop project resolved against the registry."""
    binary = resolve_desktop_bin(desktop_bin)
    db_path = resolve_db_path(db)
    context, reported, entry, status, reason = _resolve_current_identity(
        binary, db_path, registry_file
    )
    project = _display_for_entry(reported, entry)
    # Stale stays reported for debugging but counts as unknown for linkage.
    if status == "stale-removed":
        project_out = None
    else:
        project_out = project
    logseq_path = ""
    has_link = False
    if isinstance(entry, dict) and isinstance(entry.get("logseq_path"), str):
        logseq_path = entry["logseq_path"]
        has_link = bool(logseq_path.strip())
    return {
        "project": project_out,
        "reported_project": reported,
        "registry": entry,
        "logseq_path": logseq_path,
        "has_logseq_linkage": has_link,
        "status": status,
        "reason": reason,
        "context": context,
    }


def _logseq_view(
    *,
    graph=None,
    project_id=None,
    registry_file=None,
    desktop_bin=None,
    db=None,
    want: str,
) -> dict:
    """Shared todos / logseq-context implementation via read_page reuse."""
    if want not in ("todos", "logseq-context"):
        _error("internal view must be todos or logseq-context")
    binary = resolve_desktop_bin(desktop_bin)
    db_path = resolve_db_path(db)
    requested: str | None = None
    reported: dict | None = None
    entry: dict | None = None
    status = ""
    reason = ""
    if project_id is not None and str(project_id).strip():
        requested = _validate_project_id(project_id)
        display, entry, status, reason = _resolve_explicit_identity(
            requested, registry_file
        )
        reported = display
        project = display if status == "associated" else None
        # Unknown/name-only explicit: explicit no-linkage, no graph needed.
        if status != "associated" or entry is None:
            logseq_path = entry.get("logseq_path", "") if entry else ""
            base: dict = {
                "project": project,
                "reported_project": reported,
                "registry": entry,
                "requested_project_id": requested,
                "logseq_path": logseq_path if isinstance(logseq_path, str) else "",
                "has_logseq_linkage": False,
                "status": status,
                "reason": reason or "project has no Logseq linkage",
            }
            if want == "todos":
                base.update(
                    {
                        "page": entry.get("page", "") if entry else "",
                        "path": logseq_path if isinstance(logseq_path, str) else "",
                        "graphName": "",
                        "revision": "",
                        "todos": [],
                    }
                )
            else:
                base.update(
                    {
                        "page": entry.get("page", "") if entry else "",
                        "path": logseq_path if isinstance(logseq_path, str) else "",
                        "graphName": "",
                        "revision": "",
                        "content": "",
                        "todos": [],
                    }
                )
            return base
    else:
        context, reported_raw, entry_raw, status, reason = (
            _resolve_current_identity(binary, db_path, registry_file)
        )
        reported = reported_raw
        entry = entry_raw
        project = _display_for_entry(reported, entry)
        if status != "associated" or entry is None:
            logseq_path = ""
            if isinstance(entry, dict):
                raw_path = entry.get("logseq_path", "")
                logseq_path = raw_path if isinstance(raw_path, str) else ""
            out: dict = {
                "project": None if status in ("unassociated", "stale-removed") else project,
                "reported_project": reported,
                "registry": entry,
                "requested_project_id": None,
                "logseq_path": logseq_path,
                "has_logseq_linkage": False,
                "status": status,
                "reason": reason,
            }
            if want == "todos":
                out.update(
                    {
                        "page": "",
                        "path": logseq_path,
                        "graphName": "",
                        "revision": "",
                        "todos": [],
                    }
                )
            else:
                out.update(
                    {
                        "page": "",
                        "path": logseq_path,
                        "graphName": "",
                        "revision": "",
                        "content": "",
                        "todos": [],
                    }
                )
            return out
        requested = None
    # Associated path: registry supplies the page path; never trust history.
    assert entry is not None
    logseq_path = entry.get("logseq_path", "")
    from logseq_common import resolve_graph as _resolve_graph
    import project_planner as _planner

    graph_resolved = _resolve_graph(graph)
    current = _planner.read_page(graph_resolved, logseq_path)
    project = _display_for_entry(reported, entry)
    base = {
        "project": project,
        "reported_project": reported,
        "registry": entry,
        "requested_project_id": requested,
        "logseq_path": logseq_path,
        "has_logseq_linkage": True,
        "status": "associated",
        "reason": "",
        "page": current.get("page", ""),
        "path": current.get("path", ""),
        "graphName": current.get("graphName", ""),
        "revision": current.get("revision", ""),
        "todos": current.get("todos", []),
    }
    if want == "logseq-context":
        base["content"] = current.get("content", "")
    return base


def project_todos(
    graph=None,
    project_id=None,
    registry_file=None,
    desktop_bin=None,
    db=None,
) -> dict:
    """Return TODOs for the current (or explicit) project via read_page."""
    return _logseq_view(
        graph=graph,
        project_id=project_id,
        registry_file=registry_file,
        desktop_bin=desktop_bin,
        db=db,
        want="todos",
    )


def project_logseq_context(
    graph=None,
    project_id=None,
    registry_file=None,
    desktop_bin=None,
    db=None,
) -> dict:
    """Return full Logseq page context for the current (or explicit) project."""
    return _logseq_view(
        graph=graph,
        project_id=project_id,
        registry_file=registry_file,
        desktop_bin=desktop_bin,
        db=db,
        want="logseq-context",
    )


def recent_activity(
    project_id=None,
    limit: object = None,
    registry_file=None,
    desktop_bin=None,
    db=None,
) -> dict:
    """Return bounded history; defaults to current unless --project is given.

    History needs only identity (any present registry entry, including
    name-only projects without a Logseq page). Only unassociated/stale
    (no entry) return empty without a DB query.
    """
    bound = _validate_limit(limit, DEFAULT_LIMIT)
    binary = resolve_desktop_bin(desktop_bin)
    db_path = resolve_db_path(db)
    if project_id is not None and str(project_id).strip():
        canonical = _validate_project_id(project_id)
        display, entry, _status, reason = _resolve_explicit_identity(
            canonical, registry_file
        )
        # Unknown/deleted UUIDs still query history: no registry gate here.
        history = _fetch_history(binary, db_path, canonical, bound)
        return {
            "project": display,
            "registry": entry,
            "requested_project_id": canonical,
            "history": history,
            "count": len(history),
            "reason": reason if entry is None else "",
        }
    deadline = time.monotonic() + REQUEST_TIMEOUT
    context, reported, entry, status, reason = _resolve_current_identity(
        binary, db_path, registry_file, timeout=REQUEST_TIMEOUT
    )
    _ = context
    project_out, should_query = _default_history_target(reported, entry, status)
    if not should_query:
        return {
            "project": project_out,
            "reported_project": reported,
            "registry": entry,
            "requested_project_id": None,
            "history": [],
            "count": 0,
            "reason": reason,
        }
    assert entry is not None
    target = entry["id"]
    history = _fetch_history(binary, db_path, target, bound, timeout=_remaining(deadline))
    return {
        "project": _display_for_entry(reported, entry),
        "reported_project": reported,
        "registry": entry,
        "requested_project_id": None,
        "history": history,
        "count": len(history),
        "reason": "",
    }


def last_activity(
    project_id=None,
    registry_file=None,
    desktop_bin=None,
    db=None,
) -> dict:
    """Return the latest activity row; defaults to current unless --project.

    Needs only identity; name-only projects without a Logseq page still
    query. Only unassociated/stale return empty without a DB query.
    """
    binary = resolve_desktop_bin(desktop_bin)
    db_path = resolve_db_path(db)
    if project_id is not None and str(project_id).strip():
        canonical = _validate_project_id(project_id)
        display, entry, _status, reason = _resolve_explicit_identity(
            canonical, registry_file
        )
        row = _fetch_last_activity(binary, db_path, canonical)
        return {
            "project": display,
            "registry": entry,
            "requested_project_id": canonical,
            "activity": row,
            "reason": reason if entry is None else (""
                      if row is not None else "no activity recorded"),
        }
    deadline = time.monotonic() + REQUEST_TIMEOUT
    context, reported, entry, status, reason = _resolve_current_identity(
        binary, db_path, registry_file, timeout=REQUEST_TIMEOUT
    )
    _ = context
    project_out, should_query = _default_history_target(reported, entry, status)
    if not should_query:
        return {
            "project": project_out,
            "reported_project": reported,
            "registry": entry,
            "requested_project_id": None,
            "activity": None,
            "reason": reason,
        }
    assert entry is not None
    row = _fetch_last_activity(binary, db_path, entry["id"], timeout=_remaining(deadline))
    return {
        "project": _display_for_entry(reported, entry),
        "reported_project": reported,
        "registry": entry,
        "requested_project_id": None,
        "activity": row,
        "reason": "" if row is not None else "no activity recorded",
    }


def project_resources(
    project_id=None,
    limit: object = None,
    registry_file=None,
    desktop_bin=None,
    db=None,
) -> dict:
    """Return bounded resource observations; defaults to current unless --project.

    Needs only identity; name-only projects without a Logseq page still
    query. Only unassociated/stale return empty without a DB query.
    """
    bound = _validate_limit(limit, DEFAULT_LIMIT)
    binary = resolve_desktop_bin(desktop_bin)
    db_path = resolve_db_path(db)
    if project_id is not None and str(project_id).strip():
        canonical = _validate_project_id(project_id)
        display, entry, _status, reason = _resolve_explicit_identity(
            canonical, registry_file
        )
        items = _fetch_resources(binary, db_path, canonical, bound)
        return {
            "project": display,
            "registry": entry,
            "requested_project_id": canonical,
            "resources": items,
            "count": len(items),
            "reason": reason if entry is None else "",
        }
    deadline = time.monotonic() + REQUEST_TIMEOUT
    context, reported, entry, status, reason = _resolve_current_identity(
        binary, db_path, registry_file, timeout=REQUEST_TIMEOUT
    )
    _ = context
    project_out, should_query = _default_history_target(reported, entry, status)
    if not should_query:
        return {
            "project": project_out,
            "reported_project": reported,
            "registry": entry,
            "requested_project_id": None,
            "resources": [],
            "count": 0,
            "reason": reason,
        }
    assert entry is not None
    items = _fetch_resources(binary, db_path, entry["id"], bound, timeout=_remaining(deadline))
    return {
        "project": _display_for_entry(reported, entry),
        "reported_project": reported,
        "registry": entry,
        "requested_project_id": None,
        "resources": items,
        "count": len(items),
        "reason": "",
    }


def current_session(desktop_bin=None, db=None) -> dict:
    """Return the persisted current work session ({session,reason}).

    Direct DB query only: no compositor ``current`` call and no registry
    lookup. ``session`` is the Rust ``current-session`` object or null;
    ``reason`` is empty when a session is present.
    """
    binary = resolve_desktop_bin(desktop_bin)
    db_path = resolve_db_path(db)
    session = _fetch_current_session(binary, db_path)
    return {
        "session": session,
        "reason": "" if session is not None else "no current session recorded",
    }


def work_sessions(
    project_id=None,
    limit: object = None,
    from_ms: object = None,
    to_ms: object = None,
    registry_file=None,
    desktop_bin=None,
    db=None,
) -> dict:
    """Return deterministic work sessions ({project,registry,...,sessions}).

    No-project lists ALL sessions directly (no compositor/registry call).
    Explicit project is UUID-validated; registry metadata is resolved for
    display exactly like the explicit history paths, but unknown/deleted
    UUIDs still query the DB. The paired range is validated here and
    passed to Rust; limit stays bounded 1..1000.
    """
    bound = _validate_limit(limit, DEFAULT_LIMIT)
    range_from, range_to = _validate_session_range(from_ms, to_ms)
    binary = resolve_desktop_bin(desktop_bin)
    db_path = resolve_db_path(db)
    if project_id is not None and str(project_id).strip():
        canonical = _validate_project_id(project_id)
        display, entry, _status, reason = _resolve_explicit_identity(
            canonical, registry_file
        )
        rows = _fetch_sessions(
            binary, db_path, canonical, bound, range_from, range_to
        )
        return {
            "project": display,
            "registry": entry,
            "requested_project_id": canonical,
            "sessions": rows,
            "count": len(rows),
            "reason": reason if entry is None else "",
        }
    rows = _fetch_sessions(binary, db_path, None, bound, range_from, range_to)
    return {
        "project": None,
        "registry": None,
        "requested_project_id": None,
        "sessions": rows,
        "count": len(rows),
        "reason": "",
    }


def last_session(
    project_id=None,
    registry_file=None,
    desktop_bin=None,
    db=None,
) -> dict:
    """Return the latest work session for an explicit project.

    Explicit project UUID is required; unknown/deleted UUIDs still query
    the DB (same rule as explicit history). No compositor call.
    """
    if project_id is None or not str(project_id).strip():
        _error("last-session requires an explicit --project UUID")
    canonical = _validate_project_id(project_id)
    binary = resolve_desktop_bin(desktop_bin)
    db_path = resolve_db_path(db)
    display, entry, _status, reason = _resolve_explicit_identity(
        canonical, registry_file
    )
    row = _fetch_last_session(binary, db_path, canonical)
    return {
        "project": display,
        "registry": entry,
        "requested_project_id": canonical,
        "session": row,
        "reason": reason if entry is None else (""
                  if row is not None else "no session recorded"),
    }


def session_resources(
    session_id=None,
    limit: object = None,
    desktop_bin=None,
    db=None,
) -> dict:
    """Return per-session resource rollups; no compositor/registry call."""
    canonical = _validate_session_id(session_id)
    bound = _validate_limit(limit, DEFAULT_LIMIT)
    binary = resolve_desktop_bin(desktop_bin)
    db_path = resolve_db_path(db)
    items = _fetch_session_resources(binary, db_path, canonical, bound)
    return {
        "session_id": canonical,
        "resources": items,
        "count": len(items),
    }


def device_id(desktop_bin=None, db=None) -> dict:
    """Return the durable local device identity ({device_id,reason}).

    Direct DB query only: no compositor ``current`` call and no registry
    lookup. ``device_id`` is the canonical lowercase 32-hex identity from
    the ``device_info`` singleton, or null when the database is missing
    (the collector never ran). Missing DB reads as empty with exit 0,
    consistent with the other read-only query commands.
    """
    binary = resolve_desktop_bin(desktop_bin)
    db_path = resolve_db_path(db)
    value = _fetch_device_id(binary, db_path)
    return {
        "device_id": value,
        "reason": "" if value is not None else "no desktop database recorded",
    }


def session_events(
    session_id=None,
    limit: object = None,
    desktop_bin=None,
    db=None,
) -> dict:
    """Return activity rows for one session; no compositor/registry call."""
    canonical = _validate_session_id(session_id)
    bound = _validate_limit(limit, DEFAULT_LIMIT)
    binary = resolve_desktop_bin(desktop_bin)
    db_path = resolve_db_path(db)
    rows = _fetch_session_events(binary, db_path, canonical, bound)
    return {
        "session_id": canonical,
        "events": rows,
        "count": len(rows),
    }


def current_context(
    registry_file=None, desktop_bin=None, db=None
) -> dict:
    """Return the current desktop context (alias of ``current_project``).

    Same ``{project,reported_project,registry,logseq_path,
    has_logseq_linkage,status,reason,context}`` response with the full
    compositor ``context`` plus project/registry linkage. Kept as a separate
    command so the coherent history surface (``current-context``,
    ``search-activity``, ``get-session``, ``project-activity``) does not
    reuse the legacy ``current-project`` name in Pi tools.
    """
    return current_project(registry_file, desktop_bin, db)


def search_activity(
    project_id=None,
    application: object = None,
    resource: object = None,
    device: object = None,
    query: object = None,
    from_ms: object = None,
    to_ms: object = None,
    limit: object = None,
    desktop_bin=None,
    db=None,
) -> dict:
    """Return compact session-centric search (Rust ``search`` verbatim).

    Direct DB query; no compositor/registry call. All filters are optional;
    empty filters (all None) list recent sessions newest-first. Project is
    an optional UUID; application/resource/query are trimmed ``1..256``
    chars; device is 32-hex (canonical lowercase); ``--from/--to`` must
    pair (``0..=i64::MAX`` UTC epoch-ms, start-inclusive/end-exclusive,
    ``from <= to``); limit stays ``1..1000`` (default 20). The exact
    ``{query,sessions,count}`` object shape is validated before return.
    Compact summaries carry ``matched_at_ms`` (newest matching observation)
    plus resources but no snapshots/raw event arrays.
    """
    canonical_project = _optional_project_filter(project_id)
    app = _optional_search_text("application", application) if application is not None else None
    res = _optional_search_text("resource", resource) if resource is not None else None
    dev = _optional_device_filter(device)
    qtext = _optional_search_text("query", query) if query is not None else None
    range_from, range_to = _validate_session_range(from_ms, to_ms)
    bound = _validate_limit(limit, DEFAULT_LIMIT)
    binary = resolve_desktop_bin(desktop_bin)
    db_path = resolve_db_path(db)
    return _fetch_search(
        binary, db_path, canonical_project, app, res, dev, qtext,
        range_from, range_to, bound,
    )


def get_session(
    session_id=None,
    resource_limit: object = None,
    include_events: object = None,
    event_limit: object = None,
    desktop_bin=None,
    db=None,
) -> dict:
    """Return one session detail (Rust ``session-detail`` verbatim).

    Direct DB query; no compositor/registry call. ``session`` (32-hex) is
    required. Events are excluded by default and only ``include_events``
    includes them; ``event_limit`` is only valid with ``include_events``.
    Limits stay ``1..1000`` (defaults 20). The exact
    ``{session,resources,events_included,events?}`` shape is validated:
    ``events`` is present only when requested.
    """
    canonical = _validate_session_id(session_id)
    bound_res = _validate_resource_limit(resource_limit)
    want_events = _parse_include_events(include_events)
    bound_evt: int | None = None
    if event_limit is not None:
        if not want_events:
            _error("--event-limit requires --include-events")
        bound_evt = _validate_event_limit(event_limit)
    binary = resolve_desktop_bin(desktop_bin)
    db_path = resolve_db_path(db)
    return _fetch_session_detail(
        binary, db_path, canonical, bound_res, want_events, bound_evt,
    )


def project_activity(
    project_id=None,
    application: object = None,
    resource: object = None,
    device: object = None,
    query: object = None,
    from_ms: object = None,
    to_ms: object = None,
    limit: object = None,
    registry_file=None,
    desktop_bin=None,
    db=None,
) -> dict:
    """Return session-centric search scoped to one project plus metadata.

    Same Rust ``search`` as :func:`search_activity`, but defaults to the
    fresh current stable project identity; an explicit project UUID skips
    the compositor. Registry is resolved for display only (unknown explicit
    UUIDs still query, like the history APIs). Returns
    ``{project,reported_project?,registry,requested_project_id,query,
    sessions,count,reason}``. All non-project filters/range/limit thread
    through. The default-current two-call path shares one
    ``REQUEST_TIMEOUT`` deadline. Compact summaries carry resources but no
    snapshots/raw event arrays.
    """
    app = _optional_search_text("application", application) if application is not None else None
    res = _optional_search_text("resource", resource) if resource is not None else None
    dev = _optional_device_filter(device)
    qtext = _optional_search_text("query", query) if query is not None else None
    range_from, range_to = _validate_session_range(from_ms, to_ms)
    bound = _validate_limit(limit, DEFAULT_LIMIT)
    binary = resolve_desktop_bin(desktop_bin)
    db_path = resolve_db_path(db)
    if project_id is not None and str(project_id).strip():
        canonical = _validate_project_id(project_id)
        display, entry, _status, reason = _resolve_explicit_identity(
            canonical, registry_file
        )
        found = _fetch_search(
            binary, db_path, canonical, app, res, dev, qtext,
            range_from, range_to, bound,
        )
        return {
            "project": display,
            "registry": entry,
            "requested_project_id": canonical,
            "query": found.get("query"),
            "sessions": found.get("sessions"),
            "count": found.get("count"),
            "reason": reason if entry is None else "",
        }
    deadline = time.monotonic() + REQUEST_TIMEOUT
    context, reported, entry, status, reason = _resolve_current_identity(
        binary, db_path, registry_file, timeout=REQUEST_TIMEOUT
    )
    _ = context
    project_out, should_query = _default_history_target(reported, entry, status)
    if not should_query:
        return {
            "project": project_out,
            "reported_project": reported,
            "registry": entry,
            "requested_project_id": None,
            "query": {
                "project": None,
                "application": app,
                "resource": res,
                "device": dev,
                "query": qtext,
                "from_ms": range_from,
                "to_ms": range_to,
                "range_semantics": (
                    "start inclusive, end exclusive, UTC epoch-ms"
                    if range_from is not None else None
                ),
                "limit": bound,
            },
            "sessions": [],
            "count": 0,
            "reason": reason,
        }
    assert entry is not None
    target = entry["id"]
    found = _fetch_search(
        binary, db_path, target, app, res, dev, qtext,
        range_from, range_to, bound, timeout=_remaining(deadline),
    )
    return {
        "project": _display_for_entry(reported, entry),
        "reported_project": reported,
        "registry": entry,
        "requested_project_id": None,
        "query": found.get("query"),
        "sessions": found.get("sessions"),
        "count": found.get("count"),
        "reason": "",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only desktop-project views (current desktop project via "
        "qs-desktop-context + canonical registry + Logseq read_page)."
    )
    parser.add_argument("--desktop-bin", default=None,
                        help="qs-desktop-context binary; defaults to "
                        "QS_DESKTOP_CONTEXT_BIN or "
                        "<repo>/services/agent-orchestrator/target/release/"
                        "qs-desktop-context")
    parser.add_argument("--db", default=None,
                        help="activity DB path; defaults to QS_DESKTOP_DB / "
                        "QS_DESKTOP_CONTEXT_DB or the binary default")
    parser.add_argument("--projects-file", default=None,
                        help="registry TOML file; defaults to "
                        "QUICKSHELL_PROJECTS_FILE or <repo>/projects.toml")
    parser.add_argument("--graph", default=None,
                        help="graph directory for todos/logseq-context only; "
                        "defaults to LOGSEQ_GRAPH or logseqGraph in settings.json")
    parser.add_argument("command", choices=("current-project", "todos",
                                            "logseq-context", "recent-activity",
                                            "last-activity", "resources",
                                            "current-session", "sessions",
                                            "last-session", "session-resources",
                                            "session-events", "current-context",
                                            "search-activity", "get-session",
                                            "project-activity", "device-id"))
    parser.add_argument("--project", default=None,
                        help="explicit project UUID (historical queries); "
                        "defaults to the current desktop project")
    parser.add_argument("--limit", default=None,
                        help="bounded history/resources limit 1..1000 "
                        "(default 20; recent-activity/resources/sessions/"
                        "session-resources/session-events/search-activity/"
                        "project-activity only)")
    parser.add_argument("--session", default=None,
                        help="explicit work session id (32 hex chars) for "
                        "session-resources/session-events/get-session only")
    parser.add_argument("--from", dest="from_ms", default=None,
                        help="range start (UTC epoch-ms, >= 0); "
                        "sessions/search-activity/project-activity only, "
                        "must pair with --to")
    parser.add_argument("--to", dest="to_ms", default=None,
                        help="range end (UTC epoch-ms, >= 0); "
                        "sessions/search-activity/project-activity only, "
                        "must pair with --from")
    parser.add_argument("--application", default=None,
                        help="application filter for "
                        "search-activity/project-activity only "
                        "(trimmed 1..256 chars)")
    parser.add_argument("--resource", default=None,
                        help="resource filter for search-activity/"
                        "project-activity only (trimmed 1..256 chars)")
    parser.add_argument("--device", default=None,
                        help="device filter for search-activity/"
                        "project-activity only (32 hex chars)")
    parser.add_argument("--query", default=None,
                        help="free-text filter for search-activity/"
                        "project-activity only (trimmed 1..256 chars)")
    parser.add_argument("--resource-limit", default=None,
                        help="bounded resource limit 1..1000 (default 20; "
                        "get-session only)")
    parser.add_argument("--include-events", action="store_true", default=False,
                        help="include raw events (get-session only)")
    parser.add_argument("--event-limit", default=None,
                        help="bounded event limit 1..1000 (default 20; "
                        "get-session only with --include-events)")
    try:
        args = parser.parse_args(argv)
        raw_commands = ("current-project", "todos", "logseq-context",
                        "recent-activity", "last-activity", "resources")
        session_commands = ("current-session", "sessions", "last-session",
                            "session-resources", "session-events")
        legacy_commands = raw_commands + session_commands + ("device-id",)
        search_commands = ("search-activity", "project-activity")
        if args.command == "current-project" and args.project:
            _error("current-project does not accept --project")
        if args.command == "current-project" and args.limit is not None:
            _error("current-project does not accept --limit")
        if args.command in ("todos", "logseq-context") and args.limit is not None:
            _error(f"{args.command} does not accept --limit")
        if args.command == "last-activity" and args.limit is not None:
            _error("last-activity does not accept --limit")
        # Raw operations keep their contract: new session flags are rejected.
        if args.command in raw_commands and args.session:
            _error(f"{args.command} does not accept --session")
        if args.command in ("current-project", "todos", "logseq-context",
                            "recent-activity", "last-activity", "resources") and (
                args.from_ms is not None or args.to_ms is not None):
            _error(f"{args.command} does not accept --from/--to")
        # New work-session gates (mirrors the Rust CLI usage).
        if args.command == "current-session" and (
                args.project or args.limit is not None
                or args.session or args.from_ms is not None
                or args.to_ms is not None):
            _error("current-session does not accept "
                   "--project/--limit/--session/--from/--to")
        if args.command == "sessions" and args.session:
            _error("sessions does not accept --session")
        if args.command == "last-session" and not args.project:
            _error("last-session requires an explicit --project UUID")
        if args.command == "last-session" and (
                args.limit is not None or args.session
                or args.from_ms is not None or args.to_ms is not None):
            _error("last-session does not accept "
                   "--limit/--session/--from/--to")
        if args.command in ("session-resources", "session-events"):
            if not args.session:
                _error(f"{args.command} requires --session SESSION_ID")
            if args.project:
                _error(f"{args.command} does not accept --project")
            if args.from_ms is not None or args.to_ms is not None:
                _error(f"{args.command} does not accept --from/--to")
        # Legacy commands never accept Phase 5 search/detail flags.
        if args.command in legacy_commands and (
                args.application is not None or args.resource is not None
                or args.device is not None or args.query is not None):
            _error(f"{args.command} does not accept "
                   "--application/--resource/--device/--query")
        if args.command in legacy_commands and (
                args.resource_limit is not None or args.event_limit is not None
                or args.include_events):
            _error(f"{args.command} does not accept "
                   "--resource-limit/--event-limit/--include-events")
        # Phase 5 coherent surface gates.
        if args.command == "current-context" and (
                args.project or args.limit is not None or args.session
                or args.from_ms is not None or args.to_ms is not None
                or args.application is not None or args.resource is not None
                or args.device is not None or args.query is not None
                or args.resource_limit is not None
                or args.event_limit is not None or args.include_events):
            _error("current-context does not accept "
                   "--project/--limit/--session/--from/--to/"
                   "--application/--resource/--device/--query/"
                   "--resource-limit/--event-limit/--include-events")
        if args.command in search_commands and args.session:
            _error(f"{args.command} does not accept --session")
        if args.command in search_commands and (
                args.resource_limit is not None or args.event_limit is not None
                or args.include_events):
            _error(f"{args.command} does not accept "
                   "--resource-limit/--event-limit/--include-events")
        if args.command == "get-session":
            if not args.session:
                _error("get-session requires --session SESSION_ID")
            if args.project:
                _error("get-session does not accept --project")
            if args.limit is not None:
                _error("get-session does not accept --limit")
            if args.from_ms is not None or args.to_ms is not None:
                _error("get-session does not accept --from/--to")
            if (args.application is not None or args.resource is not None
                    or args.device is not None or args.query is not None):
                _error("get-session does not accept "
                       "--application/--resource/--device/--query")
            if args.event_limit is not None and not args.include_events:
                _error("--event-limit requires --include-events")
        if args.command == "current-context":
            value = current_context(args.projects_file, args.desktop_bin, args.db)
        elif args.command == "device-id":
            if (args.project or args.limit is not None or args.session
                    or args.from_ms is not None or args.to_ms is not None
                    or args.application is not None or args.resource is not None
                    or args.device is not None or args.query is not None
                    or args.resource_limit is not None
                    or args.event_limit is not None or args.include_events):
                _error("device-id does not accept "
                       "--project/--limit/--session/--from/--to/"
                       "--application/--resource/--device/--query/"
                       "--resource-limit/--event-limit/--include-events")
            value = device_id(args.desktop_bin, args.db)
        elif args.command == "search-activity":
            value = search_activity(args.project, args.application,
                                    args.resource, args.device, args.query,
                                    args.from_ms, args.to_ms, args.limit,
                                    args.desktop_bin, args.db)
        elif args.command == "get-session":
            value = get_session(args.session, args.resource_limit,
                                args.include_events, args.event_limit,
                                args.desktop_bin, args.db)
        elif args.command == "project-activity":
            value = project_activity(args.project, args.application,
                                     args.resource, args.device, args.query,
                                     args.from_ms, args.to_ms, args.limit,
                                     args.projects_file, args.desktop_bin,
                                     args.db)
        elif args.command == "current-project":
            value = current_project(args.projects_file, args.desktop_bin, args.db)
        elif args.command == "todos":
            value = project_todos(args.graph, args.project,
                                  args.projects_file, args.desktop_bin, args.db)
        elif args.command == "logseq-context":
            value = project_logseq_context(args.graph, args.project,
                                           args.projects_file, args.desktop_bin,
                                           args.db)
        elif args.command == "recent-activity":
            value = recent_activity(args.project, args.limit,
                                    args.projects_file, args.desktop_bin, args.db)
        elif args.command == "last-activity":
            value = last_activity(args.project, args.projects_file,
                                  args.desktop_bin, args.db)
        elif args.command == "current-session":
            value = current_session(args.desktop_bin, args.db)
        elif args.command == "sessions":
            value = work_sessions(args.project, args.limit,
                                  args.from_ms, args.to_ms,
                                  args.projects_file, args.desktop_bin, args.db)
        elif args.command == "last-session":
            value = last_session(args.project,
                                 args.projects_file, args.desktop_bin, args.db)
        elif args.command == "session-resources":
            value = session_resources(args.session, args.limit,
                                      args.desktop_bin, args.db)
        elif args.command in session_commands:
            value = session_events(args.session, args.limit,
                                   args.desktop_bin, args.db)
        else:
            value = project_resources(args.project, args.limit,
                                      args.projects_file, args.desktop_bin, args.db)
        print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        return 0
    except SystemExit as exc:
        return 0 if exc.code == 0 else 1
    except (DesktopError, OSError, TypeError, ValueError,
            UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
