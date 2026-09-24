#!/usr/bin/env python3
"""A small, bounded JSON API for preparing Logseq daily-journal entries.

The read side intentionally does not use :func:`Path.read_text`: all graph
children are opened without following symlinks and with non-blocking flags.
The append side shares project_planner's lock, but has its own final identity
checks because the journal may not exist yet.
"""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from urllib.parse import unquote

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from logseq_common import (GraphError, SENSITIVE_NAMES as BASELINE_SENSITIVE_NAMES,
                           graph_path, resolve_graph)
from project_planner import (LOCK_NAME, LOCK_TIMEOUT, _GraphLock)
import qscli


JOURNAL_LIMIT = 128 * 1024
# Transport cap; single source of truth is qscli.INPUT_LIMIT (same value).
INPUT_LIMIT = 1024 * 1024
OUTPUT_LIMIT = 1024 * 1024
PAGE_NAME_LIMIT = 500
RECENT_LIMIT = 5
MATCH_LIMIT = 30
RECENT_EXCERPT_LIMIT = 16 * 1024
MATCH_TEXT_LIMIT = 2048
PATH_LIMIT = 4096
DATE_RE = re.compile(r"^\d{4}_\d{2}_\d{2}$")
#: Provenance identity bound (§8.2 bounded labels, ≤ 160 chars each).
REF_IDENTITY_LIMIT = 160
#: quickshell-at:: bound (a clock timestamp, never free prose).
AT_LIMIT = 64
_SESSION_ID_RE = re.compile(r"[0-9a-fA-F]{32}\Z")
_BULLET_RE = re.compile(r"\A([ \t]*)[-*][ \t]+")
HEX_REVISION = re.compile(r"^[0-9a-f]{64}$")
# These limits apply to the traversal itself, rather than just to its JSON
# response. Rejected entries count against the budgets too.
SCAN_ENTRY_LIMIT = 10000
SCAN_DIRECTORY_LIMIT = 1000
SCAN_DEPTH_LIMIT = 16
SCAN_FILE_LIMIT = 5000
SCAN_BYTES_LIMIT = 8 * 1024 * 1024
SENSITIVE_NAMES = set(BASELINE_SENSITIVE_NAMES) | {"config", "private", "secret"}


def _error(message):
    raise GraphError(message)


def _today():
    return datetime.date.today().strftime("%Y_%m_%d")


_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug",
               "Sep", "Oct", "Nov", "Dec")


def journal_page_title(date_text):
    """Logseq default journal page title (``MMM do, yyyy``) for a date.

    ``date_text`` is the stored journal date string in ``%Y_%m_%d`` (as
    :func:`_today` produces). Returns e.g. ``Sep 20th, 2026`` in
    English, or ``""`` for any invalid input.
    """
    if not isinstance(date_text, str):
        return ""
    if not DATE_RE.fullmatch(date_text):
        return ""
    try:
        parsed = datetime.datetime.strptime(date_text, "%Y_%m_%d").date()
    except ValueError:
        return ""
    day = parsed.day
    if 11 <= day % 100 <= 13:
        suffix = "th"
    elif day % 10 == 1:
        suffix = "st"
    elif day % 10 == 2:
        suffix = "nd"
    elif day % 10 == 3:
        suffix = "rd"
    else:
        suffix = "th"
    return f"{_MONTH_ABBR[parsed.month - 1]} {day}{suffix}, {parsed.year}"


def _date(value):
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        _error("date must be YYYY_MM_DD")
    try:
        datetime.datetime.strptime(value, "%Y_%m_%d")
    except ValueError as exc:
        raise GraphError("invalid date") from exc
    if value != _today():
        _error("date is not today; reload the journal")
    return value


def _require_today(value):
    if value != _today():
        _error("date is not today; reload the journal")


def _revision(value):
    if value == "missing":
        return value
    if not isinstance(value, str) or not HEX_REVISION.fullmatch(value):
        _error("revision must be a SHA-256 hex digest or 'missing'")
    return value


