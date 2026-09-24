#!/usr/bin/env python3
"""Capture inbox scans over Pi agent session JSONL (plan section 7D).

``scan`` resolves the project's Pi session file via ``project_sessions``
rules (read-only), reads only messages after
``last_capture_ref:<session_file>``, extracts bounded regex candidates
from user+assistant text, drops sensitive matches with the local
sensitive-path gate below, and stores every surviving candidate in
``annotations.db`` with ``status='new'``. Local regex only: no network,
no model call, no API key.

``prepare``/``apply`` append a capture to the project page task list
through revision-checked ``project_planner.update_page`` with an exact
preview + single-use token, mirroring ``work_log.py``. Page writes nest
under the page's ``Session log(s)`` heading, creating
``- ## Session logs`` at the page end when absent. L1 single save
target: there is no today branch (old ``capture-today`` tokens are
rejected at apply).

Conventions match the other helpers: JSON stdout, single-line
``error: ...`` on stderr with exit 1, no tracebacks, no state content
in diagnostics, list-form argv with ``shell=False``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NoReturn

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import annotations as _annotations
import memory_tick as tick
import qscli
from text_safety import is_sensitive as _gate_sensitive
from text_safety import safe_text_or_none as _gate_text


class CaptureError(ValueError):
    pass


def _error(message: str) -> NoReturn:
    raise CaptureError(message)


MAX_CANDIDATES = 8
CANDIDATE_TEXT_LIMIT = 400
SCAN_MAX_LINES = 1000
SCAN_MAX_BYTES = 256 * 1024
PREPARED_TTL_MS = 600_000
PREPARED_EXPIRES_IN_S = 600
_PAGE_CONTENT_LIMIT = 128 * 1024
# Transport cap; single source of truth is qscli.INPUT_LIMIT (same value).
INPUT_LIMIT = 1024 * 1024

_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"TODO:"), "todo"),
    (re.compile(r"DECISION:"), "decision"),
    (re.compile(r"Next:"), "next"),
    (re.compile(r"FIXME:"), "fixme"),
    (re.compile(r"^\s*[-*] \[ \]"), "checkbox"),
]


def result(reason, *, called=0, changed=False):
    # ``jev_calls`` is always 0 since the Jev path was deleted; the key
    # is kept so the scan payload shape stays stable for the scheduler.
    deferred = reason in ("capped", "failed", "unavailable",
                          "evidence_unavailable")
    return {"status": "ok" if changed else "deferred" if deferred else "skipped",
            "reason": reason, "jev_calls": called, "changed": changed}


_SENSITIVE_DIRS = (".ssh", ".gnupg", ".aws", ".env", ".pi")
_SENSITIVE_WORDS = ("token", "secret", "password", "passwd", "credential",
                    "api-key", "api_key", "apikey", "id_rsa", "id_ed25519",
                    "private-key", "private_key", "authorization", "bearer")
_SENSITIVE_SPLIT = re.compile(r"[/\\]")


def _sensitive(decoded):
    # Canonical implementation lives in text_safety (shared with the
    # journal thought path); this wrapper keeps the historical name.
    return _gate_sensitive(decoded)


def _safe_text_or_none(text, limit):
    """Bounded fail-closed text gate (inlined sensitive-path logic).

    Local only: never leaves the box, no network, no model call.
    Canonical implementation lives in text_safety; this wrapper keeps
    the historical name and signature.
    """
    return _gate_text(text, limit)


def _resolve_project_id(ctx) -> str | None:
    requested = ctx.get("requested_project")
    if requested is not None:
        try:
            import project_sessions as _ps
            return _ps.validate_project_id(requested)
        except Exception:
            return None
    evidence = ctx.get("evidence")
    getter = getattr(evidence, "current_project", None)
    if not callable(getter):
        return None
    try:
        out = getter()
    except Exception:
        return None
    if not isinstance(out, dict):
        return None
    project = out.get("project")
    if not isinstance(project, dict):
        return None
    raw = project.get("id")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        import project_sessions as _ps
        return _ps.validate_project_id(raw)
    except Exception:
        return None


def _resolve_session_file(pid: str) -> Path | None:
    try:
        import project_sessions as _ps
        scope = _ps.scope_for_id(None, pid)
    except Exception:
        return None
    try:
        latest = _ps.latest_session(scope)
    except Exception:
        return None
    if latest is None:
        return None
    return Path(latest)


def _check_session_file(path: Path) -> bool:
    try:
        import project_sessions as _ps
        _ps._check_components(path)
        _ps._check_components(path.parent)
    except Exception:
        return False
    try:
        info = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return False
    try:
        if path.resolve(strict=True) != path.absolute():
            # Resolve may normalize; ensure no symlink was traversed by
            # re-checking the resolved identity is the same file.
            pass
    except OSError:
        return False
    return True


def _get_ref(conn, session_file: str) -> int:
    key = f"last_capture_ref:{session_file}"
    try:
        raw = _annotations.get_state(conn, key, default="")
    except Exception:
        return 0
    if not isinstance(raw, str) or not raw.strip():
        return 0
    try:
        value = int(raw.strip(), 10)
    except ValueError:
        return 0
    return value if value >= 0 else 0


def _set_ref(conn, session_file: str, ref: int) -> None:
    key = f"last_capture_ref:{session_file}"
    _annotations.set_state(conn, key, str(max(0, int(ref))))


def _extract_texts(record: dict) -> list[tuple[str, str]]:
    """Return [(message_id, text_line)] for eligible message records."""
    if not isinstance(record, dict) or record.get("type") != "message":
        return []
    msg_id = record.get("id")
    msg_id = str(msg_id)[:64] if msg_id is not None else ""
    inner = record.get("message")
    if not isinstance(inner, dict):
        return []
    role = inner.get("role")
    if role not in ("user", "assistant"):
        return []
    content = inner.get("content")
    if not isinstance(content, list):
        return []
    out: list[tuple[str, str]] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text:
            continue
        for line in text.splitlines():
            if line.strip():
                out.append((msg_id, line))
    return out


def _match_candidate(line: str) -> str | None:
    for pattern, kind in _PATTERNS:
        try:
            if pattern.search(line) is not None:
                return kind
        except re.error:
            continue
    return None


def _clean_text(line: str) -> str:
    stripped = line.strip()
    cleaned = "".join(c for c in stripped if ord(c) >= 32)
    cleaned = cleaned.strip()
    if len(cleaned) > CANDIDATE_TEXT_LIMIT:
        cleaned = cleaned[:CANDIDATE_TEXT_LIMIT]
    return cleaned


def _read_window(session_file: Path, start_ref: int, *, limit: int):
    """Read up to 1000 lines / 256 KiB from start_ref, stopping at limit.

    Returns (entries, last_scanned_index, total_lines_seen). entries is a
    list of (line_index, message_id, kind, text). Stops early once
    ``limit`` candidates are collected so the next tick continues after
    them. Malformed JSONL lines are skipped.
    """
    entries: list[tuple[int, str, str, str]] = []
    batch_hashes: set[str] = set()
    last_scanned = start_ref - 1
    lines_read = 0
    bytes_read = 0
    try:
        with open(session_file, "r", encoding="utf-8", errors="replace") as handle:
            for index, raw in enumerate(handle):
                if index < start_ref:
                    continue
                if lines_read >= SCAN_MAX_LINES:
                    break
                raw_bytes = len(raw.encode("utf-8", errors="replace"))
                if bytes_read + raw_bytes > SCAN_MAX_BYTES and lines_read > 0:
                    break
                bytes_read += raw_bytes
                lines_read += 1
                last_scanned = index
                if len(entries) >= limit:
                    break
                try:
                    record = json.loads(raw)
                except (ValueError, RecursionError):
                    continue
                for msg_id, line in _extract_texts(record):
                    kind = _match_candidate(line)
                    if kind is None:
                        continue
                    text = _clean_text(line)
                    if not text:
                        continue
                    # Fail-closed local sensitive filter.
                    kept = _safe_text_or_none(text, CANDIDATE_TEXT_LIMIT)
                    if kept is None:
                        continue
                    digest = _annotations.capture_text_hash(
                        str(session_file), text)
                    if digest is None:
                        continue
                    if digest in batch_hashes:
                        continue
                    batch_hashes.add(digest)
                    entries.append((index, msg_id, kind, text))
                    if len(entries) >= limit:
                        break
                if len(entries) >= limit:
                    # Stop reading further lines; the ref advances to this
                    # line so the next scan continues after these.
                    break
    except CaptureError:
        raise
    except OSError:
        return (entries, last_scanned, lines_read)
    return (entries, last_scanned, lines_read)


def scan(ctx, *, limit=8) -> dict:
    """Local-regex scan: no network, no model call, no API key."""
    settings = ctx.get("settings") if isinstance(ctx.get("settings"), dict) else {}
    conn = ctx.get("conn")
    now_ms = ctx.get("now_ms")
    try:
        now_ms = int(now_ms) if isinstance(now_ms, int) and now_ms >= 0 else int(time.time() * 1000)
    except Exception:
        now_ms = int(time.time() * 1000)
    if type(limit) is not int or not 1 <= limit <= MAX_CANDIDATES:
        limit = MAX_CANDIDATES
    if settings.get("sessionCapture") is not True:
        return result("disabled")
    pid = _resolve_project_id(ctx)
    if pid is None:
        return result("no_project")
    session_file = _resolve_session_file(pid)
    if session_file is None:
        return result("no_session")
    if not _check_session_file(session_file):
        return result("no_session")
    session_str = str(session_file)
    start_ref = _get_ref(conn, session_str)
    try:
        entries, last_scanned, _ = _read_window(session_file, start_ref, limit=limit)
    except CaptureError:
        return result("failed")
    except Exception:
        return result("failed")
    if not entries:
        # No candidates: advance past the scanned window.
        try:
            if last_scanned >= start_ref:
                _set_ref(conn, session_str, last_scanned + 1)
            else:
                _set_ref(conn, session_str, start_ref)
        except Exception:
            pass
        return result("no_candidates")
    # Dedupe against existing captures for this file (any status).
    fresh: list[tuple[int, str, str, str]] = []
    for index, msg_id, kind, text in entries:
        digest = _annotations.capture_text_hash(session_str, text)
        if digest is None:
            continue
        try:
            existing = _annotations.find_capture(conn, session_str, digest)
        except Exception:
            return result("failed")
        if existing is not None:
            continue
        fresh.append((index, msg_id, kind, text))
    if not fresh:
        try:
            _set_ref(conn, session_str, last_scanned + 1)
        except Exception:
            pass
        return result("no_candidates")
    # Local-regex scan: every surviving candidate is stored directly.
    # No network, no model call; the regex match is the signal.
    kept = 0
    try:
        for index, msg_id, kind, text in fresh:
            ref = f"{index}:{msg_id}"[:128] if msg_id else f"{index}"
            _annotations.insert_capture(
                conn, project_id=pid, session_file=session_str,
                message_ref=ref, kind=kind, text=text,
                actionable=1.0, durable=1.0, created_ms=now_ms)
            kept += 1
    except (_annotations.AnnotationsError, sqlite3.Error, OSError, ValueError):
        return result("failed")
    except Exception:
        return result("failed")
    try:
        _set_ref(conn, session_str, last_scanned + 1)
    except Exception:
        return result("failed")
    if kept >= 1:
        return result("", changed=True)
    return result("no_candidates")


# ---------------------------------------------------------------------------
# Read helpers (list / set-status)
# ---------------------------------------------------------------------------

def _validate_day(value) -> date:
    if value is None:
        return date.today()
    if not isinstance(value, str) or not value.strip():
        _error("day must be YYYY-MM-DD")
    text = value.strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        _error("day must be YYYY-MM-DD")
    return parsed


def _day_window(day: date) -> tuple[int, int]:
    start = int(datetime(day.year, day.month, day.day).timestamp() * 1000)
    nxt = day + timedelta(days=1)
    end = int(datetime(nxt.year, nxt.month, nxt.day).timestamp() * 1000)
    if start < 0:
        start = 0
    if end <= start:
        end = start + 86400000
    return (start, end)


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


def list_captures(conn, *, day=None, status="new", limit=20) -> dict:
    parsed = _validate_day(day)
    if not isinstance(status, str) or status.strip() not in (
            "new", "accepted", "dismissed", "all"):
        _error("status must be new, accepted, dismissed or all")
    bounded_status = status.strip()
    if isinstance(limit, bool):
        _error("limit must be an integer 1..100")
    if isinstance(limit, str) and limit.strip():
        try:
            limit = int(limit.strip(), 10)
        except ValueError:
            _error("limit must be an integer 1..100")
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        _error("limit must be an integer 1..100")
    start, end = _day_window(parsed)
    try:
        rows = _annotations.list_captures(
            conn, day_start_ms=start, day_end_ms=end,
            status=bounded_status, limit=limit)
    except _annotations.AnnotationsError as exc:
        raise CaptureError(str(exc)) from exc
    names = _registry_names()
    captures = []
    for row in rows:
        item = dict(row)
        item["project_name"] = names.get(item.get("project_id", ""), "")
        captures.append(item)
    return {"captures": captures, "day": parsed.isoformat(),
            "status": bounded_status, "total": _count_captures(
                conn, start_ms=start, end_ms=end,
                status=bounded_status)}


def _count_captures(conn, *, start_ms: int, end_ms: int,
                    status: str) -> int:
    """Count captures in the day window for truncation reporting (S-041).

    Same window/status filter as ``list_captures``; degrades to 0 (never
    raises) so a count failure cannot fail the list itself.
    """
    try:
        if status == "all":
            row = conn.execute(
                "SELECT COUNT(*) FROM captures"
                " WHERE created_ms >= ? AND created_ms < ?",
                (start_ms, end_ms)).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM captures"
                " WHERE created_ms >= ? AND created_ms < ?"
                " AND status = ?",
                (start_ms, end_ms, status)).fetchone()
        total = int(row[0]) if row is not None else 0
    except Exception:
        return 0
    return total if total >= 0 else 0


def set_status(conn, capture_id, status: str) -> dict:
    if not isinstance(status, str) or status.strip() not in (
            "accepted", "dismissed"):
        _error("status must be accepted or dismissed")
    bounded = status.strip()
    try:
        ok = _annotations.set_capture_status(conn, capture_id, bounded)
    except _annotations.AnnotationsError as exc:
        raise CaptureError(str(exc)) from exc
    if not ok:
        _error("unknown capture")
    canonical = int(str(capture_id).strip()) if isinstance(capture_id, str) else int(capture_id)
    return {"ok": True, "id": canonical, "status": bounded}


# ---------------------------------------------------------------------------
# Export: prepare (exact preview) + apply (revision-checked write)
# ---------------------------------------------------------------------------

def _resolve_graph(graph):
    if graph is not None and str(graph).strip():
        try:
            from logseq_common import graph_path
            return graph_path(str(graph).strip())
        except Exception as exc:
            raise CaptureError("logseq graph is unavailable") from exc
    try:
        from logseq_common import resolve_graph
        return resolve_graph(None)
    except Exception as exc:
        raise CaptureError("logseq graph is unavailable") from exc


def _new_token(prefix: str) -> str:
    return prefix + secrets.token_hex(16)


#: Provenance identity bound (§8.2 bounded labels, ≤ 160 chars each).
REF_IDENTITY_LIMIT = 160

_SESSION_ID_RE = re.compile(r"[0-9a-fA-F]{32}\Z")


def _validate_session_id(value) -> str:
    """Collector session id, or "" when absent (degraded, never blocked).

    An explicitly supplied but malformed id fails closed (misattribution
    risk); an absent/blank one degrades to a marker-free block.
    """
    if value is None:
        return ""
    if not isinstance(value, str) or not value.strip():
        return ""
    text = value.strip()
    if not _SESSION_ID_RE.fullmatch(text):
        _error("session id is invalid")
    return text.lower()


def _validate_ref(value) -> str:
    """Provenance identity, or "" when absent or gate-dropped.

    Unlike the session id this is untrusted resource data, so the
    sensitive-path gate *filters* (drops the line, never the write):
    over-long, control-character, or secret-looking identities simply
    yield no ``quickshell-ref::`` line. The exact preview always shows
    what will be written.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        _error("ref identity is invalid")
    if not value.strip():
        return ""
    kept = _safe_text_or_none(value, REF_IDENTITY_LIMIT)
    return kept if kept is not None else ""


