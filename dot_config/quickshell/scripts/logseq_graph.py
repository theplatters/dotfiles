#!/usr/bin/env python3
"""Safe, bounded JSON operations on a Logseq graph."""
import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys

from logseq_common import (GraphError, MAX_FILE_BYTES, graph_path, markdown_files,
                           page_name, read_lines, MAX_RESULTS)
from logseq_todos import todos

DEFAULT_GRAPH = "/home/franzs/Nextcloud/Documents/Notes"
DATE = re.compile(r"^\d{4}_\d{2}_\d{2}$")


def search(graph, query):
    if not query or len(query) > 5000:
        raise GraphError("query must be between 1 and 5000 characters")
    query = query.casefold()
    result = []
    for path in markdown_files(graph):
        lines = read_lines(path)
        if lines is None:
            continue
        for number, line in enumerate(lines, 1):
            if query in line.casefold():
                result.append({"page": page_name(path, graph), "path": str(path),
                               "line": number, "text": line})
                if len(result) >= MAX_RESULTS:
                    return result
    return result


def append_journal(graph, text, date):
    if not text or len(text) > 100_000:
        raise GraphError("text must be between 1 and 100000 characters")
    if date is None:
        date = datetime.date.today().strftime("%Y_%m_%d")
    if not DATE.fullmatch(date):
        raise GraphError("date must be YYYY_MM_DD")
    try:
        datetime.datetime.strptime(date, "%Y_%m_%d")
    except ValueError as exc:
        raise GraphError("invalid date") from exc
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    graph_fd = journals_fd = lock_fd = target_fd = temp_fd = None
    temporary = None
    try:
        graph_fd = os.open(graph, flags)
        try:
            os.mkdir("journals", 0o755, dir_fd=graph_fd)
        except FileExistsError:
            pass
        try:
            journals_fd = os.open("journals", flags, dir_fd=graph_fd)
        except OSError as exc:
            raise GraphError("journals path is unsafe") from exc
        try:
            lock_fd = os.open(".logseq_graph.lock",
                              os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                              0o600, dir_fd=journals_fd)
        except OSError as exc:
            raise GraphError("lock path is unsafe") from exc
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise GraphError("lock path is unsafe")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            target_fd = os.open(date + ".md", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                dir_fd=journals_fd)
        except FileNotFoundError:
            old, mode = b"", 0o644
        else:
            target_stat = os.fstat(target_fd)
            if not stat.S_ISREG(target_stat.st_mode):
                raise GraphError("journal path is unsafe")
            mode = target_stat.st_mode & 0o777
            chunks = []
            remaining = MAX_FILE_BYTES + 1
            while remaining:
                chunk = os.read(target_fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            old = b"".join(chunks)
            if len(old) > MAX_FILE_BYTES:
                raise GraphError("journal is too large")
        separator = b"" if not old or old.endswith(b"\n") else b"\n"
        payload = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        addition = separator + payload + (b"" if payload.endswith(b"\n") else b"\n")
        first_line = 1 if not old else old.count(b"\n") + (1 if old.endswith(b"\n") else 2)
        for attempt in range(10):
            temporary = f".logseq-{os.getpid()}-{attempt}"
            try:
                temp_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                  mode, dir_fd=journals_fd)
                break
            except FileExistsError:
                continue
        else:
            raise GraphError("cannot create temporary journal")
        os.fchmod(temp_fd, mode)
        data = old + addition
        view = memoryview(data)
        while view:
            view = view[os.write(temp_fd, view):]
        os.fsync(temp_fd)
        os.close(temp_fd); temp_fd = None
        os.replace(temporary, date + ".md", src_dir_fd=journals_fd, dst_dir_fd=journals_fd)
        os.fsync(journals_fd)
        return {"path": str(graph / "journals" / (date + ".md")), "page": date, "line": first_line}
    finally:
        if temporary is not None and journals_fd is not None:
            try: os.unlink(temporary, dir_fd=journals_fd)
            except OSError: pass
        for fd in (target_fd, lock_fd, journals_fd, graph_fd, temp_fd):
            if fd is not None:
                try: os.close(fd)
                except OSError: pass


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--graph", default=os.environ.get("LOGSEQ_GRAPH") or DEFAULT_GRAPH)
    sub = parser.add_subparsers(dest="command", required=True)
    todos_parser = sub.add_parser("todos"); todos_parser.add_argument("--query")
    found = sub.add_parser("search"); found.add_argument("query")
    add = sub.add_parser("append"); add.add_argument("--text", required=True); add.add_argument("--date")
    args = parser.parse_args(argv)
    try:
        graph = graph_path(args.graph)
        if args.command == "todos": value = todos(graph, args.query)
        elif args.command == "search": value = search(graph, args.query)
        else: value = append_journal(graph, args.text, args.date)
        print(json.dumps(value, ensure_ascii=False))
        return 0
    except (GraphError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
