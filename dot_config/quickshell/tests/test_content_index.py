"""Tests for the derived content index (scripts/content_index.py).

Covers the exact FTS5 declaration check, the per-apply INSERT helper,
and rebuild idempotency over a fixture graph. The fixture graph is
never written by rebuild (read-only on markdown).
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import annotations
import content_index
from content_index import ContentIndexError

SCRIPT = ROOT / "scripts" / "content_index.py"
PID = "11111111-1111-4111-8111-111111111111"
SID = "a" * 32


def run_cli(args, env_extra=None):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, timeout=30, check=False, env=env)


def write_graph(base):
    graph = base / "graph"
    (graph / "pages").mkdir(parents=True)
    (graph / "journals").mkdir(parents=True)
    page = (graph / "pages" / "Demo.md")
    page.write_text(
        "- TODO Fix the retry backoff\n"
        "  quickshell-session:: %s\n"
        "  quickshell-ref:: file:///tmp/work.py\n"
        "- NOTE plain bullet\n"
        "  quickshell-ref:: file:///tmp/other.py\n"
        "- [ ] checkbox task without marker\n"
        "```\n"
        "- TODO fenced code is not content\n"
        "```\n" % SID, encoding="utf-8")
    journal = (graph / "journals" / "2026_09_21.md")
    journal.write_text(
        "- single writer might be enough here\n"
        "  quickshell-session:: %s\n"
        "  quickshell-at:: 2026-09-21T09:00\n"
        "- TODO buy milk\n" % SID, encoding="utf-8")
    return graph


class DeclarationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.db = str(self.base / "annotations.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_fresh_db_validates(self):
        conn = annotations.connect(self.db)
        try:
            verdict = annotations.check_content_index(conn)
            self.assertEqual(verdict, {"ok": True, "reason": ""})
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            for shadow in annotations.CONTENT_INDEX_SHADOW_TABLES:
                self.assertIn(shadow, names)
        finally:
            conn.close()

    def test_schema_sql_carries_exact_declaration(self):
        def _flat(text):
            return "".join(text.split())
        self.assertIn(_flat(annotations.CONTENT_INDEX_SQL),
                      _flat(annotations.SCHEMA_SQL))
        self.assertIn("content_index", annotations.EXPECTED_TABLES)

    def test_missing_table_reports_missing(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            verdict = annotations.check_content_index(conn)
            self.assertEqual(verdict["ok"], False)
            self.assertIn("missing", verdict["reason"])
        finally:
            conn.close()

    def test_spoofed_table_reports_unexpected(self):
        conn = annotations.connect(self.db)
        try:
            conn.execute("DROP TABLE content_index")
            conn.execute("CREATE TABLE content_index(text, page)")
            conn.commit()
            verdict = annotations.check_content_index(conn)
            self.assertEqual(verdict["ok"], False)
            self.assertIn("unexpected", verdict["reason"])
        finally:
            conn.close()

    def test_rebuild_repairs_spoofed_table(self):
        graph = write_graph(self.base)
        conn = annotations.connect(self.db)
        try:
            conn.execute("DROP TABLE content_index")
            conn.execute("CREATE TABLE content_index(text, page)")
            conn.commit()
        finally:
            conn.close()
        out = content_index.rebuild(self.db, str(graph))
        self.assertTrue(out["ok"])
        self.assertGreater(out["rows"], 0)
        conn = annotations.connect(self.db)
        try:
            self.assertEqual(annotations.check_content_index(conn)["ok"],
                             True)
        finally:
            conn.close()


class IndexBlockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.db = str(self.base / "annotations.db")
        self.conn = annotations.connect(self.db)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.temp.cleanup()

    def _match(self, term):
        return self.conn.execute(
            "SELECT text, page, line, kind, session_id, project_id"
            " FROM content_index WHERE content_index MATCH ?"
            " ORDER BY rowid ASC", (term,)).fetchall()

    def test_insert_and_match_roundtrip(self):
        rowid = content_index.index_block(
            self.conn, text="Fix the retry backoff", page="Demo",
            line=12, kind="todo", session_id=SID, project_id=PID)
        self.assertGreaterEqual(rowid, 1)
        rows = self._match("backoff")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["text"], "Fix the retry backoff")
        self.assertEqual(row["page"], "Demo")
        self.assertEqual(row["line"], 12)
        self.assertEqual(row["kind"], "todo")
        self.assertEqual(row["session_id"], SID)
        self.assertEqual(row["project_id"], PID)

    def test_diacritics_fold_like_activity_fts(self):
        content_index.index_block(
            self.conn, text="caf\u00e9 reopen", page="Demo", kind="todo")
        self.assertEqual(len(self._match("cafe")), 1)

    def test_unindexed_columns_are_not_searchable(self):
        content_index.index_block(
            self.conn, text="someordinaryword", page="UniquePageNameZZ",
            kind="thought")
        # Page is UNINDEXED: searching it matches nothing.
        self.assertEqual(self._match("UniquePageNameZZ"), [])
        self.assertEqual(len(self._match("someordinaryword")), 1)

    def test_defaults_and_validation(self):
        rowid = content_index.index_block(
            self.conn, text="bare thought", page="2026_09_21",
            kind="thought")
        self.assertGreaterEqual(rowid, 1)
        rows = self._match("bare")
        self.assertEqual(rows[0]["line"], 0)
        self.assertEqual(rows[0]["session_id"], "")
        for kwargs in ({"text": "", "page": "p", "kind": "todo"},
                       {"text": "x" * 4001, "page": "p", "kind": "todo"},
                       {"text": "ok", "page": "", "kind": "todo"},
                       {"text": "ok", "page": "p", "kind": "idea"},
                       {"text": "ok", "page": "p", "kind": "todo",
                        "session_id": "short"},
                       {"text": "ok", "page": "p", "kind": "todo",
                        "project_id": "nope"},
                       {"text": "ok", "page": "p", "kind": "todo",
                        "line": -1},
                       {"text": None, "page": "p", "kind": "todo"}):
            with self.subTest(kwargs=str(kwargs)[:60]):
                with self.assertRaises(ContentIndexError):
                    content_index.index_block(self.conn, **kwargs)
        with self.assertRaises(ContentIndexError):
            content_index.index_block(None, text="ok", page="p",
                                      kind="todo")


class RebuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.db = str(self.base / "annotations.db")
        self.graph = write_graph(self.base)
        self.before = {}
        for path in (self.graph / "pages" / "Demo.md",
                     self.graph / "journals" / "2026_09_21.md"):
            self.before[path] = path.read_bytes()

    def tearDown(self):
        self.temp.cleanup()

    def _rows(self):
        conn = annotations.connect(self.db)
        try:
            return conn.execute(
                "SELECT text, page, line, kind, session_id, project_id"
                " FROM content_index ORDER BY rowid ASC").fetchall()
        finally:
            conn.close()

    def test_rebuild_indexes_todos_and_thoughts(self):
        out = content_index.rebuild(self.db, str(self.graph))
        self.assertTrue(out["ok"])
        self.assertEqual(out["files"], 2)
        rows = self._rows()
        kinds = sorted(r["kind"] for r in rows)
        self.assertIn("todo", kinds)
        self.assertIn("thought", kinds)
        texts = [r["text"] for r in rows]
        # Fenced code is never indexed.
        self.assertFalse(any("fenced" in t for t in texts))
        # The session marker rides along on its TODO row.
        backoff = [r for r in rows if "backoff" in r["text"]]
        self.assertEqual(len(backoff), 1)
        self.assertEqual(backoff[0]["session_id"], SID)
        self.assertEqual(backoff[0]["page"], "Demo")
        # The journal thought carries its session too.
        thoughts = [r for r in rows if r["kind"] == "thought"]
        self.assertTrue(any(SID in (r["session_id"], r["text"])
                            for r in thoughts))
        # Graph untouched: rebuild is read-only on markdown.
        for path, raw in self.before.items():
            self.assertEqual(path.read_bytes(), raw)

    def test_rebuild_is_idempotent(self):
        first = content_index.rebuild(self.db, str(self.graph))
        second = content_index.rebuild(self.db, str(self.graph))
        self.assertEqual(first["rows"], second["rows"])
        snap = [(r["text"], r["page"], r["line"], r["kind"],
                 r["session_id"], r["project_id"]) for r in self._rows()]
        content_index.rebuild(self.db, str(self.graph))
        again = [(r["text"], r["page"], r["line"], r["kind"],
                  r["session_id"], r["project_id"]) for r in self._rows()]
        self.assertEqual(snap, again)

    def test_rebuild_matches_searchable_content(self):
        content_index.rebuild(self.db, str(self.graph))
        conn = annotations.connect(self.db)
        try:
            hits = conn.execute(
                "SELECT COUNT(*) AS n FROM content_index"
                " WHERE content_index MATCH 'backoff'").fetchone()["n"]
            self.assertEqual(int(hits), 1)
        finally:
            conn.close()

    def test_rebuild_missing_graph_fails_closed(self):
        with self.assertRaises(ContentIndexError):
            content_index.rebuild(
                self.db, str(self.base / "no-such-graph"))

    def test_rebuild_cli_roundtrip(self):
        proc = run_cli(["--db", self.db, "--graph", str(self.graph),
                        "rebuild"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        value = json.loads(proc.stdout)
        self.assertTrue(value.get("ok"))
        self.assertGreater(value.get("rows", 0), 0)
        check = run_cli(["--db", self.db, "check"])
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertTrue(json.loads(check.stdout).get("ok"))
        bad = run_cli(["--db", self.db, "--graph",
                       str(self.base / "no-such-graph"), "rebuild"])
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("error:", bad.stderr)
        self.assertNotIn("Traceback", bad.stderr)
        self.assertEqual(bad.stdout, "")
        lines = [line for line in bad.stderr.strip().splitlines()
                 if line]
        self.assertEqual(len(lines), 1)


class SearchContentTests(unittest.TestCase):
    """Tests for search_content (Phase 2c §5.5 union reader)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.db = str(self.base / "annotations.db")
        self.conn = annotations.connect(self.db)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.temp.cleanup()

    def _index(self, text, **over):
        kwargs = {"page": "Demo", "kind": "todo"}
        kwargs.update(over)
        return content_index.index_block(self.conn, text=text, **kwargs)

    def test_search_returns_bounded_row_dicts(self):
        self._index("Fix the retry backoff", line=12, kind="todo",
                    session_id=SID, project_id=PID)
        rows = content_index.search_content(self.conn, "backoff")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row, {"text": "Fix the retry backoff",
                               "page": "Demo", "line": 12,
                               "kind": "todo", "session_id": SID,
                               "project_id": PID})

    def test_tokens_are_anded_literally(self):
        self._index("alpha backoff note", session_id=SID)
        self._index("alpha nothing else", session_id="c" * 32)
        rows = content_index.search_content(self.conn, "alpha backoff")
        self.assertEqual(len(rows), 1)
        self.assertIn("backoff", rows[0]["text"])
        # User FTS operators cannot inject: OR is just another token.
        self.assertEqual(
            content_index.search_content(self.conn, "backoff OR nothing"),
            [])
        self.assertEqual(
            content_index.search_content(self.conn, "OR"), [])

    def test_quote_chars_cannot_break_match(self):
        self._index("quoted backoff row", session_id=SID)
        rows = content_index.search_content(self.conn, '"backoff"')
        self.assertEqual(len(rows), 1)
        rows = content_index.search_content(self.conn, 'backoff" AND "1"="1')
        self.assertLessEqual(len(rows), 1)

    def test_limit_bounds_results(self):
        for index in range(5):
            self._index("repeatedword row %d" % index,
                        session_id="%032x" % (index + 1))
        rows = content_index.search_content(self.conn, "repeatedword",
                                            limit=2)
        self.assertEqual(len(rows), 2)
        rows = content_index.search_content(self.conn, "repeatedword")
        self.assertEqual(len(rows), 5)

    def test_bad_input_fails_closed(self):
        for query in ("", "   ", None, 123):
            with self.subTest(query=repr(query)):
                with self.assertRaises(ContentIndexError):
                    content_index.search_content(self.conn, query)
        for limit in (0, -1, 1001, True):
            with self.subTest(limit=repr(limit)):
                with self.assertRaises(ContentIndexError):
                    content_index.search_content(self.conn, "backoff",
                                                 limit=limit)
        with self.assertRaises(ContentIndexError):
            content_index.search_content(None, "backoff")

    def test_missing_table_reads_unavailable(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            with self.assertRaises(ContentIndexError) as ctx:
                content_index.search_content(conn, "backoff")
            self.assertIn("content index is unavailable",
                          str(ctx.exception))
        finally:
            conn.close()

    def test_dropping_table_costs_only_a_rescan(self):
        graph = write_graph(self.base)
        out = content_index.rebuild(self.db, str(graph))
        self.assertGreater(out["rows"], 0)
        before = content_index.search_content(self.conn, "backoff")
        self.assertEqual(len(before), 1)
        # Dropping the derived index loses search until the next
        # rebuild — never data (the graph is untouched).
        self.conn.execute("DROP TABLE content_index")
        self.conn.commit()
        with self.assertRaises(ContentIndexError):
            content_index.search_content(self.conn, "backoff")
        out = content_index.rebuild(self.db, str(graph))
        self.assertTrue(out["ok"])
        after = content_index.search_content(self.conn, "backoff")
        self.assertEqual(
            [(r["text"], r["page"], r["session_id"]) for r in after],
            [(r["text"], r["page"], r["session_id"]) for r in before])


if __name__ == "__main__":
    unittest.main()