def _open_directory(path, *, dir_fd=None):
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                       os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError as exc:
        raise GraphError("graph directory is unsafe") from exc


def _identity(fd):
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


class _ReadLimitExceeded(GraphError):
    """A bounded read observed more bytes than its allowance."""

    def __init__(self, message, size):
        super().__init__(message)
        self.size = size


def _read_fd(fd, limit):
    chunks = []
    remaining = limit + 1
    while remaining:
        try:
            chunk = os.read(fd, remaining)
        except BlockingIOError as exc:
            raise GraphError("journal is not a regular readable file") from exc
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    value = b"".join(chunks)
    if len(value) > limit:
        raise _ReadLimitExceeded("journal is larger than 128 KiB", len(value))
    return value


def _read_named(parent_fd, name, *, missing_ok=False, limit=JOURNAL_LIMIT):
    fd = None
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent_fd)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise GraphError("journal is missing")
        except OSError as exc:
            raise GraphError("journal path is unsafe") from exc
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _error("journal path is not a regular file")
        raw = _read_fd(fd, limit)
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GraphError("journal is not valid UTF-8") from exc
        return raw, stat.S_IMODE(info.st_mode), _identity(fd)
    finally:
        if fd is not None:
            os.close(fd)


def _open_optional_root(graph, name):
    graph_fd = _open_directory(str(graph))
    try:
        try:
            return _open_directory(name, dir_fd=graph_fd)
        except GraphError as exc:
            # A missing pages/ or journals/ directory is an empty collection;
            # every other failure is an unsafe graph layout.
            try:
                os.stat(name, dir_fd=graph_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            except OSError:
                pass
            raise exc
    finally:
        os.close(graph_fd)


def _safe_component(name):
    folded = name.casefold()
    if not name or name in (".", "..") or name.startswith("."):
        return False
    if folded in SENSITIVE_NAMES:
        return False
    if folded.startswith((".env.", ".credentials.")):
        return False
    return True


def _safe_markdown_name(name):
    folded = name.casefold()
    stem = Path(name).stem.casefold()
    return (_safe_component(name) and folded.endswith(".md") and
            folded not in SENSITIVE_NAMES and stem not in SENSITIVE_NAMES)


class _ScanBudget:
    def __init__(self):
        self.entries = 0
        self.directories = 0
        self.files = 0
        self.bytes = 0
        self.truncated = False


def _close_scan_frames(stack):
    for frame in reversed(stack):
        iterator = frame["iterator"]
        if iterator is not None:
            iterator.close()
        if frame["owned"]:
            os.close(frame["fd"])


def _walk_markdown(root_fd, root_name, limit=JOURNAL_LIMIT, budget=None):
    """Yield bounded ``(relative path, raw bytes)`` without following links.

    This is iterative so a hostile directory tree cannot consume the Python
    call stack. It also does not sort or materialize a directory's entries:
    every entry, including rejected ones, consumes the traversal budget.
    """
    budget = budget or _ScanBudget()
    stack = [{"fd": root_fd, "parts": (root_name,), "depth": 0,
              "iterator": None, "owned": False}]
    while stack:
        frame = stack[-1]
        if frame["iterator"] is None:
            try:
                frame["iterator"] = os.scandir(frame["fd"])
            except OSError:
                stack.pop()
                if frame["owned"]:
                    os.close(frame["fd"])
                continue
        try:
            entry = next(frame["iterator"])
        except StopIteration:
            frame["iterator"].close()
            stack.pop()
            if frame["owned"]:
                os.close(frame["fd"])
            continue
        except OSError:
            frame["iterator"].close()
            stack.pop()
            if frame["owned"]:
                os.close(frame["fd"])
            continue

        if budget.entries >= SCAN_ENTRY_LIMIT:
            budget.truncated = True
            _close_scan_frames(stack)
            return
        budget.entries += 1
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        child_parts = frame["parts"] + (entry.name,)

        # Count directories and regular files before applying name policy so
        # a large rejected subtree cannot bypass the bounds.
        file_size = 0
        remaining = SCAN_BYTES_LIMIT - budget.bytes
        if stat.S_ISDIR(info.st_mode):
            if budget.directories >= SCAN_DIRECTORY_LIMIT:
                budget.truncated = True
                _close_scan_frames(stack)
                return
            budget.directories += 1
            if not _safe_component(entry.name):
                continue
            if frame["depth"] >= SCAN_DEPTH_LIMIT:
                budget.truncated = True
                continue
            try:
                child = _open_directory(entry.name, dir_fd=frame["fd"])
            except GraphError:
                # A concurrently replaced directory or a symlink is ignored;
                # importantly, it is never read through.
                continue
            stack.append({"fd": child, "parts": child_parts,
                          "depth": frame["depth"] + 1, "iterator": None,
                          "owned": True})
            continue

        if stat.S_ISREG(info.st_mode):
            if budget.files >= SCAN_FILE_LIMIT:
                budget.truncated = True
                _close_scan_frames(stack)
                return
            budget.files += 1
            file_size = max(0, info.st_size)
            remaining = SCAN_BYTES_LIMIT - budget.bytes
            # Reserve the stat size once. The read allowance is calculated
            # from the pre-reservation remainder, not from the remainder a
            # second time after charging this file.
            budget.bytes += file_size
            if budget.bytes > SCAN_BYTES_LIMIT:
                budget.truncated = True
                _close_scan_frames(stack)
                return
        if (not stat.S_ISREG(info.st_mode) or
                not _safe_component(entry.name) or
                not _safe_markdown_name(entry.name)):
            continue
        relative = Path(root_name, *child_parts[1:]).as_posix()
        if len(relative) > PATH_LIMIT:
            continue
        read_limit = min(limit, remaining)
        try:
            value = _read_named(frame["fd"], entry.name, limit=read_limit)
        except _ReadLimitExceeded as exc:
            # The one-byte probe is bounded by the aggregate allowance. If
            # the allowance, rather than the per-file limit, caused the
            # omission, report that budget truncation to the caller.
            budget.bytes += max(0, exc.size - file_size)
            if remaining < limit:
                budget.truncated = True
                _close_scan_frames(stack)
                return
            continue
        except GraphError:
            continue
        if value is not None:
            # A file can change between stat() and open(). Account for a
            # growth race as well, without ever retaining more than the
            # remaining aggregate budget.
            actual_size = len(value[0])
            if actual_size > file_size:
                budget.bytes += actual_size - file_size
                if budget.bytes > SCAN_BYTES_LIMIT:
                    budget.truncated = True
                    _close_scan_frames(stack)
                    return
            yield (relative, value[0])


def _page_name(relative):
    path = Path(relative)
    if path.parts and path.parts[0] in ("pages", "journals"):
        value = str(path.relative_to(path.parts[0]).with_suffix(""))
    else:
        value = str(path.with_suffix(""))
    return unquote(value).replace("___", "/")


def _current_journal(graph, date=None, *, create=False):
    """Return ``(raw, mode, identity)`` or ``(b"", None, None)`` if absent."""
    if create:
        # The cooperating writer is the only operation allowed to create the
        # journals directory.  _GraphLock performs this with O_NOFOLLOW.
        raise AssertionError("current journal creation is lock-owned")
    journals_fd = _open_optional_root(graph, "journals")
    if journals_fd is None:
        return b"", None, None
    try:
        if date is None:
            date = _today()
        value = _read_named(journals_fd, date + ".md", missing_ok=True)
        if value is None:
            return b"", None, None
        return value
    finally:
        os.close(journals_fd)


def _digest(raw, exists):
    return hashlib.sha256(raw).hexdigest() if exists else "missing"


def _clip_bytes(raw, limit):
    if len(raw) <= limit:
        return raw.decode("utf-8"), False
    return raw[:limit].decode("utf-8", errors="ignore"), True


def _line_matches(content, terms):
    for number, line in enumerate(content.splitlines(), 1):
        if any(term in line.casefold() for term in terms):
            yield number, line


def _query_terms(query):
    if query is None:
        return []
    if not isinstance(query, str):
        _error("query must be a bounded string")
    try:
        if len(query.encode("utf-8")) > 5000:
            _error("query must be a bounded string")
    except UnicodeEncodeError as exc:
        raise GraphError("query is not valid UTF-8") from exc
    # Tokenizing keeps a pasted paragraph from being copied into a response
    # and makes a query useful for finding several linked concepts.
    return list(dict.fromkeys(re.findall(r"[^\W_]+", query.casefold(), flags=re.UNICODE)))[:100]


def _context_response(graph, query):
    today = _today()
    terms = _query_terms(query)
    raw, _mode, today_identity = _current_journal(graph, date=today)
    today_exists = today_identity is not None
    page_names = []
    recent_candidates = []
    matches = []
    budget = _ScanBudget()
    truncated = False

    for root_name in ("pages", "journals"):
        root_fd = _open_optional_root(graph, root_name)
        if root_fd is None:
            continue
        try:
            for relative, value in _walk_markdown(root_fd, root_name, budget=budget):
                if root_name == "pages":
                    name = _page_name(relative)
                    if len(name) > 512:
                        name = name[:512]
                        truncated = True
                    if len(page_names) < PAGE_NAME_LIMIT:
                        page_names.append(name)
                    else:
                        # Keep the lexicographically earliest bounded set so
                        # output does not depend on directory enumeration.
                        largest = max(range(len(page_names)),
                                      key=lambda index: (page_names[index].casefold(),
                                                         page_names[index]))
                        if (name.casefold(), name) < (
                                page_names[largest].casefold(), page_names[largest]):
                            page_names[largest] = name
                        truncated = True
                else:
                    match = re.fullmatch(r"(\d{4}_\d{2}_\d{2})\.md",
                                         Path(relative).name)
                    if (Path(relative).parent == Path("journals") and
                            match is not None and match.group(1) != today):
                        try:
                            parsed = datetime.datetime.strptime(
                                match.group(1), "%Y_%m_%d").date()
                        except ValueError:
                            parsed = None
                        if parsed is not None:
                            content, was_truncated = _clip_bytes(
                                value, RECENT_EXCERPT_LIMIT)
                            truncated |= was_truncated
                            recent_candidates.append((parsed, relative, content))
                            recent_candidates.sort(
                                key=lambda item: (item[0], item[1]), reverse=True)
                            if len(recent_candidates) > RECENT_LIMIT:
                                recent_candidates.pop()
                                truncated = True
                if terms and len(matches) < MATCH_LIMIT:
                    content = value.decode("utf-8")
                    for line_number, line in _line_matches(content, terms):
                        text, was_truncated = _clip_bytes(
                            line.encode("utf-8"), MATCH_TEXT_LIMIT)
                        truncated |= was_truncated
                        matches.append({"path": relative, "page": _page_name(relative),
                                        "line": line_number, "text": text})
                        if len(matches) >= MATCH_LIMIT:
                            truncated = True
                            break
        finally:
            os.close(root_fd)

    truncated |= budget.truncated
    page_names.sort(key=lambda value: (value.casefold(), value))
    recent = []
    for _parsed, relative, content in recent_candidates:
        recent.append({"path": relative, "content": content})

    response = {
        "graphName": graph.name,
        "date": today,
        "path": "journals/" + today + ".md",
        "revision": _digest(raw, today_exists),
        "content": raw.decode("utf-8"),
        "pageNames": page_names,
        "recentJournals": recent,
        "matches": matches,
        "truncated": bool(truncated),
    }
    return _fit_context(response)


def context(graph, query=None):
    """Return today's journal plus bounded graph context without writing."""
    return _context_response(graph_path(graph), query)


def _json_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _fit_context(value):
    if _json_size(value) + 1 <= OUTPUT_LIMIT:
        return value
    value["truncated"] = True
    # These are excerpts, so reducing them is preferable to dropping today's
    # journal, whose exact content is the primary context payload.
    for size in (8192, 4096, 1024, 0):
        for item in value["recentJournals"]:
            item["content"], _ = _clip_bytes(item["content"].encode("utf-8"), size)
        if _json_size(value) + 1 <= OUTPUT_LIMIT:
            return value
    while value["matches"] and _json_size(value) + 1 > OUTPUT_LIMIT:
        value["matches"].pop()
    while value["pageNames"] and _json_size(value) + 1 > OUTPUT_LIMIT:
        value["pageNames"].pop()
    if _json_size(value) + 1 > OUTPUT_LIMIT:
        _error("context response exceeds 1 MiB")
    return value


def _normal_text(value, *, nonblank=True):
    if not isinstance(value, str):
        _error("text must be a string")
    try:
        original = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise GraphError("text is not valid UTF-8") from exc
    if len(original) > JOURNAL_LIMIT:
        _error("text is larger than 128 KiB")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if nonblank and not value.strip():
        _error("text must not be blank")
    if not value.endswith("\n"):
        value += "\n"
    normalized = value.encode("utf-8")
    if len(normalized) > JOURNAL_LIMIT:
        _error("text is larger than 128 KiB")
    return value, normalized


def _validate_session_id(value) -> str:
    """Collector session id, or "" when absent (degraded, never blocked).

    An explicitly supplied but malformed id fails closed
    (misattribution risk); an absent/blank one means the text is stored
    without markers.
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

    Untrusted resource data, so the shared ``text_safety`` gate
    *filters* (drops the line, never the write): over-long (160),
    control-character, or secret-looking identities yield no
    ``quickshell-ref::`` line. The exact preview always shows what
    lands.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        _error("ref identity is invalid")
    if not value.strip():
        return ""
    from text_safety import safe_text_or_none as _gate
    kept = _gate(value, REF_IDENTITY_LIMIT)
    return kept if kept is not None else ""


def _validate_at(value) -> str:
    """Clock timestamp for ``quickshell-at::``, or "" when absent.

    Explicit metadata (never resource data): a supplied but malformed
    value fails closed rather than writing a misleading timestamp.
    """
    if value is None:
        return ""
    if not isinstance(value, str) or not value.strip():
        if isinstance(value, str):
            return ""
        _error("timestamp is invalid")
    text = value.strip()
    if len(text) > AT_LIMIT or "\x00" in text:
        _error("timestamp is invalid")
    if any(ord(c) < 32 for c in text):
        _error("timestamp is invalid")
    return text


def _session_markers(normalized_text, session_id, ref, at) -> list:
    """Marker lines for a session-marked thought (indent-aware).

    The child indent extends the thought's first-line bullet indent by
    two spaces (matching the fixture shape); non-bullet text gets a
    flat two-space indent. ``quickshell-at::`` / ``quickshell-ref::``
    appear only when applicable.
    """
    first = normalized_text.split("\n", 1)[0] if normalized_text else ""
    match = _BULLET_RE.match(first)
    child = (match.group(1) + "  ") if match is not None else "  "
    markers = [child + "quickshell-session:: " + session_id]
    if at:
        markers.append(child + "quickshell-at:: " + at)
    if ref:
        markers.append(child + "quickshell-ref:: " + ref)
    return markers


def _insert_markers(payload: bytes, markers: list) -> bytes:
    """Insert marker lines after the payload's first line."""
    lines = payload.split(b"\n")
    encoded = [marker.encode("utf-8") for marker in markers]
    # The normalized payload always ends with LF, so lines[-1] == b""
    # and rejoining preserves the terminator.
    return b"\n".join([lines[0]] + encoded + lines[1:])


def _link_session_thought(conn, *, session_id: str, thought_ref: str,
                           index_text: str, page: str, line: int,
                           now_ms: int) -> None:
    """Record a saved thought in session_meta + content_index.

    Fail-closed: sidecar errors propagate as GraphError (bounded, no
    traceback). The journal write already landed, so the message never
    claims otherwise; the derived index is rebuildable via
    ``content_index rebuild``. ``attended`` is never touched here — an
    apply does not mean nothing remains (read path derives pending).
    """
    import annotations as _annotations
    from content_index import index_block
    try:
        meta = _annotations.get_session_meta(conn, session_id)
    except _annotations.AnnotationsError as exc:
        raise GraphError(str(exc)) from exc
    try:
        if meta is None:
            _annotations.upsert_session_meta(
                conn, session_id=session_id, now_ms=now_ms,
                thought_ref=thought_ref)
        else:
            try:
                _annotations.upsert_session_meta(
                    conn, session_id=session_id, now_ms=now_ms,
                    expect_revision=meta.get("revision"),
                    thought_ref=thought_ref)
            except _annotations.AnnotationsError as exc:
                if str(exc) != "session meta changed":
                    raise
                fresh = _annotations.get_session_meta(conn, session_id)
                if fresh is None:
                    raise GraphError(str(exc)) from exc
                _annotations.upsert_session_meta(
                    conn, session_id=session_id, now_ms=now_ms,
                    expect_revision=fresh.get("revision"),
                    thought_ref=thought_ref)
    except _annotations.AnnotationsError as exc:
        raise GraphError(str(exc)) from exc
    try:
        index_block(conn, text=index_text, page=page, line=line,
                    kind="thought", session_id=session_id)
    except Exception as exc:
        raise GraphError("session link failed") from exc


def prepare(graph, date, revision, text, session_id=None, ref=None,
            at=None):
    """Stage the exact journal addition (UI-level prepare → preview → Confirm).

    There is no token table: the caller shows ``addition`` verbatim,
    obtains user confirmation, then calls :func:`append` with the same
    arguments. Destination is always today's journal (``_require_today``
    enforced here and rechecked under lock in :func:`append`).

    When *session_id* (32-hex collector session) is supplied, session
    provenance markers are composed into the addition: non-bullet
    property lines directly under the thought's first line (before any
    child bullet), following the ``quickshell-agenda::`` pattern::

        - the thought
          quickshell-session:: <id>
          quickshell-at:: <ts>          (only when *at* is supplied)
          quickshell-ref:: <identity>   (only when *ref* passes the gate)

    Without a session id the text is appended unchanged (degraded, never
    blocked). A malformed session id or timestamp fails closed; a
    gated-out ref identity (over-long, control characters,
    secret-looking per the shared ``text_safety`` gate) simply yields no
    ``quickshell-ref::`` line — the preview always shows what lands.
    """
    graph = graph_path(graph)
    date = _date(date)
    expected = _revision(revision)
    sid = _validate_session_id(session_id)
    gated_ref = _validate_ref(ref)
    bounded_at = _validate_at(at)
    raw, _mode, identity_value = _current_journal(graph, date=date)
    exists = identity_value is not None
    actual = _digest(raw, exists)
    if actual != expected:
        _error("journal revision is stale; reload the journal")
    normalized, payload = _normal_text(text)
    if sid:
        markers = _session_markers(normalized, sid, gated_ref, bounded_at)
        payload = _insert_markers(payload, markers)
        normalized = payload.decode("utf-8")
    separator = b"\n" if raw and not raw.endswith(b"\n") else b""
    addition = separator + payload
    if len(raw) + len(addition) > JOURNAL_LIMIT:
        _error("journal would be larger than 128 KiB")
    return {"date": date, "path": "journals/" + date + ".md",
            "revision": actual, "addition": addition.decode("utf-8")}


def _validate_addition(addition, raw):
    if not isinstance(addition, str):
        _error("addition must be a string")
    try:
        value = addition.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise GraphError("addition is not valid UTF-8") from exc
    if not value or len(value) > JOURNAL_LIMIT:
        _error("addition must be between 1 byte and 128 KiB")
    if b"\r" in value or not value.endswith(b"\n"):
        _error("addition must use LF newlines and end with LF")
    required = b"\n" if raw and not raw.endswith(b"\n") else b""
    if required and not value.startswith(required):
        _error("addition does not match the current journal boundary")
    if value == required:
        _error("addition must contain text")
    if len(raw) + len(value) > JOURNAL_LIMIT:
        _error("journal would be larger than 128 KiB")
    return value


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            _error("journal write failed")
        view = view[count:]


def append(graph, date, revision, addition, session_id=None, ref=None,
           at=None, db=None):
    """Append a prepared addition to today's journal (revision-checked).

    The atomic lock → recheck → write flow is unchanged; *addition* is
    written verbatim (the exact approved preview — never recomposed
    here). Destination is always today's journal: ``_require_today``
    runs before lock acquisition and again immediately before replace,
    so a request validated before midnight never writes after it.

    When *session_id* is supplied (with the same *ref*/*at* passed to
    :func:`prepare`), the addition must contain the
    ``quickshell-session:: <id>`` marker line it produced — a mismatch
    fails closed. On success the block ref
    (``journal:<date>.md:<line>``) is written to
    ``session_meta.thought_ref`` and the exact block text is inserted
    into ``content_index`` (kind ``thought``); both are returned as
    ``thought_ref`` ("" without a session, in which case no sidecar is
    touched). ``attended`` is never modified here.
    """
    graph = graph_path(graph)
    date = _date(date)
    expected = _revision(revision)
    sid = _validate_session_id(session_id)
    gated_ref = _validate_ref(ref)
    bounded_at = _validate_at(at)
    if sid and not isinstance(addition, str):
        _error("addition must be a string")
    target = date + ".md"
    graph_before_fd = _open_directory(str(graph))
    graph_before_identity = _identity(graph_before_fd)
    os.close(graph_before_fd)
    with _GraphLock(graph) as lock:
        # A request validated just before lock acquisition must not write a
        # journal after midnight while it was waiting for the lock.
        _require_today(date)
        graph_fd = _open_directory(str(graph))
        graph_identity = _identity(graph_fd)
        os.close(graph_fd)
        if graph_identity != graph_before_identity:
            _error("graph directory changed before write")
        journals_identity = _identity(lock.journals_fd)
        current = _read_named(lock.journals_fd, target, missing_ok=True)
        if current is None:
            raw, mode, target_identity = b"", 0o644, None
        else:
            raw, mode, target_identity = current
        actual = _digest(raw, target_identity is not None)
        if actual != expected:
            _error("journal revision is stale; reload the journal")
        payload = _validate_addition(addition, raw)
        if sid and ("quickshell-session:: " + sid) not in addition:
            _error("addition does not carry the session marker;"
                   " prepare it again")
        first_line = 1 if not raw else raw.count(b"\n") + (1 if raw.endswith(b"\n") else 2)
        temporary = None
        temp_fd = None
        try:
            for attempt in range(10):
                candidate = f".journal-assistant-{os.getpid()}-{attempt}.tmp"
                try:
                    temp_fd = os.open(candidate, os.O_WRONLY | os.O_CREAT |
                                      os.O_EXCL | os.O_NOFOLLOW, mode,
                                      dir_fd=lock.journals_fd)
                    temporary = candidate
                    break
                except FileExistsError:
                    continue
            if temp_fd is None:
                _error("cannot create temporary journal")
            assert temp_fd is not None
            os.fchmod(temp_fd, mode)
            _write_all(temp_fd, raw + payload)
            os.fsync(temp_fd)
            os.close(temp_fd)
            temp_fd = None

            final_graph_fd = _open_directory(str(graph))
            try:
                final_graph_identity = _identity(final_graph_fd)
                final_parent = _open_directory("journals", dir_fd=final_graph_fd)
            finally:
                os.close(final_graph_fd)
            try:
                if (final_graph_identity != graph_identity or
                        _identity(final_parent) != journals_identity):
                    _error("graph or journal directory changed before write")
                final = _read_named(final_parent, target, missing_ok=True)
                if final is None:
                    final_raw, _final_mode, final_target_identity = b"", mode, None
                else:
                    final_raw, _final_mode, final_target_identity = final
                if final_raw != raw or final_target_identity != target_identity:
                    _error("journal changed before write; reload the journal")
                assert temporary is not None
                # Keep this check directly beside replace: the date can roll
                # over while the temporary file is being prepared.
                _require_today(date)
                os.replace(temporary, target, src_dir_fd=lock.journals_fd,
                           dst_dir_fd=final_parent)
                temporary = None
                os.fsync(final_parent)
            finally:
                os.close(final_parent)
        except OSError as exc:
            raise GraphError(f"cannot replace journal safely: {exc}") from exc
        finally:
            if temp_fd is not None:
                try:
                    os.close(temp_fd)
                except OSError:
                    pass
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=lock.journals_fd)
                except OSError:
                    pass
        new_raw = raw + payload
    thought_ref = ""
    if sid:
        thought_ref = f"journal:{date}.md:{first_line}"
        _write_session_link(db, sid, thought_ref, addition, date,
                            first_line)
    return {"date": date, "path": "journals/" + date + ".md",
            "revision": hashlib.sha256(new_raw).hexdigest(),
            "line": first_line, "thought_ref": thought_ref}


