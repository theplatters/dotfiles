#!/usr/bin/env python3
"""Compatibility command for listing actionable Logseq tasks."""
import json
import sys

from logseq_common import GraphError, TASK, graph_path, markdown_files, page_name, read_lines, MAX_RESULTS


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


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("graph")
    parser.add_argument("--query")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(todos(graph_path(args.graph), args.query), ensure_ascii=False))
        return 0
    except GraphError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
