#!/usr/bin/env python3
"""Shared CLI plumbing for the scripts/* JSON helpers.

Every helper speaks the same protocol: JSON on stdout (sorted keys,
compact separators), single-line ``error: ...`` on stderr with exit 1,
no tracebacks, no untrusted content in diagnostics, bounded IO. This
module holds that boilerplate once so the per-script CLIs stop
hand-rolling it:

- :data:`INPUT_LIMIT` — 1 MiB transport cap for stdin reads and stdout
  payloads (``projects.py`` intentionally keeps its own 256 KiB cap;
  see below).
- :class:`SafeParser` — ``argparse.ArgumentParser`` whose ``error()``
  raises ``ValueError("invalid arguments")`` instead of echoing the
  offending argv (the default prints usage plus untrusted input).
  ``--help`` still exits 0 via ``exit()``, untouched.
- :func:`emit` — sorted/compact JSON write with the output cap.
- :func:`read_input` — buffered stdin read with the input cap,
  JSON-object-only (non-dict payloads are rejected, never coerced).
- :func:`add_global_flags` — opt-in ``--db`` / ``--graph`` /
  ``--desktop-bin`` / ``--projects-file`` overrides (``default=None``;
  ``--graph`` keeps the historical help text naming ``settings.json``,
  so argv and ``--help`` stay identical to the hand-rolled versions).
- :func:`run_main` — ``SystemExit`` passthrough plus a bounded
  exception tuple plus a bare-``Exception`` fallback
  (``error: <tool> failed``), all single-line stderr, exit 1, no
  tracebacks.

Import-cycle rule: this module imports stdlib only (``argparse``,
``json``, ``sys``) — never ``annotations``, ``memory_tick``,
``project_*``, or any other helper. The per-script import DAG
(``daily_agenda`` -> ``project_planner`` -> ``project_files``,
``*/review/capture/organise`` -> ``memory_tick`` + ``annotations``)
already exists, so a shared leaf must not grow edges into it.

Grandfathered difference: ``projects.py`` keeps ``INPUT_LIMIT =
256 * 1024`` (registry writes are small and the narrower cap is load
bearing for its tests). It is not migrated to this module; if it ever
is, unify the cap deliberately, not by accident.

Conventions mirror the modern family (``annotations.py``,
``sessions.py``): ``emit``/``read_input`` failures raise
``ValueError`` with the same fixed tokens (``"JSON output is too
large"``, ``"cannot read JSON input"``, ``"JSON input is too large"``,
``"JSON input is invalid"``), so every caller's existing bounded tuple
(which always includes ``ValueError``) catches them unchanged.
"""

from __future__ import annotations

import argparse
import json
import sys

INPUT_LIMIT = 1024 * 1024


class SafeParser(argparse.ArgumentParser):
    """ArgumentParser that never echoes untrusted input.

    The default ``error()`` prints usage plus the offending argv to
    stderr, which can leak multi-line attacker data. Raise a fixed
    ``ValueError`` instead so callers report single-line
    ``error: invalid arguments`` with exit 1. ``--help`` still exits 0
    via ``exit()``, untouched.
    """

    def error(self, message):  # noqa: ARG002 - message intentionally dropped
        raise ValueError("invalid arguments")


def emit(value: dict) -> None:
    """Write one sorted/compact JSON line to stdout, capped at INPUT_LIMIT."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError, OverflowError):
        raise ValueError("JSON output is too large")
    if len(encoded) + 1 > INPUT_LIMIT:
        raise ValueError("JSON output is too large")
    sys.stdout.buffer.write(encoded + b"\n")


def read_input() -> dict:
    """Read one buffered stdin JSON object, capped at INPUT_LIMIT.

    Dict-only: arrays, scalars, and malformed input are rejected, never
    coerced. Fixed tokens only, so stdin content is never echoed.
    """
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    try:
        data = stream.read(INPUT_LIMIT + 1)
    except OSError:
        raise ValueError("cannot read JSON input")
    if isinstance(data, str):
        data = data.encode("utf-8")
    if len(data) > INPUT_LIMIT:
        raise ValueError("JSON input is too large")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError,
            RecursionError):
        raise ValueError("JSON input is invalid")
    if not isinstance(value, dict):
        raise ValueError("JSON input is invalid")
    return value


def add_global_flags(parser: argparse.ArgumentParser, *, db: bool = False,
                     graph: bool = False, desktop_bin: bool = False,
                     projects_file: bool = False) -> argparse.ArgumentParser:
    """Add opt-in global overrides; returns the parser for chaining."""
    if db:
        parser.add_argument("--db", default=None)
    if graph:
        parser.add_argument("--graph", default=None,
                            help="graph directory; defaults to LOGSEQ_GRAPH or logseqGraph in settings.json")
    if desktop_bin:
        parser.add_argument("--desktop-bin", default=None)
    if projects_file:
        parser.add_argument("--projects-file", default=None)
    return parser


def _one_line(text: str) -> str:
    """Fold CR/LF so diagnostics stay single-line (§8.2)."""
    return text.replace("\r", " ").replace("\n", " ")


def run_main(parse_fn, dispatch_fn, tool_name: str,
             bounded_exceptions=(), argv=None) -> int:
    """Run one CLI: parse, dispatch, emit.

    - ``parse_fn(argv)`` maps argv (or ``sys.argv[1:]`` when ``argv`` is
      ``None``) to a namespace. ``SystemExit`` (``--help`` → 0, usage
      errors → 2) passes through as ``0``/``1`` without a traceback.
    - ``dispatch_fn(args)`` returns the value dict to emit; it may also
      return a ``(code, payload)`` tuple (emitted, ``code`` returned) or
      a bare ``int`` (already emitted, returned as-is) for commands
      with their own exit codes.
    - ``bounded_exceptions`` is the caller's explicit tuple (always
      including ``ValueError`` so :class:`SafeParser`, :func:`emit`,
      and :func:`read_input` failures land here). Reported as
      single-line ``error: <detail>``, exit 1.
    - Anything else falls back to ``error: <tool> failed``, exit 1 —
      no tracebacks, no state content in diagnostics.
    """
    bounded = tuple(bounded_exceptions)
    try:
        args = parse_fn(argv)
        value = dispatch_fn(args)
        if isinstance(value, tuple):
            code, payload = value
            emit(payload)
            return code
        if isinstance(value, int):
            return value
        emit(value)
        return 0
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 1
    except bounded as exc:
        print(f"error: {_one_line(str(exc))}", file=sys.stderr)
        return 1
    except Exception:
        print(f"error: {tool_name} failed", file=sys.stderr)
        return 1