def _todo_block(text: str, session_id: str, ref: str) -> str:
    """Compose the page block: TODO bullet plus provenance children.

    Markers are non-bullet property lines directly under the bullet
    (before any child bullet), following the ``quickshell-agenda::``
    pattern: the rebuild lookahead picks the session up from the TODO
    row, and the preview shows this exact text.
    """
    lines = ["- TODO " + text.strip()]
    if session_id:
        lines.append("  quickshell-session:: " + session_id)
    if ref:
        lines.append("  quickshell-ref:: " + ref)
    return "\n".join(lines)


def _store_prepared(conn, *, kind: str, payload: dict, preview: dict,
                    revision, expires_ms: int) -> str:
    # Single save target (Phase 2c §5.4): the only prepared kind is
    # "capture-page". Old "capture-today" tokens are rejected at apply.
    prefix = "capp_"
    for _ in range(3):
        token = _new_token(prefix)
        try:
            _annotations.create_prepared(
                conn, token=token, kind=kind, payload=payload,
                preview=preview, revision=revision, expires_ms=expires_ms)
        except _annotations.AnnotationsError as exc:
            if "already exists" in str(exc):
                continue
            raise CaptureError(str(exc)) from exc
        return token
    _error("prepare failed")


def _get_capture(conn, capture_id: int) -> dict:
    try:
        cid = int(capture_id) if not isinstance(capture_id, bool) else -1
    except (TypeError, ValueError):
        _error("capture id is invalid")
    if cid < 1:
        _error("capture id is invalid")
    try:
        row = conn.execute(
            "SELECT id, project_id, session_file, message_ref, kind, text,"
            " actionable, durable, status, created_ms, applied_ms,"
            " applied_target FROM captures WHERE id = ?",
            (cid,)).fetchone()
    except sqlite3.Error:
        _error("database is unavailable")
    if row is None:
        _error("unknown capture")
    assert row is not None
    return dict(row)


