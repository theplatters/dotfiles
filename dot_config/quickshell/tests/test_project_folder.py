import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import project_files
import project_folder
from logseq_common import GraphError


def make_registry(tmp: Path, name="Demo", folder: Path | None = None,
                  logseq_path=""):
    pid = str(uuid.uuid4())
    lines = ["version = 1", "", "[[projects]]", f'id = "{pid}"',
             f'name = "{name}"']
    if logseq_path:
        lines.append(f'logseq_path = "{logseq_path}"')
    if folder is not None:
        lines.append(f'local_folder = "{folder}"')
    lines.append("")
    reg = tmp / f"projects-{pid[:8]}.toml"
    reg.write_text("\n".join(lines), encoding="utf-8")
    return reg, pid


class ProjectFolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.folder = self.base / "linked"
        self.folder.mkdir()
        self.reg, self.pid = make_registry(self.base, folder=self.folder)
        # Isolate registry resolution to this file.
        self.old_env = os.environ.get("QUICKSHELL_PROJECTS_FILE")
        os.environ["QUICKSHELL_PROJECTS_FILE"] = str(self.reg)
        # Folder-only: no LOGSEQ_GRAPH must be needed.
        self.old_graph = os.environ.pop("LOGSEQ_GRAPH", None)

    def tearDown(self):
        if self.old_env is None:
            os.environ.pop("QUICKSHELL_PROJECTS_FILE", None)
        else:
            os.environ["QUICKSHELL_PROJECTS_FILE"] = self.old_env
        if self.old_graph is not None:
            os.environ["LOGSEQ_GRAPH"] = self.old_graph
        self.temp.cleanup()

    def test_list_empty_folder_only(self):
        listed = project_folder.list_folder({"project_id": self.pid})
        self.assertEqual(listed["entries"], [])
        self.assertEqual(listed["project_id"], self.pid)

    def test_create_update_with_explicit_semantics(self):
        created = project_folder.write_folder(
            {"project_id": self.pid, "file": "hello.txt",
             "content": "hi\n", "create": True})
        self.assertTrue(created["created"])
        self.assertEqual((self.folder / "hello.txt").read_text(), "hi\n")
        read = project_folder.read_folder(
            {"project_id": self.pid, "file": "hello.txt"})
        self.assertEqual(read["content"], "hi\n")
        self.assertEqual(read["revision"],
                         hashlib.sha256(b"hi\n").hexdigest())
        # Overwrite with fresh revision preserves content.
        updated = project_folder.write_folder(
            {"project_id": self.pid, "file": "hello.txt",
             "content": "hi2\n", "revision": read["revision"]})
        self.assertFalse(updated["created"])
        self.assertEqual((self.folder / "hello.txt").read_text(), "hi2\n")
        # Stale revision never writes.
        with self.assertRaises(GraphError) as stale:
            project_folder.write_folder(
                {"project_id": self.pid, "file": "hello.txt",
                 "content": "stale\n", "revision": read["revision"]})
        self.assertIn("stale", str(stale.exception))
        self.assertEqual((self.folder / "hello.txt").read_text(), "hi2\n")
        # Creation without explicit flag is rejected.
        with self.assertRaises(GraphError):
            project_folder.write_folder(
                {"project_id": self.pid, "file": "new.txt",
                 "content": "x\n"})
        self.assertFalse((self.folder / "new.txt").exists())
        # Creation over an existing file is rejected.
        with self.assertRaises(GraphError):
            project_folder.write_folder(
                {"project_id": self.pid, "file": "hello.txt",
                 "content": "x\n", "create": True})
        # Update without revision is rejected.
        with self.assertRaises(GraphError):
            project_folder.write_folder(
                {"project_id": self.pid, "file": "hello.txt",
                 "content": "x\n"})
        # Revision must be empty when creating.
        with self.assertRaises(GraphError):
            project_folder.write_folder(
                {"project_id": self.pid, "file": "brand.txt",
                 "content": "x\n", "revision": "0" * 64, "create": True})

    def test_subdir_parent_must_exist(self):
        with self.assertRaises(GraphError):
            project_folder.write_folder(
                {"project_id": self.pid, "file": "nodir/a.txt",
                 "content": "x\n", "create": True})
        (self.folder / "sub").mkdir()
        created = project_folder.write_folder(
            {"project_id": self.pid, "file": "sub/a.txt",
             "content": "x\n", "create": True})
        self.assertTrue(created["created"])

    def test_traversal_symlink_sensitive_git_protected_rejected(self):
        (self.folder / "ok.txt").write_text("ok\n", encoding="utf-8")
        (self.folder / ".env").write_text("SECRET=1\n", encoding="utf-8")
        outside = self.base / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        (self.folder / "sub").mkdir(exist_ok=True)
        (self.folder / "sub" / "evil").symlink_to(outside)
        (self.folder / "link.txt").symlink_to(self.folder / "ok.txt")
        for bad in ("../outside.txt", "/etc/hostname", "sub/evil",
                    "link.txt", ".env", ".ssh/id_rsa", ".git/config",
                    "ScopedAgent.qml"):
            with self.subTest(path=bad):
                with self.assertRaises(GraphError):
                    project_folder.read_folder(
                        {"project_id": self.pid, "file": bad})
                with self.assertRaises(GraphError):
                    project_folder.write_folder(
                        {"project_id": self.pid, "file": bad,
                         "content": "x\n", "create": True})
        listing = project_folder.list_folder({"project_id": self.pid})
        paths = [e["path"] for e in listing["entries"]]
        self.assertIn("ok.txt", paths)
        self.assertNotIn(".env", paths)
        self.assertNotIn("sub/evil", paths)
        self.assertNotIn("link.txt", paths)

    def test_mid_path_symlink_component_rejected(self):
        real = self.folder / "real"
        real.mkdir()
        (real / "secret.txt").write_text("s\n", encoding="utf-8")
        (self.folder / "gate").mkdir()
        (self.folder / "gate" / "redir").symlink_to(
            real, target_is_directory=True)
        with self.assertRaises(GraphError):
            project_folder.read_folder(
                {"project_id": self.pid, "file": "gate/redir/secret.txt"})
        with self.assertRaises(GraphError):
            project_folder.write_folder(
                {"project_id": self.pid, "file": "gate/redir/secret.txt",
                 "content": "x\n", "create": True})

    def test_size_and_encoding_bounds(self):
        big = "x" * (project_folder.WRITE_LIMIT + 1)
        with self.assertRaises(GraphError):
            project_folder.write_folder(
                {"project_id": self.pid, "file": "big.txt",
                 "content": big, "create": True})
        self.assertFalse((self.folder / "big.txt").exists())
        with self.assertRaises(GraphError):
            project_folder.write_folder(
                {"project_id": self.pid, "file": "nul.txt",
                 "content": "a\x00b", "create": True})
        (self.folder / "bin.txt").write_bytes(b"\xff\xfe")
        with self.assertRaises(GraphError):
            project_folder.read_folder(
                {"project_id": self.pid, "file": "bin.txt"})
        (self.folder / "oversize.txt").write_bytes(
            b"x" * (project_folder.READ_LIMIT + 1))
        with self.assertRaises(GraphError):
            project_folder.read_folder(
                {"project_id": self.pid, "file": "oversize.txt"})

    def test_permissions_preserved_and_new_file_mode(self):
        (self.folder / "m.txt").write_text("a\n", encoding="utf-8")
        os.chmod(self.folder / "m.txt", 0o640)
        rev = project_folder.read_folder(
            {"project_id": self.pid, "file": "m.txt"})["revision"]
        project_folder.write_folder(
            {"project_id": self.pid, "file": "m.txt",
             "content": "b\n", "revision": rev})
        self.assertEqual((self.folder / "m.txt").stat().st_mode & 0o777, 0o640)

    def test_legacy_path_resolves_via_registry_only(self):
        reg2, pid2 = make_registry(
            self.base, name="Legacy", folder=self.folder,
            logseq_path="pages/Legacy.md")
        os.environ["QUICKSHELL_PROJECTS_FILE"] = str(reg2)
        listed = project_folder.list_folder({"path": "pages/Legacy.md"})
        self.assertEqual(listed["path"], "pages/Legacy.md")
        created = project_folder.write_folder(
            {"path": "pages/Legacy.md", "file": "legacy.txt",
             "content": "L\n", "create": True})
        self.assertTrue(created["created"])
        # Unregistered legacy page has no folder (no file:: fallback).
        with self.assertRaises(GraphError) as missing:
            project_folder.list_folder({"path": "pages/Unknown.md"})
        self.assertIn("no linked folder", str(missing.exception))

    def test_missing_folder_and_unknown_id_fail_closed(self):
        reg3, _ = make_registry(self.base, name="NoFolder")
        os.environ["QUICKSHELL_PROJECTS_FILE"] = str(reg3)
        # Re-read the fresh registry id for the folder-less entry.
        import projects as _projects
        pid_nofolder = [e for e in _projects.list_projects(
            str(reg3))["projects"] if e["name"] == "NoFolder"][0]["id"]
        with self.assertRaises(GraphError) as nofolder:
            project_folder.list_folder({"project_id": pid_nofolder})
        self.assertIn("no linked folder", str(nofolder.exception))
        with self.assertRaises(GraphError):
            project_folder.list_folder({"project_id": str(uuid.uuid4())})
        with self.assertRaises(GraphError):
            project_folder.list_folder({})

    def test_cli_list_read_write(self):
        helper = ROOT / "scripts" / "project_folder.py"
        env = dict(os.environ, QUICKSHELL_PROJECTS_FILE=str(self.reg))
        listed = subprocess.run(
            [sys.executable, str(helper), "list"],
            input=json.dumps({"project_id": self.pid}), text=True,
            capture_output=True, timeout=5, env=env)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("entries", listed.stdout)
        created = subprocess.run(
            [sys.executable, str(helper), "write"],
            input=json.dumps({"project_id": self.pid, "file": "cli.txt",
                              "content": "C\n", "create": True}),
            text=True, capture_output=True, timeout=5, env=env)
        self.assertEqual(created.returncode, 0, created.stderr)
        read = subprocess.run(
            [sys.executable, str(helper), "read"],
            input=json.dumps({"project_id": self.pid, "file": "cli.txt"}),
            text=True, capture_output=True, timeout=5, env=env)
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertEqual(json.loads(read.stdout)["content"], "C\n")
        rev = json.loads(read.stdout)["revision"]
        denied = subprocess.run(
            [sys.executable, str(helper), "write"],
            input=json.dumps({"project_id": self.pid, "file": "cli.txt",
                              "content": "stale\n", "revision": "0" * 64}),
            text=True, capture_output=True, timeout=5, env=env)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("stale", denied.stderr)
        traversal = subprocess.run(
            [sys.executable, str(helper), "read"],
            input=json.dumps({"project_id": self.pid, "file": "../x"}),
            text=True, capture_output=True, timeout=5, env=env)
        self.assertNotEqual(traversal.returncode, 0)
        # Stale revision from a fresh read still succeeds.
        ok = subprocess.run(
            [sys.executable, str(helper), "write"],
            input=json.dumps({"project_id": self.pid, "file": "cli.txt",
                              "content": "C2\n", "revision": rev}),
            text=True, capture_output=True, timeout=5, env=env)
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_trusted_helpers_are_excluded(self):
        root = Path("/repo")
        for name in ("projects.py", "zotero.py", "desktop_projects.py",
                     "desktop_resume.py", "daily_agenda.py",
                     "palette_files.py", "project_overview.py",
                     "project_session_changes.py", "quickshell_settings.py",
                     "project_folder.py", "project_files.py"):
            with self.subTest(helper=name):
                self.assertTrue(
                    project_files.is_excluded(root, Path("scripts") / name),
                    name)
        # Lock/transaction files are never user data.
        self.assertTrue(project_files.is_excluded(
            root, Path(".project-folder.lock")))
        self.assertTrue(project_files.is_excluded(
            root, Path(".project-folder-1.tmp")))

    def test_active_registry_inside_root_is_blocked(self):
        reg_inside = self.folder / "custom.toml"
        pid = str(uuid.uuid4())
        reg_inside.write_text(
            f'version = 1\n\n[[projects]]\nid = "{pid}"\nname = "Self"\n'
            f'local_folder = "{self.folder}"\n', encoding="utf-8")
        old = os.environ.get("QUICKSHELL_PROJECTS_FILE")
        os.environ["QUICKSHELL_PROJECTS_FILE"] = str(reg_inside)
        try:
            resolved = self.folder.resolve()
            self.assertTrue(project_files.is_excluded(
                resolved, Path("custom.toml")))
            self.assertTrue(project_files.is_excluded(
                resolved, Path("custom.toml.lock")))
            # An unrelated projects.toml elsewhere is not the active path.
            self.assertFalse(project_files.is_excluded(
                Path("/tmp"), Path("projects.toml")))
            with self.assertRaises(GraphError):
                project_folder.read_folder(
                    {"project_id": pid, "file": "custom.toml"})
            with self.assertRaises(GraphError):
                project_folder.write_folder(
                    {"project_id": pid, "file": "custom.toml",
                     "content": "x\n", "create": True})
            (self.folder / "ok.txt").write_text("ok\n", encoding="utf-8")
            listing = project_folder.list_folder({"project_id": pid})
            self.assertNotIn("custom.toml",
                             [e["path"] for e in listing["entries"]])
            self.assertIn("ok.txt",
                          [e["path"] for e in listing["entries"]])
        finally:
            if old is None:
                os.environ.pop("QUICKSHELL_PROJECTS_FILE", None)
            else:
                os.environ["QUICKSHELL_PROJECTS_FILE"] = old

    def test_preflight_binding_and_registry_switch_rejected(self):
        (self.folder / "a.txt").write_text("hi\n", encoding="utf-8")
        bound = project_folder.preflight(
            {"project_id": self.pid, "file": "a.txt"})
        self.assertTrue(bound["exists"])
        self.assertEqual(bound["file"], "a.txt")
        self.assertIn("root_dev", bound)
        self.assertIn("root_ino", bound)
        # Identity travels as decimal strings (never JSON numbers).
        self.assertIsInstance(bound["root_dev"], str)
        self.assertIsInstance(bound["root_ino"], str)
        self.assertRegex(bound["root_dev"], r"^[0-9]{1,20}$")
        self.assertRegex(bound["root_ino"], r"^[0-9]{1,20}$")
        missing = project_folder.preflight(
            {"project_id": self.pid, "file": "missing.txt"})
        self.assertFalse(missing["exists"])
        self.assertEqual(missing["root"], bound["root"])
        # Protected targets never yield a valid create preflight.
        with self.assertRaises(GraphError):
            project_folder.preflight(
                {"project_id": self.pid, "file": ".env"})
        # Write with the inspected binding succeeds.
        ok = project_folder.write_folder(
            {"project_id": self.pid, "file": "a.txt", "content": "new\n",
             "revision": bound["revision"],
             "expected_root": bound["root"],
             "expected_root_dev": bound["root_dev"],
             "expected_root_ino": bound["root_ino"]})
        self.assertFalse(ok["created"])
        # Registry switch (fresh root differs) is rejected even with a
        # previously valid revision.
        other = self.base / "other"
        other.mkdir(exist_ok=True)
        reg2 = self.base / "switched.toml"
        reg2.write_text(
            f'version = 1\n\n[[projects]]\nid = "{self.pid}"\n'
            f'name = "Demo"\nlocal_folder = "{other}"\n', encoding="utf-8")
        old = os.environ.get("QUICKSHELL_PROJECTS_FILE")
        os.environ["QUICKSHELL_PROJECTS_FILE"] = str(reg2)
        try:
            with self.assertRaises(GraphError) as switched:
                project_folder.write_folder(
                    {"project_id": self.pid, "file": "a.txt",
                     "content": "evil\n", "revision": ok["revision"],
                     "expected_root": bound["root"],
                     "expected_root_dev": bound["root_dev"],
                     "expected_root_ino": bound["root_ino"]})
            self.assertIn("changed", str(switched.exception))
            self.assertFalse((other / "a.txt").exists())
        finally:
            if old is None:
                os.environ.pop("QUICKSHELL_PROJECTS_FILE", None)
            else:
                os.environ["QUICKSHELL_PROJECTS_FILE"] = old

    def test_concurrent_cooperating_writes_only_one_succeeds(self):
        # Barrier-synchronized start: both threads observe the same revision
        # before either writes. Outcome is deterministic (exactly one winner)
        # regardless of scheduling; the advisory lock plus revision recheck
        # serializes cooperating helpers.
        (self.folder / "race.txt").write_text("start\n", encoding="utf-8")
        revision = project_folder.read_folder(
            {"project_id": self.pid, "file": "race.txt"})["revision"]
        barrier = threading.Barrier(2)
        results: list = []

        def attempt(value: str):
            try:
                barrier.wait(timeout=5)
                results.append(project_folder.write_folder(
                    {"project_id": self.pid, "file": "race.txt",
                     "content": value, "revision": revision}))
            except GraphError as exc:
                results.append(exc)

        threads = [threading.Thread(target=attempt, args=("one\n",)),
                   threading.Thread(target=attempt, args=("two\n",))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(
            sum(isinstance(value, dict) for value in results), 1)
        self.assertEqual(
            sum(isinstance(value, GraphError) for value in results), 1)
        self.assertIn("stale", str(
            next(value for value in results
                 if isinstance(value, GraphError))))

    def test_deterministic_lock_contention_is_busy(self):
        # Fully deterministic interleaving: hold the per-root advisory lock
        # in the main thread, then a cooperating write must fail busy instead
        # of silently queueing forever. Short timeout keeps it fast.
        import project_folder as _pfmod
        (self.folder / "locked.txt").write_text("start\n", encoding="utf-8")
        revision = project_folder.read_folder(
            {"project_id": self.pid, "file": "locked.txt"})["revision"]
        resolved = self.folder.resolve()
        root_fd = project_files._open_root_fd(resolved)
        old_timeout = _pfmod.LOCK_TIMEOUT
        _pfmod.LOCK_TIMEOUT = 0.15
        outcome: list = []
        try:
            with _pfmod._FolderLock(root_fd):
                def attempt():
                    try:
                        outcome.append(project_folder.write_folder(
                            {"project_id": self.pid, "file": "locked.txt",
                             "content": "contended\n", "revision": revision}))
                    except GraphError as exc:
                        outcome.append(exc)
                thread = threading.Thread(target=attempt)
                thread.start()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
        finally:
            _pfmod.LOCK_TIMEOUT = old_timeout
            try:
                import os as _os
                _os.close(root_fd)
            except OSError:
                pass
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], GraphError)
        self.assertIn("busy", str(outcome[0]))
        # After release the same revision still wins.
        ok = project_folder.write_folder(
            {"project_id": self.pid, "file": "locked.txt",
             "content": "after\n", "revision": revision})
        self.assertFalse(ok["created"])

    def test_binding_large_identity_roundtrips_as_strings(self):
        # st_dev/st_ino routinely exceed JS MAX_SAFE_INTEGER (2**53-1):
        # numeric JSON transport would lose precision, so bindings are
        # decimal strings end-to-end with exact comparison.
        large_dev = "9007199254740993"  # 2**53 + 1
        large_ino = "18446744073709551615"  # max uint64
        self.assertGreater(int(large_dev), 2 ** 53)
        bound_root, bound_dev, bound_ino = project_folder._parse_binding({
            "expected_root": "/linked",
            "expected_root_dev": large_dev,
            "expected_root_ino": large_ino,
        })
        self.assertEqual((bound_root, bound_dev, bound_ino),
                         ("/linked", large_dev, large_ino))
        # JSON roundtrip preserves strings exactly (numbers would not).
        encoded = json.dumps({"root_dev": large_dev, "root_ino": large_ino})
        decoded = json.loads(encoded)
        self.assertEqual(decoded["root_dev"], large_dev)
        self.assertEqual(decoded["root_ino"], large_ino)
        # Numeric binding is rejected: extension must pass strings, never
        # Number().
        with self.assertRaises(GraphError):
            project_folder._parse_binding({
                "expected_root": "/linked",
                "expected_root_dev": 9007199254740993,
                "expected_root_ino": 22,
            })
        # Preflight itself returns strings on this machine.
        (self.folder / "bigid.txt").write_text("x\n", encoding="utf-8")
        pf = project_folder.preflight(
            {"project_id": self.pid, "file": "bigid.txt"})
        self.assertIsInstance(pf["root_dev"], str)
        self.assertIsInstance(pf["root_ino"], str)

    def test_module_shadow_creation_rejected_in_trusted_scripts_dir(self):
        trusted = (ROOT / "scripts").resolve()
        # Actual helper dir can never be a linked root.
        with self.assertRaises(GraphError):
            project_files._validate_root(trusted)
        # Nor can its descendants be listed/read/written via a repo-root link.
        repo_root = ROOT.resolve()
        for shadow in ("scripts/json.py", "scripts/hashlib.py",
                       "scripts/fcntl.py", "scripts/__init__.py",
                       "scripts/sub/__init__.py"):
            with self.subTest(shadow=shadow):
                self.assertTrue(
                    project_files.is_excluded(repo_root, Path(shadow)))
        # Unrelated project's scripts/ directory is still writable (path-aware,
        # not basename-banned).
        other_root = self.base / "otherproj"
        other_root.mkdir(exist_ok=True)
        reg2, pid2 = make_registry(self.base, name="Other", folder=other_root)
        old = os.environ.get("QUICKSHELL_PROJECTS_FILE")
        os.environ["QUICKSHELL_PROJECTS_FILE"] = str(reg2)
        try:
            created = project_folder.write_folder(
                {"project_id": pid2, "file": "scripts/notes.txt",
                 "content": "ok\n", "create": True})
            # Parent scripts/ does not exist yet -> must fail; create parent
            # then retry to prove unrelated scripts/ is allowed.
            self.assertTrue(False, "should have required parent")  # unreachable
        except GraphError:
            pass
        (self.base / "otherproj" / "scripts").mkdir(exist_ok=True)
        # Need fresh pid lookup after env change? pid2 still valid.
        created = project_folder.write_folder(
            {"project_id": pid2, "file": "scripts/notes.txt",
             "content": "ok\n", "create": True})
        self.assertTrue(created["created"])
        # But the actual trusted dir stays blocked even via repo-root link.
        repo_reg, repo_pid = make_registry(self.base, name="Repo", folder=repo_root)
        os.environ["QUICKSHELL_PROJECTS_FILE"] = str(repo_reg)
        try:
            with self.assertRaises(GraphError):
                project_folder.write_folder(
                    {"project_id": repo_pid, "file": "scripts/json.py",
                     "content": "evil\n", "create": True})
            with self.assertRaises(GraphError):
                project_folder.preflight(
                    {"project_id": repo_pid, "file": "scripts/json.py"})
        finally:
            if old is None:
                os.environ.pop("QUICKSHELL_PROJECTS_FILE", None)
            else:
                os.environ["QUICKSHELL_PROJECTS_FILE"] = old

    def test_cli_preflight_reports_binding(self):
        helper = ROOT / "scripts" / "project_folder.py"
        env = dict(os.environ, QUICKSHELL_PROJECTS_FILE=str(self.reg))
        (self.folder / "p.txt").write_text("P\n", encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, str(helper), "preflight"],
            input=json.dumps({"project_id": self.pid, "file": "p.txt"}),
            text=True, capture_output=True, timeout=5, env=env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertTrue(value["exists"])
        self.assertIn("root_dev", value)
        self.assertIn("root_ino", value)
        self.assertIsInstance(value["root_dev"], str)
        self.assertIsInstance(value["root_ino"], str)
        self.assertRegex(value["root_dev"], r"^[0-9]{1,20}$")
        self.assertRegex(value["root_ino"], r"^[0-9]{1,20}$")


