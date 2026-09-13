import json
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import project_files
import project_planner
from logseq_common import GraphError


def git_available():
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=5, check=True)
        return True
    except Exception:
        return False


GIT = git_available()


def run_git(args, cwd, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(["git", *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=10, check=False)


class ProjectFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.graph = base / "graph"
        (self.graph / "pages").mkdir(parents=True)
        (self.graph / "journals").mkdir()
        # Folder path with spaces exercises argv handling (no shell).
        self.folder = base / "my projects" / "demo repo"
        self.folder.mkdir(parents=True)
        self.page = self.graph / "pages" / "Project.md"

    def tearDown(self):
        self.temp.cleanup()

    def write_page(self, content):
        self.page.write_text(content, encoding="utf-8")

    def root_for(self, content=None):
        if content is None:
            content = self.page.read_text(encoding="utf-8")
        from logseq_common import graph_path
        return project_files.resolve_root_from_content(graph_path(self.graph), content)

    def test_file_property_absolute_and_outside_graph(self):
        self.write_page(f"file:: {self.folder}\n\n- TODO x\n")
        root = self.root_for()
        self.assertEqual(root, self.folder.resolve())

    def test_file_property_forms(self):
        (self.graph / "rel").mkdir()
        self.write_page("file:: rel\n")
        self.assertEqual(self.root_for(), (self.graph / "rel").resolve())
        # file:// URL
        self.write_page(f"file:: file://{self.folder}\n")
        self.assertEqual(self.root_for(), self.folder.resolve())
        # markdown link
        self.write_page(f"file:: [demo](file://{self.folder})\n")
        self.assertEqual(self.root_for(), self.folder.resolve())
        # tilde
        home = Path.home().resolve()
        self.write_page("file:: ~\n")
        self.assertEqual(self.root_for(), home)

    def test_file_property_missing_duplicate_empty_and_block_only(self):
        self.write_page("title:: x\n\n- TODO x\n")
        with self.assertRaises(GraphError):
            self.root_for()
        self.write_page("file:: \n")
        with self.assertRaises(GraphError):
            self.root_for()
        self.write_page(f"file:: {self.folder}\nfile:: {self.folder}\n")
        with self.assertRaises(GraphError):
            self.root_for()
        # Block-level property must not grant a root.
        self.write_page("- TODO x\n  file:: /tmp\n")
        with self.assertRaises(GraphError):
            self.root_for()
        self.write_page(f"file:: {self.folder}\n- TODO x\n  file:: /tmp\n")
        self.assertEqual(self.root_for(), self.folder.resolve())
        # Nonexistent / invalid roots.
        self.write_page("file:: /nonexistent-qs-probe-12345\n")
        with self.assertRaises(GraphError):
            self.root_for()
        self.write_page("file:: /etc\n")
        with self.assertRaises(GraphError):
            self.root_for()
        (self.folder / "f.txt").write_text("x\n", encoding="utf-8")
        self.write_page(f"file:: {self.folder}/f.txt\n")
        with self.assertRaises(GraphError):
            self.root_for()

    def test_listing_and_read_with_spaces(self):
        (self.folder / "src").mkdir()
        (self.folder / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
        (self.folder / "notes.txt").write_text("hello\n", encoding="utf-8")
        self.write_page(f"file:: {self.folder}\n")
        root = self.root_for()
        listing = project_files.list_files(root)
        paths = sorted(e["path"] for e in listing["entries"])
        self.assertIn("src/main.py", paths)
        self.assertIn("notes.txt", paths)
        value = project_files.read_file(root, "src/main.py")
        self.assertEqual(value["content"], "print('hi')\n")

    def test_traversal_symlink_sensitive_rejected(self):
        (self.folder / "ok.txt").write_text("ok\n", encoding="utf-8")
        (self.folder / ".env").write_text("SECRET=1\n", encoding="utf-8")
        (self.folder / "sub").mkdir()
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        (self.folder / "sub" / "evil").symlink_to(outside)
        (self.folder / "linkdir").symlink_to(self.folder / "sub", target_is_directory=True)
        os.mkfifo(self.folder / "pipe")
        self.write_page(f"file:: {self.folder}\n")
        root = self.root_for()
        listing = project_files.list_files(root)
        paths = [e["path"] for e in listing["entries"]]
        self.assertIn("ok.txt", paths)
        self.assertNotIn(".env", paths)
        self.assertNotIn("sub/evil", paths)
        self.assertNotIn("pipe", paths)
        with self.assertRaises(GraphError):
            project_files.read_file(root, "../outside.txt")
        with self.assertRaises(GraphError):
            project_files.read_file(root, "/etc/hostname")
        with self.assertRaises(GraphError):
            project_files.read_file(root, "sub/evil")
        with self.assertRaises(GraphError):
            project_files.read_file(root, ".env")
        with self.assertRaises(GraphError):
            project_files.read_file(root, "sub")
        with self.assertRaises(GraphError):
            project_files.read_file(root, "missing.txt")
        # .ssh subtree is always excluded.
        ssh = self.folder / ".ssh"
        ssh.mkdir()
        (ssh / "id_rsa").write_text("x\n", encoding="utf-8")
        listing = project_files.list_files(root)
        self.assertNotIn(".ssh/id_rsa", [e["path"] for e in listing["entries"]])
        with self.assertRaises(GraphError):
            project_files.read_file(root, ".ssh/id_rsa")

    def test_read_bounds(self):
        (self.folder / "big.txt").write_bytes(b"x" * (project_files.READ_LIMIT + 1))
        (self.folder / "bin.txt").write_bytes(b"\xff\xfe")
        self.write_page(f"file:: {self.folder}\n")
        root = self.root_for()
        with self.assertRaises(GraphError):
            project_files.read_file(root, "big.txt")
        with self.assertRaises(GraphError):
            project_files.read_file(root, "bin.txt")

    @unittest.skipUnless(GIT, "git is required")
    def test_git_staged_unstaged_untracked_with_spaces(self):
        run_git(["init"], self.folder)
        run_git(["config", "user.email", "t@t"], self.folder)
        run_git(["config", "user.name", "t"], self.folder)
        (self.folder / "tracked.txt").write_text("one\n", encoding="utf-8")
        run_git(["add", "tracked.txt"], self.folder)
        run_git(["commit", "-m", "first"], self.folder)
        (self.folder / "tracked.txt").write_text("two\n", encoding="utf-8")
        run_git(["add", "tracked.txt"], self.folder)
        (self.folder / "second.txt").write_text("s\n", encoding="utf-8")
        run_git(["add", "second.txt"], self.folder)
        (self.folder / "tracked.txt").write_text("three\n", encoding="utf-8")
        (self.folder / "new untracked.txt").write_text("via read tool\n", encoding="utf-8")
        self.write_page(f"file:: {self.folder}\n")
        root = self.root_for()
        info = project_files.git_info(root)
        self.assertTrue(info["isRepo"])
        self.assertFalse(info.get("unborn", False))
        self.assertIsNotNone(info["head"])
        self.assertEqual(info["head"]["subject"], "first")
        status_paths = {c["path"] for c in info["changes"]} | set(info["untracked"])
        self.assertIn("tracked.txt", status_paths)
        self.assertIn("new untracked.txt", status_paths)
        # Untracked contents come via the read tool, not the diff.
        self.assertNotIn("via read tool", info["diff"])
        self.assertIn("tracked.txt", info["diff"])
        # Pinned-page CLI works and ignores a caller-supplied root override.
        payload = json.dumps({"path": "pages/Project.md",
                              "root": "/tmp/evil"}).encode()
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "project_planner.py"),
             "--graph", str(self.graph), "files-git"],
            input=payload, capture_output=True, timeout=15, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode()[:1000])
        value = json.loads(completed.stdout.decode())
        self.assertTrue(value["isRepo"])
        self.assertIn("tracked.txt", value["diff"])

    @unittest.skipUnless(GIT, "git is required")
    def test_git_sensitive_excluded_and_external_disabled(self):
        run_git(["init"], self.folder)
        run_git(["config", "user.email", "t@t"], self.folder)
        run_git(["config", "user.name", "t"], self.folder)
        (self.folder / "ok.txt").write_text("a\n", encoding="utf-8")
        (self.folder / ".env").write_text("SECRET=xyz\n", encoding="utf-8")
        run_git(["add", "ok.txt", ".env"], self.folder)
        run_git(["commit", "-m", "init"], self.folder)
        (self.folder / "ok.txt").write_text("b\n", encoding="utf-8")
        (self.folder / ".env").write_text("SECRET=changed\n", encoding="utf-8")
        self.write_page(f"file:: {self.folder}\n")
        root = self.root_for()
        info = project_files.git_info(root)
        self.assertNotIn("SECRET", info["diff"])
        self.assertNotIn(".env", info["diff"])
        self.assertIn("ok.txt", info["diff"])
        # Execution-capable local config is refused fail-closed, before any
        # content command runs.
        marker = Path(self.temp.name) / "pwned-marker"
        hook = Path(self.temp.name) / "evil-diff.sh"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\necho evil\n", encoding="utf-8")
        hook.chmod(0o755)
        run_git(["config", "diff.external", str(hook)], self.folder)
        with self.assertRaises(GraphError) as refused:
            project_files.git_info(root)
        self.assertIn("refusing", str(refused.exception))
        self.assertFalse(marker.exists())
        run_git(["config", "--unset", "diff.external"], self.folder)
        # GIT_EXTERNAL_DIFF in the environment must also be ignored.
        marker2 = Path(self.temp.name) / "pwned-env"
        hook2 = Path(self.temp.name) / "evil-env.sh"
        hook2.write_text(f"#!/bin/sh\ntouch {marker2}\n", encoding="utf-8")
        hook2.chmod(0o755)
        old = os.environ.get("GIT_EXTERNAL_DIFF")
        os.environ["GIT_EXTERNAL_DIFF"] = str(hook2)
        try:
            info2 = project_files.git_info(root)
        finally:
            if old is None:
                os.environ.pop("GIT_EXTERNAL_DIFF", None)
            else:
                os.environ["GIT_EXTERNAL_DIFF"] = old
        self.assertIn("ok.txt", info2["diff"])
        self.assertFalse(marker2.exists())

    @unittest.skipUnless(GIT, "git is required")
    def test_git_subtree_scoping(self):
        run_git(["init"], self.folder)
        run_git(["config", "user.email", "t@t"], self.folder)
        run_git(["config", "user.name", "t"], self.folder)
        sub = self.folder / "sub project"
        sub.mkdir()
        (self.folder / "top.txt").write_text("top\n", encoding="utf-8")
        (sub / "inner.txt").write_text("inner\n", encoding="utf-8")
        run_git(["add", "."], self.folder)
        run_git(["commit", "-m", "init"], self.folder)
        (self.folder / "top.txt").write_text("top changed\n", encoding="utf-8")
        (sub / "inner.txt").write_text("inner changed\n", encoding="utf-8")
        self.write_page(f"file:: {sub}\n")
        root = self.root_for()
        info = project_files.git_info(root)
        self.assertTrue(info["isRepo"])
        self.assertIn("inner.txt", info["diff"])
        self.assertNotIn("top changed", info["diff"])
        self.assertNotIn("top.txt", [c["path"] for c in info["changes"]])

    @unittest.skipUnless(GIT, "git is required")
    def test_git_nonrepo_and_unborn(self):
        self.write_page(f"file:: {self.folder}\n")
        root = self.root_for()
        info = project_files.git_info(root)
        self.assertFalse(info["isRepo"])
        run_git(["init"], self.folder)
        (self.folder / "draft.txt").write_text("d\n", encoding="utf-8")
        info = project_files.git_info(root)
        self.assertTrue(info["isRepo"])
        self.assertTrue(info.get("unborn"))
        self.assertIsNone(info["head"])
        self.assertEqual(info["diff"], "")

    def test_cli_files_list_and_read(self):
        (self.folder / "a.txt").write_text("A\n", encoding="utf-8")
        self.write_page(f"file:: {self.folder}\n")
        listed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "project_planner.py"),
             "--graph", str(self.graph), "files-list"],
            input=json.dumps({"path": "pages/Project.md"}), text=True,
            capture_output=True, timeout=10, check=False)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("a.txt", listed.stdout)
        read = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "project_planner.py"),
             "--graph", str(self.graph), "files-read"],
            input=json.dumps({"path": "pages/Project.md", "file": "a.txt"}),
            text=True, capture_output=True, timeout=10, check=False)
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertEqual(json.loads(read.stdout)["content"], "A\n")
        denied = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "project_planner.py"),
             "--graph", str(self.graph), "files-read"],
            input=json.dumps({"path": "pages/Project.md", "file": "../x"}),
            text=True, capture_output=True, timeout=10, check=False)
        self.assertNotEqual(denied.returncode, 0)
        noproperty = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "project_planner.py"),
             "--graph", str(self.graph), "files-list"],
            input=json.dumps({"path": "pages/Project.md"}), text=True,
            capture_output=True, timeout=10, check=False)
        # Sanity: with a property this succeeds; rewrite without and retry.
        self.assertEqual(noproperty.returncode, 0, noproperty.stderr)
        self.write_page("no property here\n")
        missing = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "project_planner.py"),
             "--graph", str(self.graph), "files-list"],
            input=json.dumps({"path": "pages/Project.md"}), text=True,
            capture_output=True, timeout=10, check=False)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("file::", missing.stderr)

    @unittest.skipUnless(GIT, "git is required")
    def test_git_execution_config_refused_fail_closed(self):
        run_git(["init"], self.folder)
        run_git(["config", "user.email", "t@t"], self.folder)
        run_git(["config", "user.name", "t"], self.folder)
        (self.folder / "ok.txt").write_text("a\n", encoding="utf-8")
        run_git(["add", "ok.txt"], self.folder)
        run_git(["commit", "-m", "init"], self.folder)
        self.write_page(f"file:: {self.folder}\n")
        root = self.root_for()
        # Baseline without execution config works.
        self.assertTrue(project_files.git_info(root)["isRepo"])
        cases = [
            (["filter.lfs.clean", "git-lfs clean %f"],
             ["filter.lfs.smudge", "git-lfs smudge %f"],
             ["filter.lfs.process", "git-lfs filter-process"]),
            (["diff.ipynb.textconv", "jupyter nbconvert"],),
            (["core.fsmonitorHook", "/bin/true"],),
            (["core.pager", "less"],),
            (["merge.custom.driver", "touch %A"],),
            (["include.path", "/tmp/other.inc"],),
            (["submodule.sub.update", "!evil"],),
            (["protocol.allow", "always"],),
            # Dotted subsections normalize with dots intact and must not
            # bypass the subsection patterns.
            (["filter.safe.driver.clean", "touch MARKER"],),
            (["diff.safe.cmd.textconv", "cat"],),
            (["merge.safe.opts.driver", "touch %A"],),
        ]
        for keys in cases:
            with self.subTest(keys=[k for k, _ in keys]):
                for key, value in keys:
                    run_git(["config", key, value], self.folder)
                try:
                    with self.assertRaises(GraphError) as refused:
                        project_files.git_info(root)
                    self.assertIn("refusing", str(refused.exception))
                finally:
                    for key, _value in keys:
                        run_git(["config", "--unset", key], self.folder)
        # Restrictive protocol.allow=never is not execution: allowed.
        run_git(["config", "protocol.allow", "never"], self.folder)
        try:
            self.assertTrue(project_files.git_info(root)["isRepo"])
        finally:
            run_git(["config", "--unset", "protocol.allow"], self.folder)

    @unittest.skipUnless(GIT, "git is required")
    def test_git_submodule_content_never_leaks(self):
        run_git(["init"], self.folder)
        run_git(["config", "user.email", "t@t"], self.folder)
        run_git(["config", "user.name", "t"], self.folder)
        sub_src = Path(self.temp.name) / "subsrc"
        sub_src.mkdir()
        run_git(["init"], sub_src)
        run_git(["config", "user.email", "t@t"], sub_src)
        run_git(["config", "user.name", "t"], sub_src)
        (sub_src / ".env").write_text("SUB_SECRET=leak-me\n", encoding="utf-8")
        run_git(["add", ".env"], sub_src)
        run_git(["commit", "-m", "sub"], sub_src)
        run_git(["-c", "protocol.file.allow=always",
                 "submodule", "add", str(sub_src), "vendor/sub"], self.folder)
        run_git(["commit", "-m", "add sub"], self.folder)
        # Dirty the submodule and try the submodule-diff leak mode.
        (self.folder / "vendor" / "sub" / ".env").write_text(
            "SUB_SECRET=changed\n", encoding="utf-8")
        run_git(["config", "diff.submodule", "diff"], self.folder)
        self.write_page(f"file:: {self.folder}\n")
        root = self.root_for()
        info = project_files.git_info(root)
        self.assertTrue(info["isRepo"])
        self.assertNotIn("SUB_SECRET", info["diff"])
        self.assertNotIn("leak-me", info["diff"])
        for change in info["changes"]:
            self.assertNotIn(".env", change["path"])

    def test_root_inside_protected_or_secret_tree_refused(self):
        secret = Path(self.temp.name) / "secrets" / "proj"
        secret.mkdir(parents=True)
        self.write_page(f"file:: {secret}\n")
        with self.assertRaises(GraphError):
            self.root_for()
        ssh = Path(self.temp.name) / ".ssh" / "proj"
        ssh.mkdir(parents=True)
        self.write_page(f"file:: {ssh}\n")
        with self.assertRaises(GraphError):
            self.root_for()

    @unittest.skipUnless(GIT, "git is required")
    def test_root_at_git_internals_refused(self):
        run_git(["init"], self.folder)
        self.write_page(f"file:: {self.folder}/.git\n")
        with self.assertRaises(GraphError):
            self.root_for()

    def test_property_preamble_restriction(self):
        # Other page properties may precede file:: in the leading block.
        self.write_page(f"title:: Demo\nalias:: D\nfile:: {self.folder}\n")
        self.assertEqual(self.root_for(), self.folder.resolve())
        # A fenced sample containing file:: grants nothing...
        self.write_page("```\nfile:: /tmp/evil\n```\n- TODO x\n")
        with self.assertRaises(GraphError):
            self.root_for()
        # ...and neither does a column-zero file:: after the first block.
        self.write_page(f"- TODO x\nfile:: {self.folder}\n")
        with self.assertRaises(GraphError):
            self.root_for()
        # Duplicates inside the preamble are still ambiguous.
        self.write_page(f"file:: {self.folder}\ntitle:: T\nfile:: {self.folder}\n")
        with self.assertRaises(GraphError):
            self.root_for()

    @unittest.skipUnless(GIT, "git is required")
    def test_status_paths_root_relative_usable_and_rename_endpoints(self):
        run_git(["init"], self.folder)
        run_git(["config", "user.email", "t@t"], self.folder)
        run_git(["config", "user.name", "t"], self.folder)
        sub = self.folder / "sub dir"
        sub.mkdir()
        (sub / "old name.txt").write_text("same body\n", encoding="utf-8")
        (sub / "[brackets].txt").write_text("glob?\n", encoding="utf-8")
        run_git(["add", "."], self.folder)
        run_git(["commit", "-m", "init"], self.folder)
        run_git(["mv", "sub dir/old name.txt", "sub dir/new name.txt"], self.folder)
        (sub / "[brackets].txt").write_text("changed?\n", encoding="utf-8")
        self.write_page(f"file:: {sub}\n")
        root = self.root_for()
        info = project_files.git_info(root)
        by_path = {c["path"]: c for c in info["changes"]}
        # Repo-relative leaks ("sub dir/...") must not appear; display paths
        # are relative to the selected root.
        self.assertNotIn("sub dir/new name.txt", by_path)
        self.assertIn("new name.txt", by_path)
        self.assertIn("[brackets].txt", by_path)
        renamed = by_path["new name.txt"]
        self.assertEqual(renamed.get("orig"), "old name.txt")
        # Every reported path round-trips through the read tool...
        readable = project_files.read_file(root, "[brackets].txt")
        self.assertEqual(readable["content"], "changed?\n")
        # ...and the literal-pathspec diff renders the glob-named file plus
        # the rename as an explicit delete+add pair (renames disabled).
        self.assertIn("[brackets].txt", info["diff"])
        self.assertIn("new name.txt", info["diff"])
        self.assertIn("old name.txt", info["diff"])

    @unittest.skipUnless(GIT, "git is required")
    def test_nonrepo_and_unborn_share_git_schema(self):
        self.write_page(f"file:: {self.folder}\n")
        root = self.root_for()
        info = project_files.git_info(root)
        for key in ("root", "isRepo", "unborn", "head", "lastTouching",
                    "toplevel", "prefix", "changes", "untracked", "truncated",
                    "diff", "diffTruncated"):
            self.assertIn(key, info)
        self.assertFalse(info["isRepo"])
        self.assertFalse(info["unborn"])
        self.assertEqual(info["diff"], "")
        self.assertIsNone(info["toplevel"])
        run_git(["init"], self.folder)
        (self.folder / "draft.txt").write_text("d\n", encoding="utf-8")
        born = project_files.git_info(root)
        self.assertTrue(born["isRepo"])
        self.assertTrue(born["unborn"])
        self.assertIsNone(born["head"])
        self.assertEqual(born["diff"], "")
        self.assertEqual(born["untracked"], ["draft.txt"])

    @unittest.skipUnless(GIT, "git is required")
    def test_diff_truncation_is_byte_budgeted_and_utf8_safe(self):
        run_git(["init"], self.folder)
        run_git(["config", "user.email", "t@t"], self.folder)
        run_git(["config", "user.name", "t"], self.folder)
        line = "€".join(["x"] * 40) + "\n"  # multibyte content per line
        (self.folder / "wide.txt").write_text(line * 200, encoding="utf-8")
        run_git(["add", "wide.txt"], self.folder)
        run_git(["commit", "-m", "init"], self.folder)
        # Sized so the resulting diff lands between DIFF_LIMIT (256 KiB) and
        # the 512 KiB subprocess cap: truncation, not refusal, applies.
        (self.folder / "wide.txt").write_text(line * 2500, encoding="utf-8")
        self.write_page(f"file:: {self.folder}\n")
        root = self.root_for()
        info = project_files.git_info(root)
        self.assertTrue(info["diffTruncated"])
        encoded = info["diff"].encode("utf-8")
        self.assertLessEqual(len(encoded), project_files.DIFF_LIMIT + 1)
        self.assertTrue(info["diff"].endswith("[truncated at 256 KiB]\n"))
        info["diff"].encode("utf-8").decode("utf-8", errors="strict")

    def test_mid_path_symlink_component_rejected(self):
        real = self.folder / "real"
        real.mkdir()
        (real / "secret.txt").write_text("s\n", encoding="utf-8")
        (self.folder / "gate").mkdir()
        (self.folder / "gate" / "redir").symlink_to(real, target_is_directory=True)
        self.write_page(f"file:: {self.folder}\n")
        root = self.root_for()
        with self.assertRaises(GraphError):
            project_files.read_file(root, "gate/redir/secret.txt")
        listing = project_files.list_files(root)
        self.assertNotIn("gate/redir/secret.txt",
                         [e["path"] for e in listing["entries"]])

    @unittest.skipUnless(GIT, "git is required")
    def test_git_dotted_filter_driver_refused_without_execution(self):
        run_git(["init"], self.folder)
        run_git(["config", "user.email", "t@t"], self.folder)
        run_git(["config", "user.name", "t"], self.folder)
        (self.folder / "note.txt").write_text("hello\n", encoding="utf-8")
        run_git(["add", "note.txt"], self.folder)
        run_git(["commit", "-m", "init"], self.folder)
        marker = Path(self.temp.name) / "dotted-marker"
        hook = Path(self.temp.name) / "dotted-clean.sh"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n", encoding="utf-8")
        hook.chmod(0o755)
        run_git(["config", "filter.safe.driver.clean", str(hook)], self.folder)
        (self.folder / ".gitattributes").write_text(
            "*.txt filter=safe.driver\n", encoding="utf-8")
        (self.folder / "note.txt").write_text("hello again\n", encoding="utf-8")
        self.write_page(f"file:: {self.folder}\n")
        root = self.root_for()
        with self.assertRaises(GraphError) as refused:
            project_files.git_info(root)
        self.assertIn("refusing", str(refused.exception))
        self.assertFalse(marker.exists())

    @unittest.skipUnless(GIT, "git is required")
    def test_git_dir_to_file_replacement_hides_secret(self):
        run_git(["init"], self.folder)
        run_git(["config", "user.email", "t@t"], self.folder)
        run_git(["config", "user.name", "t"], self.folder)
        bundle = self.folder / "bundle"
        bundle.mkdir()
        (bundle / ".env").write_text("DIR_SECRET=xyz\n", encoding="utf-8")
        (bundle / "keep.txt").write_text("keep\n", encoding="utf-8")
        run_git(["add", "."], self.folder)
        run_git(["commit", "-m", "init"], self.folder)
        shutil.rmtree(bundle)
        (self.folder / "bundle").write_text("public file now\n", encoding="utf-8")
        run_git(["add", "bundle"], self.folder)
        self.write_page(f"file:: {self.folder}\n")
        root = self.root_for()
        info = project_files.git_info(root)
        self.assertIn("public file now", info["diff"])
        self.assertNotIn("DIR_SECRET", info["diff"])
        self.assertNotIn(".env", info["diff"])
        # Reverse direction: a tracked file replaced by a directory whose new
        # secret file is untracked must not leak either.
        run_git(["commit", "-m", "replace"], self.folder)
        (self.folder / "bundle").unlink()
        bundle.mkdir()
        (bundle / ".env").write_text("NEW_SECRET=abc\n", encoding="utf-8")
        info = project_files.git_info(root)
        self.assertNotIn("NEW_SECRET", info["diff"])

    def test_protected_child_inspects_all_pi_occurrences(self):
        root = Path("/repo")
        self.assertTrue(project_files.is_excluded(
            root, Path(".pi/extensions/pkg/.pi/public.txt")))
        self.assertTrue(project_files.is_excluded(
            root, Path(".pi/auth/x")))
        self.assertFalse(project_files.is_excluded(
            root, Path(".pi/public.txt")))
        self.assertFalse(project_files.is_excluded(
            root, Path("src/main.py")))

    def test_root_open_rejects_swapped_symlink_ancestor(self):
        base = Path(self.temp.name) / "race"
        leaf = base / "a" / "b" / "c"
        leaf.mkdir(parents=True)
        (leaf / "f.txt").write_text("data\n", encoding="utf-8")
        fd = project_files._open_root_fd(leaf)
        # The pinned fd keeps referring to the original directory object
        # even after it is unlinked: identity, not link count, is stable.
        pinned = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
        other = base / "other"
        other.mkdir()
        (other / "f.txt").write_text("evil\n", encoding="utf-8")
        middle = base / "a" / "b"
        # Swap the ancestor directory for a symlink after the first open.
        import shutil as _shutil
        _shutil.rmtree(middle)
        middle.symlink_to(other, target_is_directory=True)
        try:
            # The previously pinned fd still refers to the original tree.
            current = os.fstat(fd)
            self.assertEqual((current.st_dev, current.st_ino), pinned)
            # Any fresh acquisition through the swapped ancestor fails.
            with self.assertRaises(GraphError):
                project_files._open_root_fd(leaf)
            with self.assertRaises(GraphError):
                project_files.read_file(base, "a/b/c/f.txt")
        finally:
            os.close(fd)
            middle.unlink()
            middle.mkdir()
            (middle / "c").mkdir()

    @unittest.skipUnless(GIT and shutil.which("sh") and shutil.which("sleep"),
                        "git and sh/sleep are required")
    def test_sigterm_kills_git_group_and_exits_nonzero(self):
        run_git(["init"], self.folder)
        run_git(["config", "user.email", "t@t"], self.folder)
        run_git(["config", "user.name", "t"], self.folder)
        (self.folder / "a.txt").write_text("a\n", encoding="utf-8")
        run_git(["add", "a.txt"], self.folder)
        run_git(["commit", "-m", "init"], self.folder)
        self.write_page(f"file:: {self.folder}\n")
        bindir = Path(self.temp.name) / "fakebin"
        bindir.mkdir()
        pidfile = Path(self.temp.name) / "fakegit.pid"
        fake = bindir / "git"
        fake.write_text(f"#!/bin/sh\necho $$ > {pidfile}\nexec sleep 30\n",
                        encoding="utf-8")
        fake.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "/usr/bin:/bin")
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "project_planner.py"),
             "--graph", str(self.graph), "files-git"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env)
        try:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps({"path": "pages/Project.md"}))
            proc.stdin.close()
            deadline = time.monotonic() + 10
            while not pidfile.exists() and time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertTrue(pidfile.exists(), "fake git was never spawned")
            grandchild = int(pidfile.read_text().strip())
            proc.send_signal(signal.SIGTERM)
            code = proc.wait(timeout=10)
            _, stderr = proc.communicate(timeout=5)
            self.assertEqual(code, 1)
            self.assertIn("aborted", stderr)
            # The git descendant in its own process group must be gone, not
            # orphaned and sleeping.
            gone_by = time.monotonic() + 5
            while time.monotonic() < gone_by:
                try:
                    os.kill(grandchild, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            with self.assertRaises(ProcessLookupError):
                os.kill(grandchild, 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_listing_bounded_fds_under_low_rlimit(self):
        width, per = 400, 2
        for num in range(width):
            sub = self.folder / f"d{num:03d}"
            sub.mkdir()
            for seq in range(per):
                (sub / f"f{seq}.txt").write_text("x\n", encoding="utf-8")
        self.write_page(f"file:: {self.folder}\n")
        root = self.root_for()
        old = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, old[1]))
        try:
            listing = project_files.list_files(root)
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, old)
        self.assertEqual(len(listing["entries"]), width * per)
        self.assertFalse(listing["truncated"])
        self.assertIn("d000/f0.txt",
                      [e["path"] for e in listing["entries"]])

    def test_existing_page_semantics_unchanged(self):
        self.write_page("- TODO keep\n")
        current = project_planner.read_page(self.graph, "pages/Project.md")
        updated = project_planner.update_page(
            self.graph, "pages/Project.md", current["revision"], "- DONE keep\n")
        self.assertEqual(updated["content"], "- DONE keep\n")


if __name__ == "__main__":
    unittest.main()
