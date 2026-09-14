#!/usr/bin/env python3
"""Bounded, lock-aware page operations for a Logseq project planner.

The public functions mirror the JSON commands and deliberately accept only
existing pages below ``pages/``.  In particular, this module does not create
pages or journals.  Writes use the graph's normal journal lock so they are
serialized with :func:`logseq_graph.append_journal`.
"""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import NoReturn

from logseq_common import (GraphError, MAX_FILES, MAX_RESULTS, graph_path,
                           markdown_files, page_name, resolve_graph)


PAGE_LIMIT = 128 * 1024
# JSON string escaping can expand each UTF-8 byte substantially.  Keep the
# page limit strict while allowing a complete page to cross the transport.
INPUT_LIMIT = 1024 * 1024
PATH_LIMIT = 4096
LOCK_NAME = ".logseq_graph.lock"
LOCK_TIMEOUT = 2.0

_MARKERS = "TODO|NOW|LATER|DOING|WAITING|DONE"
_MARKER_TASK = re.compile(
    r"^(?P<prefix>\s*-\s*)"
    r"(?:(?P<checkbox>\[[ \t]*(?P<checked>[xX]?)[ \t]*\])[ \t]*)?"
    r"(?P<marker>" + _MARKERS + r")\b"
    r"[ \t]*:?[ \t]*(?P<task>.*)$"
)
_CHECKBOX_TASK = re.compile(
    r"^(?P<prefix>\s*-\s*)"
    r"(?P<checkbox>\[[ \t]*(?P<checked>[xX]?)[ \t]*\])[ \t]+"
    r"(?P<task>.*)$"
)
_HEX_REVISION = re.compile(r"^[0-9a-f]{64}$")


def _error(message: str) -> NoReturn:
    raise GraphError(message)


def _read_fd(fd, limit):
    chunks = []
    remaining = limit + 1
    while remaining:
        try:
            chunk = os.read(fd, remaining)
        except BlockingIOError as exc:
            raise GraphError("page is not a regular readable file") from exc
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    value = b"".join(chunks)
    if len(value) > limit:
        _error("page is larger than 128 KiB")
    return value


def _validate_relative_path(value: object) -> Path:
    if not isinstance(value, str) or not value or len(value) > PATH_LIMIT:
        _error("path must be a bounded graph-relative page path")
    if "\\" in value or "\x00" in value:
        _error("path is unsafe")
    path = Path(value)
    if path.is_absolute() or len(path.parts) < 2 or path.parts[0] != "pages":
        _error("path must name a page below pages/")
    if any(part in ("", ".", "..") for part in path.parts):
        _error("path contains an unsafe directory component")
    if path.suffix.casefold() != ".md":
        _error("path must name a markdown page")
    return path


def _open_directory(path, components=()):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = None
    try:
        fd = os.open(path, flags)
        for component in components:
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        raise GraphError("page directory is unsafe") from exc


def _open_page_parent(graph, relative):
    return _open_directory(graph, relative.parts[:-1])


