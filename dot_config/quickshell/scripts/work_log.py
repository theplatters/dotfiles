#!/usr/bin/env python3
"""Deterministic work-log drafts for closed work sessions.

A closed desktop session automatically produces a structured draft
(what happened, open TODOs, decisions, next steps) cached in the
``annotations.db`` sidecar. Saving to the project page is always an
explicit two-step flow (``prepare`` shows the exact addition and
revision, ``apply`` writes through the existing revision checks);
page saves nest under the page's ``Session log(s)`` heading. There is
no journal target and no polish: one save target (the project page),
no model calls.

This module never touches the network: evidence comes from local helpers
(``desktop_projects``, ``project_session_changes``, ``annotations``).
Conventions match the other helpers: JSON stdout, single-line
``error: ...`` on stderr with exit 1, list-form argv with ``shell=False``,
bounded IO, no tracebacks, no state content in diagnostics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sqlite3
import stat
import sys
import time
import uuid
from pathlib import Path
from typing import NoReturn

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import annotations as _annotations
import qscli


class WorkLogError(ValueError):
    pass


class EvidenceUnavailable(WorkLogError):
    """Local evidence could not be read (transient; retried next tick)."""


def _error(message: str) -> NoReturn:
    raise WorkLogError(message)


# ---------------------------------------------------------------------------
# Constants (frozen contract)
# ---------------------------------------------------------------------------

MAX_SECTION_ITEMS = 8
MAX_ITEM_CHARS = 240
MAX_MARKDOWN_CHARS = 16384
PREPARED_TTL_MS = 600_000
PREPARED_EXPIRES_IN_S = 600
DRAFT_PROPERTY = "quickshell-worklog"
DRAFT_SECTIONS = (
    ("what_happened", "What happened"),
    ("open_todos", "Open TODOs"),
    ("decisions", "Decisions"),
    ("next", "Next"),
)
INPUT_LIMIT = 1024 * 1024
# Transport cap; single source of truth is qscli.INPUT_LIMIT (same value).
I64_MAX = 9223372036854775807
_PAGE_CONTENT_LIMIT = 128 * 1024
CONTINUATION_TEXT_LIMIT = 2400
CONTINUATION_PROBE_LIMIT = 5
# Established cap from annotations._JSON_PAYLOAD_BYTES (256 KiB): no cached
# draft payload may exceed it, so the continuation lookup excludes larger
# rows in SQL before fetching and re-checks bytes in Python.
_PAYLOAD_BYTES = getattr(_annotations, "_JSON_PAYLOAD_BYTES", 256 * 1024)
if not isinstance(_PAYLOAD_BYTES, int) or isinstance(_PAYLOAD_BYTES, bool) \
        or _PAYLOAD_BYTES <= 0:
    _PAYLOAD_BYTES = 256 * 1024
# Pre-normalization input cap: bounds intermediate string allocations in
# _sanitize_fragment (output is clipped far smaller anyway, so output is
# unchanged). Section-list scan cap: bounds per-row scan work.
_SANITIZE_INPUT_CAP = 4096
_CONTINUATION_SECTION_SCAN_CAP = 32
_CONTINUATION_SECTIONS = (
    ("what_happened", 2),
    ("changes", 2),
    ("files", 3),
    ("open_todos", 3),
    ("next", 2),
)
# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_project_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("project is required")
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        _error("project must be a UUID string")
    raise AssertionError("unreachable")


def _validate_session_opt(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if not isinstance(value, str):
        _error("session must be 32 hex chars (128-bit)")
    text = value.strip()
    if len(text) != 32 or any(c not in "0123456789abcdefABCDEF"
                              for c in text):
        _error("session must be 32 hex chars (128-bit)")
    return text.lower()


def _validate_ms(value: object, what: str = "timestamp is invalid") -> int:
    if isinstance(value, bool):
        _error(what)
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = int(value.strip(), 10)
        except ValueError:
            _error(what)
    else:
        _error(what)
    if parsed < 0 or parsed > I64_MAX:
        _error(what)
    return parsed


def _clip_item(text: object) -> str:
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) > MAX_ITEM_CHARS:
        collapsed = collapsed[:MAX_ITEM_CHARS - 1].rstrip() + "…"
    return collapsed


# ---------------------------------------------------------------------------
# Evidence provider (injectable; production default shells out locally)
# ---------------------------------------------------------------------------

class DefaultEvidence:
    """Production evidence over local helpers (no network, ever).

    ``db`` is the activity-DB override forwarded to the desktop helpers;
    ``desktop_bin`` overrides the helper binary. Every failure reads as
    :class:`EvidenceUnavailable` with a fixed message (never echoing
    paths or helper output).
    """

    def __init__(self, db=None, desktop_bin=None):
        self._db = db
        self._bin = desktop_bin

    def last_session(self, project_id):
        try:
            import desktop_projects as _dp
            result = _dp.last_session(
                project_id, desktop_bin=self._bin, db=self._db)
        except Exception as exc:
            raise EvidenceUnavailable("evidence unavailable") from exc
        if not isinstance(result, dict):
            raise EvidenceUnavailable("evidence unavailable")
        return result.get("session")

    def get_session(self, session_id):
        try:
            import desktop_projects as _dp
            result = _dp.get_session(
                session_id, desktop_bin=self._bin, db=self._db)
        except Exception as exc:
            raise EvidenceUnavailable("evidence unavailable") from exc
        if not isinstance(result, dict):
            raise EvidenceUnavailable("evidence unavailable")
        return result.get("session")

    def session_resources(self, session_id, limit=20):
        try:
            import desktop_projects as _dp
            result = _dp.session_resources(
                session_id, limit, desktop_bin=self._bin, db=self._db)
        except Exception as exc:
            raise EvidenceUnavailable("evidence unavailable") from exc
        if not isinstance(result, dict):
            raise EvidenceUnavailable("evidence unavailable")
        return result

    def project_todos(self, project_id):
        try:
            import desktop_projects as _dp
            result = _dp.project_todos(
                project_id=project_id, desktop_bin=self._bin, db=self._db)
        except Exception as exc:
            raise EvidenceUnavailable("evidence unavailable") from exc
        if not isinstance(result, dict):
            raise EvidenceUnavailable("evidence unavailable")
        return result

    def session_changes(self, project_id, session_id):
        try:
            import project_session_changes as _psc
            result = _psc.session_changes(
                project_id, session_id, db=self._db)
        except Exception as exc:
            raise EvidenceUnavailable("evidence unavailable") from exc
        if not isinstance(result, dict):
            raise EvidenceUnavailable("evidence unavailable")
        return result

    def work_sessions(self, project_id, limit=20):
        try:
            import desktop_projects as _dp
            result = _dp.work_sessions(
                project_id, limit, desktop_bin=self._bin, db=self._db)
        except Exception as exc:
            raise EvidenceUnavailable("evidence unavailable") from exc
        if not isinstance(result, dict):
            raise EvidenceUnavailable("evidence unavailable")
        return result


_EVIDENCE_METHODS = ("last_session", "get_session", "session_resources",
                     "project_todos", "session_changes")


def _as_evidence(evidence, allow_default=False):
    """Return an object exposing the required evidence methods.

    ``None`` (no adapter provided) always means the production
    :class:`DefaultEvidence`. An explicitly provided adapter that is
    missing required methods only falls back to production pieces when
    ``allow_default`` is true (the tick coordinator passes true for its
    own production evidence); otherwise :class:`EvidenceUnavailable` is
    raised so partial fakes can never silently spawn real helpers.
    """
    if evidence is None:
        return DefaultEvidence()
    missing = [name for name in _EVIDENCE_METHODS
               if not callable(getattr(evidence, name, None))]
    if not missing:
        return evidence
    if not allow_default:
        raise EvidenceUnavailable("evidence unavailable")
    fallback = DefaultEvidence()

    class _View:
        pass

    view = _View()
    for name in _EVIDENCE_METHODS:
        fn = getattr(evidence, name, None)
        setattr(view, name, fn if callable(fn) else getattr(fallback, name))
    return view


def _normalize_session_row(value: object) -> dict | None:
    """Accept a bare session row or a ``{"session": row}`` wrapper."""
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    if "session_id" not in value and isinstance(value.get("session"), dict):
        return _normalize_session_row(value.get("session"))
    sid = value.get("session_id")
    if not isinstance(sid, str) or not sid.strip():
        return None
    return value


# ---------------------------------------------------------------------------
# Session helpers (bounded, deterministic)
# ---------------------------------------------------------------------------

def _session_apps(session: dict) -> list[str]:
    apps = session.get("applications")
    if isinstance(apps, list):
        return [str(a)[:64] for a in apps
                if isinstance(a, str) and a.strip()][:32]
    return []


def _session_event_count(session: dict) -> int:
    count = session.get("event_count")
    if isinstance(count, bool):
        return 0
    if isinstance(count, int) and count >= 0:
        return count
    return 0


def _session_bounds(session: dict) -> tuple[int, int]:
    try:
        start = int(session.get("start_ms", 0))
        end = int(session.get("end_ms", 0))
    except (TypeError, ValueError):
        return (0, 0)
    if start < 0 or end < 0:
        return (0, 0)
    return (start, end)


def _is_closed(session: dict) -> bool:
    return session.get("status") == "closed" \
        or session.get("effective_status") == "closed"


def _effective_status(session: dict) -> str:
    for key in ("effective_status", "status"):
        value = session.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:64]
    return ""


def _format_duration(ms: int) -> str:
    total_s = max(0, int(ms) // 1000)
    hours, rest = divmod(total_s, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


# ---------------------------------------------------------------------------
# Resolution: stored attribution -> confirmed override -> suggestion
# ---------------------------------------------------------------------------

def _stored_project(session: dict) -> str | None:
    """Project UUID from an explicit field or the store attribution."""
    raw = session.get("project_id")
    if isinstance(raw, str) and raw.strip():
        try:
            return str(uuid.UUID(raw.strip()))
        except ValueError:
            pass
    project = session.get("project")
    if isinstance(project, dict):
        raw = project.get("id")
        if isinstance(raw, str) and raw.strip():
            try:
                return str(uuid.UUID(raw.strip()))
            except ValueError:
                return None
    return None


def _session_sid(session: dict) -> str | None:
    raw = session.get("session_id")
    if raw is None:
        return None
    if isinstance(raw, str) and not raw.strip():
        return None
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if len(text) != 32 or any(c not in "0123456789abcdefABCDEF"
                              for c in text):
        return None
    return text.lower()


def _registry_display_name(project_id: str) -> str:
    """Best-effort registry display name (failures read as empty)."""
    try:
        import projects as _projects
        data = _projects.list_projects()
    except Exception:
        return ""
    try:
        entries = data.get("projects", []) if isinstance(data, dict) else []
    except Exception:
        return ""
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") == project_id:
            name = entry.get("name", "")
            return name.strip()[:256] if isinstance(name, str) else ""
    return ""


def resolve_session_project(session, *, conn, settings, evidence,
                            now_ms) -> dict | None:
    """Resolve the session's project without network.

    Order: explicit field / stored attribution -> None. No inference,
    no override or suggestion branches. Returns
    ``{"project_id", "source", "suggested"}`` with ``suggested`` always
    False.
    """
    _ = conn
    _ = evidence
    _ = now_ms
    _ = settings
    if not isinstance(session, dict):
        return None
    stored = _stored_project(session)
    if stored is not None:
        source = "explicit" if isinstance(session.get("project_id"), str) \
            and session.get("project_id", "").strip() else "attribution"
        return {"project_id": stored, "source": source, "suggested": False}
    return None


# ---------------------------------------------------------------------------
# Evidence gathering + digest
# ---------------------------------------------------------------------------

def _safe_call(evidence, name: str, *args):
    """Call one evidence method; failures read as EvidenceUnavailable."""
    fn = getattr(evidence, name, None)
    if not callable(fn):
        raise EvidenceUnavailable("evidence unavailable")
    try:
        return fn(*args)
    except EvidenceUnavailable:
        raise
    except Exception as exc:
        raise EvidenceUnavailable("evidence unavailable") from exc


def _resource_list(resources_result: object) -> list:
    if not isinstance(resources_result, dict):
        return []
    items = resources_result.get("resources")
    if not isinstance(items, list):
        return []
    return items[:20]


def _todo_list(todos_result: object) -> list:
    if not isinstance(todos_result, dict):
        return []
    items = todos_result.get("todos")
    if not isinstance(items, list):
        return []
    return [t for t in items if isinstance(t, dict)][:64]


def _resource_key(item: object) -> str:
    if not isinstance(item, dict):
        return str(item)[:120]
    resource = item.get("resource")
    if not isinstance(resource, dict):
        resource = item
    parts = []
    for key in ("adapter", "file", "cwd", "git_root", "git_branch",
                "url", "page", "title"):
        value = resource.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"{key}={value.strip()[:160]}")
    count = item.get("occurrence_count", resource.get("occurrence_count"))
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        parts.append(f"n={count}")
    return "|".join(parts)[:400]


def _evidence_inputs(session: dict, resources: list, changes: dict,
                     todos_result: dict, label: dict | None) -> dict:
    start, end = _session_bounds(session)
    sid = _session_sid(session) or ""
    todos = _todo_list(todos_result)
    lines = []
    for item in todos[:20]:
        line = item.get("line")
        task = item.get("task")
        if isinstance(task, str) and task.strip():
            lines.append(f"{line}:{task.strip()[:80]}")
    label_digest = ""
    if isinstance(label, dict):
        digest = label.get("evidence_digest")
        if isinstance(digest, str):
            label_digest = digest[:256]
    summary_key = changes.get("summary_key", "")
    if not isinstance(summary_key, str):
        summary_key = ""
    return {
        "session": {
            "id": sid,
            "start": start,
            "end": end,
            "apps": sorted(set(_session_apps(session)))[:32],
            "events": _session_event_count(session),
            "status": _effective_status(session),
        },
        "resources": sorted(_resource_key(item)
                            for item in resources[:20]),
        "changes": {
            "summary_key": summary_key[:256],
            "baseline": str(changes.get("baseline_commit") or "")[:64],
            "latest": str(changes.get("latest_commit") or "")[:64],
        },
        "todos": {
            "revision": str(todos_result.get("revision", ""))[:64]
            if isinstance(todos_result, dict) else "",
            "lines": sorted(lines),
        },
        "label": label_digest,
    }


def _evidence_digest(inputs: dict) -> str:
    canonical = json.dumps(inputs, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Deterministic sections
# ---------------------------------------------------------------------------

def _section_what_happened(session: dict, resources=None) -> list[str]:
    """What-happened bullets: resources first, prose demoted (§5.6).

    At most 4 bullets: up to three observed-resource lines, then a
    single collapsed duration/apps/events line. The duration prose is
    never more than that one trailing line.
    """
    items: list[str] = []
    seen: set[str] = set()
    if isinstance(resources, list):
        for item in resources:
            if len(items) >= 3:
                break
            bullet = _resource_bullet(item)
            if not bullet or bullet in seen:
                continue
            seen.add(bullet)
            items.append(_clip_item(bullet))
    start, end = _session_bounds(session)
    duration = _format_duration(max(0, end - start))
    apps = sorted(set(_session_apps(session)))
    events = _session_event_count(session)
    reason = session.get("ended_reason")
    suffix = f", ended by {str(reason).strip()[:64]}" \
        if isinstance(reason, str) and reason.strip() else ""
    if apps:
        collapsed = (f"Session lasted {duration}{suffix}; {events} "
                     f"events across {len(apps)} apps.")
    else:
        collapsed = (f"Session lasted {duration}{suffix}; {events} "
                     f"events recorded.")
    items.append(_clip_item(collapsed))
    return items[:4]


def _resource_bullet(item: object) -> str:
    """One observational resource line, or "" when unusable."""
    label, kind = _resource_label(item)
    if not label:
        return ""
    if kind == "file":
        return f"Worked in {label}."
    if kind == "url":
        return f"Visited {label}."
    return f"Saw {label}."


def _resource_label(item: object) -> tuple[str, str]:
    """Short human label plus kind for one resource row.

    File paths collapse to their trailing component so full home
    paths never reach the filed markdown; URLs collapse to host
    (with title when present); anything else reads as seen text.
    Returns ``(label, kind)`` with kind ``file``/``url``/``other``.
    """
    if not isinstance(item, dict):
        return ("", "other")
    resource = item.get("resource")
    if not isinstance(resource, dict):
        resource = item
    try:
        file = resource.get("file")
        if isinstance(file, str) and file.strip():
            base = file.strip().replace("\\", "/").split("/")[-1].strip()
            base = "".join(" " if ord(c) < 32 or ord(c) == 127 else c
                           for c in base)
            base = " ".join(base.split())[:120]
            if base:
                return (base, "file")
        url = resource.get("url")
        if isinstance(url, str) and url.strip():
            text = url.strip()
            host = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", text)
            host = host.split("/")[0].split("?")[0].strip()[:120]
            title = resource.get("title")
            if isinstance(title, str) and title.strip():
                short = " ".join(title.strip().split())[:80]
                if short and host:
                    return (f"{short} ({host})", "url")
            if host:
                return (host, "url")
        for key in ("page", "title"):
            value = resource.get(key)
            if isinstance(value, str) and value.strip():
                cleaned = " ".join(value.strip().split())[:120]
                cleaned = "".join(" " if ord(c) < 32 or ord(c) == 127
                                  else c for c in cleaned)
                if cleaned:
                    return (cleaned, "other")
    except Exception:
        return ("", "other")
    return ("", "other")


_COMMIT_LINE_RE = re.compile(r"^[0-9a-f]{12}(?:\s|$)", re.MULTILINE)


def _commit_count(changes: object) -> int:
    """Honest commit count from change evidence (bounded, no rescan)."""
    if not isinstance(changes, dict):
        return 0
    base = changes.get("baseline_commit")
    late = changes.get("latest_commit")
    ranged = (isinstance(base, str) and isinstance(late, str) and bool(base)
              and bool(late) and base != late)
    if not ranged:
        return 0
    evidence = changes.get("evidence")
    if isinstance(evidence, str) and evidence:
        try:
            found = len(_COMMIT_LINE_RE.findall(evidence[:65536]))
        except Exception:
            found = 0
        if found > 0:
            return min(found, 9999)
    return 1


def _context_strip(resources: object, changes: object) -> dict:
    """One collapsed context strip from already-fetched evidence.

    Returns ``{"summary": "3 commits · 7 files", "detail": [...]}``:
    the summary is the only line filed as prose; the bounded detail
    (commit range plus up to six resource labels) rides behind it as
    nested bullets the reader can expand. No prose ``Changes``/``Files``
    sections — the user explicitly does not want to read open-file
    lists. Reads only the evidence the caller already holds; never
    shells out, never rescans.
    """
    labels: list[str] = []
    seen: set[str] = set()
    if isinstance(resources, list):
        for item in resources[:20]:
            label, _kind = _resource_label(item)
            if not label or label in seen:
                continue
            seen.add(label)
            labels.append(label)
            if len(labels) >= 6:
                break
    commits = _commit_count(changes)
    files = len(labels)
    summary = (f"{commits} commit{'s' if commits != 1 else ''} · "
               f"{files} file{'s' if files != 1 else ''}")
    detail: list[str] = []
    if isinstance(changes, dict):
        summary_key = changes.get("summary_key")
        if isinstance(summary_key, str) and summary_key.strip():
            detail.append(_clip_item(f"Range {summary_key.strip()[:64]}."))
    for label in labels:
        detail.append(_clip_item(label))
    if not detail:
        detail = ["No files observed; no new commits."]
    return {"summary": summary, "detail": detail[:8]}


def _todos_unlinked_reason(todos_result: dict) -> str | None:
    """Explicit reason when the project has no linked note, else None."""
    if not isinstance(todos_result, dict):
        return "project TODOs are unavailable"
    if todos_result.get("has_logseq_linkage") is False:
        reason = todos_result.get("reason")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()[:MAX_ITEM_CHARS]
        return "project has no linked note"
    return None


def _open_todos(todos_result: dict) -> list[dict]:
    return [t for t in _todo_list(todos_result)
            if not t.get("done")][:MAX_SECTION_ITEMS]


def _section_open_todos(todos_result: dict) -> list[str]:
    reason = _todos_unlinked_reason(todos_result)
    if reason is not None:
        return [_clip_item(f"TODOs unavailable: {reason}")]
    open_items = _open_todos(todos_result)
    if not open_items:
        return ["No open TODOs on the project page."]
    items = []
    for item in open_items:
        line = item.get("line")
        task = item.get("task", "")
        task = task.strip() if isinstance(task, str) else ""
        if not task:
            continue
        prefix = f"#{line} " if isinstance(line, int) and line > 0 else ""
        items.append(_clip_item(f"{prefix}{task}"))
    return items[:MAX_SECTION_ITEMS] or ["No open TODOs on the project page."]


def _section_decisions() -> list[str]:
    return ["No decisions recorded for this session."]


def _section_next(todos_result: dict) -> list[str]:
    reason = _todos_unlinked_reason(todos_result)
    if reason is not None:
        return [_clip_item("Decide the next step from the session files.")]
    open_items = _open_todos(todos_result)
    items = []
    for item in open_items[:3]:
        task = item.get("task", "")
        task = task.strip() if isinstance(task, str) else ""
        if task:
            items.append(_clip_item(f"Continue: {task}"))
    if not items:
        items = ["Review the session and decide the next step."]
    return items[:MAX_SECTION_ITEMS]


_TRUNCATION_SUFFIX = "\n… [truncated]"


def _truncate_utf8(text: str, limit: int) -> str:
    """Clip text to at most ``limit`` UTF-8 bytes, marker included."""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    suffix = _TRUNCATION_SUFFIX.encode("utf-8")
    clipped = raw[:max(0, limit - len(suffix))].decode(
        "utf-8", errors="ignore").rstrip()
    return clipped + _TRUNCATION_SUFFIX


def _build_markdown(project_name: str, session: dict,
                    sections: list[tuple[str, str, list[str]]],
                    context: dict | None = None) -> str:
    lines = ["# Session log"]
    start, end = _session_bounds(session)
    duration = _format_duration(max(0, end - start))
    sid = _session_sid(session) or "none"
    lines.append(f"Project: {project_name or 'unknown'}")
    lines.append(f"Session: {sid[:8]} ({duration})")
    for _sid, title, items in sections:
        lines.append(f"## {title}")
        if items:
            for item in items:
                lines.append(f"- {item}")
        else:
            lines.append("- (none)")
    strip = context if isinstance(context, dict) else {}
    summary = strip.get("summary")
    if isinstance(summary, str) and summary.strip():
        lines.append(f"- Context: {summary.strip()[:120]}")
        detail = strip.get("detail")
        if isinstance(detail, list):
            for entry in detail[:8]:
                if isinstance(entry, str) and entry.strip():
                    lines.append(f"  - {entry.strip()[:240]}")
    return _truncate_utf8("\n".join(lines) + "\n", MAX_MARKDOWN_CHARS)


# ---------------------------------------------------------------------------
# Draft generation + cache
# ---------------------------------------------------------------------------

def _draft_id_for(project_id: str, session_id: str | None,
                  digest: str) -> str:
    seed = f"{project_id}\0{session_id or ''}\0{digest}"
    return "dwl_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _merge_cached(payload: dict, row: dict, settings: dict) -> dict:
    _ = settings
    merged = dict(payload)
    merged["cached"] = True
    merged["saved"] = {
        "journal_ms": row.get("saved_journal_ms"),
        "page_ms": row.get("saved_page_ms"),
    }
    return merged


def _newest_closed_session(view, allow_default: bool,
                           project_id: str) -> dict | None:
    """Newest closed session from a bounded work-session listing."""
    fn = getattr(view, "work_sessions", None)
    if not callable(fn):
        if not allow_default:
            raise EvidenceUnavailable("evidence unavailable")
        fn = DefaultEvidence().work_sessions
    try:
        result = fn(project_id, 20)
    except EvidenceUnavailable:
        raise
    except Exception as exc:
        raise EvidenceUnavailable("evidence unavailable") from exc
    items = result.get("sessions", []) \
        if isinstance(result, dict) else []
    best: dict | None = None
    best_key: tuple[int, int] | None = None
    for item in items:
        row = _normalize_session_row(item)
        if row is None or not _is_closed(row):
            continue
        start, end = _session_bounds(row)
        key = (end, start)
        if best is None or (best_key is not None and key > best_key):
            best, best_key = row, key
    return best


def generate_draft(project_id, session_id=None, *, conn, now_ms,
                   evidence=None, settings=None, refresh=False,
                   allow_default=False) -> dict:
    """Build (or fetch the cached) deterministic draft for a session."""
    canonical_project = _validate_project_id(project_id)
    canonical_session = _validate_session_opt(session_id)
    stamp = _validate_ms(now_ms)
    settings = settings if isinstance(settings, dict) else {}
    view = _as_evidence(evidence, allow_default)
    try:
        if canonical_session is None:
            found = _safe_call(view, "last_session", canonical_project)
            row = _normalize_session_row(found)
            if row is None or not _is_closed(row):
                row = _newest_closed_session(view, allow_default,
                                             canonical_project)
                if row is None:
                    raise WorkLogError("no closed session for project")
        else:
            found = _safe_call(view, "get_session", canonical_session)
            row = _normalize_session_row(found)
            if row is None:
                raise EvidenceUnavailable("evidence unavailable")
        resources_result = _safe_call(
            view, "session_resources", _session_sid(row) or canonical_session
            or "", 20)
        resources = _resource_list(resources_result)
        changes_result = _safe_call(
            view, "session_changes", canonical_project,
            _session_sid(row) or canonical_session or "")
        if not isinstance(changes_result, dict):
            changes_result = {"available": False,
                              "reason": "change evidence unavailable"}
        todos_result = _safe_call(view, "project_todos", canonical_project)
        if not isinstance(todos_result, dict):
            todos_result = {}
    except EvidenceUnavailable:
        raise
    except WorkLogError:
        raise
    except Exception as exc:
        raise EvidenceUnavailable("evidence unavailable") from exc
    sid = _session_sid(row)
    inputs = _evidence_inputs(row, resources, changes_result, todos_result,
                              None)
    digest = _evidence_digest(inputs)
    if conn is not None and not refresh:
        try:
            hit = _annotations.find_draft(conn, canonical_project, sid,
                                          digest)
        except Exception:
            hit = None
        if isinstance(hit, dict) and isinstance(hit.get("draft"), dict):
            return _merge_cached(hit["draft"], hit, settings)
    start, end = _session_bounds(row)
    project_name = _registry_display_name(canonical_project)
    resolved_source = "explicit"
    sections = [
        ("what_happened", "What happened",
         _section_what_happened(row, resources)),
        ("open_todos", "Open TODOs", _section_open_todos(todos_result)),
        ("decisions", "Decisions", _section_decisions()),
        ("next", "Next", _section_next(todos_result)),
    ]
    section_objs = [{"id": ident, "title": title, "items": items}
                    for ident, title, items in sections]
    context = _context_strip(resources, changes_result)
    markdown = _build_markdown(project_name, row, sections, context)
    summary_key = changes_result.get("summary_key", "")
    if not isinstance(summary_key, str) or not summary_key:
        summary_key = "unavailable"
    draft_id = _draft_id_for(canonical_project, sid, digest)
    payload: dict = {
        "draft_id": draft_id,
        "project": canonical_project,
        "project_source": resolved_source,
        "project_name": project_name,
        "session": sid,
        "session_start_ms": start,
        "session_end_ms": end,
        "sections": section_objs,
        "context": context,
        "markdown": markdown,
        "properties": {DRAFT_PROPERTY: f"{canonical_project}:{sid or 'null'}"},
        "labels": None,
        "evidence_digest": digest,
        "summary_key": summary_key[:256],
        "suggested": False,
        "created_ms": stamp,
        "cached": False,
        "saved": {"journal_ms": None, "page_ms": None},
    }
    if conn is not None:
        try:
            _annotations.save_draft(
                conn, draft_id=draft_id, project_id=canonical_project,
                session_id=sid, summary_key=payload["summary_key"],
                evidence_digest=digest, payload=payload, created_ms=stamp)
        except Exception:
            pass
        # Re-read the stored row so the returned draft reflects the
        # persisted save markers.
        try:
            stored = _annotations.get_draft(conn, draft_id)
        except Exception:
            stored = None
        if isinstance(stored, dict) \
                and isinstance(stored.get("draft"), dict):
            payload["saved"] = {
                "journal_ms": stored.get("saved_journal_ms"),
                "page_ms": stored.get("saved_page_ms"),
            }
    return payload


def draft_for_session(session, *, conn, settings, now_ms, evidence=None,
                      refresh=False, allow_default=False) -> dict:
    """Memory-tick adapter shape (never raises for domain misses).

    ``jev_calls`` is always 0 since the Jev path was deleted; the key is
    kept so the draft payload shape stays stable for the scheduler.
    """
    try:
        settings = settings if isinstance(settings, dict) else {}
        stamp = _validate_ms(now_ms)
        if not isinstance(session, dict):
            raise WorkLogError("session is invalid")
        resolved = resolve_session_project(
            session, conn=conn, settings=settings, evidence=evidence,
            now_ms=stamp)
        if resolved is None:
            return {"status": "skipped", "reason": "no_project",
                    "changed": False, "jev_calls": 0}
        project_id = resolved["project_id"]
        try:
            project_id = str(uuid.UUID(str(project_id)))
        except (ValueError, AttributeError, TypeError):
            return {"status": "skipped", "reason": "no_project",
                    "changed": False, "jev_calls": 0}
        sid = _session_sid(session)
        draft = generate_draft(project_id, sid, conn=conn, now_ms=stamp,
                               evidence=evidence, settings=settings,
                               refresh=refresh, allow_default=allow_default)
        if not isinstance(draft, dict):
            raise WorkLogError("draft generation failed")
        if isinstance(resolved.get("source"), str) \
                and resolved["source"] != "explicit":
            draft["project_source"] = resolved["source"]
        draft["suggested"] = bool(resolved.get("suggested"))
        return {"status": "ok", "reason": "",
                "changed": not draft.get("cached", False),
                "jev_calls": 0, "draft_id": draft.get("draft_id"),
                "project": project_id, "cached": bool(draft.get("cached"))}
    except EvidenceUnavailable:
        return {"status": "deferred", "reason": "evidence_unavailable",
                "changed": False, "jev_calls": 0}
    except WorkLogError as exc:
        message = str(exc)
        if "no closed session" in message or "evidence unavailable" in message:
            return {"status": "deferred", "reason": "evidence_unavailable",
                    "changed": False, "jev_calls": 0}
        return {"status": "deferred", "reason": "failed",
                "changed": False, "jev_calls": 0}
    except Exception:
        return {"status": "deferred", "reason": "failed",
                "changed": False, "jev_calls": 0}


# ---------------------------------------------------------------------------
# Continuation (read-only cached enrichment for planner Ask prefill)
# ---------------------------------------------------------------------------

def _sanitize_fragment(text: object, limit: int) -> str:
    raw = str(text or "")
    if len(raw) > _SANITIZE_INPUT_CAP:
        # Bound intermediate normalization allocations; the output clip
        # below is far smaller, so results are unchanged.
        raw = raw[:_SANITIZE_INPUT_CAP]
    collapsed = " ".join(raw.replace("\r", " ").replace("\n", " ").split())
    collapsed = "".join(" " if ord(c) < 32 or ord(c) == 127 else c
                         for c in collapsed)
    collapsed = " ".join(collapsed.split()).strip()
    if len(collapsed) > limit:
        collapsed = collapsed[:max(0, limit - 1)].rstrip() + "…"
    return collapsed


def _summarize_cached_sections(payload: dict) -> str:
    """Deterministic sanitized summary from cached sections.

    Only the frozen deterministic ``sections`` list is read; the full
    ``markdown`` blob is never used here.
    """
    if not isinstance(payload, dict):
        return ""
    sections = payload.get("sections")
    if not isinstance(sections, list):
        return ""
    by_id: dict[str, tuple[str, list]] = {}
    for entry in sections[:_CONTINUATION_SECTION_SCAN_CAP]:
        if not isinstance(entry, dict):
            continue
        ident = entry.get("id")
        title = entry.get("title")
        items = entry.get("items")
        if not isinstance(ident, str) or not isinstance(title, str) \
                or not isinstance(items, list):
            continue
        by_id[ident] = (title.strip()[:64] or ident, items)
    lines = []
    for ident, limit in _CONTINUATION_SECTIONS:
        found = by_id.get(ident)
        if found is None:
            continue
        title, items = found
        cleaned = []
        for item in items[:limit]:
            if not isinstance(item, str) or not item.strip():
                continue
            fragment = _sanitize_fragment(item, MAX_ITEM_CHARS)
            if fragment:
                cleaned.append(fragment)
        if not cleaned:
            continue
        lines.append(_sanitize_fragment(
            f"{title}: {'; '.join(cleaned)}", MAX_ITEM_CHARS * 2))
    return "\n".join(lines)


def _latest_cached_draft(conn, project_id: str) -> dict | None:
    """Newest parseable cached draft row for a project, or None.

    Read-only: a single bounded SELECT fetching only the columns the
    lookup needs. Rows whose stored JSON exceeds the established draft
    payload cap are excluded in SQL before fetching (``length`` over a
    ``BLOB`` cast counts bytes past any embedded NUL, which plain
    ``length(TEXT)`` would stop at; bytes are re-checked in Python
    before parsing), so one oversized row can neither exhaust memory
    nor poison the lookup. Malformed JSON rows are skipped
    deterministically (newest first). Never generates a draft and never
    touches the activity store.
    """
    try:
        rows = conn.execute(
            "SELECT project_id,"
            " CASE WHEN typeof(session_id) = 'text'"
            " AND length(CAST(session_id AS BLOB)) = 32"
            " THEN session_id ELSE NULL END AS session_id,"
            " json FROM drafts"
            " WHERE project_id = ? AND length(CAST(json AS BLOB)) <= ?"
            " ORDER BY created_ms DESC, draft_id ASC LIMIT ?",
            (project_id, _PAYLOAD_BYTES,
             CONTINUATION_PROBE_LIMIT)).fetchall()
    except Exception:
        return None
    for row in rows:
        try:
            raw = row["json"]
        except (TypeError, KeyError, IndexError):
            continue
        if not isinstance(raw, str):
            continue
        try:
            if len(raw.encode("utf-8")) > _PAYLOAD_BYTES:
                continue
        except (UnicodeEncodeError, MemoryError):
            continue
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError, RecursionError):
            continue
        if not isinstance(payload, dict):
            continue
        if not isinstance(payload.get("sections"), list):
            continue
        try:
            project_raw = row["project_id"]
            session_raw = row["session_id"]
        except (TypeError, KeyError, IndexError):
            continue
        if not isinstance(project_raw, str) \
                or project_raw.strip().lower() != project_id.lower():
            continue
        return {
            "project_id": project_raw,
            "session_id": session_raw,
            "draft": payload,
        }
    return None


def _check_readonly_parent(path) -> bool:
    """Noncreating/nonmutating parent-chain safety check; True when safe.

    Read-only mirror of the project_sessions private-dir policy, without
    its create/chmod side effects (never call ``_ensure_private_dir``
    here): every existing path component must be a non-symlink directory
    (lstat only, never following), and the immediate parent must exist
    with private permissions and current-user ownership. Anything else
    is unsafe. Callers fail soft (read as absent); nothing is repaired,
    chmod'ed, or created.
    """
    try:
        parent = path.parent
        current = Path(parent.anchor)
        for part in parent.parts[1:]:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                return False
            except OSError:
                return False
            if stat.S_ISLNK(info.st_mode):
                return False
            if not stat.S_ISDIR(info.st_mode):
                return False
            if current == parent:
                if stat.S_IMODE(info.st_mode) & 0o077 != 0:
                    return False
                try:
                    _annotations._check_owner(info)
                except Exception:
                    return False
    except Exception:
        return False
    return True


def _open_readonly_conn(db):
    """Open the sidecar read-only without creating it; None when absent.

    Mirrors the read-only safety contract (no schema init, no directory
    creation, no permission repair): a missing DB, an unsafe parent
    chain (symlink component, non-private or foreign-owned immediate
    parent), or an unsafe file/sidecar reads as absent rather than
    created or fixed.
    """
    try:
        path = _annotations.resolve_db_path(db)
    except Exception:
        return None
    if not _check_readonly_parent(path):
        return None
    try:
        exists = path.exists()
    except OSError:
        return None
    if not exists:
        return None
    try:
        _annotations._reject_file_safety(path)
        _annotations._check_sidecars(path)
    except Exception:
        return None
    try:
        conn = sqlite3.connect(
            path.as_uri() + "?mode=ro", uri=True,
            timeout=_annotations.BUSY_TIMEOUT_MS / 1000.0)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1").fetchone()
        return conn
    except Exception:
        try:
            conn.close()  # type: ignore[possibly-used-before-assignment]
        except Exception:
            pass
        return None


def continuation_text(project_id, *, conn, settings) -> dict:
    """Read-only cached continuation enrichment for one project.

    Returns ``{"project": canonical UUID, "available": bool,
    "text": str}`` with ``text`` bounded to ``CONTINUATION_TEXT_LIMIT``
    chars. ``available`` is False (with empty text) for every
    missing/disabled state: master ``enabled`` off, ``workLog`` off,
    no DB, no cached draft, malformed rows, or no usable summary.
    Only an invalid project UUID raises :class:`WorkLogError`. Never
    generates drafts, never touches the network or the activity store.
    """
    canonical = _validate_project_id(project_id)
    settings = settings if isinstance(settings, dict) else {}
    enabled = settings.get("enabled", True)
    if enabled is not True:
        return {"project": canonical, "available": False, "text": ""}
    if settings.get("workLog", True) is not True:
        return {"project": canonical, "available": False, "text": ""}
    if conn is None:
        return {"project": canonical, "available": False, "text": ""}
    latest = _latest_cached_draft(conn, canonical)
    if latest is None:
        return {"project": canonical, "available": False, "text": ""}
    payload = latest.get("draft")
    summary = _summarize_cached_sections(
        payload if isinstance(payload, dict) else {})
    text = summary.strip() if isinstance(summary, str) else ""
    text = _sanitize_fragment(text, CONTINUATION_TEXT_LIMIT)
    # _sanitize_fragment clips with an ellipsis at the char limit; an
    # empty/whitespace result reads as unavailable.
    if not text:
        return {"project": canonical, "available": False, "text": ""}
    if len(text) > CONTINUATION_TEXT_LIMIT:
        text = text[:CONTINUATION_TEXT_LIMIT - 1].rstrip() + "…"
    return {"project": canonical, "available": True, "text": text}


# ---------------------------------------------------------------------------
# Export: prepare (exact preview) + apply (revision-checked write)
# ---------------------------------------------------------------------------

def _marker(project_id: str, session_id: str | None) -> str:
    return f"quickshell-worklog:: {project_id}:{session_id or 'null'}"


def _resolve_graph(graph):
    if graph is not None and str(graph).strip():
        try:
            from logseq_common import graph_path
            return graph_path(str(graph).strip())
        except Exception as exc:
            raise WorkLogError("logseq graph is unavailable") from exc
    try:
        from logseq_common import resolve_graph
        return resolve_graph(None)
    except Exception as exc:
        raise WorkLogError("logseq graph is unavailable") from exc


def _new_token() -> str:
    return "wlp_" + secrets.token_hex(16)


def _store_prepared(conn, *, kind: str, payload: dict, preview: dict,
                    revision, expires_ms: int) -> str:
    """Persist a prepared export, retrying on token collision."""
    for _ in range(3):
        token = _new_token()
        try:
            _annotations.create_prepared(
                conn, token=token, kind=kind, payload=payload,
                preview=preview, revision=revision, expires_ms=expires_ms)
        except _annotations.AnnotationsError as exc:
            if "already exists" in str(exc):
                continue
            raise WorkLogError(str(exc)) from exc
        return token
    _error("prepare failed")


def _export_markdown(payload_draft: dict) -> str:
    """Text to export: the deterministic draft markdown."""
    body = payload_draft.get("markdown", "")
    return body if isinstance(body, str) else ""


_PREVIEW_TEXT_RE = re.compile(r"[\x00-\x1f\x7f]")
_PREVIEW_TEXT_LIMIT = 200


def _preview_text(value: object) -> str:
    """Bounded preview-only text for graph/page names (never a write path).

    Control chars (including NUL) are stripped and output is capped at
    200 chars. Returns "" when unavailable so callers can omit the field.
    """
    if not isinstance(value, str):
        return ""
    cleaned = _PREVIEW_TEXT_RE.sub("", value).strip()
    if not cleaned:
        return ""
    return cleaned[:_PREVIEW_TEXT_LIMIT]


def _page_block(markdown: object, project_id: str,
                session_id: str | None) -> str:
    # Restored: still required by _prepare_page (the page target
    # survives L1; only the journal target was deleted).
    body = markdown if isinstance(markdown, str) else ""
    head = f"- Session log {(session_id or 'none')[:8]}"
    lines = [head, f"  {_marker(project_id, session_id)}"]
    for line in body.split("\n"):
        lines.append(f"  {line}" if line.strip() else "")
    return "\n".join(lines)


def _prepare_page(conn, row: dict, graph, now_ms: int) -> dict:
    import project_planner as _pp
    from logseq_common import append_session_log
    project_id = row["project_id"]
    session_id = row["session_id"]
    payload_draft = row["draft"]
    marker = _marker(project_id, session_id)
    try:
        page = _pp.read_page(graph, {"project_id": project_id})
    except Exception as exc:
        raise WorkLogError("project page is unavailable") from exc
    if not isinstance(page, dict):
        raise WorkLogError("project page is unavailable")
    content = page.get("content", "")
    if not isinstance(content, str):
        raise WorkLogError("project page is unavailable")
    if marker in content:
        _error("draft already saved to project page")
    block = _page_block(_export_markdown(payload_draft), project_id,
                        session_id)
    new_content, inserted = append_session_log(content, block)
    if len(new_content.encode("utf-8")) > _PAGE_CONTENT_LIMIT:
        _error("project page would be too large")
    payload = {"draft_id": row["draft_id"], "target": "page",
               "path": page.get("path"), "revision": page.get("revision"),
               "content": new_content}
    preview = {"target": "page", "path": page.get("path"),
               "revision": page.get("revision"), "block": inserted,
               "content": new_content}
    # Read-only open hint (preview only; the stored payload above is
    # unchanged). Both fields already exist on project_planner.read_page;
    # they are re-bounded here (200 chars, no NUL/control chars) and
    # omitted when unavailable.
    page_title = _preview_text(page.get("page"))
    if page_title:
        preview["page"] = page_title
    page_graph = _preview_text(page.get("graphName"))
    if page_graph:
        preview["graphName"] = page_graph
    token = _store_prepared(conn, kind="page", payload=payload,
                            preview=preview,
                            revision=page.get("revision"),
                            expires_ms=now_ms + PREPARED_TTL_MS)
    return {"prepared": token, "target": "page", "preview": preview,
            "revision": page.get("revision"),
            "expires_in": PREPARED_EXPIRES_IN_S}


def prepare_export(conn, draft_id, target, *, graph=None, now_ms) -> dict:
    """Stage an exact-preview export; refuses already-saved drafts.

    Single save target: the project page (L1). The journal target is
    deleted; journal writing survives through the Journal tab only.
    """
    if not isinstance(target, str) or target.strip() != "page":
        _error("target must be page")
    stamp = _validate_ms(now_ms)
    try:
        row = _annotations.get_draft(conn, draft_id)
    except _annotations.AnnotationsError as exc:
        raise WorkLogError(str(exc)) from exc
    if row is None or not isinstance(row.get("draft"), dict):
        _error("unknown draft")
    assert row is not None
    if row.get("saved_page_ms") is not None:
        _error("draft already saved to project page")
    resolved_graph = _resolve_graph(graph)
    return _prepare_page(conn, row, resolved_graph, stamp)


def _apply_payload(payload: object) -> dict:
    if not isinstance(payload, dict):
        _error("prepared export is invalid")
    return payload


def apply_export(conn, token, *, graph=None, now_ms) -> dict:
    """Consume a prepared token (at-most-once) and write through helpers."""
    stamp = _validate_ms(now_ms)
    try:
        row = _annotations.consume_prepared(conn, token, now_ms=stamp)
    except _annotations.AnnotationsError as exc:
        raise WorkLogError(str(exc)) from exc
    if row is None:
        _error("prepared export is unknown or expired")
    assert row is not None
    kind = row.get("kind")
    stored_revision = row.get("revision")
    payload = _apply_payload(row.get("payload"))
    draft_id = payload.get("draft_id")
    if not isinstance(draft_id, str) or not draft_id:
        _error("prepared export is invalid")
    resolved_graph = _resolve_graph(graph)
    if kind == "page":
        path = payload.get("path")
        content = payload.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            _error("prepared export is invalid")
        try:
            import project_planner as _pp
            result = _pp.update_page(resolved_graph, path, stored_revision,
                                     content)
        except Exception as exc:
            raise WorkLogError("project page write failed") from exc
        if not isinstance(result, dict):
            raise WorkLogError("project page write failed")
        try:
            _annotations.mark_draft_saved(conn, draft_id, "page",
                                          now_ms=stamp)
        except Exception:
            pass
        return {"applied": True, "target": "page",
                "path": result.get("path"),
                "revision": result.get("revision"), "draft_id": draft_id,
                "saved_ms": stamp}
    _error("prepared export is invalid")
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# CLI (shared plumbing lives in qscli.py; argv stays byte-identical)
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = qscli.SafeParser(description=__doc__)
    qscli.add_global_flags(parser, db=True, graph=True)
    sub = parser.add_subparsers(dest="command", required=True,
                                parser_class=qscli.SafeParser)
    p_draft = sub.add_parser("draft")
    p_draft.add_argument("--project", default=None)
    p_draft.add_argument("--session", default=None)
    p_draft.add_argument("--draft-id", default=None)
    p_draft.add_argument("--refresh", action="store_true")
    p_prepare = sub.add_parser("prepare")
    p_prepare.add_argument("--draft-id", required=True)
    p_prepare.add_argument("--target", required=True,
                           choices=("page",))
    p_apply = sub.add_parser("apply")
    p_apply.add_argument("--prepared", required=True)
    p_cont = sub.add_parser("continuation")
    p_cont.add_argument("--project", default=None)
    return parser.parse_args(argv if argv is not None else sys.argv[1:])


def _load_settings() -> dict:
    try:
        import quickshell_settings as _settings
        return _settings.memory_settings()
    except Exception:
        return {}


def _do_draft(args, conn, settings: dict, now_ms: int) -> dict:
    if args.draft_id is not None and str(args.draft_id).strip():
        if (args.project is not None and str(args.project).strip()) \
                or (args.session is not None and str(args.session).strip()):
            _error("draft takes either --project or --draft-id, not both")
        try:
            row = _annotations.get_draft(conn, args.draft_id)
        except _annotations.AnnotationsError as exc:
            raise WorkLogError(str(exc)) from exc
        if row is None or not isinstance(row.get("draft"), dict):
            _error("unknown draft")
        assert isinstance(row.get("draft"), dict)
        return _merge_cached(row["draft"], row, settings)
    if args.project is None or not str(args.project).strip():
        _error("draft requires --project UUID or --draft-id ID")
    return generate_draft(args.project, args.session, conn=conn,
                          now_ms=now_ms, settings=settings,
                          refresh=bool(args.refresh))


def _do_continuation(args, settings: dict) -> dict:
    if args.project is None or not str(args.project).strip():
        _error("continuation requires --project UUID")
    conn = _open_readonly_conn(args.db)
    try:
        return continuation_text(args.project, conn=conn, settings=settings)
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _dispatch(args: argparse.Namespace) -> dict:
    if getattr(args, "command", None) == "continuation":
        settings = _load_settings()
        return _do_continuation(args, settings)
    conn = _annotations.connect(args.db)
    try:
        settings = _load_settings()
        now_ms = int(time.time() * 1000)
        if args.command == "draft":
            return _do_draft(args, conn, settings, now_ms)
        elif args.command == "prepare":
            return prepare_export(conn, args.draft_id, args.target,
                                  graph=args.graph, now_ms=now_ms)
        elif args.command == "apply":
            return apply_export(conn, args.prepared, graph=args.graph,
                                now_ms=now_ms)
        else:
            _error("unknown command")
    finally:
        try:
            conn.close()
        except Exception:
            pass


_BOUNDED_EXCEPTIONS = (WorkLogError, _annotations.AnnotationsError, OSError,
                        TypeError, ValueError, UnicodeError, RecursionError,
                        OverflowError)


def main(argv: list[str] | None = None) -> int:
    return qscli.run_main(_parse_args, _dispatch, "work log",
                          _BOUNDED_EXCEPTIONS, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
