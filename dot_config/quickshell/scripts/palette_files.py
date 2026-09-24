#!/usr/bin/env python3
"""Find files for the command palette without inspecting their contents."""

import json
import os
from pathlib import Path
import sys
import time

import qscli


DEFAULT_LIMIT = 40
MAX_RESULTS = 100
ENTRY_BUDGET = 40_000
TIME_BUDGET = 1.5
GENERATED_DIRECTORIES = {"node_modules", "target", "build", "dist", "__pycache__"}


def _search(query, root=None, limit=DEFAULT_LIMIT):
    """Return matching files and whether the search was cut short.

    The walk is deliberately bounded: this helper is called from an interactive
    palette, so an unusually large home directory must not make it hang.
    """
    if limit < 0:
        raise ValueError("limit must not be negative")

    tokens = query.split()
    if not tokens:
        return {"results": [], "truncated": False}

    root = Path(root if root is not None else os.environ.get("PALETTE_FILE_ROOT", Path.home()))
    root = root.expanduser().resolve(strict=False)
    if not root.exists():
        raise OSError(f"root does not exist: {root}")
    if not root.is_dir():
        raise OSError(f"root is not a directory: {root}")

    limit = min(limit, MAX_RESULTS)
    needle = tuple(token.casefold() for token in tokens)
    found = []
    truncated = False
    entries = 0
    deadline = time.monotonic() + TIME_BUDGET
    walk_errors = []

    def onerror(error):
        walk_errors.append(error)

    # os.walk can itself block while enumerating a directory.  The budget is
    # therefore a soft bound on work after each walk result is yielded, not a
    # guarantee that the call cannot hang in the OS.
    for directory, directories, filenames in os.walk(
        str(root), topdown=True, followlinks=False, onerror=onerror
    ):
        if time.monotonic() >= deadline or entries >= ENTRY_BUDGET:
            truncated = True
            break

        kept_directories = []
        for dirname in sorted(directories, key=str.casefold):
            if time.monotonic() >= deadline or entries >= ENTRY_BUDGET:
                truncated = True
                break
            entries += 1
            child = Path(directory) / dirname
            if dirname.startswith(".") or dirname.casefold() in GENERATED_DIRECTORIES:
                continue
            try:
                if child.is_symlink():
                    continue
            except OSError:
                continue
            kept_directories.append(dirname)
        directories[:] = kept_directories
        if truncated:
            break

        for filename in sorted(filenames, key=str.casefold):
            if time.monotonic() >= deadline or entries >= ENTRY_BUDGET:
                truncated = True
                break
            entries += 1
            if filename.startswith("."):
                continue
            path = Path(directory) / filename
            try:
                if path.is_symlink():
                    continue
                if not path.is_file():
                    continue
            except OSError:
                continue
            relative = path.relative_to(root).as_posix()
            folded = relative.casefold()
            if not all(token in folded for token in needle):
                continue
            found.append({"name": filename, "path": str(path), "uri": path.as_uri()})
            if len(found) > limit:
                # One extra result lets us distinguish an exact limit from a
                # result set that was actually clipped.  Stop immediately so
                # collection remains bounded even when there are many hits.
                truncated = True
                break
        if truncated or time.monotonic() >= deadline:
            truncated = True
            break

    if walk_errors:
        # The caller still gets useful partial results, while the CLI reports
        # that the traversal was incomplete.
        truncated = True
    found.sort(key=lambda item: (item["name"].casefold(), item["path"].casefold()))
    result_truncated = truncated or len(found) > limit
    return {"results": found[:limit], "truncated": result_truncated}, walk_errors


def palette_files(query, root=None, limit=DEFAULT_LIMIT):
    """Return results for *query*; a zero limit intentionally returns []."""
    result = _search(query, root, limit)
    return result[0] if isinstance(result, tuple) else result


def _parse_args(argv):
    parser = qscli.SafeParser()
    parser.add_argument("--query", required=True)
    parser.add_argument("--root")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    return parser.parse_args(argv if argv is not None else sys.argv[1:])


def _dispatch(args):
    if args.limit < 0:
        raise ValueError("--limit must not be negative")
    result = _search(args.query, args.root, args.limit)
    # Keep the public function convenient while retaining traversal errors
    # so the command line can report a partial traversal.
    if isinstance(result, tuple):
        payload, errors = result
    else:
        payload, errors = result, []
    if errors:
        noun = "directory" if len(errors) == 1 else "directories"
        print(
            f"warning: could not read {len(errors)} {noun}; results are partial",
            file=sys.stderr,
        )
    print(json.dumps(payload, ensure_ascii=False))
    return 0


_BOUNDED_EXCEPTIONS = (OSError, ValueError)


def main(argv=None):
    return qscli.run_main(_parse_args, _dispatch, "palette files",
                          _BOUNDED_EXCEPTIONS, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
