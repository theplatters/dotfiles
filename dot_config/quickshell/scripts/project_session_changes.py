#!/usr/bin/env python3
"""Read-only per-desktop-session repository change summary (change-only recap).

Each desktop work session is associated with the repository state at that
time: the collector captures the start ``HEAD`` plus a bounded worktree
snapshot/diff when the session is created, refreshes the latest snapshot
while the session stays active (rate-limited), and freezes the row when the
next session begins. Old sessions therefore never compare against today's
mutable ``HEAD``.

This module is the read side only. It never touches window activity for the
recap and never runs ``git``: it reads the private sidecar store
(``<activity-db>.changes.db`` next to the activity DB, schema v4 untouched)
and returns a bounded change-only evidence text suitable for an LLM prompt.
The UI worker calls :func:`session_changes`; it must never use app activity
rows for the recap.

Sidecar rows are keyed by the immutable desktop ``session_id`` (32 lowercase
hex) plus the project UUID. Rows distinguish:

- fresh sessions with a real baseline (``has_baseline`` true),
- upgrade sessions first seen after capture shipped (``has_baseline`` false:
  the start commit is NOT fabricated from the current ``HEAD``),
- sessions with no linked repository (unavailable with an explicit reason).

Baseline dirty state is persisted, so evidence tells "already dirty at
session start" apart from "changed during this session". Timestamps are the
honest last-observed state: an offline gap means the frozen ``latest`` may
predate the exact session end.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import NoReturn

import qscli


class SessionChangesError(ValueError):
    pass


def _error(message: str) -> NoReturn:
    raise SessionChangesError(message)


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

MAX_EVIDENCE_CHARS = 8000
MAX_COMMIT_CHARS = 40
PATH_LIMIT = 4096

DB_ENV_PRIMARY = "QS_DESKTOP_DB"
DB_ENV_FALLBACK = "QS_DESKTOP_CONTEXT_DB"


def _validate_project_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("project must be a UUID string")
    try:
        return str(uuid.UUID(value.strip())).lower()
    except ValueError as exc:
        raise SessionChangesError("project must be a UUID string") from exc


def _validate_session_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("session must be 32 hex chars (128-bit)")
    text = value.strip()
    if len(text) != 32 or any(c not in "0123456789abcdefABCDEF" for c in text):
        _error("session must be 32 hex chars (128-bit)")
    return text.lower()


def _resolve_db_path(explicit: object = None) -> Path | None:
    raw: str | None = None
    if isinstance(explicit, (str, Path)) and str(explicit).strip():
        raw = str(explicit).strip()
    else:
        for key in (DB_ENV_PRIMARY, DB_ENV_FALLBACK):
            value = os.environ.get(key, "")
            if isinstance(value, str) and value.strip():
                raw = value.strip()
                break
    if raw is None:
        # Mirror the Rust default: $XDG_STATE_HOME/quickshell/desktop-activity/
        # activity.db else $HOME/.local/state/... (no creation here).
        xdg = os.environ.get("XDG_STATE_HOME", "")
        if isinstance(xdg, str) and xdg.strip():
            return Path(xdg.strip()) / "quickshell" / "desktop-activity" / "activity.db"
        home = os.environ.get("HOME", "")
        if isinstance(home, str) and home.strip():
            return (Path(home.strip()) / ".local" / "state" / "quickshell"
                    / "desktop-activity" / "activity.db")
        return None
    if len(raw) > PATH_LIMIT or "\x00" in raw:
        _error("desktop db path is too long or contains NUL")
    expanded = os.path.expanduser(raw)
    candidate = Path(expanded)
    if not candidate.is_absolute():
        try:
            candidate = Path(os.path.abspath(os.path.join(os.getcwd(), expanded)))
        except OSError as exc:
            raise SessionChangesError("desktop db path is not accessible") from exc
    return candidate


def sidecar_path_for(db_path: Path) -> Path:
    """Derive the sidecar path exactly like the Rust collector.

    ``<dir>/<file>`` -> ``<dir>/<file>.changes.db`` (e.g. ``activity.db``
    -> ``activity.db.changes.db``). No filesystem access.
    """
    name = db_path.name if db_path.name else "activity.db"
    sidecar_name = f"{name}.changes.db"
    parent = db_path.parent
    if str(parent) and str(parent) not in ("", "."):
        return parent / sidecar_name
    # Rust joins onto the parent even when it is empty-ish; keep the bare
    # filename form for relative DB paths.
    if parent is not None and str(parent) not in ("", "."):
        return parent / sidecar_name
    return Path(sidecar_name)


def _connect_readonly(path: Path):
    if not path.is_absolute():
        try:
            path = Path(os.path.abspath(str(path)))
        except OSError as exc:
            raise SessionChangesError(f"path is not accessible: {exc}") from exc
    uri = "file:" + str(path).replace("?", "%3F") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0, check_same_thread=True)
    except sqlite3.OperationalError as exc:
        raise SessionChangesError(f"cannot open {path}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    return conn


def _clip(text: object, limit: int) -> str:
    collapsed = " ".join(str(text or "").split())
    # Keep newlines for diffs: re-split handled by callers; this helper is
    # for single-line reasons. Diff clipping preserves lines separately.
    if len(collapsed) > limit:
        collapsed = collapsed[:limit].rstrip() + "…"
    return collapsed


def _clip_lines(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    clipped = text[:limit].rstrip()
    # Avoid splitting mid-line awkwardly: cut back to the last newline.
    cut = clipped.rfind("\n")
    if cut > limit // 2:
        clipped = clipped[:cut]
    return clipped.rstrip() + "\n… [evidence truncated]"


def _assemble(limitation: str, sections: list) -> str:
    """Join the mandatory limitation FIRST with sections after it.

    The limitation budget is reserved up front so a fully populated
    evidence body can never clip the disclaimer: only the sections are
    truncated to the remaining budget.
    """
    head = " ".join(limitation.split())
    body_budget = MAX_EVIDENCE_CHARS - len(head) - 1
    if body_budget < 0:
        return _clip_lines(head, MAX_EVIDENCE_CHARS)
    body = _clip_lines("\n".join(s for s in sections if s), body_budget)
    if not body.strip():
        return head
    return f"{head}\n{body}"


def _short(commit: object) -> str | None:
    if not isinstance(commit, str) or not commit.strip():
        return None
    text = commit.strip().lower()
    if len(text) == 40 and all(c in "0123456789abcdef" for c in text):
        return text[:12]
    return text[:12]


ATTRIBUTION_LIMITATION = (
    "Attribution limitation: the baseline worktree patch, the latest "
    "worktree patch, and the commit-range patch below are separately "
    "bounded snapshots. Same-file hunks appearing in more than one "
    "section cannot be mechanically attributed to this session: a "
    "baseline-dirty edit that was committed mid-session appears in BOTH "
    "the baseline section and the commit-range section, and overlapping "
    "worktree hunks may reflect pre-existing edits rather than new work. "
    "Treat same-file overlap as possibly pre-existing."
)


def _build_evidence(row: dict) -> str:
    """Assemble the bounded change-only evidence text from a sidecar row.

    Three separately bounded sections: the frozen commit range
    (baseline..latest, the actual committed session work), the baseline
    worktree patch (pre-existing dirty state), and the latest worktree
    patch. The explicit attribution limitation always leads when a
    baseline exists (its budget is reserved first, so full sections can
    never clip the disclaimer).
    """
    has_baseline = bool(row.get("has_baseline"))
    baseline_commit = row.get("baseline_commit")
    latest_commit = row.get("latest_commit")
    baseline_status = str(row.get("baseline_status") or "")
    baseline_diff = str(row.get("baseline_diff") or "")
    latest_status = str(row.get("latest_status") or "")
    latest_diff = str(row.get("latest_diff") or "")
    commit_meta = str(row.get("commit_meta") or "")
    commit_patch = str(row.get("commit_patch") or "")
    repo_path = str(row.get("repo_path") or "")
    capture_note = str(row.get("capture_note") or "")
    baseline_complete = row.get("baseline_complete", False)
    latest_complete = row.get("latest_complete", False)
    # Conservative pre-v2 defaults: a v1 sidecar never recorded
    # completeness provenance, so missing flags read as partial.
    commit_complete = row.get("commit_complete", False)

    if not has_baseline:
        limitation = (
            "Change capture is incomplete for this session: it started "
            "before change capture shipped, so no start commit was recorded "
            "(the current HEAD was never backfilled as a fabricated start). "
            "Frozen at the last observation; an offline gap means this may "
            "predate the exact session end."
        )
        sections = []
        if latest_commit:
            sections.append(f"Latest observed commit: {_short(latest_commit)}.")
        if latest_status.strip():
            sections.append("Latest observed worktree status (last observed state):")
            sections.append(_clip_lines(latest_status.strip(), 1500))
        if latest_diff.strip():
            sections.append("Latest observed diff vs HEAD (last observed state):")
            sections.append(_clip_lines(latest_diff.strip(), 2500))
        if capture_note.strip():
            sections.append(f"Capture note: {capture_note.strip()}")
        return _assemble(limitation, sections)

    base_short = _short(baseline_commit) or "unknown"
    late_short = _short(latest_commit) or "unknown"
    frozen = (
        "Frozen at the last observation (last observed state) while the "
        "session was active; the row froze when the next session began, "
        "and an offline gap means this may predate the exact session end."
    )
    limitation = f"{ATTRIBUTION_LIMITATION.strip()} {frozen}"
    sections = []
    # Section 1: committed session work (frozen at refresh time).
    if baseline_commit and latest_commit and baseline_commit != latest_commit:
        sections.append(
            f"Committed during this session: {base_short}..{late_short} "
            f"(start HEAD {baseline_commit}, latest HEAD {latest_commit})."
        )
        if commit_meta.strip():
            sections.append("Commits in range (newest first):")
            sections.append(_clip_lines(commit_meta.strip(), 1200))
        if commit_patch.strip():
            sections.append("Commit-range patch (change-only):")
            sections.append(_clip_lines(commit_patch.strip(), 2500))
        elif not commit_complete:
            sections.append(
                "Commit-range patch unavailable (partial capture; "
                "hashes above are the honest range).")
    elif baseline_commit and latest_commit:
        sections.append(
            f"No new commits during this session (HEAD stayed {base_short}).")
    else:
        sections.append(
            "Commit range unavailable (unborn HEAD or git failed at capture); "
            "worktree sections below are the last observed state.")
    # Section 2: baseline worktree (pre-existing, NOT new work).
    if baseline_status.strip() or baseline_diff.strip():
        sections.append(
            "Already dirty at session start (baseline worktree vs HEAD; "
            "pre-existing edits, NOT new work in this session):")
        if baseline_status.strip():
            sections.append(_clip_lines(baseline_status.strip(), 1000))
        if baseline_diff.strip():
            sections.append(_clip_lines(baseline_diff.strip(), 1500))
        if not baseline_complete:
            sections.append("(baseline snapshot partial; see capture note)")
    else:
        sections.append("Worktree was clean at session start.")
    # Section 3: latest worktree.
    if (latest_status.strip() != baseline_status.strip()
            or latest_diff.strip() != baseline_diff.strip()):
        if latest_status.strip() or latest_diff.strip():
            sections.append("Worktree at last observation (latest vs HEAD):")
            if latest_status.strip():
                sections.append(_clip_lines(latest_status.strip(), 1000))
            if latest_diff.strip():
                sections.append(_clip_lines(latest_diff.strip(), 1500))
            if not latest_complete:
                sections.append("(latest snapshot partial; see capture note)")
        else:
            sections.append(
                "Worktree clean at last observation (baseline dirt was "
                "committed or reverted during the session).")
    if capture_note.strip():
        sections.append(f"Capture note: {capture_note.strip()}")
    if repo_path:
        sections.append(f"Repository: {repo_path}")
    return _assemble(limitation, sections)


def session_changes(project_id, session_id, *, db=None) -> dict:
    """Return the read-only change summary for one desktop session.

    Arguments are validated fail-closed (``project`` UUID, ``session``
    32-hex). ``db`` optionally overrides the activity DB path (same
    precedence as the other scripts); the sidecar is always derived as
    ``<db>.changes.db`` and opened read-only.

    Returns a dict with ``available``, ``reason``, ``project_id``,
    ``session_id``, ``baseline_commit``, ``latest_commit``,
    ``has_baseline``, ``summary_key``, ``evidence`` (bounded change-only
    text), ``repo_path``, ``observed_at_ms``, ``updated_at_ms``, and
    ``limitations``. Never raises for missing data: those read as
    ``available: False`` with an explicit reason. Only argument validation
    raises :class:`SessionChangesError`.
    """
    canonical_project = _validate_project_id(project_id)
    canonical_session = _validate_session_id(session_id)

    db_path = _resolve_db_path(db)
    if db_path is None:
        return {
            "available": False,
            "reason": "activity database path cannot be resolved "
            "(set --db or QS_DESKTOP_DB)",
            "project_id": canonical_project,
            "session_id": canonical_session,
            "baseline_commit": None,
            "latest_commit": None,
            "has_baseline": False,
            "summary_key": "unavailable",
            "evidence": "",
            "repo_path": "",
            "observed_at_ms": None,
            "updated_at_ms": None,
            "limitations": "last observed state, not exact end if offline",
        }
    sidecar = sidecar_path_for(db_path)
    if not sidecar.exists():
        # Distinguish "session unknown" from "capture not shipped yet" when
        # the activity DB itself is readable.
        session_known: bool | None = None
        try:
            if db_path.exists():
                with _connect_readonly(db_path) as conn:
                    cur = conn.execute(
                        "SELECT session_id, project_id FROM sessions "
                        "WHERE session_id = ?1",
                        (canonical_session,),
                    ).fetchone()
                    session_known = cur is not None
        except Exception:
            session_known = None
        if session_known:
            reason = (
                "no change capture for this session "
                "(session predates capture or has no linked repository; "
                "start commit not fabricated)"
            )
        elif session_known is False:
            reason = "unknown session (no such desktop session recorded)"
        else:
            reason = (
                "change capture unavailable "
                "(no sidecar store yet; collector upgrade pending)"
            )
        return {
            "available": False,
            "reason": reason,
            "project_id": canonical_project,
            "session_id": canonical_session,
            "baseline_commit": None,
            "latest_commit": None,
            "has_baseline": False,
            "summary_key": "unavailable",
            "evidence": "",
            "repo_path": "",
            "observed_at_ms": None,
            "updated_at_ms": None,
            "limitations": "last observed state, not exact end if offline",
        }
    try:
        with _connect_readonly(sidecar) as conn:
            # v1/v2 tolerance: older sidecars lack the v2 columns (the
            # collector migrates on next open, but reads must not break).
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(session_changes)").fetchall()]
            want = ["session_id", "project_id", "repo_path", "repo_gitdir",
                    "repo_status", "baseline_commit", "baseline_status",
                    "baseline_diff", "baseline_complete", "latest_commit",
                    "latest_status", "latest_diff", "latest_complete",
                    "commit_meta", "commit_patch", "commit_complete",
                    "capture_note", "has_baseline", "reason",
                    "created_at_ms", "updated_at_ms", "observed_at_ms"]
            have = [c for c in want if c in cols]
            cur = conn.execute(
                "SELECT {} FROM session_changes WHERE session_id = ?1".format(
                    ", ".join(have)),
                (canonical_session,),
            ).fetchone()
            if cur is not None:
                cur = dict(zip(have, cur))
    except SessionChangesError as exc:
        return {
            "available": False,
            "reason": f"change capture unreadable: {exc}",
            "project_id": canonical_project,
            "session_id": canonical_session,
            "baseline_commit": None,
            "latest_commit": None,
            "has_baseline": False,
            "summary_key": "unavailable",
            "evidence": "",
            "repo_path": "",
            "observed_at_ms": None,
            "updated_at_ms": None,
            "limitations": "last observed state, not exact end if offline",
        }
    except Exception as exc:
        return {
            "available": False,
            "reason": f"change capture unreadable: {exc}",
            "project_id": canonical_project,
            "session_id": canonical_session,
            "baseline_commit": None,
            "latest_commit": None,
            "has_baseline": False,
            "summary_key": "unavailable",
            "evidence": "",
            "repo_path": "",
            "observed_at_ms": None,
            "updated_at_ms": None,
            "limitations": "last observed state, not exact end if offline",
        }
    if cur is None:
        return {
            "available": False,
            "reason": "no change capture for this session "
            "(session predates capture, has no linked repository, "
            "or is unresolved; start commit not fabricated)",
            "project_id": canonical_project,
            "session_id": canonical_session,
            "baseline_commit": None,
            "latest_commit": None,
            "has_baseline": False,
            "summary_key": "unavailable",
            "evidence": "",
            "repo_path": "",
            "observed_at_ms": None,
            "updated_at_ms": None,
            "limitations": "last observed state, not exact end if offline",
        }
    row_project = str(cur["project_id"] or "").lower()
    if row_project != canonical_project:
        return {
            "available": False,
            "reason": "session belongs to another project "
            "(project mismatch; no cross-project recap)",
            "project_id": canonical_project,
            "session_id": canonical_session,
            "baseline_commit": None,
            "latest_commit": None,
            "has_baseline": False,
            "summary_key": "unavailable",
            "evidence": "",
            "repo_path": "",
            "observed_at_ms": None,
            "updated_at_ms": None,
            "limitations": "last observed state, not exact end if offline",
        }
    row = {
        "has_baseline": bool(cur["has_baseline"]),
        "baseline_commit": cur["baseline_commit"],
        "latest_commit": cur["latest_commit"],
        "baseline_status": cur["baseline_status"] or "",
        "baseline_diff": cur["baseline_diff"] or "",
        # Conservative pre-v2 defaults: a v1 sidecar never recorded
        # completeness provenance, so missing flags read as partial.
        "baseline_complete": bool(cur.get("baseline_complete", 0)),
        "latest_status": cur["latest_status"] or "",
        "latest_diff": cur["latest_diff"] or "",
        "latest_complete": bool(cur.get("latest_complete", 0)),
        "commit_meta": cur.get("commit_meta") or "",
        "commit_patch": cur.get("commit_patch") or "",
        "commit_complete": bool(cur.get("commit_complete", 0)),
        "capture_note": cur.get("capture_note") or "",
        "repo_path": cur["repo_path"] or "",
        "repo_status": cur.get("repo_status") or "ok",
    }
    stored_reason = str(cur["reason"] or "")
    observed_at = cur["observed_at_ms"]
    updated_at = cur["updated_at_ms"]
    if row["repo_status"] == "mismatch":
        return {
            "available": False,
            "reason": "linked folder changed repositories after capture; "
            "row frozen and never compared across repos",
            "project_id": canonical_project,
            "session_id": canonical_session,
            "baseline_commit": None,
            "latest_commit": None,
            "has_baseline": False,
            "summary_key": "unavailable",
            "evidence": "",
            "repo_path": "",
            "observed_at_ms": observed_at,
            "updated_at_ms": updated_at,
            "limitations": "last observed state, not exact end if offline",
        }
    if not row["has_baseline"] and not row["repo_path"]:
        return {
            "available": False,
            "reason": stored_reason or "no linked repository for project",
            "project_id": canonical_project,
            "session_id": canonical_session,
            "baseline_commit": None,
            "latest_commit": row["latest_commit"],
            "has_baseline": False,
            "summary_key": "no-repo",
            "evidence": "",
            "repo_path": "",
            "observed_at_ms": observed_at,
            "updated_at_ms": updated_at,
            "limitations": "last observed state, not exact end if offline",
        }
    evidence = _build_evidence(row)
    base = row["baseline_commit"]
    late = row["latest_commit"]
    if row["has_baseline"] and base and late:
        summary_key = f"{_short(base)}..{_short(late)}"
    elif row["has_baseline"]:
        summary_key = f"{_short(base) or 'unknown'}..{_short(late) or 'unknown'}"
    else:
        summary_key = "missing-baseline"
    return {
        "available": True,
        "reason": stored_reason,
        "project_id": canonical_project,
        "session_id": canonical_session,
        "baseline_commit": base,
        "latest_commit": late,
        "has_baseline": bool(row["has_baseline"]),
        "summary_key": summary_key,
        "evidence": evidence,
        "repo_path": row["repo_path"],
        "observed_at_ms": observed_at,
        "updated_at_ms": updated_at,
        "limitations": "last observed state, not exact end if offline",
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = qscli.SafeParser(
        description="Read-only per-session repository change summary.")
    qscli.add_global_flags(parser, db=True)
    parser.add_argument("command", choices=("changes",))
    parser.add_argument("--project", default=None,
                        help="explicit project UUID (required)")
    parser.add_argument("--session", default=None,
                        help="explicit desktop session id, 32 hex (required)")
    return parser.parse_args(argv if argv is not None else sys.argv[1:])


def _dispatch(args: argparse.Namespace) -> int:
    if not args.project or not str(args.project).strip():
        _error("changes requires --project UUID")
    if not args.session or not str(args.session).strip():
        _error("changes requires --session 32HEX")
    value = session_changes(args.project, args.session, db=args.db)
    print(json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")))
    return 0


_BOUNDED_EXCEPTIONS = (SessionChangesError, OSError, TypeError, ValueError,
                       UnicodeError)


def main(argv=None) -> int:
    return qscli.run_main(_parse_args, _dispatch, "session changes",
                          _BOUNDED_EXCEPTIONS, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
