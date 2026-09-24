"""Tests for the isolated no-tools recap helper (scripts/project_recap.py)."""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import project_recap as recap

PID = "11111111-1111-1111-1111-111111111111"
SID = "a" * 32


def run_cli(args):
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "project_recap.py"), *args],
        text=True, capture_output=True, timeout=20, check=False,
    )
    return completed


CHANGES_OK = {
    "available": True, "reason": "",
    "project_id": PID, "session_id": SID,
    "baseline_commit": "b" * 40, "latest_commit": "c" * 40,
    "has_baseline": True, "summary_key": "abc..def",
    "evidence": "M foo.py\n+M bar.py",
    "repo_path": "/repo", "observed_at_ms": 1, "updated_at_ms": 2,
    "limitations": "last observed state",
}


def completed_ok(stdout="short recap text"):
    return SimpleNamespace(returncode=0,
                           stdout=stdout.encode("utf-8"),
                           stderr=b"")


class ValidationTests(unittest.TestCase):
    def test_bad_project_or_session_rejected(self):
        with self.assertRaises(recap.RecapError):
            recap.recap_for_session("nope", SID)
        with self.assertRaises(recap.RecapError):
            recap.recap_for_session(PID, "short")

    def test_bad_model_rejected(self):
        with self.assertRaises(recap.RecapError):
            recap.recap_for_session(PID, SID, model="not a model!!")
        with self.assertRaises(recap.RecapError):
            recap.recap_for_session(PID, SID, model="--print")

    def test_cli_requires_project_and_session(self):
        self.assertNotEqual(run_cli(["recap"]).returncode, 0)
        bad = run_cli(["recap", "--project", PID])
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("error:", bad.stderr)


