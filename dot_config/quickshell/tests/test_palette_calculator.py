import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).parents[1]
CALCULATOR = ROOT / "widgets" / "PaletteCalculator.js"


@unittest.skipUnless(shutil.which("node"), "node is required for calculator coverage")
class PaletteCalculatorTests(unittest.TestCase):
    def calculate(self, *values):
        script = r'''
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const context = {};
vm.createContext(context);
vm.runInContext(source, context);
console.log(JSON.stringify(process.argv.slice(2).map(value => context.calculate(value))));
'''
        completed = subprocess.run(
            ["node", "-e", script, str(CALCULATOR), *values],
            check=True, text=True, capture_output=True,
        )
        return json.loads(completed.stdout)

    def test_precedence_unary_and_right_associative_exponents(self):
        results = self.calculate(
            "2 + 3 * 4",
            "-2^2",
            "(-2)^2",
            "2^-2",
            "2^3^2",
            "10 % 3",
            "1e3 + .5",
        )
        self.assertEqual([result["value"] for result in results],
                         ["14", "-4", "4", "0.25", "512", "1", "1000.5"])
        self.assertTrue(all(result["matched"] and not result["error"] for result in results))

    def test_rounding_and_negative_zero(self):
        results = self.calculate("0.1 + 0.2", "-0", "1 / 3")
        self.assertEqual([result["value"] for result in results], ["0.3", "0", "0.333333333333333"])

    def test_invalid_math_returns_helpful_errors(self):
        results = self.calculate("1 / 0", "0 % 0", "2 +", "(2 + 3", "2 * / 3", "1e309")
        self.assertTrue(all(result["matched"] for result in results))
        self.assertIn("zero", results[0]["error"].lower())
        self.assertIn("zero", results[1]["error"].lower())
        self.assertTrue(results[2]["error"])
        self.assertIn("closing", results[3]["error"].lower())
        self.assertTrue(results[4]["error"])
        self.assertIn("finite", results[5]["error"].lower())

    def test_bare_numbers_and_ordinary_searches_do_not_match(self):
        results = self.calculate(
            "42",
            "2e-3",
            "terminal",
            "/home/user/notes/2026.txt",
            "2026.txt",
            "2026-notes.txt",
            "notes/2026-notes.txt",
            "firefox --new-window",
            "hello + world",
        )
        self.assertTrue(all(not result["matched"] for result in results))

    def test_arithmetic_detection_keeps_valid_and_incomplete_expressions(self):
        results = self.calculate("2026 - 9", "1e3-2", "-1e3", "2026 -", "2 + foo", "1e")
        self.assertEqual([results[index] for index in range(3)], [
            {"matched": True, "value": "2017", "error": ""},
            {"matched": True, "value": "998", "error": ""},
            {"matched": True, "value": "-1000", "error": ""},
        ])
        for result in results[3:]:
            self.assertTrue(result["matched"] and result["error"])

    def test_input_depth_and_work_limits(self):
        too_deep = "(" * 80 + "1" + ")" * 80
        too_long = "1+" + "1" * 600
        too_complex = "+".join(["1"] * 180)
        results = self.calculate(too_deep, too_long, too_complex)
        self.assertIn("deeply nested", results[0]["error"])
        self.assertIn("too long", results[1]["error"])
        self.assertTrue(results[2]["error"])


if __name__ == "__main__":
    unittest.main()
