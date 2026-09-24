#!/usr/bin/env python3
"""Bounded, lock-aware page operations for a Logseq project planner.

The public functions mirror the JSON commands. ``list``/``page``/``toggle``/
``update`` (and the ``files-*`` helpers) deliberately accept only existing
pages below ``pages/`` and never create pages or journals. The sole explicit
page-creation path is the ``create-page`` subcommand below: it renders a new
page deterministically from a Logseq template (plus optional page properties)
and creates it with ``O_CREAT|O_EXCL|O_NOFOLLOW`` under the graph lock, never
overwriting an existing page. Writes use the graph's normal journal lock so
they are serialized with :func:`logseq_graph.append_journal`.

UUID mode (``{"project_id": "<uuid>"}``) resolves the current optional
note/folder fresh from the registry per operation, so Zotero-only projects
work without a note. Note tools fail clearly without a linked note; folder
tools use the registry folder directly when present.
"""

import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import NoReturn

from logseq_common import (GraphError, MAX_FILES, MAX_RESULTS, graph_path,
                           markdown_files, page_name, resolve_graph)

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import qscli


PAGE_LIMIT = 128 * 1024
# JSON string escaping can expand each UTF-8 byte substantially.  Keep the
# page limit strict while allowing a complete page to cross the transport.
# Transport cap; single source of truth is qscli.INPUT_LIMIT (same value).
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
    """Read one existing page and return its exact-byte revision and tasks.

    When *path* is a dict carrying ``project_id``, the note is resolved fresh
    from the registry (per-operation, never frozen). A Zotero-only project
    with no linked note fails clearly instead of reading an arbitrary page.
    """
    project_id = _project_id_from_request(path)
    if project_id:
        entry = _registry_entry_for_id(project_id)
        note = entry.get("logseq_path", "") or entry.get("path", "")
        if not note:
            _error("project has no linked note; link a note or use folder/Zotero tools")
        # Fresh registry wins: ignore a stale caller-supplied path.
        path = note
    elif isinstance(path, dict):
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
    if isinstance(path, dict):
        request = path
        pid = _project_id_from_request(request)
        if pid:
            entry = _registry_entry_for_id(pid)
            note = entry.get("logseq_path", "") or entry.get("path", "")
            if not note:
                _error("project has no linked note; link a note or use folder/Zotero tools")
            path = note
        else:
            path = request.get("path")
        if revision is None:
            revision = request.get("revision")
        if line is None:
            line = request.get("line")
        if done is None:
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
    if isinstance(path, dict):
        request = path
        pid = _project_id_from_request(request)
        if pid:
            entry = _registry_entry_for_id(pid)
            note = entry.get("logseq_path", "") or entry.get("path", "")
            if not note:
                _error("project has no linked note; link a note or use folder/Zotero tools")
            path = note
        else:
            path = request.get("path")
        if revision is None:
            revision = request.get("revision")
        if content is None:
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


def _validate_project_id(value: object) -> str:
    import uuid as _uuid
    if not isinstance(value, str) or not value.strip():
        _error("project_id must be a UUID string")
    try:
        return str(_uuid.UUID(value.strip()))
    except ValueError as exc:
        raise GraphError("project_id must be a UUID string") from exc


def _registry_entry_for_id(project_id: str) -> dict:
    """Return the fresh registry record for *project_id* (fail-closed)."""
    pid = _validate_project_id(project_id)
    try:
        import projects as _projects
    except ImportError as exc:
        raise GraphError("project registry is unusable") from exc
    try:
        data = _projects.list_projects()
    except Exception as exc:
        raise GraphError(f"project registry is unusable: {exc}") from exc
    entries = data.get("projects", []) if isinstance(data, dict) else []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") == pid:
            return entry
    _error("project id is unknown")


