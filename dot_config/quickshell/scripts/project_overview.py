#!/usr/bin/env python3
"""Compact active-project overview for the tray popup (read-only).

One bounded CLI call backing ``widgets/ProjectOverviewPopup.qml``. It
aggregates, for an explicit project UUID:

- open TODOs via ``desktop_projects.project_todos`` (which reuses the
  ``project_planner`` page parser verbatim; no duplicate parser here),
- tracked work-session time via ``desktop_projects.work_sessions`` (sum of
  ``end_ms - start_ms`` over a bounded latest-N window, with the scope
  explicitly labelled so the UI never claims an unbounded "all-time" total),
- the latest work session via ``desktop_projects.last_session`` plus its
  change-only evidence via ``project_session_changes.session_changes``
  (baseline commit at session start -> latest/current commit plus working
  tree, bounded change-only text owned by the session sidecar; no LLM
  here, no app/window activity anywhere in the recap path). Recap
  eligibility additionally requires ``has_baseline``: sessions without a
  captured start commit stay explicitly unavailable.

The ``project_session_changes`` module is owned by another worker and may
not exist yet: the import is guarded and the contract is mocked in tests.
A missing baseline is an explicit unavailable result, never a guessed
recap. This module never implements session persistence or baseline
capture itself.

All sections are best-effort: any helper failure yields
``{available: False, reason}`` for that section while the other sections
still return. Only argument validation fails the whole request
(single-line ``error: ...`` on stderr, exit 1, no traceback).
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import NoReturn

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import desktop_projects as _dp
import qscli

try:
    import project_session_changes as _psc
except ImportError:  # Owned by another worker; may not exist yet.
    _psc = None  # type: ignore


class OverviewError(ValueError):
    pass


def _error(message: str) -> NoReturn:
    raise OverviewError(message)


MAX_OPEN_TODOS = 10
DEFAULT_SESSIONS_LIMIT = 20
MAX_SESSIONS_LIMIT = 100
MAX_TEXT_CHARS = 300
# Defensive clip on sidecar evidence (the sidecar owns its own bound;
# this only guarantees our transport stays small if it ever grows).
# Upper bound matching the sidecar (project_session_changes caps evidence at
# 8000 with balanced per-section budgets). This is a defensive pass-through
# cap only: bounded sidecar evidence must never be prefix-clipped here, or
# the baseline/latest attribution sections and the trailing limitation
# disclaimer could be stripped, leaving false committed-work attribution.
MAX_EVIDENCE_CHARS = 8000


def _validate_project_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("project must be a UUID string")
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError as exc:
        raise OverviewError("project must be a UUID string") from exc


def _validate_bounded_limit(value: object, default: int, maximum: int,
                             name: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        _error(f"{name} must be an integer 1..{maximum}")
    if isinstance(value, int):
        limit = value
    elif isinstance(value, str) and value.strip():
        try:
            limit = int(value.strip(), 10)
        except ValueError as exc:
            raise OverviewError(
                f"{name} must be an integer 1..{maximum}") from exc
    else:
        _error(f"{name} must be an integer 1..{maximum}")
        raise AssertionError("unreachable")
    if limit < 1 or limit > maximum:
        _error(f"{name} must be 1..{maximum}")
    return limit


def _bound_text(value: object, limit: int = MAX_TEXT_CHARS) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _session_duration_ms(row: object) -> int:
    if not isinstance(row, dict):
        return 0
    start = row.get("start_ms")
    end = row.get("end_ms")
    if (isinstance(start, int) and not isinstance(start, bool)
            and isinstance(end, int) and not isinstance(end, bool)
            and end >= start):
        return end - start
    return 0


def _curate_session_row(row: object) -> dict:
    if not isinstance(row, dict):
        return {}
    out: dict = {
        "session_id": row.get("session_id", ""),
        "start_ms": row.get("start_ms"),
        "end_ms": row.get("end_ms"),
        "duration_ms": _session_duration_ms(row),
        "event_count": row.get("event_count"),
        "status": row.get("status", ""),
    }
    return out


def _collect_session_changes(project_id: str, session_id: str) -> dict:
    """Return the change-only evidence contract for one work session.

    Real sidecar shape (``project_session_changes.session_changes``):
    ``{available, reason, project_id, session_id, baseline_commit,
    latest_commit, has_baseline, summary_key, evidence, repo_path,
    observed_at_ms, updated_at_ms, limitations}``. A missing module, a
    malformed response, or sidecar ``available: False`` all yield
    ``{available: False, reason}`` explicitly.

    Recap eligibility is decided by the popup from ``has_baseline``: a
    session without a captured baseline (e.g. ``missing-baseline``,
    which still carries latest-state evidence) shows its reason and
    never gets an LLM recap. Old sessions can never be retroactively
    baselined here.
    """
    unavailable = {"available": False, "baseline_commit": "",
                   "latest_commit": "", "has_baseline": False,
                   "summary_key": "", "evidence": "", "repo_path": "",
                   "limitations": "", "reason": ""}
    if _psc is None:
        unavailable["reason"] = (
            "session change tracking is unavailable "
            "(project_session_changes is not installed)")
        return unavailable
    try:
        result = _psc.session_changes(project_id, session_id)
    except Exception as exc:
        unavailable["reason"] = _bound_text(
            f"session changes unavailable: {exc}")
        return unavailable
    if not isinstance(result, dict):
        unavailable["reason"] = "session changes response is invalid"
        return unavailable
    if not result.get("available"):
        unavailable["reason"] = _bound_text(
            result.get("reason") or "no baseline commit for this session")
        return unavailable
    baseline = result.get("baseline_commit") or ""
    latest = result.get("latest_commit") or ""
    key = result.get("summary_key") or ""
    evidence = result.get("evidence") or ""
    if not isinstance(baseline, str):
        baseline = ""
    if not isinstance(latest, str):
        latest = ""
    if not isinstance(key, str):
        key = ""
    if not isinstance(evidence, str):
        evidence = ""
    has_baseline = bool(result.get("has_baseline")) and bool(baseline.strip())
    if not has_baseline:
        # No captured start commit: explicit unavailable recap, never a
        # fabricated baseline. Latest-state evidence (if any) is kept
        # for display context only.
        return {
            "available": False,
            "baseline_commit": baseline.strip(),
            "latest_commit": latest.strip(),
            "has_baseline": False,
            "summary_key": key.strip(),
            "evidence": evidence[:MAX_EVIDENCE_CHARS],
            "repo_path": str(result.get("repo_path") or ""),
            "limitations": str(result.get("limitations") or ""),
            "reason": _bound_text(
                result.get("reason")
                or "no baseline commit for this session"),
        }
    if not key.strip():
        unavailable["reason"] = "session changes have no summary identity"
        return unavailable
    if not evidence.strip():
        unavailable["reason"] = "no repository changes recorded for this session"
        return unavailable
    if len(evidence) > MAX_EVIDENCE_CHARS:
        evidence = evidence[:MAX_EVIDENCE_CHARS].rstrip() + "…"
    return {
        "available": True,
        "baseline_commit": baseline.strip(),
        "latest_commit": latest.strip(),
        "has_baseline": True,
        "summary_key": key.strip(),
        "evidence": evidence,
        "repo_path": str(result.get("repo_path") or ""),
        "limitations": str(result.get("limitations") or ""),
        "reason": "",
    }


def _collect_todos(project_id: str, graph, registry_file, desktop_bin,
                   db) -> dict:
    try:
        view = _dp.project_todos(
            graph=graph, project_id=project_id,
            registry_file=registry_file, desktop_bin=desktop_bin, db=db)
    except Exception as exc:
        return {"available": False, "open": [], "open_count": 0,
                "total_count": 0,
                "reason": _bound_text(f"todos unavailable: {exc}")}
    todos = view.get("todos", [])
    if not isinstance(todos, list):
        return {"available": False, "open": [], "open_count": 0,
                "total_count": 0, "reason": "todos response is invalid"}
    valid = [t for t in todos if isinstance(t, dict)]
    open_all = [t for t in valid if not t.get("done")]
    clipped = []
    for item in open_all[:MAX_OPEN_TODOS]:
        clipped.append({
            "line": item.get("line", 0),
            "task": _bound_text(item.get("task", "")),
            "marker": str(item.get("marker", ""))[:16],
        })
    if not valid:
        reason = str(view.get("reason", "")) or "no TODOs on the linked page"
        # Name-only / no-linkage projects surface here as valid-but-empty.
        return {"available": True, "open": [], "open_count": 0,
                "total_count": 0, "reason": _bound_text(reason)}
    return {"available": True, "open": clipped, "open_count": len(open_all),
            "total_count": len(valid), "reason": ""}


def _collect_work_time(project_id: str, sessions_limit: int, registry_file,
                       desktop_bin, db) -> dict:
    try:
        result = _dp.work_sessions(
            project_id=project_id, limit=sessions_limit,
            registry_file=registry_file, desktop_bin=desktop_bin, db=db)
    except Exception as exc:
        return {"available": False, "sessions": [], "count": 0,
                "total_ms": 0,
                "scope_label": f"last {sessions_limit} work sessions",
                "reason": _bound_text(f"work sessions unavailable: {exc}")}
    rows = result.get("sessions", [])
    if not isinstance(rows, list):
        return {"available": False, "sessions": [], "count": 0,
                "total_ms": 0,
                "scope_label": f"last {sessions_limit} work sessions",
                "reason": "sessions response is invalid"}
    curated = [_curate_session_row(r) for r in rows if isinstance(r, dict)]
    total = sum(int(c.get("duration_ms", 0) or 0) for c in curated)
    scope = (f"last {sessions_limit} work sessions "
             f"(showing {len(curated)})")
    if not curated:
        reason = str(result.get("reason", "")) or "no work sessions recorded"
        return {"available": True, "sessions": [], "count": 0,
                "total_ms": 0, "scope_label": scope,
                "reason": _bound_text(reason)}
    return {"available": True, "sessions": curated, "count": len(curated),
            "total_ms": total, "scope_label": scope, "reason": ""}


def _collect_last_session(project_id: str, registry_file,
                          desktop_bin, db) -> dict:
    try:
        result = _dp.last_session(
            project_id=project_id, registry_file=registry_file,
            desktop_bin=desktop_bin, db=db)
    except Exception as exc:
        return {"available": False, "session": None,
                "changes": _collect_session_changes(project_id, ""),
                "reason": _bound_text(f"last session unavailable: {exc}")}
    row = result.get("session")
    if not isinstance(row, dict):
        reason = str(result.get("reason", "")) or "no work session recorded"
        return {"available": False, "session": None,
                "changes": {"available": False, "baseline_commit": "",
                            "latest_commit": "", "summary_key": "",
                            "evidence": "", "reason": _bound_text(reason)},
                "reason": _bound_text(reason)}
    session_id = row.get("session_id", "")
    if not isinstance(session_id, str) or not session_id.strip():
        return {"available": False, "session": None,
                "changes": {"available": False, "baseline_commit": "",
                            "latest_commit": "", "summary_key": "",
                            "evidence": "",
                            "reason": "latest session has no session identity"},
                "reason": "latest session has no session identity"}
    changes = _collect_session_changes(project_id, session_id.strip())
    return {"available": True, "session": _curate_session_row(row),
            "changes": changes,
            "reason": "" if changes.get("available") else changes.get(
                "reason", "")}


def overview_for_project(project_id, *, sessions_limit=None,
                         registry_file=None,
                         desktop_bin=None, db=None, graph=None) -> dict:
    """Build the compact overview object (pure aggregation, no LLM)."""
    canonical = _validate_project_id(project_id)
    sessions_bound = _validate_bounded_limit(
        sessions_limit, DEFAULT_SESSIONS_LIMIT, MAX_SESSIONS_LIMIT,
        "sessions-limit")
    try:
        display, entry, status, reason = _dp._resolve_explicit_identity(
            canonical, registry_file)
    except Exception as exc:
        display = {"id": canonical, "name": "", "matched_by": ""}
        entry = None
        status = "unknown-explicit"
        reason = f"project identity unavailable: {exc}"
    todos = _collect_todos(canonical, graph, registry_file, desktop_bin, db)
    work_time = _collect_work_time(canonical, sessions_bound, registry_file,
                                   desktop_bin, db)
    last_session = _collect_last_session(canonical,
                                         registry_file, desktop_bin, db)
    return {
        "project": display,
        "registry": entry,
        "requested_project_id": canonical,
        "status": status,
        "reason": reason,
        "todos": todos,
        "work_time": work_time,
        "last_session": last_session,
        "sessions_limit": sessions_bound,
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = qscli.SafeParser(
        description="Compact active-project overview (open TODOs, bounded "
        "work-session time, latest session change evidence).")
    qscli.add_global_flags(parser, db=True, graph=True, desktop_bin=True,
                            projects_file=True)
    parser.add_argument("command", choices=("overview",))
    parser.add_argument("--project", default=None,
                        help="explicit project UUID (required)")
    parser.add_argument("--sessions-limit", default=None,
                        help=f"bounded session window 1..{MAX_SESSIONS_LIMIT} "
                        f"(default {DEFAULT_SESSIONS_LIMIT})")
    return parser.parse_args(argv if argv is not None else sys.argv[1:])


def _dispatch(args: argparse.Namespace) -> int:
    if not args.project or not str(args.project).strip():
        _error("overview requires --project UUID")
    value = overview_for_project(
        args.project, sessions_limit=args.sessions_limit,
        registry_file=args.projects_file, desktop_bin=args.desktop_bin,
        db=args.db, graph=args.graph)
    print(json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")))
    return 0


_BOUNDED_EXCEPTIONS = (OverviewError, OSError, TypeError, ValueError,
                       UnicodeError)


def main(argv=None) -> int:
    return qscli.run_main(_parse_args, _dispatch, "project overview",
                          _BOUNDED_EXCEPTIONS, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
