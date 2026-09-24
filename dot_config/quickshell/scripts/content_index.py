#!/usr/bin/env python3
"""Derived full-text index over thought and TODO block text (Phase 2c, §5.5).

``content_index`` is an FTS5 table in ``annotations.db`` (never a source
of truth — dropping it costs only a rescan). It is populated two ways:

- incrementally, one INSERT per successful write via :func:`index_block`
  (the exact block text is already in hand at ``apply`` time — the
  thought/TODO write paths call this helper, never a rescan);
- wholesale via ``rebuild`` (this CLI), which rescans the journal +
  project pages for ``quickshell-session::`` / ``quickshell-ref::`` /
  ``quickshell-at::`` marker blocks and TODO lines and refills the table
  idempotently.

Read-only on the graph: rebuild never writes markdown. Bounded: file
count/size caps come from ``logseq_common.markdown_files`` (5000 files,
4 MiB each), rows are capped at ``MAX_INDEX_ROWS``, texts at
``INDEX_TEXT_LIMIT``.

Conventions match the other helpers: JSON stdout, single-line
``error: ...`` on stderr with exit 1, no tracebacks, no state content in
diagnostics, list-form argv with ``shell=False`` by callers.
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
import logseq_common as _graph
import qscli


class ContentIndexError(ValueError):
    pass


def _error(message: str) -> NoReturn:
    raise ContentIndexError(message)


INPUT_LIMIT = 1024 * 1024
# Transport cap; single source of truth is qscli.INPUT_LIMIT (same value).
INDEX_TEXT_LIMIT = 4000
INDEX_PAGE_LIMIT = 512
INDEX_KINDS = ("thought", "todo")
MAX_INDEX_ROWS = 20000
MAX_SCAN_LINES_PER_FILE = 20000
REGISTRY_ENTRY_LIMIT = 10000

_SESSION_RE = re.compile(r"quickshell-session::\s*([0-9a-fA-F]{32})\b")
_MARKER_ANY_RE = re.compile(r"quickshell-(?:session|ref|at)::")
_BULLET_RE = re.compile(r"^(?:[ \t]*[-*+][ \t]+)(.*)$")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("index text is invalid")
    assert isinstance(value, str)
    text = value.strip()
    if len(text) > INDEX_TEXT_LIMIT or "\x00" in text:
        _error("index text is invalid")
    if any(ord(c) < 32 and c not in ("\n", "\t") for c in text):
        _error("index text is invalid")
    return text


def _validate_page(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("index page is invalid")
    text = value.strip()
    if len(text) > INDEX_PAGE_LIMIT or "\x00" in text:
        _error("index page is invalid")
    if any(ord(c) < 32 for c in text):
        _error("index page is invalid")
    return text


def _validate_line(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        _error("index line is invalid")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = int(value.strip(), 10)
        except ValueError:
            _error("index line is invalid")
    else:
        _error("index line is invalid")
    if parsed < 0 or parsed > 2**31 - 1:
        _error("index line is invalid")
    return parsed


def _validate_kind(value: object) -> str:
    if not isinstance(value, str) or value.strip() not in INDEX_KINDS:
        _error("index kind is invalid")
    return value.strip()


def _validate_session_opt(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str) and not value.strip():
        return ""
    if not isinstance(value, str):
        _error("session id is invalid")
    text = value.strip()
    if len(text) != 32 or any(c not in "0123456789abcdefABCDEF"
                              for c in text):
        _error("session id is invalid")
    return text.lower()


def _validate_project_opt(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str) and not value.strip():
        return ""
    if not isinstance(value, str):
        _error("project id is invalid")
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        _error("project id is invalid")
    raise AssertionError("unreachable")


def _validate_graph_value(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if "\x00" in text or len(text) > 4096:
        _error("graph is not accessible")
    return text


# ---------------------------------------------------------------------------
# Read helper: literal full-text search (sessions.py union reads this)
# ---------------------------------------------------------------------------

SEARCH_DEFAULT_LIMIT = 20
SEARCH_MAX_LIMIT = 1000


def _validate_search_query(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("query is invalid")
    text = value.strip()
    if "\x00" in text or len(text) > 256:
        _error("query is invalid")
    return text


def _validate_search_limit(value: object) -> int:
    if value is None:
        return SEARCH_DEFAULT_LIMIT
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
    if parsed < 1 or parsed > SEARCH_MAX_LIMIT:
        _error("limit is invalid")
    return parsed


def _fts_literal(query: str) -> str:
    """Quote user text as literal FTS5 phrase tokens (retrieval rules).

    User text is split on whitespace and each token is double-quote
    quoted (``"`` doubled), joined with ``AND`` — the same
    escaped/literal discipline as ``activity_fts`` retrieval
    (``docs/desktop-history-search.md``): user FTS syntax cannot
    inject operators. Never interpolate raw user text into MATCH.
    """
    tokens = query.split()
    if not tokens:
        _error("query is invalid")
    return " AND ".join(
        '"' + token.replace('"', '""') + '"' for token in tokens)


def search_content(conn: sqlite3.Connection, query: str,
                   limit: int | None = None) -> list[dict]:
    """Literal full-text search over ``content_index`` (bounded).

    Returns at most ``limit`` rows as
    ``{text, page, line, kind, session_id, project_id}`` dicts in FTS5
    ``rank`` order (best match first). The MATCH expression is built
    with :func:`_fts_literal` and bound as a parameter — user text is
    never interpolated. A missing/misshapen table or any SQLite
    failure reads as :class:`ContentIndexError` (``content index is
    unavailable``) so the ``sessions.py`` union degrades to
    collector-only; bad input raises ``query is invalid`` /
    ``limit is invalid``. Read-only: no schema creation, safe on the
    read-path connection.
    """
    if conn is None:
        _error("database is unavailable")
    text = _validate_search_query(query)
    bound = _validate_search_limit(limit)
    expression = _fts_literal(text)
    try:
        rows = conn.execute(
            "SELECT text, page, line, kind, session_id, project_id"
            " FROM content_index WHERE content_index MATCH ?"
            " ORDER BY rank LIMIT ?",
            (expression, bound)).fetchall()
    except sqlite3.Error:
        _error("content index is unavailable")
    out: list[dict] = []
    for row in rows:
        try:
            body = row["text"]
            page = row["page"]
            line = row["line"]
            kind = row["kind"]
            session_id = row["session_id"]
            project_id = row["project_id"]
        except (IndexError, KeyError, TypeError):
            try:
                body, page, line, kind, session_id, project_id = (
                    row[0], row[1], row[2], row[3], row[4], row[5])
            except (IndexError, KeyError, TypeError):
                continue
        out.append({
            "text": body if isinstance(body, str) else "",
            "page": page if isinstance(page, str) else "",
            "line": line if isinstance(line, int)
            and not isinstance(line, bool) else 0,
            "kind": kind if isinstance(kind, str) else "",
            "session_id": session_id
            if isinstance(session_id, str) else "",
            "project_id": project_id
            if isinstance(project_id, str) else "",
        })
        if len(out) >= bound:
            break
    return out


# ---------------------------------------------------------------------------
# Per-apply INSERT helper (write paths call this; never a rescan)
# ---------------------------------------------------------------------------

def index_block(conn: sqlite3.Connection, *, text: str, page: str,
                line: int = 0, kind: str,
                session_id: str = "", project_id: str = "") -> int:
    """Insert one thought/TODO block into ``content_index``; return rowid.

    Import path: ``from content_index import index_block``. The caller
    holds the exact block text at ``apply`` time, so no graph read is
    needed. ``kind`` is ``thought`` or ``todo``; ``line`` is the 1-based
    markdown line (0 when unknown); ``session_id``/``project_id`` are
    optional ("" when unattributed). Bad input raises
    :class:`ContentIndexError` with a bounded message; the indexed text
    stays in the local sidecar (verbatim, like ``activity.db`` paths) and
    never leaves the machine through this helper.
    """
    if conn is None:
        _error("database is unavailable")
    body = _validate_text(text)
    bounded_page = _validate_page(page)
    bounded_line = _validate_line(line)
    bounded_kind = _validate_kind(kind)
    bounded_session = _validate_session_opt(session_id)
    bounded_project = _validate_project_opt(project_id)
    try:
        conn.execute(_annotations.CONTENT_INDEX_SQL)
        cursor = conn.execute(
            "INSERT INTO content_index(text, page, line, kind,"
            " session_id, project_id) VALUES(?, ?, ?, ?, ?, ?)",
            (body, bounded_page, bounded_line, bounded_kind,
             bounded_session, bounded_project))
        conn.commit()
    except sqlite3.Error:
        _error("database is unavailable")
    return int(cursor.lastrowid)


# ---------------------------------------------------------------------------
# Rebuild (rescan the graph; read-only on markdown)
# ---------------------------------------------------------------------------

def _project_map() -> dict:
    """Map graph-relative page paths (lowercased) to registry project ids.

    Best-effort and bounded: any registry failure yields an empty map
    (rows index with ``project_id`` "") rather than failing the rebuild.
    """
    try:
        import projects as _projects
        data = _projects.list_projects()
    except Exception:
        return {}
    entries = data.get("projects", []) if isinstance(data, dict) else []
    if not isinstance(entries, list):
        return {}
    out: dict[str, str] = {}
    for entry in entries[:REGISTRY_ENTRY_LIMIT]:
        if not isinstance(entry, dict):
            continue
        pid = entry.get("id")
        rel = entry.get("logseq_path")
        if isinstance(pid, str) and pid.strip() \
                and isinstance(rel, str) and rel.strip():
            try:
                canonical = str(uuid.UUID(pid.strip()))
            except ValueError:
                continue
            out[rel.strip().lower()] = canonical
    return out


def _session_in_lines(lines: list[str], start: int, stop: int) -> str:
    for index in range(start, stop):
        if 0 <= index < len(lines):
            match = _SESSION_RE.search(lines[index])
            if match is not None:
                return match.group(1).lower()
    return ""


def _rows_for_lines(lines: list[str], *, page: str,
                    project_id: str) -> list[tuple]:
    """Extract index rows from one page's lines (bounded, fence-aware)."""
    try:
        protected = _graph._fence_protected(lines)
    except Exception:
        protected = [False] * len(lines)
    rows: list[tuple] = []
    total = len(lines)
    for index, line in enumerate(lines):
        if index >= MAX_SCAN_LINES_PER_FILE:
            break
        if protected[index]:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if _graph.TASK.match(line) is not None:
            # Property lines (session/ref markers) sit just below the
            # bullet; look ahead so the TODO row carries its session.
            found = _SESSION_RE.search(line)
            sid = found.group(1).lower() if found else ""
            if not sid:
                sid = _session_in_lines(lines, index + 1,
                                        min(index + 3, total))
            rows.append((stripped[:INDEX_TEXT_LIMIT], page, index + 1,
                         "todo", sid, project_id))
        elif _MARKER_ANY_RE.search(line):
            bullet = _BULLET_RE.match(line)
            if bullet is not None:
                # A bulleted marker line that is not itself a task (task
                # lines are handled above): index it as a thought.
                found = _SESSION_RE.search(line)
                sid = found.group(1).lower() if found else ""
                rows.append((stripped[:INDEX_TEXT_LIMIT], page, index + 1,
                             "thought", sid, project_id))
                continue
            # Bare property line: attach to the nearest parent bullet so
            # the row carries readable text, not just the marker. When
            # the parent is itself a TODO it was already indexed above
            # (with this marker's session via lookahead) — skip.
            parent_body = ""
            parent_task = False
            for back in range(index - 1, max(index - 4, -1), -1):
                if protected[back] or not lines[back].strip():
                    continue
                match = _BULLET_RE.match(lines[back])
                if match is None:
                    break
                parent_body = match.group(1).strip()
                parent_task = _graph.TASK.match(lines[back]) is not None
                break
            if parent_task:
                continue
            found = _SESSION_RE.search(line)
            sid = found.group(1).lower() if found else ""
            if not sid and parent_body:
                sid = _session_in_lines(lines, max(index - 3, 0), index)
            if parent_body:
                combined = (parent_body + " / " + stripped)[:INDEX_TEXT_LIMIT]
                rows.append((combined, page, index + 1, "thought", sid,
                             project_id))
            else:
                rows.append((stripped[:INDEX_TEXT_LIMIT], page, index + 1,
                             "thought", sid, project_id))
        if len(rows) >= MAX_INDEX_ROWS:
            break
    return rows


def _refill(conn: sqlite3.Connection,
            rows: list[tuple]) -> None:
    verdict = _annotations.check_content_index(conn)
    if not verdict["ok"]:
        # Derived index: a misshapen table is safe to drop and recreate
        # (worst case is a rescan, never data loss).
        try:
            conn.execute("DROP TABLE IF EXISTS content_index")
            conn.execute(_annotations.CONTENT_INDEX_SQL)
            conn.commit()
        except sqlite3.Error:
            _error("database is unavailable")
        verdict = _annotations.check_content_index(conn)
        if not verdict["ok"]:
            _error("content index is unavailable")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM content_index")
        if rows:
            conn.executemany(
                "INSERT INTO content_index(text, page, line, kind,"
                " session_id, project_id) VALUES(?, ?, ?, ?, ?, ?)",
                rows)
        conn.execute("COMMIT")
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        _error("database is unavailable")


def rebuild(db=None, graph=None) -> dict:
    """Rescan journal + project pages and refill ``content_index``.

    Idempotent (full refill in one transaction), bounded
    (``MAX_INDEX_ROWS`` rows), read-only on the graph. Returns
    ``{"ok": True, "files": N, "rows": M, "truncated": bool}``.
    """
    bounded_graph = _validate_graph_value(graph)
    try:
        root = _graph.resolve_graph(bounded_graph)
    except _graph.GraphError as exc:
        _error(str(exc))
        raise AssertionError("unreachable")
    conn = _annotations.connect(db)
    try:
        page_projects = _project_map()
        collected: list[tuple] = []
        files = 0
        truncated = False
        for path in _graph.markdown_files(root):
            files += 1
            lines = _graph.read_lines(path)
            if lines is None:
                continue
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                continue
            try:
                page = _graph.page_name(path, root)
            except ValueError:
                continue
            project_id = ""
            if not relative.lower().startswith("journals/"):
                project_id = page_projects.get(relative.lower(), "")
            for row in _rows_for_lines(lines, page=page,
                                       project_id=project_id):
                collected.append(row)
                if len(collected) >= MAX_INDEX_ROWS:
                    truncated = True
                    break
            if truncated:
                break
        _refill(conn, collected)
        return {"ok": True, "files": files, "rows": len(collected),
                "truncated": truncated}
    finally:
        try:
            conn.close()
        except Exception:
            pass


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
    sub.add_parser("rebuild")
    sub.add_parser("check")
    return parser.parse_args(argv if argv is not None else sys.argv[1:])


def _dispatch(args: argparse.Namespace) -> dict:
    if args.command == "rebuild":
        return rebuild(args.db, args.graph)
    if args.command == "check":
        conn = _annotations.connect(args.db)
        try:
            return _annotations.check_content_index(conn)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    _error("invalid arguments")


_BOUNDED_EXCEPTIONS = (ContentIndexError, _annotations.AnnotationsError,
                        OSError, ValueError, UnicodeError, sqlite3.Error,
                        RecursionError, OverflowError)


def main(argv: list[str] | None = None) -> int:
    return qscli.run_main(_parse_args, _dispatch, "content index",
                          _BOUNDED_EXCEPTIONS, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
