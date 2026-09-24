#!/usr/bin/env python3
"""Registry-pinned linked-folder list/read/write for the project planner agent.

The registry ``local_folder`` is authoritative. Requests carry the pinned
``QS_PROJECT_ID`` (preferred, works folder-only with no Logseq graph) or, for
legacy workers, the pinned ``QS_PROJECT_PATH`` page which resolves through the
existing ``projects.lookup_local_folder`` supported lookup only. There is no
``file::`` fallback here: the legacy ``logseq_project_files``/``read_file``/
``git`` tools keep that read-only behavior separately.

Only a folder-relative ``file`` is model-supplied; the root is resolved fresh
per operation and never overridden. Writes require an existing file revision
(``sha256`` of the exact current bytes) for overwrites and explicit
``create:true`` for new files, preserve permissions on overwrite, constrain
size to 128 KiB UTF-8 text, reject traversal/symlinks/protected
secrets/``.git``, and use pinned directory-fd patterns (``O_NOFOLLOW`` /
``O_DIRECTORY``) so swapped ancestors cannot redirect access. No shell, no
unscoped filesystem access.

File writes never create parent directories. The single explicit
directory-creation path is ``create-dir`` (with its ``create-dir-preflight``
binding preview): home-only ``mkdir -p`` with the same no-symlink fd walking
and a dev/ino binding recheck, so an approved parent cannot be swapped before
creation.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import NoReturn

from logseq_common import GraphError

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import qscli

READ_LIMIT = 128 * 1024
WRITE_LIMIT = 128 * 1024
# Transport cap; single source of truth is qscli.INPUT_LIMIT (same value).
INPUT_LIMIT = 1024 * 1024
PATH_LIMIT = 4096
LOCK_NAME = ".project-folder.lock"
LOCK_TIMEOUT = 2.0

_HEX_REVISION = re.compile(r"^[0-9a-f]{64}$")


def _error(message: str) -> NoReturn:
    raise GraphError(message)


def _validate_project_id(value: object) -> str:
    import uuid as _uuid

    if not isinstance(value, str) or not value.strip():
        _error("project_id must be a UUID string")
    try:
        return str(_uuid.UUID(value.strip()))
    except ValueError as exc:
        raise GraphError("project_id must be a UUID string") from exc


def _project_id_from_request(request) -> str:
    if isinstance(request, dict):
        raw = request.get("project_id", "")
        if isinstance(raw, str) and raw.strip():
            return _validate_project_id(raw)
    return ""


def _registry_entry_for_id(project_id: str) -> dict:
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
    folder = entry.get("local_folder", "") if isinstance(entry, dict) else ""
    if not folder:
        return None
    import project_files as _pf

    expanded = os.path.expanduser(folder)
    candidate = Path(expanded)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GraphError("registered project folder is not accessible") from exc
    return _pf._validate_root(resolved)


def _legacy_folder_root(logseq_path: str):
    """Resolve a legacy pinned page via the existing registry lookup only."""
    if not isinstance(logseq_path, str) or not logseq_path.strip():
        _error("project mode is not active")
    try:
        import projects as _projects
    except ImportError as exc:
        raise GraphError("project registry is unusable") from exc
    try:
        folder = _projects.lookup_local_folder(logseq_path.strip())
    except Exception as exc:
        raise GraphError(f"project registry is unusable: {exc}") from exc
    if not folder:
        _error("project has no linked folder; link a folder or use Zotero tools")
    import project_files as _pf

    expanded = os.path.expanduser(folder)
    candidate = Path(expanded)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GraphError("registered project folder is not accessible") from exc
    return _pf._validate_root(resolved)


def resolve_root(request) -> tuple[Path, str, str]:
    """Return ``(root, project_id, note)`` for a list/read/write request.

    ``project_id`` wins when present; otherwise the legacy ``path`` resolves
    through ``lookup_local_folder`` only. Both paths fail closed when no
    registry folder is linked.
    """
    if not isinstance(request, dict):
        _error("JSON input must be an object")
    project_id = _project_id_from_request(request)
    if project_id:
        entry = _registry_entry_for_id(project_id)
        root = _folder_root_for_entry(entry)
        note = entry.get("logseq_path", "") or entry.get("path", "")
        if not isinstance(note, str):
            note = ""
        if root is None:
            _error("project has no linked folder; link a folder or use Zotero tools")
        assert root is not None
        return root, project_id, note
    raw_path = request.get("path", "")
    if isinstance(raw_path, str) and raw_path.strip():
        root = _legacy_folder_root(raw_path.strip())
        return root, "", raw_path.strip()
    _error("project mode is not active")


def _validate_revision(value: object) -> str:
    if not isinstance(value, str) or not _HEX_REVISION.fullmatch(value):
        _error("revision must be a SHA-256 hex digest")
    return value


def _read_current_bytes(root_fd: int, parent_fd: int, leaf: str) -> tuple[bytes, int]:
    """Read bounded current bytes + mode for *leaf* via pinned fds."""
    import project_files as _pf

    fd = -1
    try:
        fd = _pf._open_child_fd(parent_fd, leaf, directory=False)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _error("file is not a regular file")
        if info.st_size > READ_LIMIT:
            _error("file is larger than 128 KiB")
        chunks: list[bytes] = []
        remaining = READ_LIMIT + 1
        while remaining > 0:
            try:
                chunk = os.read(fd, min(32768, remaining))
            except BlockingIOError as exc:
                raise GraphError("file is not a regular readable file") from exc
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except OSError as exc:
        raise GraphError("file is not accessible") from exc
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
    if len(raw) > READ_LIMIT:
        _error("file is larger than 128 KiB")
    try:
        mode = stat.S_IMODE(os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False).st_mode)
    except OSError as exc:
        raise GraphError("file is not accessible") from exc
    return raw, mode


def _open_parent_fd(root_fd: int, rel: Path) -> tuple[int, list[int]]:
    """Open the immediate parent of *rel* below *root_fd* (fd-pinned walk).

    Returns ``(parent_fd, held)`` where ``parent_fd`` is ``root_fd`` itself
    for top-level files (no new fd) or a newly opened directory fd the caller
    owns, and ``held`` holds intermediate directory fds the caller must close.
    Every component opens with ``O_DIRECTORY | O_NOFOLLOW`` so symlinks and
    swapped ancestors fail instead of redirecting.
    """
    import project_files as _pf

    parts = rel.parts
    if len(parts) == 1:
        return root_fd, []
    held: list[int] = []
    current = root_fd
    try:
        for part in parts[:-1]:
            child = _pf._open_child_fd(current, part, directory=True)
            info = os.fstat(child)
            if not stat.S_ISDIR(info.st_mode):
                try:
                    os.close(child)
                except OSError:
                    pass
                _error("file is not accessible")
            if current != root_fd:
                held.append(current)
            current = child
        return current, held
    except BaseException:
        _pf._close_all(held)
        # current is either root_fd (not owned) or a fresh fd needing close.
        if current != root_fd:
            try:
                os.close(current)
            except OSError:
                pass
        raise


class _FolderLock:
    """Stable per-root advisory lock serializing cooperating folder writers.

    The lock file lives at ``<root>/.project-folder.lock`` (itself excluded
    from listing/reads/writes) and is opened via the pinned root fd with
    ``O_NOFOLLOW`` so a symlinked lock fails closed. ``fcntl`` exclusive
    locking with a bounded timeout mirrors the page-writer convention:
    cooperating helpers queue on the lock from initial revision check through
    final replace, so deterministic concurrent tests have exactly one winner.
    Hostile writers ignoring the lock remain outside the guarantees (same as
    page writes); the revision recheck still protects them best-effort.
    """

    def __init__(self, root_fd: int):
        self.root_fd = root_fd
        self.lock_fd: int | None = None

    def __enter__(self):
        flags = (os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                 | getattr(os, "O_CLOEXEC", 0))
        try:
            fd = os.open(LOCK_NAME, flags, 0o600, dir_fd=self.root_fd)
        except OSError as exc:
            raise GraphError("cannot lock project folder safely") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                _error("lock path is not a regular file")
        except OSError as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            if isinstance(exc, GraphError):
                raise
            raise GraphError("cannot lock project folder safely") from exc
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    _error("project folder is busy; retry later")
                time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
            except OSError as exc:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise GraphError("cannot lock project folder safely") from exc
        self.lock_fd = fd
        return self

    def __exit__(self, *_args):
        if self.lock_fd is not None:
            try:
                os.close(self.lock_fd)
            except OSError:
                pass
            self.lock_fd = None


def _parse_binding(request: dict) -> tuple[str | None, str | None, str | None]:
    """Extract the approval binding (expected root identity) if present.

    Device/inode travel as decimal strings (never JSON numbers): ``st_dev``/
    ``st_ino`` routinely exceed JS ``MAX_SAFE_INTEGER``, so numeric transport
    would silently lose precision. Exact string comparison preserves identity.
    """
    raw_root = request.get("expected_root")
    raw_dev = request.get("expected_root_dev")
    raw_ino = request.get("expected_root_ino")
    if raw_root is None and raw_dev is None and raw_ino is None:
        return None, None, None
    if not isinstance(raw_root, str) or not raw_root:
        _error("expected_root must be a bounded path")
    if len(raw_root) > PATH_LIMIT or "\x00" in raw_root:
        _error("expected_root is unsafe")
    for label, raw in (("expected_root_dev", raw_dev),
                       ("expected_root_ino", raw_ino)):
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            _error(f"{label} must be a decimal digit string")
        if len(raw) > 20 or not raw.isdigit():
            _error(f"{label} must be a decimal digit string")
    assert isinstance(raw_root, str)
    assert isinstance(raw_dev, str) and isinstance(raw_ino, str)
    return raw_root, raw_dev, raw_ino


def _check_binding(validated: Path, root_fd: int,
                   expected: tuple[str | None, str | None, str | None]) -> None:
    """Reject a stale approval binding (registry switch or root swap)."""
    exp_root, exp_dev, exp_ino = expected
    if exp_root is None:
        return
    assert exp_dev is not None and exp_ino is not None
    if str(validated) != exp_root:
        _error("project folder changed; read the file again and request a new approval")
    try:
        info = os.fstat(root_fd)
    except OSError as exc:
        raise GraphError("project folder is not accessible") from exc
    if str(info.st_dev) != exp_dev or str(info.st_ino) != exp_ino:
        _error("project folder changed; read the file again and request a new approval")


def preflight(request=None) -> dict:
    """Return the approval binding for one folder-relative target.

    Success means the destination is writable-policy-clean: traversal,
    symlinks, sensitive/protected, registry, and lock/tmp names are already
    rejected. ``exists`` distinguishes create (False, parent exists) from
    update (True plus fresh ``revision``/``size``). Any error must abort the
    write before approval; callers must never treat an arbitrary read error
    as a valid create preflight.
    """
    import project_files as _pf

    if not isinstance(request, dict):
        _error("JSON input must be an object")
    file = request.get("file")
    root, project_id, note = resolve_root(request)
    rel = _pf._validate_child(file)
    if _pf.is_excluded(root, rel):
        _error("file is sensitive or protected")
    try:
        resolved = Path(root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GraphError("project folder is not accessible") from exc
    validated = _pf._validate_root(resolved)
    root_fd = _pf._open_root_fd(validated)
    try:
        try:
            info = os.fstat(root_fd)
        except OSError as exc:
            raise GraphError("project folder is not accessible") from exc
        root_dev, root_ino = str(info.st_dev), str(info.st_ino)
        parent_fd, held = _open_parent_fd(root_fd, rel)
        try:
            leaf = rel.parts[-1]
            try:
                leaf_stat = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                out: dict = {"root": str(validated), "root_dev": root_dev,
                             "root_ino": root_ino, "file": rel.as_posix(),
                             "exists": False}
                if project_id:
                    out["project_id"] = project_id
                    out["path"] = note
                else:
                    out["path"] = note
                return out
            except OSError as exc:
                raise GraphError("file is not accessible") from exc
            if stat.S_ISLNK(leaf_stat.st_mode):
                _error("file is not a regular file")
            if stat.S_ISDIR(leaf_stat.st_mode):
                _error("file is not a regular file")
            if not stat.S_ISREG(leaf_stat.st_mode):
                _error("file is not a regular file")
            current_raw, _mode = _read_current_bytes(root_fd, parent_fd, leaf)
            revision = hashlib.sha256(current_raw).hexdigest()
            out = {"root": str(validated), "root_dev": root_dev,
                   "root_ino": root_ino, "file": rel.as_posix(),
                   "exists": True, "revision": revision,
                   "size": len(current_raw)}
            if project_id:
                out["project_id"] = project_id
                out["path"] = note
            else:
                out["path"] = note
            return out
        finally:
            _pf._close_all(held)
            if parent_fd != root_fd:
                try:
                    os.close(parent_fd)
                except OSError:
                    pass
    finally:
        try:
            os.close(root_fd)
        except OSError:
            pass


def list_folder(request=None) -> dict:
    import project_files as _pf

    root, project_id, note = resolve_root(request or {})
    listing = _pf.list_files(root)
    value: dict = {"root": listing["root"], "entries": listing["entries"],
                   "truncated": listing["truncated"]}
    if project_id:
        value["project_id"] = project_id
        value["path"] = note
    else:
        value["path"] = note
    return value


def read_folder(request=None, file=None) -> dict:
    import project_files as _pf

    if isinstance(request, dict) and file is None:
        file = request.get("file")
    root, project_id, note = resolve_root(request or {})
    rel = _pf._validate_child(file)
    if _pf.is_excluded(root, rel):
        _error("file is sensitive or protected")
    # Reuse the pinned-fd reader, then attach the content revision so
    # overwrites can require the exact observed version.
    value = _pf.read_file(root, rel.as_posix())
    revision = hashlib.sha256(value["content"].encode("utf-8")).hexdigest()
    out: dict = {"root": value["root"], "file": value["path"],
                 "size": value["size"], "content": value["content"],
                 "revision": revision}
    if project_id:
        out["project_id"] = project_id
        out["path"] = note
    else:
        out["path"] = note
    return out


def write_folder(request=None, file=None, content=None, revision=None,
                 create=None, expected_root=None, expected_root_dev=None,
                 expected_root_ino=None) -> dict:
    """Create or overwrite one UTF-8 text file below the pinned folder.

    Overwrites require the exact ``revision`` (sha256 of current bytes) from
    a fresh ``preflight``; creations require explicit ``create:true`` with no
    revision and fail when the file already exists. When the approval binding
    (``expected_root``/``expected_root_dev``/``expected_root_ino`` from
    ``preflight``) is supplied, the fresh registry resolution and the pinned
    root-fd identity must match it or the write is rejected (registry switch
    or root swap). Parent directories are never created by file writes
    (``create-dir`` is the single explicit home-only directory-creation
    path). Permissions are
    preserved on overwrite; new files use ``0o644`` (masked by umask). A
    stable per-root advisory lock serializes cooperating writers from the
    initial revision check through final replace. All opens are fd-relative
    with ``O_NOFOLLOW``; symlinks, ``.git``/sensitive/protected/registry/lock
    paths, traversal, NUL bytes, and oversize content are rejected.
    """
    import project_files as _pf

    if isinstance(request, dict):
        if file is None:
            file = request.get("file")
        if content is None:
            content = request.get("content")
        if revision is None:
            revision = request.get("revision")
        if create is None:
            create = request.get("create")
        if expected_root is None:
            expected_root = request.get("expected_root")
        if expected_root_dev is None:
            expected_root_dev = request.get("expected_root_dev")
        if expected_root_ino is None:
            expected_root_ino = request.get("expected_root_ino")
    binding: tuple[str | None, str | None, str | None] = (None, None, None)
    if (expected_root is not None or expected_root_dev is not None
            or expected_root_ino is not None):
        if not isinstance(request, dict):
            _error("expected_root must be a bounded path")
        binding = _parse_binding({
            "expected_root": expected_root,
            "expected_root_dev": expected_root_dev,
            "expected_root_ino": expected_root_ino,
        })
    root, project_id, note = resolve_root(request if isinstance(request, dict) else {})
    rel = _pf._validate_child(file)
    if _pf.is_excluded(root, rel):
        _error("file is sensitive or protected")
    if not isinstance(content, str):
        _error("content must be a string")
    if "\x00" in content:
        _error("content must not contain NUL")
    try:
        raw_new = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise GraphError("content is not valid UTF-8") from exc
    if len(raw_new) > WRITE_LIMIT:
        _error("content is larger than 128 KiB")
    want_create = create is True
    supplied_revision: str | None = None
    if revision is not None and revision != "":
        if not isinstance(revision, str):
            _error("revision must be a SHA-256 hex digest")
        supplied_revision = _validate_revision(revision)
    elif isinstance(revision, str) and revision == "":
        supplied_revision = None

    try:
        resolved = Path(root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GraphError("project folder is not accessible") from exc
    validated = _pf._validate_root(resolved)
    root_fd = _pf._open_root_fd(validated)
    try:
        _check_binding(validated, root_fd, binding)
        with _FolderLock(root_fd):
            return _write_locked(root_fd, validated, rel, raw_new,
                                 want_create, supplied_revision,
                                 project_id, note)
    finally:
        try:
            os.close(root_fd)
        except OSError:
            pass


def _write_locked(root_fd: int, validated: Path, rel: Path, raw_new: bytes,
                  want_create: bool, supplied_revision: str | None,
                  project_id: str, note: str) -> dict:
    import project_files as _pf

    parent_fd = -1
    held: list[int] = []
    temp_name: str | None = None
    try:
        parent_fd, held = _open_parent_fd(root_fd, rel)
        leaf = rel.parts[-1]
        # Existence probe without following links.
        try:
            leaf_stat = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            exists = True
        except FileNotFoundError:
            exists = False
            leaf_stat = None
        except OSError as exc:
            raise GraphError("file is not accessible") from exc
        if exists:
            assert leaf_stat is not None
            if stat.S_ISLNK(leaf_stat.st_mode):
                _error("file is not a regular file")
            if stat.S_ISDIR(leaf_stat.st_mode):
                _error("file is not a regular file")
            if not stat.S_ISREG(leaf_stat.st_mode):
                _error("file is not a regular file")
            if want_create:
                _error("file already exists; pass its revision to update")
            if supplied_revision is None:
                _error("revision is required to update an existing file")
            current_raw, mode = _read_current_bytes(root_fd, parent_fd, leaf)
            if hashlib.sha256(current_raw).hexdigest() != supplied_revision:
                _error("file revision is stale; read the file again")
            # Atomic overwrite: temp O_EXCL in the same directory, then a
            # final revision recheck before replace narrows the clobber
            # window (no pathname CAS exists; concurrent writers that ignore
            # revisions remain outside the guarantees, same as page writes).
            temp_fd_owned = -1
            for attempt in range(10):
                candidate = f".project-folder-{os.getpid()}-{attempt}.tmp"
                try:
                    temp_fd_owned = os.open(
                        candidate,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL
                        | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                        mode, dir_fd=parent_fd)
                    temp_name = candidate
                    break
                except FileExistsError:
                    continue
                except OSError as exc:
                    raise GraphError("cannot create temporary file") from exc
            if temp_fd_owned < 0 or temp_name is None:
                _error("cannot create temporary file")
            try:
                os.fchmod(temp_fd_owned, mode)
                view = memoryview(raw_new)
                while view:
                    view = view[os.write(temp_fd_owned, view):]
                os.fsync(temp_fd_owned)
            except OSError as exc:
                raise GraphError(f"cannot write file safely: {exc}") from exc
            finally:
                try:
                    os.close(temp_fd_owned)
                except OSError:
                    pass
            # Final race check before replacement.
            current_raw2, _mode2 = _read_current_bytes(root_fd, parent_fd, leaf)
            if hashlib.sha256(current_raw2).hexdigest() != supplied_revision:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except OSError:
                    pass
                temp_name = None
                _error("file revision is stale; read the file again")
            try:
                os.replace(temp_name, leaf, src_dir_fd=parent_fd,
                           dst_dir_fd=parent_fd)
            except OSError as exc:
                raise GraphError(f"cannot replace file safely: {exc}") from exc
            temp_name = None
            try:
                os.fsync(parent_fd)
            except OSError as exc:
                raise GraphError(f"cannot sync directory safely: {exc}") from exc
            new_revision = hashlib.sha256(raw_new).hexdigest()
            out = {"root": str(validated), "file": rel.as_posix(),
                   "size": len(raw_new), "revision": new_revision,
                   "created": False}
        else:
            if not want_create:
                _error("file does not exist; pass create:true to create it")
            if supplied_revision is not None:
                _error("revision must be empty when creating a file")
            # Atomic create-only: O_CREAT|O_EXCL|O_NOFOLLOW fails when the
            # name already exists (including a concurrently created file or
            # a symlink), never overwriting.
            create_fd = -1
            try:
                create_fd = os.open(leaf,
                                    os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                    | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                                    0o644, dir_fd=parent_fd)
            except FileExistsError:
                _error("file already exists; pass its revision to update")
            except OSError as exc:
                raise GraphError("file is not accessible") from exc
            try:
                view = memoryview(raw_new)
                while view:
                    view = view[os.write(create_fd, view):]
                os.fsync(create_fd)
            except OSError as exc:
                try:
                    os.close(create_fd)
                except OSError:
                    pass
                create_fd = -1
                try:
                    os.unlink(leaf, dir_fd=parent_fd)
                except OSError:
                    pass
                raise GraphError(f"cannot write file safely: {exc}") from exc
            try:
                os.close(create_fd)
            except OSError as exc:
                raise GraphError(f"cannot write file safely: {exc}") from exc
            create_fd = -1
            try:
                os.fsync(parent_fd)
            except OSError as exc:
                raise GraphError(f"cannot sync directory safely: {exc}") from exc
            new_revision = hashlib.sha256(raw_new).hexdigest()
            out = {"root": str(validated), "file": rel.as_posix(),
                   "size": len(raw_new), "revision": new_revision,
                   "created": True}
        if project_id:
            out["project_id"] = project_id
            out["path"] = note
        else:
            out["path"] = note
        return out
    finally:
        if temp_name is not None:
            try:
                if parent_fd >= 0:
                    os.unlink(temp_name, dir_fd=parent_fd)
            except OSError:
                pass
        _pf._close_all(held)
        if parent_fd >= 0 and parent_fd != root_fd:
            try:
                os.close(parent_fd)
            except OSError:
                pass
        # root_fd stays owned by write_folder (lock scope); never closed here.


# ---------------------------------------------------------------------------
# create-dir: the single explicit home-only directory-creation path
# ---------------------------------------------------------------------------

CREATE_DIR_MAX_MISSING = 8

# Defense in depth beyond the human preview: no created target may pass
# through (or below) these home-relative components, mirroring the
# protected/sensitive exclusions in project_files.py.
_CREATE_DIR_PROTECTED_COMPONENTS = frozenset(
    {".ssh", ".gnupg", ".aws", ".pi", ".git"})

_DIR_FLAGS = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
              | getattr(os, "O_CLOEXEC", 0))


def _home_dir() -> Path:
    """Return the resolved home directory (fail-closed when unusable)."""
    try:
        home = Path.home()
    except Exception as exc:
        raise GraphError("home directory is not accessible") from exc
    try:
        return home.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GraphError("home directory is not accessible") from exc


def _validate_home_path(value: object) -> Path:
    """Validate an absolute or ``~/``-relative creation target (lexical).

    Same input hygiene as the module's other path fields: bounded, no NUL,
    no backslashes, no ``.``/``..`` components. Returns the expanded
    absolute path; home containment and symlink freedom are established
    separately via fd walking over the live filesystem.
    """
    if not isinstance(value, str) or not value or len(value) > PATH_LIMIT:
        _error("path must be a bounded absolute or home-relative path")
    if "\\" in value or "\x00" in value:
        _error("path is unsafe")
    expanded = os.path.expanduser(value)
    if not expanded or len(expanded) > PATH_LIMIT:
        _error("path must be a bounded absolute or home-relative path")
    target = Path(expanded)
    if not target.is_absolute():
        _error("path must be absolute or start with ~/")
    if any(part in (".", "..") for part in target.parts[1:]):
        _error("path contains an unsafe directory component")
    return target


def _walk_home_target(target: Path) -> tuple[int, Path, list[str]]:
    """Walk *target* from ``/`` without following symlinks.

    Returns ``(ancestor_fd, ancestor_path, missing)`` where ``ancestor_fd``
    is an owned fd for the deepest existing ancestor directory,
    ``ancestor_path`` is its kernel-reported (fully resolved) path, and
    ``missing`` holds the remaining non-existent components. Any existing
    symlink component, or any existing non-directory component, fails
    closed. Ownership of ``ancestor_fd`` transfers to the caller.
    """
    parts = target.parts[1:]
    for part in parts:
        if not part or "/" in part or part in (".", ".."):
            _error("path is unsafe")
    try:
        current = os.open("/", _DIR_FLAGS)
    except OSError as exc:
        raise GraphError("path is not accessible") from exc
    index = len(parts)
    try:
        for pos, part in enumerate(parts):
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=current)
            except FileNotFoundError:
                index = pos
                break
            except OSError as exc:
                _classify_dir_open_error(current, part, exc)
            os.close(current)
            current = child
        else:
            index = len(parts)
        missing = list(parts[index:])
        try:
            actual = os.readlink(f"/proc/self/fd/{current}")
        except OSError as exc:
            raise GraphError("cannot verify path safely") from exc
        return current, Path(actual), missing
    except BaseException:
        try:
            os.close(current)
        except OSError:
            pass
        raise


def _canon_home_target(ancestor: Path, missing: list[str],
                       home: Path) -> Path:
    """Resolve the canonical target and require it strictly inside home."""
    try:
        ancestor.relative_to(home)
    except ValueError:
        raise GraphError("path must be inside the home directory") from None
    canonical = ancestor.joinpath(*missing) if missing else ancestor
    try:
        relative = canonical.relative_to(home)
    except ValueError:
        raise GraphError("path must be inside the home directory") from None
    if not relative.parts:
        _error("path must be strictly inside the home directory")
    for part in relative.parts:
        if (part in _CREATE_DIR_PROTECTED_COMPONENTS
                or part.startswith(".env")):
            _error("path is sensitive or protected")
    return canonical


def _classify_dir_open_error(current_fd: int, part: str,
                               exc: OSError) -> None:
    """Raise the fail-closed error for a failed ``O_NOFOLLOW`` dir open.

    A symlink to a directory surfaces as ``ENOTDIR`` (not ``ELOOP``) when
    ``O_DIRECTORY`` is set, so disambiguate with a non-following stat used
    only for the diagnostic: the open itself already failed, so the
    classification cannot redirect access.
    """
    import errno as _errno

    if exc.errno == _errno.ELOOP:
        raise GraphError("path traverses a symlink") from exc
    if exc.errno == _errno.ENOTDIR:
        try:
            info = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
        except OSError:
            raise GraphError("path is not accessible") from exc
        if stat.S_ISLNK(info.st_mode):
            raise GraphError("path traverses a symlink") from exc
        raise GraphError("path traverses a non-directory") from exc
    raise GraphError("path is not accessible") from exc


def _validate_parent_binding(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _error(f"{label} must be a non-negative integer")
    return value


def create_dir_preflight(request=None) -> dict:
    """Preview a home-only directory creation and bind its parent identity.

    Returns the canonical absolute ``path`` with ``exists``/``is_dir`` plus
    ``parent_path``/``parent_dev``/``parent_ino`` for the deepest existing
    ancestor directory (the target itself when it already exists as a
    directory). Every existing component is walked with ``O_DIRECTORY |
    O_NOFOLLOW`` so symlinks fail instead of redirecting; targets outside
    the home directory, the home directory itself, and existing files or
    symlinks are rejected.
    """
    if not isinstance(request, dict):
        _error("JSON input must be an object")
    target = _validate_home_path(request.get("path"))
    home = _home_dir()
    ancestor_fd = -1
    try:
        ancestor_fd, ancestor, missing = _walk_home_target(target)
        canonical = _canon_home_target(ancestor, missing, home)
        try:
            info = os.fstat(ancestor_fd)
        except OSError as exc:
            raise GraphError("path is not accessible") from exc
        if missing:
            exists, is_dir = False, False
        else:
            exists, is_dir = True, True
        return {"path": str(canonical), "exists": exists, "is_dir": is_dir,
                "parent_path": str(ancestor),
                "parent_dev": info.st_dev, "parent_ino": info.st_ino}
    finally:
        if ancestor_fd >= 0:
            try:
                os.close(ancestor_fd)
            except OSError:
                pass


def _verify_fd_path(fd: int, expected: Path) -> None:
    """Fail closed unless the pinned *fd* still resolves to *expected*.

    TOCTOU hardening: the dev/ino binding recheck alone cannot detect a
    same-identity path swap, so re-resolve the fd through
    ``/proc/self/fd`` and compare against the canonical predicted path.
    Any mismatch (or an unverifiable fd) errors out without touching
    foreign state: nothing is created, deleted, or cleaned up.
    """
    try:
        live = os.readlink(f"/proc/self/fd/{fd}")
    except OSError as exc:
        raise GraphError("cannot verify path safely") from exc
    if Path(live) != expected:
        _error("directory changed; request a new preflight")


def create_dir(request=None) -> dict:
    """Create a home-only directory and any missing ancestors (mkdir -p).

    The deepest existing ancestor's dev/ino is rechecked against
    ``expected_parent_dev``/``expected_parent_ino`` from
    :func:`create_dir_preflight` first (mismatch creates nothing). The
    pinned ancestor fd is additionally verified via ``/proc/self/fd`` to
    still resolve to the canonical predicted path before and after
    creation (mismatch reports fail-closed without cleanup). Missing
    components (at most 8) are then created one by one with mode ``0o755``,
    each reopened with ``O_DIRECTORY | O_NOFOLLOW`` and verified to be a
    real directory, so symlinks and file races fail instead of redirecting.
    An already-existing directory is an idempotent no-op
    (``created:false``). Nothing is ever deleted, renamed, or modified.
    """
    if not isinstance(request, dict):
        _error("JSON input must be an object")
    target = _validate_home_path(request.get("path"))
    expected_dev = _validate_parent_binding(
        request.get("expected_parent_dev"), "expected_parent_dev")
    expected_ino = _validate_parent_binding(
        request.get("expected_parent_ino"), "expected_parent_ino")
    home = _home_dir()
    ancestor_fd = -1
    owned: list[int] = []
    try:
        ancestor_fd, ancestor, missing = _walk_home_target(target)
        owned.append(ancestor_fd)
        canonical = _canon_home_target(ancestor, missing, home)
        try:
            info = os.fstat(ancestor_fd)
        except OSError as exc:
            raise GraphError("path is not accessible") from exc
        if (info.st_dev, info.st_ino) != (expected_dev, expected_ino):
            _error("directory changed; request a new preflight")
        # The pinned ancestor fd must still resolve to the walked ancestor
        # path before anything is created.
        _verify_fd_path(ancestor_fd, ancestor)
        if not missing:
            return {"path": str(canonical), "created": False, "exists": True}
        if len(missing) > CREATE_DIR_MAX_MISSING:
            _error("path needs more than 8 new directories")
        current = ancestor_fd
        for part in missing:
            created_now = False
            try:
                os.mkdir(part, 0o755, dir_fd=current)
                created_now = True
            except FileExistsError:
                pass
            except OSError as exc:
                raise GraphError("cannot create directory safely") from exc
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=current)
            except OSError as exc:
                try:
                    _classify_dir_open_error(current, part, exc)
                except GraphError as classified:
                    if "non-directory" in str(classified):
                        raise GraphError("path exists as a file") from exc
                    raise
                raise GraphError("cannot create directory safely") from exc
            try:
                child_info = os.fstat(child)
            except OSError as exc:
                try:
                    os.close(child)
                except OSError:
                    pass
                raise GraphError("cannot create directory safely") from exc
            if not stat.S_ISDIR(child_info.st_mode):
                try:
                    os.close(child)
                except OSError:
                    pass
                _error("path exists as a file")
            try:
                # Exact mode only for directories this call created; a
                # raced pre-existing directory is left untouched.
                if created_now:
                    os.fchmod(child, 0o755)
                os.fsync(child)
            except OSError as exc:
                try:
                    os.close(child)
                except OSError:
                    pass
                raise GraphError("cannot create directory safely") from exc
            owned.append(child)
            current = child
        # After creation the pinned ancestor must be unchanged and the
        # final fd must resolve to the canonical predicted path; on any
        # mismatch report fail-closed without cleaning up (the state may
        # be foreign, so never delete).
        _verify_fd_path(owned[0], ancestor)
        _verify_fd_path(current, canonical)
        try:
            os.fsync(owned[0])
        except OSError as exc:
            raise GraphError("cannot create directory safely") from exc
        return {"path": str(canonical), "created": True}
    finally:
        for fd in owned:
            try:
                os.close(fd)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# CLI (shared plumbing lives in qscli.py; argv stays byte-identical)
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = qscli.SafeParser(description=__doc__)
    qscli.add_global_flags(parser, projects_file=True)
    parser.add_argument("command", choices=("list", "read", "write",
                                            "preflight", "create-dir-preflight",
                                            "create-dir"))
    return parser.parse_args(argv if argv is not None else sys.argv[1:])


def _dispatch(args: argparse.Namespace) -> dict:
    # Honor an explicit --projects-file by exporting it for the
    # registry backend (which itself honors QUICKSHELL_PROJECTS_FILE).
    if args.projects_file:
        os.environ["QUICKSHELL_PROJECTS_FILE"] = str(args.projects_file)
    request = qscli.read_input()
    if args.command == "list":
        return list_folder(request)
    elif args.command == "read":
        return read_folder(request)
    elif args.command == "preflight":
        return preflight(request)
    elif args.command == "create-dir-preflight":
        return create_dir_preflight(request)
    elif args.command == "create-dir":
        return create_dir(request)
    else:
        return write_folder(request)


_BOUNDED_EXCEPTIONS = (GraphError, OSError, TypeError, ValueError,
                       UnicodeError, RecursionError, OverflowError)


def main(argv=None) -> int:
    return qscli.run_main(_parse_args, _dispatch, "project_folder",
                          _BOUNDED_EXCEPTIONS, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
