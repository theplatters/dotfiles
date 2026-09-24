"""Tests for the Zen native-messaging host (marker-verified, fail-closed).

All Hyprland I/O is faked by injecting ``get_active`` samplers: no live
``hyprctl``, no live browser, no network, no profile reads. The real atomic
``0600`` writer is used so file mode, size bounds, and symlink/parent checks
are exercised for real. Stderr is captured to prove URLs never leak into
diagnostics.
"""

import importlib.util
import io
import json
import os
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).parents[1]
HOST_PATH = (
    ROOT
    / "services"
    / "agent-orchestrator"
    / "integrations"
    / "zen"
    / "qs-zen-native-host.py"
)

def _load_host():
    old_flag = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location(
            "qs_zen_native_host_under_test", str(HOST_PATH)
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = old_flag


host = _load_host()

NOW_MS = 1710000000000
ADDR = "0xabc123"
PID = 4242
MARKER = "[qs-zen-a1b2c3d4] "
OTHER_MARKER = "[qs-zen-ffffffff] "
URL = "https://example.com/page?q=1"
TITLE = "Example Page"


def _active(window_id=ADDR, pid=PID, cls="zen", title=MARKER + TITLE):
    return {
        "window_id": window_id,
        "pid": pid,
        "class": cls,
        "initial_class": cls,
        "title": title,
    }


def _update(**over):
    msg = {
        "type": "update",
        "marker": MARKER,
        "url": URL,
        "title": TITLE,
        "updatedAtMs": NOW_MS,
    }
    msg.update(over)
    return msg


def _read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _frame_bytes(obj) -> bytes:
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


class FramingTests(unittest.TestCase):
    def test_write_then_read_roundtrip(self):
        buf = io.BytesIO()
        self.assertTrue(host.write_frame(buf, {"type": "update", "n": 1}))
        buf.seek(0)
        msg, eof, malformed = host.read_frame(buf)
        self.assertFalse(eof)
        self.assertFalse(malformed)
        self.assertEqual(msg, {"type": "update", "n": 1})

    def test_clean_eof_at_boundary(self):
        msg, eof, malformed = host.read_frame(io.BytesIO(b""))
        self.assertTrue(eof)
        self.assertFalse(malformed)
        self.assertIsNone(msg)

    def test_partial_header_is_malformed_not_eof(self):
        msg, eof, malformed = host.read_frame(io.BytesIO(b"\x05\x00"))
        self.assertFalse(eof)
        self.assertTrue(malformed)
        self.assertIsNone(msg)

    def test_oversize_frame_rejected(self):
        buf = io.BytesIO(struct.pack("<I", host.FRAME_LIMIT + 1))
        msg, eof, malformed = host.read_frame(buf)
        self.assertTrue(malformed)
        self.assertFalse(eof)

    def test_zero_length_frame_rejected(self):
        buf = io.BytesIO(struct.pack("<I", 0))
        msg, eof, malformed = host.read_frame(buf)
        self.assertTrue(malformed)

    def test_truncated_body_is_malformed(self):
        raw = json.dumps({"a": 1}).encode()
        buf = io.BytesIO(struct.pack("<I", len(raw) + 10) + raw)
        msg, eof, malformed = host.read_frame(buf)
        self.assertTrue(malformed)
        self.assertFalse(eof)

    def test_non_json_body_is_malformed(self):
        body = b"not json{"
        buf = io.BytesIO(struct.pack("<I", len(body)) + body)
        msg, eof, malformed = host.read_frame(buf)
        self.assertTrue(malformed)

    def test_read_exact_collects_split_chunks(self):
        class Split(io.BytesIO):
            def read(self, n=-1):
                # Force 1-byte reads to exercise the read-exact loop.
                return super().read(1 if n is None or n > 1 else n)

        payload = {"hello": "world"}
        buf = Split(_frame_bytes(payload))
        msg, eof, malformed = host.read_frame(buf)
        self.assertFalse(malformed)
        self.assertEqual(msg, payload)

    def test_write_frame_never_exceeds_bound(self):
        buf = io.BytesIO()
        self.assertFalse(host.write_frame(buf, {"big": "x" * (host.FRAME_LIMIT + 1)}))
        self.assertEqual(buf.getvalue(), b"")


class SecurityValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "zen-context.json")
        self.sampler = lambda: _active()

    def tearDown(self):
        self.temp.cleanup()

    def handle(self, msg, sampler=None, now=NOW_MS):
        err = io.StringIO()
        with redirect_stderr(err):
            outcome = host.handle_message(
                msg, self.path, get_active=(sampler or self.sampler), now_ms=now
            )
        return outcome, err.getvalue()

    def test_non_dict_message_invalidates(self):
        for bad in (None, [], "x", 42):
            if Path(self.path).exists():
                Path(self.path).unlink()
            outcome, _ = self.handle(bad)
            self.assertEqual(outcome, "invalidated")
            self.assertEqual(_read_json(self.path), {})

    def test_unknown_type_invalidates(self):
        outcome, _ = self.handle({"type": "nope", "marker": MARKER})
        self.assertEqual(outcome, "invalidated")
        self.assertEqual(_read_json(self.path), {})

    def test_incognito_flag_invalidates(self):
        outcome, _ = self.handle(_update(incognito=True))
        self.assertEqual(outcome, "invalidated")
        self.assertEqual(_read_json(self.path), {})

    def test_bad_markers_invalidate(self):
        for bad in (None, "", "nope", "[qs-zen-XYZ] ",
                    "[qs-zen-a1b2c3d4]", " [qs-zen-a1b2c3d4] ",
                    "[qs-zen-a1b2c3d4]  ", "[QS-ZEN-A1B2C3D4] "):
            with self.subTest(marker=bad):
                if Path(self.path).exists():
                    Path(self.path).unlink()
                outcome, _ = self.handle(_update(marker=bad))
                self.assertEqual(outcome, "invalidated")
                self.assertEqual(_read_json(self.path), {})

    def test_non_http_urls_invalidate(self):
        for bad in (None, "", "example.com", "ftp://x.test/",
                    "javascript:alert(1)", "file:///etc/passwd",
                    "https://x.test/\x01", "https://x.test/\x00y"):
            with self.subTest(url=bad):
                if Path(self.path).exists():
                    Path(self.path).unlink()
                outcome, _ = self.handle(_update(url=bad))
                self.assertEqual(outcome, "invalidated")
                self.assertEqual(_read_json(self.path), {})

    def test_oversize_url_invalidates(self):
        outcome, _ = self.handle(_update(url="https://x.test/" + "y" * 3000))
        self.assertEqual(outcome, "invalidated")
        self.assertEqual(_read_json(self.path), {})

    def test_timestamps_reject_delayed_and_future(self):
        cases = [
            None, "now", 0, -5, True,
            NOW_MS - host.FRESHNESS_MS - 1,  # stale
            NOW_MS + host.FUTURE_SKEW_MS + 1,  # future
        ]
        for bad in cases:
            with self.subTest(ts=bad):
                if Path(self.path).exists():
                    Path(self.path).unlink()
                outcome, _ = self.handle(_update(updatedAtMs=bad))
                self.assertEqual(outcome, "invalidated", msg=f"ts={bad}")
                self.assertEqual(_read_json(self.path), {})
        # Boundary freshness still writes.
        outcome, _ = self.handle(_update(updatedAtMs=NOW_MS - host.FRESHNESS_MS))
        self.assertEqual(outcome, "wrote")

    def test_symlinked_file_is_refused(self):
        target = Path(self.temp.name) / "target.json"
        target.write_text("{}")
        Path(self.path).symlink_to(target)
        outcome, _ = self.handle(_update())
        # Write refused: the symlink itself must survive untouched.
        self.assertEqual(outcome, "invalidated")
        self.assertTrue(Path(self.path).is_symlink())
        self.assertEqual(target.read_text(), "{}")

    def test_public_parent_is_refused(self):
        os.chmod(self.temp.name, 0o755)
        try:
            outcome, _ = self.handle(_update())
            self.assertEqual(outcome, "invalidated")
            self.assertFalse(Path(self.path).exists())
        finally:
            os.chmod(self.temp.name, 0o700)

    def test_symlinked_parent_is_refused(self):
        real = Path(self.temp.name) / "real"
        real.mkdir(mode=0o700)
        link = Path(self.temp.name) / "link"
        link.symlink_to(real)
        nested = str(link / "zen-context.json")
        outcome, _ = self.handle(_update())
        # handle() writes to self.path, so exercise the nested path directly.
        err = io.StringIO()
        with redirect_stderr(err):
            ok = host.handle_message(
                _update(), nested, get_active=self.sampler, now_ms=NOW_MS
            )
        self.assertEqual(ok, "invalidated")
        self.assertFalse((real / "zen-context.json").exists())

    def test_diagnostics_never_contain_url_or_title(self):
        secret_url = "https://secret.example/s3cr3t-path?tok=abc123"
        secret_title = "Secret Title 98765"
        err = io.StringIO()
        with redirect_stderr(err):
            host.handle_message(
                _update(url=secret_url, title=secret_title),
                self.path,
                get_active=lambda: None,  # force mismatch path
                now_ms=NOW_MS,
            )
        diagnostics = err.getvalue()
        self.assertNotIn(secret_url, diagnostics)
        self.assertNotIn("s3cr3t-path", diagnostics)
        self.assertNotIn(secret_title, diagnostics)
        # stdout is never used for logs: serve path writes frames only
        # (covered in ServeLoopTests).


class MultiwindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "zen-context.json")

    def tearDown(self):
        self.temp.cleanup()

    def handle(self, msg, active, now=NOW_MS, extra_reads=None):
        reads = [active] + list(extra_reads or [])

        def sampler():
            if len(reads) > 1:
                return reads.pop(0)
            return reads[0]

        err = io.StringIO()
        with redirect_stderr(err):
            outcome = host.handle_message(msg, self.path, get_active=sampler, now_ms=now)
        return outcome

    def test_matching_marker_writes_bound_record_with_mode_0600(self):
        outcome = self.handle(_update(), _active())
        self.assertEqual(outcome, "wrote")
        record = _read_json(self.path)
        self.assertEqual(record["window_id"], ADDR)
        self.assertEqual(record["pid"], PID)
        self.assertEqual(record["url"], URL)
        self.assertEqual(record["title"], TITLE)
        self.assertEqual(record["updated_at_ms"], NOW_MS)
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertLessEqual(os.path.getsize(self.path), host.FILE_LIMIT)

    def test_wrong_window_marker_never_writes_url(self):
        # Focus shows window A's marker; a message carrying window B's marker
        # must invalidate without leaking B's URL anywhere.
        active_a = _active(title=MARKER + "Page A")
        msg_b = _update(marker=OTHER_MARKER, url="https://b.example/secret-b")
        outcome = self.handle(msg_b, active_a)
        self.assertEqual(outcome, "invalidated")
        self.assertEqual(_read_json(self.path), {})

    def test_second_window_matching_marker_binds_its_own_address(self):
        addr_b = "0xdef456"
        active_b = _active(window_id=addr_b, pid=7777, title=OTHER_MARKER + "Page B")
        msg = _update(marker=OTHER_MARKER, url="https://b.example/b",
                      title="Page B")
        outcome = self.handle(msg, active_b)
        self.assertEqual(outcome, "wrote")
        record = _read_json(self.path)
        self.assertEqual(record["window_id"], addr_b)
        self.assertEqual(record["pid"], 7777)
        self.assertNotIn("secret", json.dumps(record))

    def test_non_zen_class_invalidates(self):
        for cls in ("firefox", "kitty", "", None):
            with self.subTest(cls=cls):
                if Path(self.path).exists():
                    Path(self.path).unlink()
                outcome = self.handle(
                    _update(), _active(cls=cls, title=MARKER + TITLE)
                )
                self.assertEqual(outcome, "invalidated")
                self.assertEqual(_read_json(self.path), {})

    def test_title_without_marker_invalidates(self):
        outcome = self.handle(_update(), _active(title="Plain Title"))
        self.assertEqual(outcome, "invalidated")
        self.assertEqual(_read_json(self.path), {})

    def test_title_prefix_must_be_exact(self):
        # Marker elsewhere in the title (not a prefix) is page-title
        # guessing and must fail closed.
        outcome = self.handle(
            _update(), _active(title="Page " + MARKER.strip())
        )
        self.assertEqual(outcome, "invalidated")
        self.assertEqual(_read_json(self.path), {})

    def test_class_matching_is_case_insensitive(self):
        outcome = self.handle(_update(), _active(cls="ZEN"))
        self.assertEqual(outcome, "wrote")

    def test_invalidate_message_writes_empty_object(self):
        Path(self.path).write_text(json.dumps({"window_id": ADDR}))
        outcome = self.handle({"type": "invalidate", "reason": "unsupported-url"},
                              _active())
        self.assertEqual(outcome, "invalidated")
        self.assertEqual(_read_json(self.path), {})


class FocusRaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "zen-context.json")

    def tearDown(self):
        self.temp.cleanup()

    def handle_two_samples(self, before, after):
        calls = [before, after]

        def sampler():
            return calls.pop(0) if len(calls) > 1 else calls[0]

        err = io.StringIO()
        with redirect_stderr(err):
            return host.handle_message(
                _update(), self.path, get_active=sampler, now_ms=NOW_MS
            )

    def test_address_change_between_samples_invalidates(self):
        outcome = self.handle_two_samples(_active(), _active(window_id="0x999"))
        self.assertEqual(outcome, "invalidated")
        self.assertEqual(_read_json(self.path), {})

    def test_pid_change_between_samples_invalidates(self):
        outcome = self.handle_two_samples(_active(), _active(pid=9999))
        self.assertEqual(outcome, "invalidated")
        self.assertEqual(_read_json(self.path), {})

    def test_title_change_between_samples_invalidates(self):
        outcome = self.handle_two_samples(
            _active(), _active(title=MARKER + "Other Tab")
        )
        self.assertEqual(outcome, "invalidated")
        self.assertEqual(_read_json(self.path), {})

    def test_sampler_failure_invalidates(self):
        err = io.StringIO()
        with redirect_stderr(err):
            outcome = host.handle_message(
                _update(), self.path, get_active=lambda: None, now_ms=NOW_MS
            )
        self.assertEqual(outcome, "invalidated")
        self.assertEqual(_read_json(self.path), {})

    def test_after_sample_losing_marker_invalidates(self):
        outcome = self.handle_two_samples(
            _active(), _active(title="Marker vanished")
        )
        self.assertEqual(outcome, "invalidated")
        self.assertEqual(_read_json(self.path), {})


class ServeLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "zen-context.json")
        self._orig_active = host.hypr_active

    def tearDown(self):
        host.hypr_active = self._orig_active
        self.temp.cleanup()

    def _fresh_update(self, **over):
        import time as _time

        msg = _update(updatedAtMs=int(_time.time() * 1000))
        msg.update(over)
        return msg

    def serve(self, stdin_bytes, active):
        host.hypr_active = lambda _h="hyprctl": active
        stdin = io.BytesIO(stdin_bytes)
        stdout = io.BytesIO()
        err = io.StringIO()
        with redirect_stderr(err):
            code = host.serve_forever(self.path, stdin=stdin, stdout=stdout)
        return code, stdout.getvalue(), err.getvalue()

    def test_update_then_eof_writes_then_clears(self):
        payload = _frame_bytes(self._fresh_update())
        code, out, err = self.serve(payload, _active())
        self.assertEqual(code, 0)
        # EOF clears to {} even after a successful write.
        self.assertEqual(_read_json(self.path), {})
        # Stdout carries exactly one ack frame; no log lines.
        buf = io.BytesIO(out)
        msg, eof, malformed = host.read_frame(buf)
        self.assertFalse(malformed)
        self.assertEqual(msg["outcome"], "wrote")
        self.assertTrue(msg["ok"])
        self.assertNotIn(URL, err)
        rest = buf.read()
        self.assertEqual(rest, b"")
        self.assertNotIn(URL, out.decode("utf-8", "replace"))

    def test_malformed_frame_invalidates_and_stays_connected(self):
        body = b"nope{"
        bad = struct.pack("<I", len(body)) + body
        good = _frame_bytes({"type": "invalidate"})
        code, out, err = self.serve(bad + good, _active())
        self.assertEqual(code, 0)
        self.assertEqual(_read_json(self.path), {})
        buf = io.BytesIO(out)
        first, _, m1 = host.read_frame(buf)
        self.assertFalse(m1)
        self.assertFalse(first["ok"])
        second, _, m2 = host.read_frame(buf)
        self.assertFalse(m2)
        self.assertEqual(second["outcome"], "invalidated")

    def test_pure_eof_clears_existing_record(self):
        Path(self.path).write_text(json.dumps(
            {"window_id": ADDR, "pid": PID, "url": URL,
             "updated_at_ms": NOW_MS}))
        code, _, _ = self.serve(b"", _active())
        self.assertEqual(code, 0)
        self.assertEqual(_read_json(self.path), {})

    def test_ack_never_echoes_url(self):
        code, out, _ = self.serve(_frame_bytes(self._fresh_update()), _active())
        self.assertEqual(code, 0)
        self.assertNotIn(URL.encode(), out)


