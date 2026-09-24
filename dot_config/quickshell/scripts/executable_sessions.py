#!/usr/bin/env python3
"""Sessions: read-only joins over collector sessions plus user metadata.

Every closed work session is a grouping key plus a context hook: immutable
collector facts (via ``desktop_projects`` read commands) joined with
optional user-owned ``annotations.db`` metadata (``session_meta``,
``session_links``, draft stubs). Read commands never create or write the
sidecar; write commands never touch the collector.

A session is NOT a managed object: no states, no units, no keep/split,
no rollups, no focus blocks, no auto-ignore. ``inbox`` is a filter over
the same list — sessions with pending content (an unaccepted capture or
an unsaved thought draft) that are not yet attended — not a separate
view.

Conventions match the other helpers: JSON stdout, single-line
``error: ...`` on stderr with exit 1, no tracebacks, no state content in
diagnostics, list-form argv with ``shell=False``, bounded IO.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import NoReturn

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import annotations as _annotations
import daily_review as _review
import qscli

try:
    import desktop_projects as _desktop_projects
except ImportError:
    _desktop_projects = None  # type: ignore

try:
    import projects as _projects
except ImportError:
    _projects = None  # type: ignore


class LedgerError(ValueError):
    pass


def _error(message: str) -> NoReturn:
    raise LedgerError(message)


DEFAULT_LIMIT = 20
INBOX_DEFAULT_LIMIT = 50
INBOX_MAX_LIMIT = 999
MAX_LIMIT = 1000
# Transport cap; single source of truth is qscli.INPUT_LIMIT (same value).
INPUT_LIMIT = 1024 * 1024
I64_MAX = 9223372036854775807

LINK_KINDS = ("capture", "draft", "agent_session", "continued_from")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

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


def _validate_session_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("session id is invalid")
    text = value.strip()
    if len(text) != 32 or any(c not in "0123456789abcdefABCDEF" for c in text):
        _error("session id is invalid")
    return text.lower()


def _validate_project_id_opt(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if not isinstance(value, str):
        _error("project id is invalid")
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        _error("project id is invalid")
    raise AssertionError("unreachable")


def _validate_limit(value: object, default: int = DEFAULT_LIMIT) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        _error("limit is invalid")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = int(value.strip(), 10)
        except ValueError:
            _error("limit is invalid")
    else:
        _error("limit is invalid")
    if parsed < 1 or parsed > MAX_LIMIT:
        _error("limit is invalid")
    return parsed


def _validate_range(from_ms: object, to_ms: object) -> tuple[int | None, int | None]:
    from_present = from_ms is not None
    to_present = to_ms is not None
    if not from_present and not to_present:
        return (None, None)
    if from_present != to_present:
        _error("--from and --to must be given together")
    parsed_from = _validate_ms(from_ms)
    parsed_to = _validate_ms(to_ms)
    if parsed_from > parsed_to:
        _error("range is invalid")
    return (parsed_from, parsed_to)


def _validate_search_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("query is invalid")
    text = value.strip()
    if "\x00" in text:
        _error("query is invalid")
    if len(text) < 1 or len(text) > 256:
        _error("query is invalid")
    return text


def _validate_link_kind(value: object) -> str:
    if not isinstance(value, str) or value.strip() not in LINK_KINDS:
        _error("link kind is invalid")
    return value.strip()


def _now_ms(now_ms: object) -> int:
    if now_ms is None:
        return int(time.time() * 1000)
    return _validate_ms(now_ms)


# ---------------------------------------------------------------------------
# Collector + sidecar plumbing
# ---------------------------------------------------------------------------

def _collector(collector):
    if collector is not None:
        return collector
    if _desktop_projects is not None:
        return _desktop_projects
    _error("collector unavailable")
    raise AssertionError("unreachable")


def _local_device_id(collector, desktop_bin) -> str | None:
    try:
        result = collector.device_id(desktop_bin=desktop_bin, db=None)
    except Exception as exc:
        raise LedgerError("collector unavailable") from exc
    if not isinstance(result, dict):
        raise LedgerError("collector unavailable")
    raw = result.get("device_id")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if len(text) != 32 or any(c not in "0123456789abcdefABCDEF" for c in text):
        return None
    return text.lower()


def _has_table(conn, name: str) -> bool:
    if conn is None:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,)).fetchone()
    except sqlite3.Error as exc:
        raise LedgerError("database is unavailable") from exc
    return row is not None


def _registry_names() -> dict:
    try:
        if _projects is None:
            return {}
        data = _projects.list_projects()
    except Exception:
        return {}
    entries = data.get("projects", []) if isinstance(data, dict) else []
    out: dict = {}
    if not isinstance(entries, list) or len(entries) > 10000:
        return {}
    for entry in entries[:10000]:
        if not isinstance(entry, dict):
            continue
        pid = entry.get("id")
        name = entry.get("name")
        if isinstance(pid, str) and isinstance(name, str) and pid.strip():
            out[pid.strip()] = name.strip()[:256]
    return out


def _session_name(session: dict | None) -> str:
    if not isinstance(session, dict):
        return ""
    project = session.get("project")
    if isinstance(project, dict):
        name = project.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()[:256]
    return ""


def _resolve_project(session: dict | None, meta: dict | None,
                     names: dict) -> dict | None:
    """Resolve the session's project from collector attribution only.

    No inference, no suggestion lookup, no overrides: ``meta`` is
    accepted (callers already hold it) but never read for the project.
    """
    _ = meta
    pid_raw: str | None = None
    if isinstance(session, dict):
        project = session.get("project")
        if isinstance(project, dict):
            candidate = project.get("id")
            if isinstance(candidate, str) and candidate.strip():
                pid_raw = candidate.strip()
        if pid_raw is None:
            candidate = session.get("project_id")
            if isinstance(candidate, str) and candidate.strip():
                pid_raw = candidate.strip()
    if pid_raw is not None:
        try:
            pid = str(uuid.UUID(pid_raw))
        except ValueError:
            pid = ""
        if pid:
            return {"id": pid,
                    "name": names.get(pid, _session_name(session))[:256]}
        return None
    return None


def _draft_pending(draft: dict | None) -> bool:
    """True while a draft stub exists with neither save marker stamped."""
    if not isinstance(draft, dict):
        return False
    return draft.get("saved_journal_ms") is None \
        and draft.get("saved_page_ms") is None


def _session_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    if parsed < 0:
        return 0
    return parsed


def _session_view(session: dict, project: dict | None = None) -> dict:
    project_view = None
    if isinstance(project, dict):
        pid = project.get("id")
        pname = project.get("name")
        pid_out = pid if isinstance(pid, str) else None
        name_out = pname.strip()[:256] if isinstance(pname, str) \
            and pname.strip() else ""
        if pid_out is not None or name_out:
            project_view = {"id": pid_out, "name": name_out}
    status = session.get("status")
    eff = session.get("effective_status")
    ended = session.get("ended_reason")
    apps = session.get("applications")
    bounded_apps: list[str] = []
    if isinstance(apps, list):
        for item in apps:
            if isinstance(item, str) and item.strip():
                bounded_apps.append(item.strip()[:64])
                if len(bounded_apps) >= 32:
                    break
    return {
        "session_id": session.get("session_id"),
        "device_id": session.get("device_id"),
        "project": project_view,
        "start_ms": _session_int(session.get("start_ms")),
        "end_ms": _session_int(session.get("end_ms")),
        "event_count": _session_int(session.get("event_count")),
        "status": status if isinstance(status, str) else "",
        "effective_status": eff if isinstance(eff, str) else "",
        "ended_reason": ended if isinstance(ended, str) else None,
        "applications": bounded_apps,
        "active": session.get("active") is True,
        "gap_ms": _session_int(session.get("gap_ms")),
    }


def _sort_key(session: dict) -> tuple:
    end = _session_int(session.get("end_ms"))
    start = _session_int(session.get("start_ms"))
    sid = session.get("session_id")
    return (end, start, sid if isinstance(sid, str) else "")


# ---------------------------------------------------------------------------
# Sidecar read helpers (never create schema; missing tables read as empty)
# ---------------------------------------------------------------------------

def _bulk_meta(conn, ids: list) -> dict:
    if conn is None or not ids:
        return {}
    if not _has_table(conn, "session_meta"):
        return {}
    try:
        return _annotations.list_session_meta(conn, ids)
    except _annotations.AnnotationsError:
        raise
    except Exception as exc:
        raise LedgerError("database is unavailable") from exc


def _meta_for(conn, sid: str) -> dict | None:
    if conn is None:
        return None
    if not _has_table(conn, "session_meta"):
        return None
    try:
        return _annotations.get_session_meta(conn, sid)
    except _annotations.AnnotationsError:
        raise
    except Exception as exc:
        raise LedgerError("database is unavailable") from exc


def _draft_stub(conn, sid: str) -> dict | None:
    if conn is None:
        return None
    if not _has_table(conn, "drafts"):
        return None
    try:
        rows = _annotations.list_drafts_for_session(conn, sid, limit=1)
    except _annotations.AnnotationsError:
        raise
    except Exception as exc:
        raise LedgerError("database is unavailable") from exc
    return rows[0] if rows else None


def _drafts_for(conn, sid: str, limit: int) -> list[dict]:
    if conn is None:
        return []
    if not _has_table(conn, "drafts"):
        return []
    try:
        return _annotations.list_drafts_for_session(conn, sid, limit=limit)
    except _annotations.AnnotationsError:
        raise
    except Exception as exc:
        raise LedgerError("database is unavailable") from exc


def _links_for(conn, sid: str) -> list[dict]:
    if conn is None:
        return []
    if not _has_table(conn, "session_links"):
        return []
    try:
        rows = _annotations.list_session_links(conn, sid)
    except _annotations.AnnotationsError:
        raise
    except Exception as exc:
        raise LedgerError("database is unavailable") from exc
    return [{"kind": r.get("kind"), "target": r.get("target"),
             "created_ms": r.get("created_ms")} for r in rows]


def _captures_new_projects(conn, from_ms: int, to_ms: int) -> set:
    """Project ids with an unaccepted capture in the window (advisory).

    Captures are scanned per project (``session_capture`` files agent
    session files per project, not per collector session), so a session
    needs attention when its project has a ``status='new'`` capture
    created inside the listed window. Missing tables, a missing sidecar,
    or any read failure degrade to "no capture signal" — inbox is
    advisory and never fails for the sidecar (P5 conventions).
    """
    if conn is None:
        return set()
    try:
        if not _has_table(conn, "captures"):
            return set()
    except Exception:
        return set()
    try:
        rows = conn.execute(
            "SELECT DISTINCT project_id FROM captures"
            " WHERE status = 'new' AND created_ms >= ?"
            " AND created_ms < ? LIMIT 10000",
            (from_ms, to_ms)).fetchall()
    except Exception:
        return set()
    out: set = set()
    for row in rows:
        try:
            pid = row["project_id"]
        except (IndexError, KeyError, TypeError):
            continue
        if isinstance(pid, str) and pid.strip():
            out.add(pid.strip())
    return out


def _project_has_new_capture(conn, project_id: str | None) -> bool:
    """True when the project has any unaccepted capture (advisory)."""
    if conn is None or not isinstance(project_id, str) or not project_id:
        return False
    try:
        if not _has_table(conn, "captures"):
            return False
    except Exception:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM captures WHERE status = 'new'"
            " AND project_id = ? LIMIT 1",
            (project_id,)).fetchone()
    except Exception:
        return False
    return row is not None


def _attended_of(meta: dict | None) -> bool:
    if not isinstance(meta, dict):
        return False
    try:
        return int(meta.get("attended", 0)) == 1
    except (TypeError, ValueError):
        return False


def _fetch_session_rows(coll, from_ms: int, to_ms: int,
                        desktop_bin) -> tuple[list, bool]:
    try:
        result = coll.work_sessions(None, MAX_LIMIT, from_ms, to_ms,
                                    None, desktop_bin, None)
    except Exception as exc:
        raise LedgerError("collector unavailable") from exc
    if not isinstance(result, dict) or not isinstance(
            result.get("sessions"), list):
        raise LedgerError("collector unavailable")
    rows = [r for r in result["sessions"] if isinstance(r, dict)]
    return (rows, len(rows) == MAX_LIMIT)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _load_window(conn, coll, *, from_ms, to_ms, desktop_bin,
                 include_foreign) -> dict:
    """Load session entries over the full requested window."""
    rows, hit_limit = _fetch_session_rows(coll, from_ms, to_ms, desktop_bin)
    local = _local_device_id(coll, desktop_bin)
    if not include_foreign and local is None:
        return {"device_id": None, "entries": [],
                "entry_by_sid": {}, "hit_limit": hit_limit}
    if not include_foreign:
        rows = [r for r in rows
                if isinstance(r.get("device_id"), str)
                and r["device_id"].lower() == local]
    rows.sort(key=_sort_key, reverse=True)
    ids: list[str] = []
    for row in rows:
        try:
            ids.append(_validate_session_id(row.get("session_id")))
        except ValueError:
            continue
    metamap = _bulk_meta(conn, ids)
    names = _registry_names()
    capture_projects = _captures_new_projects(conn, from_ms, to_ms)
    entries: list[dict] = []
    for row in rows:
        try:
            sid = _validate_session_id(row.get("session_id"))
        except ValueError:
            continue
        meta = metamap.get(sid)
        draft = _draft_stub(conn, sid)
        resolved = _resolve_project(row, meta, names)
        pid = resolved.get("id") if isinstance(resolved, dict) else None
        draft_pending = _draft_pending(draft)
        capture_pending = isinstance(pid, str) and pid in capture_projects
        attended = _attended_of(meta)
        entries.append({"session": _session_view(row, resolved), "meta": meta,
                        "pending": bool(draft_pending or capture_pending),
                        "pending_draft": bool(draft_pending),
                        "pending_capture": bool(capture_pending),
                        "attended": attended,
                        "draft": draft})
    entry_by_sid = {e["session"]["session_id"]: e for e in entries
                    if isinstance(e.get("session"), dict)
                    and isinstance(e["session"].get("session_id"), str)}
    return {"device_id": local, "entries": entries,
            "entry_by_sid": entry_by_sid, "hit_limit": hit_limit}


def list_sessions(conn, *, from_ms=None, to_ms=None, project_id=None,
                  limit=None, include_foreign=False,
                  desktop_bin=None, collector=None, now_ms=None) -> dict:
    now = _now_ms(now_ms)
    pid_filter = _validate_project_id_opt(project_id)
    bound = _validate_limit(limit, DEFAULT_LIMIT)
    if from_ms is None and to_ms is None:
        f_ms, t_ms = _review._day_window(_review._local_dt(now).date())
    else:
        validated = _validate_range(from_ms, to_ms)
        assert validated[0] is not None and validated[1] is not None
        f_ms, t_ms = validated
    coll = _collector(collector)
    win = _load_window(conn, coll, from_ms=f_ms, to_ms=t_ms,
                       desktop_bin=desktop_bin,
                       include_foreign=include_foreign)
    local = win["device_id"]
    hit_limit = win["hit_limit"]
    if not include_foreign and local is None:
        return {"from_ms": f_ms, "to_ms": t_ms, "device_id": None,
                "count": 0, "total": 0, "entries": [],
                "reason": "no local device recorded"}
    entries = win["entries"]
    filtered: list[dict] = []
    for entry in entries:
        if pid_filter is not None:
            project = entry.get("session", {}).get("project")
            rpid = project.get("id") if isinstance(project, dict) else None
            if rpid != pid_filter:
                continue
        filtered.append(entry)
    total = len(filtered)
    sliced = filtered[:bound]
    return {"from_ms": f_ms, "to_ms": t_ms, "device_id": local,
            "count": len(sliced), "total": total, "entries": sliced,
            "reason": "session limit reached" if hit_limit else ""}


def inbox(conn, *, from_ms=None, to_ms=None, limit=None,
          desktop_bin=None, collector=None, now_ms=None) -> dict:
    """Sessions needing attention: unattended with pending content.

    A filter over the same list, not a separate view: ``attended == 0``
    and either an unaccepted capture exists for the session's project or
    a thought draft is unsaved. The bar badge counts these. Capped at
    999, advisory, silent on sidecar failure (a missing/unreadable
    captures signal degrades to draft-only rather than failing).
    """
    now = _now_ms(now_ms)
    bound = _validate_limit(limit, INBOX_DEFAULT_LIMIT)
    bound = min(bound, INBOX_MAX_LIMIT)
    if from_ms is None and to_ms is None:
        f_ms, t_ms = _review._day_window(_review._local_dt(now).date())
    else:
        validated = _validate_range(from_ms, to_ms)
        assert validated[0] is not None and validated[1] is not None
        f_ms, t_ms = validated
    coll = _collector(collector)
    win = _load_window(conn, coll, from_ms=f_ms, to_ms=t_ms,
                       desktop_bin=desktop_bin, include_foreign=False)
    local = win["device_id"]
    hit_limit = win["hit_limit"]
    if local is None:
        return {"from_ms": f_ms, "to_ms": t_ms, "device_id": None,
                "count": 0, "total": 0, "entries": [],
                "reason": "no local device recorded"}
    attention = [e for e in win["entries"]
                 if e.get("pending") is True
                 and not e.get("attended")]
    total = len(attention)
    sliced = attention[:bound]
    return {"from_ms": f_ms, "to_ms": t_ms, "device_id": local,
            "count": len(sliced), "total": total,
            "entries": sliced,
            "reason": "session limit reached" if hit_limit else ""}


def get_session_detail(conn, *, session_id, resource_limit=None,
                       desktop_bin=None, collector=None,
                       now_ms=None) -> dict:
    sid = _validate_session_id(session_id)
    bound = _validate_limit(resource_limit, DEFAULT_LIMIT)
    _now_ms(now_ms)
    coll = _collector(collector)
    try:
        result = coll.get_session(sid, bound, desktop_bin=desktop_bin,
                                  db=None)
    except Exception as exc:
        raise LedgerError("collector unavailable") from exc
    if not isinstance(result, dict) or not isinstance(
            result.get("session"), dict):
        _error("unknown session")
    assert isinstance(result, dict)
    session = result["session"]
    assert isinstance(session, dict)
    meta = _meta_for(conn, sid)
    drafts = _drafts_for(conn, sid, 5)
    draft = drafts[0] if drafts else None
    links = _links_for(conn, sid)
    resources = result.get("resources")
    if not isinstance(resources, list):
        raise LedgerError("collector unavailable")
    names = _registry_names()
    resolved = _resolve_project(session, meta, names)
    pid = resolved.get("id") if isinstance(resolved, dict) else None
    draft_pending = _draft_pending(draft)
    capture_pending = _project_has_new_capture(conn, pid)
    return {"session": _session_view(session, resolved), "meta": meta,
            "pending": bool(draft_pending or capture_pending),
            "pending_draft": bool(draft_pending),
            "pending_capture": bool(capture_pending),
            "attended": _attended_of(meta),
            "draft": draft,
            "drafts": drafts, "links": links,
            "resources": resources[:bound]}


def _search_bounds(row: dict) -> tuple:
    def _num(value: object):
        if isinstance(value, bool):
            return None
        try:
            parsed = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return parsed
    return (_num(row.get("start_ms")), _num(row.get("end_ms")))


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _resource_copy(session) -> str:
    try:
        if not isinstance(session, dict):
            return ""
        resources = session.get("resources")
        if not isinstance(resources, list) or not resources:
            return ""
        first = resources[0]
        if not isinstance(first, dict):
            return ""
        detail = first.get("resource")
        if not isinstance(detail, dict):
            detail = {}
        zotero = detail.get("zotero")
        zotero_uri = zotero.get("uri") if isinstance(zotero, dict) else None
        candidates = (
            detail.get("file"),
            detail.get("url"),
            zotero_uri,
            detail.get("page"),
            detail.get("title"),
            first.get("portable_identity"),
            first.get("local_identity"),
            first.get("resource_key"),
        )
        for candidate in candidates:
            if isinstance(candidate, str) and candidate:
                cleaned = _CONTROL_RE.sub("", candidate)[:160]
                if cleaned:
                    return cleaned
        return ""
    except Exception:
        return ""


def _content_snippet(text: object) -> str:
    try:
        if not isinstance(text, str) or not text:
            return ""
        return _CONTROL_RE.sub("", text)[:160]
    except Exception:
        return ""


def _content_project(content: dict, names: dict) -> dict | None:
    try:
        raw = content.get("project_id")
    except AttributeError:
        return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        pid = str(uuid.UUID(raw.strip()))
    except ValueError:
        return None
    return {"id": pid, "name": names.get(pid, "")[:256]}


def search(conn, *, query, limit=None, desktop_bin=None,
           collector=None, now_ms=None) -> dict:
    """Thought/TODO full-text unioned with collector activity search.

    ``content_index`` MATCH (literal-quoted per the retrieval rules,
    never raw-interpolated) ∪ collector ``search-activity``, deduped
    by ``session_id`` with the content row winning, content matches
    ranked first (today's ranking rule), truncated to the bound.
    Entries carry ``matched`` ``"content"`` or ``"collector"``.
    Collector failure degrades to content-only entries (not zero);
    content failure degrades to collector-only; both failing reads as
    empty with ``reason: collector unavailable``.

    The dropped ``session_meta`` content columns are never read here.
    """
    text = _validate_search_text(query)
    bound = _validate_limit(limit, DEFAULT_LIMIT)
    _now_ms(now_ms)
    reason = ""
    collector_rows: list[dict] = []
    try:
        coll = _collector(collector)
    except LedgerError:
        coll = None
    if coll is None:
        reason = "collector unavailable"
    else:
        try:
            result = coll.search_activity(query=text, limit=bound,
                                          desktop_bin=desktop_bin, db=None)
        except Exception:
            result = None
        if not isinstance(result, dict) or not isinstance(
                result.get("sessions"), list):
            collector_rows = []
            reason = "collector unavailable"
        else:
            collector_rows = [r for r in result["sessions"]
                              if isinstance(r, dict)]
    content_rows: list[dict] = []
    try:
        from content_index import search_content as _search_content
        if conn is None:
            raise ValueError("no sidecar")
        found = _search_content(conn, text, bound)
        if isinstance(found, list):
            content_rows = [r for r in found if isinstance(r, dict)]
    except Exception:
        content_rows = []
    coll_by_sid: dict[str, dict] = {}
    coll_order: list[str] = []
    for row in collector_rows:
        try:
            sid = _validate_session_id(row.get("session_id"))
        except ValueError:
            continue
        if sid not in coll_by_sid:
            coll_by_sid[sid] = row
            coll_order.append(sid)
    content_by_sid: dict[str, dict] = {}
    content_order: list[str] = []
    for row in content_rows:
        try:
            sid = _validate_session_id(row.get("session_id"))
        except ValueError:
            continue
        if sid not in content_by_sid:
            content_by_sid[sid] = row
            content_order.append(sid)
    order = content_order + [sid for sid in coll_order
                             if sid not in content_by_sid]
    metamap = _bulk_meta(conn, order)
    names = _registry_names()
    entries: list[dict] = []
    for sid in order[:bound]:
        stored = metamap.get(sid)
        if sid in content_by_sid:
            content = content_by_sid[sid]
            grown = coll_by_sid.get(sid)
            if grown is not None:
                start, end = _search_bounds(grown)
                raw_did = grown.get("device_id")
                resolved = _resolve_project(grown, stored, names)
            else:
                start, end = (None, None)
                raw_did = None
                resolved = _content_project(content, names)
            entries.append({
                "session_id": sid,
                "device_id": raw_did if isinstance(raw_did, str) else None,
                "project_id": resolved["id"] if resolved else None,
                "project_name": resolved["name"] if resolved else "",
                "start_ms": start, "end_ms": end,
                "matched": "content",
                "resource_copy": _content_snippet(content.get("text"))})
        else:
            row = coll_by_sid[sid]
            start, end = _search_bounds(row)
            raw_did = row.get("device_id")
            resolved = _resolve_project(row, stored, names)
            entries.append({
                "session_id": sid,
                "device_id": raw_did if isinstance(raw_did, str) else None,
                "project_id": resolved["id"] if resolved else None,
                "project_name": resolved["name"] if resolved else "",
                "start_ms": start, "end_ms": end,
                "matched": "collector",
                "resource_copy": _resource_copy(row)})
    return {"query": text, "limit": bound, "count": len(entries),
            "total": len(order), "truncated": len(order) > len(entries),
            "entries": entries, "reason": reason}


_ANNOTATE_KEYS = frozenset({"session", "device_id", "revision",
                             "thought_ref", "todo_refs", "refs",
                             "attended"})


def annotate(conn, payload: object, *, now_ms=None) -> dict:
    now = _now_ms(now_ms)
    if conn is None:
        _error("database is unavailable")
    if not isinstance(payload, dict):
        _error("annotate payload is invalid")
    assert isinstance(payload, dict)
    for key in payload:
        if key not in _ANNOTATE_KEYS:
            _error("annotate payload is invalid")
    if "session" not in payload:
        _error("annotate payload is invalid")
    try:
        sid = _validate_session_id(payload["session"])
    except ValueError:
        _error("annotate payload is invalid")
        raise AssertionError("unreachable")
    try:
        existing = _annotations.get_session_meta(conn, sid)
    except _annotations.AnnotationsError:
        raise
    except Exception as exc:
        raise LedgerError("database is unavailable") from exc
    kwargs = {}
    for key in ("device_id", "thought_ref", "todo_refs", "refs",
                "attended"):
        if key in payload:
            kwargs[key] = payload[key]
    if existing is not None:
        rev = payload.get("revision")
        stored_rev = str(existing.get("revision", ""))
        if not isinstance(rev, str) \
                or rev.strip().lower() != stored_rev.lower():
            _error("session meta changed")
        try:
            result = _annotations.upsert_session_meta(
                conn, session_id=sid, now_ms=now,
                expect_revision=existing["revision"], **kwargs)
        except _annotations.AnnotationsError:
            raise
        except Exception as exc:
            raise LedgerError("database is unavailable") from exc
    else:
        if "revision" in payload and payload["revision"] is not None:
            _error("session meta changed")
        try:
            result = _annotations.upsert_session_meta(
                conn, session_id=sid, now_ms=now, **kwargs)
        except _annotations.AnnotationsError:
            raise
        except Exception as exc:
            raise LedgerError("database is unavailable") from exc
    return {"ok": True, "session_id": sid,
            "revision": result["revision"],
            "updated_ms": result["updated_ms"]}


def set_link(conn, *, session_id, kind, target, remove=False,
             now_ms=None) -> dict:
    now = _now_ms(now_ms)
    if conn is None:
        _error("database is unavailable")
    sid = _annotations._validate_session_id(session_id)
    bounded_kind = _validate_link_kind(kind)
    bounded_target = _annotations._validate_link_target(target)
    if remove:
        try:
            removed = _annotations.remove_session_link(
                conn, session_id=sid, kind=bounded_kind,
                target=bounded_target)
        except _annotations.AnnotationsError:
            raise
        except Exception as exc:
            raise LedgerError("database is unavailable") from exc
        return {"ok": True, "session_id": sid, "kind": bounded_kind,
                "target": bounded_target, "removed": bool(removed)}
    try:
        added = _annotations.add_session_link(
            conn, session_id=sid, kind=bounded_kind,
            target=bounded_target, now_ms=now)
    except _annotations.AnnotationsError:
        raise
    except Exception as exc:
        raise LedgerError("database is unavailable") from exc
    return {"ok": True, "session_id": sid, "kind": bounded_kind,
            "target": bounded_target, "added": bool(added)}


# ---------------------------------------------------------------------------
# CLI (shared plumbing lives in qscli.py; argv stays byte-identical)
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = qscli.SafeParser(description=__doc__)
    qscli.add_global_flags(parser, db=True, desktop_bin=True)
    sub = parser.add_subparsers(dest="command", required=True,
                                parser_class=qscli.SafeParser)
    p_list = sub.add_parser("list")
    p_list.add_argument("--from", dest="from_ms", default=None)
    p_list.add_argument("--to", dest="to_ms", default=None)
    p_list.add_argument("--project", default=None)
    p_list.add_argument("--limit", default=None)
    p_list.add_argument("--include-foreign", action="store_true")
    p_inbox = sub.add_parser("inbox")
    p_inbox.add_argument("--from", dest="from_ms", default=None)
    p_inbox.add_argument("--to", dest="to_ms", default=None)
    p_inbox.add_argument("--limit", default=None)
    p_get = sub.add_parser("get")
    p_get.add_argument("--session", required=True)
    p_get.add_argument("--resource-limit", default=None)
    p_search = sub.add_parser("search")
    p_search.add_argument("--query", required=True)
    p_search.add_argument("--limit", default=None)
    sub.add_parser("annotate")
    p_link = sub.add_parser("link")
    p_link.add_argument("--session", required=True)
    p_link.add_argument("--kind", required=True)
    p_link.add_argument("--target", required=True)
    p_link.add_argument("--remove", action="store_true")
    return parser.parse_args(argv if argv is not None else sys.argv[1:])


_READ_COMMANDS = frozenset({"list", "inbox", "get", "search"})


def _dispatch(args: argparse.Namespace) -> dict:
    now = int(time.time() * 1000)
    if args.command in _READ_COMMANDS:
        conn = _review.read_connection(args.db)
    else:
        conn = _annotations.connect(args.db)
    try:
        if args.command == "list":
            return list_sessions(
                conn, from_ms=args.from_ms, to_ms=args.to_ms,
                project_id=args.project,
                limit=args.limit, include_foreign=args.include_foreign,
                desktop_bin=args.desktop_bin, collector=None, now_ms=now)
        elif args.command == "inbox":
            return inbox(
                conn, from_ms=args.from_ms, to_ms=args.to_ms,
                limit=args.limit, desktop_bin=args.desktop_bin,
                collector=None, now_ms=now)
        elif args.command == "get":
            return get_session_detail(
                conn, session_id=args.session,
                resource_limit=args.resource_limit,
                desktop_bin=args.desktop_bin, collector=None, now_ms=now)
        elif args.command == "search":
            return search(
                conn, query=args.query, limit=args.limit,
                desktop_bin=args.desktop_bin, collector=None, now_ms=now)
        elif args.command == "annotate":
            payload = qscli.read_input()
            return annotate(conn, payload, now_ms=now)
        elif args.command == "link":
            return set_link(conn, session_id=args.session, kind=args.kind,
                            target=args.target, remove=args.remove,
                            now_ms=now)
        else:
            _error("invalid arguments")
    finally:
        try:
            conn.close()
        except Exception:
            pass


_BOUNDED_EXCEPTIONS = (LedgerError, _annotations.AnnotationsError, OSError,
                       TypeError, ValueError, UnicodeError, RecursionError,
                       OverflowError)


def main(argv=None) -> int:
    return qscli.run_main(_parse_args, _dispatch, "sessions",
                          _BOUNDED_EXCEPTIONS, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