def _identity(fd):
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def _open_page_parent_context(graph, relative):
    """Open a page parent and retain identities for the final write check."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = _open_directory(graph)
    graph_identity = _identity(fd)
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd, graph_identity, _identity(fd)
    except OSError as exc:
        os.close(fd)
        raise GraphError("page directory is unsafe") from exc


def _read_named(parent_fd, name):
    fd = None
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent_fd)
        except OSError as exc:
            raise GraphError("page path is unsafe or missing") from exc
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _error("page path is not a regular file")
        value = _read_fd(fd, PAGE_LIMIT)
        try:
            value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GraphError("page is not valid UTF-8") from exc
        return value, stat.S_IMODE(info.st_mode)
    finally:
        if fd is not None:
            os.close(fd)


def _page_bytes(graph, relative):
    parent = _open_page_parent(graph, relative)
    try:
        return _read_named(parent, relative.name)
    finally:
        os.close(parent)


class _GraphLock:
    """The same advisory lock used by the graph journal writer."""

    def __init__(self, graph):
        self.graph = graph
        self.journals_fd = None
        self.lock_fd = None

    def __enter__(self):
        graph_fd = _open_directory(self.graph)
        try:
            # Match append_journal: create only the lock's containing
            # directory, never a page or journal document.  O_NOFOLLOW on the
            # subsequent open rejects a pre-existing symlink.
            try:
                os.mkdir("journals", 0o755, dir_fd=graph_fd)
            except FileExistsError:
                pass
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
            self.journals_fd = os.open("journals", flags, dir_fd=graph_fd)
            os.close(graph_fd)
            graph_fd = None
            self.lock_fd = os.open(
                LOCK_NAME,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=self.journals_fd,
            )
            if not stat.S_ISREG(os.fstat(self.lock_fd).st_mode):
                _error("lock path is not a regular file")
            deadline = time.monotonic() + LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise GraphError("graph lock is busy; retry later")
                    time.sleep(min(0.02, remaining))
            return self
        except (OSError, GraphError) as exc:
            self.close()
            if graph_fd is not None:
                try:
                    os.close(graph_fd)
                except OSError:
                    pass
            if isinstance(exc, GraphError):
                raise
            raise GraphError("cannot lock graph safely") from exc

    def close(self):
        for name in ("lock_fd", "journals_fd"):
            fd = getattr(self, name)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, name, None)

    def __exit__(self, *_args):
        self.close()


def _relative_page(graph, relative):
    return (graph / relative).resolve(strict=True)


def _response(graph, relative, raw):
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GraphError("page is not valid UTF-8") from exc
    todos = []
    for line, text in enumerate(re.split(r"\r\n|\r|\n", content), 1):
        item = _task(text, line)
        if item is not None:
            todos.append(item)
            if len(todos) >= MAX_RESULTS:
                break
    absolute = _relative_page(graph, relative)
    return {
        "path": relative.as_posix(),
        "page": page_name(absolute, graph),
        "graphName": graph.name,
        "revision": hashlib.sha256(raw).hexdigest(),
        "content": content,
        "todos": todos,
    }


def _task(line, number):
    match = _MARKER_TASK.match(line)
    if match is not None:
        task = match.group("task").strip()
        if not task:
            return None
        marker = match.group("marker")
        checked = bool(match.group("checked"))
        return {
            "line": number,
            "task": task,
            "marker": marker,
            "done": marker == "DONE" or checked,
        }

    match = _CHECKBOX_TASK.match(line)
    if match is None:
        return None
    task = match.group("task").strip()
    if not task:
        return None
    return {
        "line": number,
        "task": task,
        "marker": "",
        "done": bool(match.group("checked")),
    }


def list_projects(graph):
    """Return every safe, bounded markdown page in ``pages/``."""
    graph = graph_path(graph)
    projects = []
    for absolute in markdown_files(graph):
        try:
            relative = absolute.relative_to(graph)
            if len(relative.parts) < 2 or relative.parts[0] != "pages":
                continue
            _validate_relative_path(relative.as_posix())
            # markdown_files has a broader shared-reader bound.  Planner
            # responses must only advertise pages this backend can read.
            _page_bytes(graph, relative)
        except (ValueError, GraphError):
            continue
        projects.append({"path": relative.as_posix(), "page": page_name(absolute, graph)})
        if len(projects) >= MAX_FILES:
            break
    projects.sort(key=lambda item: (item["path"].casefold(), item["path"]))
    return {"projects": projects, "graphName": graph.name}


def read_page(graph, path):
    """Read one existing page and return its exact-byte revision and tasks."""
    if isinstance(path, dict):
        path = path.get("path")
    graph = graph_path(graph)
    relative = _validate_relative_path(path)
    raw, _mode = _page_bytes(graph, relative)
    return _response(graph, relative, raw)


def _validate_revision(value: object) -> str:
    if not isinstance(value, str) or not _HEX_REVISION.fullmatch(value):
        _error("revision must be a SHA-256 hex digest")
    return value


def _validate_line(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _error("line must be a positive integer")
    return value


def _split_lines(content):
    # Only markdown's three ASCII newline encodings are separators here.  Any
    # other Unicode characters remain part of the line and are byte-stable.
    parts = re.split(r"(\r\n|\r|\n)", content)
    return parts


def _set_checkbox(line, match, done):
    start, end = match.span("checkbox")
    old = line[start:end]
    inner = old[1:-1]
    if done:
        position = next((i for i, char in enumerate(inner)
                         if char not in " \t"), 0)
        inner = inner[:position] + "x" + inner[position + 1:]
    else:
        inner = "".join(" " if char in "xX" else char for char in inner)
    return line[:start] + "[" + inner + "]" + line[end:]


def _toggle_line(line, done):
    marker_match = _MARKER_TASK.match(line)
    if marker_match is not None and marker_match.group("task").strip():
        # A marker is the status for marker tasks.  If a checkbox is also
        # present, keep both status representations consistent.
        start, end = marker_match.span("marker")
        line = line[:start] + ("DONE" if done else "TODO") + line[end:]
        if marker_match.group("checkbox") is not None:
            # The marker replacement has the same length, so the old spans
            # remain valid.
            line = _set_checkbox(line, marker_match, done)
        return line

    checkbox_match = _CHECKBOX_TASK.match(line)
    if checkbox_match is None or not checkbox_match.group("task").strip():
        _error("line does not contain a task")
    return _set_checkbox(line, checkbox_match, done)


def _replace_page(graph, relative, expected, replacement):
    """Atomically replace a page after best-effort final race checks.

    An editor which ignores the graph lock can still write after the final
    check; no pathname-based implementation can prevent that without taking
    over the editor's save mechanism.  These checks prevent clobbering a
    change observed before replacement and reject relocated directories.
    """
    parent_fd, graph_identity, parent_identity = _open_page_parent_context(graph, relative)
    name = relative.name
    try:
        current, mode = _read_named(parent_fd, name)
    except Exception:
        os.close(parent_fd)
        raise
    if current != expected:
        os.close(parent_fd)
        _error("page changed before write; reload the page")
    temporary = None
    temp_fd = None
    try:
        for attempt in range(10):
            candidate = f".project-planner-{os.getpid()}-{attempt}.tmp"
            try:
                temp_fd = os.open(candidate,
                                  os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                  mode, dir_fd=parent_fd)
                temporary = candidate
                break
            except FileExistsError:
                continue
        if temp_fd is None:
            _error("cannot create temporary page")
        os.fchmod(temp_fd, mode)
        view = memoryview(replacement)
        while view:
            view = view[os.write(temp_fd, view):]
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = None

        # Reopen the graph-relative parent immediately before replacement.
        # This prevents writing to an old, detached directory after a rename
        # or replacement of pages/ (and also checks the graph root identity).
        final_parent = None
        try:
            final_parent, final_graph_identity, final_parent_identity = \
                _open_page_parent_context(graph, relative)
            if (final_graph_identity != graph_identity or
                    final_parent_identity != parent_identity):
                _error("page directory changed before write; reload the page")
            current, _mode = _read_named(final_parent, name)
            if current != expected:
                _error("page changed before write; reload the page")
            if temporary is None:
                _error("cannot create temporary page")
            os.replace(temporary, name, src_dir_fd=final_parent, dst_dir_fd=final_parent)
            temporary = None
            os.fsync(final_parent)
        finally:
            if final_parent is not None:
                os.close(final_parent)
    except OSError as exc:
        raise GraphError(f"cannot replace page safely: {exc}") from exc
    finally:
        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except OSError:
                pass
        os.close(parent_fd)


def toggle_task(graph, path, revision=None, line=None, done=None):
    """Set one task's completion state, rejecting stale page revisions."""
    if isinstance(path, dict) and revision is None:
        request = path
        path = request.get("path")
        revision = request.get("revision")
        line = request.get("line")
        done = request.get("done")
    graph = graph_path(graph)
    relative = _validate_relative_path(path)
    revision = _validate_revision(revision)
    line = _validate_line(line)
    if not isinstance(done, bool):
        _error("done must be boolean")

    with _GraphLock(graph):
        raw, _mode = _page_bytes(graph, relative)
        if hashlib.sha256(raw).hexdigest() != revision:
            _error("page revision is stale; reload the page")
        content = raw.decode("utf-8")
        parts = _split_lines(content)
        bodies = parts[0::2]
        if line > len(bodies):
            _error("line does not contain a task")
        item = _task(bodies[line - 1], line)
        if item is None:
            _error("line does not contain a task")
        if item["done"] == done:
            return _response(graph, relative, raw)
        parts[(line - 1) * 2] = _toggle_line(bodies[line - 1], done)
        replacement = "".join(parts).encode("utf-8")
        if len(replacement) > PAGE_LIMIT:
            _error("page is larger than 128 KiB")
        _replace_page(graph, relative, raw, replacement)
    return _response(graph, relative, replacement)