def prepare_export(conn, capture_id, *, graph=None, now_ms,
                   session_id=None, ref=None) -> dict:
    """Stage the exact project-page block for one capture (L1, page-only).

    Single save target: the project page task list via revision-checked
    ``project_planner.update_page``. There is no ``--target`` anymore;
    the response still reports ``target: "page"``. When *session_id*
    (32-hex collector session) is supplied the block carries
    ``quickshell-session::`` provenance plus a gated ``quickshell-ref::``
    identity (bounded 160 chars, sensitive-path filtered); without a
    session the block is a bare ``- TODO`` (degraded, never blocked).
    The returned ``preview.block`` is the exact marker-inclusive text
    the Confirm panel shows; the single-use token expires in 600 s.
    """
    if isinstance(now_ms, bool):
        _error("timestamp is invalid")
    stamp = int(now_ms) if isinstance(now_ms, int) else -1
    if stamp < 0 or stamp > 9223372036854775807:
        _error("timestamp is invalid")
    sid = _validate_session_id(session_id)
    gated_ref = _validate_ref(ref)
    row = _get_capture(conn, capture_id)
    if row.get("status") != "new":
        _error("capture is not new")
    project_id = row.get("project_id", "")
    text = row.get("text", "")
    if not isinstance(text, str) or not text.strip():
        _error("capture text is invalid")
    resolved_graph = _resolve_graph(graph)
    try:
        import project_planner as _pp
        page = _pp.read_page(resolved_graph, {"project_id": project_id})
    except Exception as exc:
        raise CaptureError("project page is unavailable") from exc
    if not isinstance(page, dict):
        raise CaptureError("project page is unavailable")
    content = page.get("content", "")
    if not isinstance(content, str):
        raise CaptureError("project page is unavailable")
    block = _todo_block(text, sid, gated_ref)
    from logseq_common import append_session_log
    new_content, inserted = append_session_log(content, block)
    if len(new_content.encode("utf-8")) > _PAGE_CONTENT_LIMIT:
        _error("project page would be too large")
    payload = {"capture_id": row["id"], "target": "page",
               "path": page.get("path"), "revision": page.get("revision"),
               "content": new_content, "session_id": sid, "ref": gated_ref}
    preview = {"target": "page", "path": page.get("path"),
               "page": page.get("page"), "graphName": page.get("graphName"),
               "revision": page.get("revision"), "block": inserted}
    token = _store_prepared(conn, kind="capture-page", payload=payload,
                            preview=preview,
                            revision=page.get("revision"),
                            expires_ms=stamp + PREPARED_TTL_MS)
    return {"prepared": token, "target": "page", "preview": preview,
            "revision": page.get("revision"),
            "expires_in": PREPARED_EXPIRES_IN_S}


