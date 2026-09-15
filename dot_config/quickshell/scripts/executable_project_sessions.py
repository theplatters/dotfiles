#!/usr/bin/env python3
"""Launch a Pi RPC worker in the session directory for one project page.

Project workers must not share the palette's session pool.  This wrapper is
intentionally small: it owns the directory policy and then replaces itself
with ``pi`` so signals and exit status retain their normal meaning.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path


SESSION_SUBTREE = ("quickshell", "project-sessions")
PROJECT_SUBTREE = "projects"
MAX_SESSION_SCAN = 2048
MAX_SESSION_HEADER_BYTES = 64 * 1024
MAX_SESSION_NAME = 120


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
                pending_name: str | None = None) -> list[str]:
    """Build the exact child command after scope and candidate validation."""

    command = ["pi", "--mode", "rpc", "--approve", "--session-dir", str(scope)]
    if cached:
        existing = _contained_session(scope, cached)
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
    parser.add_argument("--project", required=True, help="normalized page path relative to LOGSEQ_GRAPH")
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
        graph = resolved_graph()
        project = project_relative_page(graph, args.project)
        scope = scope_for(graph, project)
        command = command_for(scope, args.session, args.new_session, args.pending_name)
    except (OSError, SessionPathError, ValueError) as error:
        print(f"project session launch refused: {error}", file=sys.stderr)
        return 2

    environment = os.environ.copy()
    environment["PI_CODING_AGENT_SESSION_DIR"] = str(scope)
    environment["QS_PROJECT_SESSION_SCOPE"] = str(scope)
    os.execvpe(command[0], command, environment)
    return 127  # pragma: no cover - execvpe either replaces or raises


if __name__ == "__main__":
    raise SystemExit(main())
