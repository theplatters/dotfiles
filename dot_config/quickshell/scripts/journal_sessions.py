#!/usr/bin/env python3
"""Launch a Pi RPC worker in a private graph-level journal session pool."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import project_sessions as _project


def journal_scope(graph: Path, base: Path | None = None) -> Path:
    """Return a deterministic private scope distinct from palette/projects."""

    graph_key = str(graph.resolve(strict=True))
    digest = hashlib.sha256(graph_key.encode("utf-8")).hexdigest()
    # project_sessions.session_base() intentionally returns its ``projects``
    # child, so do not use it here. Journal and project pools are siblings
    # under the private palette base.
    if base is None:
        configured = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
        base = Path(configured) if configured else (
            _project.state_home() / _project.SESSION_SUBTREE[0] / _project.SESSION_SUBTREE[1])
    root = _project._private_dir(base)
    return _project._private_dir(root / "journals" / digest)


# Keep the deterministic-scope API parallel to project_sessions for callers
# that select the worker kind separately.
scope_for = journal_scope


# Public aliases make the wrapper's safety contract easy to exercise without
# launching pi, while keeping validation and command construction in one place.
resolved_graph = _project.resolved_graph
latest_session = _project.latest_session
command_for = _project.command_for


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", help="cached session path from the worker's last RPC state")
    parser.add_argument("--pending-name", help="name for a cached but not-yet-flushed empty session")
    parser.add_argument("--new-session", action="store_true", help="start a fresh session")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        graph = resolved_graph()
        scope = journal_scope(graph)
        command = command_for(scope, args.session, args.new_session, args.pending_name)
    except (OSError, _project.SessionPathError, ValueError) as error:
        print(f"journal session launch refused: {error}", file=sys.stderr)
        return 2

    environment = os.environ.copy()
    environment.pop("QS_PROJECT_PATH", None)
    environment.pop("QS_PROJECT_SESSION_SCOPE", None)
    environment["QS_JOURNAL_MODE"] = "1"
    environment["QS_JOURNAL_SESSION_SCOPE"] = str(scope)
    environment["PI_CODING_AGENT_SESSION_DIR"] = str(scope)
    # The wrapper resolved this exact graph for the scope hash. Never pass a
    # replaceable symlink spelling to the child after that decision.
    environment["LOGSEQ_GRAPH"] = str(graph)
    os.execvpe(command[0], command, environment)
    return 127  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