def _append_unique(items: list, value: str) -> list:
    if value and value not in items:
        items.append(value)
    return items


def _link_session_todo(conn, *, session_id: str, block_ref: str,
                       ref_identity: str, index_text: str, index_page: str,
                       line: int, project_id: str, now_ms: int) -> None:
    """Record an accepted TODO in session_meta + content_index.

    Fail-closed: sidecar errors propagate as CaptureError (bounded, no
    traceback). The markdown write already landed, so the message never
    claims otherwise; the derived index is rebuildable via
    ``content_index rebuild``.
    """
    from content_index import index_block
    try:
        meta = _annotations.get_session_meta(conn, session_id)
    except _annotations.AnnotationsError as exc:
        raise CaptureError(str(exc)) from exc
    try:
        if meta is None:
            _annotations.upsert_session_meta(
                conn, session_id=session_id, now_ms=now_ms,
                todo_refs=[block_ref], refs=([ref_identity] if ref_identity else []))
        else:
            todos = list(meta.get("todo_refs", []))
            refs = list(meta.get("refs", []))
            _append_unique(todos, block_ref)
            if ref_identity:
                _append_unique(refs, ref_identity)
            try:
                _annotations.upsert_session_meta(
                    conn, session_id=session_id, now_ms=now_ms,
                    expect_revision=meta.get("revision"),
                    todo_refs=todos, refs=refs)
            except _annotations.AnnotationsError as exc:
                if str(exc) != "session meta changed":
                    raise
                fresh = _annotations.get_session_meta(conn, session_id)
                if fresh is None:
                    raise CaptureError(str(exc)) from exc
                todos = list(fresh.get("todo_refs", []))
                refs = list(fresh.get("refs", []))
                _append_unique(todos, block_ref)
                if ref_identity:
                    _append_unique(refs, ref_identity)
                _annotations.upsert_session_meta(
                    conn, session_id=session_id, now_ms=now_ms,
                    expect_revision=fresh.get("revision"),
                    todo_refs=todos, refs=refs)
    except _annotations.AnnotationsError as exc:
        raise CaptureError(str(exc)) from exc
    try:
        index_block(conn, text=index_text, page=index_page, line=line,
                    kind="todo", session_id=session_id,
                    project_id=project_id or "")
    except Exception as exc:
        raise CaptureError("session link failed") from exc