class LaunchArgsTests(unittest.TestCase):
    def test_standalone_file_hyprctl_still_work(self):
        calls = {}
        orig = host.serve_forever

        def fake(path, hyprctl="hyprctl", stdin=None, stdout=None):
            calls["path"] = path
            calls["hyprctl"] = hyprctl
            return 0

        host.serve_forever = fake
        try:
            with tempfile.TemporaryDirectory() as td:
                ctx = str(Path(td) / "zen-context.json")
                err = io.StringIO()
                with redirect_stderr(err):
                    self.assertEqual(host.main(["--file", ctx]), 0)
                self.assertEqual(calls["path"], ctx)
                self.assertEqual(calls["hyprctl"], "hyprctl")
                with redirect_stderr(err):
                    self.assertEqual(
                        host.main(["--file", ctx, "--hyprctl", "/bin/false"]), 0
                    )
                self.assertEqual(calls["hyprctl"], "/bin/false")
                # Firefox positionals pass through alongside --file.
                with redirect_stderr(err):
                    self.assertEqual(
                        host.main([
                            "/tmp/fake-manifest.json",
                            host.EXPECTED_EXTENSION_ID,
                            "--file", ctx,
                        ]),
                        0,
                    )
                self.assertEqual(calls["path"], ctx)
        finally:
            host.serve_forever = orig

    def test_unexpected_extension_id_refused(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = str(Path(td) / "zen-context.json")
            err = io.StringIO()
            with redirect_stderr(err):
                code = host.main(
                    ["/tmp/fake-manifest.json", "evil@example.com",
                     "--file", ctx]
                )
            self.assertEqual(code, 2)
            self.assertFalse(Path(ctx).exists())
            with redirect_stderr(err):
                code = host.main(["evil@example.com", "--file", ctx])
            self.assertEqual(code, 2)
            self.assertFalse(Path(ctx).exists())

    def test_substring_wrapper_ids_refused(self):
        evil_two_arg = f"evil-{host.EXPECTED_EXTENSION_ID}.evil"
        evilsingles = [
            f"evil-{host.EXPECTED_EXTENSION_ID}",
            f"{host.EXPECTED_EXTENSION_ID}.evil",
            f"prefix-{host.EXPECTED_EXTENSION_ID}-suffix",
            f"chrome-extension://evil-{host.EXPECTED_EXTENSION_ID}",
            f"chrome-extension://{host.EXPECTED_EXTENSION_ID}.evil",
            f"chrome-extension://{host.EXPECTED_EXTENSION_ID}/evil-path",
        ]
        with tempfile.TemporaryDirectory() as td:
            ctx = str(Path(td) / "zen-context.json")
            err = io.StringIO()
            with redirect_stderr(err):
                code = host.main(
                    ["/tmp/fake-manifest.json", evil_two_arg,
                     "--file", ctx]
                )
            self.assertEqual(code, 2)
            self.assertFalse(Path(ctx).exists())
            for evil in evilsingles:
                if Path(ctx).exists():
                    Path(ctx).unlink()
                with redirect_stderr(err):
                    code = host.main([evil, "--file", ctx])
                self.assertEqual(code, 2, msg=f"evil={evil!r}")
                self.assertFalse(Path(ctx).exists())

    def test_exact_ids_succeed(self):
        calls = {}
        orig = host.serve_forever

        def fake(path, hyprctl="hyprctl", stdin=None, stdout=None):
            calls["path"] = path
            return 0

        host.serve_forever = fake
        try:
            with tempfile.TemporaryDirectory() as td:
                ctx = str(Path(td) / "zen-context.json")
                err = io.StringIO()
                # Exact single extension id succeeds.
                with redirect_stderr(err):
                    self.assertEqual(
                        host.main([host.EXPECTED_EXTENSION_ID,
                                   "--file", ctx]), 0)
                self.assertEqual(calls["path"], ctx)
                # Exact chrome-extension:// origin succeeds.
                origin = f"chrome-extension://{host.EXPECTED_EXTENSION_ID}"
                with redirect_stderr(err):
                    self.assertEqual(host.main([origin, "--file", ctx]), 0)
                self.assertEqual(calls["path"], ctx)
                # Unknown bare manifest path is still ignored.
                with redirect_stderr(err):
                    self.assertEqual(
                        host.main(["/tmp/fake-manifest.json",
                                   "--file", ctx]), 0)
                self.assertEqual(calls["path"], ctx)
        finally:
            host.serve_forever = orig

    def test_too_many_positionals_refused(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = str(Path(td) / "zen-context.json")
            err = io.StringIO()
            with redirect_stderr(err):
                code = host.main(["a", "b", "c", "--file", ctx])
            self.assertEqual(code, 2)

    def test_subprocess_launch_with_both_positionals_and_file(self):
        # Firefox invokes: host <manifest-path> <extension-id>. Prove the
        # real entry point accepts both plus a temporary --file by driving
        # actual framed stdin through a subprocess.
        with tempfile.TemporaryDirectory() as td:
            ctx = str(Path(td) / "zen-context.json")
            manifest_arg = str(Path(td) / "fake-manifest.json")
            Path(manifest_arg).write_text("{}", encoding="utf-8")
            payload = _frame_bytes({"type": "invalidate"})
            proc = subprocess.Popen(
                [
                    sys.executable, str(HOST_PATH),
                    manifest_arg, host.EXPECTED_EXTENSION_ID,
                    "--file", ctx,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                out, err = proc.communicate(payload, timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
                self.fail(
                    "native host subprocess did not exit "
                    f"(stderr={err.decode('utf-8', 'replace')[-1000:]})"
                )
            self.assertEqual(
                proc.returncode, 0,
                msg=f"stderr={err.decode('utf-8', 'replace')[-2000:]}",
            )
            # Launch succeeded: invalidate acked over frames, file cleared.
            self.assertEqual(_read_json(ctx), {})
            buf = io.BytesIO(out)
            msg, eof, malformed = host.read_frame(buf)
            self.assertFalse(malformed)
            self.assertFalse(eof)
            self.assertEqual(msg.get("outcome"), "invalidated")

    def test_subprocess_evil_wrapper_refused_without_write(self):
        # Real entry point with a substring-wrapper id must refuse (exit
        # 2) before serving: immediate EOF must not create the file.
        evil = f"evil-{host.EXPECTED_EXTENSION_ID}.evil"
        with tempfile.TemporaryDirectory() as td:
            ctx = str(Path(td) / "zen-context.json")
            manifest_arg = str(Path(td) / "fake-manifest.json")
            Path(manifest_arg).write_text("{}", encoding="utf-8")
            proc = subprocess.Popen(
                [
                    sys.executable, str(HOST_PATH),
                    manifest_arg, evil,
                    "--file", ctx,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                out, err = proc.communicate(b"", timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
                self.fail(
                    "native host subprocess did not exit "
                    f"(stderr={err.decode('utf-8', 'replace')[-1000:]})"
                )
            self.assertEqual(
                proc.returncode, 2,
                msg=f"stderr={err.decode('utf-8', 'replace')[-2000:]}",
            )
            self.assertEqual(out, b"")
            self.assertFalse(Path(ctx).exists())


class UnrecoverableFramingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "zen-context.json")
        self._orig_active = host.hypr_active

    def tearDown(self):
        host.hypr_active = self._orig_active
        self.temp.cleanup()

    def serve(self, stdin_bytes, active):
        host.hypr_active = lambda _h="hyprctl": active
        stdin = io.BytesIO(stdin_bytes)
        stdout = io.BytesIO()
        err = io.StringIO()
        with redirect_stderr(err):
            code = host.serve_forever(self.path, stdin=stdin, stdout=stdout)
        return code, stdout.getvalue(), err.getvalue()

    def test_oversized_frame_terminates_without_draining(self):
        # Attacker length + payload bytes + a trailing good frame. A
        # correct host terminates on the length alone (no drain) so the
        # payload is never reparsed as headers: no acks, {} on exit.
        trailing = _frame_bytes({"type": "invalidate"})
        stdin_bytes = (
            struct.pack("<I", host.FRAME_LIMIT + 1) + b"X" * 64 + trailing
        )
        code, out, _ = self.serve(stdin_bytes, _active())
        self.assertEqual(code, 1)
        self.assertEqual(_read_json(self.path), {})
        self.assertEqual(out, b"")

    def test_truncated_body_terminates(self):
        stdin_bytes = struct.pack("<I", 20) + b"short"
        code, out, _ = self.serve(stdin_bytes, _active())
        self.assertEqual(code, 1)
        self.assertEqual(_read_json(self.path), {})
        self.assertEqual(out, b"")

    def test_truncated_header_terminates(self):
        code, out, _ = self.serve(b"\x05\x00", _active())
        self.assertEqual(code, 1)
        self.assertEqual(_read_json(self.path), {})
        self.assertEqual(out, b"")

    def test_zero_length_terminates(self):
        code, out, _ = self.serve(struct.pack("<I", 0), _active())
        self.assertEqual(code, 1)
        self.assertEqual(_read_json(self.path), {})
        self.assertEqual(out, b"")


class BrokenStdoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "zen-context.json")
        self._orig_active = host.hypr_active

    def tearDown(self):
        host.hypr_active = self._orig_active
        self.temp.cleanup()

    class _BrokenStdout(io.BytesIO):
        def write(self, _b):
            raise OSError("broken pipe")

        def flush(self):
            raise OSError("broken pipe")

    def serve_broken(self, stdin_bytes, active):
        host.hypr_active = lambda _h="hyprctl": active
        stdin = io.BytesIO(stdin_bytes)
        stdout = self._BrokenStdout()
        err = io.StringIO()
        with redirect_stderr(err):
            code = host.serve_forever(self.path, stdin=stdin, stdout=stdout)
        return code, err.getvalue()

    def test_broken_stdout_on_update_terminates_and_invalidates(self):
        # Two frames: with working stdout both would ack. Broken stdout
        # must stop after the first failed write (no spin), file cleared.
        payload = _frame_bytes({"type": "invalidate"}) * 2
        code, _ = self.serve_broken(payload, _active())
        self.assertEqual(code, 1)
        self.assertEqual(_read_json(self.path), {})

    def test_broken_stdout_on_malformed_ack_terminates(self):
        body = b"nope{"
        bad = struct.pack("<I", len(body)) + body
        good = _frame_bytes({"type": "invalidate"})
        code, _ = self.serve_broken(bad + good, _active())
        self.assertEqual(code, 1)
        self.assertEqual(_read_json(self.path), {})


if __name__ == "__main__":
    unittest.main()
