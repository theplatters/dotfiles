import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest import mock

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "palette_files.py"
sys.path.insert(0, str(ROOT / "scripts"))
import palette_files


class PaletteFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_helper(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *map(str, args)],
            text=True, capture_output=True,
        )

    def test_matches_all_case_insensitive_path_tokens_and_uri(self):
        path = self.root / "Notes & Café" / "My Plan.txt"
        path.parent.mkdir()
        path.touch()
        completed = self.run_helper("--root", self.root, "--query", "CAFÉ plan")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        item = json.loads(completed.stdout)["results"][0]
        self.assertEqual(item["name"], "My Plan.txt")
        self.assertEqual(item["path"], str(path))
        self.assertEqual(item["uri"], path.as_uri())

    def test_ignores_hidden_generated_and_symlink_entries(self):
        (self.root / ".hidden").mkdir()
        (self.root / ".hidden" / "needle.txt").touch()
        for name in ("node_modules", "target", "build", "dist", "__pycache__"):
            (self.root / name).mkdir()
            (self.root / name / "needle.txt").touch()
        visible = self.root / "needle.txt"
        visible.touch()
        if hasattr(os, "symlink"):
            (self.root / "link.txt").symlink_to(visible)
            (self.root / "link-dir").symlink_to(self.root, target_is_directory=True)
        result = json.loads(self.run_helper("--root", self.root, "--query", "needle").stdout)
        self.assertEqual([item["name"] for item in result["results"]], ["needle.txt"])

    def test_limit_and_empty_query(self):
        for name in ("a.txt", "b.txt", "c.txt"):
            (self.root / name).touch()
        limited = json.loads(self.run_helper("--root", self.root, "--query", ".txt", "--limit", "2").stdout)
        self.assertEqual(len(limited["results"]), 2)
        self.assertTrue(limited["truncated"])
        empty = self.run_helper("--root", self.root / "missing", "--query", " ")
        self.assertEqual(empty.returncode, 0)
        self.assertEqual(json.loads(empty.stdout), {"results": [], "truncated": False})

    def test_zero_and_negative_limits(self):
        (self.root / "needle.txt").touch()
        result = palette_files.palette_files("needle", self.root, 0)
        self.assertEqual(result["results"], [])
        self.assertTrue(result["truncated"])
        with self.assertRaises(ValueError):
            palette_files.palette_files("", self.root, -1)

    def test_environment_root_and_maximum_limit(self):
        for number in range(palette_files.MAX_RESULTS + 1):
            (self.root / f"needle-{number}.txt").touch()
        with mock.patch.dict(os.environ, {"PALETTE_FILE_ROOT": str(self.root)}):
            result = palette_files.palette_files("needle", limit=10_000)
        self.assertEqual(len(result["results"]), palette_files.MAX_RESULTS)
        self.assertTrue(result["truncated"])

    def test_time_budget_is_checked_for_nonmatching_files(self):
        for name in ("first.txt", "second.txt"):
            (self.root / name).touch()
        # The first call starts the deadline; the second is the per-entry
        # check.  No matching result is needed to trigger the cutoff.
        with mock.patch.object(palette_files.time, "monotonic", side_effect=[0, 2]):
            result, errors = palette_files._search("not-present", self.root)
        self.assertEqual(errors, [])
        self.assertEqual(result["results"], [])
        self.assertTrue(result["truncated"])

    def test_entry_budget_bounds_nonmatching_files(self):
        for name in ("first.txt", "second.txt"):
            (self.root / name).touch()
        with mock.patch.object(palette_files.time, "monotonic", return_value=0):
            with mock.patch.object(palette_files, "ENTRY_BUDGET", 1):
                result, errors = palette_files._search("not-present", self.root)
        self.assertEqual(errors, [])
        self.assertEqual(result["results"], [])
        self.assertTrue(result["truncated"])

    def test_inaccessible_subdirectory_returns_partial_success(self):
        def walk(_root, topdown, followlinks, onerror):
            onerror(PermissionError("private"))
            yield str(self.root), [], ["visible.txt"]

        (self.root / "visible.txt").touch()
        stdout = StringIO()
        stderr = StringIO()
        with mock.patch.object(palette_files.os, "walk", walk):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = palette_files.main(
                    ["--root", str(self.root), "--query", "visible"]
                )
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(stdout.getvalue())["truncated"])
        self.assertIn("warning:", stderr.getvalue())

    def test_invalid_root_is_an_error(self):
        completed = self.run_helper("--root", self.root / "missing", "--query", "file")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("error:", completed.stderr)


if __name__ == "__main__":
    unittest.main()