def update_page(graph, path, revision=None, content=None):
    """Replace an existing page, subject to its exact UTF-8 revision."""
    if isinstance(path, dict) and revision is None:
        request = path
        path = request.get("path")
        revision = request.get("revision")
        content = request.get("content")
    graph = graph_path(graph)
    relative = _validate_relative_path(path)
    _validate_revision(revision)
    if not isinstance(content, str):
        _error("content must be a string")
    try:
        replacement = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise GraphError("content is not valid UTF-8") from exc
    if len(replacement) > PAGE_LIMIT:
        _error("content is larger than 128 KiB")

    with _GraphLock(graph):
        raw, _mode = _page_bytes(graph, relative)
        if hashlib.sha256(raw).hexdigest() != revision:
            _error("page revision is stale; reload the page")
        if raw != replacement:
            _replace_page(graph, relative, raw, replacement)
    return _response(graph, relative, replacement)


def files_list(graph, request=None):
    """List the folder declared by the selected page's ``file::`` property.

    The page is reread fresh on every call; only the pinned page path is
    accepted.  Any ``root`` supplied by the caller is ignored.
    """
    import project_files
    if isinstance(request, dict):
        path = request.get("path")
    else:
        path = request
    current = read_page(graph, path)
    root = project_files.resolve_root_from_content(graph_path(graph),
                                                   current["content"])
    listing = project_files.list_files(root)
    return {"path": current["path"], "page": current["page"],
            "graphName": current["graphName"], "root": listing["root"],
            "entries": listing["entries"], "truncated": listing["truncated"]}


