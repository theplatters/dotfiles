import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).parents[1]
QUERY = ROOT / "widgets" / "PaletteQuery.js"


@unittest.skipUnless(shutil.which("node"), "node is required for query helper coverage")
class PaletteQueryTests(unittest.TestCase):
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
            ["node", "-e", script, str(QUERY), expression],
            check=True, text=True, capture_output=True,
        )
        return json.loads(completed.stdout)

    def test_exact_match_outranks_substring_and_fuzzy(self):
        self.assertEqual(
            self.evaluate(
                '[[{title: "a", subtitle: "b", keywords: "c"}, "a b c"],'
                ' [{title: "Terminal", subtitle: "App", keywords: ""}, "term"],'
                ' [{title: "Terminal", subtitle: "App", keywords: ""}, "tmnl"]].'
                "map(([row, needle]) => score(row, needle))"
            ),
            [1000, 500, 40],
        )

    def test_late_substring_stays_positive_and_miss_is_negative(self):
        self.assertEqual(
            self.evaluate(
                '[score({title: "x".repeat(600) + "needle", subtitle: "", keywords: ""}, "needle"),'
                ' score({title: "Terminal", subtitle: "App", keywords: ""}, ""),'
                ' score({title: "Terminal", subtitle: "App", keywords: ""}, "zzz")]'
            ),
            [100, 1, -1],
        )

    def test_parse_routes_and_trims(self):
        self.assertEqual(
            self.evaluate(
                '["ai: hello", "clip: cats", "= 1 + 2", "> lock"].map(parseQuery)'
            ),
            [
                {"mode": "ai", "text": "hello"},
                {"mode": "clip", "text": "cats"},
                {"mode": "=", "text": "1 + 2"},
                {"mode": ">", "text": "lock"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
