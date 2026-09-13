import importlib.util
import signal
import subprocess
import unittest
from pathlib import Path
from unittest.mock import call, patch


ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "scripts" / "screen_capture.py"
SPEC = importlib.util.spec_from_file_location("screen_capture", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise ImportError(f"could not load {MODULE_PATH}")
screen_capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(screen_capture)


def png():
    """The smallest structurally valid PNG accepted by the helper."""
    import struct

    ihdr = struct.pack(">I4sIIBBBBB", 13, b"IHDR", 1, 1, 8, 0, 0, 0, 0) + b"\x00" * 4
    iend = struct.pack(">I4sI", 0, b"IEND", 0)
    return b"\x89PNG\r\n\x1a\n" + ihdr + iend


class FakeProcess:
    def __init__(self, output=(b"", b""), communicate_error=None, waits=None,
                 returncode=0, pid=4242):
        self.pid = pid
        self.returncode = returncode
        self.output = output
        self.communicate_error = communicate_error
        self.waits = list(waits or [])
        self.alive = communicate_error is not None
        self.communicate_calls = []

    def poll(self):
        return None if self.alive else self.returncode

    def communicate(self, timeout=None):
        self.communicate_calls.append(timeout)
        if self.communicate_error is not None:
            error = self.communicate_error
            self.communicate_error = None
            raise error
        self.alive = False
        return self.output

    def wait(self, timeout=None):
        if self.waits:
            result = self.waits.pop(0)
            if isinstance(result, BaseException):
                raise result
            self.alive = False
            return result
        self.alive = False
        return self.returncode


class ScreenCaptureProcessTests(unittest.TestCase):
    def test_geometry_mode_runs_only_grim_with_the_exact_geometry(self):
        capture = FakeProcess(output=(png(), b""), pid=1002)
        with patch.object(screen_capture.shutil, "which", side_effect={
                "grim": "/usr/bin/grim", "magick": None, "convert": None,
                "slurp": self.fail}.get), \
                patch.object(screen_capture.subprocess, "Popen",
                             return_value=capture) as popen:
            value = screen_capture.capture("-10,+20 640x480")

        self.assertEqual(value["images"][0]["mimeType"], "image/png")
        popen.assert_called_once_with(
            ["/usr/bin/grim", "-g", "-10,20 640x480", "-t", "png", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )

    def test_invalid_supplied_geometry_is_rejected_before_any_process(self):
        with patch.object(screen_capture.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "invalid capture geometry"):
                screen_capture.capture("0,0 0x20")
        popen.assert_not_called()

    def test_success_uses_a_private_process_group(self):
        selection = FakeProcess(output=(b"-10,+20 640x480\n", b""), pid=1001)
        capture = FakeProcess(output=(png(), b""), pid=1002)
        with patch.object(screen_capture.shutil, "which", side_effect={
                "slurp": "/usr/bin/slurp", "grim": "/usr/bin/grim",
                "magick": None, "convert": None}.get), \
                patch.object(screen_capture.subprocess, "Popen",
                             side_effect=[selection, capture]) as popen:
            value = screen_capture.capture()

        self.assertEqual(value["images"][0]["mimeType"], "image/png")
        self.assertEqual(
            popen.call_args_list,
            [
                call(["/usr/bin/slurp", "-d"], stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, start_new_session=True),
                call(["/usr/bin/grim", "-g", "-10,20 640x480", "-t", "png", "-"],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     start_new_session=True),
            ],
        )
        self.assertEqual(selection.communicate_calls, [screen_capture.TIMEOUT])
        self.assertEqual(capture.communicate_calls, [screen_capture.TIMEOUT])

    def test_invalid_geometry_does_not_start_grim(self):
        selection = FakeProcess(output=(b"not geometry\n", b""))
        with patch.object(screen_capture.shutil, "which", side_effect={
                "slurp": "/usr/bin/slurp", "grim": "/usr/bin/grim",
                "magick": None, "convert": None}.get), \
                patch.object(screen_capture.subprocess, "Popen",
                             return_value=selection) as popen:
            with self.assertRaisesRegex(RuntimeError, "invalid geometry"):
                screen_capture.capture()
        popen.assert_called_once()

    def test_selection_cancelled_includes_slurp_stderr(self):
        selection = FakeProcess(output=(b"", b"selection cancelled\n"), returncode=1)
        with patch.object(screen_capture.shutil, "which", side_effect={
                "slurp": "/usr/bin/slurp", "grim": "/usr/bin/grim",
                "magick": None, "convert": None}.get), \
                patch.object(screen_capture.subprocess, "Popen",
                             return_value=selection):
            with self.assertRaisesRegex(screen_capture.CaptureCancelled,
                                        "selection cancelled"):
                screen_capture.capture()

    def test_grim_failure_includes_stderr(self):
        selection = FakeProcess(output=(b"0,0 10x20\n", b""))
        capture = FakeProcess(output=(b"", b"grim: failed\n"), returncode=1)
        with patch.object(screen_capture.shutil, "which", side_effect={
                "slurp": "/usr/bin/slurp", "grim": "/usr/bin/grim",
                "magick": None, "convert": None}.get), \
                patch.object(screen_capture.subprocess, "Popen",
                             side_effect=[selection, capture]):
            with self.assertRaisesRegex(RuntimeError, "grim: failed"):
                screen_capture.capture()

    def test_cancellation_terminates_the_capture_process_group(self):
        process = FakeProcess(
            communicate_error=screen_capture.CaptureCancelled("cancelled"))
        with patch.object(screen_capture.subprocess, "Popen", return_value=process), \
                patch.object(screen_capture.os, "killpg") as killpg:
            with self.assertRaises(screen_capture.CaptureCancelled):
                screen_capture._run_capture_process(["slurp", "-d"])

        killpg.assert_called_once_with(process.pid, signal.SIGTERM)
        self.assertFalse(process.alive)

    def test_cleanup_does_not_signal_a_reaped_pid(self):
        process = FakeProcess()
        with patch.object(screen_capture.os, "killpg") as killpg:
            screen_capture._terminate_process_group(process)
        killpg.assert_not_called()

    def test_timeout_escalates_from_sigterm_to_sigkill_and_reaps(self):
        timeout = subprocess.TimeoutExpired(["grim"], screen_capture.TIMEOUT)
        process = FakeProcess(communicate_error=timeout, waits=[timeout, 0])
        with patch.object(screen_capture.subprocess, "Popen", return_value=process), \
                patch.object(screen_capture.os, "killpg") as killpg:
            with self.assertRaisesRegex(RuntimeError, "screen capture timed out"):
                screen_capture._run_capture_process(["grim", "-g", "0,0 1x1", "-t", "png", "-"],
                                                     timeout=screen_capture.TIMEOUT)

        self.assertEqual(
            killpg.call_args_list,
            [call(process.pid, signal.SIGTERM),
             call(process.pid, signal.SIGKILL)],
        )
        self.assertFalse(process.alive)

    def test_main_maps_capture_cancellation_to_exit_130(self):
        with patch.object(
                screen_capture, "capture",
                side_effect=screen_capture.CaptureCancelled("cancelled")):
            self.assertEqual(screen_capture.main([]), 130)


if __name__ == "__main__":
    unittest.main()
