"""Tests for the annotations sidecar (scripts/annotations.py)."""

import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import annotations

SID = "a" * 32
SID2 = "b" * 32
PID = "11111111-1111-4111-8111-111111111111"
PID2 = "22222222-2222-4222-8222-222222222222"

SCRIPT = ROOT / "scripts" / "annotations.py"


def run_cli(args, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        text=True, capture_output=True, timeout=20, check=False, env=env,
    )
    return completed


def temp_db_path(base, name="annotations.db"):
    return str(base / name)


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_schema_has_all_v1_tables(self):
        conn = annotations.connect(temp_db_path(self.base))
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            names = {r["name"] for r in rows}
            for expected in ("captures", "drafts", "prepared",
                             "reviews", "state", "session_meta",
                             "session_links"):
                self.assertIn(expected, names)
            for gone in ("session_labels", "project_overrides",
                         "jev_calls", "unit_labels", "focus_blocks"):
                self.assertNotIn(gone, names)
        finally:
            conn.close()

    def test_connection_uses_row_factory_wal_and_busy_timeout(self):
        path = temp_db_path(self.base)
        conn = annotations.connect(path)
        try:
            self.assertEqual(conn.row_factory, sqlite3.Row)
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(str(mode).lower(), "wal")
            timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertGreaterEqual(int(timeout), 1000)
        finally:
            conn.close()

    def test_schema_is_idempotent(self):
        path = temp_db_path(self.base)
        conn = annotations.connect(path)
        try:
            pass
        finally:
            conn.close()
        conn2 = annotations.connect(path)
        try:
            count = conn2.execute(
                "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table'").fetchone()["n"]
            self.assertGreaterEqual(count, 7)
        finally:
            conn2.close()


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.conn = annotations.connect(temp_db_path(self.base))

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.temp.cleanup()

    def test_state_roundtrip_and_default(self):
        self.assertIsNone(annotations.get_state(self.conn, "missing"))
        self.assertEqual(annotations.get_state(self.conn, "missing", default="d"), "d")
        annotations.set_state(self.conn, "tick_last_ms", "123")
        self.assertEqual(annotations.get_state(self.conn, "tick_last_ms"), "123")
        annotations.set_state(self.conn, "tick_last_ms", "456")
        self.assertEqual(annotations.get_state(self.conn, "tick_last_ms"), "456")

    def test_state_mutation_commits(self):
        annotations.set_state(self.conn, "k", "v")
        path = temp_db_path(self.base)
        conn2 = annotations.connect(path)
        try:
            self.assertEqual(annotations.get_state(conn2, "k"), "v")
        finally:
            conn2.close()

    def test_state_key_value_validation(self):
        for bad in ("", "   ", None, 123, True, "x" * 257, "a\x00b"):
            with self.assertRaises(annotations.AnnotationsError):
                annotations.set_state(self.conn, bad, "v")
            with self.assertRaises(annotations.AnnotationsError):
                annotations.get_state(self.conn, bad)
        for bad in (None, 123, True, b"x", "x" * (64 * 1024 + 1)):
            with self.assertRaises(annotations.AnnotationsError):
                annotations.set_state(self.conn, "k", bad)


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.old_env = os.environ.get(annotations.ENV_DB)
        os.environ.pop(annotations.ENV_DB, None)
        self.old_xdg = os.environ.get("XDG_STATE_HOME")

    def tearDown(self):
        if self.old_env is None:
            os.environ.pop(annotations.ENV_DB, None)
        else:
            os.environ[annotations.ENV_DB] = self.old_env
        if self.old_xdg is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self.old_xdg
        self.temp.cleanup()

    def test_new_db_is_0600(self):
        path = str(self.base / "annotations.db")
        conn = annotations.connect(path)
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o600)
        finally:
            conn.close()

    def test_new_db_is_0600_under_permissive_umask(self):
        path = str(self.base / "umask.db")
        old = os.umask(0o022)
        try:
            conn = annotations.connect(path)
        finally:
            os.umask(old)
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o600)
        finally:
            conn.close()

    def test_default_private_dir_is_0700(self):
        os.environ["XDG_STATE_HOME"] = str(self.base / "state")
        conn = annotations.connect(None)
        try:
            leaf = annotations.default_db_path().parent
            self.assertTrue(str(leaf).startswith(str(self.base)))
            mode = stat.S_IMODE(os.stat(leaf).st_mode)
            self.assertEqual(mode, 0o700)
        finally:
            conn.close()

    def test_existing_world_readable_db_rejected(self):
        path = str(self.base / "annotations.db")
        conn = annotations.connect(path)
        try:
            pass
        finally:
            conn.close()
        os.chmod(path, 0o644)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.connect(path)

    def test_db_symlink_rejected(self):
        real = self.base / "real.db"
        conn = annotations.connect(str(real))
        try:
            pass
        finally:
            conn.close()
        link = self.base / "link.db"
        link.symlink_to(real)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.connect(str(link))

    def test_wal_shm_symlink_rejected(self):
        path = str(self.base / "annotations.db")
        conn = annotations.connect(path)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS t(x)")
            conn.execute("INSERT INTO t(x) VALUES(1)")
            conn.commit()
        finally:
            conn.close()
        # Force WAL artifacts to exist.
        conn = annotations.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS t2(y)")
            conn.commit()
        finally:
            conn.close()
        wal = Path(str(path) + "-wal")
        shm = Path(str(path) + "-shm")
        # At least one sidecar may exist after WAL writes; plant symlinks
        # for both suffixes to prove the guard regardless of checkpointing.
        for sidecar in (wal, shm):
            try:
                if sidecar.exists() or sidecar.is_symlink():
                    sidecar.unlink()
            except OSError:
                pass
            target = self.base / ("target-%s" % sidecar.suffix)
            target.write_text("x", encoding="utf-8")
            try:
                sidecar.symlink_to(target)
            except FileExistsError:
                pass
        with self.assertRaises(annotations.AnnotationsError):
            annotations.connect(path)
        # Clean up planted symlinks so the temp dir teardown stays quiet.
        for sidecar in (wal, shm):
            try:
                if sidecar.is_symlink():
                    sidecar.unlink()
            except OSError:
                pass

    def test_journal_symlink_rejected(self):
        # Rollback-journal mode leaves a -journal sidecar path; even when
        # the main db runs WAL, a planted -journal symlink must be refused.
        path = str(self.base / "journal.db")
        conn = annotations.connect(path)
        try:
            pass
        finally:
            conn.close()
        journal = Path(str(path) + "-journal")
        try:
            if journal.exists() or journal.is_symlink():
                journal.unlink()
        except OSError:
            pass
        target = self.base / "journal-target"
        target.write_text("x", encoding="utf-8")
        journal.symlink_to(target)
        try:
            with self.assertRaises(annotations.AnnotationsError):
                annotations.connect(path)
        finally:
            try:
                if journal.is_symlink():
                    journal.unlink()
            except OSError:
                pass

    def test_nonregular_db_rejected(self):
        directory = self.base / "dir.db"
        directory.mkdir()
        with self.assertRaises(annotations.AnnotationsError):
            annotations.connect(str(directory))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.db = str(self.base / "annotations.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_init_and_state_cli_roundtrip(self):
        init = run_cli(["--db", self.db, "init"])
        self.assertEqual(init.returncode, 0, init.stderr)
        value = json.loads(init.stdout)
        self.assertTrue(value.get("ok"))
        set_out = run_cli(["--db", self.db, "state-set", "--key", "tick_last_ms",
                           "--value", "99"])
        self.assertEqual(set_out.returncode, 0, set_out.stderr)
        get_out = run_cli(["--db", self.db, "state-get", "--key", "tick_last_ms"])
        self.assertEqual(get_out.returncode, 0, get_out.stderr)
        self.assertEqual(json.loads(get_out.stdout)["value"], "99")

    def test_cli_errors_are_single_line_no_traceback_no_leak(self):
        run_cli(["--db", self.db, "init"])
        bad_key = run_cli(["--db", self.db, "state-get", "--key", ""])
        self.assertNotEqual(bad_key.returncode, 0)
        self.assertIn("error:", bad_key.stderr)
        self.assertNotIn("Traceback", bad_key.stderr)
        self.assertEqual(bad_key.stdout, "")
        bad_value = run_cli(["--db", self.db, "state-set",
                             "--key", "k", "--value", "x" * 70000])
        self.assertNotEqual(bad_value.returncode, 0)
        self.assertIn("error:", bad_value.stderr)
        self.assertNotIn("Traceback", bad_value.stderr)
        # Deleted subcommands fail closed without echo.
        secret = "SECRET-MARKER-9f8e7d6c5b"
        proc = run_cli(["--db", self.db, "set-label", "--session", secret])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("error:", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertNotIn(secret, proc.stderr + proc.stdout)
        self.assertEqual(proc.stdout, "")
        bad_calls = run_cli(["--db", self.db, "count-calls",
                             "--since-ms", "nope"])
        self.assertNotEqual(bad_calls.returncode, 0)
        self.assertIn("error:", bad_calls.stderr)
        self.assertNotIn("Traceback", bad_calls.stderr)
        # Symlinked DB via CLI is refused safely.
        real = str(self.base / "real.db")
        run_cli(["--db", real, "init"])
        link = str(self.base / "link.db")
        try:
            Path(link).symlink_to(real)
        except FileExistsError:
            pass
        linked = run_cli(["--db", link, "init"])
        self.assertNotEqual(linked.returncode, 0)
        self.assertIn("error:", linked.stderr)
        self.assertNotIn("Traceback", linked.stderr)


class PathTraversalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.old_env = os.environ.get(annotations.ENV_DB)
        os.environ.pop(annotations.ENV_DB, None)
        self.old_xdg = os.environ.get("XDG_STATE_HOME")
        os.environ.pop("XDG_STATE_HOME", None)

    def tearDown(self):
        if self.old_env is None:
            os.environ.pop(annotations.ENV_DB, None)
        else:
            os.environ[annotations.ENV_DB] = self.old_env
        if self.old_xdg is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self.old_xdg
        self.temp.cleanup()

    def test_symlink_dotdot_absolute_rejected_no_creation(self):
        other_deep = self.base / "other" / "deep"
        other_deep.mkdir(parents=True)
        link = self.base / "link"
        link.symlink_to(other_deep)
        # Absolute path with symlink component + '..' must be rejected
        # before normalization; SQLite must never open the raw string.
        evil = str(link / ".." / "a.db")
        self.assertIn("..", evil)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.connect(evil)
        self.assertFalse((self.base / "a.db").exists())
        self.assertFalse((other_deep / "a.db").exists())
        self.assertFalse((other_deep.parent / "a.db").exists())

    def test_explicit_relative_dotdot_rejected(self):
        with self.assertRaises(annotations.AnnotationsError):
            annotations.resolve_db_path(os.path.join("sub", "..", "evil.db"))
        with self.assertRaises(annotations.AnnotationsError):
            annotations.connect(os.path.join("sub", "..", "evil.db"))

    def test_env_relative_dotdot_rejected(self):
        os.environ[annotations.ENV_DB] = os.path.join("rel", "..", "evil.db")
        with self.assertRaises(annotations.AnnotationsError):
            annotations.resolve_db_path(None)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.connect(None)

    def test_xdg_dotdot_rejected(self):
        os.environ["XDG_STATE_HOME"] = str(self.base / "link" / ".." / "evil-state")
        with self.assertRaises(annotations.AnnotationsError):
            annotations.default_db_path()
        with self.assertRaises(annotations.AnnotationsError):
            annotations.connect(None)
        self.assertFalse((self.base / "evil-state").exists())


class SchemaConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_init_schema_error_wrapped(self):
        conn = sqlite3.connect(":memory:")
        conn.close()
        with self.assertRaises(annotations.AnnotationsError):
            annotations._init_schema(conn)
        try:
            annotations._init_schema(conn)
        except annotations.AnnotationsError:
            pass
        except sqlite3.Error:
            self.fail("sqlite3.Error leaked instead of AnnotationsError")

    def test_two_connections_busy_short_timeout(self):
        import sqlite3 as _sqlite3
        path = str(self.base / "busy.db")
        old_umask = os.umask(0o077)
        try:
            holder = _sqlite3.connect(path, timeout=5.0, isolation_level=None)
        finally:
            os.umask(old_umask)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        try:
            holder.execute("PRAGMA journal_mode=WAL")
            holder.execute("BEGIN EXCLUSIVE")
            holder.execute("CREATE TABLE holder(x)")
            old_timeout = annotations.BUSY_TIMEOUT_MS
            annotations.BUSY_TIMEOUT_MS = 50
            try:
                with self.assertRaises(annotations.AnnotationsError):
                    try:
                        conn2 = annotations.connect(path)
                    except annotations.AnnotationsError:
                        raise
                    except _sqlite3.Error:
                        self.fail("sqlite3.Error leaked instead of AnnotationsError")
                    else:
                        try:
                            conn2.close()
                        except Exception:
                            pass
                        self.fail("expected AnnotationsError under lock")
            finally:
                annotations.BUSY_TIMEOUT_MS = old_timeout
        finally:
            try:
                holder.execute("ROLLBACK")
            except Exception:
                pass
            try:
                holder.close()
            except Exception:
                pass


class ArgparseSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.db = str(self.base / "annotations.db")

    def tearDown(self):
        self.temp.cleanup()

    def _assert_safe_error(self, proc, secret=None):
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("error:", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(proc.stdout, "")
        # Single line: no embedded newline beyond trailing one.
        self.assertNotIn("\n", proc.stderr.strip())
        if secret is not None:
            self.assertNotIn(secret, proc.stderr)
            self.assertNotIn(secret, proc.stdout)

    def test_unknown_command_no_echo(self):
        secret = "SECRET-UNKNOWN-4f2a9c"
        proc = run_cli(["--db", self.db, "unknown-%s" % secret])
        self._assert_safe_error(proc, secret=secret)

    def test_missing_required_no_echo(self):
        proc = run_cli(["--db", self.db, "get-label"])
        self._assert_safe_error(proc)

    def test_malformed_newline_no_echo(self):
        secret = "SECRET-NEWLINE-7b3e1a"
        proc = run_cli(["--db", self.db, "get-label", "--session",
                        "bad\n%s\nline" % secret])
        self._assert_safe_error(proc, secret=secret)
        proc2 = run_cli(["--db", self.db, "--bad-flag-%s" % secret, "init"])
        self._assert_safe_error(proc2, secret=secret)

    def test_help_preserved(self):
        top = run_cli(["--help"])
        self.assertEqual(top.returncode, 0)
        self.assertIn("init", top.stdout)
        sub = run_cli(["state-get", "--help"])
        self.assertEqual(sub.returncode, 0)
        self.assertIn("key", sub.stdout.lower())


class RecursionSafetyTests(unittest.TestCase):
    def test_raw_json_recursion_wrapped(self):
        import unittest.mock as mock
        with mock.patch.object(annotations.json, "loads",
                               side_effect=RecursionError("deep")):
            with self.assertRaises(annotations.AnnotationsError):
                annotations._validate_raw_json('{"a":1}')

    def test_emit_recursion_wrapped(self):
        import unittest.mock as mock
        with mock.patch.object(annotations.json, "dumps",
                               side_effect=RecursionError("deep")):
            with self.assertRaises(annotations.AnnotationsError):
                annotations._emit({"ok": True})


class DraftStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.conn = annotations.connect(str(self.base / "annotations.db"))

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.temp.cleanup()

    def _payload(self, tag="a"):
        return {"draft_id": "dwl_" + tag, "markdown": "hello",
                "sections": []}

    def test_save_and_get_roundtrip(self):
        self.assertIsNone(annotations.get_draft(self.conn, "dwl_missing"))
        annotations.save_draft(
            self.conn, draft_id="dwl_abc", project_id=PID,
            session_id=SID, summary_key="abc..def",
            evidence_digest="e" * 64, payload=self._payload(),
            created_ms=1000)
        row = annotations.get_draft(self.conn, "dwl_abc")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["draft_id"], "dwl_abc")
        self.assertEqual(row["project_id"], PID)
        self.assertEqual(row["session_id"], SID)
        self.assertEqual(row["summary_key"], "abc..def")
        self.assertEqual(row["evidence_digest"], "e" * 64)
        self.assertEqual(row["draft"], self._payload())
        self.assertEqual(row["created_ms"], 1000)
        self.assertFalse(row["polished"])
        self.assertIsNone(row["saved_journal_ms"])
        self.assertIsNone(row["saved_page_ms"])

    def test_save_allows_null_session(self):
        annotations.save_draft(
            self.conn, draft_id="dwl_null", project_id=PID,
            session_id=None, summary_key="k",
            evidence_digest="e" * 64, payload=self._payload("n"),
            created_ms=7)
        row = annotations.get_draft(self.conn, "dwl_null")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertIsNone(row["session_id"])

    def test_upsert_preserves_saved_flags(self):
        annotations.save_draft(
            self.conn, draft_id="dwl_up", project_id=PID,
            session_id=SID, summary_key="k1",
            evidence_digest="e" * 64, payload=self._payload("1"),
            created_ms=1000)
        self.assertTrue(annotations.mark_draft_saved(
            self.conn, "dwl_up", "journal", now_ms=2000))
        self.assertTrue(annotations.mark_draft_saved(
            self.conn, "dwl_up", "page", now_ms=3000))
        # Re-saving (cache refresh) must never clear the save markers.
        annotations.save_draft(
            self.conn, draft_id="dwl_up", project_id=PID,
            session_id=SID, summary_key="k2",
            evidence_digest="f" * 64, payload=self._payload("2"),
            created_ms=4000)
        row = annotations.get_draft(self.conn, "dwl_up")
        assert row is not None
        self.assertEqual(row["saved_journal_ms"], 2000)
        self.assertEqual(row["saved_page_ms"], 3000)
        self.assertEqual(row["draft"], self._payload("2"))
        self.assertEqual(row["summary_key"], "k2")

    def test_upsert_without_polished_carries_polished_payload(self):
        annotations.save_draft(
            self.conn, draft_id="dwl_carry", project_id=PID,
            session_id=SID, summary_key="k",
            evidence_digest="e" * 64, payload=self._payload("1"),
            created_ms=1000)
        self.assertTrue(annotations.mark_draft_polished(
            self.conn, "dwl_carry", "# shiny", now_ms=1500))
        # A same-digest refresh (no polished flag) keeps the polished text
        # in the payload and keeps the column state.
        annotations.save_draft(
            self.conn, draft_id="dwl_carry", project_id=PID,
            session_id=SID, summary_key="k",
            evidence_digest="e" * 64, payload=self._payload("2"),
            created_ms=2000)
        row = annotations.get_draft(self.conn, "dwl_carry")
        assert row is not None
        self.assertTrue(row["polished"])
        self.assertEqual(row["draft"]["polished_markdown"], "# shiny")
        self.assertTrue(row["draft"]["polished"])
        self.assertEqual(row["draft"]["markdown"], "hello")
        found = annotations.find_draft(self.conn, PID, SID, "e" * 64)
        assert found is not None
        self.assertEqual(found["draft"]["polished_markdown"], "# shiny")
        # An explicit polished flag stays authoritative (no carry).
        annotations.save_draft(
            self.conn, draft_id="dwl_carry", project_id=PID,
            session_id=SID, summary_key="k",
            evidence_digest="e" * 64, payload=self._payload("3"),
            created_ms=3000, polished=False)
        row = annotations.get_draft(self.conn, "dwl_carry")
        assert row is not None
        self.assertFalse(row["polished"])
        self.assertNotIn("polished_markdown", row["draft"])

    def test_upsert_polished_only_when_provided(self):
        annotations.save_draft(
            self.conn, draft_id="dwl_pol", project_id=PID,
            session_id=SID, summary_key="k",
            evidence_digest="e" * 64, payload=self._payload(),
            created_ms=1000, polished=True)
        row = annotations.get_draft(self.conn, "dwl_pol")
        assert row is not None
        self.assertTrue(row["polished"])
        # A later upsert without polished=... keeps the flag.
        annotations.save_draft(
            self.conn, draft_id="dwl_pol", project_id=PID,
            session_id=SID, summary_key="k",
            evidence_digest="e" * 64, payload=self._payload(),
            created_ms=2000)
        row = annotations.get_draft(self.conn, "dwl_pol")
        assert row is not None
        self.assertTrue(row["polished"])
        # Explicit False clears it.
        annotations.save_draft(
            self.conn, draft_id="dwl_pol", project_id=PID,
            session_id=SID, summary_key="k",
            evidence_digest="e" * 64, payload=self._payload(),
            created_ms=3000, polished=False)
        row = annotations.get_draft(self.conn, "dwl_pol")
        assert row is not None
        self.assertFalse(row["polished"])

    def test_mark_polished_sets_payload_and_flag(self):
        annotations.save_draft(
            self.conn, draft_id="dwl_m", project_id=PID,
            session_id=SID, summary_key="k",
            evidence_digest="e" * 64, payload=self._payload(),
            created_ms=1000)
        self.assertFalse(annotations.mark_draft_polished(
            self.conn, "dwl_nope", "text", now_ms=1000))
        self.assertTrue(annotations.mark_draft_polished(
            self.conn, "dwl_m", "# polished", now_ms=1500))
        row = annotations.get_draft(self.conn, "dwl_m")
        assert row is not None
        self.assertTrue(row["polished"])
        self.assertEqual(row["draft"]["polished_markdown"], "# polished")
        self.assertTrue(row["draft"]["polished"])

    def test_mark_saved_targets(self):
        annotations.save_draft(
            self.conn, draft_id="dwl_s", project_id=PID,
            session_id=SID, summary_key="k",
            evidence_digest="e" * 64, payload=self._payload(),
            created_ms=1000)
        self.assertFalse(annotations.mark_draft_saved(
            self.conn, "dwl_nope", "journal", now_ms=1000))
        with self.assertRaises(annotations.AnnotationsError):
            annotations.mark_draft_saved(
                self.conn, "dwl_s", "inbox", now_ms=1000)
        self.assertTrue(annotations.mark_draft_saved(
            self.conn, "dwl_s", "journal", now_ms=1111))
        self.assertTrue(annotations.mark_draft_saved(
            self.conn, "dwl_s", "page", now_ms=2222))
        row = annotations.get_draft(self.conn, "dwl_s")
        assert row is not None
        self.assertEqual(row["saved_journal_ms"], 1111)
        self.assertEqual(row["saved_page_ms"], 2222)

    def test_find_draft_newest_and_null_session_aware(self):
        annotations.save_draft(
            self.conn, draft_id="dwl_old", project_id=PID,
            session_id=SID, summary_key="k",
            evidence_digest="e" * 64, payload=self._payload("o"),
            created_ms=1000)
        annotations.save_draft(
            self.conn, draft_id="dwl_new", project_id=PID,
            session_id=SID, summary_key="k",
            evidence_digest="e" * 64, payload=self._payload("n"),
            created_ms=2000)
        annotations.save_draft(
            self.conn, draft_id="dwl_null", project_id=PID,
            session_id=None, summary_key="k",
            evidence_digest="e" * 64, payload=self._payload("z"),
            created_ms=3000)
        found = annotations.find_draft(self.conn, PID, SID, "e" * 64)
        assert found is not None
        self.assertEqual(found["draft_id"], "dwl_new")
        # A NULL session never matches a hex session and vice versa.
        found_null = annotations.find_draft(self.conn, PID, None, "e" * 64)
        assert found_null is not None
        self.assertEqual(found_null["draft_id"], "dwl_null")
        self.assertIsNone(annotations.find_draft(
            self.conn, PID, SID2, "e" * 64))
        self.assertIsNone(annotations.find_draft(
            self.conn, PID, SID, "d" * 64))
        self.assertIsNone(annotations.find_draft(
            self.conn, PID2, SID, "e" * 64))

    def test_get_draft_unparseable_returns_none(self):
        annotations.save_draft(
            self.conn, draft_id="dwl_bad", project_id=PID,
            session_id=SID, summary_key="k",
            evidence_digest="e" * 64, payload=self._payload(),
            created_ms=1000)
        self.conn.execute("UPDATE drafts SET json = '[[broken' "
                          "WHERE draft_id = 'dwl_bad'")
        self.conn.commit()
        self.assertIsNone(annotations.get_draft(self.conn, "dwl_bad"))
        found = annotations.find_draft(self.conn, PID, SID, "e" * 64)
        self.assertIsNone(found)

    def test_draft_validation(self):
        for bad in ("", None, 123, "has space!", "x" * 129, "a/b"):
            with self.assertRaises(annotations.AnnotationsError):
                annotations.get_draft(self.conn, bad)
            with self.assertRaises(annotations.AnnotationsError):
                annotations.mark_draft_saved(
                    self.conn, bad, "journal", now_ms=1)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.save_draft(
                self.conn, draft_id="dwl_x", project_id="nope",
                session_id=SID, summary_key="k",
                evidence_digest="e" * 64, payload=self._payload(),
                created_ms=1)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.save_draft(
                self.conn, draft_id="dwl_x", project_id=PID,
                session_id="short", summary_key="k",
                evidence_digest="e" * 64, payload=self._payload(),
                created_ms=1)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.save_draft(
                self.conn, draft_id="dwl_x", project_id=PID,
                session_id=SID, summary_key="k",
                evidence_digest="e" * 64, payload="not-a-dict",
                created_ms=1)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.save_draft(
                self.conn, draft_id="dwl_x", project_id=PID,
                session_id=SID, summary_key="k",
                evidence_digest="e" * 64,
                payload={"blob": "x" * (256 * 1024 + 1)},
                created_ms=1)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.mark_draft_polished(
                self.conn, "dwl_x", "ok", now_ms=-5)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.save_draft(
                self.conn, draft_id="dwl_x", project_id=PID,
                session_id=SID, summary_key="k",
                evidence_digest="e" * 64, payload=self._payload(),
                created_ms=1, polished="yes")


class PreparedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.conn = annotations.connect(str(self.base / "annotations.db"))

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.temp.cleanup()

    def test_create_get_roundtrip(self):
        self.assertIsNone(annotations.get_prepared(self.conn, "tok_missing"))
        annotations.create_prepared(
            self.conn, token="tok_1", kind="journal",
            payload={"draft_id": "dwl_a"}, preview={"target": "journal"},
            revision="r" * 64, expires_ms=5000)
        row = annotations.get_prepared(self.conn, "tok_1")
        assert row is not None
        self.assertEqual(row["token"], "tok_1")
        self.assertEqual(row["kind"], "journal")
        self.assertEqual(row["payload"], {"draft_id": "dwl_a"})
        self.assertEqual(row["preview"], {"target": "journal"})
        self.assertEqual(row["revision"], "r" * 64)
        self.assertEqual(row["expires_ms"], 5000)
        self.assertEqual(row["used"], 0)

    def test_create_allows_null_revision(self):
        annotations.create_prepared(
            self.conn, token="tok_nr", kind="page",
            payload={"a": 1}, preview={"b": 2},
            revision=None, expires_ms=9)
        row = annotations.get_prepared(self.conn, "tok_nr")
        assert row is not None
        self.assertIsNone(row["revision"])

    def test_duplicate_token_rejected(self):
        annotations.create_prepared(
            self.conn, token="tok_dup", kind="journal",
            payload={}, preview={}, revision=None, expires_ms=9)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.create_prepared(
                self.conn, token="tok_dup", kind="journal",
                payload={}, preview={}, revision=None, expires_ms=9)

    def test_consume_single_use_and_expiry(self):
        annotations.create_prepared(
            self.conn, token="tok_once", kind="journal",
            payload={"draft_id": "dwl_a"}, preview={"t": 1},
            revision=None, expires_ms=5000)
        first = annotations.consume_prepared(
            self.conn, "tok_once", now_ms=4999)
        assert first is not None
        self.assertEqual(first["payload"], {"draft_id": "dwl_a"})
        self.assertEqual(first["used"], 1)
        # Second consume is refused: single-use.
        self.assertIsNone(annotations.consume_prepared(
            self.conn, "tok_once", now_ms=4999))
        # get still shows the burned row.
        row = annotations.get_prepared(self.conn, "tok_once")
        assert row is not None
        self.assertEqual(row["used"], 1)
        # Expired tokens are refused without burning (fresh token).
        annotations.create_prepared(
            self.conn, token="tok_old", kind="page",
            payload={}, preview={}, revision=None, expires_ms=100)
        self.assertIsNone(annotations.consume_prepared(
            self.conn, "tok_old", now_ms=101))
        row = annotations.get_prepared(self.conn, "tok_old")
        assert row is not None
        self.assertEqual(row["used"], 0)
        # Boundary: expiry equal to now is still valid.
        self.assertIsNotNone(annotations.consume_prepared(
            self.conn, "tok_old", now_ms=100))
        # Missing token reads as None.
        self.assertIsNone(annotations.consume_prepared(
            self.conn, "tok_missing", now_ms=1))

    def test_prepared_validation_bounded(self):
        for bad in ("", None, "has space!", "x" * 129):
            with self.assertRaises(annotations.AnnotationsError):
                annotations.create_prepared(
                    self.conn, token=bad, kind="journal",
                    payload={}, preview={}, revision=None, expires_ms=1)
            with self.assertRaises(annotations.AnnotationsError):
                annotations.get_prepared(self.conn, bad)
            with self.assertRaises(annotations.AnnotationsError):
                annotations.consume_prepared(self.conn, bad, now_ms=1)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.create_prepared(
                self.conn, token="tok_k", kind="inbox",
                payload={}, preview={}, revision=None, expires_ms=1)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.create_prepared(
                self.conn, token="tok_k", kind="journal",
                payload=[], preview={}, revision=None, expires_ms=1)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.create_prepared(
                self.conn, token="tok_k", kind="journal",
                payload={"blob": "x" * (256 * 1024 + 1)}, preview={},
                revision=None, expires_ms=1)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.create_prepared(
                self.conn, token="tok_k", kind="journal",
                payload={}, preview={}, revision="r" * 129, expires_ms=1)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.consume_prepared(self.conn, "tok_k", now_ms=-1)


class SessionLedgerSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.path = temp_db_path(self.base)

    def tearDown(self):
        self.temp.cleanup()

    def test_new_tables_and_indexes_present(self):
        conn = annotations.connect(self.path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            names = {r["name"] for r in rows}
            self.assertIn("session_meta", names)
            self.assertIn("session_links", names)
            idx = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'").fetchall()
            idx_names = {r["name"] for r in idx}
            self.assertNotIn("idx_session_meta_state", idx_names)
            self.assertIn("idx_session_links_kind_target", idx_names)
            self.assertIn("session_meta", annotations.EXPECTED_TABLES)
            self.assertIn("session_links", annotations.EXPECTED_TABLES)
        finally:
            conn.close()

    def test_schema_idempotent_reconnect(self):
        conn = annotations.connect(self.path)
        try:
            pass
        finally:
            conn.close()
        conn2 = annotations.connect(self.path)
        try:
            rows = conn2.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            names = {r["name"] for r in rows}
            self.assertIn("session_meta", names)
            self.assertIn("session_links", names)
        finally:
            conn2.close()


class SessionMetaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.conn = annotations.connect(str(self.base / "annotations.db"))

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.temp.cleanup()

    def test_get_missing_returns_none(self):
        self.assertIsNone(annotations.get_session_meta(self.conn, SID))

    def test_insert_defaults_and_roundtrip(self):
        row = annotations.upsert_session_meta(
            self.conn, session_id=SID, now_ms=1000)
        self.assertEqual(row["session_id"], SID)
        self.assertEqual(row["device_id"], "")
        self.assertEqual(row["created_ms"], 1000)
        self.assertEqual(row["updated_ms"], 1000)
        self.assertEqual(len(row["revision"]), 32)
        # Reduced shape: no managed content columns survive.
        for gone in ("title", "tags", "state", "filed_ref", "intent",
                     "outcome", "next_step", "ignore_reason",
                     "project_override"):
            self.assertNotIn(gone, row)
        fetched = annotations.get_session_meta(self.conn, SID.upper())
        self.assertIsNotNone(fetched)
        assert fetched is not None
        self.assertEqual(fetched["revision"], row["revision"])

    def test_insert_with_device_id(self):
        row = annotations.upsert_session_meta(
            self.conn, session_id=SID, device_id="c" * 32, now_ms=1000)
        self.assertEqual(row["device_id"], "c" * 32)
        updated = annotations.upsert_session_meta(
            self.conn, session_id=SID, device_id="d" * 32, now_ms=2000,
            expect_revision=row["revision"])
        self.assertEqual(updated["device_id"], "d" * 32)
        self.assertNotEqual(updated["revision"], row["revision"])

    def test_content_fields_rejected(self):
        for field in ("title", "intent", "outcome", "tags", "next_step",
                      "state", "filed_ref", "ignore_reason",
                      "project_override"):
            with self.subTest(field=field):
                with self.assertRaises(TypeError):
                    annotations.upsert_session_meta(
                        self.conn, session_id=SID, now_ms=1000,
                        **{field: "x"})

    def test_cas_stale_revision(self):
        row = annotations.upsert_session_meta(
            self.conn, session_id=SID, now_ms=1000)
        rev = row["revision"]
        with self.assertRaisesRegex(annotations.AnnotationsError,
                                    "session meta changed"):
            annotations.upsert_session_meta(
                self.conn, session_id=SID, device_id="d" * 32,
                now_ms=2000, expect_revision="0" * 32)
        with self.assertRaisesRegex(annotations.AnnotationsError,
                                    "session meta changed"):
            annotations.upsert_session_meta(
                self.conn, session_id=SID, device_id="d" * 32,
                now_ms=2000)
        updated = annotations.upsert_session_meta(
            self.conn, session_id=SID, device_id="d" * 32,
            now_ms=2000, expect_revision=rev)
        self.assertEqual(updated["device_id"], "d" * 32)
        self.assertNotEqual(updated["revision"], rev)
        self.assertEqual(updated["created_ms"], 1000)
        self.assertEqual(updated["updated_ms"], 2000)

    def test_insert_with_expect_revision_fails(self):
        with self.assertRaisesRegex(annotations.AnnotationsError,
                                    "session meta changed"):
            annotations.upsert_session_meta(
                self.conn, session_id=SID, now_ms=1000,
                expect_revision="1" * 32)

    def test_insert_revision_used_and_update_ignores_revision(self):
        custom = "ab" * 16
        row = annotations.upsert_session_meta(
            self.conn, session_id=SID, revision=custom, now_ms=1000)
        self.assertEqual(row["revision"], custom)
        other = "cd" * 16
        updated = annotations.upsert_session_meta(
            self.conn, session_id=SID, device_id="d" * 32,
            revision=other, now_ms=2000, expect_revision=custom)
        self.assertNotEqual(updated["revision"], other)
        self.assertNotEqual(updated["revision"], custom)

    def test_revision_validation(self):
        for bad in ("x", "", "z" * 32, "a" * 31,
                    "a" * 33, 123, None, True, "short"):
            with self.assertRaises(annotations.AnnotationsError):
                annotations._validate_revision(bad)
        self.assertEqual(annotations._validate_revision("AB" * 16), "ab" * 16)

    def test_state_and_session_validation(self):
        with self.assertRaises(annotations.AnnotationsError):
            annotations.get_session_meta(self.conn, "bad")
        with self.assertRaises(annotations.AnnotationsError):
            annotations.upsert_session_meta(
                self.conn, session_id="bad", now_ms=1000)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.upsert_session_meta(
                self.conn, session_id=SID, device_id="bogus",
                now_ms=1000)


class SessionMetaBulkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.conn = annotations.connect(str(self.base / "annotations.db"))

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.temp.cleanup()

    def _sid(self, n):
        return "%032x" % n

    def test_bulk_map_missing_dedupe_empty(self):
        s1 = self._sid(1)
        s2 = self._sid(2)
        missing = self._sid(3)
        annotations.upsert_session_meta(
            self.conn, session_id=s1, device_id="d" * 32, now_ms=1000)
        annotations.upsert_session_meta(
            self.conn, session_id=s2, now_ms=1000)
        out = annotations.list_session_meta(self.conn, [s1, s2, missing])
        self.assertEqual(set(out.keys()), {s1, s2})
        self.assertEqual(out[s1]["device_id"], "d" * 32)
        self.assertEqual(out[s2]["device_id"], "")
        dup = annotations.list_session_meta(self.conn, [s1, s1, s2])
        self.assertEqual(set(dup.keys()), {s1, s2})
        self.assertEqual(annotations.list_session_meta(self.conn, []), {})

    def test_bulk_limit_and_invalid(self):
        with self.assertRaises(annotations.AnnotationsError):
            annotations.list_session_meta(
                self.conn, [self._sid(i) for i in range(1001)])
        with self.assertRaises(annotations.AnnotationsError):
            annotations.list_session_meta(self.conn, [s for s in ["bad"]])
        with self.assertRaises(annotations.AnnotationsError):
            annotations.list_session_meta(self.conn, "not-a-list")


class SessionDraftStubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.conn = annotations.connect(str(self.base / "annotations.db"))

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.temp.cleanup()

    def _save(self, draft_id, created, polished=False):
        annotations.save_draft(
            self.conn, draft_id=draft_id, project_id=PID,
            session_id=SID, summary_key="k",
            evidence_digest="e" * 64,
            payload={"draft_id": draft_id, "markdown": "m", "sections": []},
            created_ms=created,
            polished=polished if polished else None)

    def test_stubs_only_ordering_and_flags(self):
        self._save("dwl_old", 1000)
        self._save("dwl_new", 3000, polished=True)
        self._save("dwl_mid", 2000)
        annotations.mark_draft_saved(self.conn, "dwl_new", "journal",
                                     now_ms=4000)
        rows = annotations.list_drafts_for_session(self.conn, SID)
        self.assertEqual([r["draft_id"] for r in rows],
                         ["dwl_new", "dwl_mid", "dwl_old"])
        for row in rows:
            self.assertNotIn("json", row.keys())
            self.assertNotIn("draft", row.keys())
        newest = rows[0]
        self.assertEqual(newest["saved_journal_ms"], 4000)
        self.assertIsNone(newest["saved_page_ms"])
        self.assertTrue(newest["polished"])
        self.assertFalse(rows[1]["polished"])
        limited = annotations.list_drafts_for_session(
            self.conn, SID, limit=2)
        self.assertEqual(len(limited), 2)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.list_drafts_for_session(self.conn, SID, limit=0)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.list_drafts_for_session(self.conn, SID, limit=21)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.list_drafts_for_session(self.conn, "bad")


class SessionLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.conn = annotations.connect(str(self.base / "annotations.db"))

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.temp.cleanup()

    def test_add_list_remove_roundtrip(self):
        self.assertEqual(annotations.list_session_links(self.conn, SID), [])
        self.assertTrue(annotations.add_session_link(
            self.conn, session_id=SID, kind="draft",
            target="dwl_1", now_ms=1000))
        self.assertTrue(annotations.add_session_link(
            self.conn, session_id=SID, kind="capture",
            target="3", now_ms=1000))
        rows = annotations.list_session_links(self.conn, SID)
        self.assertEqual([(r["kind"], r["target"]) for r in rows],
                         [("capture", "3"), ("draft", "dwl_1")])
        self.assertTrue(annotations.remove_session_link(
            self.conn, session_id=SID, kind="draft", target="dwl_1"))
        rows = annotations.list_session_links(self.conn, SID)
        self.assertEqual(len(rows), 1)

    def test_duplicate_add_false_single_row(self):
        self.assertTrue(annotations.add_session_link(
            self.conn, session_id=SID, kind="draft",
            target="dwl_1", now_ms=1000))
        self.assertFalse(annotations.add_session_link(
            self.conn, session_id=SID, kind="draft",
            target="dwl_1", now_ms=2000))
        rows = annotations.list_session_links(self.conn, SID)
        self.assertEqual(len(rows), 1)

    def test_remove_missing_false(self):
        self.assertFalse(annotations.remove_session_link(
            self.conn, session_id=SID, kind="draft", target="nope"))

    def test_invalid_inputs(self):
        with self.assertRaises(annotations.AnnotationsError):
            annotations.add_session_link(
                self.conn, session_id="bad", kind="draft",
                target="t", now_ms=1000)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.add_session_link(
                self.conn, session_id=SID, kind="bogus",
                target="t", now_ms=1000)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.add_session_link(
                self.conn, session_id=SID, kind="draft",
                target="   ", now_ms=1000)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.add_session_link(
                self.conn, session_id=SID, kind="draft",
                target="t" * 513, now_ms=1000)
        with self.assertRaises(annotations.AnnotationsError):
            annotations.remove_session_link(
                self.conn, session_id=SID, kind="bogus", target="t")
        with self.assertRaises(annotations.AnnotationsError):
            annotations.list_session_links(self.conn, "bad")


class SessionLinksBulkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.conn = annotations.connect(str(self.base / "annotations.db"))

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.temp.cleanup()

    def _sid(self, n):
        return "%032x" % n

    def test_grouped_map(self):
        s1 = self._sid(1)
        s2 = self._sid(2)
        annotations.add_session_link(
            self.conn, session_id=s1, kind="continued_from",
            target=self._sid(9), now_ms=1000)
        annotations.add_session_link(
            self.conn, session_id=s1, kind="draft",
            target="dwl_1", now_ms=1000)
        annotations.add_session_link(
            self.conn, session_id=s2, kind="continued_from",
            target=self._sid(9), now_ms=1000)
        out = annotations.list_session_links_bulk(self.conn, [s1, s2])
        self.assertEqual(set(out.keys()), {s1, s2})
        self.assertEqual(len(out[s1]), 2)
        self.assertEqual(len(out[s2]), 1)

    def test_kind_filter(self):
        s1 = self._sid(1)
        annotations.add_session_link(
            self.conn, session_id=s1, kind="continued_from",
            target=self._sid(9), now_ms=1000)
        annotations.add_session_link(
            self.conn, session_id=s1, kind="draft",
            target="dwl_1", now_ms=1000)
        out = annotations.list_session_links_bulk(
            self.conn, [s1], kind="continued_from")
        self.assertEqual(set(out.keys()), {s1})
        self.assertEqual(len(out[s1]), 1)
        self.assertEqual(out[s1][0]["kind"], "continued_from")
        empty = annotations.list_session_links_bulk(
            self.conn, [s1], kind="capture")
        self.assertEqual(empty, {})

    def test_empty_list(self):
        self.assertEqual(annotations.list_session_links_bulk(self.conn, []), {})

    def test_over_limit(self):
        ids = [self._sid(i + 1) for i in range(1001)]
        with self.assertRaisesRegex(annotations.AnnotationsError,
                                    "session list is too large"):
            annotations.list_session_links_bulk(self.conn, ids)

    def test_invalid_ids(self):
        with self.assertRaisesRegex(annotations.AnnotationsError,
                                    "session list is invalid"):
            annotations.list_session_links_bulk(self.conn, ["bad"])
        with self.assertRaisesRegex(annotations.AnnotationsError,
                                    "session list is invalid"):
            annotations.list_session_links_bulk(self.conn, "not-a-list")
        with self.assertRaisesRegex(annotations.AnnotationsError,
                                    "session list is invalid"):
            annotations.list_session_links_bulk(
                self.conn, [self._sid(1), None])

    def test_unknown_absent_and_dedupe(self):
        s1 = self._sid(1)
        missing = self._sid(99)
        annotations.add_session_link(
            self.conn, session_id=s1, kind="draft",
            target="dwl_1", now_ms=1000)
        out = annotations.list_session_links_bulk(self.conn, [s1, missing])
        self.assertEqual(set(out.keys()), {s1})
        dup = annotations.list_session_links_bulk(self.conn, [s1, s1])
        self.assertEqual(set(dup.keys()), {s1})

    def test_ordering(self):
        s1 = self._sid(1)
        s2 = self._sid(2)
        # Insert out of order; result must be ORDER BY session, kind, target.
        annotations.add_session_link(
            self.conn, session_id=s2, kind="draft",
            target="z-target", now_ms=1000)
        annotations.add_session_link(
            self.conn, session_id=s1, kind="draft",
            target="b-target", now_ms=1000)
        annotations.add_session_link(
            self.conn, session_id=s1, kind="capture",
            target="a-target", now_ms=1000)
        annotations.add_session_link(
            self.conn, session_id=s1, kind="draft",
            target="a-target", now_ms=1000)
        out = annotations.list_session_links_bulk(self.conn, [s2, s1])
        self.assertEqual(
            [(r["kind"], r["target"]) for r in out[s1]],
            [("capture", "a-target"), ("draft", "a-target"),
             ("draft", "b-target")])
        self.assertEqual(len(out[s2]), 1)

    def test_invalid_kind(self):
        with self.assertRaises(annotations.AnnotationsError):
            annotations.list_session_links_bulk(
                self.conn, [self._sid(1)], kind="bogus")




def _build_legacy_db(path):
    """Create a pre-Phase-2a sidecar via raw SQL (legacy tables + columns)."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript("""
CREATE TABLE session_labels(session_id TEXT PRIMARY KEY,
  evidence_digest TEXT NOT NULL, model TEXT NOT NULL,
  created_ms INTEGER NOT NULL, raw_json TEXT NOT NULL);
INSERT INTO session_labels VALUES('a','d','m',1,'{}');
CREATE TABLE unit_labels(anchor TEXT PRIMARY KEY,
  member_digest TEXT NOT NULL, evidence_digest TEXT NOT NULL,
  model TEXT NOT NULL, created_ms INTEGER NOT NULL,
  raw_json TEXT NOT NULL);
INSERT INTO unit_labels VALUES('x','m','e','m',1,'{}');
CREATE TABLE project_overrides(session_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL, status TEXT NOT NULL,
  created_ms INTEGER NOT NULL);
INSERT INTO project_overrides VALUES('a','p','confirmed',1);
CREATE TABLE jev_calls(id INTEGER PRIMARY KEY AUTOINCREMENT,
  job TEXT NOT NULL, created_ms INTEGER NOT NULL, cost REAL);
INSERT INTO jev_calls(job, created_ms, cost) VALUES('j',1,0.1);
CREATE TABLE focus_blocks(id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_ms INTEGER NOT NULL, ended_ms INTEGER, title TEXT NOT NULL,
  project_id TEXT, task_ref TEXT, planned_ms INTEGER,
  state TEXT NOT NULL, revision TEXT NOT NULL,
  created_ms INTEGER NOT NULL);
INSERT INTO focus_blocks(started_ms, title, state, revision, created_ms)
  VALUES(1,'t','open','r',1);
CREATE TABLE session_meta(session_id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  intent TEXT NOT NULL DEFAULT '',
  outcome TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '[]',
  next_step TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'new',
  project_override TEXT NOT NULL DEFAULT '',
  filed_ref TEXT NOT NULL DEFAULT '',
  ignore_reason TEXT NOT NULL DEFAULT '',
  revision TEXT NOT NULL,
  created_ms INTEGER NOT NULL,
  updated_ms INTEGER NOT NULL);
INSERT INTO session_meta VALUES(
  'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','','Old title','i','o','[]','n',
  'new','','','', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',1000,2000);
CREATE TABLE captures(id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL, session_file TEXT NOT NULL,
  message_ref TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
  text_hash TEXT NOT NULL, actionable REAL, durable REAL,
  status TEXT NOT NULL DEFAULT 'new', created_ms INTEGER NOT NULL,
  applied_ms INTEGER, applied_target TEXT,
  UNIQUE(session_file, message_ref, text_hash));
INSERT INTO captures(project_id, session_file, message_ref, kind, text,
  text_hash, status, created_ms)
  VALUES('11111111-1111-4111-8111-111111111111','f','m','todo',
  'Do thing','h','new',1500);
""")
        conn.commit()
    finally:
        conn.close()
    os.chmod(path, 0o600)


def _table_names(path):
    conn = sqlite3.connect(str(path))
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        conn.close()


def _column_names(path, table):
    conn = sqlite3.connect(str(path))
    try:
        return [r[1] for r in conn.execute(
            f"PRAGMA table_info({table})").fetchall()]
    finally:
        conn.close()


class PruneLegacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.db = str(self.base / "annotations.db")
        _build_legacy_db(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_prune_drops_legacy_and_rebuilds_meta(self):
        out = annotations.prune_legacy(self.db, now_ms=12345)
        self.assertTrue(out["ok"])
        self.assertEqual(out["backup"], self.db + ".bak-12345")
        self.assertEqual(out["dropped_tables"],
                         ["focus_blocks", "jev_calls", "project_overrides",
                          "session_labels", "unit_labels"])
        self.assertTrue(out["pruned_session_meta"])
        live = _table_names(self.db)
        for gone in ("session_labels", "unit_labels", "project_overrides",
                     "jev_calls", "focus_blocks"):
            self.assertNotIn(gone, live)
        self.assertEqual(set(_column_names(self.db, "session_meta")),
                         {"session_id", "device_id", "revision",
                          "created_ms", "updated_ms", "thought_ref",
                          "todo_refs", "refs", "attended"})
        # Surviving rows are preserved (meta core values + captures).
        conn = annotations.connect(self.db)
        try:
            meta = annotations.get_session_meta(
                conn, "a" * 32)
            self.assertEqual(meta["revision"], "b" * 32)
            self.assertEqual(meta["created_ms"], 1000)
            self.assertEqual(meta["updated_ms"], 2000)
            # Phase-2c columns default on a pre-2c table.
            self.assertEqual(meta["thought_ref"], "")
            self.assertEqual(meta["todo_refs"], [])
            self.assertEqual(meta["refs"], [])
            self.assertEqual(meta["attended"], 0)
            rows = conn.execute(
                "SELECT COUNT(*) AS n FROM captures").fetchone()
            self.assertEqual(int(rows["n"]), 1)
        finally:
            conn.close()

    def test_prune_backup_first_holds_pre_prune_content(self):
        out = annotations.prune_legacy(self.db, now_ms=12345)
        backup = out["backup"]
        self.assertTrue(Path(backup).is_file())
        names = _table_names(backup)
        for kept in ("session_labels", "unit_labels", "project_overrides",
                     "jev_calls", "focus_blocks"):
            self.assertIn(kept, names)
        cols = _column_names(backup, "session_meta")
        for legacy in ("title", "intent", "outcome", "tags", "next_step",
                       "state", "project_override", "filed_ref",
                       "ignore_reason"):
            self.assertIn(legacy, cols)

    def test_prune_aborts_before_any_write_when_backup_exists(self):
        wilted = self.db + ".bak-12345"
        Path(wilted).write_text("placeholder", encoding="utf-8")
        with self.assertRaisesRegex(annotations.AnnotationsError,
                                    "backup already exists"):
            annotations.prune_legacy(self.db, now_ms=12345)
        # Nothing was dropped: the abort precedes every write.
        live = _table_names(self.db)
        for kept in ("session_labels", "unit_labels", "project_overrides",
                     "jev_calls", "focus_blocks"):
            self.assertIn(kept, live)
        self.assertIn("title", _column_names(self.db, "session_meta"))

    def test_prune_is_idempotent(self):
        first = annotations.prune_legacy(self.db, now_ms=12345)
        self.assertTrue(first["pruned_session_meta"])
        second = annotations.prune_legacy(self.db, now_ms=12346)
        self.assertEqual(second["dropped_tables"], [])
        self.assertFalse(second["pruned_session_meta"])

    def test_prune_missing_db_fails_closed(self):
        with self.assertRaisesRegex(annotations.AnnotationsError,
                                    "database not found"):
            annotations.prune_legacy(str(self.base / "missing.db"),
                                     now_ms=12345)

    def test_prune_cli_roundtrip_and_single_line_errors(self):
        proc = run_cli(["--db", self.db, "prune-legacy"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        value = json.loads(proc.stdout)
        self.assertTrue(value.get("ok"))
        self.assertTrue(str(value.get("backup", "")).startswith(self.db))
        missing = run_cli(["--db", str(self.base / "missing.db"),
                           "prune-legacy"])
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("error:", missing.stderr)
        self.assertNotIn("Traceback", missing.stderr)
        self.assertEqual(missing.stdout, "")

    def test_prune_never_runs_automatically(self):
        """No QML ladder, scheduler, or helper may invoke prune-legacy."""
        repo = Path(__file__).parents[1]
        offenders = []
        for path in repo.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in (".qml", ".js", ".ts", ".py", ".qmltypes"):
                continue
            if path.name in ("annotations.py", "test_annotations.py"):
                continue
            if ".git" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if "prune-legacy" in text or "prune_legacy" in text:
                offenders.append(str(path.relative_to(repo)))
        self.assertEqual(offenders, [])


class SessionMetaPhase2cTests(unittest.TestCase):
    """Phase-2c session_meta: thought_ref/todo_refs/refs/attended."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.conn = annotations.connect(str(self.base / "annotations.db"))

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.temp.cleanup()

    def test_fresh_schema_has_nine_columns(self):
        cols = [r[1] for r in self.conn.execute(
            "PRAGMA table_info(session_meta)").fetchall()]
        self.assertEqual(set(cols), set(
            annotations.REDUCED_SESSION_META_COLUMNS))
        self.assertIn("content_index", annotations.EXPECTED_TABLES)

    def test_insert_defaults_phase2c_fields(self):
        row = annotations.upsert_session_meta(
            self.conn, session_id=SID, now_ms=1000)
        self.assertEqual(row["thought_ref"], "")
        self.assertEqual(row["todo_refs"], [])
        self.assertEqual(row["refs"], [])
        self.assertEqual(row["attended"], 0)

    def test_insert_with_phase2c_fields_roundtrip(self):
        row = annotations.upsert_session_meta(
            self.conn, session_id=SID, now_ms=1000,
            thought_ref="journal:2026_09_21.md:41",
            todo_refs=["Demo.md:12", "Demo.md:13"],
            refs=["file:///tmp/x.py"],
            attended=True)
        self.assertEqual(row["thought_ref"], "journal:2026_09_21.md:41")
        self.assertEqual(row["todo_refs"], ["Demo.md:12", "Demo.md:13"])
        self.assertEqual(row["refs"], ["file:///tmp/x.py"])
        self.assertEqual(row["attended"], 1)
        fetched = annotations.get_session_meta(self.conn, SID)
        assert fetched is not None
        self.assertEqual(fetched, row)

    def test_update_only_provided_fields_and_rotates_revision(self):
        row = annotations.upsert_session_meta(
            self.conn, session_id=SID, now_ms=1000,
            thought_ref="journal:2026_09_21.md:41",
            attended=False)
        updated = annotations.upsert_session_meta(
            self.conn, session_id=SID, now_ms=2000,
            expect_revision=row["revision"], attended=1)
        self.assertEqual(updated["attended"], 1)
        # Unmentioned fields survive the CAS update.
        self.assertEqual(updated["thought_ref"], "journal:2026_09_21.md:41")
        self.assertEqual(updated["todo_refs"], [])
        self.assertNotEqual(updated["revision"], row["revision"])
        self.assertEqual(updated["created_ms"], 1000)
        self.assertEqual(updated["updated_ms"], 2000)

    def test_cas_still_guards_phase2c_writes(self):
        row = annotations.upsert_session_meta(
            self.conn, session_id=SID, now_ms=1000)
        with self.assertRaisesRegex(annotations.AnnotationsError,
                                    "session meta changed"):
            annotations.upsert_session_meta(
                self.conn, session_id=SID, now_ms=2000,
                attended=True)
        updated = annotations.upsert_session_meta(
            self.conn, session_id=SID, now_ms=2000,
            expect_revision=row["revision"], attended=True)
        self.assertEqual(updated["attended"], 1)
        # A stale revision now fails even with valid new fields.
        with self.assertRaisesRegex(annotations.AnnotationsError,
                                    "session meta changed"):
            annotations.upsert_session_meta(
                self.conn, session_id=SID, now_ms=3000,
                expect_revision=row["revision"],
                thought_ref="journal:2026_09_21.md:7")

    def test_thought_ref_validation(self):
        for bad in ("nope", "journal:x", "journal:2026_09_21.md:0",
                    "journal:2026_09_21.md:", "page:2026_09_21.md:3",
                    "journal:2026_09_21.md:3x", 123, True, ["x"]):
            with self.subTest(value=repr(bad)[:40]):
                with self.assertRaises(annotations.AnnotationsError):
                    annotations.upsert_session_meta(
                        self.conn, session_id=SID, now_ms=1000,
                        thought_ref=bad)
        # Thought text is never stored — only the ref shape passes.
        with self.assertRaises(annotations.AnnotationsError):
            annotations.upsert_session_meta(
                self.conn, session_id=SID, now_ms=1000,
                thought_ref="some long free text about the session")

    def test_ref_list_validation(self):
        row = annotations.upsert_session_meta(
            self.conn, session_id=SID, now_ms=1000,
            todo_refs='["a.md:1","b.md:2"]', refs=[])
        self.assertEqual(row["todo_refs"], ["a.md:1", "b.md:2"])
        self.assertEqual(row["refs"], [])
        for bad in ("not-json", '{"a":1}', "[1,2]", [""], ["  "],
                    ["ok", 7], ["x" * 513], "  ", ["a\x00b"], 42,
                    ["x"] * 1001):
            with self.subTest(value=repr(bad)[:40]):
                with self.assertRaises(annotations.AnnotationsError):
                    annotations.upsert_session_meta(
                        self.conn, session_id=SID2, now_ms=1000,
                        expect_revision=None, todo_refs=bad)
                with self.assertRaises(annotations.AnnotationsError):
                    annotations.upsert_session_meta(
                        self.conn, session_id=SID2, now_ms=1000,
                        refs=bad)

    def test_attended_validation(self):
        revision = None
        for good, want in ((True, 1), (False, 0), (1, 1), (0, 0)):
            row = annotations.upsert_session_meta(
                self.conn, session_id=SID, now_ms=1000, attended=good,
                expect_revision=revision)
            self.assertEqual(row["attended"], want)
            fetched = annotations.get_session_meta(self.conn, SID)
            assert fetched is not None
            self.assertEqual(fetched["attended"], want)
            revision = row["revision"]
        for bad in ("yes", 2, -1, None.__class__, 1.5):
            with self.subTest(value=repr(bad)[:20]):
                with self.assertRaises(annotations.AnnotationsError):
                    annotations._validate_attended(bad)

    def test_bulk_map_carries_phase2c_fields(self):
        annotations.upsert_session_meta(
            self.conn, session_id=SID, now_ms=1000,
            thought_ref="journal:2026_09_21.md:9", attended=True)
        out = annotations.list_session_meta(self.conn, [SID, SID2])
        self.assertEqual(set(out.keys()), {SID})
        self.assertEqual(out[SID]["thought_ref"],
                         "journal:2026_09_21.md:9")
        self.assertEqual(out[SID]["attended"], 1)

    def test_old_sidecar_reads_default_new_fields(self):
        """A pre-2c session_meta (5 cols) still reads via get/list."""
        raw = sqlite3.connect(str(self.base / "legacy5.db"))
        raw.row_factory = sqlite3.Row
        try:
            raw.execute(
                "CREATE TABLE session_meta(session_id TEXT PRIMARY KEY,"
                " device_id TEXT NOT NULL DEFAULT '',"
                " revision TEXT NOT NULL,"
                " created_ms INTEGER NOT NULL,"
                " updated_ms INTEGER NOT NULL)")
            raw.execute(
                "INSERT INTO session_meta VALUES(?, '', ?, 100, 200)",
                (SID, "c" * 32))
            raw.commit()
            row = raw.execute(
                "SELECT session_id FROM session_meta").fetchone()
            self.assertIsNotNone(row)
            fetched = annotations.get_session_meta(raw, SID)
            assert fetched is not None
            self.assertEqual(fetched["revision"], "c" * 32)
            self.assertEqual(fetched["thought_ref"], "")
            self.assertEqual(fetched["todo_refs"], [])
            self.assertEqual(fetched["refs"], [])
            self.assertEqual(fetched["attended"], 0)
            bulk = annotations.list_session_meta(raw, [SID])
            self.assertEqual(bulk[SID]["attended"], 0)
        finally:
            raw.close()

    def test_writer_migration_adds_columns_to_old_sidecar(self):
        raw = sqlite3.connect(str(self.base / "migrate.db"))
        try:
            raw.execute(
                "CREATE TABLE session_meta(session_id TEXT PRIMARY KEY,"
                " device_id TEXT NOT NULL DEFAULT '',"
                " revision TEXT NOT NULL,"
                " created_ms INTEGER NOT NULL,"
                " updated_ms INTEGER NOT NULL)")
            raw.commit()
        finally:
            raw.close()
        import os as _os
        _os.chmod(str(self.base / "migrate.db"), 0o600)
        conn = annotations.connect(str(self.base / "migrate.db"))
        try:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(session_meta)").fetchall()}
            for expected in ("thought_ref", "todo_refs", "refs",
                             "attended"):
                self.assertIn(expected, cols)
            row = annotations.upsert_session_meta(
                conn, session_id=SID, now_ms=5, attended=True)
            self.assertEqual(row["attended"], 1)
        finally:
            conn.close()

    def test_legacy_content_fields_still_rejected(self):
        for field in ("title", "intent", "outcome", "tags", "next_step",
                      "state", "filed_ref", "ignore_reason",
                      "project_override"):
            with self.subTest(field=field):
                with self.assertRaises(TypeError):
                    annotations.upsert_session_meta(
                        self.conn, session_id=SID, now_ms=1000,
                        **{field: "x"})


class PruneLegacyPhase2cTests(unittest.TestCase):
    """Prune keeps the new columns intact (additive, §8.1)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.db = str(self.base / "annotations.db")
        _build_legacy_db(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def _cols(self):
        conn = sqlite3.connect(self.db)
        try:
            return [r[1] for r in conn.execute(
                "PRAGMA table_info(session_meta)").fetchall()]
        finally:
            conn.close()

    def test_prune_leaves_new_columns_intact(self):
        # A 2c-shaped sidecar prunes legacy tables but keeps meta whole.
        conn = annotations.connect(self.db)
        try:
            row = annotations.upsert_session_meta(
                conn, session_id=SID2, now_ms=1000,
                thought_ref="journal:2026_09_21.md:3",
                todo_refs=["p.md:1"], refs=["r"], attended=True)
        finally:
            conn.close()
        out = annotations.prune_legacy(self.db, now_ms=99999)
        self.assertTrue(out["ok"])
        self.assertEqual(set(self._cols()),
                         set(annotations.REDUCED_SESSION_META_COLUMNS))
        conn = annotations.connect(self.db)
        try:
            fetched = annotations.get_session_meta(conn, SID2)
            assert fetched is not None
            self.assertEqual(fetched["thought_ref"],
                             "journal:2026_09_21.md:3")
            self.assertEqual(fetched["todo_refs"], ["p.md:1"])
            self.assertEqual(fetched["refs"], ["r"])
            self.assertEqual(fetched["attended"], 1)
            # The legacy fixture row survived with defaulted 2c fields.
            legacy = annotations.get_session_meta(conn, "a" * 32)
            assert legacy is not None
            self.assertEqual(legacy["thought_ref"], "")
            self.assertEqual(legacy["attended"], 0)
        finally:
            conn.close()
        # Second prune is a no-op on the 2c shape.
        again = annotations.prune_legacy(self.db, now_ms=100000)
        self.assertFalse(again["pruned_session_meta"])
