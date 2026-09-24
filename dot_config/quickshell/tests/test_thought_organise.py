"""Backend tests for explicit thought Organise (scripts/thought_organise.py).

Offline: the single isolated ``pi`` subprocess is faked at the
``_run`` boundary; prepared tokens live in a temp annotations sidecar.
No network beyond the (faked) local Pi call, ever.

L8: exactly one model path — explicit "Organise", preview-first, never
automatic, never silent. Gated on ``memory.organise`` (strict True;
anything else fails closed).
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import annotations
import thought_organise as org

SID = "a" * 32
NOW = 5_000_000
ENABLED = {"organise": True}


class FakeCompleted:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRun:
    """Fake ``subprocess.run`` capturing argv/kwargs for assertions."""

    def __init__(self, stdout=b"tidy thought", returncode=0, stderr=b"",
                 timeout_exc=None):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.timeout_exc = timeout_exc
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), "kwargs": dict(kwargs)})
        if self.timeout_exc is not None:
            raise self.timeout_exc
        return FakeCompleted(returncode=self.returncode,
                             stdout=self.stdout, stderr=self.stderr)


class OrganiseCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "annotations.db")
        self.conn = annotations.connect(self.db)
        self.addCleanup(self.conn.close)

    def prepare(self, text="rough thought", session_id=None,
                settings=None, now_ms=NOW, run=None):
        run = FakeRun() if run is None else run
        value = org.prepare_organise(
            self.conn, text, session_id,
            settings=ENABLED if settings is None else settings,
            now_ms=now_ms, _run=run)
        return value, run

    def test_prepare_apply_roundtrip(self):
        value, run = self.prepare()
        self.assertEqual(value["preview"], "tidy thought")
        self.assertTrue(value["prepared"].startswith("orgt_"))
        self.assertEqual(value["expires_in"], 600)
        self.assertEqual(len(run.calls), 1)
        applied = org.apply_organised(self.conn, value["prepared"],
                                      now_ms=NOW + 1)
        self.assertEqual(applied, {"applied": True, "text": "tidy thought",
                                   "session_id": ""})

    def test_prepare_apply_roundtrip_with_session(self):
        value, _ = self.prepare(session_id=SID.upper())
        applied = org.apply_organised(self.conn, value["prepared"],
                                      now_ms=NOW + 1)
        self.assertEqual(applied["session_id"], SID)
        self.assertEqual(applied["text"], "tidy thought")

    def test_apply_burns_single_use_token(self):
        value, _ = self.prepare()
        org.apply_organised(self.conn, value["prepared"], now_ms=NOW + 1)
        with self.assertRaises(org.OrganiseError) as ctx:
            org.apply_organised(self.conn, value["prepared"], now_ms=NOW + 2)
        self.assertIn("unknown or expired", str(ctx.exception))

    def test_apply_unknown_token(self):
        with self.assertRaises(org.OrganiseError) as ctx:
            org.apply_organised(self.conn, "orgt_" + "0" * 32, now_ms=NOW)
        self.assertIn("unknown or expired", str(ctx.exception))

    def test_gate_off_fails_closed_without_pi_call(self):
        for settings in ({}, {"organise": False}, {"organise": 0},
                         {"organise": 1}, {"organise": "yes"},
                         {"organise": "true"}, {"other": True}):
            with self.subTest(settings=settings):
                run = FakeRun()
                with self.assertRaises(org.OrganiseError) as ctx:
                    org.prepare_organise(self.conn, "rough thought", None,
                                         settings=settings, now_ms=NOW,
                                         _run=run)
                self.assertEqual(str(ctx.exception), "organise is disabled")
                self.assertEqual(run.calls, [])

    def test_run_uses_shared_lockdown_argv(self):
        run = FakeRun()
        self.prepare(run=run)
        argv = run.calls[0]["argv"]
        # Isolation reuses project_recap.build_pi_argv byte-for-byte: 8
        # lockdown flags, fixed read-only system prompt, then `--` and
        # the untrusted prompt as a message argument.
        flags = ["--print", "--no-session", "--no-tools", "--no-extensions",
                 "--no-skills", "--no-prompt-templates", "--no-context-files",
                 "--no-approve"]
        for flag in flags:
            self.assertIn(flag, argv)
        self.assertEqual([argv.index(flag) for flag in flags],
                         sorted(argv.index(flag) for flag in flags))
        prompt_index = argv.index("--system-prompt")
        self.assertEqual(argv[prompt_index + 1], org.SYSTEM_PROMPT)
        self.assertIn("--", argv)
        prompt = argv[-1]
        self.assertIn("THOUGHT_BEGIN", prompt)
        self.assertIn("rough thought", prompt)
        self.assertIn("THOUGHT_END", prompt)
        # List-form argv, no shell, stdin closed.
        kwargs = run.calls[0]["kwargs"]
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_system_prompt_is_fixed(self):
        self.assertIn("Preserve its meaning", org.SYSTEM_PROMPT)
        self.assertIn("never invent", org.SYSTEM_PROMPT)
        self.assertIn("no tools", org.SYSTEM_PROMPT)
        self.assertIn("untrusted", org.SYSTEM_PROMPT)

    def test_prompt_carries_session_provenance(self):
        prompt = org.build_organise_prompt("hello", SID)
        lines = prompt.split("\n")
        self.assertEqual(lines[0], "THOUGHT_BEGIN")
        self.assertIn(SID, prompt)
        self.assertEqual(lines[-2], "THOUGHT_END")
        bare = org.build_organise_prompt("hello", "")
        self.assertNotIn("Session:", bare)
        self.assertTrue(bare.startswith("THOUGHT_BEGIN\nhello\n"))

    def test_pi_timeout_is_60s(self):
        run = FakeRun(timeout_exc=subprocess.TimeoutExpired("pi", 60))
        self.assertEqual(org.PI_TIMEOUT, 60.0)
        with self.assertRaises(org.OrganiseError) as ctx:
            self.prepare(run=run)
        self.assertIn("timed out after 60s", str(ctx.exception))
        self.assertEqual(run.calls[0]["kwargs"]["timeout"], 60.0)

    def test_output_bounded_at_4kib(self):
        self.assertEqual(org.MAX_OUTPUT_BYTES, 4 * 1024)
        run = FakeRun(stdout=("x" * 9999 + "\nline\n").encode("utf-8"))
        value, _ = self.prepare(run=run)
        encoded = value["preview"].encode("utf-8")
        self.assertLessEqual(len(encoded), 4 * 1024)
        self.assertIn("[truncated]", value["preview"])

    def test_empty_output_rejected(self):
        for stdout in (b"", b"   \n  "):
            with self.subTest(stdout=stdout):
                with self.assertRaises(org.OrganiseError) as ctx:
                    self.prepare(run=FakeRun(stdout=stdout))
                self.assertIn("empty output", str(ctx.exception))

    def test_nonzero_exit_reports_bounded_diagnostic(self):
        run = FakeRun(returncode=3,
                      stderr="error: overloaded\ntraceback…".encode("utf-8"))
        with self.assertRaises(org.OrganiseError) as ctx:
            self.prepare(run=run)
        message = str(ctx.exception)
        self.assertIn("organise exited 3", message)
        self.assertIn("overloaded", message)
        self.assertNotIn("rough thought", message)

    def test_wrong_kind_token_rejected(self):
        annotations.create_prepared(
            self.conn, token="orgt_" + "1" * 32, kind="journal",
            payload={"text": "x"}, preview={"text": "x"},
            revision=None, expires_ms=NOW + 600_000)
        with self.assertRaises(org.OrganiseError) as ctx:
            org.apply_organised(self.conn, "orgt_" + "1" * 32, now_ms=NOW)
        self.assertIn("invalid", str(ctx.exception))

    def test_prepared_token_expires(self):
        value, _ = self.prepare()
        with self.assertRaises(org.OrganiseError) as ctx:
            org.apply_organised(self.conn, value["prepared"],
                                now_ms=NOW + org.PREPARED_TTL_MS + 1)
        self.assertIn("unknown or expired", str(ctx.exception))

    def test_input_bounds(self):
        run = FakeRun()
        for text in ("", "   ", None, 42):
            with self.subTest(text=text):
                with self.assertRaises(org.OrganiseError) as ctx:
                    self.prepare(text=text, run=run)
                self.assertIn("text is required", str(ctx.exception))
        with self.assertRaises(org.OrganiseError) as ctx:
            self.prepare(text="x" * (org.MAX_INPUT_CHARS + 1), run=run)
        self.assertIn("too long", str(ctx.exception))
        with self.assertRaises(org.OrganiseError) as ctx:
            self.prepare(text="ok", session_id="not-hex", run=run)
        self.assertIn("32 hex", str(ctx.exception))
        with self.assertRaises(org.OrganiseError) as ctx:
            self.prepare(text="bad\x00text", run=run)
        self.assertIn("invalid", str(ctx.exception))
        self.assertEqual(run.calls, [])

    def test_bad_timestamp_rejected(self):
        with self.assertRaisesRegex(org.OrganiseError, "timestamp"):
            self.prepare(now_ms=-1)
        value, _ = self.prepare()
        with self.assertRaisesRegex(org.OrganiseError, "timestamp"):
            org.apply_organised(self.conn, value["prepared"], now_ms="nope")


if __name__ == "__main__":
    unittest.main()
