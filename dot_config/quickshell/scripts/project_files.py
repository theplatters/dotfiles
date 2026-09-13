#!/usr/bin/env python3
"""Bounded read-only folder inspection for a Logseq project page.

The selected page declares its folder with a single page-level property::

    file:: /path/to/folder

The root is resolved afresh from the selected page on every request.  Callers
must never let the model override the page, the property, or the root: only a
model-supplied path *relative to that root* is accepted for file reads.
There are no writes to the folder.
"""

from __future__ import annotations

import os
import errno
import re
import select
import signal
import stat
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote

from logseq_common import SENSITIVE_NAMES, GraphError

READ_LIMIT = 128 * 1024
LIST_LIMIT = 1000
# Cap on directories visited per listing: bounds getdents/stat work on
# file-heavy trees independently of the entry cap, with explicit truncation.
DIR_VISIT_LIMIT = 4096
STATUS_LIMIT = 500
DIFF_LIMIT = 256 * 1024
DIFF_NOTICE = "\n... [truncated at 256 KiB]\n"
GIT_OUTPUT_LIMIT = 512 * 1024
GIT_TIMEOUT = 10.0
# The TS extension allows ~10 s for a whole helper invocation, while one
# files-git request fans out to several git processes.  Cap the total git
# budget so the helper always answers (or fails) inside the TS timeout.
GIT_BUDGET = 8.0
PATH_LIMIT = 4096

_PROPERTY_RE = re.compile(
    r"^(?P<key>[A-Za-z][A-Za-z0-9_-]*)\s*::\s*(?P<value>.*)$")
_MARKDOWN_LINK_RE = re.compile(r"\[.*?\]\(\s*(?P<target>.+?)\s*\)\s*$")
_SENSITIVE_DIRS = {".ssh", ".gnupg", ".aws"}
_SYSTEM_ROOTS = (Path("/etc"), Path("/proc"), Path("/sys"),
                 Path("/dev"), Path("/boot"))
_PI_PROTECTED = {"auth", "credentials", "config", "agent", "extensions",
                 "skills", "SYSTEM.md", "settings.json", "trust.json",
                 "APPEND_SYSTEM.md", "prompts", "themes"}
_HELPER_NAMES = {"logseq_graph.py", "logseq_common.py", "logseq_todos.py",
                 "project_planner.py", "project_files.py",
                 "project_sessions.py", "journal_assistant.py",
                 "journal_sessions.py", "screen_capture.py"}


def _error(message: str):
    raise GraphError(message)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _sensitive_name(name: str) -> bool:
    folded = name.casefold()
    stem = Path(name).stem.casefold()
    return (folded in SENSITIVE_NAMES or stem in SENSITIVE_NAMES
            or folded == ".env" or folded.startswith((".env.", ".credentials.")))


def _protected_child(absolute: Path) -> bool:
    parts = [p for p in absolute.as_posix().split("/") if p]
    name = parts[-1] if parts else ""
    if any(part in _SENSITIVE_DIRS for part in parts):
        return True
    if _sensitive_name(name):
        return True
    # Every `.pi` occurrence counts: `.pi/extensions/pkg/.pi/public.txt` is
    # still under the protected `.pi/extensions` subtree even though its
    # last `.pi` is followed by an unprotected name.
    for pos, part in enumerate(parts):
        if part == ".pi" and pos + 1 < len(parts) \
                and parts[pos + 1] in _PI_PROTECTED:
            return True
    if "scripts" in parts and name in _HELPER_NAMES:
        return True
    if name == "ScopedAgent.qml":
        return True
    return False


def is_excluded(root: Path, rel: Path) -> bool:
    """True when a root-relative path must never be listed, read, or diffed."""
    for part in rel.parts:
        if part in (".git", *_SENSITIVE_DIRS):
            return True
        if _sensitive_name(part):
            return True
    absolute = root / rel if not rel.is_absolute() else rel
    try:
        if _protected_child(absolute.resolve(strict=False)):
            return True
    except OSError:
        return True
    # Lexical check as well so a replaced-by-symlink spelling stays excluded.
    if _protected_child(absolute):
        return True
    return False


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1].strip()
    return value


def _unwrap_value(raw: str) -> str:
    value = _strip_quotes(raw.strip())
    # <file:///path> autolink.
    if len(value) >= 2 and value.startswith("<") and value.endswith(">"):
        value = _strip_quotes(value[1:-1].strip())
    match = _MARKDOWN_LINK_RE.search(value)
    if match:
        value = _strip_quotes(match.group("target").strip())
    # [[target]] page-link spelling: treat as a plain relative path target.
    if value.startswith("[[") and value.endswith("]]"):
        value = _strip_quotes(value[2:-2].strip())
    return value


