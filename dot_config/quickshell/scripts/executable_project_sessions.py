#!/usr/bin/env python3
"""Launch a Pi RPC worker in the session directory for one project.

Project workers are pinned by stable registry UUID
(``--project-id`` → ``.../projects/by-id/<uuid>``) and work without a Logseq
note, including Zotero-only projects. The current optional note/folder/
collection is resolved per operation by the extension/backend from a fresh
registry read, never from a frozen path. Legacy page scopes
(``--project pages/X.md`` → ``.../projects/<sha256>``) remain for
compatibility; a legacy session is restored only when its page is explicitly
declared alongside ``--project-id``, never by scanning another project's
directory. No cross-project scope mixing occurs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import uuid as _uuid
from pathlib import Path


SESSION_SUBTREE = ("quickshell", "project-sessions")
PROJECT_SUBTREE = "projects"
BY_ID_SUBTREE = "by-id"
MAX_SESSION_SCAN = 2048
MAX_SESSION_HEADER_BYTES = 64 * 1024
MAX_SESSION_NAME = 120
# UUID-scoped sessions live under <base>/by-id/<uuid>; legacy page scopes are
# the direct <base>/<sha256(graph\0page)> children. The subtrees never mix:
# latest_session() only scans the active scope, and a legacy file is honored
# only when its page is explicitly declared alongside --project-id.
PROJECT_ID_ENV = "QS_PROJECT_ID"
LEGACY_PATH_ENV = "QS_PROJECT_PATH"


class SessionPathError(ValueError):
    """The graph, project, or session path is outside the worker scope."""


def _absolute(value: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _check_components(path: Path) -> None:
    """Reject symlinked path components, including a symlinked final dir."""

    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        info = _lstat(current)
        if info is not None and stat.S_ISLNK(info.st_mode):
            raise SessionPathError(f"session directory contains a symlink: {current}")


def _private_dir(path: Path) -> Path:
    """Create a private directory without following any existing symlink."""

    path = _absolute(path)
    _check_components(path)
    created: list[Path] = []
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        info = _lstat(current)
        if info is None:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                info = _lstat(current)
            else:
                created.append(current)
                info = _lstat(current)
        if info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SessionPathError(f"session path is not a directory: {current}")

    # Do not leave a directory made by a permissive umask (or an existing
    # configured pool) readable by other users.  Only the managed leaf and
    # directories we created are chmod'ed; home and its parents are untouched.
    for directory in (*created, path):
        try:
            directory.chmod(0o700)
        except OSError as error:
            raise SessionPathError(f"cannot make session directory private: {directory}") from error
    return path


def resolved_graph(value: str | None = None) -> Path:
    """Return the existing, canonical Logseq graph directory."""

    raw = (value or "").strip() if isinstance(value, str) else value
    if not raw:
        raw = (os.environ.get("LOGSEQ_GRAPH") or "").strip()
    if not raw:
        try:
            import quickshell_settings as _settings
            raw = _settings.resolve_graph_raw(None) or ""
        except ImportError:
            raw = ""
    if not raw:
        raise SessionPathError(
            "logseq graph is not configured; set LOGSEQ_GRAPH "
            "or logseqGraph in settings.json"
        )
    graph = _absolute(raw)
    try:
        graph = graph.resolve(strict=True)
    except OSError as error:
        raise SessionPathError(f"LOGSEQ_GRAPH is not an accessible directory: {raw}") from error
    if not graph.is_dir():
        raise SessionPathError(f"LOGSEQ_GRAPH is not a directory: {raw}")
    return graph


def project_relative_page(graph: Path, value: str) -> Path:
    """Validate and canonicalize a graph-relative project page path."""

    if not value or "\x00" in value:
        raise SessionPathError("project page must be a non-empty relative path")
    supplied = Path(value)
    if supplied.is_absolute() or any(part in ("", ".", "..") for part in supplied.parts):
        raise SessionPathError("project page must be a normalized graph-relative path")
    graph = graph.resolve(strict=True)
    candidate = graph.joinpath(*supplied.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise SessionPathError(f"project page does not exist: {value}") from error
    try:
        resolved.relative_to(graph)
    except ValueError as error:
        raise SessionPathError("project page resolves outside LOGSEQ_GRAPH") from error
    if not resolved.is_file():
        raise SessionPathError("project page is not a regular file")
    return Path(*supplied.parts)


def state_home() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    return _absolute(configured) if configured else _absolute(Path.home() / ".local" / "state")


def session_base() -> Path:
    """Return the configured base, never the palette pool's files directly."""

    configured = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    if configured:
        base = _private_dir(Path(configured))
    else:
        base = _private_dir(state_home() / SESSION_SUBTREE[0] / SESSION_SUBTREE[1])
    return _private_dir(base / PROJECT_SUBTREE)