def _folder_root_for_entry(entry: dict):
    """Validate the registry folder for an entry dict, or None when empty."""
    folder = entry.get("local_folder", "") if isinstance(entry, dict) else ""
    if not folder:
        return None
    import project_files as _project_files
    expanded = os.path.expanduser(folder)
    candidate = Path(expanded)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GraphError("registered project folder is not accessible") from exc
    return _project_files._validate_root(resolved)


def _project_id_from_request(request) -> str:
    if isinstance(request, dict):
        raw = request.get("project_id", "")
        if isinstance(raw, str) and raw.strip():
            return _validate_project_id(raw)
    return ""


def _registry_folder_root(graph, logseq_path):
    """Return the validated registry folder for *logseq_path*, or None.

    None means the note is unregistered (or registered without a folder),
    and the caller falls back to the legacy page-level ``file::`` property.
    A registered non-empty folder is resolved strictly and screened with the
    ``project_files`` safety checks; failures raise instead of silently
    falling back to a possibly stale ``file::`` folder.  A corrupt registry
    likewise raises fail-closed.  The registry override for these helpers is
    the ``QUICKSHELL_PROJECTS_FILE`` environment variable (no caller root).
    """
    try:
        import projects as _projects
    except ImportError:
        return None
    try:
        folder = _projects.lookup_local_folder(logseq_path)
    except Exception as exc:
        raise GraphError(f"project registry is unusable: {exc}") from exc
    if not folder:
        return None
    import project_files as _project_files
    expanded = os.path.expanduser(folder)
    candidate = Path(expanded)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GraphError("registered project folder is not accessible") from exc
    return _project_files._validate_root(resolved)


def files_list(graph, request=None):
    """List the folder for a project id or a selected page.

    UUID mode (``{"project_id": "<uuid>"}``) resolves the folder fresh from
    the registry by id, so Zotero-only projects work without a note. When the
    entry also has a note, the note's ``file::`` remains a fallback when no
    registry folder is set. Legacy ``{"path": ...}`` keeps the previous
    page-pinned behavior.
    """
    import project_files
    project_id = _project_id_from_request(request)
    if project_id:
        entry = _registry_entry_for_id(project_id)
        root = _folder_root_for_entry(entry)
        note = entry.get("logseq_path", "") or entry.get("path", "")
        if root is None:
            if not note:
                _error("project has no linked folder; link a folder or use Zotero tools")
            current = read_page(graph, note)
            root = _registry_folder_root(graph_path(graph), current["path"])
            if root is None:
                root = project_files.resolve_root_from_content(graph_path(graph),
                                                               current["content"])
            listing = project_files.list_files(root)
            return {"project_id": project_id, "path": current["path"], "page": current["page"],
                    "graphName": current["graphName"], "root": listing["root"],
                    "entries": listing["entries"], "truncated": listing["truncated"]}
        listing = project_files.list_files(root)
        return {"project_id": project_id, "path": note, "page": entry.get("name", ""),
                "graphName": graph_path(graph).name, "root": listing["root"],
                "entries": listing["entries"], "truncated": listing["truncated"]}
    if isinstance(request, dict):
        path = request.get("path")
    else:
        path = request
    current = read_page(graph, path)
    root = _registry_folder_root(graph_path(graph), current["path"])
    if root is None:
        root = project_files.resolve_root_from_content(graph_path(graph),
                                                       current["content"])
    listing = project_files.list_files(root)
    return {"path": current["path"], "page": current["page"],
            "graphName": current["graphName"], "root": listing["root"],
            "entries": listing["entries"], "truncated": listing["truncated"]}


