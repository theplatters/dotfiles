#!/usr/bin/env python3
"""Evening review / morning plan.

``get`` returns the deterministic review for a day (projects worked,
repository changes, captures, TODOs, journal presence, tomorrow top-3),
generating and storing it on demand: ``morning`` generates any time
(popup-open generation), ``evening`` only once local time reaches
``reviewTime``. The top-3 is deterministic order only (no
prioritization, no polish, no model calls). ``prepare-save`` / ``apply``
save the review to today's journal through revision-checked
``journal_assistant`` with an exact preview + single-use token,
mirroring ``work_log.py``.

Conventions match the other helpers: JSON stdout, single-line
``error: ...`` on stderr with exit 1, no tracebacks, no state content
in diagnostics, list-form argv with ``shell=False``, bounded IO.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import secrets
import sqlite3
import stat
import sys
import time
from pathlib import Path
from typing import NoReturn

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import annotations as _annotations
import memory_tick as tick
import qscli
from text_safety import safe_text_or_none as _safe_text_or_none


class ReviewError(ValueError):
    pass


def _error(message: str) -> NoReturn:
    raise ReviewError(message)


MAX_PROJECTS = 8
MAX_CANDIDATES = 12
TOP_N = 3
TODO_TEXT_LIMIT = 240
MARKDOWN_LIMIT = 8 * 1024
PREPARED_TTL_MS = 600_000
PREPARED_EXPIRES_IN_S = 600
# Transport cap; single source of truth is qscli.INPUT_LIMIT (same value).
INPUT_LIMIT = 1024 * 1024
SESSION_LIST_LIMIT = 200
PAGE_CONTENT_LIMIT = 128 * 1024
REVIEW_MARKER_PREFIX = "quickshell-review::"


def result(reason, *, called=0, changed=False):
    # ``jev_calls`` is always 0 since the Jev path was deleted; the key
    # is kept so the review payload shape stays stable for the scheduler.
    deferred = reason in ("capped", "failed", "unavailable",
                          "evidence_unavailable")
    return {"status": "ok" if changed else "deferred" if deferred else "skipped",
            "reason": reason, "jev_calls": called, "changed": changed}


# ---------------------------------------------------------------------------
# Validation / time helpers
# ---------------------------------------------------------------------------

def _validate_kind(value) -> str:
    if not isinstance(value, str) or value.strip() not in ("evening",
                                                           "morning"):
        _error("kind must be evening or morning")
    return value.strip()


def _validate_day(value) -> datetime.date:
    if value is None:
        return datetime.date.today()
    if not isinstance(value, str) or not value.strip():
        _error("date must be YYYY-MM-DD")
    text = value.strip()
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        _error("date must be YYYY-MM-DD")


def _validate_ms(value) -> int:
    if isinstance(value, bool):
        _error("timestamp is invalid")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = int(value.strip(), 10)
        except ValueError:
            _error("timestamp is invalid")
    else:
        _error("timestamp is invalid")
    if parsed < 0 or parsed > 9223372036854775807:
        _error("timestamp is invalid")
    return parsed


def _local_dt(now_ms: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(now_ms / 1000)


def _today_iso(now_ms: int) -> str:
    return _local_dt(now_ms).date().isoformat()


def _parse_hhmm(value, default: str) -> tuple[int, int]:
    text = value if isinstance(value, str) else default
    stripped = text.strip() if isinstance(text, str) else ""
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", stripped)
    if not match:
        match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", default)
    assert match is not None
    return (int(match.group(1)), int(match.group(2)))


def _review_time(settings: dict) -> str:
    raw = settings.get("reviewTime", "18:00") \
        if isinstance(settings, dict) else "18:00"
    hour, minute = _parse_hhmm(raw, "18:00")
    return f"{hour:02d}:{minute:02d}"


def _morning_time(settings: dict) -> str:
    raw = settings.get("morningTime", "07:00") \
        if isinstance(settings, dict) else "07:00"
    hour, minute = _parse_hhmm(raw, "07:00")
    return f"{hour:02d}:{minute:02d}"


def _at_or_after(now_ms: int, hhmm: str) -> bool:
    hour, minute = _parse_hhmm(hhmm, "00:00")
    local = _local_dt(now_ms)
    return (local.hour, local.minute) >= (hour, minute)


def _day_window(day: datetime.date) -> tuple[int, int]:
    start = int(datetime.datetime(day.year, day.month, day.day)
                .timestamp() * 1000)
    nxt = day + datetime.timedelta(days=1)
    end = int(datetime.datetime(nxt.year, nxt.month, nxt.day)
              .timestamp() * 1000)
    if start < 0:
        start = 0
    if end <= start:
        end = start + 86400000
    return (start, end)


def _target_date(day: datetime.date, kind: str) -> str:
    if kind == "evening":
        return (day + datetime.timedelta(days=1)).isoformat()
    return day.isoformat()


def _marker(day_iso: str, kind: str) -> str:
    return f"{REVIEW_MARKER_PREFIX}{day_iso}-{kind}"


def _get_state(conn, key: str) -> str:
    try:
        value = _annotations.get_state(conn, key, default="")
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# Evidence / graph access (bounded, fail-soft per section)
# ---------------------------------------------------------------------------

def _list_sessions(evidence, *, limit: int, from_ms: int,
                   to_ms: int) -> list[dict]:
    fn = getattr(evidence, "list_sessions", None)
    if not callable(fn):
        raise ReviewError("evidence is unavailable")
    try:
        rows = fn(limit=limit, from_ms=from_ms, to_ms=to_ms)
    except ReviewError:
        raise
    except Exception as exc:
        raise ReviewError("evidence is unavailable") from exc
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)][:limit]


def _resolve_graph_soft(graph):
    if graph is not None and str(graph).strip():
        try:
            from logseq_common import graph_path
            return graph_path(str(graph).strip())
        except Exception:
            return None
    try:
        from logseq_common import resolve_graph
        return resolve_graph(None)
    except Exception:
        return None


def _resolve_graph_hard(graph):
    resolved = _resolve_graph_soft(graph)
    if resolved is None:
        _error("logseq graph is unavailable")
    assert resolved is not None
    return resolved


def _registry_names() -> dict:
    try:
        import projects as _projects
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


def _session_bounds(session: dict) -> tuple[int, int]:
    try:
        start = session.get("start_ms", 0)
        end = session.get("end_ms", 0)
        if isinstance(start, bool) or isinstance(end, bool):
            return (0, 0)
        start = int(start)
        end = int(end)
    except (TypeError, ValueError):
        return (0, 0)
    if start < 0 or end < 0:
        return (0, 0)
    return (start, end)


def _truncate_utf8(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    clipped = raw[:limit]
    while clipped:
        try:
            return clipped.decode("utf-8")
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return ""


# ---------------------------------------------------------------------------
# Deterministic sections
# ---------------------------------------------------------------------------

def _build_projects(sessions: list[dict], *, conn, settings: dict,
                    evidence, now_ms: int):
    """Aggregate sessions per project; returns (public, latest_by_pid)."""
    import work_log as _work_log
    names = _registry_names()
    agg: dict[str, dict] = {}
    for session in sessions:
        try:
            resolved = _work_log.resolve_session_project(
                session, conn=conn, settings=settings, evidence=evidence,
                now_ms=now_ms)
        except Exception:
            resolved = None
        pid = resolved.get("project_id") \
            if isinstance(resolved, dict) else None
        if not isinstance(pid, str) or not pid.strip():
            pid = None
        start, end = _session_bounds(session)
        minutes = max(0, (end - start) // 60000) if end >= start else 0
        raw_sid = session.get("session_id")
        sid = raw_sid.strip() if isinstance(raw_sid, str) else ""
        key = pid or ""
        entry = agg.get(key)
        if entry is None:
            entry = {"project_id": pid,
                     "name": names.get(pid, "") if pid else "other",
                     "sessions": 0, "minutes": 0, "latest_sid": "",
                     "latest_end": -1}
            agg[key] = entry
        entry["sessions"] += 1
        entry["minutes"] += minutes
        if sid and end >= entry["latest_end"]:
            entry["latest_end"] = end
            entry["latest_sid"] = sid
    ordered = sorted(agg.values(),
                     key=lambda e: (-e["minutes"], -e["sessions"],
                                    e["name"]))[:MAX_PROJECTS]
    public = [{"project_id": e["project_id"], "name": e["name"],
               "sessions": e["sessions"], "minutes": e["minutes"]}
              for e in ordered]
    latest = {e["project_id"]: e["latest_sid"] for e in ordered
              if e["project_id"]}
    return public, latest


def _build_changes(public_projects: list[dict],
                   latest_by_pid: dict) -> list[dict]:
    """One bounded change summary per worked project (short hashes)."""
    import project_session_changes as _session_changes
    out: list[dict] = []
    for project in public_projects[:MAX_PROJECTS]:
        pid = project.get("project_id")
        name = project.get("name", "")
        if not isinstance(pid, str) or not pid:
            continue
        sid = latest_by_pid.get(pid, "")
        if not isinstance(sid, str) or not sid:
            out.append({"project_id": pid, "name": name,
                        "commits": "none", "note": "unavailable"})
            continue
        try:
            changes = _session_changes.session_changes(pid, sid)
        except Exception:
            changes = None
        if not isinstance(changes, dict) or not changes.get("available"):
            out.append({"project_id": pid, "name": name,
                        "commits": "none", "note": "unavailable"})
            continue
        base = changes.get("baseline_commit")
        latest = changes.get("latest_commit")
        if isinstance(base, str) and base.strip() \
                and isinstance(latest, str) and latest.strip():
            commits = f"{base.strip()[:7]}..{latest.strip()[:7]}"
            note = ""
        else:
            commits = "none"
            note = "no baseline"
        out.append({"project_id": pid, "name": name,
                    "commits": commits[:64], "note": note[:160]})
    return out


def _build_captures(conn, start_ms: int, end_ms: int) -> dict | None:
    try:
        rows = _annotations.list_captures(
            conn, day_start_ms=start_ms, day_end_ms=end_ms, status="all",
            limit=100)
    except Exception:
        return None
    new = sum(1 for r in rows if r.get("status") == "new")
    accepted = sum(1 for r in rows if r.get("status") == "accepted")
    applied = sum(1 for r in rows
                  if isinstance(r.get("applied_ms"), int)
                  and not isinstance(r.get("applied_ms"), bool)
                  and start_ms <= r["applied_ms"] < end_ms)
    return {"new": new, "accepted": accepted, "applied": applied}


def _build_todos(graph, day_iso: str) -> dict | None:
    """Map ``list_agenda`` rows: scheduled = rows on the day (done or
    not), completed = the done subset on the day."""
    if graph is None:
        return None
    import daily_agenda as _agenda
    try:
        data = _agenda.list_agenda(graph, day_iso)
    except Exception:
        return None
    tasks = data.get("tasks", []) if isinstance(data, dict) else []
    if not isinstance(tasks, list):
        return {"scheduled": 0, "completed": 0}
    scheduled = sum(1 for t in tasks
                    if isinstance(t, dict)
                    and t.get("scheduledDate") == day_iso)
    completed = sum(1 for t in tasks
                    if isinstance(t, dict)
                    and t.get("scheduledDate") == day_iso
                    and t.get("done") is True)
    return {"scheduled": scheduled, "completed": completed}


def _build_journal(graph, day_iso: str) -> dict | None:
    if graph is None:
        return None
    import journal_assistant as _journal
    try:
        _raw, _mode, identity = _journal._current_journal(
            graph, date=day_iso.replace("-", "_"), create=False)
    except Exception:
        return None
    return {"present": identity is not None}


# ---------------------------------------------------------------------------
# Tomorrow top-3 (deterministic order only)
# ---------------------------------------------------------------------------

def _todo_candidates(graph) -> list[dict]:
    if graph is None:
        return []
    import logseq_todos as _todos
    try:
        rows = _todos.todos(graph)
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if len(out) >= MAX_CANDIDATES:
            break
        kept = _safe_text_or_none(row.get("task", ""), TODO_TEXT_LIMIT)
        if kept is None:
            continue
        line = row.get("line")
        out.append({
            "task": kept,
            "path": str(row.get("path", ""))[:512],
            "line": line if type(line) is int and line >= 0 else 0,
            "page": str(row.get("page", ""))[:256],
        })
    return out


def _relative_page_path(graph, abs_path: str) -> str | None:
    try:
        rel = Path(abs_path).relative_to(graph)
    except Exception:
        return None
    if not rel.parts or rel.parts[0] != "pages":
        return None
    if rel.suffix.casefold() != ".md":
        return None
    text = rel.as_posix()
    if len(text) > 1024:
        return None
    return text


def _attach_revisions(graph, items: list[dict]) -> list[dict]:
    """Current per-page revision per candidate (None when unreadable).

    Stored so ``Add to tomorrow`` (``daily_agenda.py select``, which
    requires a fresh revision) can proceed; a stale revision at click
    time surfaces the bounded select error and the user refreshes.
    """
    if graph is None:
        return [dict(item, revision=None) for item in items]
    import project_planner as _planner
    out: list[dict] = []
    for item in items:
        revision = None
        try:
            rel = _relative_page_path(graph, item.get("path", ""))
            if rel is not None:
                page = _planner.read_page(graph, {"path": rel})
                if isinstance(page, dict) \
                        and isinstance(page.get("revision"), str):
                    revision = page["revision"]
        except Exception:
            revision = None
        out.append(dict(item, revision=revision))
    return out


def _top_item(item: dict, score, target_date: str) -> dict:
    return {"task": item["task"], "path": item["path"],
            "line": item["line"], "page": item["page"],
            "revision": item.get("revision"), "score": score,
            "target_date": target_date}


def _prioritize(items: list[dict], *, kind: str,
                target_date: str):
    """Return (top, prioritized, jev_calls) for up to 12 candidates.

    Deterministic only: the first 3 candidates, zero calls. The Jev
    prioritization path is deleted; ``prioritized`` is always False.
    """
    _ = kind
    deterministic = [_top_item(i, None, target_date)
                     for i in items[:TOP_N]]
    if not items:
        return ([], False, 0)
    return (deterministic, False, 0)


def _render_markdown(kind: str, day_iso: str, sections: dict,
                     top: list[dict]) -> str:
    lines = [f"# {kind.capitalize()} review — {day_iso}", ""]
    lines.append("## Projects")
    projects = sections.get("projects", [])
    if isinstance(projects, list) and projects:
        for entry in projects:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or "other"
            lines.append(f"- {name} — {entry.get('sessions', 0)}"
                         f" session(s), {entry.get('minutes', 0)} min")
    else:
        lines.append("(no sessions)")
    lines.append("")
    lines.append("## Changes")
    changes = sections.get("changes", [])
    if isinstance(changes, list) and changes:
        for entry in changes:
            if not isinstance(entry, dict):
                continue
            lines.append(f"- {entry.get('name') or 'other'}:"
                         f" {entry.get('commits', 'none')}")
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("## Captures")
    captures = sections.get("captures")
    if isinstance(captures, dict):
        lines.append(f"new={captures.get('new', 0)}"
                     f" accepted={captures.get('accepted', 0)}"
                     f" applied={captures.get('applied', 0)}")
    else:
        lines.append("(unavailable)")
    lines.append("")
    lines.append("## TODOs")
    todos = sections.get("todos")
    if isinstance(todos, dict):
        lines.append(f"scheduled={todos.get('scheduled', 0)}"
                     f" completed={todos.get('completed', 0)}")
    else:
        lines.append("(unavailable)")
    lines.append("")
    lines.append("## Journal")
    journal = sections.get("journal")
    if isinstance(journal, dict):
        lines.append("present" if journal.get("present") else "absent")
    else:
        lines.append("(unavailable)")
    lines.append("")
    lines.append("## Top 3")
    if top:
        for rank, item in enumerate(top, 1):
            if not isinstance(item, dict):
                continue
            lines.append(f"{rank}. {item.get('task', '')}"
                         f" ({item.get('page', '')},"
                         f" {item.get('target_date', '')})")
    else:
        lines.append("(none)")
    lines.append("")
    return _truncate_utf8("\n".join(lines), MARKDOWN_LIMIT)


# ---------------------------------------------------------------------------
# Generation + stored view
# ---------------------------------------------------------------------------

def generate_review(conn, *, kind: str, day: str, settings: dict,
                    now_ms: int, evidence, graph) -> dict:
    """Build (but do not store) the deterministic review payload."""
    settings = settings if isinstance(settings, dict) else {}
    stamp = _validate_ms(now_ms)
    bounded_kind = _validate_kind(kind)
    parsed = _validate_day(day)
    day_iso = parsed.isoformat()
    start, end = _day_window(parsed)
    sessions = _list_sessions(evidence, limit=SESSION_LIST_LIMIT,
                              from_ms=start, to_ms=end)
    projects, latest = _build_projects(
        sessions, conn=conn, settings=settings, evidence=evidence,
        now_ms=stamp)
    changes = _build_changes(projects, latest)
    captures = _build_captures(conn, start, end)
    todos = _build_todos(graph, day_iso)
    journal = _build_journal(graph, day_iso)
    sections: dict = {"projects": projects, "changes": changes}
    unavailable: list[str] = []
    if captures is None:
        unavailable.append("captures")
    else:
        sections["captures"] = captures
    if todos is None:
        unavailable.append("todos")
    else:
        sections["todos"] = todos
    if journal is None:
        unavailable.append("journal")
    else:
        sections["journal"] = journal
    if unavailable:
        sections["unavailable"] = unavailable
    candidates = _todo_candidates(graph)
    with_revisions = _attach_revisions(graph, candidates)
    target_date = _target_date(parsed, bounded_kind)
    top, prioritized, calls = _prioritize(
        with_revisions, kind=bounded_kind, target_date=target_date)
    markdown = _render_markdown(bounded_kind, day_iso, sections, top)
    return {"kind": bounded_kind, "day": day_iso,
            "generated_ms": stamp, "prioritized": prioritized,
            "jev_calls": calls, "sections": sections, "top": top,
            "markdown": markdown}


def _export_markdown(payload: dict) -> str:
    body = payload.get("markdown", "")
    return body if isinstance(body, str) else ""


def review_view(row: dict, *, settings: dict) -> dict:
    """Public ``get`` shape for one stored review row."""
    payload = row.get("json", {})
    if not isinstance(payload, dict):
        payload = {}
    _ = settings
    sections = payload.get("sections", {})
    top = payload.get("top", [])
    return {"kind": row["kind"], "day": row["day"], "found": True,
            "review": {
                "generated_ms": row["generated_ms"],
                "polished": bool(row["polished"]),
                "saved_ms": row["saved_ms"],
                "prioritized": bool(payload.get("prioritized", False)),
                "polish_enabled": False,
                "sections": sections if isinstance(sections, dict) else {},
                "top": top if isinstance(top, list) else [],
                "markdown": _export_markdown(payload),
            }}


def _load_stored(conn, kind: str, day_iso: str):
    try:
        return _annotations.get_review(conn, day_iso, kind)
    except _annotations.AnnotationsError as exc:
        raise ReviewError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Tick adapter (called by memory_tick.default_review)
# ---------------------------------------------------------------------------

def review(ctx) -> dict:
    """Generate due reviews once per day (morning first, then evening)."""
    settings = ctx.get("settings") \
        if isinstance(ctx.get("settings"), dict) else {}
    conn = ctx.get("conn")
    try:
        now_ms = _validate_ms(ctx.get("now_ms"))
    except ReviewError:
        return result("failed")
    if settings.get("dailyReview") is not True:
        return result("disabled")
    today = _today_iso(now_ms)
    morning_due = _at_or_after(now_ms, _morning_time(settings)) \
        and _get_state(conn, "last_review_day:morning") != today
    evening_due = _at_or_after(now_ms, _review_time(settings)) \
        and _get_state(conn, "last_review_day:evening") != today
    due: list[str] = []
    if morning_due:
        due.append("morning")
    if evening_due:
        due.append("evening")
    if not due:
        return result("not_due")
    evidence = ctx.get("evidence")
    start, end = _day_window(datetime.date.fromisoformat(today))
    probe_failure = None
    quiet_evening = False
    if "evening" in due:
        try:
            probe = _list_sessions(evidence, limit=1, from_ms=start,
                                   to_ms=end)
        except ReviewError:
            due.remove("evening")
            probe_failure = "graph_unavailable"
        else:
            if not probe:
                # Late-evening work must still be reviewed: skip without
                # setting last_review_day:evening so a later tick retries.
                due.remove("evening")
                quiet_evening = True
    if not due:
        if probe_failure is not None:
            return result(probe_failure)
        return result("no_activity" if quiet_evening else "not_due")
    graph = _resolve_graph_soft(ctx.get("graph"))
    calls = 0
    changed = False
    failure = None
    for kind in due:
        try:
            payload = generate_review(
                conn, kind=kind, day=today, settings=settings,
                now_ms=now_ms, evidence=evidence, graph=graph)
        except ReviewError as exc:
            failure = "graph_unavailable" \
                if "unavailable" in str(exc) else "failed"
            continue
        except Exception:
            failure = "failed"
            continue
        try:
            _annotations.save_review(conn, today, kind, payload, now_ms)
            _annotations.set_state(conn, f"last_review_day:{kind}", today)
        except Exception:
            failure = "failed"
            continue
        try:
            calls += max(0, int(payload.get("jev_calls", 0)))
        except (TypeError, ValueError):
            pass
        changed = True
    if changed:
        return result("", called=calls, changed=True)
    if probe_failure is not None and failure is None:
        return result(probe_failure, called=calls)
    return result(failure or "failed", called=calls)


# ---------------------------------------------------------------------------
# Export: prepare-save (exact preview) + apply (revision-checked write)
# ---------------------------------------------------------------------------

def _new_token() -> str:
    return "rev_" + secrets.token_hex(16)


def _store_prepared(conn, *, payload: dict, preview: dict, revision,
                    expires_ms: int) -> str:
    for _ in range(3):
        token = _new_token()
        try:
            _annotations.create_prepared(
                conn, token=token, kind="review-journal", payload=payload,
                preview=preview, revision=revision, expires_ms=expires_ms)
        except _annotations.AnnotationsError as exc:
            if "already exists" in str(exc):
                continue
            raise ReviewError(str(exc)) from exc
        return token
    _error("prepare failed")


def prepare_save(conn, kind: str, day: str, *, graph=None,
                 settings=None, now_ms) -> dict:
    """Stage the review export to today's journal (exact preview).

    Reviews always land in today's journal: ``journal_assistant``
    rejects any other date, so a backfilled ``--date`` still saves to
    the current journal (the ``quickshell-review::<day>-<kind>``
    marker keeps days distinct).
    """
    _ = settings
    stamp = _validate_ms(now_ms)
    bounded_kind = _validate_kind(kind)
    parsed = _validate_day(day)
    day_iso = parsed.isoformat()
    try:
        row = _annotations.get_review(conn, day_iso, bounded_kind)
    except _annotations.AnnotationsError as exc:
        raise ReviewError(str(exc)) from exc
    if row is None or not isinstance(row.get("json"), dict):
        _error("unknown review")
    assert row is not None
    marker = _marker(day_iso, bounded_kind)
    export = _export_markdown(row["json"])
    if not export.strip():
        _error("review has no text to save")
    text = export.rstrip("\n") + "\n\n" + marker + "\n"
    if len(text.encode("utf-8")) > PAGE_CONTENT_LIMIT:
        _error("review is too large")
    resolved = _resolve_graph_hard(graph)
    import journal_assistant as _journal
    try:
        current = _journal.context(resolved)
    except Exception as exc:
        raise ReviewError("journal is unavailable") from exc
    if not isinstance(current, dict):
        raise ReviewError("journal is unavailable")
    content = current.get("content", "")
    if not isinstance(content, str):
        raise ReviewError("journal is unavailable")
    if marker in content:
        _error("review already saved to journal")
    revision = current.get("revision")
    today_journal = datetime.date.today().strftime("%Y_%m_%d")
    try:
        prepped = _journal.prepare(resolved, today_journal, revision, text)
    except Exception as exc:
        raise ReviewError("journal is unavailable") from exc
    if not isinstance(prepped, dict):
        raise ReviewError("journal is unavailable")
    payload = {"kind": bounded_kind, "day": day_iso,
               "path": prepped.get("path"), "date": prepped.get("date"),
               "revision": prepped.get("revision"),
               "addition": prepped.get("addition")}
    preview = {"target": "journal", "path": prepped.get("path"),
               "date": prepped.get("date"),
               "revision": prepped.get("revision"),
               "addition": prepped.get("addition")}
    token = _store_prepared(conn, payload=payload, preview=preview,
                            revision=prepped.get("revision"),
                            expires_ms=stamp + PREPARED_TTL_MS)
    return {"prepared": token, "target": "journal", "kind": bounded_kind,
            "day": day_iso, "preview": preview,
            "revision": prepped.get("revision"),
            "expires_in": PREPARED_EXPIRES_IN_S}


def apply_saved(conn, token, *, graph=None, now_ms) -> dict:
    """Consume a prepared token (at-most-once) and append to the journal."""
    stamp = _validate_ms(now_ms)
    try:
        row = _annotations.consume_prepared(conn, token, now_ms=stamp)
    except _annotations.AnnotationsError as exc:
        raise ReviewError(str(exc)) from exc
    if row is None:
        _error("prepared export is unknown or expired")
    assert row is not None
    if row.get("kind") != "review-journal":
        _error("prepared export is invalid")
    stored_revision = row.get("revision")
    payload = row.get("payload")
    if not isinstance(payload, dict):
        _error("prepared export is invalid")
    kind = payload.get("kind")
    day_iso = payload.get("day")
    date = payload.get("date")
    addition = payload.get("addition")
    if kind not in ("evening", "morning") \
            or not isinstance(day_iso, str) or not day_iso \
            or not isinstance(date, str) or not date \
            or not isinstance(addition, str) or not addition:
        _error("prepared export is invalid")
    resolved = _resolve_graph_hard(graph)
    import journal_assistant as _journal
    try:
        result_page = _journal.append(resolved, date, stored_revision,
                                      addition)
    except Exception as exc:
        raise ReviewError("journal write failed") from exc
    if not isinstance(result_page, dict):
        raise ReviewError("journal write failed")
    try:
        _annotations.mark_review_saved(conn, day_iso, kind, stamp)
    except Exception:
        pass
    return {"applied": True, "target": "journal", "kind": kind,
            "day": day_iso, "path": result_page.get("path"),
            "revision": result_page.get("revision")}


def read_connection(db):
    # No schema initialization, directory creation or review generation
    # on read paths.
    path = _annotations.resolve_db_path(db)
    if ".." in path.parts:
        raise ValueError("unsafe database")
    if not path.parent.exists():
        return None
    import project_sessions
    project_sessions._check_components(path.parent)
    info = path.parent.stat()
    _annotations._check_owner(info)
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("unsafe database")
    _annotations._reject_file_safety(path)
    _annotations._check_sidecars(path)
    if not path.exists():
        return None
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                           timeout=_annotations.BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# CLI (shared plumbing lives in qscli.py; argv stays byte-identical)
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = qscli.SafeParser(description=__doc__)
    qscli.add_global_flags(parser, db=True, graph=True)
    sub = parser.add_subparsers(dest="command", required=True,
                                parser_class=qscli.SafeParser)
    p_get = sub.add_parser("get")
    p_get.add_argument("--kind", required=True)
    p_get.add_argument("--date", default=None)
    p_get.add_argument("--refresh", action="store_true")
    p_prep = sub.add_parser("prepare-save")
    p_prep.add_argument("--kind", required=True)
    p_prep.add_argument("--date", default=None)
    p_apply = sub.add_parser("apply")
    p_apply.add_argument("--prepared", required=True)
    return parser.parse_args(argv)


def _default_settings() -> dict:
    return {"dailyReview": True, "reviewTime": "18:00",
            "morningTime": "07:00"}


def _load_settings() -> dict:
    try:
        return tick._load_settings()
    except Exception:
        return _default_settings()


def _do_get(args, settings: dict, now_ms: int) -> dict:
    kind = _validate_kind(args.kind)
    day_iso = _validate_day(args.date).isoformat()
    conn = read_connection(args.db)
    try:
        stored = _load_stored(conn, kind, day_iso) \
            if conn is not None else None
    finally:
        if conn is not None:
            conn.close()
    if stored is not None and not args.refresh:
        return review_view(stored, settings=settings)
    if kind == "evening" and not _at_or_after(now_ms,
                                              _review_time(settings)):
        return {"kind": kind, "day": day_iso, "found": False,
                "reason": "not_due"}
    conn_w = _annotations.connect(args.db)
    try:
        graph = _resolve_graph_soft(args.graph)
        payload = generate_review(
            conn_w, kind=kind, day=day_iso, settings=settings,
            now_ms=now_ms, evidence=tick.DefaultEvidence(), graph=graph)
        _annotations.save_review(conn_w, day_iso, kind, payload, now_ms)
        row = _load_stored(conn_w, kind, day_iso)
    finally:
        try:
            conn_w.close()
        except Exception:
            pass
    if row is None:
        _error("review generation failed")
    assert row is not None
    return review_view(row, settings=settings)


def _dispatch(args: argparse.Namespace) -> dict | int:
    # Grandfathered: get bypasses qscli.emit with
    # print(json.dumps(..., sort_keys=True, allow_nan=False)) — preserved
    # exact output path.
    if args.command == "get":
        settings = _load_settings()
        now_ms = int(time.time() * 1000)
        value = _do_get(args, settings, now_ms)
        print(json.dumps(value, sort_keys=True, allow_nan=False))
        return 0
    conn = _annotations.connect(args.db)
    try:
        settings = _load_settings()
        now_ms = int(time.time() * 1000)
        if args.command == "prepare-save":
            value = prepare_save(conn, args.kind, args.date,
                                 graph=args.graph, settings=settings,
                                 now_ms=now_ms)
        elif args.command == "apply":
            value = apply_saved(conn, args.prepared, graph=args.graph,
                                now_ms=now_ms)
        else:
            _error("unknown command")
        return value
    finally:
        try:
            conn.close()
        except Exception:
            pass


_BOUNDED_EXCEPTIONS = (ReviewError, _annotations.AnnotationsError,
                       tick.TickError, OSError, TypeError, ValueError,
                       UnicodeError, RecursionError, OverflowError)


def main(argv=None) -> int:
    return qscli.run_main(_parse_args, _dispatch, "daily review",
                          _BOUNDED_EXCEPTIONS, argv=argv)


if __name__ == "__main__":
    sys.exit(main())
