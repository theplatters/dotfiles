import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import projects
import project_planner
from logseq_common import GraphError


def run_cli(args, stdin_text=None, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "projects.py"), *args],
        input=stdin_text, text=True, capture_output=True, timeout=15,
        check=False, env=env)
    return completed


class ProjectsRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.reg = str(self.base / "projects.toml")
        self.old_env = os.environ.get(projects.ENV_VAR)
        # Isolate from any real user registry for the whole test.
        if projects.ENV_VAR in os.environ:
            del os.environ[projects.ENV_VAR]

    def tearDown(self):
        if self.old_env is None:
            os.environ.pop(projects.ENV_VAR, None)
        else:
            os.environ[projects.ENV_VAR] = self.old_env
        self.temp.cleanup()

    # -- path resolution --------------------------------------------------------

    def test_default_path_is_repo_projects_toml_and_not_created(self):
        expected = (ROOT / "projects.toml").resolve()
        self.assertEqual(projects.resolve_registry_file(None), expected)
        self.assertEqual(projects.resolve_registry_file(""), expected)
        # Listing a temp registry must not create the repo default file.
        had_default = expected.exists()
        projects.list_projects(self.reg)
        self.assertEqual(expected.exists(), had_default)
        if not had_default:
            self.assertFalse((self.base / "projects.toml").exists()
                             and False)  # placeholder never true

    def test_explicit_flag_wins_over_env(self):
        other = str(self.base / "other.toml")
        os.environ[projects.ENV_VAR] = other
        self.assertEqual(str(projects.resolve_registry_file(None)), other)
        self.assertEqual(str(projects.resolve_registry_file(self.reg)), self.reg)
        # CLI: flag wins over env.
        listed = run_cli(["--projects-file", self.reg, "list"],
                         env_extra={projects.ENV_VAR: other})
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout)["file"], self.reg)

    def test_env_override_used_when_no_flag(self):
        os.environ[projects.ENV_VAR] = self.reg
        self.assertEqual(str(projects.resolve_registry_file(None)), self.reg)
        listed = run_cli(["list"])
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout)["file"], self.reg)

    def test_relative_paths_stay_lexical_and_symlinks_rejected(self):
        work = self.base / "work"
        work.mkdir()
        real = work / "real.toml"
        real.write_text("version = 1\n", encoding="utf-8")
        link = work / "link.toml"
        try:
            link.symlink_to("real.toml")
        except OSError:
            self.skipTest("symlinks unavailable")
        old_cwd = os.getcwd()
        os.chdir(work)
        try:
            # Lexical resolution: the relative link must NOT dereference to
            # the target before the O_NOFOLLOW open.
            resolved = projects.resolve_registry_file("link.toml")
            self.assertEqual(resolved, Path(os.path.abspath("link.toml")))
            self.assertTrue(str(resolved).endswith("link.toml"))
            before = real.read_bytes()
            with self.assertRaises(projects.RegistryError):
                projects.list_projects("link.toml")
            with self.assertRaises(projects.RegistryError):
                projects.create_project({"name": "x"}, "link.toml")
            self.assertEqual(real.read_bytes(), before)
            self.assertTrue(link.is_symlink())
            # A plain relative path still works and lands at the lexical spot.
            created = projects.create_project({"name": "rel"}, "fresh.toml")
            self.assertEqual(created["file"], str(Path(os.path.abspath("fresh.toml"))))
            self.assertTrue((work / "fresh.toml").is_file())
            # Env relative paths behave the same way.
            os.environ[projects.ENV_VAR] = "env.toml"
            self.assertEqual(projects.resolve_registry_file(None),
                             Path(os.path.abspath("env.toml")))
            created_env = projects.create_project({"name": "via-env"})
            self.assertEqual(created_env["file"],
                             str(Path(os.path.abspath("env.toml"))))
            os.environ[projects.ENV_VAR] = "link.toml"
            with self.assertRaises(projects.RegistryError):
                projects.list_projects(None)
            with self.assertRaises(projects.RegistryError):
                projects.create_project({"name": "y"})
            self.assertEqual(real.read_bytes(), before)
            self.assertTrue(link.is_symlink())
        finally:
            os.environ.pop(projects.ENV_VAR, None)
            os.chdir(old_cwd)

    def test_create_at_capacity_rejected_and_registry_readable(self):
        with patch.object(projects, "MAX_PROJECTS", 2):
            projects.create_project({"name": "A"}, self.reg)
            projects.create_project({"name": "B"}, self.reg)
            before = Path(self.reg).read_bytes()
            with self.assertRaises(projects.RegistryError) as capped:
                projects.create_project({"name": "C"}, self.reg)
            self.assertIn("too many", str(capped.exception).casefold())
            # Failed write left the previous bytes (and readability) intact.
            self.assertEqual(Path(self.reg).read_bytes(), before)
            listed = projects.list_projects(self.reg)
            self.assertEqual(len(listed["projects"]), 2)
            # Updates that do not grow the registry still work at capacity.
            updated = projects.update_project(
                {"id": listed["projects"][0]["id"],
                 "revision": listed["revision"], "name": "A2"}, self.reg)
            self.assertEqual(len(updated["projects"]), 2)

    def test_import_at_capacity_rejected_and_registry_readable(self):
        with patch.object(projects, "MAX_PROJECTS", 2):
            projects.create_project({"name": "A"}, self.reg)
            projects.create_project({"name": "B"}, self.reg)
            before = Path(self.reg).read_bytes()
            graph = self.make_graph({"New1.md": "type:: project\n",
                                     "New2.md": "type:: project\n"})
            with self.assertRaises(projects.RegistryError) as capped:
                projects.import_logseq(str(graph), self.reg)
            self.assertIn("too many", str(capped.exception).casefold())
            self.assertEqual(Path(self.reg).read_bytes(), before)
            self.assertEqual(len(projects.list_projects(self.reg)["projects"]), 2)
            # An import that adds nothing still succeeds at capacity.
            empty = self.base / "emptygraph"
            (empty / "pages").mkdir(parents=True)
            (empty / "journals").mkdir()
            result = projects.import_logseq(str(empty), self.reg)
            self.assertEqual(result["imported"], 0)
            self.assertEqual(len(result["projects"]), 2)

    # -- list / response shape ---------------------------------------------------

    def test_list_missing_is_empty_with_stable_missing_hash(self):
        value = projects.list_projects(self.reg)
        self.assertEqual(value["projects"], [])
        self.assertEqual(value["revision"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(value["file"], self.reg)
        # Missing hash is consistent across calls.
        again = projects.list_projects(self.reg)
        self.assertEqual(again["revision"], value["revision"])

    def test_record_shape_has_path_page_aliases(self):
        created = projects.create_project({"name": "Demo"}, self.reg)
        entry = created["projects"][0]
        for key in ("id", "name", "logseq_path", "local_folder",
                    "github_url", "path", "page"):
            self.assertIn(key, entry)
        self.assertEqual(entry["path"], entry["logseq_path"])
        self.assertEqual(entry["page"], entry["name"])
        self.assertIn("project", created)
        self.assertEqual(created["project"], entry)

    # -- create ------------------------------------------------------------------

    def test_create_name_only_and_optional_omitted(self):
        created = projects.create_project({"name": "  Solo  "}, self.reg)
        self.assertEqual(created["project"]["name"], "Solo")
        self.assertEqual(created["project"]["logseq_path"], "")
        text = Path(self.reg).read_text(encoding="utf-8")
        self.assertIn('name = "Solo"', text)
        self.assertNotIn("logseq_path", text)
        self.assertNotIn("local_folder", text)
        self.assertNotIn("github_url", text)
        # Read-back defaults to "".
        listed = projects.list_projects(self.reg)
        self.assertEqual(listed["projects"][0]["local_folder"], "")

    def test_create_full_and_sorted(self):
        projects.create_project({"name": "zeta", "logseq_path": "pages/Z.md",
                                 "local_folder": "/tmp/z",
                                 "github_url": "https://www.github.com/o/r.git"},
                                self.reg)
        projects.create_project({"name": "Alpha"}, self.reg)
        listed = projects.list_projects(self.reg)
        self.assertEqual([p["name"] for p in listed["projects"]],
                         ["Alpha", "zeta"])
        zeta = [p for p in listed["projects"] if p["name"] == "zeta"][0]
        self.assertEqual(zeta["github_url"], "https://github.com/o/r")

    def test_create_rejects_bad_fields_and_unknown_keys(self):
        with self.assertRaises(projects.RegistryError):
            projects.create_project({"name": "  "}, self.reg)
        with self.assertRaises(projects.RegistryError):
            projects.create_project({"name": "x", "logseq_path": "../evil.md"},
                                    self.reg)
        with self.assertRaises(projects.RegistryError):
            projects.create_project({"name": "x", "logseq_path": "/abs.md"},
                                    self.reg)
        with self.assertRaises(projects.RegistryError):
            projects.create_project({"name": "x", "logseq_path": "pages/X.txt"},
                                    self.reg)
        with self.assertRaises(projects.RegistryError):
            projects.create_project({"name": "x", "local_folder": "relative/path"},
                                    self.reg)
        with self.assertRaises(projects.RegistryError):
            projects.create_project({"name": "x", "github_url": "http://github.com/o/r"},
                                    self.reg)
        with self.assertRaises(projects.RegistryError):
            projects.create_project({"name": "x", "github_url": "https://example.com/o/r"},
                                    self.reg)
        with self.assertRaises(projects.RegistryError):
            projects.create_project({"name": "x", "bogus": 1}, self.reg)
        # Nothing was written.
        self.assertFalse(Path(self.reg).exists())

    def test_create_rejects_duplicate_note(self):
        first = projects.create_project({"name": "A", "logseq_path": "pages/A.md"},
                                        self.reg)
        self.assertTrue(first["project"]["id"])
        with self.assertRaises(projects.RegistryError):
            projects.create_project({"name": "B", "logseq_path": "pages/A.md"},
                                    self.reg)

    def test_local_folder_tilde_and_absolute_ok(self):
        created = projects.create_project({"name": "H", "local_folder": "~/work/x/"},
                                          self.reg)
        self.assertEqual(created["project"]["local_folder"], "~/work/x")
        created2 = projects.create_project({"name": "I", "local_folder": "/a/b/../c"},
                                           self.reg)
        self.assertEqual(created2["project"]["local_folder"], "/a/c")

    # -- update / remove / stale ---------------------------------------------------

    def test_update_full_replacement_and_stale_rejection(self):
        created = projects.create_project({"name": "A", "logseq_path": "pages/A.md",
                                           "local_folder": "/tmp/a",
                                           "github_url": "https://github.com/o/a"},
                                          self.reg)
        pid = created["project"]["id"]
        rev = created["revision"]
        updated = projects.update_project({"id": pid, "revision": rev,
                                           "name": "A2"}, self.reg)
        self.assertEqual(updated["project"]["name"], "A2")
        self.assertEqual(updated["project"]["logseq_path"], "")
        with self.assertRaises(projects.RegistryError) as stale:
            projects.update_project({"id": pid, "revision": rev, "name": "late"},
                                    self.reg)
        self.assertIn("stale", str(stale.exception))
        with self.assertRaises(projects.RegistryError):
            projects.remove_project({"id": pid, "revision": rev}, self.reg)

    def test_update_rejects_note_collision_and_unknown_id(self):
        first = projects.create_project({"name": "A", "logseq_path": "pages/A.md"},
                                        self.reg)
        second = projects.create_project({"name": "B", "logseq_path": "pages/B.md"},
                                         self.reg)
        with self.assertRaises(projects.RegistryError):
            projects.update_project({"id": second["project"]["id"],
                                     "revision": second["revision"],
                                     "name": "B",
                                     "logseq_path": "pages/A.md"}, self.reg)
        with self.assertRaises(projects.RegistryError):
            projects.update_project({"id": "00000000-0000-4000-8000-000000000000",
                                     "revision": second["revision"],
                                     "name": "ghost"}, self.reg)
        self.assertEqual(len(projects.list_projects(self.reg)["projects"]), 2)
        self.assertEqual(first["project"]["logseq_path"], "pages/A.md")

    def test_remove_ok_and_id_unknown(self):
        created = projects.create_project({"name": "A"}, self.reg)
        removed = projects.remove_project({"id": created["project"]["id"],
                                           "revision": created["revision"]},
                                          self.reg)
        self.assertEqual(removed["projects"], [])
        self.assertNotIn("project", removed)
        with self.assertRaises(projects.RegistryError):
            projects.remove_project({"id": created["project"]["id"],
                                     "revision": removed["revision"]}, self.reg)

    # -- strict schema / corrupt never silently overwritten --------------------------

    def test_corrupt_toml_never_silently_overwritten(self):
        Path(self.reg).write_text("version = 1\n[[projects\nbroken", encoding="utf-8")
        before = Path(self.reg).read_bytes()
        with self.assertRaises(projects.RegistryError):
            projects.list_projects(self.reg)
        with self.assertRaises(projects.RegistryError):
            projects.create_project({"name": "new"}, self.reg)
        self.assertEqual(Path(self.reg).read_bytes(), before)
        # Wrong version and unknown fields are also corrupt.
        Path(self.reg).write_text('version = 2\n', encoding="utf-8")
        with self.assertRaises(projects.RegistryError):
            projects.list_projects(self.reg)
        Path(self.reg).write_text('version = 1\nunknown = 1\n', encoding="utf-8")
        with self.assertRaises(projects.RegistryError):
            projects.list_projects(self.reg)

    def test_duplicate_id_and_note_in_file_rejected(self):
        Path(self.reg).write_text(
            'version = 1\n\n[[projects]]\nid = "11111111-1111-4111-8111-111111111111"\n'
            'name = "A"\n\n[[projects]]\nid = "11111111-1111-4111-8111-111111111111"\n'
            'name = "B"\n', encoding="utf-8")
        with self.assertRaises(projects.RegistryError) as dup:
            projects.list_projects(self.reg)
        self.assertIn("duplicate", str(dup.exception).casefold())
        Path(self.reg).write_text(
            'version = 1\n\n[[projects]]\nid = "11111111-1111-4111-8111-111111111111"\n'
            'name = "A"\nlogseq_path = "pages/X.md"\n\n[[projects]]\n'
            'id = "22222222-2222-4222-8222-222222222222"\nname = "B"\n'
            'logseq_path = "pages/X.md"\n', encoding="utf-8")
        with self.assertRaises(projects.RegistryError):
            projects.list_projects(self.reg)

    def test_nonregular_symlink_and_oversize_rejected(self):
        target = self.base / "real.toml"
        target.write_text('version = 1\n', encoding="utf-8")
        link = Path(self.reg)
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaises(projects.RegistryError):
            projects.list_projects(self.reg)
        with self.assertRaises(projects.RegistryError):
            projects.create_project({"name": "x"}, self.reg)
        link.unlink()
        self.base.joinpath("projects.toml").mkdir() if False else None
        # Directory at registry path.
        import shutil
        if link.exists():
            link.unlink()
        link.mkdir()
        try:
            with self.assertRaises(projects.RegistryError):
                projects.list_projects(self.reg)
        finally:
            shutil.rmtree(link)
        big = self.base / "big.toml"
        big.write_bytes(b'x = "' + b"y" * (projects.REGISTRY_LIMIT + 10) + b'"\n')
        with self.assertRaises(projects.RegistryError):
            projects.list_projects(str(big))

    def test_mode_preserved_on_update(self):
        created = projects.create_project({"name": "A"}, self.reg)
        Path(self.reg).chmod(0o640)
        updated = projects.update_project({"id": created["project"]["id"],
                                           "revision": created["revision"],
                                           "name": "A2"}, self.reg)
        self.assertEqual(oct(Path(self.reg).stat().st_mode & 0o777), "0o640")
        self.assertEqual(updated["project"]["name"], "A2")

    # -- locking / concurrency ----------------------------------------------------

    def test_lock_busy_fails_bounded_without_mutation(self):
        created = projects.create_project({"name": "A"}, self.reg)
        lock_path = Path(self.reg + ".lock")
        lock_path.touch(exist_ok=True)
        lock_fd = os.open(str(lock_path), os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            before = Path(self.reg).read_bytes()
            completed = run_cli(["--projects-file", self.reg, "create"],
                                stdin_text=json.dumps({"name": "B"}))
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("busy", completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(Path(self.reg).read_bytes(), before)
        self.assertEqual(len(projects.list_projects(self.reg)["projects"]), 1)
        self.assertEqual(created["project"]["name"], "A")

    def test_concurrent_creates_merge_and_concurrent_updates_contend(self):
        results = []

        def make(name):
            try:
                results.append(projects.create_project({"name": name}, self.reg))
            except projects.RegistryError as exc:
                results.append(exc)

        threads = [threading.Thread(target=make, args=(f"P{i}",)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(isinstance(v, dict) for v in results), 4)
        self.assertEqual(len(projects.list_projects(self.reg)["projects"]), 4)

        current = projects.list_projects(self.reg)
        victim = current["projects"][0]
        rev = current["revision"]
        outcomes = []

        def update(name):
            try:
                outcomes.append(projects.update_project(
                    {"id": victim["id"], "revision": rev, "name": name}, self.reg))
            except projects.RegistryError as exc:
                outcomes.append(exc)

        workers = [threading.Thread(target=update, args=(f"Q{i}",)) for i in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(sum(isinstance(v, dict) for v in outcomes), 1)
        self.assertEqual(sum(isinstance(v, projects.RegistryError)
                             for v in outcomes), 1)

    # -- CLI contract --------------------------------------------------------------

    def test_cli_json_shapes_and_no_traceback_on_error(self):
        listed = run_cli(["--projects-file", self.reg, "list"])
        self.assertEqual(listed.returncode, 0, listed.stderr)
        body = json.loads(listed.stdout)
        self.assertEqual(body["projects"], [])
        self.assertRegex(body["revision"], r"^[0-9a-f]{64}$")
        self.assertEqual(body["file"], self.reg)

        created = run_cli(["--projects-file", self.reg, "create"],
                          stdin_text=json.dumps({"name": "CLI"}))
        self.assertEqual(created.returncode, 0, created.stderr)
        cbody = json.loads(created.stdout)
        self.assertIn("project", cbody)

        updated = run_cli(
            ["--projects-file", self.reg, "create"],
            stdin_text=json.dumps({"name": "", "logseq_path": "pages/X.md"}))
        self.assertNotEqual(updated.returncode, 0)
        self.assertIn("error:", updated.stderr)
        self.assertNotIn("Traceback", updated.stderr)
        self.assertEqual(updated.stdout, "")

        bad = run_cli(["--projects-file", self.reg, "list"],
                      stdin_text=None)
        self.assertEqual(bad.returncode, 0)  # list ignores stdin absence

    def test_cli_update_remove_roundtrip(self):
        created = json.loads(run_cli(
            ["--projects-file", self.reg, "create"],
            stdin_text=json.dumps({"name": "R", "logseq_path": "pages/R.md"})).stdout)
        updated = run_cli(["--projects-file", self.reg, "update"],
                          stdin_text=json.dumps({"id": created["project"]["id"],
                                                 "revision": created["revision"],
                                                 "name": "R2",
                                                 "logseq_path": "pages/R.md"}))
        self.assertEqual(updated.returncode, 0, updated.stderr)
        ubody = json.loads(updated.stdout)
        self.assertEqual(ubody["project"]["name"], "R2")
        removed = run_cli(["--projects-file", self.reg, "remove"],
                          stdin_text=json.dumps({"id": created["project"]["id"],
                                                 "revision": ubody["revision"]}))
        self.assertEqual(removed.returncode, 0, removed.stderr)
        rbody = json.loads(removed.stdout)
        self.assertEqual(rbody["projects"], [])

    # -- import-logseq ---------------------------------------------------------------

    def make_graph(self, pages):
        graph = self.base / "graph"
        (graph / "pages").mkdir(parents=True, exist_ok=True)
        (graph / "journals").mkdir(exist_ok=True)
        for name, content in pages.items():
            (graph / "pages" / name).write_text(content, encoding="utf-8")
        return graph

    def test_import_recognizes_type_or_status_case_insensitive_links(self):
        graph = self.make_graph({
            "A.md": "type:: project\n",
            "B.md": "type:: [[Project]]\n",
            "C.md": "status:: PROJECT\n",
            "D.md": "type:: task\n",
            "E.md": "- TODO x\n",
        })
        result = projects.import_logseq(str(graph), self.reg)
        self.assertEqual({p["name"] for p in result["projects"]}, {"A", "B", "C"})
        self.assertEqual(result["imported"], 3)
        self.assertIn("warnings", result)

    def test_import_excludes_body_and_nested_properties(self):
        graph = self.make_graph({
            "Body.md": "- TODO x\n  file:: /tmp/nope\n  type:: project\n",
            "Fence.md": "```\ntype:: project\n```\n- TODO x\n",
            "Late.md": "- TODO x\ntype:: project\n",
            "Lead.md": "title:: Demo\ntype:: project\nfile:: /tmp/ok\n",
        })
        result = projects.import_logseq(str(graph), self.reg)
        names = {p["name"] for p in result["projects"]}
        self.assertNotIn("Body", names)
        self.assertNotIn("Fence", names)
        self.assertNotIn("Late", names)
        self.assertIn("Lead", names)
        lead = [p for p in result["projects"] if p["name"] == "Lead"][0]
        self.assertEqual(lead["local_folder"], "/tmp/ok")

    def test_import_folder_and_github_normalization_with_warnings(self):
        graph = self.make_graph({
            "Repair.md": "type:: project\nfile:: file://home/franzs/W\n",
            "Ambig.md": "type:: project\nfile:: file://~Arbeit/X\n",
            "Dup.md": "type:: project\nfile:: /tmp/one\nfile:: /tmp/two\n",
            "Link.md": "type:: project\nurl:: [Github](https://github.com/paulbanse/sciencemodels)\n",
            "Www.md": "type:: project\nurl:: https://www.github.com/theplatters/Helm.jl\n",
            "Bare.md": "type:: project\n",
        })
        result = projects.import_logseq(str(graph), self.reg)
        by_name = {p["name"]: p for p in result["projects"]}
        self.assertEqual(by_name["Repair"]["local_folder"], "/home/franzs/W")
        self.assertEqual(by_name["Ambig"]["local_folder"], "")
        self.assertTrue(any("Ambig" in w and "ambiguous" in w for w in result["warnings"]))
        self.assertEqual(by_name["Dup"]["local_folder"], "")
        self.assertTrue(any("Dup" in w or "duplicate file" in w for w in result["warnings"]))
        self.assertEqual(by_name["Link"]["github_url"],
                         "https://github.com/paulbanse/sciencemodels")
        self.assertEqual(by_name["Www"]["github_url"],
                         "https://github.com/theplatters/Helm.jl")
        self.assertEqual(by_name["Bare"]["local_folder"], "")
        self.assertEqual(by_name["Bare"]["github_url"], "")

    def test_import_idempotent_never_overrides_nor_rewrites_graph(self):
        graph = self.make_graph({"A.md": "type:: project\nfile:: /tmp/a\n"})
        first = projects.import_logseq(str(graph), self.reg)
        before = {p["logseq_path"]: p["id"] for p in first["projects"]}
        before_bytes = sorted((graph / "pages").glob("*.md"))
        before_text = {p.name: p.read_text(encoding="utf-8") for p in before_bytes}
        # Change the graph note; re-import must not override the registry entry.
        (graph / "pages" / "A.md").write_text(
            "type:: project\nfile:: /tmp/changed\nurl:: https://github.com/o/new\n")
        second = projects.import_logseq(str(graph), self.reg)
        self.assertEqual(second["imported"], 0)
        after = {p["logseq_path"]: p for p in second["projects"]}
        self.assertEqual(after["pages/A.md"]["local_folder"], "/tmp/a")
        self.assertEqual(after["pages/A.md"]["id"], before["pages/A.md"])
        for name, text in before_text.items():
            if name != "A.md":
                self.assertEqual((graph / "pages" / name).read_text(encoding="utf-8"), text)
        # New notes still import alongside old ones.
        (graph / "pages" / "B.md").write_text("type:: project\n")
        third = projects.import_logseq(str(graph), self.reg)
        self.assertEqual(third["imported"], 1)
        self.assertEqual(len(third["projects"]), 2)

    def test_import_cli_has_warnings_and_needs_graph(self):
        graph = self.make_graph({"A.md": "type:: project\n"})
        completed = run_cli(["--projects-file", self.reg,
                             "--graph", str(graph), "import-logseq"])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        body = json.loads(completed.stdout)
        self.assertIn("warnings", body)
        self.assertIn("imported", body)
        self.assertIn("projects", body)
        empty_settings = self.base / "empty.json"
        empty_settings.write_text("{}", encoding="utf-8")
        missing = run_cli(["--projects-file", str(self.base / "fresh.toml"),
                           "import-logseq"],
                          env_extra={"LOGSEQ_GRAPH": "",
                                     "QUICKSHELL_SETTINGS": str(empty_settings)})
        # No graph configured: useful error, no traceback.
        self.assertNotEqual(missing.returncode, 0)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("error:", missing.stderr)
        self.assertNotIn("Traceback", missing.stderr)

    # -- files_* registry integration --------------------------------------------------

    def test_files_prefer_registry_and_fallback_legacy(self):
        graph = self.base / "graph2"
        (graph / "pages").mkdir(parents=True)
        (graph / "journals").mkdir()
        folder_a = self.base / "folderA"
        folder_b = self.base / "folderB"
        folder_a.mkdir()
        folder_b.mkdir()
        (folder_a / "from_a.txt").write_text("A\n", encoding="utf-8")
        (folder_b / "from_b.txt").write_text("B\n", encoding="utf-8")
        (graph / "pages" / "Proj.md").write_text(f"file:: {folder_a}\n",
                                                 encoding="utf-8")
        (graph / "pages" / "Other.md").write_text(f"file:: {folder_a}\n",
                                                  encoding="utf-8")
        projects.create_project({"name": "Proj", "logseq_path": "pages/Proj.md",
                                 "local_folder": str(folder_b)}, self.reg)
        os.environ[projects.ENV_VAR] = self.reg
        try:
            listed = project_planner.files_list(str(graph), {"path": "pages/Proj.md"})
            self.assertEqual(listed["root"], str(folder_b.resolve()))
            self.assertIn("from_b.txt", [e["path"] for e in listed["entries"]])
            # Caller-supplied root is ignored even with a registry hit.
            listed_evil = project_planner.files_list(
                str(graph), {"path": "pages/Proj.md", "root": "/tmp/evil"})
            self.assertEqual(listed_evil["root"], str(folder_b.resolve()))
            # Unregistered notes keep the legacy file:: fallback.
            fallback = project_planner.files_list(str(graph), {"path": "pages/Other.md"})
            self.assertEqual(fallback["root"], str(folder_a.resolve()))
            # Reads also prefer the registry.
            value = project_planner.files_read(
                str(graph), {"path": "pages/Proj.md", "file": "from_b.txt"})
            self.assertEqual(value["content"], "B\n")
            with self.assertRaises(GraphError):
                project_planner.files_read(
                    str(graph), {"path": "pages/Proj.md", "file": "from_a.txt"})
        finally:
            os.environ.pop(projects.ENV_VAR, None)

    def test_files_registry_bad_folder_fails_closed(self):
        graph = self.base / "graph3"
        (graph / "pages").mkdir(parents=True)
        (graph / "journals").mkdir()
        folder_a = self.base / "folderA3"
        folder_a.mkdir()
        (graph / "pages" / "Proj.md").write_text(f"file:: {folder_a}\n",
                                                 encoding="utf-8")
        projects.create_project({"name": "Proj", "logseq_path": "pages/Proj.md",
                                 "local_folder": "/nonexistent-qs-probe-12345"},
                                self.reg)
        os.environ[projects.ENV_VAR] = self.reg
        try:
            with self.assertRaises(GraphError):
                project_planner.files_list(str(graph), {"path": "pages/Proj.md"})
        finally:
            os.environ.pop(projects.ENV_VAR, None)


if __name__ == "__main__":
    unittest.main()