def _parse_file_target(raw: str) -> str:
    value = _unwrap_value(raw)
    if not value or "\x00" in value:
        _error("project page file property is empty")
    lowered = value.casefold()
    if lowered.startswith("file://"):
        rest = value[len("file://"):]
        if rest.casefold().startswith("localhost"):
            rest = rest[len("localhost"):]
        # file://host/path is not a local folder; only localhost/empty host.
        if rest.startswith("/") or rest == "":
            target = unquote(rest)
        elif "/" in rest and not rest.startswith("/"):
            # file://<host>/... with a non-localhost host: reject.
            _error("project page file property is not a local folder")
            target = ""  # unreachable
        else:
            _error("project page file property is not a local folder")
            target = ""
        value = _strip_quotes(target.strip())
        if not value or "\x00" in value:
            _error("project page file property is empty")
    if len(value) > PATH_LIMIT:
        _error("project page file property is too long")
    if "\\" in value or "\x00" in value:
        _error("project page file property is unsafe")
    return value


def extract_file_values(content: str) -> list[str]:
    """Return raw page-level ``file::`` values from the leading property block.

    Only the initial page-property section counts: leading blank lines and
    ``key:: value`` lines at column zero.  Scanning stops at the first line
    of any other content (blocks, headings, lists, fences, drawers), so a
    ``file::`` line inside fenced samples, later blocks, or quoted examples
    never grants a folder root.  Indented lines and list items such as
    ``- file:: ...`` are block-level properties and likewise never count.
    """
    content = content.lstrip("\ufeff")
    values: list[str] = []
    for line in re.split(r"\r\n|\r|\n", content):
        if line.strip() == "":
            continue
        if not line or line[0] in (" ", "\t", "-", "*", "#", ">", "|", ":",
                                   "`", "~", "{", "}"):
            break
        match = _PROPERTY_RE.match(line)
        if match is None:
            break
        if match.group("key").casefold() != "file":
            continue
        values.append(match.group("value").strip())
    return values


def resolve_root_from_content(graph: Path, content: str) -> Path:
    """Resolve the folder root declared by page *content*."""
    from logseq_common import graph_path as _graph_path
    graph = _graph_path(graph)
    values = extract_file_values(content)
    if not values:
        _error("project page does not define a file:: folder property")
    if len(values) > 1:
        _error("project page defines more than one file:: property")
    target = _parse_file_target(values[0])
    if not target:
        _error("project page file property is empty")
    expanded = os.path.expanduser(target)
    candidate = Path(expanded) if os.path.isabs(expanded) else (graph / expanded)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GraphError("project folder is not accessible") from exc
    return _validate_root(resolved)


def resolve_project_root(graph: Path, page_path: str) -> tuple[Path, str]:
    """Read the selected page fresh and resolve its folder root.

    Returns ``(root, page_content)``.  The page is always reread; there is no
    caller-supplied root override.
    """
    from project_planner import read_page as _read_page
    response = _read_page(graph, page_path)
    from logseq_common import graph_path as _graph_path
    root = resolve_root_from_content(_graph_path(graph), response["content"])
    return root, response["content"]


def _validate_root(resolved: Path) -> Path:
    if resolved == Path("/") or not resolved.is_dir():
        _error("project folder is not a directory")
    if any(_inside(resolved, root) for root in _SYSTEM_ROOTS):
        _error("project folder is sensitive")
    # Every canonical component is screened, not just the leaf: a root at or
    # below a secrets directory, a .git directory, or a protected policy path
    # is refused.  A root that merely *contains* excludable children is still
    # allowed; those children are excluded from listing, reads, and diffs.
    parts = [p for p in resolved.as_posix().split("/") if p]
    for part in parts:
        if part == ".git":
            _error("project folder is a git internals directory")
        if part in _SENSITIVE_DIRS:
            _error("project folder is sensitive")
        if _sensitive_name(part):
            _error("project folder is sensitive")
    if _protected_child(resolved):
        _error("project folder is protected")
    if resolved.name in _HELPER_NAMES or resolved.name == "ScopedAgent.qml":
        _error("project folder is protected")
    return resolved


def _validate_child(value: object) -> Path:
    if not isinstance(value, str) or not value or len(value) > PATH_LIMIT:
        _error("file must be a bounded path relative to the project folder")
    assert isinstance(value, str)
    if "\\" in value or "\x00" in value:
        _error("file path is unsafe")
    rel = Path(value)
    if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
        _error("file must be a normalized path relative to the project folder")
    return rel


_O_DIR = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
          | getattr(os, "O_CLOEXEC", 0))
