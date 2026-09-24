"""Focused palette `seen:` resource-source tests (Phase 2b §4.1, L6).

Runs the real PaletteQuery.js / PaletteDataSources.qml / CommandPalette.qml
JavaScript in node, like tests/test_resume_palette.py: no QML engine, no
network, no store access. desktop_projects.py `seen` itself is covered by
tests/test_desktop_projects.py; here the payload shape is simulated.

L6: `hist:` is removed outright (falls through to unified search) and
`inbox:` is deleted (its meaning is the default session sort). Rows are
flat resources {kind,label,identity,...}, not sessions.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
PALETTE = ROOT / "widgets" / "CommandPalette.qml"
DATASOURCES = ROOT / "widgets" / "PaletteDataSources.qml"
QUERY = ROOT / "widgets" / "PaletteQuery.js"
PALETTE_TEXT = PALETTE.read_text(encoding="utf-8")
DATASOURCES_TEXT = DATASOURCES.read_text(encoding="utf-8")


def extract_function(source, name):
    start = source.index("function " + name + "(")
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError("unterminated function: " + name)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class SeenQueryTests(unittest.TestCase):
    def parse(self, *values):
        script = r'''
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const context = {};
vm.createContext(context);
vm.runInContext(source, context);
console.log(JSON.stringify(process.argv.slice(2).map(value => context.parseQuery(value))));
'''
        completed = subprocess.run(
            ["node", "-e", script, str(QUERY), *values],
            check=True, text=True, capture_output=True,
        )
        return json.loads(completed.stdout)

    def test_seen_prefix_bare_and_lookalike(self):
        self.assertEqual(
            self.parse("seen: deploy", "SEEN:deploy", "seen", "seenfoo"),
            [
                {"mode": "seen", "text": "deploy"},
                {"mode": "seen", "text": "deploy"},
                {"mode": "seen", "text": ""},
                {"mode": "", "text": "seenfoo"},
            ],
        )

    def test_hist_and_inbox_fall_through_to_unified(self):
        # L6 + §4.2: no transition period, no dual prefix.
        self.assertEqual(
            self.parse("hist: deploy", "HIST:deploy", "hist", "history: deploy",
                       "histproject", "inbox:", "inbox: triage", "inboxfoo"),
            [
                {"mode": "", "text": "hist: deploy"},
                {"mode": "", "text": "HIST:deploy"},
                {"mode": "", "text": "hist"},
                {"mode": "", "text": "history: deploy"},
                {"mode": "", "text": "histproject"},
                {"mode": "", "text": "inbox:"},
                {"mode": "", "text": "inbox: triage"},
                {"mode": "", "text": "inboxfoo"},
            ],
        )

    def test_existing_prefixes_unchanged(self):
        self.assertEqual(
            self.parse("resume BeforeIT", "file: notes", "ai: hi", "clip: c",
                       "todo: fix it", "session: deploy", "+ firefox"),
            [
                {"mode": "resume", "text": "BeforeIT"},
                {"mode": "file", "text": "notes"},
                {"mode": "ai", "text": "hi"},
                {"mode": "clip", "text": "c"},
                {"mode": "todo", "text": "fix it"},
                {"mode": "session", "text": "deploy"},
                {"mode": "+", "text": "firefox"},
            ],
        )


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class SeenDataSourceTests(unittest.TestCase):
    def run_node(self, script):
        completed = subprocess.run(
            ["node", "-e", script], text=True, capture_output=True,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr)
        return completed.stdout.strip()

    def functions(self, names):
        return {n: extract_function(DATASOURCES_TEXT, n) for n in names}

    def test_command_is_single_arg_seen_and_scope(self):
        fns = self.functions(("seenListCommand", "shouldSearchSeen", "validSeenList"))
        script = f"""