def files_read(graph, request=None, file=None):
    """Read one folder-relative file for a project id or selected page."""
    import project_files
    project_id = _project_id_from_request(request)
    if project_id:
        entry = _registry_entry_for_id(project_id)
        if isinstance(request, dict) and file is None:
            file = request.get("file")
        root = _folder_root_for_entry(entry)
        note = entry.get("logseq_path", "") or entry.get("path", "")
        if root is None:
            if not note:
                _error("project has no linked folder; link a folder or use Zotero tools")
            current = read_page(graph, note)
            root = _registry_folder_root(graph_path(graph), current["path"])
            if root is None:
                root = project_files.resolve_root_from_content(graph_path(graph),
                                                               current["content"])
            value = project_files.read_file(root, file)
            return {"project_id": project_id, "path": current["path"], "page": current["page"],
                    "graphName": current["graphName"], "root": value["root"],
                    "file": value["path"], "size": value["size"],
                    "content": value["content"]}
        value = project_files.read_file(root, file)
        return {"project_id": project_id, "path": note, "page": entry.get("name", ""),
                "graphName": graph_path(graph).name, "root": value["root"],
                "file": value["path"], "size": value["size"],
                "content": value["content"]}
    if isinstance(request, dict) and file is None:
        path = request.get("path")
        file = request.get("file")
    else:
        path = request
    current = read_page(graph, path)
    root = _registry_folder_root(graph_path(graph), current["path"])
    if root is None:
        root = project_files.resolve_root_from_content(graph_path(graph),
                                                       current["content"])
    value = project_files.read_file(root, file)
    return {"path": current["path"], "page": current["page"],
            "graphName": current["graphName"], "root": value["root"],
            "file": value["path"], "size": value["size"],
            "content": value["content"]}


def files_git(graph, request=None):
    """Return scoped git status/diff for a project id or selected page."""
    import project_files
    project_id = _project_id_from_request(request)
    if project_id:
        entry = _registry_entry_for_id(project_id)
        root = _folder_root_for_entry(entry)
        note = entry.get("logseq_path", "") or entry.get("path", "")
        if root is None:
            if not note:
                _error("project has no linked folder; link a folder or use Zotero tools")
            current = read_page(graph, note)
            root = _registry_folder_root(graph_path(graph), current["path"])
            if root is None:
                root = project_files.resolve_root_from_content(graph_path(graph),
                                                               current["content"])
            info = project_files.git_info(root)
            return {"project_id": project_id, "path": current["path"], "page": current["page"],
                    "graphName": current["graphName"], **info}
        info = project_files.git_info(root)
        return {"project_id": project_id, "path": note, "page": entry.get("name", ""),
                "graphName": graph_path(graph).name, **info}
    if isinstance(request, dict):
        path = request.get("path")
    else:
        path = request
    current = read_page(graph, path)
    root = _registry_folder_root(graph_path(graph), current["path"])
    if root is None:
        root = project_files.resolve_root_from_content(graph_path(graph),
                                                       current["content"])
    info = project_files.git_info(root)
    return {"path": current["path"], "page": current["page"],
            "graphName": current["graphName"], **info}


# ---------------------------------------------------------------------------
# create-page: the sole explicit page-creation path (template-driven, O_EXCL)
# ---------------------------------------------------------------------------

_CREATE_ALLOWED_BASE = frozenset({"stage", "page", "template", "template_page",
                                  "properties"})
_CREATE_PROPERTIES_LIMIT = 16
_CREATE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CREATE_PROP_RE = re.compile(r"^([A-Za-z0-9_-]+)::\s?(.*)$")
_CREATE_RESERVED_KEYS = frozenset({"template", "template-including-parent"})
_CREATE_BULLET_RE = re.compile(r"^(\t*)-(?: (.*))?$")
_CREATE_TODAY_RE = re.compile(r"<%\s*today\s*%>")
_CREATE_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug",
                  "Sep", "Oct", "Nov", "Dec")


class _TemplateBlock:
    __slots__ = ("content", "conts", "children", "depth")

    def __init__(self, content, depth):
        self.content = content
        self.conts = []  # list of (extra_indent, text)
        self.children = []
        self.depth = depth


