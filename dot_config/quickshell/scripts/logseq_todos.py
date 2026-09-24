#!/usr/bin/env python3
"""Compatibility command for listing actionable Logseq tasks."""
import json
import sys

from logseq_common import GraphError, TASK, markdown_files, page_name, read_lines, MAX_RESULTS, resolve_graph

import qscli


def todos(graph, query=None):
    query = query.casefold() if query else None
    found = []
    for path in markdown_files(graph):
        lines = read_lines(path)
        if lines is None:
            continue
        for number, line in enumerate(lines, 1):
            match = TASK.match(line)
            if not match or (match.group(1) and "x" in match.group(1).lower()) or not match.group(3).strip():
                continue
            item = {"marker": match.group(2), "task": match.group(3).strip(),
                          "path": str(path), "line": number, "page": page_name(path, graph),
                          "graph": graph.name}
            if query and not any(query in str(item[field]).casefold()
                                 for field in ("task", "page", "marker")):
                continue
            found.append(item)
            if len(found) >= MAX_RESULTS:
                return found
    return sorted(found, key=lambda item: (item["path"].casefold(), item["line"], item["task"].casefold()))


def _parse_args(argv):
    parser = qscli.SafeParser()
    parser.add_argument("graph", nargs="?", default=None,
                        help="graph directory; defaults to LOGSEQ_GRAPH or logseqGraph in settings.json")
    parser.add_argument("--query")
    return parser.parse_args(argv if argv is not None else sys.argv[1:])


def _dispatch(args):
    value = todos(resolve_graph(args.graph), args.query)
    print(json.dumps(value, ensure_ascii=False))
    return 0


_BOUNDED_EXCEPTIONS = (GraphError, OSError, TypeError, ValueError,
                       UnicodeError)


def main(argv=None):
    return qscli.run_main(_parse_args, _dispatch, "logseq todos",
                          _BOUNDED_EXCEPTIONS, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