const vm = require("vm");
const root = {{mode: "seen"}};
const context = {{root, Quickshell: {{shellPath(v) {{ return "/qs/" + v; }} }}}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
const bare = context.seenListCommand.call(root, "");
const query = context.seenListCommand.call(root, "deploy");
const seenOnly = context.shouldSearchSeen.call(root);
root.mode = "resume";
const resumeOff = context.shouldSearchSeen.call(root);
root.mode = "session";
const sessionOff = context.shouldSearchSeen.call(root);
console.log(JSON.stringify({{
  bare,
  query,
  seenOnly,
  resumeOff,
  sessionOff,
  valid: context.validSeenList.call(root, {{rows: []}}),
  validExtra: context.validSeenList.call(root, {{rows: [], truncated: true, total: 3}}),
  invalidMissing: context.validSeenList.call(root, {{sessions: []}}),
  invalidEntries: context.validSeenList.call(root, {{entries: []}}),
  invalidNull: context.validSeenList.call(root, null)
}}));
"""
        value = json.loads(self.run_node(script))
        # One thin mapper command: bare `seen:` lists recent, a needle
        # adds --query. Single-arg (query only); no mode/second arg.
        self.assertEqual(value["bare"], ["python3",
                                         "/qs/scripts/desktop_projects.py",
                                         "seen", "--limit", "20"])
        self.assertEqual(value["query"], ["python3",
                                          "/qs/scripts/desktop_projects.py",
                                          "seen", "--limit", "20",
                                          "--query", "deploy"])
        self.assertTrue(value["seenOnly"])
        self.assertFalse(value["resumeOff"])
        self.assertFalse(value["sessionOff"])
        # The envelope is {rows}, never {sessions}/{entries}.
        self.assertTrue(value["valid"])
        self.assertTrue(value["validExtra"])
        self.assertFalse(value["invalidMissing"])
        self.assertFalse(value["invalidEntries"])
        self.assertFalse(value["invalidNull"])

    def test_kind_coercion_and_labels(self):
        fns = self.functions(("seenKindOf", "seenKindLabel"))
        script = f"""
const vm = require("vm");
const root = {{}};
const context = {{root}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
const kinds = ["file", "url", "zotero", "page", "web", "adapter", "", null, undefined]
  .map(k => context.seenKindOf.call(root, {{kind: k}}));
kinds.push(context.seenKindOf.call(root, null));
kinds.push(context.seenKindOf.call(root, {{}}));
const labels = ["file", "url", "zotero", "page", "other"].map(
  k => context.seenKindLabel.call(root, k));
console.log(JSON.stringify({{kinds, labels}}));
"""
        value = json.loads(self.run_node(script))
        # Anything outside file/url/zotero/page coerces to page for
        # display; the identity still opens/copies.
        self.assertEqual(value["kinds"],
                         ["file", "url", "zotero", "page", "page", "page",
                          "page", "page", "page", "page", "page"])
        self.assertEqual(value["labels"],
                         ["File", "Link", "Zotero", "Page", "Page"])

    def test_project_id_and_observed_gates(self):
        fns = self.functions(("seenProjectIdOf", "seenObservedMs", "cleanBoundedText"))
        long_text = "T" * 200
        script = f"""
const vm = require("vm");
const root = {{}};
const context = {{root}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
console.log(JSON.stringify({{
  valid: context.seenProjectIdOf.call(root, {{project_id: "11111111-1111-4111-8111-111111111111"}}),
  upper: context.seenProjectIdOf.call(root, {{project_id: "AAAAAAAA-1111-4111-8111-111111111111"}}),
  bad: context.seenProjectIdOf.call(root, {{project_id: "not-a-uuid"}}),
  missing: context.seenProjectIdOf.call(root, {{}}),
  nonString: context.seenProjectIdOf.call(root, {{project_id: 42}}),
  numeric: context.seenObservedMs.call(root, {{last_seen_ms: 1700000000000}}),
  zero: context.seenObservedMs.call(root, {{last_seen_ms: 0}}),
  negative: context.seenObservedMs.call(root, {{last_seen_ms: -5}}),
  missingMs: context.seenObservedMs.call(root, {{}}),
  nullMs: context.seenObservedMs.call(root, null),
  capped: context.cleanBoundedText.call(root, "{long_text}").length,
  control: context.cleanBoundedText.call(root, "/x/\\u0001bad\\u0007"),
  nullText: context.cleanBoundedText.call(root, null),
  numText: context.cleanBoundedText.call(root, 42)
}}));
"""
        value = json.loads(self.run_node(script))
        self.assertEqual(value["valid"], "11111111-1111-4111-8111-111111111111")
        self.assertEqual(value["upper"], "AAAAAAAA-1111-4111-8111-111111111111")
        self.assertEqual(value["bad"], "")
        self.assertEqual(value["missing"], "")
        self.assertEqual(value["nonString"], "")
        self.assertEqual(value["numeric"], 1700000000000)
        self.assertEqual(value["zero"], 0)
        self.assertEqual(value["negative"], 0)
        self.assertEqual(value["missingMs"], 0)
        self.assertEqual(value["nullMs"], 0)
        self.assertEqual(value["capped"], 160)
        self.assertEqual(value["control"], "/x/bad")
        self.assertEqual(value["nullText"], "")
        self.assertEqual(value["numText"], "42")

    def test_row_shape_null_session_coerce_kind_drop(self):
        fns = self.functions(("cleanBoundedText", "seenObservedMs", "seenKindOf",
                              "seenKindLabel", "seenProjectIdOf",
                              "seenRowForEntry"))
        script = f"""
const vm = require("vm");
const root = {{}};
const context = {{root}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
const row = context.seenRowForEntry.call(root, {{
  kind: "file", label: "a.py", identity: "file:/x/a.py",
  project_id: "11111111-1111-4111-8111-111111111111", project_name: "Demo",
  session_id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  last_seen_ms: 1700000000000, occurrence_count: 3
}});
// Bare `seen:` rows carry no session: null survives, the row stays.
const bareRow = context.seenRowForEntry.call(root, {{
  kind: "url", label: "Example", identity: "url:https://example.com/a",
  project_id: "11111111-1111-4111-8111-111111111111", project_name: "Demo",
  session_id: null, last_seen_ms: 1700000000000, occurrence_count: 1
}});
// Unknown kinds coerce to page for display.
const coerced = context.seenRowForEntry.call(root, {{
  kind: "adapter", label: "thing", identity: "page:thing",
  project_id: "", project_name: "",
  session_id: "", last_seen_ms: 0, occurrence_count: 0
}});
const noLabel = context.seenRowForEntry.call(root, {{
  kind: "file", label: "", identity: "file:/x/a.py"}});
const noIdentity = context.seenRowForEntry.call(root, {{
  kind: "file", label: "a.py", identity: ""}});
const nullEntry = context.seenRowForEntry.call(root, null);
const nonObject = context.seenRowForEntry.call(root, 42);
console.log(JSON.stringify({{row, bareRow, coerced, noLabel, noIdentity,
  nullEntry, nonObject}}));
"""
        value = json.loads(self.run_node(script))
        row = value["row"]
        self.assertEqual(row["prefix"], "seen:")
        self.assertEqual(row["kind"], "seen")
        self.assertEqual(row["title"], "a.py")
        self.assertTrue(row["subtitle"].startswith("File · Demo"))
        self.assertIn("×3", row["subtitle"])
        self.assertEqual(set(row["payload"].keys()),
                         {"kind", "label", "identity", "project_id",
                          "project_name", "session_id", "last_seen_ms",
                          "occurrence_count"})
        self.assertEqual(row["payload"]["kind"], "file")
        self.assertEqual(row["payload"]["label"], "a.py")
        self.assertEqual(row["payload"]["identity"], "file:/x/a.py")
        self.assertEqual(row["payload"]["project_id"],
                         "11111111-1111-4111-8111-111111111111")
        self.assertEqual(row["payload"]["project_name"], "Demo")
        self.assertEqual(row["payload"]["session_id"], "a" * 32)
        self.assertEqual(row["payload"]["last_seen_ms"], 1700000000000)
        self.assertEqual(row["payload"]["occurrence_count"], 3)
        self.assertEqual(row["score"], 1)
        # Null session (bare list): row kept, session blanked.
        self.assertEqual(value["bareRow"]["payload"]["session_id"], "")
        self.assertEqual(value["bareRow"]["title"], "Example")
        self.assertTrue(value["bareRow"]["subtitle"].startswith("Link · Demo"))
        self.assertNotIn("×", value["bareRow"]["subtitle"])
        # Coerced kind + unresolved project + zero count.
        self.assertEqual(value["coerced"]["payload"]["kind"], "page")
        self.assertTrue(value["coerced"]["subtitle"].startswith(
            "Page · Unresolved project"))
        self.assertNotIn("×", value["coerced"]["subtitle"])
        # Label/identity are required: drop without them.
        self.assertIsNone(value["noLabel"])
        self.assertIsNone(value["noIdentity"])
        self.assertIsNone(value["nullEntry"])
        self.assertIsNone(value["nonObject"])

    def base_root(self):
        return {
            "openingGeneration": 1, "requestedOpen": True, "paletteOpen": True,
            "mode": "seen", "modeQuery": "deploy", "calculatorOnly": False,
            "seenGeneration": 2, "seenProcessGeneration": 2,
            "pendingSeenQuery": "deploy", "seenProcessQuery": "deploy",
            "seenBusy": True, "seenError": "", "seenTruncated": False,
            "seenRows": [{"title": "old"}],
            "loaded": None, "failed": None, "loadedCount": 0, "failedCount": 0,
        }

    def test_finish_installs_bounded_rows_and_truncation(self):
        fns = self.functions(("shouldSearchSeen", "validSeenList", "cleanBoundedText",
                              "seenObservedMs", "seenKindOf", "seenKindLabel",
                              "seenProjectIdOf", "seenRowForEntry",
                              "storeSeenTruncation",
                              "finishSeenSearch"))
        script = f"""
const vm = require("vm");
const root = {json.dumps(self.base_root())};
root.loaded = function(s) {{ this.loaded = s; this.loadedCount++; }};
root.loadFailed = function(s, m) {{ this.failed = m; this.failedCount++; }};
const seenTimeout = {{stops: 0, stop() {{ this.stops++; }}}};
const seenDelay = {{restarts: 0, restart() {{ this.restarts++; }}, stop() {{}}}};
const context = {{root, seenTimeout, seenDelay}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
root.shouldSearchSeen = context.shouldSearchSeen.bind(root);
const entry = (i) => ({{
  kind: "file", label: "f" + i + ".py", identity: "file:/x/" + i + ".py",
  project_id: "11111111-1111-4111-8111-111111111111", project_name: "Demo",
  session_id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  last_seen_ms: 1700000000000 + i, occurrence_count: 1
}});
const rows = Array.from({{length: 25}}, (_, i) => entry(i + 1));
context.finishSeenSearch.call(root, 0, JSON.stringify({{rows, truncated: true, total: 25}}), 2, "deploy");
console.log(JSON.stringify({{
  count: root.seenRows.length,
  first: root.seenRows[0],
  truncated: root.seenTruncated,
  total: root.seenTotal,
  loaded: root.loaded, error: root.seenError, stops: seenTimeout.stops
}}));
"""
        value = json.loads(self.run_node(script))
        self.assertEqual(value["count"], 20)
        self.assertEqual(value["first"]["kind"], "seen")
        self.assertEqual(value["first"]["prefix"], "seen:")
        self.assertTrue(value["truncated"])
        # S-041: the backend total rides along for "showing X of Y".
        self.assertEqual(value["total"], 25)
        self.assertEqual(value["loaded"], "seen")
        self.assertEqual(value["error"], "")
        self.assertEqual(value["stops"], 1)

    def test_finish_rejects_invalid_and_reports_process_failure(self):
        fns = self.functions(("shouldSearchSeen", "validSeenList", "cleanBoundedText",
                              "seenObservedMs", "seenKindOf", "seenKindLabel",
                              "seenProjectIdOf", "seenRowForEntry",
                              "finishSeenSearch"))
        base = self.base_root()
        script = f"""
const vm = require("vm");
const root = {json.dumps(base)};
root.loaded = function(s) {{ this.loaded = s; }};
root.loadFailed = function(s, m) {{ this.failed = m; }};
const seenTimeout = {{stop() {{}}}};
const seenDelay = {{restart() {{}}, stop() {{}}}};
const context = {{root, seenTimeout, seenDelay}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
root.shouldSearchSeen = context.shouldSearchSeen.bind(root);
root.finishSeenSearch(0, '{{"nope":1}}', 2, "deploy");
const invalidError = root.seenError;
const invalidLoaded = root.loaded;
const invalidRows = root.seenRows.length;
const invalidTruncated = root.seenTruncated;
root.seenError = ""; root.loaded = function(s) {{ this.loaded = s; }};
root.finishSeenSearch(1, "", 2, "deploy");
console.log(JSON.stringify({{
  invalidError, invalidLoaded, invalidRows, invalidTruncated,
  failed: root.failed, loadedAfterFailure: root.loaded, rowsAfterFailure: root.seenRows.length,
  failureError: root.seenError
}}));
"""
        value = json.loads(self.run_node(script))
        self.assertEqual(value["invalidError"], "Seen search returned invalid data")
        self.assertEqual(value["invalidLoaded"], "seen")
        self.assertEqual(value["invalidRows"], 0)
        self.assertFalse(value["invalidTruncated"])
        # Process failure clears stale rows and rebuilds through loaded
        # (one error row + Enter-to-retry in the palette).
        self.assertEqual(value["failureError"], "Seen search failed")
        self.assertEqual(value["loadedAfterFailure"], "seen")
        self.assertEqual(value["rowsAfterFailure"], 0)

    def test_stale_and_pending_query_completions(self):
        fns = self.functions(("shouldSearchSeen", "validSeenList", "cleanBoundedText",
                              "seenObservedMs", "seenKindOf", "seenKindLabel",
                              "seenProjectIdOf", "seenRowForEntry",
                              "finishSeenSearch"))
        base = self.base_root()
        script = f"""
const vm = require("vm");
const root = {json.dumps(base)};
root.loaded = function(s) {{ this.loaded = s; }};
root.loadFailed = function(s, m) {{ this.failed = m; }};
const seenTimeout = {{stop() {{}}}};
const seenDelay = {{restarts: 0, restart() {{ this.restarts++; }}, stop() {{}}}};
const context = {{root, seenTimeout, seenDelay}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
root.shouldSearchSeen = context.shouldSearchSeen.bind(root);
// Stale process generation: ignored entirely.
root.finishSeenSearch(0, JSON.stringify({{rows: []}}), 1, "deploy");
const staleRows = root.seenRows.length;
// Older pending query while a newer one is typed: reschedule, no install.
root.pendingSeenQuery = "deploy2";
root.seenBusy = true;
root.finishSeenSearch(0, JSON.stringify({{rows: []}}), 2, "deploy");
const rescheduled = seenDelay.restarts;
const busyAfterReschedule = root.seenBusy;
// Generation moved (timeout/cancel): terminal, never reschedule.
root.pendingSeenQuery = "deploy";
root.seenGeneration = 3;
root.seenBusy = true;
context.finishSeenSearch.call(root, 0, JSON.stringify({{rows: []}}), 2, "deploy");
console.log(JSON.stringify({{staleRows, rescheduled, busyAfterReschedule,
  restartsAfterStale: seenDelay.restarts, busyAfterStale: root.seenBusy}}));
"""
        value = json.loads(self.run_node(script))
        self.assertEqual(value["staleRows"], 1)  # untouched old row
        self.assertEqual(value["rescheduled"], 1)
        self.assertFalse(value["busyAfterReschedule"])
        self.assertEqual(value["restartsAfterStale"], 1)
        self.assertTrue(value["busyAfterStale"])

    def test_start_bare_lists_recent_and_cancel_is_terminal(self):
        fns = self.functions(("shouldSearchSeen", "seenListCommand", "startSeenSearch",
                              "cancelSeenSearch", "scheduleSeenSearch", "stopSeenSearch",
                              "validSeenList", "cleanBoundedText", "seenObservedMs",
                              "seenKindOf", "seenKindLabel", "seenProjectIdOf",
                              "seenRowForEntry", "finishSeenSearch"))
        base = self.base_root()
        base["modeQuery"] = ""
        base["seenRows"] = [{"title": "old"}]
        script = f"""
const vm = require("vm");
const root = {json.dumps(base)};
root.loaded = function(s) {{ this.loaded = s; }};
root.loadFailed = function(s, m) {{ this.failed = m; }};
const seenTimeout = {{restarts: 0, stops: 0, restart() {{ this.restarts++; }},
  stop() {{ this.stops++; }}}};
const seenDelay = {{restarts: 0, restart() {{ this.restarts++; }}, stop() {{}}}};
const seenListProcess = {{running: false, command: null}};
const context = {{root, seenTimeout, seenDelay, seenListProcess,
  Quickshell: {{shellPath(v) {{ return "/qs/" + v; }} }}}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
root.shouldSearchSeen = context.shouldSearchSeen.bind(root);
context.startSeenSearch.call(root);
const bareCommand = seenListProcess.command;
const bareRunning = seenListProcess.running;
const bareBusy = root.seenBusy;
const bareTimeoutRestarts = seenTimeout.restarts;
// Non-empty query launches with the bounded command and restarts the timeout.
root.modeQuery = "deploy";
root.seenBusy = false;
seenListProcess.running = false;
seenListProcess.command = null;
context.startSeenSearch.call(root);
const launched = seenListProcess.command;
const launchedRunning = seenListProcess.running;
const launchedBusy = root.seenBusy;
const timeoutRestarts = seenTimeout.restarts;
// Cancel is terminal: a late old-generation exit must not reschedule,
// and stale rows are replaced by the timeout error row.
root.seenGeneration = 2;
root.seenBusy = true;
root.seenRows = [{{title: "old"}}];
context.cancelSeenSearch.call(root);
const cancelledGeneration = root.seenGeneration;
const cancelledError = root.seenError;
const cancelledLoaded = root.loaded;
const cancelledRows = root.seenRows.length;
const delayRestartsAfterCancel = seenDelay.restarts;
const processAfterCancel = seenListProcess.running;
console.log(JSON.stringify({{bareCommand, bareRunning, bareBusy, bareTimeoutRestarts,
  launched, launchedRunning, launchedBusy, timeoutRestarts,
  cancelledGeneration, cancelledError, cancelledLoaded, cancelledRows,
  delayRestartsAfterCancel, processAfterCancel}}));
"""
        value = json.loads(self.run_node(script))
        # Bare `seen:` lists recent through the same mapper command.
        self.assertEqual(value["bareCommand"], ["python3",
                                                "/qs/scripts/desktop_projects.py",
                                                "seen", "--limit", "20"])
        self.assertTrue(value["bareRunning"])
        self.assertTrue(value["bareBusy"])
        self.assertEqual(value["bareTimeoutRestarts"], 1)
        self.assertEqual(value["launched"], ["python3",
                                             "/qs/scripts/desktop_projects.py",
                                             "seen", "--limit", "20",
                                             "--query", "deploy"])
        self.assertTrue(value["launchedRunning"])
        self.assertTrue(value["launchedBusy"])
        self.assertEqual(value["timeoutRestarts"], 2)
        self.assertEqual(value["cancelledGeneration"], 3)
        self.assertEqual(value["cancelledError"], "Seen search timed out; retry")
        self.assertEqual(value["cancelledLoaded"], "seen")
        self.assertEqual(value["cancelledRows"], 0)
        self.assertEqual(value["delayRestartsAfterCancel"], 0)
        self.assertFalse(value["processAfterCancel"])

    def test_start_restarts_debounce_while_running(self):
        fns = self.functions(("shouldSearchSeen", "seenListCommand", "startSeenSearch"))
        script = f"""
const vm = require("vm");
const root = {{mode: "seen", modeQuery: "deploy", paletteOpen: true, seenBusy: false,
  seenGeneration: 0, seenProcessGeneration: 0, seenProcessQuery: "", seenError: ""}};
const seenTimeout = {{restart() {{}}, stop() {{}}}};
const seenDelay = {{restarts: 0, restart() {{ this.restarts++; }}, stop() {{}}}};
const seenListProcess = {{running: true, command: null}};
const context = {{root, seenTimeout, seenDelay, seenListProcess}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
root.shouldSearchSeen = context.shouldSearchSeen.bind(root);
context.startSeenSearch.call(root);
console.log(JSON.stringify({{delays: seenDelay.restarts, busy: root.seenBusy,
  command: seenListProcess.command}}));
"""
        value = json.loads(self.run_node(script))
        self.assertEqual(value["delays"], 1)
        self.assertFalse(value["busy"])
        self.assertIsNone(value["command"])


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class SeenPaletteActionTests(unittest.TestCase):
    def run_node(self, script):
        completed = subprocess.run(
            ["node", "-e", script], text=True, capture_output=True,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr)
        return completed.stdout.strip()

    def test_open_target_gate_table(self):
        fns = {n: extract_function(PALETTE_TEXT, n) for n in ("seenOpenTarget",)}
        long_identity = "url:https://" + "a" * 1024 + ".com"
        script = f"""
const vm = require("vm");
const root = {{}};
const context = {{root}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
const target = (identity) => root.seenOpenTarget({{identity}});
console.log(JSON.stringify({{
  httpUrl: target("url:https://example.com/a"),
  bareHttps: target("https://example.com/a"),
  filePrefixed: target("file:/x/a.py"),
  cwdPrefixed: target("cwd:/x/work"),
  barePath: target("/x/a.py"),
  zotero: target("zotero://select/item/1"),
  pageTitle: target("page:Some Window"),
  relative: target("relative/path.py"),
  blank: target(""),
  long: target("{long_identity}"),
  control: target("url:https://example.com/\\u0001"),
  nonHttp: target("url:ftp://example.com/a"),
  nullEntry: root.seenOpenTarget(null),
  nonString: target(42)
}}));
"""
        value = json.loads(self.run_node(script))
        self.assertEqual(value["httpUrl"], "https://example.com/a")
        self.assertEqual(value["bareHttps"], "https://example.com/a")
        self.assertEqual(value["filePrefixed"], "file:///x/a.py")
        self.assertEqual(value["cwdPrefixed"], "file:///x/work")
        self.assertEqual(value["barePath"], "file:///x/a.py")
        self.assertEqual(value["zotero"], "zotero://select/item/1")
        # Window titles, relative paths, blanks, over-long values,
        # and control characters are refused (Copy still works).
        self.assertEqual(value["pageTitle"], "")
        self.assertEqual(value["relative"], "")
        self.assertEqual(value["blank"], "")
        self.assertEqual(value["long"], "")
        self.assertEqual(value["control"], "")
        self.assertEqual(value["nonHttp"], "")
        self.assertEqual(value["nullEntry"], "")
        self.assertEqual(value["nonString"], "")

    def test_selected_entry_actions_and_defaults(self):
        fns = {n: extract_function(PALETTE_TEXT, n) for n in
               ("selectedSeenEntry", "seenOpenTarget", "seenOpenSelected",
                "seenCopySelected", "seenAskPiSelected", "seenAddTodoHere",
                "seenDefaultAction")}
        script = f"""
const vm = require("vm");
const opened = [];
let closed = 0;
const Qt = {{openUrlExternally(u) {{ opened.push(String(u)); return true; }}}};
const input = {{text: "", forced: 0, forceActiveFocus() {{ this.forced++; }}}};
const root = {{
  mode: "seen", selectedIndex: 0, query: "seen: ",
  rows: [{{kind: "seen", payload: {{kind: "file", label: "a.py",
          identity: "file:/x/a.py", project_id: "p", project_name: "Demo",
          session_id: "s"}}}}],
  notice: "", copies: [],
  copyRawText(value, purpose) {{ this.copies.push([value, purpose]); }},
  close() {{ closed++; }},
  todoRefOverride: ""
}};
const context = {{root, Qt, input, close() {{ closed++; }}}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
const entry = root.selectedSeenEntry();
const openResult = root.seenOpenSelected();
const openedAfter = opened.slice();
const closedAfterOpen = closed;
root.copies = [];
const copyResult = root.seenCopySelected();
const askResult = root.seenAskPiSelected();
const askQuery = root.query;
const askInput = input.text;
const todoResult = root.seenAddTodoHere();
const todoRef = root.todoRefOverride;
const todoQuery = root.query;
const defaultResult = root.seenDefaultAction();
// Unopenable identity falls back to copy for the default action.
root.rows = [{{kind: "seen", payload: {{kind: "page", label: "Win",
  identity: "page:Some Window"}}}}];
root.selectedIndex = 0;
root.copies = [];
const defaultCopy = root.seenDefaultAction();
// No identity at all: default fails, copy/ask/todo refuse.
root.rows = [{{kind: "seen", payload: {{kind: "page", label: "Win",
  identity: ""}}}}];
const defaultNone = root.seenDefaultAction();
const copyNone = root.seenCopySelected();
const askNone = root.seenAskPiSelected();
const todoNone = root.seenAddTodoHere();
const noticeAfter = root.notice;
console.log(JSON.stringify({{entry, openResult, openedAfter, closedAfterOpen,
  copyResult, copies: root.copies, askResult, askQuery, askInput,
  todoResult, todoRef, todoQuery, defaultResult, defaultCopy,
  defaultNone, copyNone, askNone, todoNone, notice: noticeAfter}}));
"""
        value = json.loads(self.run_node(script))
        self.assertEqual(value["entry"]["identity"], "file:/x/a.py")
        # Open is navigation: opens the target and closes.
        self.assertTrue(value["openResult"])
        self.assertEqual(value["openedAfter"], ["file:///x/a.py"])
        self.assertEqual(value["closedAfterOpen"], 1)
        self.assertTrue(value["copyResult"])
        # Ask Pi routes into the unified chat as visible, editable
        # context — never auto-sent.
        self.assertTrue(value["askResult"])
        self.assertTrue(value["askQuery"].startswith("ai: About this resource (a.py): "))
        self.assertIn("file:/x/a.py", value["askQuery"])
        self.assertEqual(value["askInput"], value["askQuery"])
        # Add TODO here pins the identity as single-use provenance and
        # prefills the todo: composer.
        self.assertTrue(value["todoResult"])
        self.assertEqual(value["todoRef"], "file:/x/a.py")
        self.assertEqual(value["todoQuery"], "todo: ")
        self.assertTrue(value["defaultResult"])
        self.assertTrue(value["defaultCopy"])
        self.assertEqual(value["copies"], [["page:Some Window", "resource"]])
        self.assertFalse(value["defaultNone"])
        self.assertFalse(value["copyNone"])
        self.assertFalse(value["askNone"])
        self.assertFalse(value["todoNone"])

    def test_add_todo_here_pins_row_project_as_target(self):
        # S-045: the pinned seen row's project becomes the TODO target;
        # rows without a project keep the current-project behavior.
        fns = {n: extract_function(PALETTE_TEXT, n) for n in
               ("selectedSeenEntry", "seenAddTodoHere")}
        pid = "11111111-1111-4111-8111-111111111111"
        script = f"""
const vm = require("vm");
const input = {{text: "", forceActiveFocus() {{}}}};
const root = {{
  mode: "seen", selectedIndex: 0, query: "seen: ",
  rows: [{{kind: "seen", payload: {{kind: "file", label: "a.py",
          identity: "file:/x/a.py", project_id: "{pid}",
          project_name: "Demo", session_id: "s"}}}}],
  notice: "", query: "seen: ",
  todoRefOverride: "", todoTargetProjectOverride: "",
  todoTargetProjectNameOverride: ""
}};
const context = {{root, input}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
const ok = root.seenAddTodoHere();
const pinned = {{ref: root.todoRefOverride,
  project: root.todoTargetProjectOverride,
  name: root.todoTargetProjectNameOverride, query: root.query}};
// A row without a project pins only the ref: current behavior stands.
root.rows = [{{kind: "seen", payload: {{kind: "page", label: "Win",
  identity: "page:Some Window", project_id: "",
  project_name: ""}}}}];
root.selectedIndex = 0;
const okBare = root.seenAddTodoHere();
const bare = {{ref: root.todoRefOverride,
  project: root.todoTargetProjectOverride,
  name: root.todoTargetProjectNameOverride}};
// A malformed project id never becomes a target.
root.rows = [{{kind: "seen", payload: {{kind: "file", label: "a.py",
  identity: "file:/x/a.py", project_id: "not-a-uuid",
  project_name: "Demo"}}}}];
const okBad = root.seenAddTodoHere();
const bad = {{project: root.todoTargetProjectOverride,
  name: root.todoTargetProjectNameOverride}};
console.log(JSON.stringify({{ok, pinned, okBare, bare, okBad, bad}}));
"""
        value = json.loads(self.run_node(script))
        self.assertTrue(value["ok"])
        self.assertEqual(value["pinned"]["ref"], "file:/x/a.py")
        self.assertEqual(value["pinned"]["project"], pid)
        self.assertEqual(value["pinned"]["name"], "Demo")
        self.assertEqual(value["pinned"]["query"], "todo: ")
        self.assertTrue(value["okBare"])
        self.assertEqual(value["bare"]["ref"], "page:Some Window")
        self.assertEqual(value["bare"]["project"], "")
        self.assertEqual(value["bare"]["name"], "")
        self.assertTrue(value["okBad"])
        self.assertEqual(value["bad"]["project"], "")
        self.assertEqual(value["bad"]["name"], "")

    def test_not_in_seen_mode_selects_nothing(self):
        fns = {n: extract_function(PALETTE_TEXT, n) for n in
               ("selectedSeenEntry", "seenDefaultAction")}
        script = f"""
const vm = require("vm");
const root = {{mode: "resume", selectedIndex: 0,
  rows: [{{kind: "seen", payload: {{identity: "file:/x/a.py"}}}}]}};
const context = {{root}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
console.log(JSON.stringify({{entry: root.selectedSeenEntry(),
  action: root.seenDefaultAction()}}));
"""
        value = json.loads(self.run_node(script))
        self.assertIsNone(value["entry"])
        self.assertFalse(value["action"])

    def test_open_failure_is_bounded_and_keeps_palette_open(self):
        fns = {n: extract_function(PALETTE_TEXT, n) for n in
               ("selectedSeenEntry", "seenOpenTarget", "seenOpenSelected")}
        script = f"""
const vm = require("vm");
let closed = 0;
const Qt = {{openUrlExternally(u) {{ throw new Error("denied"); }}}};
const root = {{
  mode: "seen", selectedIndex: 0,
  rows: [{{kind: "seen", payload: {{kind: "file", label: "a.py",
          identity: "file:/x/a.py"}}}}],
  notice: "",
  close() {{ closed++; }},
}};
const context = {{root, Qt, close() {{ closed++; }}}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
const result = root.seenOpenSelected();
const bareNotice = context.notice === undefined ? null : context.notice;
console.log(JSON.stringify({{result, notice: bareNotice, closed}}));
"""
        value = json.loads(self.run_node(script))
        self.assertFalse(value["result"])
        self.assertEqual(value["notice"], "Could not open resource")
        self.assertEqual(value["closed"], 0)


class SeenStructuralTests(unittest.TestCase):
    def test_datasources_wiring(self):
        text = DATASOURCES_TEXT
        for name in ("shouldSearchSeen", "validSeenList", "seenListCommand",
                     "cleanBoundedText", "seenObservedMs", "seenKindOf",
                     "seenKindLabel", "seenProjectIdOf", "seenRowForEntry",
                     "finishSeenSearch", "startSeenSearch",
                     "scheduleSeenSearch", "stopSeenSearch",
                     "cancelSeenSearch"):
            self.assertIn("function " + name + "(", text)
        self.assertIn("scripts/desktop_projects.py", text)
        self.assertIn('"seen"', text)
        self.assertIn("seenDelay", text)
        self.assertIn("seenTimeout", text)
        self.assertIn("interval: 180", text)
        self.assertIn("interval: 12000", text)
        self.assertIn('"Seen search timed out; retry"', text)
        self.assertIn('"Seen search failed"', text)
        self.assertIn('"Seen search returned invalid data"', text)
        # hist: is gone: no hist process, rows, timers, or commands.
        for gone in ("histListProcess", "histRows", "histGeneration",
                     "histProcessGeneration", "pendingHistQuery",
                     "histProcessQuery", "histListCommand", "histRowForSession",
                     "finishHistSearch", "startHistSearch", "scheduleHistSearch",
                     "stopHistSearch", "cancelHistSearch", "shouldSearchHist",
                     "validHistList", "histCleanText", "histObservedMs",
                     "histResourceOf", "histDelay", "histTimeout",
                     '"search-activity"'):
            self.assertNotIn(gone, text)

    def test_palette_wiring(self):
        text = PALETTE_TEXT
        self.assertIn("dataSources.seenRows", text)
        self.assertIn('root.mode === "seen"', text)
        self.assertIn("dataSources.scheduleSeenSearch()", text)
        self.assertIn('"seen: search resources · empty lists recent"', text)
        self.assertIn('"Searching seen resources…"', text)
        self.assertIn('"Showing recent resources"', text)
        self.assertIn('objectName: "seenOpenButton"', text)
        self.assertIn('objectName: "seenAskPiButton"', text)
        self.assertIn('objectName: "seenCopyButton"', text)
        self.assertIn('objectName: "seenTodoButton"', text)
        self.assertIn('"Resource copy failed"', text)
        self.assertIn('purpose === "resource"', text)
        self.assertIn("icons/copy.svg", text)
        # hist: affordances are gone with the prefix.
        for gone in ('objectName: "histResumeButton"',
                     '"hist: search activity · empty lists recent"',
                     '"Searching activity…"',
                     '"Showing recent activity"',
                     "selectedHistEntry",
                     "histResumeSelected",
                     "histHistorySelected",
                     "histCopySelected",
                     "histDefaultAction"):
            self.assertNotIn(gone, text)

    def test_no_model_or_write_surface(self):
        # seen: is local navigation only: it must never touch the agent,
        # the annotations sidecar, or a Jev helper. Only the Ask-Pi
        # prefill (visible, never auto-sent), the todo: prefill, and the
        # opener/wl-copy path may be reached.
        for text in (PALETTE_TEXT, DATASOURCES_TEXT):
            for line in text.splitlines():
                if "seen" not in line.lower() and "Seen search" not in line:
                    continue
                self.assertNotIn("agent.prompt", line)
                self.assertNotIn("annotations", line)
                self.assertNotIn("jev", line.lower())
                self.assertNotIn("memory_tick", line)


if __name__ == "__main__":
    unittest.main()