_O_FILE = (os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
           | getattr(os, "O_CLOEXEC", 0))


def _open_root_fd(root: Path) -> int:
    """Open *root* as a directory fd without trusting ancestor pathnames.

    The canonical components are walked from `/` with ``O_DIRECTORY |
    O_NOFOLLOW`` at every step, so a concurrently swapped ancestor symlink
    fails the open instead of redirecting it.  Afterwards the kernel-reported
    path     of the fd is revalidated: it must equal the expected root (any live
    race surfaces as an error, never as silent access elsewhere).
    """
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GraphError("project folder is not accessible") from exc
    try:
        current = os.open("/", _O_DIR)
    except OSError as exc:
        raise GraphError("project folder is not accessible") from exc
    try:
        for part in resolved.parts[1:]:
            try:
                child = os.open(part, _O_DIR, dir_fd=current)
            except OSError as exc:
                raise GraphError("project folder is not accessible") from exc
            os.close(current)
            current = child
        info = os.fstat(current)
        if not stat.S_ISDIR(info.st_mode):
            raise GraphError("project folder is not a directory")
        actual = _fd_path(current)
        if actual is not None:
            try:
                if Path(actual).resolve(strict=False) != resolved:
                    raise GraphError("project folder changed before read")
            except (OSError, RuntimeError) as exc:
                raise GraphError("project folder changed before read") from exc
        else:
            # No /proc fallback: compare identity against a fresh stat as a
            # best-effort swap check.
            try:
                expected = resolved.stat()
            except OSError as exc:
                raise GraphError("project folder is not accessible") from exc
            if (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino):
                raise GraphError("project folder changed before read")
    except BaseException:
        try:
            os.close(current)
        except OSError:
            pass
        raise
    return current


def _fd_path(fd: int) -> str | None:
    """Return the kernel path of *fd*, or None when unavailable."""
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return None


def _open_child_fd(dir_fd: int, name: str, *, directory: bool) -> int:
    """Open one single-component child relative to *dir_fd*.

    ``O_NOFOLLOW`` rejects a symlinked final component, and because every
    ancestor is an already-open directory fd, a concurrent rename/replacement
    of an ancestor cannot redirect this open outside the pinned tree.
    """
    if (not name or "/" in name or "\\" in name or "\x00" in name
            or name in (".", "..")):
        _error("file path is unsafe")
    try:
        return os.open(name, _O_DIR if directory else _O_FILE, dir_fd=dir_fd)
    except OSError as exc:
        raise GraphError("file is not accessible") from exc


def _close_all(fds: list[int]) -> None:
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


def _open_file_at(root_fd: int, rel: Path) -> int:
    """Open the regular file *rel* below the open root fd; never follows links."""
    held: list[int] = []
    current = root_fd
    try:
        parts = rel.parts
        for part in parts[:-1]:
            child = _open_child_fd(current, part, directory=True)
            held.append(child)
            info = os.fstat(child)
            if not stat.S_ISDIR(info.st_mode):
                _error("file is not accessible")
            current = child
        leaf = _open_child_fd(current, parts[-1], directory=False)
        info = os.fstat(leaf)
        if not stat.S_ISREG(info.st_mode):
            try:
                os.close(leaf)
            except OSError:
                pass
            _error("file is not a regular file")
        held.append(leaf)
        # Transfer ownership of the leaf fd to the caller; close ancestors.
        held.remove(leaf)
        return leaf
    finally:
        _close_all(held)


def _is_fd_limit(exc: OSError) -> bool:
    return exc.errno in (errno.EMFILE, errno.ENFILE)