class PromptFramingTests(unittest.TestCase):
    def test_prompt_frames_evidence_as_untrusted(self):
        prompt = recap.build_recap_prompt("M foo.py", "b" * 40, "c" * 40)
        self.assertIn("SESSION_CHANGES_BEGIN", prompt)
        self.assertIn("SESSION_CHANGES_END", prompt)
        self.assertIn("untrusted data", prompt)
        self.assertIn("M foo.py", prompt)
        self.assertIn("b" * 40, prompt)

    def test_system_prompt_declares_no_tools(self):
        lowered = recap.SYSTEM_PROMPT.lower()
        self.assertIn("no tools", lowered)

    def test_system_prompt_carries_attribution_rules(self):
        lowered = recap.SYSTEM_PROMPT.lower()
        self.assertIn("baseline", lowered)
        self.assertIn("pre-existing", lowered)
        self.assertIn("partial", lowered)

    def test_full_bounded_evidence_consumed_whole(self):
        # Sidecar-shaped evidence at exactly the backend bound, with the
        # mandatory trailing limitation, must reach the prompt intact:
        # no prefix clip may strip attribution sections or disclaimer.
        head = ("Commits during this session: abc..def.\n"
                "Files already dirty at session start (baseline):\nM old.py\n"
                "Diff vs HEAD at last observation (change-only):\n")
        tail = "M new.py\nLimitation: last observed state while active."
        filler = "x\n" * ((8000 - len(head) - len(tail)) // 2)
        evidence = head + filler + tail
        self.assertLessEqual(len(evidence), 8000)
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return completed_ok()

        changes = dict(CHANGES_OK, evidence=evidence)
        with patch.object(recap._psc, "session_changes",
                          return_value=changes):
            recap.recap_for_session(PID, SID, _run=fake_run)
        prompt = seen["argv"][seen["argv"].index("--") + 1]
        self.assertIn(head, prompt)
        self.assertIn(tail, prompt)
        self.assertIn("pre-existing, not new work", prompt)
        self.assertNotIn("truncated", prompt)

    def test_long_evidence_keeps_partial_marker(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return completed_ok()

        changes = dict(CHANGES_OK, evidence="M a.py\n" + "x\n" * 6000)
        with patch.object(recap._psc, "session_changes",
                          return_value=changes):
            recap.recap_for_session(PID, SID, _run=fake_run)
        prompt = seen["argv"][seen["argv"].index("--") + 1]
        self.assertIn("M a.py", prompt)
        self.assertIn("truncated", prompt)
        self.assertIn("SESSION_CHANGES_BEGIN", prompt)
        self.assertIn("SESSION_CHANGES_END", prompt)

    def test_evidence_bounded_with_marker(self):
        big = "line\n" * 5000
        clipped = recap._bound_evidence(big)
        self.assertLessEqual(len(clipped), recap.MAX_EVIDENCE_CHARS + 64)
        self.assertIn("truncated", clipped)


class IsolationArgvTests(unittest.TestCase):
    def test_pi_argv_is_isolated_no_tools_no_session(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return completed_ok()

        with patch.object(recap._psc, "session_changes",
                          return_value=dict(CHANGES_OK)):
            value = recap.recap_for_session(
                PID, SID, _run=fake_run)
        argv = seen["argv"]
        for flag in ("--print", "--no-session", "--no-tools",
                     "--no-extensions", "--no-skills",
                     "--no-prompt-templates", "--no-context-files",
                     "--no-approve"):
            self.assertIn(flag, argv)
        self.assertIn("--system-prompt", argv)
        # No session persistence, no tool allowlists, no credentials.
        for token in argv:
            self.assertNotIn("--session", [token])
            self.assertNotIn("--tools", [token])
            self.assertNotIn("--api-key", [token])
        self.assertNotIn("api-key", json.dumps(seen["kwargs"]).lower())
        # Prompt travels after `--`, never on a shell command line.
        self.assertFalse(seen["kwargs"].get("shell", False))
        dashdash = argv.index("--")
        self.assertIn("SESSION_CHANGES_BEGIN", argv[dashdash + 1])
        self.assertEqual(value["summary"], "short recap text")
        self.assertEqual(value["summary_key"], "abc..def")
        self.assertIsNone(value["model"])

    def test_model_override_validated_shape(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return completed_ok()

        with patch.object(recap._psc, "session_changes",
                          return_value=dict(CHANGES_OK)):
            value = recap.recap_for_session(
                PID, SID, model="openai/gpt-4o-mini", _run=fake_run)
        self.assertIn("--model", seen["argv"])
        self.assertIn("openai/gpt-4o-mini", seen["argv"])
        self.assertEqual(value["model"], "openai/gpt-4o-mini")

    def test_default_model_omits_flag(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return completed_ok()

        with patch.object(recap._psc, "session_changes",
                          return_value=dict(CHANGES_OK)):
            recap.recap_for_session(PID, SID, _run=fake_run)
        self.assertNotIn("--model", seen["argv"])


class FailureTests(unittest.TestCase):
    def test_missing_baseline_refused_without_spawn(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return completed_ok()

        bad = dict(CHANGES_OK, available=True, has_baseline=False,
                   baseline_commit=None, summary_key="missing-baseline")
        with patch.object(recap._psc, "session_changes",
                          return_value=bad):
            with self.assertRaises(recap.RecapError) as ctx:
                recap.recap_for_session(PID, SID, _run=fake_run)
        self.assertIn("baseline", str(ctx.exception).lower())
        self.assertEqual(calls, [])

    def test_unavailable_sidecar_refused_without_spawn(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return completed_ok()

        bad = dict(CHANGES_OK, available=False, reason="nope")
        with patch.object(recap._psc, "session_changes",
                          return_value=bad):
            with self.assertRaises(recap.RecapError):
                recap.recap_for_session(PID, SID, _run=fake_run)
        self.assertEqual(calls, [])

    def test_nonzero_exit_reports_code_only_no_prompt_text(self):
        def fake_run(argv, **kwargs):
            return SimpleNamespace(returncode=2, stdout=b"",
                                   stderr=b"some helper diagnostic")

        with patch.object(recap._psc, "session_changes",
                          return_value=dict(CHANGES_OK)):
            with self.assertRaises(recap.RecapError) as ctx:
                recap.recap_for_session(
                    PID, SID, _run=fake_run,
                    model=None)
        message = str(ctx.exception)
        self.assertIn("exited 2", message)
        self.assertNotIn("foo.py", message)
        self.assertNotIn("SESSION_CHANGES", message)

    def test_timeout_is_bounded_error(self):
        def fake_run(argv, **kwargs):
            self.assertLessEqual(kwargs.get("timeout"), 61)
            raise subprocess.TimeoutExpired(argv, 60)

        with patch.object(recap._psc, "session_changes",
                          return_value=dict(CHANGES_OK)):
            with self.assertRaises(recap.RecapError) as ctx:
                recap.recap_for_session(PID, SID, _run=fake_run)
        self.assertIn("timed out", str(ctx.exception).lower())

    def test_empty_output_rejected(self):
        def fake_run(argv, **kwargs):
            return completed_ok(stdout="   \n ")

        with patch.object(recap._psc, "session_changes",
                          return_value=dict(CHANGES_OK)):
            with self.assertRaises(recap.RecapError):
                recap.recap_for_session(PID, SID, _run=fake_run)

    def test_summary_bounded(self):
        def fake_run(argv, **kwargs):
            return completed_ok(stdout="word " * 5000)

        with patch.object(recap._psc, "session_changes",
                          return_value=dict(CHANGES_OK)):
            value = recap.recap_for_session(PID, SID, _run=fake_run)
        self.assertLessEqual(len(value["summary"]),
                             recap.MAX_SUMMARY_CHARS + 1)


if __name__ == "__main__":
    unittest.main()