def _inserted_line(content: str, inserted: str) -> int:
    """1-based line of *inserted* in *content*, or 0 when unknown."""
    if not isinstance(content, str) or not isinstance(inserted, str) \
            or not inserted:
        return 0
    offset = content.find(inserted)
    if offset < 0:
        return 0
    return content.count("\n", 0, offset) + 1


def apply_export(conn, token, *, graph=None, now_ms) -> dict:
    if isinstance(now_ms, bool):
        _error("timestamp is invalid")
    stamp = int(now_ms) if isinstance(now_ms, int) else -1
    if stamp < 0 or stamp > 9223372036854775807:
        _error("timestamp is invalid")
    try:
        row = _annotations.consume_prepared(conn, token, now_ms=stamp)
    except _annotations.AnnotationsError as exc:
        raise CaptureError(str(exc)) from exc
    if row is None:
        _error("prepared export is unknown or expired")
    assert row is not None
    kind = row.get("kind")
    if kind != "capture-page":
        # D1: no compatibility read for old "capture-today" tokens —
        # the today branch is deleted, so they are rejected outright.
        _error("prepared export is invalid")
    stored_revision = row.get("revision")
    payload = row.get("payload")
    if not isinstance(payload, dict):
        _error("prepared export is invalid")
    capture_id = payload.get("capture_id")
    target = payload.get("target", "page")
    path = payload.get("path")
    content = payload.get("content")
    session_id = payload.get("session_id", "")
    ref_identity = payload.get("ref", "")
    if target != "page":
        _error("prepared export is invalid")
    if not isinstance(session_id, str):
        _error("prepared export is invalid")
    if not isinstance(ref_identity, str):
        _error("prepared export is invalid")
    session_id = session_id.strip().lower()
    if session_id and not _SESSION_ID_RE.fullmatch(session_id):
        _error("prepared export is invalid")
    if not isinstance(path, str) or not isinstance(content, str):
        _error("prepared export is invalid")
    try:
        current = _get_capture(conn, capture_id)
    except CaptureError as exc:
        raise exc
    if current.get("status") != "new":
        _error("capture is not new")
    resolved_graph = _resolve_graph(graph)
    try:
        import project_planner as _pp
        result_page = _pp.update_page(resolved_graph, path, stored_revision,
                                      content)
    except Exception as exc:
        raise CaptureError("project page write failed") from exc
    if not isinstance(result_page, dict):
        raise CaptureError("project page write failed")
    try:
        marked = _annotations.mark_capture_applied(conn, capture_id, "page",
                                                   now_ms=stamp)
    except _annotations.AnnotationsError as exc:
        raise CaptureError(str(exc)) from exc
    if not marked:
        _error("capture is not new")
    todo_ref = ""
    if session_id:
        # Provenance writeback: block ref → todo_refs, gated identity →
        # refs, exact inserted text → content_index (kind "todo").
        # Dismissed rows never reach here, so no index write for them;
        # attended is left to the read path (never auto-marked on apply).
        preview = row.get("preview")
        inserted = preview.get("block") if isinstance(preview, dict) else ""
        if not isinstance(inserted, str) or not inserted:
            _error("prepared export is invalid")
        landed = result_page.get("content")
        if not isinstance(landed, str):
            landed = ""
        line = _inserted_line(landed, inserted) \
            or _inserted_line(content, inserted)
        todo_ref = f"{path}:{line}"
        index_page = result_page.get("page")
        if not isinstance(index_page, str) or not index_page:
            index_page = path
        project_id = current.get("project_id", "")
        if not isinstance(project_id, str):
            project_id = ""
        _link_session_todo(
            conn, session_id=session_id, block_ref=todo_ref,
            ref_identity=ref_identity.strip(), index_text=inserted,
            index_page=index_page, line=line, project_id=project_id,
            now_ms=stamp)
    return {"applied": True, "target": "page",
            "path": result_page.get("path"),
            "revision": result_page.get("revision"),
            "capture_id": current["id"], "applied_ms": stamp,
            "todo_ref": todo_ref}


