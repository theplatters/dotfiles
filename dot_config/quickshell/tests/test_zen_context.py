import importlib.util
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "scripts" / "zen_context.py"
SPEC = importlib.util.spec_from_file_location("zen_context", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise ImportError(f"could not load {MODULE_PATH}")
zen_context = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(zen_context)


def focused(address="0xabc123", pid=4242, klass="app.zen_browser.zen",
            title="Example — Zen Browser"):
    return json.dumps({
        "address": address,
        "pid": pid,
        "class": klass,
        "initialClass": klass,
        "title": title,
    }).encode("utf-8")


def completed(stdout, returncode=0):
    return subprocess.CompletedProcess(["hyprctl"], returncode, stdout, b"")


class PushTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "zen-context.json"
        self.addCleanup(lambda: os.chmod(self.tmp.name, 0o700))

    def test_valid_push_writes_private_record(self):
        with patch.object(zen_context.subprocess, "run",
                          return_value=completed(focused())) as run:
            value = zen_context.cmd_push(argparse_ns(
                url="https://example.com/page", title=None, file=str(self.path)))
        self.assertTrue(value["ok"])
        self.assertEqual(value["window_id"], "0xabc123")
        self.assertEqual(value["pid"], 4242)
        record = json.loads(self.path.read_text())
        self.assertEqual(record["window_id"], "0xabc123")
        self.assertEqual(record["pid"], 4242)
        self.assertEqual(record["url"], "https://example.com/page")
        self.assertEqual(record["title"], "Example — Zen Browser")
        self.assertIsInstance(record["updated_at_ms"], int)
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["hyprctl", "activewindow", "-j"])

    def test_explicit_title_overrides_window_title(self):
        with patch.object(zen_context.subprocess, "run",
                          return_value=completed(focused())):
            zen_context.cmd_push(argparse_ns(
                url="https://example.com/page", title="Custom", file=str(self.path)))
        self.assertEqual(json.loads(self.path.read_text())["title"], "Custom")

    def test_non_http_url_is_rejected_before_hyprctl(self):
        for bad in ("ftp://example.com", "example.com", "javascript:alert(1)",
                    "https://example.com/\x01"):
            with self.subTest(url=bad):
                with patch.object(zen_context.subprocess, "run") as run:
                    with self.assertRaises(zen_context.ZenContextError):
                        zen_context.cmd_push(argparse_ns(
                            url=bad, title=None, file=str(self.path)))
                run.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_non_zen_focus_is_rejected(self):
        with patch.object(zen_context.subprocess, "run",
                          return_value=completed(focused(klass="kitty"))):
            with self.assertRaisesRegex(zen_context.ZenContextError, "not Zen"):
                zen_context.cmd_push(argparse_ns(
                    url="https://example.com", title=None, file=str(self.path)))
        self.assertFalse(self.path.exists())

    def test_missing_address_or_pid_is_rejected(self):
        for payload in ({"class": "zen", "pid": 1},
                        {"class": "zen", "address": "0x1"}):
            with self.subTest(payload=payload):
                with patch.object(zen_context.subprocess, "run",
                                  return_value=completed(
                                      json.dumps(payload).encode())):
                    with self.assertRaises(zen_context.ZenContextError):
                        zen_context.cmd_push(argparse_ns(
                            url="https://example.com", title=None,
                            file=str(self.path)))

    def test_public_parent_directory_is_rejected(self):
        os.chmod(self.tmp.name, 0o755)
        with patch.object(zen_context.subprocess, "run",
                          return_value=completed(focused())):
            with self.assertRaisesRegex(zen_context.ZenContextError,
                                        "private"):
                zen_context.cmd_push(argparse_ns(
                    url="https://example.com", title=None, file=str(self.path)))
        self.assertFalse(self.path.exists())

    def test_symlinked_context_file_is_rejected(self):
        target = Path(self.tmp.name) / "target.json"
        target.write_text("{}")
        self.path.symlink_to(target)
        with patch.object(zen_context.subprocess, "run",
                          return_value=completed(focused())):
            with self.assertRaisesRegex(zen_context.ZenContextError,
                                        "symlink"):
                zen_context.cmd_push(argparse_ns(
                    url="https://example.com", title=None, file=str(self.path)))
        self.assertTrue(self.path.is_symlink())

    def test_hyprctl_failure_is_bounded(self):
        with patch.object(zen_context.subprocess, "run",
                          return_value=completed(b"boom", returncode=1)):
            with self.assertRaisesRegex(zen_context.ZenContextError,
                                        "activewindow failed"):
                zen_context.cmd_push(argparse_ns(
                    url="https://example.com", title=None, file=str(self.path)))

    def test_hyprctl_missing_binary_is_bounded(self):
        with patch.object(zen_context.subprocess, "run",
                          side_effect=FileNotFoundError):
            with self.assertRaisesRegex(zen_context.ZenContextError,
                                        "hyprctl not found"):
                zen_context.cmd_push(argparse_ns(
                    url="https://example.com", title=None, file=str(self.path)))


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "zen-context.json"

    def run_capture(self, responses):
        with patch.object(zen_context.subprocess, "run",
                          side_effect=responses) as run, \
                patch.object(zen_context.time, "sleep"):
            value = zen_context.cmd_capture(argparse_ns(file=str(self.path)))
        return value, run

    def test_capture_publishes_the_fresh_clipboard_url(self):
        value, run = self.run_capture([
            completed(focused()),                    # hyprctl activewindow
            completed(b""),                          # wl-copy --clear
            completed(b"ok"),                        # dispatch Ctrl+L
            completed(b"ok"),                        # dispatch Ctrl+C
            completed(b"ok"),                        # dispatch Escape
            completed(b"https://example.com/new"),   # wl-paste after
        ])
        self.assertTrue(value["captured"])
        self.assertEqual(value["url"], "https://example.com/new")
        record = json.loads(self.path.read_text())
        self.assertEqual(record["url"], "https://example.com/new")
        self.assertEqual(record["window_id"], "0xabc123")
        clear = run.call_args_list[1][0][0]
        self.assertEqual(clear, ["wl-copy", "--clear"])
        dispatch = run.call_args_list[2][0][0]
        self.assertEqual(dispatch[0], "hyprctl")
        self.assertIn('key = "l"', dispatch[2])
        self.assertIn('address:0xabc123', dispatch[2])

    def test_capture_refuses_empty_clipboard_after_copy(self):
        with self.assertRaisesRegex(zen_context.ZenContextError,
                                    "clipboard empty"):
            self.run_capture([
                completed(focused()),
                completed(b""),
                completed(b"ok"),
                completed(b"ok"),
                completed(b"ok"),
                completed(b""),
            ])
        self.assertFalse(self.path.exists())

    def test_capture_refuses_non_http_clipboard(self):
        with self.assertRaises(zen_context.ZenContextError):
            self.run_capture([
                completed(focused()),
                completed(b""),
                completed(b"ok"),
                completed(b"ok"),
                completed(b"ok"),
                completed(b"not a url"),
            ])
        self.assertFalse(self.path.exists())

    def test_capture_rejects_non_zen_focus_before_any_dispatch(self):
        with self.assertRaisesRegex(zen_context.ZenContextError, "not Zen"):
            self.run_capture([completed(focused(klass="kitty"))])
        self.assertFalse(self.path.exists())

    def test_capture_rejects_injection_shaped_address(self):
        bad = focused(address='0x1") os.execute("x")', pid=1)
        with self.assertRaises(zen_context.ZenContextError):
            self.run_capture([completed(bad)])

    def test_capture_rejects_rejected_dispatch(self):
        with self.assertRaisesRegex(zen_context.ZenContextError,
                                    "rejected the shortcut"):
            self.run_capture([
                completed(focused()),
                completed(b""),
                completed(b"error: nope", returncode=1),
            ])
        self.assertFalse(self.path.exists())


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "zen-context.json"

    def write(self, record):
        self.path.write_text(json.dumps(record))

    def test_missing_record_reports_title_fallback(self):
        value = zen_context.cmd_status(argparse_ns(file=str(self.path)))
        self.assertFalse(value["exists"])
        self.assertFalse(value["fresh"])
        self.assertIn("title fallback", value["reason"])

    def test_fresh_matching_record_is_bound(self):
        import time
        self.write({"window_id": "0xabc123", "pid": 4242,
                    "url": "https://example.com",
                    "updated_at_ms": int(time.time() * 1000)})
        with patch.object(zen_context.subprocess, "run",
                          return_value=completed(focused())):
            value = zen_context.cmd_status(argparse_ns(file=str(self.path)))
        self.assertTrue(value["exists"])
        self.assertTrue(value["fresh"])
        self.assertTrue(value["matches_focus"])
        self.assertIn("bound", value["reason"])

    def test_stale_record_is_not_fresh(self):
        self.write({"window_id": "0xabc123", "pid": 4242,
                    "url": "https://example.com", "updated_at_ms": 1000})
        with patch.object(zen_context.subprocess, "run",
                          return_value=completed(focused())):
            value = zen_context.cmd_status(argparse_ns(file=str(self.path)))
        self.assertFalse(value["fresh"])
        self.assertIn("stale", value["reason"])

    def test_record_for_another_window_is_mismatch(self):
        import time
        self.write({"window_id": "0xother", "pid": 999,
                    "url": "https://example.com",
                    "updated_at_ms": int(time.time() * 1000)})
        with patch.object(zen_context.subprocess, "run",
                          return_value=completed(focused())):
            value = zen_context.cmd_status(argparse_ns(file=str(self.path)))
        self.assertTrue(value["fresh"])
        self.assertFalse(value["matches_focus"])
        self.assertIn("another window", value["reason"])

    def test_malformed_record_is_reported(self):
        self.path.write_text("{not json")
        value = zen_context.cmd_status(argparse_ns(file=str(self.path)))
        self.assertTrue(value["exists"])
        self.assertIn("malformed", value["reason"])

    def test_status_never_writes(self):
        value = zen_context.cmd_status(argparse_ns(file=str(self.path)))
        self.assertFalse(value["exists"])
        self.assertFalse(self.path.exists())


class ClearTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "zen-context.json"

    def test_clear_removes_and_is_idempotent(self):
        self.path.write_text("{}")
        value = zen_context.cmd_clear(argparse_ns(file=str(self.path)))
        self.assertTrue(value["removed"])
        self.assertFalse(self.path.exists())
        again = zen_context.cmd_clear(argparse_ns(file=str(self.path)))
        self.assertFalse(again["removed"])

    def test_clear_refuses_symlink(self):
        target = Path(self.tmp.name) / "target.json"
        target.write_text("{}")
        self.path.symlink_to(target)
        with self.assertRaisesRegex(zen_context.ZenContextError, "symlink"):
            zen_context.cmd_clear(argparse_ns(file=str(self.path)))
        self.assertTrue(target.exists())


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "zen-context.json"

    def test_success_prints_single_line_json(self):
        import contextlib
        import io
        out = io.StringIO()
        with patch.object(zen_context.subprocess, "run",
                          return_value=completed(focused())), \
                contextlib.redirect_stdout(out):
            code = zen_context.main(["push", "--file", str(self.path),
                                     "--url", "https://example.com"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertEqual(text.count("\n"), 1)
        self.assertEqual(json.loads(text)["ok"], True)

    def test_invalid_url_exits_one_with_bounded_error(self):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = zen_context.main(["push", "--file", str(self.path),
                                     "--url", "ftp://example.com"])
        self.assertEqual(code, 1)
        self.assertTrue(err.getvalue().startswith("error: "))
        self.assertNotIn("ftp://example.com\n", err.getvalue())

    def test_status_and_clear_accept_file_after_subcommand(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = zen_context.main(["status", "--file", str(self.path)])
            self.assertEqual(code, 0)
            code = zen_context.main(["clear", "--file", str(self.path)])
            self.assertEqual(code, 0)


def argparse_ns(**kwargs):
    class NS:
        pass
    ns = NS()
    for key, value in kwargs.items():
        setattr(ns, key, value)
    return ns


if __name__ == "__main__":
    unittest.main()