def _normalize_create_target(value):
    """Normalize a ``create-page`` page reference to ``pages/<name>.md``.

    Accepts a bare page name (Logseq ``/`` namespace separators map to the
    on-disk ``___`` spelling, always flat directly below ``pages/``) or an
    explicit ``pages/<name>.md`` path (a literal graph-relative path, which
    may address a nested page). The result always goes through
    :func:`_validate_relative_path`, so absolute paths, backslashes, NUL,
    ``.``/``..``/empty components, non-markdown suffixes, and anything
    outside ``pages/`` are rejected exactly like the read/update paths.
    """
    if not isinstance(value, str) or not value or len(value) > PATH_LIMIT:
        _error("page must be a bounded page name or pages/ path")
    if "\\" in value or "\x00" in value or "\n" in value or "\r" in value:
        _error("path is unsafe")
    if value.startswith("pages/"):
        return _validate_relative_path(value)
    if Path(value).is_absolute():
        _error("page must be a page name below pages/")
    raw = value
    if raw.endswith(".md"):
        raw = raw[:-len(".md")]
    if not raw:
        _error("page must be a page name below pages/")
    if any(part in ("", ".", "..") for part in raw.split("/")):
        _error("page contains an unsafe component")
    mapped = raw.replace("/", "___")
    if not mapped:
        _error("page must be a page name below pages/")
    return _validate_relative_path("pages/" + mapped + ".md")


