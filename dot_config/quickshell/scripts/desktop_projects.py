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
                                            "last-activity", "resources"))
    parser.add_argument("--project", default=None,
                        help="explicit project UUID (historical queries); "
                        "defaults to the current desktop project")
    parser.add_argument("--limit", default=None,
                        help="bounded history/resources limit 1..1000 "
                        "(default 20; recent-activity/resources only)")
    try:
        args = parser.parse_args(argv)
        if args.command == "current-project" and args.project:
            _error("current-project does not accept --project")
        if args.command == "current-project" and args.limit is not None:
            _error("current-project does not accept --limit")
        if args.command in ("todos", "logseq-context") and args.limit is not None:
            _error(f"{args.command} does not accept --limit")
        if args.command == "last-activity" and args.limit is not None:
            _error("last-activity does not accept --limit")
        if args.command == "current-project":
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
