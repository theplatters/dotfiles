#!/usr/bin/env python3
"""Calendar daily agenda over Logseq project pages.

Project pages are the existing markdown pages below ``pages/`` (journals are
never listed). Scheduling is stored as a direct Logseq block property under
the task itself::

    - TODO ship the thing
      quickshell-agenda:: 2026-09-13

No external state is kept. Listing reports open tasks plus done tasks that
are scheduled on the requested date; the UI filters those into an agenda view
or a date picker. Selection, completion, and toggle are explicit write
operations (explicit UI action is the write approval).

Safety reuses :mod:`project_planner` internals (locking, symlink/size,
revision, and line handling) so agenda writes preserve the planner's
guarantees. This module does not create pages.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import NoReturn

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from logseq_common import GraphError, graph_path, resolve_graph
import project_planner
import qscli
from project_planner import (
    _GraphLock,
    _page_bytes,
    _replace_page,
    _split_lines,
    _task,
    _toggle_line,
    _validate_relative_path,
    _validate_revision,
)

INPUT_LIMIT = project_planner.INPUT_LIMIT
PAGE_LIMIT = project_planner.PAGE_LIMIT
try:
    from logseq_common import MAX_RESULTS as _MAX_RESULTS
except ImportError:  # pragma: no cover
    _MAX_RESULTS = 1000

AGENDA_LIMIT = _MAX_RESULTS
AGENDA_PROP = "quickshell-agenda"

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_INDENT_RE = re.compile(r"^[ \t]*")
_BULLET_RE = re.compile(r"^[ \t]*-")
# Markdown tab stops: Logseq-compatible visual depth expands tabs to the next
# multiple of 4 columns. Character count alone misorders "\t" (1 char) versus
# "    " (4 chars) even though both render at the same depth.
_TAB_WIDTH = 4
_PROPERTY_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z0-9_-]+)::[ \t]*(?P<value>.*)$"
)


def _error(message: str) -> NoReturn:
    raise GraphError(message)


def _today_iso() -> str:
    return datetime.date.today().isoformat()


def _validate_iso_date(value: object) -> str:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        _error("date must be YYYY-MM-DD")
    try:
        datetime.datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise GraphError("date must be a valid calendar day") from exc
    return value


def _validate_selected(value: object) -> bool:
    if not isinstance(value, bool):
        _error("selected must be boolean")
    return value


def _validate_done_flag(value: object) -> bool:
    if not isinstance(value, bool):
        _error("done must be boolean")
    return value


def _validate_note(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("note must be a nonblank string")
    if len(value.encode("utf-8")) > PAGE_LIMIT:
        _error("note is too large")
    if "\x00" in value:
        _error("note must not contain NUL")
    return value


def _validate_line_number(value: object) -> int:
    try:
        return project_planner._validate_line(value)
    except AttributeError:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            _error("line must be a positive integer")
        return value


def _indent_of(line: str) -> str:
    match = _INDENT_RE.match(line)
    return match.group(0) if match else ""


def _indent_width(indent: str) -> int:
    """Visual depth of leading whitespace using Markdown tab stops."""
    return len(indent.expandtabs(_TAB_WIDTH))


def _is_bullet(line: str) -> bool:
    return _BULLET_RE.match(line) is not None


def _property_match(line: str):
    if _is_bullet(line):
        return None
    return _PROPERTY_RE.match(line)


def _agenda_value(value: str):
    text = value.strip()
    if not _DATE_RE.fullmatch(text):
        return None
    try:
        datetime.datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return text


def _detect_newline(seps) -> str:
    for sep in seps:
        if sep in ("\r\n", "\r", "\n"):
            return sep
    return "\n"


def _block_info(bodies, target):
    """Describe the outline block starting at 0-based *target*.

    Returns (parent_indent, child_base, first_child, agenda, last_direct_prop,
    last_in_block, block_end). ``agenda`` holds body indices of direct
    ``quickshell-agenda`` properties before the first child bullet.

    Depth uses Logseq-compatible visual columns (tabs expand to 4-column
    stops) so ``"\\t"`` and ``"    "`` compare equal instead of 1 versus 4.
    ``child_base`` is the direct-child level for the whole block: the minimum
    visual bullet indent strictly greater than the parent, preserving that
    bullet's exact raw indent (tabs versus spaces). Copying only the first
    descendant misnests ``- P / \\t- Q / ␣␣- R`` as a tab note after ``R``
    that ``_block_info(R)`` then owns; the minimum (``R``'s ``"  "``) stays a
    sibling of ``R``. With no child bullets, the parent indent plus a
    style-preserving unit is used. See :func:`_ensure_consistent_block` for
    the write-time rejection of inconsistent outlines.
    """
    parent_indent = _indent_of(bodies[target])
    parent_width = _indent_width(parent_indent)
    first_child = None
    min_child_width = None
    min_child_indent = None
    agenda = []
    last_direct_prop = None
    last_in_block = target
    block_end = len(bodies)
    for index in range(target + 1, len(bodies)):
        body = bodies[index]
        if body.strip() == "":
            continue
        indent = _indent_of(body)
        if _indent_width(indent) <= parent_width:
            block_end = index
            break
        last_in_block = index
        if _is_bullet(body):
            if first_child is None:
                first_child = index
            width = _indent_width(indent)
            if min_child_width is None or width < min_child_width:
                min_child_width = width
                min_child_indent = indent
            continue
        if first_child is None:
            match = _property_match(body)
            if match is not None:
                last_direct_prop = index
                if match.group("key").casefold() == AGENDA_PROP:
                    agenda.append(index)
    else:
        block_end = len(bodies)
    if min_child_indent is not None:
        # Direct-child level across the entire block, not just the first
        # descendant, so the new line cannot end up inside a shallower
        # sibling's block. Ties keep the first raw at the minimum depth.
        child_base = min_child_indent
    else:
        unit = "\t" if "\t" in parent_indent else "  "
        child_base = parent_indent + unit
    return (
        parent_indent,
        child_base,
        first_child,
        agenda,
        last_direct_prop,
        last_in_block,
        block_end,
    )


def _ensure_consistent_block(bodies, target, parent_indent, last_in_block):
    """Reject ambiguous nesting before guessing a write indent.

    Every visual descendant must extend the parent's exact indent prefix.
    Top-level parents (``""`` prefix) always satisfy this, so mixed
    tabs/spaces siblings at the top level are ordered by visual depth instead
    of being rejected. A nested parent such as ``"  "`` with a ``"\\t"``
    descendant is tabsize-dependent, so writes fail unchanged rather than
    placing the new line at a guessed depth.

    Bullet descendants must also open at the direct-child level: the first
    bullet cannot be deeper than the minimum visual bullet indent in the
    block. ``- P / \\t- Q / ␣␣- R`` (depths 4 then 2) and its all-space twin
    ``- P / ␣␣␣␣- Q / ␣␣- R`` are inconsistent Luna outlines -- copying the
    first indent would nest the new note under ``R`` by this helper's own
    ``_block_info``. Writes fail unchanged; listing never calls this so
    reads keep their contract.
    """
    parent_width = _indent_width(parent_indent)
    first_bullet_width = None
    min_bullet_width = None
    for index in range(target + 1, last_in_block + 1):
        body = bodies[index]
        if body.strip() == "":
            continue
        indent = _indent_of(body)
        if _indent_width(indent) <= parent_width:
            continue
        if not indent.startswith(parent_indent):
            _error(
                "mixed tabs and spaces indentation is ambiguous; "
                "use consistent indentation"
            )
        if _is_bullet(body):
            width = _indent_width(indent)
            if first_bullet_width is None:
                first_bullet_width = width
            if min_bullet_width is None or width < min_bullet_width:
                min_bullet_width = width
    if (
        first_bullet_width is not None
        and min_bullet_width is not None
        and first_bullet_width > min_bullet_width
    ):
        _error(
            "inconsistent indentation is ambiguous; "
            "use consistent indentation"
        )


def _scheduled_date(bodies, target) -> str:
    """Return the direct agenda date for *target* or ``""``.

    Only non-bullet ``quickshell-agenda::`` lines before the first child
    bullet count. Descendant TODO properties are never read.
    """
    _parent, _base, _child, agenda, _prop, _last, _end = _block_info(
        bodies, target
    )
    found = ""
    for index in agenda:
        match = _property_match(bodies[index])
        if match is None:
            continue
        parsed = _agenda_value(match.group("value"))
        if parsed is not None:
            found = parsed
    return found


def _insert_lines(bodies, seps, pos, new_bodies, newline):
    for offset, body in enumerate(new_bodies):
        bodies.insert(pos + offset, body)
        if pos + offset <= len(seps):
            seps.insert(pos + offset, newline)
        else:  # pragma: no cover - defensive; pos is always in range
            seps.append(newline)


def _delete_indices(bodies, seps, indices):
    for index in sorted(indices, reverse=True):
        if index < 0 or index >= len(bodies):
            _error("line does not contain a task")
        if index == len(bodies) - 1:
            bodies.pop(index)
            if index - 1 >= 0 and index - 1 < len(seps):
                # Last logical line had no terminator; drop the separator
                # that previously joined it to its predecessor.
                seps.pop(index - 1)
            elif seps:
                seps.pop()
        else:
            bodies.pop(index)
            if index < len(seps):
                seps.pop(index)
            elif seps:  # pragma: no cover - defensive
                seps.pop()


def _response_for(graph, relative, raw):
    return project_planner._response(graph, relative, raw)


_UNSET = object()


def list_agenda(graph, date=_UNSET):
    """List open tasks plus done tasks scheduled on *date*.

    *date* defaults to local today when omitted (or when a request object
    omits ``date``) so palette discovery can inspect the current day
    without guessing a date. An explicit null/non-string date still fails
    validation.
    """
    if date is _UNSET:
        date = _today_iso()
    elif isinstance(date, dict):
        if "date" not in date:
            date = _today_iso()
        else:
            date = date.get("date")
    date = _validate_iso_date(date)
    graph = graph_path(graph)
    try:
        projects = project_planner.list_projects(graph)["projects"]
    except GraphError:
        raise
    graph_name = graph.name
    # Refresh graph_name from list_projects when available.
    try:
        graph_name = project_planner.list_projects(graph)["graphName"]
    except GraphError:  # pragma: no cover - list above already succeeded
        pass
    scheduled = []
    rest = []
    for entry in projects:
        relative = _validate_relative_path(entry["path"])
        try:
            raw, _mode = _page_bytes(graph, relative)
        except GraphError:
            continue
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        revision = hashlib.sha256(raw).hexdigest()
        parts = _split_lines(content)
        bodies = parts[0::2]
        try:
            page = project_planner._relative_page(graph, relative)
            from logseq_common import page_name as _page_name

            page_label = _page_name(page, graph)
        except GraphError:
            continue
        for lineno, body in enumerate(bodies, 1):
            # Trailing empty sentinel from a final newline is blank.
            if body.strip() == "":
                continue
            item = _task(body, lineno)
            if item is None:
                continue
            if not _is_bullet(body):
                # Parser currently only yields bullet tasks; reject any
                # future non-bullet form instead of misplacing properties.
                continue
            agenda_date = _scheduled_date(bodies, lineno - 1)
            if item["done"] and agenda_date != date:
                continue
            record = {
                "path": relative.as_posix(),
                "page": page_label,
                "revision": revision,
                "line": lineno,
                "task": item["task"],
                "marker": item["marker"],
                "done": item["done"],
                "scheduledDate": agenda_date,
            }
            if agenda_date == date:
                scheduled.append(record)
            else:
                rest.append(record)
    ordered = scheduled + rest
    truncated = len(ordered) > AGENDA_LIMIT
    return {
        "date": date,
        "graphName": graph_name,
        "tasks": ordered[:AGENDA_LIMIT],
        "truncated": truncated,
    }


def select_task(graph, path, revision=None, line=None, date=None, selected=None):
    """Schedule (selected=True) or unschedule (selected=False) one task."""
    if isinstance(path, dict) and revision is None:
        request = path
        path = request.get("path")
        revision = request.get("revision")
        line = request.get("line")
        date = request.get("date")
        selected = request.get("selected")
    graph = graph_path(graph)
    relative = _validate_relative_path(path)
    revision = _validate_revision(revision)
    lineno = _validate_line_number(line)
    date = _validate_iso_date(date)
    selected = _validate_selected(selected)

    with _GraphLock(graph):
        raw, _mode = _page_bytes(graph, relative)
        if hashlib.sha256(raw).hexdigest() != revision:
            _error("page revision is stale; reload the page")
        content = raw.decode("utf-8")
        parts = _split_lines(content)
        bodies = parts[0::2]
        seps = parts[1::2]
        if lineno > len(bodies):
            _error("line does not contain a task")
        target_body = bodies[lineno - 1]
        item = _task(target_body, lineno)
        if item is None:
            _error("line does not contain a task")
        if not _is_bullet(target_body):
            _error("line does not support block properties")
        _parent, child_base, first_child, agenda, last_prop, last_in_block, _end = (
            _block_info(bodies, lineno - 1)
        )
        if selected or agenda:
            _ensure_consistent_block(
                bodies, lineno - 1, _parent, last_in_block
            )
        newline = _detect_newline(seps)
        if selected:
            new_body_suffix = f"{AGENDA_PROP}:: {date}"
            if agenda:
                first = agenda[0]
                old_match = _property_match(bodies[first])
                keep_indent = (
                    old_match.group("indent")
                    if old_match is not None
                    else child_base
                )
                bodies[first] = f"{keep_indent}{new_body_suffix}"
                if len(agenda) > 1:
                    _delete_indices(bodies, seps, agenda[1:])
            else:
                if last_prop is not None:
                    insert_at = last_prop + 1
                elif first_child is not None:
                    insert_at = first_child
                else:
                    insert_at = lineno
                    # lineno is 1-based; bodies index for "just after target"
                    # is lineno (0-based target is lineno-1).
                _insert_lines(
                    bodies, seps, insert_at, [f"{child_base}{new_body_suffix}"], newline
                )
        else:
            if agenda:
                _delete_indices(bodies, seps, agenda)
            # Deselecting an unscheduled task is a no-op success.
        replacement = "".join(
            body + (seps[i] if i < len(seps) else "") for i, body in enumerate(bodies)
        ).encode("utf-8")
        if len(replacement) > PAGE_LIMIT:
            _error("page is larger than 128 KiB")
        if replacement != raw:
            _replace_page(graph, relative, raw, replacement)
        else:
            replacement = raw
    return {"page": _response_for(graph, relative, replacement)}


def complete_task(graph, path, revision=None, line=None, note=None, date=None):
    """Mark one task DONE and append a date-labelled progress child."""
    if isinstance(path, dict) and revision is None:
        request = path
        path = request.get("path")
        revision = request.get("revision")
        line = request.get("line")
        note = request.get("note")
        date = request.get("date")
    graph = graph_path(graph)
    relative = _validate_relative_path(path)
    revision = _validate_revision(revision)
    lineno = _validate_line_number(line)
    note = _validate_note(note)
    date = _validate_iso_date(date)

    normalized = note.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        _error("note must be a nonblank string")
    note_lines = normalized.split("\n")
    first = note_lines[0].strip()
    if not first:
        # Note starts with blank lines; first nonblank line leads the child.
        nonblank = [entry.strip() for entry in note_lines if entry.strip()]
        if not nonblank:
            _error("note must be a nonblank string")
        first = nonblank[0]

    with _GraphLock(graph):
        raw, _mode = _page_bytes(graph, relative)
        if hashlib.sha256(raw).hexdigest() != revision:
            _error("page revision is stale; reload the page")
        content = raw.decode("utf-8")
        parts = _split_lines(content)
        bodies = parts[0::2]
        seps = parts[1::2]
        if lineno > len(bodies):
            _error("line does not contain a task")
        target_body = bodies[lineno - 1]
        item = _task(target_body, lineno)
        if item is None:
            _error("line does not contain a task")
        if not _is_bullet(target_body):
            _error("line does not support block properties")
        _parent, child_base, _child, _agenda, _prop, last_in_block, _end = (
            _block_info(bodies, lineno - 1)
        )
        _ensure_consistent_block(bodies, lineno - 1, _parent, last_in_block)
        newline = _detect_newline(seps)
        bodies[lineno - 1] = _toggle_line(target_body, True)
        child_indent = child_base
        cont_indent = child_base + "  "
        new_lines = [f"{child_indent}- [{date}] {first}"]
        for extra in note_lines[1:]:
            if extra.strip() == "":
                new_lines.append(f"{cont_indent}")
            else:
                new_lines.append(f"{cont_indent}{extra}")
        # If the note had leading blanks, note_lines[1:] still holds the raw
        # tail; the first-line strip above only affects the bullet. The tail
        # stays nested under the new child either way.
        insert_at = last_in_block + 1
        if insert_at > len(bodies):
            insert_at = len(bodies)
        # When the file ends with a newline the last body is a "" sentinel;
        # inserting before it keeps the file newline-terminated.
        if insert_at == len(bodies) and len(bodies) >= 1 and bodies[-1] == "":
            insert_at = len(bodies) - 1
        _insert_lines(bodies, seps, insert_at, new_lines, newline)
        replacement = "".join(
            body + (seps[i] if i < len(seps) else "") for i, body in enumerate(bodies)
        ).encode("utf-8")
        if len(replacement) > PAGE_LIMIT:
            _error("page is larger than 128 KiB")
        _replace_page(graph, relative, raw, replacement)
    return {"page": _response_for(graph, relative, replacement)}


def toggle_agenda_task(graph, path, revision=None, line=None, done=None):
    """Set one task's completion state (planner toggle, wrapped)."""
    if isinstance(path, dict) and revision is None:
        request = path
        path = request.get("path")
        revision = request.get("revision")
        line = request.get("line")
        done = request.get("done")
    if not isinstance(done, bool):
        _error("done must be boolean")
    page = project_planner.toggle_task(graph, path, revision, line, done)
    return {"page": page}


def _read_input():
    # Grandfathered: local reader preserved (error tokens differ from
    # qscli.read_input); only the parser moves to qscli. Failures below
    # surface as stdout {"error": ...} (see main), never stderr.
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    try:
        data = stream.read(INPUT_LIMIT + 1)
    except OSError as exc:
        raise GraphError("cannot read JSON input") from exc
    if isinstance(data, str):
        data = data.encode("utf-8")
    if len(data) > INPUT_LIMIT:
        _error("JSON input exceeds 128 KiB")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GraphError(f"invalid JSON input: {exc}") from exc
    if not isinstance(value, dict):
        _error("JSON input must be an object")
    return value


# ---------------------------------------------------------------------------
# CLI (parser lives in qscli.py; argv stays byte-identical)
# ---------------------------------------------------------------------------

def _parse_args(argv=None):
    parser = qscli.SafeParser()
    qscli.add_global_flags(parser, graph=True)  # --graph defaults to LOGSEQ_GRAPH or logseqGraph in settings.json
    parser.add_argument(
        "command", choices=("list", "select", "complete", "toggle")
    )
    return parser.parse_args(argv if argv is not None else sys.argv[1:])


def main(argv=None):
    # Grandfathered: failures report stdout {"error": ...} (exit 1), not
    # stderr — the contract tests require it — so the emit/guard stay
    # local and main is NOT qscli.run_main. Success output also keeps
    # its exact unsorted separators=(",", ":") encoding.
    try:
        args = _parse_args(argv)
        graph = resolve_graph(args.graph)
        if args.command == "list":
            request = _read_input()
            value = list_agenda(graph, request)
        else:
            request = _read_input()
            if args.command == "select":
                value = select_task(graph, request)
            elif args.command == "complete":
                value = complete_task(graph, request)
            else:
                value = toggle_agenda_task(graph, request)
        print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        return 0
    except SystemExit as exc:
        return 0 if exc.code == 0 else 1
    except (GraphError, OSError, TypeError, ValueError, UnicodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