def files_read(graph, request=None, file=None):
    """Read one folder-relative file declared by the selected page."""
    import project_files
    if isinstance(request, dict) and file is None:
        path = request.get("path")
        file = request.get("file")
    else:
        path = request
    current = read_page(graph, path)
    root = project_files.resolve_root_from_content(graph_path(graph),
                                                   current["content"])
    value = project_files.read_file(root, file)
    return {"path": current["path"], "page": current["page"],
            "graphName": current["graphName"], "root": value["root"],
            "file": value["path"], "size": value["size"],
            "content": value["content"]}


def files_git(graph, request=None):
    """Return scoped git status/diff for the selected page's folder."""
    import project_files
    if isinstance(request, dict):
        path = request.get("path")
    else:
        path = request
    current = read_page(graph, path)
    root = project_files.resolve_root_from_content(graph_path(graph),
                                                   current["content"])
    info = project_files.git_info(root)
    return {"path": current["path"], "page": current["page"],
            "graphName": current["graphName"], **info}


def _read_input():
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


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--graph", default=None,
                        help="graph directory; defaults to LOGSEQ_GRAPH or logseqGraph in settings.json")
    parser.add_argument("command", choices=("list", "page", "toggle", "update",
                                             "files-list", "files-read",
                                             "files-git"))
    try:
        args = parser.parse_args(argv)
        # Controlled SIGTERM handling for the whole CLI lifetime: aborting
        # the helper kills active git groups and exits nonzero with a
        # diagnostic instead of dying mid-operation or orphaning children.
        try:
            from project_files import _ensure_sigterm_handler as _arm_abort
            _arm_abort()
        except ImportError:
            pass
        graph = resolve_graph(args.graph)
        if args.command == "list":
            value = list_projects(graph)
        else:
            request = _read_input()
            if args.command == "page":
                value = read_page(graph, request.get("path"))
            elif args.command == "toggle":
                value = toggle_task(graph, request.get("path"), request.get("revision"),
                                    request.get("line"), request.get("done"))
            elif args.command == "update":
                value = update_page(graph, request.get("path"), request.get("revision"),
                                    request.get("content"))
            elif args.command == "files-list":
                value = files_list(graph, request)
            elif args.command == "files-read":
                value = files_read(graph, request)
            else:
                value = files_git(graph, request)
        print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        return 0
    except SystemExit as exc:
        return 0 if exc.code == 0 else 1
    except (GraphError, OSError, TypeError, ValueError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