def list_files(root: Path) -> dict:
    """List regular files below *root*, bounded and symlink-safe.

    Traversal is fd-relative from a pinned root directory fd: symlinks are
    never followed and a swapped ancestor cannot redirect the walk.  Open
    descriptors stay bounded (one chain: a directory fd is closed once its
    subtree is done), directory visits are budgeted, and descriptor
    exhaustion marks the listing truncated explicitly instead of silently
    omitting entries.
    """
    try:
        resolved = Path(root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GraphError("project folder is not accessible") from exc
    _validate_root(resolved)
    root_fd = _open_root_fd(resolved)
    entries: list[dict] = []
    truncated = False
    visits = 0
    # Lazy items: ("open", parent_fd, name, rel) opens one child on pop,
    # ("close", fd) closes a visited directory after its subtree is done.
    # Pending items hold no fds, so open descriptors never exceed the depth
    # of the current chain no matter how broad a directory is.
    stack: list[tuple] = [("open", None, None, Path())]
    try:
        while stack and len(entries) < LIST_LIMIT:
            item = stack.pop()
            if item[0] == "close":
                try:
                    os.close(item[1])
                except OSError:
                    pass
                continue
            _, parent_fd, name, rel_base = item
            if parent_fd is None:
                dir_fd, opened = root_fd, False
            else:
                try:
                    dir_fd = os.open(name, _O_DIR, dir_fd=parent_fd)
                except OSError as exc:
                    if _is_fd_limit(exc):
                        truncated = True
                    continue
                try:
                    if not stat.S_ISDIR(os.fstat(dir_fd).st_mode):
                        os.close(dir_fd)
                        continue
                except OSError as exc:
                    try:
                        os.close(dir_fd)
                    except OSError:
                        pass
                    if _is_fd_limit(exc):
                        truncated = True
                    continue
                opened = True
            try:
                visits += 1
                if visits > DIR_VISIT_LIMIT:
                    truncated = True
                    if opened:
                        try:
                            os.close(dir_fd)
                        except OSError:
                            pass
                    break
                names = sorted(os.listdir(dir_fd))
            except OSError as exc:
                if opened:
                    try:
                        os.close(dir_fd)
                    except OSError:
                        pass
                if _is_fd_limit(exc):
                    truncated = True
                continue
            subdirs: list[str] = []
            for child in names:
                if len(entries) >= LIST_LIMIT:
                    truncated = True
                    break
                if child in (".", "..") or "/" in child:
                    continue
                rel = (rel_base / child) if rel_base.parts else Path(child)
                if is_excluded(resolved, rel):
                    continue
                try:
                    info = os.stat(child, dir_fd=dir_fd, follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    continue
                if stat.S_ISDIR(info.st_mode):
                    subdirs.append(child)
                elif stat.S_ISREG(info.st_mode):
                    entries.append({"path": rel.as_posix(), "size": info.st_size})
                # Sockets, fifos, devices, and anything else are skipped.
            if opened:
                # Runs after the pushed children (LIFO): closes this level.
                stack.append(("close", dir_fd))
            for child in reversed(subdirs):
                if len(entries) >= LIST_LIMIT:
                    truncated = True
                    break
                rel = (rel_base / child) if rel_base.parts else Path(child)
                stack.append(("open", dir_fd, child, rel))
        if any(item[0] == "open" for item in stack):
            # The entry budget cut the walk short: the listing is partial.
            truncated = True
        for item in stack:
            if item[0] == "close":
                try:
                    os.close(item[1])
                except OSError:
                    pass
    finally:
        try:
            os.close(root_fd)
        except OSError:
            pass
    entries.sort(key=lambda item: item["path"].casefold())
    return {"root": str(resolved), "entries": entries, "truncated": truncated}


def read_file(root: Path, rel_value: object) -> dict:
    """Read one UTF-8 text file relative to *root* via a pinned directory fd."""
    try:
        resolved = Path(root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GraphError("project folder is not accessible") from exc
    _validate_root(resolved)
    rel = _validate_child(rel_value)
    if is_excluded(resolved, rel):
        _error("file is sensitive or protected")
    root_fd = _open_root_fd(resolved)
    fd = -1
    try:
        fd = _open_file_at(root_fd, rel)
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
        try:
            os.close(root_fd)
        except OSError:
            pass
    if len(raw) > READ_LIMIT:
        _error("file is larger than 128 KiB")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GraphError("file is not valid UTF-8") from exc
    return {"root": str(resolved), "path": rel.as_posix(), "size": len(raw),
            "content": content}


def _sanitized_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_PAGER": "cat",
        "GIT_EDITOR": "true",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        # Never fetch missing objects from a promisor remote while diffing.
        "GIT_NO_LAZY_FETCH": "1",
    }


# Repository-local config keys that can execute arbitrary commands.  The git
# runner is fail-closed: any of these present locally refuses the whole
# files-git request instead of risking execution.  (Per-path drivers named in
# .gitattributes are inert unless their command is defined here, and the diff
# itself runs with --no-ext-diff/--no-textconv plus submodule ignoring.)
#
# Subsection patterns deliberately use `.+`: git config subsections may
# themselves contain dots (e.g. `filter "safe.driver" clean` normalizes to
# `filter.safe.driver.clean`), so `[^.]+` would let dotted drivers bypass.
#
# Concurrency note: a hostile writer that modifies .git/config between this
# check and the content commands is outside the guarantees (same as the
# existing project helper's stance on uncooperating writers).  The residual
# window cannot enable execution: every exec vector below is *also*
# neutralized at use time by command-line flags and environment that outrank
# repo config (--no-ext-diff/--no-textconv, -c core.fsmonitor=false,
# -c core.pager=cat, GIT_NO_LAZY_FETCH=1, sanitized env without GIT_*).
# At worst a concurrent modification causes refusal or stale data, never
# helper execution.
_EXEC_CONFIG_RES = (
    re.compile(r"^core\.(fsmonitor|fsmonitorhook|pager|editor|askpass|sshcommand|hookspath)$"),
    re.compile(r"^sequence\.editor$"),
    re.compile(r"^diff\.external$"),
    re.compile(r"^diff\..+\.(command|textconv|cachetextconv)$"),
    re.compile(r"^filter\..+\.(clean|smudge|process)$"),
    re.compile(r"^merge\..+\.driver$"),
    re.compile(r"^credential\.helper$"),
    re.compile(r"^credential\..+\.helper$"),
    re.compile(r"^include\.path$"),
    re.compile(r"^includeif\..*\.path$"),
    re.compile(r"^extensions\.worktreeconfig$"),
)
_SUBMODULE_UPDATE_SAFE = {"checkout", "rebase", "merge", "none", ""}


def _check_repo_config(root: Path, timeout: float) -> None:
    """Refuse repositories whose local config can execute commands."""
    code, out, _err, _ = _run_git(root, ["config", "--local", "--list", "-z"],
                                  timeout=timeout)
    if code != 0:
        if not out:
            return  # No local config (or unreadable): nothing to screen.
        raise GraphError("cannot inspect project git config")
    for record in out.split(b"\x00"):
        if not record:
            continue
        key, _, _value = record.decode("utf-8", errors="replace").partition("\n")
        folded = key.strip().casefold()
        for pattern in _EXEC_CONFIG_RES:
            if pattern.match(folded):
                raise GraphError(
                    "project folder git config enables helper execution "
                    f"({key}); refusing")
        if folded.startswith("submodule.") and folded.endswith(".update"):
            if _value.strip().lower() not in _SUBMODULE_UPDATE_SAFE \
                    or _value.strip().startswith("!"):
                raise GraphError(
                    "project folder git config enables submodule command "
                    f"({key}); refusing")
        if folded == "protocol.allow" and _value.strip().lower() != "never":
            raise GraphError(
                "project folder git config widens fetch protocols; refusing")


def _run_git(root: Path, args: list[str],
             timeout: float = GIT_TIMEOUT,
             limit: int = GIT_OUTPUT_LIMIT) -> tuple[int, bytes, bytes, bool]:
    """Run git with fixed argv, bounding output incrementally and time.

    The child runs in its own process group so a timeout/overflow kills the
    whole group (git never spawns daemons for these read-only commands, but
    defensive group cleanup costs nothing).  Both pipes are non-blocking and
    the single overall deadline governs reads through EOF: there is no
    unbounded blocking drain after the child exits.
    """
    cmd = ["git", "--no-optional-locks",
           "-c", "core.fsmonitor=false",
           "-c", "core.untrackedCache=false",
           "-c", "core.pager=cat",
           "-c", "submodule.recurse=false", *args]
    _ensure_sigterm_handler()
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(root), env=_sanitized_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, shell=False, start_new_session=True)
    except FileNotFoundError as exc:
        raise GraphError("git is not available") from exc
    except OSError as exc:
        raise GraphError("cannot run git") from exc
    assert proc.stdout is not None and proc.stderr is not None
    _ACTIVE_GIT.append(proc)
    for stream in (proc.stdout, proc.stderr):
        try:
            os.set_blocking(stream.fileno(), False)
        except OSError as exc:
            _kill_git(proc)
            _reap_git(proc)
            raise GraphError("cannot run git") from exc
    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []
    out_total = 0
    err_total = 0
    stdout_eof = stderr_eof = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            if stdout_eof and stderr_eof:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_git(proc)
                raise GraphError("git timed out")
            watch = []
            if not stdout_eof:
                watch.append(proc.stdout)
            if not stderr_eof:
                watch.append(proc.stderr)
            try:
                ready, _, _ = select.select(watch, [], [], max(0.0, remaining))
            except (OSError, ValueError) as exc:
                # A GraphError raised by the SIGTERM handler must propagate
                # (controlled abort), never be rewritten as a git failure.
                if isinstance(exc, GraphError):
                    _kill_git(proc)
                    raise
                _kill_git(proc)
                raise GraphError("cannot run git") from exc
            if not ready:
                if proc.poll() is not None:
                    # Exited but pipes not yet at EOF: loop; the deadline
                    # still bounds this drain.
                    continue
                continue
            for stream in ready:
                try:
                    chunk = os.read(stream.fileno(), 32768)
                except BlockingIOError:
                    continue
                except OSError:
                    if stream is proc.stdout:
                        stdout_eof = True
                    else:
                        stderr_eof = True
                    continue
                if chunk == b"":
                    if stream is proc.stdout:
                        stdout_eof = True
                    else:
                        stderr_eof = True
                    continue
                if out_total + err_total + len(chunk) > limit:
                    _kill_git(proc)
                    raise GraphError(f"git output exceeded {limit} bytes")
                if stream is proc.stdout:
                    out_chunks.append(chunk)
                    out_total += len(chunk)
                else:
                    # Diagnostics only; keep stderr small.
                    if err_total < 64 * 1024:
                        take = chunk[:64 * 1024 - err_total]
                        err_chunks.append(take)
                        err_total += len(take)
        # Both pipes at EOF; the child has exited (or is reaped below).
        remaining = deadline - time.monotonic()
        try:
            proc.wait(timeout=max(0.0, remaining))
        except subprocess.TimeoutExpired as exc:
            _kill_git(proc)
            raise GraphError("git timed out") from exc
    finally:
        if proc.poll() is None:
            _kill_git(proc)
        _reap_git(proc)
        try:
            _ACTIVE_GIT.remove(proc)
        except ValueError:
            pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
    return proc.returncode, b"".join(out_chunks), b"".join(err_chunks), False


def _kill_git(proc: "subprocess.Popen[bytes]") -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def _reap_git(proc: "subprocess.Popen[bytes]") -> None:
    try:
        proc.wait(timeout=2)
    except Exception:
        pass


# Active git children, so a SIGTERM to this helper (e.g. the TS extension
# aborting/timing out the request) kills the whole git process group instead
# of orphaning it: git runs in a new session and would otherwise survive the
# death of this Python process.
_ACTIVE_GIT: list["subprocess.Popen[bytes]"] = []
_SIGTERM_INSTALLED = False


def _on_sigterm(_signum, _frame) -> None:
    for proc in list(_ACTIVE_GIT):
        _kill_git(proc)
    # Raise a controlled error (not SystemExit, not silence): project_planner
    # reports it as `error: operation aborted` with a nonzero exit.
    raise GraphError("operation aborted")


def _ensure_sigterm_handler() -> None:
    """Install the SIGTERM cleanup handler exactly once, before any spawn."""
    global _SIGTERM_INSTALLED
    if _SIGTERM_INSTALLED:
        return
    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (OSError, RuntimeError, ValueError):
        return
    _SIGTERM_INSTALLED = True


def _parse_status_z(raw: bytes) -> list[tuple[str, str, str, str | None]]:
    """Parse ``git status --porcelain=v1 -z`` into repo-relative records.

    Returns ``(index, worktree, path, orig)`` tuples where paths are exactly
    as git reported them (no stripping: names may legitimately start or end
    with whitespace).  Renames/copies (`R`/`C`) consume the following bare
    NUL record as the source path.  Callers convert to root-relative display
    paths; nothing here is trusted for shell or header use.
    """
    records = raw.split(b"\x00")
    entries: list[tuple[str, str, str, str | None]] = []
    index = 0
    while index < len(records):
        token = records[index]
        index += 1
        if not token:
            continue
        try:
            text = token.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            continue
        if len(text) < 4 or text[2] != " ":
            # A bare rename-source record (or anything else without an XY
            # prefix) is never a standalone entry.
            continue
        status_index, worktree, path = text[0], text[1], text[3:]
        orig: str | None = None
        if status_index in ("R", "C"):
            if index < len(records) and records[index]:
                try:
                    candidate = records[index].decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    candidate = ""
                if (candidate and candidate != "."
                        and not Path(candidate).is_absolute()
                        and not any(part in ("", ".", "..")
                                    for part in Path(candidate).parts)):
                    orig = candidate
            index += 1
        if (not path or path == "." or Path(path).is_absolute()
                or any(part in ("", ".", "..") for part in Path(path).parts)):
            continue
        entries.append((status_index, worktree, path, orig))
    return entries


def _to_root_relative(repo_path: str, prefix: str) -> str | None:
    """Convert a repo-relative status path to a root-relative display path."""
    rel = repo_path
    if prefix:
        if not repo_path.startswith(prefix):
            return None
        rel = repo_path[len(prefix):]
    if (not rel or rel == "." or Path(rel).is_absolute()
            or any(part in ("", ".", "..") for part in Path(rel).parts)):
        return None
    return rel


def _git_empty(root: Path, *, is_repo: bool, unborn: bool,
               toplevel: Path | None, prefix: str,
               message: str | None = None) -> dict:
    """Consistent files-git schema for the non-repo and unborn cases.

    Every response carries the same keys so extension validation and model
    code never branch on a missing ``diff`` or ``changes`` field.
    """
    value = {"root": str(root), "isRepo": is_repo, "unborn": unborn,
             "head": None, "lastTouching": None,
             "toplevel": str(toplevel) if toplevel is not None else None,
             "prefix": prefix, "changes": [], "untracked": [],
             "truncated": False, "diff": "", "diffTruncated": False}
    if message is not None:
        value["message"] = message
    return value


def git_info(root: Path) -> dict:
    """Return scoped status, HEAD diff, and last-commit metadata for *root*.

    Status paths are converted from repo-relative to root-relative, so every
    reported ``path`` is directly usable with the folder read tool.  The diff
    is built from an allowlist of those same non-excluded paths (literal
    pathspecs, renames disabled, submodules ignored) instead of filtering
    human-readable headers after the fact.
    """
    try:
        root = Path(root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GraphError("project folder is not accessible") from exc
    _validate_root(root)
    start = time.monotonic()

    def remaining() -> float:
        left = GIT_BUDGET - (time.monotonic() - start)
        if left <= 0:
            raise GraphError("git timed out")
        return min(left, GIT_TIMEOUT)

    code, out, _err, _ = _run_git(root, ["rev-parse", "--show-toplevel"],
                                  timeout=remaining())
    if code != 0:
        return _git_empty(root, is_repo=False, unborn=False, toplevel=None,
                          prefix="", message="not a git repository")
    toplevel = Path(out.decode("utf-8", errors="replace").strip())
    code, out, _err, _ = _run_git(root, ["rev-parse", "--show-prefix"],
                                  timeout=remaining())
    prefix = (out.decode("utf-8", errors="replace").strip()
              if code == 0 else "")
    # Fail closed on execution-capable local config before running any
    # content-producing command.
    _check_repo_config(root, remaining())
    code, _out, _err, _ = _run_git(root, ["rev-parse", "--verify", "HEAD"],
                                   timeout=remaining())
    if code != 0:
        # Unborn repository: no commits yet.  Status still works.
        status = _scoped_status(root, prefix, remaining())
        value = _git_empty(root, is_repo=True, unborn=True, toplevel=toplevel,
                           prefix=prefix.rstrip("/"))
        value.update(status)
        return value
    head = _commit_metadata(root, "HEAD", None, remaining())
    touched = head
    if prefix:
        scoped = _commit_metadata(root, "HEAD", ".", remaining())
        if scoped is not None:
            touched = scoped
    status = _scoped_status(root, prefix, remaining())
    tracked = status.pop("_repo_tracked")
    tracked_set = set(tracked)
    changed = _git_changed_names(root, prefix, remaining())
    forbidden = [p for p in changed
                 if p not in tracked_set and is_excluded(root, Path(p))]
    diff_text, diff_truncated = _scoped_diff(root, tracked, forbidden,
                                            remaining())
    return {"root": str(root), "isRepo": True, "unborn": False,
            "toplevel": str(toplevel), "prefix": prefix.rstrip("/"),
            "head": head, "lastTouching": touched,
            **status, "diff": diff_text, "diffTruncated": diff_truncated}


def _scoped_status(root: Path, prefix: str, timeout: float) -> dict:
    """Scoped status with root-relative display paths.

    Returns ``changes``/``untracked``/``truncated`` for display plus an
    internal ``_repo_tracked`` list of repo-relative paths backing the
    allowlist diff.  Every display path is usable verbatim with the folder
    read tool.
    """
    code, out, err, _ = _run_git(
        root, ["status", "--porcelain=v1", "--untracked-files=all",
               "--ignore-submodules=all", "-z", "--", "."],
        timeout=timeout)
    if code != 0:
        detail = err.decode("utf-8", errors="replace").strip()[:500]
        raise GraphError(f"git status failed: {detail}" if detail else "git status failed")
    changes: list[dict] = []
    untracked: list[str] = []
    # Root-relative (cwd-relative) allowlist backing the diff.  Only
    # in-scope, non-excluded display paths are ever added; a rename source
    # outside the selected root is reported as info but never diffed.
    repo_tracked: list[str] = []
    for status_index, worktree, repo_path, orig in _parse_status_z(out):
        display = _to_root_relative(repo_path, prefix)
        if display is None:
            continue
        rel = Path(display)
        if is_excluded(root, rel):
            continue
        if status_index == "?" and worktree == "?":
            untracked.append(display)
        else:
            item: dict = {"path": display, "index": status_index,
                          "worktree": worktree}
            if orig is not None:
                orig_display = _to_root_relative(orig, prefix)
                if orig_display is not None and not is_excluded(
                        root, Path(orig_display)):
                    item["orig"] = orig_display
                    repo_tracked.append(orig_display)
            changes.append(item)
            repo_tracked.append(display)
        if len(changes) + len(untracked) >= STATUS_LIMIT:
            break
    truncated = len(changes) + len(untracked) >= STATUS_LIMIT
    return {"changes": changes, "untracked": untracked,
            "truncated": truncated, "_repo_tracked": repo_tracked}


def _git_changed_names(root: Path, prefix: str, timeout: float) -> list[str]:
    """Enumerate changed tracked paths (repo-relative) via names only.

    Output is metadata (no file content), so it is safe to enumerate before
    filtering.  Used to find forbidden counterparts of allowlisted paths
    (e.g. a secret deleted under a replaced directory).
    """
    code, out, err, _ = _run_git(
        root, ["diff", "--name-only", "-z", "--no-renames",
               "--ignore-submodules=all", "HEAD", "--", "."],
        timeout=timeout)
    if code != 0:
        detail = err.decode("utf-8", errors="replace").strip()[:500]
        raise GraphError(f"git diff failed: {detail}" if detail else "git diff failed")
    names: list[str] = []
    for token in out.split(b"\x00"):
        if not token:
            continue
        try:
            text = token.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            continue
        if (not text or text == "." or Path(text).is_absolute()
                or any(part in ("", ".", "..") for part in Path(text).parts)):
            continue
        display = _to_root_relative(text, prefix)
        if display is None:
            continue
        names.append(display)
        if len(names) >= STATUS_LIMIT:
            break
    return names


def _truncate_diff(out: bytes) -> tuple[str, bool]:
    """Decode bounded diff output, budgeting the truncation notice in bytes.

    The notice size is reserved from DIFF_LIMIT first, and the cut lands on a
    UTF-8 boundary, so the returned text is always valid UTF-8 within budget.
    """
    notice = DIFF_NOTICE.encode("utf-8")
    budget = DIFF_LIMIT - len(notice)
    if len(out) <= DIFF_LIMIT:
        try:
            return out.decode("utf-8", errors="strict"), False
        except UnicodeDecodeError as exc:
            raise GraphError("git diff is not valid UTF-8") from exc
    try:
        out.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise GraphError("git diff is not valid UTF-8") from exc
    return out[:budget].decode("utf-8", errors="ignore") + DIFF_NOTICE, True


def _scoped_diff(root: Path, inclusions: list[str], exclusions: list[str],
                 timeout: float) -> tuple[str, bool]:
    """Diff only the allowlisted root-relative paths, literally.

    ``--no-renames`` keeps renames as delete+add pairs (both endpoints are in
    the allowlist), ``:(literal)`` disables glob magic in file names, and
    ``--ignore-submodules=all`` suppresses submodule content (notably
    ``diff.submodule=diff`` leaks).  Sensitive/protected paths never reach
    argv as inclusions; every changed-but-forbidden path travels as a
    ``:(exclude,literal)`` pathspec alongside them.  This matters because a
    literal pathspec also matches descendants: without the exclusion, an
    allowed file that replaced a directory (e.g. `bundle` over
    `bundle/.env`) would leak the deleted secret.
    """
    if not inclusions:
        return "", False
    seen: set[str] = set()
    include = [p for p in inclusions if not (p in seen or seen.add(p))]
    seen.clear()
    exclude = [p for p in exclusions
               if p not in include and not (p in seen or seen.add(p))]
    spec = [f":(literal){p}" for p in include]
    spec += [f":(exclude,literal){p}" for p in exclude]
    code, out, err, _ = _run_git(
        root, ["diff", "--no-renames", "--no-ext-diff", "--no-textconv",
               "--no-color", "--unified=3", "--ignore-submodules=all",
               "HEAD", "--", *spec],
        timeout=timeout)
    if code != 0:
        detail = err.decode("utf-8", errors="replace").strip()[:500]
        raise GraphError(f"git diff failed: {detail}" if detail else "git diff failed")
    return _truncate_diff(out)


def _commit_metadata(root: Path, rev: str, pathspec: str | None,
                   timeout: float) -> dict | None:
    args = ["log", "-1", "--no-show-signature",
            "--format=%H%x00%an%x00%ad%x00%s", "--date=iso-strict", rev]
    if pathspec is not None:
        args += ["--", pathspec]
    code, out, _err, _ = _run_git(root, args, timeout=timeout)
    if code != 0:
        return None
    try:
        text = out.decode("utf-8", errors="strict").rstrip("\n")
    except UnicodeDecodeError:
        return None
    fields = text.split("\x00")
    if len(fields) != 4 or not fields[0]:
        return None
    return {"hash": fields[0], "author": fields[1],
            "date": fields[2], "subject": fields[3]}
