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

    def test_bounded_keywords_and_prepared_match_cache(self):
        # S-056: named bounds exist and scoring respects them. Short
        # values pass through unchanged (ranking preserved); long
        # values match only on the leading snippet; a precomputed
        # _matchText is scored directly.
        self.assertEqual(
            self.evaluate(
                "[MAX_SOURCE_ROWS, MAX_MATCH_SNIPPET,"
                ' boundedKeywords("short", 240),'
                ' boundedKeywords("x".repeat(600) + "needle", 240).length,'
                ' boundedKeywords("x".repeat(600) + "needle", 240).indexOf("needle"),'
                ' boundedKeywords(null), boundedKeywords(undefined)]'
            ),
            [200, 240, "short", 240, -1, "", ""],
        )
        self.assertEqual(
            self.evaluate(
                '[score({title: "t", subtitle: "s",'
                ' keywords: boundedKeywords("x".repeat(600) + "needle")}, "needle"),'
                ' score({title: "t", subtitle: "s",'
                ' keywords: "x".repeat(600) + "needle"}, "needle"),'
                ' prepareRow({title: "Terminal", subtitle: "App",'
                ' keywords: "term"})._matchText,'
                ' score({_matchText: "needle here", title: "zzz",'
                ' subtitle: "", keywords: ""}, "needle") >= 0,'
                ' score({title: "Terminal", subtitle: "App", keywords: ""}, "term")]'
            ),
            [-1, 100, "terminal app term", True, 500],
        )

    def test_parse_routes_and_trims(self):
        self.assertEqual(
            self.evaluate(
                '["ai: hello", "clip: cats", "= 1 + 2", "> lock", "seen: deploy",'
                ' "todo: fix backoff", "session: deploy", "work: deploy",'
                ' "+ firefox", "hist: deploy", "inbox: triage",'
                ' "history: deploy"].map(parseQuery)'
            ),
            [
                {"mode": "ai", "text": "hello"},
                {"mode": "clip", "text": "cats"},
                {"mode": "=", "text": "1 + 2"},
                {"mode": ">", "text": "lock"},
                {"mode": "seen", "text": "deploy"},
                {"mode": "todo", "text": "fix backoff"},
                {"mode": "session", "text": "deploy"},
                {"mode": "session", "text": "deploy"},
                # `+` is app search (DesktopEntries), never the todo:
                # alias — `todo:` is the only quick-add entry.
                {"mode": "+", "text": "firefox"},
                # L6: `hist:` is removed outright and `inbox:` is deleted
                # (its meaning is the default session sort) — both fall
                # through to unified search.
                {"mode": "", "text": "hist: deploy"},
                {"mode": "", "text": "inbox: triage"},
                {"mode": "", "text": "history: deploy"},
            ],
        )

    def test_session_and_seen_prefixes(self):
        self.assertEqual(
            self.evaluate(
                '["session: deploy", "WORK:deploy", "session", "work: deploy",'
                ' "seen:", "SEEN deploy", "seenfoo",'
                ' "todo:", "TODO fix it", "todofoo",'
                ' "inbox:", "sessionfoo", "inboxfoo", "hist:"].map(parseQuery)'
            ),
            [
                {"mode": "session", "text": "deploy"},
                {"mode": "session", "text": "deploy"},
                {"mode": "session", "text": ""},
                {"mode": "session", "text": "deploy"},
                {"mode": "seen", "text": ""},
                {"mode": "seen", "text": "deploy"},
                {"mode": "", "text": "seenfoo"},
                {"mode": "todo", "text": ""},
                {"mode": "todo", "text": "fix it"},
                {"mode": "", "text": "todofoo"},
                {"mode": "", "text": "inbox:"},
                {"mode": "", "text": "sessionfoo"},
                {"mode": "", "text": "inboxfoo"},
                {"mode": "", "text": "hist:"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