def scope_for(graph: Path, project: Path, base: Path | None = None) -> Path:
    """Compute the deterministic private directory for ``graph/project``."""

    graph_key = str(graph.resolve(strict=True))
    project_key = project.as_posix()
    digest = hashlib.sha256(f"{graph_key}\0{project_key}".encode("utf-8")).hexdigest()
    root = _private_dir(base if base is not None else session_base())
    return _private_dir(root / digest)


def validate_project_id(value: object) -> str:
    """Validate a registry UUID, returning its canonical lowercase form."""

    if not isinstance(value, str) or not value.strip():
        raise SessionPathError("project id must be a UUID string")
    try:
        return str(_uuid.UUID(value.strip()))
    except ValueError as error:
        raise SessionPathError("project id must be a UUID string") from error


def is_uuid(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        _uuid.UUID(value.strip())
        return True
    except ValueError:
        return False


def scope_for_id(base: Path | None, project_id: str) -> Path:
    """Compute the deterministic private directory for a registry UUID."""

    pid = validate_project_id(project_id)
    root = _private_dir(base if base is not None else session_base())
    return _private_dir(root / BY_ID_SUBTREE / pid)


def _contained_session(scope: Path, value: str) -> Path | None:
    """Return a valid existing JSONL file, or None for a not-yet-flushed file."""

    candidate = _absolute(value)
    scope = scope.resolve(strict=True)
    try:
        candidate.relative_to(scope)
    except ValueError as error:
        raise SessionPathError("cached session is outside this project session directory") from error
    if candidate == scope or candidate.suffix != ".jsonl":
        raise SessionPathError("cached session must be a JSONL file in this project directory")
    _check_components(candidate)
    info = _lstat(candidate)
    if info is None:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SessionPathError("cached session must be a regular, non-symlink file")
    try:
        if candidate.resolve(strict=True) != candidate:
            raise SessionPathError("cached session must not resolve through a symlink")
    except OSError as error:
        raise SessionPathError("cached session cannot be resolved") from error
    if not _session_header(candidate):
        raise SessionPathError("cached session does not have a valid session header")
    return candidate


def _session_header(path: Path) -> dict[str, object] | None:
    """Read only a bounded first JSONL record from a safe regular file."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        data = os.read(fd, MAX_SESSION_HEADER_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    line = data.split(b"\n", 1)[0]
    if len(line) > MAX_SESSION_HEADER_BYTES:
        return None
    try:
        header = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(header, dict):
        return None
    if header.get("type") != "session":
        return None
    if not isinstance(header.get("id"), str) or not header["id"]:
        return None
    if not isinstance(header.get("version"), int) or header["version"] < 1:
        return None
    if not isinstance(header.get("timestamp"), str) or not isinstance(header.get("cwd"), str):
        return None
    return header


def latest_session(scope: Path) -> Path | None:
    """Find the latest eligible direct child without following unsafe files."""

    scope = scope.resolve(strict=True)
    _check_components(scope)
    candidates: list[tuple[int, str, Path]] = []
    try:
        with os.scandir(scope) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_SESSION_SCAN:
                    break
                if not entry.name.endswith(".jsonl"):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                        continue
                    candidate = scope / entry.name
                    if not _session_header(candidate):
                        continue
                    candidates.append((info.st_mtime_ns, entry.name, candidate))
                except OSError:
                    continue
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def bounded_name(value: str | None) -> str:
    if not value:
        return ""
    cleaned = " ".join(value.replace("\x00", " ").split())
    return cleaned[:MAX_SESSION_NAME]


def command_for(scope: Path, cached: str | None = None, new_session: bool = False,
                pending_name: str | None = None,
                legacy_scope: Path | None = None) -> list[str]:
    """Build the exact child command after scope and candidate validation.

    ``legacy_scope`` is only honored for an explicitly declared legacy page
    (UUID + ``--project`` migration). The automatic ``latest_session`` scan
    never leaves ``scope``; a legacy file is used only when ``cached``
    explicitly names a valid session inside ``legacy_scope``. This keeps
    migration explicit and prevents cross-project scope mixing.
    """

    command = ["pi", "--mode", "rpc", "--approve", "--session-dir", str(scope)]
    if cached:
        existing = None
        try:
            existing = _contained_session(scope, cached)
        except SessionPathError:
            existing = None
            if legacy_scope is None:
                raise
        if existing is None and legacy_scope is not None:
            # Explicit migration only: the cached file must be a valid
            # session inside the declared legacy page scope.
            existing = _contained_session(legacy_scope, cached)
        if existing is not None:
            command.extend(["--session", str(existing)])
        # A newly-created empty session has a path before Pi has flushed its
        # first message. Do not let --continue silently revive an older one;
        # starting without a resume selector creates the requested fresh
        # session. A missing path is the only non-error cache miss.
        else:
            name = bounded_name(pending_name)
            if name:
                command.extend(["--name", name])
    else:
        if not new_session:
            existing = latest_session(scope)
            if existing is not None:
                # Revalidate immediately before handing the path to Pi. The
                # scan never follows symlinks, and this second check closes
                # the small replace-after-scan window.
                safe = _contained_session(scope, str(existing))
                if safe is not None:
                    command.extend(["--session", str(safe)])
    return command


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", default=None,
                        help="stable registry UUID; when present the session scope is by-id/<uuid>")
    parser.add_argument("--project", default=None,
                        help="normalized page path relative to LOGSEQ_GRAPH (legacy scope, or explicit migration source when combined with --project-id)")
    parser.add_argument("--session", help="cached session path from the worker's last RPC state")
    parser.add_argument("--pending-name", help="name for a cached but not-yet-flushed empty session")
    parser.add_argument(
        "--new-session", action="store_true",
        help="start empty when there is no cached session; never fall back to --continue",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        project_id = (args.project_id or "").strip() if isinstance(args.project_id, str) else ""
        legacy_page_raw = (args.project or "").strip() if isinstance(args.project, str) else ""
        if project_id:
            pid = validate_project_id(project_id)
            base = session_base()
            scope = scope_for_id(base, pid)
            legacy_scope: Path | None = None
            legacy_page: Path | None = None
            if legacy_page_raw:
                # Explicit migration source only: requires a graph and a
                # valid existing page; never scanned automatically.
                graph = resolved_graph()
                legacy_page = project_relative_page(graph, legacy_page_raw)
                legacy_scope = scope_for(graph, legacy_page, base)
            command = command_for(scope, args.session, args.new_session,
                                  args.pending_name, legacy_scope)
        else:
            if not legacy_page_raw:
                raise SessionPathError("--project-id or --project is required")
            graph = resolved_graph()
            project = project_relative_page(graph, legacy_page_raw)
            scope = scope_for(graph, project)
            legacy_page = project
            pid = ""
            command = command_for(scope, args.session, args.new_session, args.pending_name)
    except (OSError, SessionPathError, ValueError) as error:
        print(f"project session launch refused: {error}", file=sys.stderr)
        return 2

    environment = os.environ.copy()
    environment["PI_CODING_AGENT_SESSION_DIR"] = str(scope)
    environment["QS_PROJECT_SESSION_SCOPE"] = str(scope)
    if pid:
        environment[PROJECT_ID_ENV] = pid
    # Preserve the legacy pinned path only when an explicit page was given.
    # Zotero-only workers run with QS_PROJECT_ID alone and an empty path.
    if legacy_page is not None:
        environment[LEGACY_PATH_ENV] = legacy_page.as_posix()
    elif not pid:
        environment[LEGACY_PATH_ENV] = legacy_page_raw
    else:
        environment[LEGACY_PATH_ENV] = ""
    # Journal vars must never leak into a project worker.
    environment.pop("QS_JOURNAL_MODE", None)
    environment.pop("QS_JOURNAL_SESSION_SCOPE", None)
    os.execvpe(command[0], command, environment)
    return 127  # pragma: no cover - execvpe either replaces or raises


if __name__ == "__main__":
    raise SystemExit(main())