def _write_session_link(db, session_id, thought_ref, addition, date,
                        first_line):
    """Connect the sidecar and record the saved thought (fail-closed).

    Errors map to a bounded GraphError that states the journal write
    already landed, so callers never retry the append (which would
    duplicate the thought): the derived index is rebuildable via
    ``content_index rebuild``.
    """
    import annotations as _annotations
    index_text = addition.strip("\n") if isinstance(addition, str) else ""
    try:
        conn = _annotations.connect(db)
    except _annotations.AnnotationsError as exc:
        raise GraphError("thought saved; session link failed") from exc
    try:
        _link_session_thought(
            conn, session_id=session_id, thought_ref=thought_ref,
            index_text=index_text, page=date, line=first_line,
            now_ms=int(time.time() * 1000))
    except Exception as exc:
        raise GraphError("thought saved; session link failed") from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI (shared plumbing lives in qscli.py; argv stays byte-identical)
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = qscli.SafeParser()
    qscli.add_global_flags(parser, db=True, graph=True)  # --graph defaults to LOGSEQ_GRAPH or logseqGraph in settings.json
    parser.add_argument("command", choices=("context", "prepare", "append"))
    return parser.parse_args(argv if argv is not None else sys.argv[1:])


def _dispatch(args: argparse.Namespace) -> dict:
    graph = resolve_graph(args.graph)
    request = qscli.read_input()
    if args.command == "context":
        return _context_response(graph, request.get("query"))
    elif args.command == "prepare":
        return prepare(graph, request.get("date"), request.get("revision"),
                       request.get("text"),
                       session_id=request.get("session_id"),
                       ref=request.get("ref"), at=request.get("at"))
    else:
        return append(graph, request.get("date"), request.get("revision"),
                      request.get("addition"),
                      session_id=request.get("session_id"),
                      ref=request.get("ref"), at=request.get("at"),
                      db=args.db)


_BOUNDED_EXCEPTIONS = (GraphError, OSError, TypeError, ValueError,
                       UnicodeError)


def main(argv=None) -> int:
    return qscli.run_main(_parse_args, _dispatch, "journal assistant",
                          _BOUNDED_EXCEPTIONS, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