def _validate_create_properties(value):
    """Validate the optional ``properties`` merge object (ordered dict)."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        _error("properties must be an object")
    if len(value) > _CREATE_PROPERTIES_LIMIT:
        _error("properties must have at most 16 entries")
    merged = {}
    for key, item in value.items():
        if (not isinstance(key, str) or len(key) > 64
                or not _CREATE_KEY_RE.fullmatch(key)):
            _error("property keys must match [A-Za-z0-9_-]+, at most 64 chars")
        if key.casefold() in _CREATE_RESERVED_KEYS:
            _error(f"property {key!r} is reserved")
        if (not isinstance(item, str) or len(item) > 512
                or "\n" in item or "\r" in item or "\x00" in item):
            _error("property values must be single-line strings, at most 512 chars")
        merged[key] = item
    return merged


def _create_ws_width(line):
    width = 0
    for char in line:
        if char == "\t":
            width += 4
        elif char == " ":
            width += 1
        else:
            break
    return width


def _parse_template_blocks(text):
    """Parse Logseq bullet blocks (any nesting depth) from template text.

    Bullets are ``- `` lines indented with one TAB per nesting level (bare
    ``-`` is an empty block). Non-bullet, non-blank lines attach as
    continuation lines (block properties such as ``key:: value``) to the
    deepest preceding bullet whose indent is shallower. Page-level lines
    (bare properties, headings, prose) belong to no block and are ignored.
    """
    roots = []
    stack = []
    for raw in re.split(r"\r\n|\r|\n", text):
        if raw.strip() == "":
            continue
        match = _CREATE_BULLET_RE.match(raw)
        if match is not None:
            content = match.group(2)
            if content is None:
                content = ""
            block = _TemplateBlock(content.rstrip(), len(match.group(1)))
            while stack and stack[-1].depth >= block.depth:
                stack.pop()
            if stack:
                stack[-1].children.append(block)
            else:
                roots.append(block)
            stack.append(block)
            continue
        width = _create_ws_width(raw)
        indent_tabs = width // 4
        target = None
        for candidate in reversed(stack):
            if candidate.depth <= indent_tabs:
                target = candidate
                break
        if target is None:
            continue
        prefix = "\t" * target.depth
        stripped = raw.lstrip(" \t")
        leading = raw[:len(raw) - len(stripped)]
        if leading.startswith(prefix):
            extra = leading[len(prefix):]
        else:
            extra = leading
        target.conts.append((extra, stripped.rstrip()))
    return roots


def _block_prop_key_value(text):
    match = _CREATE_PROP_RE.match(text.strip())
    if match is None:
        return None
    return match.group(1), match.group(2).strip()


def _find_template_blocks(roots, name):
    """Collect every block carrying ``template:: <name>`` (exact match)."""
    hits = []

    def visit(block):
        lines = []
        if block.content.strip() != "":
            lines.append(block.content)
        lines.extend(text for _, text in block.conts)
        for line in lines:
            parsed = _block_prop_key_value(line)
            if parsed is not None and parsed[0] == "template" \
                    and parsed[1] == name:
                hits.append(block)
                break
        for child in block.children:
            visit(child)

    for root in roots:
        visit(root)
    return hits


def _template_including_parent(block):
    lines = []
    if block.content.strip() != "":
        lines.append(block.content)
    lines.extend(text for _, text in block.conts)
    for line in lines:
        parsed = _block_prop_key_value(line)
        if parsed is not None and parsed[0] == "template-including-parent":
            return parsed[1].casefold() == "true"
    return False


def _strip_template_markers(block):
    """Drop ``template``/``template-including-parent`` props from a subtree."""
    if block.content.strip() != "":
        parsed = _block_prop_key_value(block.content)
        if parsed is not None and parsed[0] in _CREATE_RESERVED_KEYS:
            block.content = ""
    kept = []
    for extra, text in block.conts:
        parsed = _block_prop_key_value(text)
        if parsed is not None and parsed[0] in _CREATE_RESERVED_KEYS:
            continue
        kept.append((extra, text))
    block.conts = kept
    for child in block.children:
        _strip_template_markers(child)


def _block_is_property_only(block):
    """True when a block carries only ``key:: value`` property lines."""
    if block.children:
        return False
    if block.content.strip() != "" \
            and _block_prop_key_value(block.content) is None:
        return False
    return all(_block_prop_key_value(text) is not None
               for _, text in block.conts)


def _today_logseq():
    """Local date in Logseq page-title format (e.g. "Sep 22nd, 2026")."""
    import datetime as _datetime
    today = _datetime.date.today()
    day = today.day
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
    return f"{_CREATE_MONTHS[today.month - 1]} {day}{suffix}, {today.year}"


def _expand_today_macros(text):
    """Expand ``<% today %>``; every other ``<% ... %>`` stays verbatim."""
    return _CREATE_TODAY_RE.sub(_today_logseq(), text)


def _render_template_block(block, depth, out):
    content = block.content.strip()
    if content == "":
        out.append("\t" * depth + "-")
    else:
        out.append("\t" * depth + "- " + _expand_today_macros(content))
    for extra, text in block.conts:
        out.append("\t" * depth + extra + _expand_today_macros(text.strip()))
    for child in block.children:
        _render_template_block(child, depth + 1, out)


def _render_create(graph, page, template, template_page, properties):
    """Shared prepare/commit renderer: ``(relative, content, name, parent?)``.

    Returns the normalized target, the exact file text, the template name
    (``""`` without a template), and the template's including-parent flag.
    """
    graph = graph_path(graph)
    relative = _normalize_create_target(page)
    extra_props = _validate_create_properties(properties)
    template_name = ""
    including_parent = False
    roots = []
    if template is not None:
        if (not isinstance(template, str) or not template
                or len(template) > 256 or "\x00" in template
                or "\n" in template or "\r" in template):
            _error("template must be a bounded name")
        template_name = template
        fallback = template_page if template_page is not None else "Templates"
        if not isinstance(fallback, str):
            _error("template_page must be a bounded page name or pages/ path")
        template_relative = _normalize_create_target(fallback)
        raw, _mode = _page_bytes(graph, template_relative)
        parsed = _parse_template_blocks(raw.decode("utf-8"))
        hits = _find_template_blocks(parsed, template_name)
        if not hits:
            _error(f"template not found: {template_name}")
        if len(hits) > 1:
            _error(f"template is ambiguous: {template_name}")
        marker = hits[0]
        including_parent = _template_including_parent(marker)
        _strip_template_markers(marker)
        if including_parent:
            if marker.content.strip() == "" and not marker.conts:
                roots = list(marker.children)
            else:
                roots = [marker]
        else:
            roots = list(marker.children)
    leading = []
    if roots and _block_is_property_only(roots[0]):
        first = roots.pop(0)
        if first.content.strip() != "":
            parsed = _block_prop_key_value(first.content)
            if parsed is not None:
                leading.append(parsed)
        for _, text in first.conts:
            parsed = _block_prop_key_value(text)
            if parsed is not None:
                leading.append(parsed)
    merged = []
    seen = set()
    for key, item in leading:
        if key in seen:
            continue
        seen.add(key)
        merged.append([key, item])
    for key, item in extra_props.items():
        for entry in merged:
            if entry[0] == key:
                entry[1] = item
                break
        else:
            merged.append([key, item])
    lines = []
    for key, item in merged:
        if item == "":
            lines.append(f"{key}::")
        else:
            lines.append(f"{key}:: {_expand_today_macros(item)}")
    for root in roots:
        _render_template_block(root, 0, lines)
    if not lines:
        return relative, "", template_name, including_parent
    content = "\n".join(lines) + "\n"
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise GraphError("content is not valid UTF-8") from exc
    if len(encoded) > PAGE_LIMIT:
        _error("content is larger than 128 KiB")
    return relative, content, template_name, including_parent


def _unlink_created_only(parent_fd, name, created_identity):
    """Unlink *name* only when it still refers to the file this call created.

    A write/fsync/close failure after ``O_EXCL`` creation must not delete a
    foreign file that raced into the target name in the interim (e.g. our
    file was renamed away and something else now occupies the name).
    Compare the captured dev/ino of the created file against a
    non-following stat of the name; on any mismatch or stat failure, leave
    the name in place. ``created_identity`` is ``None`` when the created
    file's identity could not be captured (fail-closed the other way: still
    attempt the unlink, since the name could only have come from our own
    ``O_EXCL`` create moments ago).
    """
    if created_identity is not None:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            return
        if (created_identity != (current.st_dev, current.st_ino)):
            return
    try:
        os.unlink(name, dir_fd=parent_fd)
    except OSError:
        pass


def _fd_identity(fd):
    """Return ``(st_dev, st_ino)`` for *fd*, or ``None`` when unavailable."""
    try:
        info = os.fstat(fd)
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


def _create_page_excl(graph, relative, raw):
    """Create an absent page atomically; never truncate or replace."""
    parent_fd, _graph_identity, _parent_identity = \
        _open_page_parent_context(graph, relative)
    name = relative.name
    try:
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise GraphError("cannot create page safely") from exc
        else:
            _error("page already exists")
        try:
            fd = os.open(name,
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o644, dir_fd=parent_fd)
        except FileExistsError:
            _error("page already exists")
        except OSError as exc:
            raise GraphError("cannot create page safely") from exc
        try:
            os.fchmod(fd, 0o644)
            view = memoryview(raw)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        except OSError as exc:
            created_identity = _fd_identity(fd)
            try:
                os.close(fd)
            except OSError:
                pass
            _unlink_created_only(parent_fd, name, created_identity)
            raise GraphError(f"cannot create page safely: {exc}") from exc
        created_identity = _fd_identity(fd)
        try:
            os.close(fd)
        except OSError as exc:
            _unlink_created_only(parent_fd, name, created_identity)
            raise GraphError(f"cannot create page safely: {exc}") from exc
        try:
            os.fsync(parent_fd)
        except OSError as exc:
            raise GraphError(f"cannot create page safely: {exc}") from exc
    finally:
        os.close(parent_fd)


def create_page(graph, request):
    """Prepare or commit a new Logseq page from a template.

    ``prepare`` renders deterministically and returns the exact bytes (plus
    their SHA-256) for approval; ``commit`` re-renders through the same code
    path, requires the bytes to still match ``expected_sha256``, requires an
    optional ``expected_target`` (the prepared target) to still match,
    requires the target to still be absent, and creates it with ``O_EXCL``
    (never overwriting) under the graph lock.
    """
    if not isinstance(request, dict):
        _error("JSON input must be an object")
    stage = request.get("stage")
    if stage not in ("prepare", "commit"):
        _error("stage must be \"prepare\" or \"commit\"")
    allowed = set(_CREATE_ALLOWED_BASE)
    if stage == "commit":
        allowed.add("expected_sha256")
        allowed.add("expected_target")
    for key in request:
        if key not in allowed:
            _error(f"unknown field: {key}")
    if "page" not in request:
        _error("page is required")
    if "template_page" in request and request["template_page"] is not None \
            and not isinstance(request["template_page"], str):
        _error("template_page must be a bounded page name or pages/ path")
    if stage == "prepare":
        relative, content, template_name, including_parent = _render_create(
            graph, request["page"], request.get("template"),
            request.get("template_page"), request.get("properties", {}))
        return {
            "stage": "prepare",
            "target": relative.as_posix(),
            "content": content,
            "content_sha256": hashlib.sha256(
                content.encode("utf-8")).hexdigest(),
            "template": template_name,
            "template_including_parent": including_parent,
        }
    expected = request.get("expected_sha256")
    if not isinstance(expected, str) or not _HEX_REVISION.fullmatch(expected):
        _error("expected_sha256 must be a SHA-256 hex digest")
    graph = graph_path(graph)
    with _GraphLock(graph):
        relative, content, _template_name, _including_parent = _render_create(
            graph, request["page"], request.get("template"),
            request.get("template_page"), request.get("properties", {}))
        bound_target = request.get("expected_target")
        if bound_target is not None:
            if not isinstance(bound_target, str) or not bound_target:
                _error("expected_target must be the prepared target path")
            if bound_target != relative.as_posix():
                _error("target changed, re-run prepare")
        try:
            encoded = content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise GraphError("content is not valid UTF-8") from exc
        if hashlib.sha256(encoded).hexdigest() != expected:
            _error("content changed, re-run prepare")
        _create_page_excl(graph, relative, encoded)
    return {
        "stage": "commit",
        "created": True,
        "target": relative.as_posix(),
        "content_sha256": expected,
    }


# ---------------------------------------------------------------------------
# CLI (shared plumbing lives in qscli.py; argv stays byte-identical)
# ---------------------------------------------------------------------------

def _read_input():
    """Backward-compatible stdin reader (qscli transport, GraphError face).

    Kept because tests call it directly; the CLI itself uses
    :func:`qscli.read_input`. Same 1 MiB cap (``qscli.INPUT_LIMIT``).
    """
    try:
        return qscli.read_input()
    except ValueError as exc:
        raise GraphError(str(exc)) from exc


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = qscli.SafeParser(description=__doc__)
    qscli.add_global_flags(parser, graph=True)  # --graph defaults to LOGSEQ_GRAPH or logseqGraph in settings.json
    parser.add_argument("command", choices=("list", "page", "toggle", "update",
                                            "files-list", "files-read",
                                            "files-git", "create-page"))
    return parser.parse_args(argv if argv is not None else sys.argv[1:])


def _dispatch(args: argparse.Namespace) -> dict:
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
        return list_projects(graph)
    request = qscli.read_input()
    if args.command == "page":
        # Pass the full request so UUID mode resolves fresh; legacy
        # {"path": ...} keeps working via read_page fallback.
        return read_page(graph, request)
    elif args.command == "toggle":
        return toggle_task(graph, request, request.get("revision"),
                           request.get("line"), request.get("done"))
    elif args.command == "update":
        return update_page(graph, request, request.get("revision"),
                           request.get("content"))
    elif args.command == "files-list":
        return files_list(graph, request)
    elif args.command == "files-read":
        return files_read(graph, request)
    elif args.command == "create-page":
        return create_page(graph, request)
    else:
        return files_git(graph, request)


_BOUNDED_EXCEPTIONS = (GraphError, OSError, TypeError, ValueError,
                       UnicodeError, RecursionError, OverflowError)


def main(argv=None) -> int:
    return qscli.run_main(_parse_args, _dispatch, "project_planner",
                          _BOUNDED_EXCEPTIONS, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