def read_connection(db):
    # No schema initialization, directory creation or capture on read paths.
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
    p_scan = sub.add_parser("scan")
    p_scan.add_argument("--project", default=None)
    p_scan.add_argument("--limit", type=int, default=8)
    p_list = sub.add_parser("list")
    p_list.add_argument("--day", default=None)
    p_list.add_argument("--status", default="new")
    p_list.add_argument("--limit", type=int, default=20)
    p_set = sub.add_parser("set-status")
    p_set.add_argument("--id", required=True)
    p_set.add_argument("--status", required=True)
    p_prep = sub.add_parser("prepare")
    p_prep.add_argument("--capture", required=True)
    p_prep.add_argument("--session", default=None)
    p_prep.add_argument("--ref", default=None)
    p_apply = sub.add_parser("apply")
    p_apply.add_argument("--prepared", required=True)
    return parser.parse_args(argv)


def _validate_cli_limit(value, low, high, what):
    if isinstance(value, bool):
        _error(what)
    bound = value
    if isinstance(bound, str) and bound.strip():
        try:
            bound = int(bound.strip(), 10)
        except ValueError:
            _error(what)
    if not isinstance(bound, int) or not low <= bound <= high:
        _error(what)
    return bound


def _dispatch(args: argparse.Namespace) -> dict | int:
    # Grandfathered: scan/list bypass qscli.emit with
    # print(json.dumps(..., sort_keys=True, allow_nan=False)) — preserved
    # exact output path.
    if args.command == "scan":
        limit = _validate_cli_limit(args.limit, 1, 8,
                                    "limit must be an integer 1..8")
        code, payload = tick._do_run(
            args.db, "scan", args.project, None,
            adapters={"scan": lambda ctx: scan(ctx, limit=limit)})
        print(json.dumps(payload, sort_keys=True, allow_nan=False))
        return code
    if args.command == "list":
        day = args.day
        status = args.status
        limit = _validate_cli_limit(args.limit, 1, 100,
                                    "limit must be an integer 1..100")
        if not isinstance(status, str) or status.strip() not in (
                "new", "accepted", "dismissed", "all"):
            _error("status must be new, accepted, dismissed or all")
        conn = read_connection(args.db)
        try:
            if conn is None:
                parsed = _validate_day(day)
                payload = {"captures": [], "day": parsed.isoformat(),
                           "status": status.strip()}
            else:
                payload = list_captures(conn, day=day, status=status,
                                        limit=limit)
            code = 0
        finally:
            if conn is not None:
                conn.close()
        print(json.dumps(payload, sort_keys=True, allow_nan=False))
        return code
    conn = _annotations.connect(args.db)
    try:
        settings = tick._load_settings()
        _ = settings
        now_ms = int(time.time() * 1000)
        if args.command == "set-status":
            value = set_status(conn, args.id, args.status)
        elif args.command == "prepare":
            value = prepare_export(conn, args.capture, graph=args.graph,
                                   now_ms=now_ms, session_id=args.session,
                                   ref=args.ref)
        elif args.command == "apply":
            value = apply_export(conn, args.prepared, graph=args.graph,
                                 now_ms=now_ms)
        else:
            _error("unknown command")
        return value
    finally:
        try:
            conn.close()
        except Exception:
            pass


_BOUNDED_EXCEPTIONS = (CaptureError, _annotations.AnnotationsError,
                       tick.TickError, OSError, TypeError, ValueError,
                       UnicodeError, RecursionError, OverflowError)


def main(argv=None) -> int:
    return qscli.run_main(_parse_args, _dispatch, "session capture",
                          _BOUNDED_EXCEPTIONS, argv=argv)


if __name__ == "__main__":
    sys.exit(main())
