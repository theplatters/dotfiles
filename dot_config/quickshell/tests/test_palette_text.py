import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).parents[1]
TEXT = ROOT / "widgets" / "PaletteText.js"


@unittest.skipUnless(shutil.which("node"), "node is required for palette text helper coverage")
class PaletteTextTests(unittest.TestCase):
    def evaluate(self, expression):
        script = r'''
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const context = {};
vm.createContext(context);
vm.runInContext(source, context);
console.log(JSON.stringify(vm.runInContext(process.argv[2], context)));
'''
        completed = subprocess.run(
            ["node", "-e", script, str(TEXT), expression],
            check=True, text=True, capture_output=True,
        )
        return json.loads(completed.stdout)

    def test_short_text_passes_through_and_collapses_whitespace(self):
        self.assertEqual(
            self.evaluate('plainSnippet("  hello\\n\\tworld  ", 180, "")'),
            "hello world",
        )
        self.assertEqual(self.evaluate('plainSnippet("", 180, "x")'), "")
        self.assertEqual(self.evaluate('plainSnippet(null, 180, "x")'), "")

    def test_long_text_without_match_truncates_at_limit(self):
        snippet = self.evaluate('plainSnippet("x".repeat(600), 180, "")')
        self.assertEqual(len(snippet), 180)
        self.assertTrue(snippet.endswith("…"))
        self.assertFalse(snippet.startswith("…"))

    def test_long_text_centers_the_match_window(self):
        snippet = self.evaluate(
            'plainSnippet("x".repeat(600) + "needle" + "x".repeat(600), 180, "needle")'
        )
        self.assertIn("needle", snippet)
        self.assertEqual(len(snippet), 180)
        self.assertTrue(snippet.startswith("…"))
        self.assertTrue(snippet.endswith("…"))

    def test_early_match_keeps_the_head(self):
        snippet = self.evaluate('plainSnippet("needle" + "x".repeat(600), 180, "needle")')
        self.assertTrue(snippet.startswith("needle"))
        self.assertTrue(snippet.endswith("…"))

    def test_capture_detail_is_bounded_at_240(self):
        self.assertEqual(
            self.evaluate('boundedCaptureDetail("x".repeat(600))'),
            "x" * 237 + "…",
        )
        self.assertEqual(
            self.evaluate('boundedCaptureDetail("  boom\\n  ")'), "boom"
        )

    def test_capture_failure_reports_cancel_vs_fail(self):
        self.assertEqual(
            self.evaluate('[captureFailure(130, "whatever"), captureFailure(2, "boom")]'),
            ["Screen capture cancelled: whatever", "Screen capture failed: boom"],
        )
        self.assertEqual(
            self.evaluate('captureFailure(1, "user cancelled the snap")'),
            "Screen capture cancelled: user cancelled the snap",
        )
        self.assertEqual(
            self.evaluate('captureFailure(1, "")'), "Screen capture failed"
        )

    def test_capture_failure_detail_stays_bounded(self):
        message = self.evaluate('captureFailure(1, "x".repeat(600))')
        self.assertTrue(message.startswith("Screen capture failed: "))
        self.assertLessEqual(len(message), 240)


if __name__ == "__main__":
    unittest.main()