class ProjectCreateDirTests(unittest.TestCase):
    """Home-only ``create-dir-preflight`` + ``create-dir`` coverage.

    These tests repoint ``HOME`` at a throwaway directory: ``expanduser``
    and ``Path.home()`` both honor it on POSIX, so nothing under the real
    home is touched and ``/tmp`` targets read as outside-home.
    """

    def setUp(self):
        self.home_temp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home_temp.name
        self.home = Path(self.home_temp.name).resolve(strict=True)

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home
        self.home_temp.cleanup()

    def under_home(self, *parts):
        base = self.home.joinpath(*parts)
        base.mkdir(parents=True, exist_ok=True)
        return base

    def preflight(self, path):
        return project_folder.create_dir_preflight({"path": path})

    def create(self, path, dev, ino):
        return project_folder.create_dir(
            {"path": path, "expected_parent_dev": dev,
             "expected_parent_ino": ino})

    def test_preflight_missing_reports_deepest_ancestor(self):
        parent = self.under_home("work")
        target = str(parent / "leaf")
        first = self.preflight(target)
        info = parent.stat()
        self.assertEqual(first, {
            "path": target, "exists": False, "is_dir": False,
            "parent_path": str(parent),
            "parent_dev": info.st_dev, "parent_ino": info.st_ino})
        self.assertIsInstance(first["parent_dev"], int)
        self.assertIsInstance(first["parent_ino"], int)
        # An untouched preflight creates nothing.
        self.assertFalse((parent / "leaf").exists())

    def test_preflight_missing_reports_higher_ancestor(self):
        first = self.preflight(str(self.home / "a" / "b" / "c"))
        self.assertEqual(first["parent_path"], str(self.home))
        self.assertFalse(first["exists"])
        self.assertFalse(first["is_dir"])

    def test_tilde_spelling_matches_absolute(self):
        parent = self.under_home("work")
        rel = str(Path("~/work/leaf").expanduser())
        self.assertEqual(self.preflight("~/work/leaf"),
                         self.preflight(str(parent / "leaf")))
        self.assertEqual(self.preflight("~/work/leaf")["path"], rel)

    def test_preflight_exists_as_dir_reports_self_parent(self):
        target = self.under_home("work", "existing")
        value = self.preflight(str(target))
        info = target.stat()
        self.assertTrue(value["exists"])
        self.assertTrue(value["is_dir"])
        self.assertEqual(value["parent_path"], str(target))
        self.assertEqual(value["parent_dev"], info.st_dev)
        self.assertEqual(value["parent_ino"], info.st_ino)

    def test_preflight_existing_file_and_symlink_are_errors(self):
        parent = self.under_home("work")
        (parent / "file.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaises(GraphError):
            self.preflight(str(parent / "file.txt"))
        (parent / "link").symlink_to(parent / "file.txt")
        with self.assertRaises(GraphError):
            self.preflight(str(parent / "link"))
        (parent / "dirlink").symlink_to(parent, target_is_directory=True)
        with self.assertRaises(GraphError):
            self.preflight(str(parent / "dirlink" / "fresh"))

    def test_preflight_outside_home_and_home_itself_rejected(self):
        with self.assertRaises(GraphError) as outside:
            self.preflight("/tmp/qs-no-such-dir-xyz/leaf")
        self.assertIn("inside the home directory", str(outside.exception))
        with self.assertRaises(GraphError):
            self.preflight("~")
        with self.assertRaises(GraphError):
            self.preflight(str(self.home))
        with self.assertRaises(GraphError):
            self.preflight(str(self.home) + "/")
        for bad in ("", "relative/path", "a\x00b", "back\\slash",
                    "~/../escape", "~otheruser/x"):
            with self.subTest(path=bad):
                with self.assertRaises(GraphError):
                    self.preflight(bad)
        # A single "." collapses lexically and stays valid: it resolves to
        # the same canonical target instead of escaping.
        dotted = self.preflight("~/work/./leaf")
        self.assertEqual(dotted["path"], str(self.home / "work" / "leaf"))

    def test_preflight_symlink_component_rejected(self):
        parent = self.under_home("work")
        real = parent / "real"
        real.mkdir()
        (parent / "gate").symlink_to(real, target_is_directory=True)
        with self.assertRaises(GraphError) as via_link:
            self.preflight(str(parent / "gate" / "fresh"))
        self.assertIn("symlink", str(via_link.exception))
        # An existing file mid-path is not a directory either.
        (parent / "blocker.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaises(GraphError):
            self.preflight(str(parent / "blocker.txt" / "fresh"))

    def test_create_dir_mkdir_p_then_idempotent(self):
        bound = self.preflight(str(self.home / "a" / "b" / "c"))
        made = self.create(str(self.home / "a" / "b" / "c"),
                           bound["parent_dev"], bound["parent_ino"])
        self.assertEqual(made, {"path": str(self.home / "a" / "b" / "c"),
                                "created": True})
        for part in ("a", "a/b", "a/b/c"):
            candidate = self.home / part
            self.assertTrue(candidate.is_dir())
            self.assertEqual(candidate.stat().st_mode & 0o777, 0o755)
        again = self.create(str(self.home / "a" / "b" / "c"),
                            *self._fresh_binding(str(self.home / "a" / "b"
                                                     / "c")))
        self.assertEqual(again["created"], False)
        self.assertEqual(again["exists"], True)

    def _fresh_binding(self, path):
        value = self.preflight(path)
        return value["parent_dev"], value["parent_ino"]

    def test_create_dir_binding_mismatch_creates_nothing(self):
        bound = self.preflight(str(self.home / "sub" / "leaf"))
        with self.assertRaises(GraphError) as mismatch:
            self.create(str(self.home / "sub" / "leaf"),
                        bound["parent_dev"] + 1, bound["parent_ino"])
        self.assertIn("preflight", str(mismatch.exception))
        self.assertFalse((self.home / "sub").exists())
        with self.assertRaises(GraphError):
            self.create(str(self.home / "sub" / "leaf"),
                        "not-an-int", bound["parent_ino"])
        with self.assertRaises(GraphError):
            project_folder.create_dir({"path": str(self.home / "x")})
        self.assertFalse((self.home / "sub").exists())

    def test_create_dir_replaced_parent_fails_binding(self):
        parent = self.under_home("work")
        bound = self.preflight(str(parent / "leaf"))
        # Swap the approved parent for a fresh directory: same path, new
        # identity. The stale binding must refuse to create into it.
        import shutil
        shutil.rmtree(parent)
        parent.mkdir()
        with self.assertRaises(GraphError):
            self.create(str(parent / "leaf"),
                        bound["parent_dev"], bound["parent_ino"])
        self.assertFalse((parent / "leaf").exists())
        # A fresh preflight rebinds and succeeds.
        fresh = self.preflight(str(parent / "leaf"))
        made = self.create(str(parent / "leaf"),
                           fresh["parent_dev"], fresh["parent_ino"])
        self.assertTrue(made["created"])

    def test_create_dir_too_deep_rejected_without_writes(self):
        deep = str(self.home.joinpath(*[f"d{i}" for i in range(9)]))
        bound = self.preflight(deep)
        with self.assertRaises(GraphError) as depth:
            self.create(deep, bound["parent_dev"], bound["parent_ino"])
        self.assertIn("8", str(depth.exception))
        self.assertFalse((self.home / "d0").exists())
        # Exactly 8 missing components is allowed.
        shallow = str(self.home.joinpath(*[f"e{i}" for i in range(8)]))
        bound8 = self.preflight(shallow)
        made = self.create(shallow, bound8["parent_dev"],
                           bound8["parent_ino"])
        self.assertTrue(made["created"])

    def test_protected_components_rejected_by_preflight_and_create(self):
        # FIX 2: any home-relative component of .ssh/.gnupg/.aws/.pi/.git
        # or starting with .env is rejected by both preflight and
        # create-dir (defense in depth beyond the human preview), and
        # nothing is created.
        targets = [
            str(self.home / ".ssh" / "fresh"),
            str(self.home / ".gnupg" / "fresh"),
            str(self.home / ".aws" / "fresh"),
            str(self.home / ".pi" / "fresh"),
            str(self.home / ".git" / "fresh"),
            str(self.home / ".env"),
            str(self.home / ".env.backup" / "fresh"),
            str(self.home / "work" / ".ssh" / "fresh"),
            str(self.home / "work" / ".env"),
            "~/.ssh/fresh",
            "~/.pi/agent/fresh",
        ]
        for target in targets:
            with self.subTest(path=target):
                with self.assertRaises(GraphError):
                    self.preflight(target)
                with self.assertRaises(GraphError):
                    project_folder.create_dir(
                        {"path": target, "expected_parent_dev": 0,
                         "expected_parent_ino": 0})
        self.assertFalse((self.home / ".ssh").exists())
        self.assertFalse((self.home / ".pi").exists())
        self.assertFalse((self.home / ".env").exists())
        self.assertFalse((self.home / "work").exists())
        # Near-miss spellings stay legal: exact-component match only
        # (except the .env prefix rule).
        allowed = self.preflight(str(self.home / "work" / ".gitignore"))
        self.assertFalse(allowed["exists"])
        made = self.create(str(self.home / "work" / ".gitignore"),
                           allowed["parent_dev"], allowed["parent_ino"])
        self.assertTrue(made["created"])

    def test_create_dir_never_touches_files_or_symlinks(self):
        parent = self.under_home("work")
        (parent / "taken.txt").write_text("keep\n", encoding="utf-8")
        dev, ino = self._fresh_binding(str(parent))
        with self.assertRaises(GraphError):
            self.create(str(parent / "taken.txt"), dev, ino)
        self.assertEqual((parent / "taken.txt").read_text(
            encoding="utf-8"), "keep\n")
        (parent / "link").symlink_to(parent / "taken.txt")
        with self.assertRaises(GraphError):
            self.create(str(parent / "link" / "x"), dev, ino)
        outside = Path(self.temp_dir_outside())
        with self.assertRaises(GraphError):
            self.create(str(outside / "x"), dev, ino)

    def temp_dir_outside(self):
        owned = tempfile.TemporaryDirectory()
        self.addCleanup(owned.cleanup)
        return owned.name

    def test_create_dir_outside_home_and_home_itself_rejected(self):
        with self.assertRaises(GraphError):
            self.create("/tmp/qs-no-such-dir-xyz/leaf", 0, 0)
        with self.assertRaises(GraphError):
            self.create(str(self.home), 0, 0)
        self.assertFalse(Path("/tmp/qs-no-such-dir-xyz").exists())

    def test_cli_preflight_and_create_dir(self):
        helper = ROOT / "scripts" / "project_folder.py"
        env = dict(os.environ)
        target = str(self.home / "cli" / "leaf")
        predicted = subprocess.run(
            [sys.executable, str(helper), "create-dir-preflight"],
            input=json.dumps({"path": target}), text=True,
            capture_output=True, timeout=5, env=env)
        self.assertEqual(predicted.returncode, 0, predicted.stderr)
        binding = json.loads(predicted.stdout)
        self.assertFalse(binding["exists"])
        made = subprocess.run(
            [sys.executable, str(helper), "create-dir"],
            input=json.dumps({"path": target,
                              "expected_parent_dev": binding["parent_dev"],
                              "expected_parent_ino": binding["parent_ino"]}),
            text=True, capture_output=True, timeout=5, env=env)
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertEqual(json.loads(made.stdout)["created"], True)
        self.assertTrue((self.home / "cli" / "leaf").is_dir())
        # Stale binding through the CLI creates nothing.
        stale = subprocess.run(
            [sys.executable, str(helper), "create-dir"],
            input=json.dumps({"path": str(self.home / "cli" / "other"),
                              "expected_parent_dev": 1,
                              "expected_parent_ino": 2}),
            text=True, capture_output=True, timeout=5, env=env)
        self.assertNotEqual(stale.returncode, 0)
        self.assertFalse((self.home / "cli" / "other").exists())


if __name__ == "__main__":
    unittest.main()
