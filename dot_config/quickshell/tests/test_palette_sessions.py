"""Focused palette `session:` unified-source tests (Phase 2b §4.2).

Runs the real PaletteQuery.js / PaletteDataSources.qml / CommandPalette.qml
JavaScript in node, like tests/test_palette_seen.py: no QML engine, no
network, no store access. scripts/sessions.py itself is covered
elsewhere; here the payload shape is simulated.

§4.2: `session:`/`work:` and `inbox:` are one source over the §0.2 union
(content index ∪ activity FTS), deduped by session_id, newest first.
`inbox:` is deleted (its meaning is the default sort and the bar badge).
Rows show time range · project · match source; the union payload carries
no TODO counts and no thought text beyond the matched snippet. Actions:
Open session (planner Daily tab focused on that day/session), Resume
project, Copy resource. No states, no titles, no Logseq URIs.
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
class SessionDataSourceTests(unittest.TestCase):
    def run_node(self, script):
        completed = subprocess.run(
            ["node", "-e", script], text=True, capture_output=True,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr)
        return completed.stdout.strip()

    def functions(self, names):
        return {n: extract_function(DATASOURCES_TEXT, n) for n in names}

    def test_command_is_single_arg_and_scope_is_session_only(self):
        fns = self.functions(("sessionListCommand", "shouldSearchSessions",
                              "validSessionList", "sessionRequestQuery"))
        script = f"""
const vm = require("vm");
const root = {{mode: "session", modeQuery: "deploy"}};
const context = {{root, Quickshell: {{shellPath(v) {{ return "/qs/" + v; }} }}}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
// Single-arg (query only): bare lists recent, a needle searches.
const searchCommand = context.sessionListCommand.call(root, "deploy");
const bareCommand = context.sessionListCommand.call(root, "");
const sessionOn = context.shouldSearchSessions.call(root);
const sessionNeedle = context.sessionRequestQuery.call(root);
root.mode = "inbox";
const inboxOff = context.shouldSearchSessions.call(root);
root.mode = "resume";
const resumeOff = context.shouldSearchSessions.call(root);
root.mode = "seen";
const seenOff = context.shouldSearchSessions.call(root);
console.log(JSON.stringify({{
  searchCommand,
  bareCommand,
  sessionOn,
  sessionNeedle,
  inboxOff,
  resumeOff,
  seenOff,
  valid: context.validSessionList.call(root, {{entries: []}}),
  invalidMissing: context.validSessionList.call(root, {{sessions: []}}),
  invalidRows: context.validSessionList.call(root, {{rows: []}}),
  invalidNull: context.validSessionList.call(root, null)
}}));
"""
        value = json.loads(self.run_node(script))
        self.assertEqual(value["searchCommand"], ["python3",
                                                  "/qs/scripts/sessions.py",
                                                  "search", "--query", "deploy",
                                                  "--limit", "20"])
        self.assertEqual(value["bareCommand"], ["python3",
                                                "/qs/scripts/sessions.py",
                                                "list", "--limit", "20"])
        self.assertTrue(value["sessionOn"])
        self.assertEqual(value["sessionNeedle"], "deploy")
        # inbox: is deleted: it no longer scopes the source.
        self.assertFalse(value["inboxOff"])
        self.assertFalse(value["resumeOff"])
        self.assertFalse(value["seenOff"])
        self.assertTrue(value["valid"])
        self.assertFalse(value["invalidMissing"])
        self.assertFalse(value["invalidRows"])
        self.assertFalse(value["invalidNull"])

    def test_range_label_shape(self):
        fns = self.functions(("sessionClock", "sessionRangeLabel"))
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
const both = context.sessionRangeLabel.call(root, 1700000000000, 1700003600000);
const startOnly = context.sessionRangeLabel.call(root, 1700000000000, null);
const neither = context.sessionRangeLabel.call(root, null, null);
const badEnd = context.sessionRangeLabel.call(root, 1700000000000, "x");
const badBoth = context.sessionRangeLabel.call(root, "a", "b");
console.log(JSON.stringify({{both, startOnly, neither, badEnd, badBoth}}));
"""
        value = json.loads(self.run_node(script))
        self.assertIn("–", value["both"])
        self.assertEqual(len(value["both"]), 11)
        self.assertRegex(value["startOnly"], r"^\d\d:\d\d$")
        self.assertEqual(value["neither"], "")
        self.assertRegex(value["badEnd"], r"^\d\d:\d\d$")
        self.assertEqual(value["badBoth"], "")

    def test_row_search_shape_no_title_state_or_logseq(self):
        fns = self.functions(("cleanBoundedText", "sessionClock", "sessionRangeLabel",
                              "sessionProjectIdOf",
                              "sessionRowForSearchEntry"))
        sid = "b" * 32
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
const row = context.sessionRowForSearchEntry.call(root, {{
  session_id: "{sid}",
  project_id: "22222222-2222-4222-8222-222222222222",
  project_name: "Demo",
  matched: "content",
  resource_copy: "fix the retry backoff",
  start_ms: 1700000000000,
  end_ms: 1700003600000
}});
const collector = context.sessionRowForSearchEntry.call(root, {{
  session_id: "{sid}",
  project_id: "22222222-2222-4222-8222-222222222222",
  project_name: "Demo",
  matched: "collector",
  resource_copy: "/x/a.py",
  start_ms: 1700000000000,
  end_ms: null
}});
const fallback = context.sessionRowForSearchEntry.call(root, {{
  session_id: "{sid}", project_id: null, project_name: null,
  matched: "other", resource_copy: null, start_ms: null, end_ms: null
}});
const noId = context.sessionRowForSearchEntry.call(root, {{project_id: "p"}});
const nullEntry = context.sessionRowForSearchEntry.call(root, null);
console.log(JSON.stringify({{row, collector, fallback, noId, nullEntry}}));
"""
        value = json.loads(self.run_node(script))
        row = value["row"]
        self.assertEqual(row["prefix"], "session:")
        self.assertEqual(row["kind"], "session")
        # Title is the matched snippet, never a stored title.
        self.assertEqual(row["title"], "fix the retry backoff")
        self.assertIn("Demo", row["subtitle"])
        self.assertIn("–", row["subtitle"])
        self.assertIn("thought/TODO match", row["subtitle"])
        # Payload is the flat union row: no title/state/next_step/filed_uri.
        self.assertEqual(set(row["payload"].keys()),
                         {"session_id", "project_id", "project_name",
                          "matched", "resource_copy", "start_ms", "end_ms"})
        self.assertEqual(row["payload"]["session_id"], "b" * 32)
        self.assertEqual(row["payload"]["project_id"],
                         "22222222-2222-4222-8222-222222222222")
        self.assertEqual(row["payload"]["project_name"], "Demo")
        self.assertEqual(row["payload"]["matched"], "content")
        self.assertEqual(row["payload"]["resource_copy"], "fix the retry backoff")
        self.assertEqual(row["payload"]["start_ms"], 1700000000000)
        self.assertEqual(row["payload"]["end_ms"], 1700003600000)
        self.assertEqual(row["score"], 1)
        self.assertIn("activity match", value["collector"]["subtitle"])
        self.assertEqual(value["collector"]["title"], "/x/a.py")
        # No snippet and no range: bare session id title, unresolved
        # project, no match-source suffix.
        self.assertEqual(value["fallback"]["title"], "Session " + "b" * 8)
        self.assertEqual(value["fallback"]["subtitle"], "Unresolved project")
        self.assertEqual(value["fallback"]["payload"]["project_id"], "")
        self.assertEqual(value["fallback"]["payload"]["matched"], "")
        self.assertEqual(value["fallback"]["payload"]["resource_copy"], "")
        self.assertIsNone(value["fallback"]["payload"]["start_ms"])
        self.assertIsNone(value["fallback"]["payload"]["end_ms"])
        self.assertIsNone(value["noId"])
        self.assertIsNone(value["nullEntry"])

    def test_row_list_nested_shape_pending_and_attended(self):
        fns = self.functions(("cleanBoundedText", "sessionClock", "sessionRangeLabel",
                              "sessionProjectIdOf",
                              "sessionRowForListEntry"))
        sid = "c" * 32
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
const row = context.sessionRowForListEntry.call(root, {{
  session: {{session_id: "{sid}", project: {{id: "11111111-1111-4111-8111-111111111111", name: "Demo"}},
    start_ms: 1700000000000, end_ms: 1700003600000}},
  meta: null, pending: true, attended: false
}});
const done = context.sessionRowForListEntry.call(root, {{
  session: {{session_id: "{sid}", project: {{id: "11111111-1111-4111-8111-111111111111", name: "Demo"}},
    start_ms: 1700000000000, end_ms: null}},
  meta: null, pending: false, attended: true
}});
const fallback = context.sessionRowForListEntry.call(root, {{
  session: {{session_id: "{sid}", project: null, start_ms: null, end_ms: null}},
  meta: null, pending: false, attended: false
}});
const noSession = context.sessionRowForListEntry.call(root, {{meta: null}});
const noId = context.sessionRowForListEntry.call(root, {{session: {{project: null}}, meta: null}});
console.log(JSON.stringify({{row, done, fallback, noSession, noId}}));
"""
        value = json.loads(self.run_node(script))
        row = value["row"]
        self.assertEqual(row["prefix"], "session:")
        self.assertEqual(row["kind"], "session")
        # Nested rows carry no snippet: title is the session id with the
        # attention marker, never a stored title.
        self.assertEqual(row["title"], "Session " + "c" * 8 + " · needs attention")
        self.assertIn("Demo", row["subtitle"])
        self.assertIn("–", row["subtitle"])
        self.assertIn("needs attention", row["subtitle"])
        self.assertEqual(set(row["payload"].keys()),
                         {"session_id", "project_id", "project_name",
                          "pending", "attended", "resource_copy",
                          "start_ms", "end_ms"})
        self.assertEqual(row["payload"]["session_id"], "c" * 32)
        self.assertEqual(row["payload"]["project_id"],
                         "11111111-1111-4111-8111-111111111111")
        self.assertEqual(row["payload"]["project_name"], "Demo")
        self.assertTrue(row["payload"]["pending"])
        self.assertFalse(row["payload"]["attended"])
        self.assertEqual(row["payload"]["resource_copy"], "")
        self.assertEqual(row["payload"]["start_ms"], 1700000000000)
        self.assertIn("done", value["done"]["subtitle"])
        self.assertNotIn("needs attention", value["done"]["title"])
        self.assertEqual(value["fallback"]["title"], "Session " + "c" * 8)
        self.assertEqual(value["fallback"]["subtitle"], "Unresolved project")
        self.assertIsNone(value["noSession"])
        self.assertIsNone(value["noId"])

    def test_row_session_id_validation_both_builders(self):
        fns = self.functions(("cleanBoundedText", "sessionClock", "sessionRangeLabel",
                              "sessionProjectIdOf",
                              "sessionRowForSearchEntry",
                              "sessionRowForListEntry"))
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
const search = (sid) => context.sessionRowForSearchEntry.call(root, {{
  session_id: sid, project_id: "22222222-2222-4222-8222-222222222222",
  project_name: "Demo", matched: "content",
  resource_copy: "/x/a.py", start_ms: null, end_ms: null
}});
const listed = (sid) => context.sessionRowForListEntry.call(root, {{
  session: {{session_id: sid,
    project: {{id: "11111111-1111-4111-8111-111111111111", name: "Demo"}},
    start_ms: null, end_ms: null}},
  meta: null, pending: true, attended: false
}});
const lower = "abcdef0123456789abcdef0123456789";
const upper = lower.toUpperCase();
console.log(JSON.stringify({{
  shortSearch: search("abc"),
  shortList: listed("abc"),
  nonHexSearch: search("g".repeat(32)),
  nonHexList: listed("z".repeat(32)),
  emptySearch: search(""),
  emptyList: listed(""),
  longSearch: search("b".repeat(33)),
  longList: listed("b".repeat(33)),
  lowerSearch: search(lower),
  lowerList: listed(lower),
  upperSearch: search(upper),
  upperList: listed(upper)
}}));
"""
        value = json.loads(self.run_node(script))
        for key in ("shortSearch", "shortList", "nonHexSearch",
                    "nonHexList", "emptySearch", "emptyList",
                    "longSearch", "longList"):
            self.assertIsNone(value[key], key)
        for key in ("lowerSearch", "lowerList",
                    "upperSearch", "upperList"):
            self.assertIsNotNone(value[key], key)
            self.assertEqual(value[key]["kind"], "session")
        self.assertEqual(value["lowerSearch"]["payload"]["session_id"],
                         "abcdef0123456789abcdef0123456789")
        self.assertEqual(value["upperSearch"]["payload"]["session_id"],
                         "ABCDEF0123456789ABCDEF0123456789")
        self.assertEqual(value["lowerList"]["payload"]["session_id"],
                         "abcdef0123456789abcdef0123456789")
        self.assertEqual(value["upperList"]["payload"]["session_id"],
                         "ABCDEF0123456789ABCDEF0123456789")

    def test_row_payload_bounds_both_builders(self):
        fns = self.functions(("cleanBoundedText", "sessionClock", "sessionRangeLabel",
                              "sessionProjectIdOf",
                              "sessionRowForSearchEntry",
                              "sessionRowForListEntry"))
        sid = "d" * 32
        long_name = "N" * 300
        long_copy = "/x/" + "c" * 300
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
const searchRow = context.sessionRowForSearchEntry.call(root, {{
  session_id: "{sid}", project_id: "not-a-uuid",
  project_name: "{long_name}", matched: "content",
  resource_copy: "{long_copy}\\u0001\\u0007",
  start_ms: 1700000000000, end_ms: null
}});
const listRow = context.sessionRowForListEntry.call(root, {{
  session: {{session_id: "{sid}", project: {{id: "bad-id", name: "{long_name}"}},
    start_ms: 1700000000000, end_ms: null}},
  meta: null, pending: true, attended: false
}});
console.log(JSON.stringify({{searchRow, listRow}}));
"""
        value = json.loads(self.run_node(script))
        for label in ("searchRow", "listRow"):
            row = value[label]
            self.assertIsNotNone(row, label)
            # Non-UUID project ids fail closed, never echoed.
            self.assertEqual(row["payload"]["project_id"], "", label)
            for key, field in row["payload"].items():
                if isinstance(field, str):
                    self.assertLessEqual(len(field), 160,
                                         f"{label}.payload.{key}")
                    self.assertNotIn("\u0001", field)
                    self.assertNotIn("\u0007", field)
        self.assertEqual(len(value["searchRow"]["payload"]["project_name"]), 160)
        self.assertEqual(len(value["listRow"]["payload"]["project_name"]), 160)
        self.assertEqual(value["searchRow"]["payload"]["resource_copy"], "/x/" + "c" * 157)

    def base_root(self, query="deploy"):
        return {
            "openingGeneration": 1, "requestedOpen": True, "paletteOpen": True,
            "mode": "session", "modeQuery": query, "calculatorOnly": False,
            "sessionGeneration": 2, "sessionProcessGeneration": 2,
            "pendingSessionQuery": query, "sessionProcessQuery": query,
            "sessionBusy": True, "sessionError": "",
            "sessionRows": [{"title": "old"}],
            "loaded": None, "failed": None, "loadedCount": 0, "failedCount": 0,
        }

    def test_finish_installs_flat_rows_for_search(self):
        fns = self.functions(("shouldSearchSessions", "sessionRequestQuery",
                              "validSessionList", "cleanBoundedText", "sessionClock",
                              "sessionRangeLabel", "sessionProjectIdOf",
                              "sessionRowForSearchEntry",
                              "sessionRowForListEntry",
                              "storeSessionTruncation", "finishSessionSearch"))
        script = f"""
const vm = require("vm");
const root = {json.dumps(self.base_root())};
root.loaded = function(s) {{ this.loaded = s; this.loadedCount++; }};
root.loadFailed = function(s, m) {{ this.failed = m; this.failedCount++; }};
const sessionTimeout = {{stops: 0, stop() {{ this.stops++; }}}};
const sessionDelay = {{restarts: 0, restart() {{ this.restarts++; }}, stop() {{}}}};
const context = {{root, sessionTimeout, sessionDelay}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
root.shouldSearchSessions = context.shouldSearchSessions.bind(root);
root.sessionRequestQuery = context.sessionRequestQuery.bind(root);
const entry = (i) => ({{
  session_id: String(i).padStart(32, "0"),
  project_id: "22222222-2222-4222-8222-222222222222", project_name: "Demo",
  matched: "content", resource_copy: "snippet " + i,
  start_ms: 1700000000000, end_ms: null
}});
const entries = Array.from({{length: 25}}, (_, i) => entry(i + 1));
context.finishSessionSearch.call(root, 0, JSON.stringify({{entries, count: 25, total: 25, truncated: true}}), 2, "deploy");
console.log(JSON.stringify({{
  count: root.sessionRows.length,
  first: root.sessionRows[0],
  truncated: root.sessionTruncated,
  total: root.sessionTotal,
  loaded: root.loaded, error: root.sessionError, stops: sessionTimeout.stops
}}));
"""
        value = json.loads(self.run_node(script))
        self.assertEqual(value["count"], 20)
        self.assertEqual(value["first"]["kind"], "session")
        self.assertEqual(value["first"]["prefix"], "session:")
        self.assertEqual(value["first"]["title"], "snippet 1")
        # S-041: search reports truncated/total for "showing X of Y".
        self.assertTrue(value["truncated"])
        self.assertEqual(value["total"], 25)
        self.assertEqual(value["loaded"], "session")
        self.assertEqual(value["error"], "")
        self.assertEqual(value["stops"], 1)

    def test_finish_bare_installs_nested_rows(self):
        fns = self.functions(("shouldSearchSessions", "sessionRequestQuery",
                              "validSessionList", "cleanBoundedText", "sessionClock",
                              "sessionRangeLabel", "sessionProjectIdOf",
                              "sessionRowForSearchEntry",
                              "sessionRowForListEntry",
                              "storeSessionTruncation", "finishSessionSearch"))
        base = self.base_root(query="")
        sid = "a" * 32
        script = f"""
const vm = require("vm");
const root = {json.dumps(base)};
root.loaded = function(s) {{ this.loaded = s; }};
root.loadFailed = function(s, m) {{ this.failed = m; }};
const sessionTimeout = {{stop() {{}}}};
const sessionDelay = {{restart() {{}}, stop() {{}}}};
const context = {{root, sessionTimeout, sessionDelay}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
root.shouldSearchSessions = context.shouldSearchSessions.bind(root);
root.sessionRequestQuery = context.sessionRequestQuery.bind(root);
const payload = {{entries: [{{session: {{session_id: "{sid}", project: {{id: "11111111-1111-4111-8111-111111111111", name: "Demo"}}, start_ms: null, end_ms: null}}, meta: null, pending: true, attended: false}}]}};
context.finishSessionSearch.call(root, 0, JSON.stringify(payload), 2, "");
console.log(JSON.stringify({{rows: root.sessionRows, loaded: root.loaded, error: root.sessionError, truncated: root.sessionTruncated, total: root.sessionTotal}}));
"""
        value = json.loads(self.run_node(script))
        self.assertEqual(len(value["rows"]), 1)
        self.assertEqual(value["rows"][0]["payload"]["project_id"],
                         "11111111-1111-4111-8111-111111111111")
        self.assertIn("needs attention", value["rows"][0]["title"])
        # No backend total on this payload: degrades to shown, silent.
        self.assertFalse(value["truncated"])
        self.assertEqual(value["total"], 1)
        self.assertEqual(value["loaded"], "session")
        self.assertEqual(value["error"], "")

    def test_finish_rejects_invalid_and_reports_process_failure(self):
        fns = self.functions(("shouldSearchSessions", "sessionRequestQuery",
                              "validSessionList", "cleanBoundedText", "sessionClock",
                              "sessionRangeLabel", "sessionProjectIdOf",
                              "sessionRowForSearchEntry",
                              "sessionRowForListEntry", "finishSessionSearch"))
        base = self.base_root()
        script = f"""
const vm = require("vm");
const root = {json.dumps(base)};
root.loaded = function(s) {{ this.loaded = s; }};
root.loadFailed = function(s, m) {{ this.failed = m; }};
const sessionTimeout = {{stop() {{}}}};
const sessionDelay = {{restart() {{}}, stop() {{}}}};
const context = {{root, sessionTimeout, sessionDelay}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
root.shouldSearchSessions = context.shouldSearchSessions.bind(root);
root.sessionRequestQuery = context.sessionRequestQuery.bind(root);
root.finishSessionSearch(0, '{{"nope":1}}', 2, "deploy");
const invalidError = root.sessionError;
const invalidLoaded = root.loaded;
const invalidRows = root.sessionRows.length;
root.sessionError = ""; root.loaded = function(s) {{ this.loaded = s; }};
root.sessionRows = [{{title: "keep"}}];
root.finishSessionSearch(1, "", 2, "deploy");
console.log(JSON.stringify({{
  invalidError, invalidLoaded, invalidRows,
  failed: root.failed, loadedAfterFailure: root.loaded, rowsAfterFailure: root.sessionRows.length,
  failureError: root.sessionError
}}));
"""
        value = json.loads(self.run_node(script))
        self.assertEqual(value["invalidError"], "Session search returned invalid data")
        self.assertEqual(value["invalidLoaded"], "session")
        self.assertEqual(value["invalidRows"], 0)
        # Process failure clears stale rows and rebuilds through loaded
        # (one error row + Enter-to-retry in the palette). One message
        # for every mode: there is no inbox-specific text anymore.
        self.assertEqual(value["failureError"], "Session search failed")
        self.assertEqual(value["loadedAfterFailure"], "session")
        self.assertEqual(value["rowsAfterFailure"], 0)

    def test_stale_and_pending_query_completions(self):
        fns = self.functions(("shouldSearchSessions", "sessionRequestQuery",
                              "validSessionList", "cleanBoundedText", "sessionClock",
                              "sessionRangeLabel", "sessionProjectIdOf",
                              "sessionRowForSearchEntry",
                              "sessionRowForListEntry", "finishSessionSearch"))
        base = self.base_root()
        script = f"""
const vm = require("vm");
const root = {json.dumps(base)};
root.loaded = function(s) {{ this.loaded = s; }};
root.loadFailed = function(s, m) {{ this.failed = m; }};
const sessionTimeout = {{stop() {{}}}};
const sessionDelay = {{restarts: 0, restart() {{ this.restarts++; }}, stop() {{}}}};
const context = {{root, sessionTimeout, sessionDelay}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
root.shouldSearchSessions = context.shouldSearchSessions.bind(root);
root.sessionRequestQuery = context.sessionRequestQuery.bind(root);
// Stale process generation: ignored entirely.
root.finishSessionSearch(0, JSON.stringify({{entries: []}}), 1, "deploy");
const staleRows = root.sessionRows.length;
// Older pending query while a newer one is typed: reschedule, no install.
root.pendingSessionQuery = "deploy2";
root.sessionBusy = true;
root.finishSessionSearch(0, JSON.stringify({{entries: []}}), 2, "deploy");
const rescheduled = sessionDelay.restarts;
const busyAfterReschedule = root.sessionBusy;
// Generation moved (timeout/cancel): terminal, never reschedule.
root.pendingSessionQuery = "deploy";
root.sessionGeneration = 3;
root.sessionBusy = true;
context.finishSessionSearch.call(root, 0, JSON.stringify({{entries: []}}), 2, "deploy");
console.log(JSON.stringify({{staleRows, rescheduled, busyAfterReschedule,
  restartsAfterStale: sessionDelay.restarts, busyAfterStale: root.sessionBusy}}));
"""
        value = json.loads(self.run_node(script))
        self.assertEqual(value["staleRows"], 1)
        self.assertEqual(value["rescheduled"], 1)
        self.assertFalse(value["busyAfterReschedule"])
        self.assertEqual(value["restartsAfterStale"], 1)
        self.assertTrue(value["busyAfterStale"])

    def test_start_bare_lists_recent_and_cancel_is_terminal(self):
        fns = self.functions(("shouldSearchSessions", "sessionRequestQuery",
                              "sessionListCommand", "startSessionSearch",
                              "cancelSessionSearch", "scheduleSessionSearch",
                              "stopSessionSearch", "validSessionList",
                              "cleanBoundedText", "sessionClock", "sessionRangeLabel",
                              "sessionProjectIdOf",
                              "sessionRowForSearchEntry", "sessionRowForListEntry",
                              "finishSessionSearch"))
        base = self.base_root()
        base["modeQuery"] = ""
        base["pendingSessionQuery"] = ""
        base["sessionProcessQuery"] = ""
        base["sessionRows"] = [{"title": "old"}]
        script = f"""
const vm = require("vm");
const root = {json.dumps(base)};
root.loaded = function(s) {{ this.loaded = s; }};
root.loadFailed = function(s, m) {{ this.failed = m; }};
const sessionTimeout = {{restarts: 0, stops: 0, restart() {{ this.restarts++; }},
  stop() {{ this.stops++; }}}};
const sessionDelay = {{restarts: 0, restart() {{ this.restarts++; }}, stop() {{}}}};
const sessionListProcess = {{running: false, command: null}};
const context = {{root, sessionTimeout, sessionDelay, sessionListProcess,
  Quickshell: {{shellPath(v) {{ return "/qs/" + v; }} }}}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
root.shouldSearchSessions = context.shouldSearchSessions.bind(root);
root.sessionRequestQuery = context.sessionRequestQuery.bind(root);
context.startSessionSearch.call(root);
const bareCommand = sessionListProcess.command;
const bareRunning = sessionListProcess.running;
const bareBusy = root.sessionBusy;
const bareTimeoutRestarts = sessionTimeout.restarts;
// Non-empty query launches with the bounded command and restarts the timeout.
root.modeQuery = "deploy";
root.pendingSessionQuery = "deploy";
root.sessionBusy = false;
sessionListProcess.running = false;
sessionListProcess.command = null;
context.startSessionSearch.call(root);
const launched = sessionListProcess.command;
const launchedRunning = sessionListProcess.running;
const launchedBusy = root.sessionBusy;
const timeoutRestarts = sessionTimeout.restarts;
// Cancel is terminal: a late old-generation exit must not reschedule,
// and stale rows are replaced by the timeout error row.
root.modeQuery = "deploy";
root.pendingSessionQuery = "deploy";
root.sessionGeneration = 2;
root.sessionBusy = true;
root.sessionRows = [{{title: "old"}}];
sessionListProcess.running = true;
context.cancelSessionSearch.call(root);
const cancelledGeneration = root.sessionGeneration;
const cancelledError = root.sessionError;
const cancelledLoaded = root.loaded;
const cancelledRows = root.sessionRows.length;
const delayRestartsAfterCancel = sessionDelay.restarts;
const processAfterCancel = sessionListProcess.running;
console.log(JSON.stringify({{bareCommand, bareRunning, bareBusy, bareTimeoutRestarts,
  launched, launchedRunning, launchedBusy, timeoutRestarts,
  cancelledGeneration, cancelledError, cancelledLoaded, cancelledRows,
  delayRestartsAfterCancel, processAfterCancel}}));
"""
        value = json.loads(self.run_node(script))
        # Bare `session:` lists recent work sessions via the no-query `list`
        # command (search rejects empty queries).
        self.assertEqual(value["bareCommand"], ["python3",
                                                "/qs/scripts/sessions.py",
                                                "list", "--limit", "20"])
        self.assertTrue(value["bareRunning"])
        self.assertTrue(value["bareBusy"])
        self.assertEqual(value["bareTimeoutRestarts"], 1)
        self.assertEqual(value["launched"], ["python3",
                                             "/qs/scripts/sessions.py",
                                             "search", "--query", "deploy",
                                             "--limit", "20"])
        self.assertTrue(value["launchedRunning"])
        self.assertTrue(value["launchedBusy"])
        self.assertEqual(value["timeoutRestarts"], 2)
        self.assertEqual(value["cancelledGeneration"], 3)
        self.assertEqual(value["cancelledError"], "Session search timed out; retry")
        self.assertEqual(value["cancelledLoaded"], "session")
        self.assertEqual(value["cancelledRows"], 0)
        self.assertEqual(value["delayRestartsAfterCancel"], 0)
        self.assertFalse(value["processAfterCancel"])

    def test_start_restarts_debounce_while_running(self):
        fns = self.functions(("shouldSearchSessions", "sessionRequestQuery",
                              "sessionListCommand", "startSessionSearch"))
        script = f"""
const vm = require("vm");
const root = {{mode: "session", modeQuery: "deploy", paletteOpen: true, sessionBusy: false,
  sessionGeneration: 0, sessionProcessGeneration: 0, sessionProcessQuery: "", sessionError: ""}};
const sessionTimeout = {{restart() {{}}, stop() {{}}}};
const sessionDelay = {{restarts: 0, restart() {{ this.restarts++; }}, stop() {{}}}};
const sessionListProcess = {{running: true, command: null}};
const context = {{root, sessionTimeout, sessionDelay, sessionListProcess}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
root.shouldSearchSessions = context.shouldSearchSessions.bind(root);
root.sessionRequestQuery = context.sessionRequestQuery.bind(root);
context.startSessionSearch.call(root);
console.log(JSON.stringify({{delays: sessionDelay.restarts, busy: root.sessionBusy,
  command: sessionListProcess.command}}));
"""
        value = json.loads(self.run_node(script))
        self.assertEqual(value["delays"], 1)
        self.assertFalse(value["busy"])
        self.assertIsNone(value["command"])


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class SessionPaletteActionTests(unittest.TestCase):
    def run_node(self, script):
        completed = subprocess.run(
            ["node", "-e", script], text=True, capture_output=True,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr)
        return completed.stdout.strip()

    def test_selected_entry_actions_and_defaults(self):
        fns = {n: extract_function(PALETTE_TEXT, n) for n in
               ("selectedSessionEntry", "sessionResumeSelected",
                "sessionHistorySelected", "sessionCopySelected",
                "sessionOpenSelected", "sessionDefaultAction")}
        sid = "b" * 32
        pid = "22222222-2222-4222-8222-222222222222"
        script = f"""
const vm = require("vm");
const root = {{
  mode: "session", selectedIndex: 0,
  rows: [{{kind: "session", payload: {{session_id: "{sid}",
          project_id: "{pid}", resource_copy: "/x/a.py"}}}}],
  notice: "", handoffs: [], copies: [],
  handoffToProjectPlanner(id, action, message) {{ this.handoffs.push([id, action, message === undefined ? "" : String(message)]); return id; }},
  copyRawText(value, purpose) {{ this.copies.push([value, purpose]); }},
}};
const context = {{root}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
const entry = root.selectedSessionEntry();
const defaultResult = root.sessionDefaultAction();
const resumeResult = root.sessionResumeSelected();
const historyResult = root.sessionHistorySelected();
root.copies = [];
const copyResult = root.sessionCopySelected();
// Open session hands (project_id, "session", session_id): the planner
// Daily tab focuses that day/session, degraded when unresolvable.
const openResult = root.sessionOpenSelected();
const firstHandoffs = root.handoffs.slice();
const firstCopies = root.copies.slice();
root.rows = [{{kind: "session", payload: {{session_id: "{sid}", project_id: "", resource_copy: "/x/b.py"}}}}];
root.selectedIndex = 0;
root.handoffs = []; root.copies = [];
const defaultCopy = root.sessionDefaultAction();
const copyHandoffs = root.handoffs.slice();
const copyCopies = root.copies.slice();
root.rows = [{{kind: "session", payload: {{session_id: "{sid}", project_id: "", resource_copy: ""}}}}];
root.handoffs = []; root.copies = [];
const defaultOpen = root.sessionOpenSelected();
const openHandoffs = root.handoffs.slice();
const resumeNone = root.sessionResumeSelected();
const copyNone = root.sessionCopySelected();
const noticeAfter = context.notice || root.notice || "";
console.log(JSON.stringify({{entry, defaultResult, resumeResult, historyResult,
  copyResult, openResult, handoffs: firstHandoffs, copies: firstCopies, defaultCopy,
  copyHandoffs, copyCopies, defaultOpen, openHandoffs, resumeNone, copyNone,
  notice: noticeAfter}}));
"""
        value = json.loads(self.run_node(script))
        self.assertEqual(value["entry"]["session_id"], "b" * 32)
        self.assertTrue(value["defaultResult"])
        self.assertTrue(value["resumeResult"])
        self.assertTrue(value["historyResult"])
        self.assertTrue(value["copyResult"])
        self.assertTrue(value["openResult"])
        # Default with project resumes; open hands the session contract.
        self.assertIn([pid, "resume", ""], value["handoffs"])
        self.assertIn([pid, "history", ""], value["handoffs"])
        self.assertIn([pid, "session", sid], value["handoffs"])
        self.assertEqual(value["copies"], [["/x/a.py", "resource"]])
        self.assertTrue(value["defaultCopy"])
        self.assertEqual(value["copyHandoffs"], [])
        self.assertEqual(value["copyCopies"], [["/x/b.py", "resource"]])
        # No project and no snippet: Open session still hands the
        # session through (degraded planner open, never an error).
        self.assertTrue(value["defaultOpen"])
        self.assertEqual(value["openHandoffs"], [["", "session", sid]])
        self.assertFalse(value["resumeNone"])
        self.assertFalse(value["copyNone"])
        self.assertIn(value["notice"], ("No project for this session",
                                        "No resource to copy"))

    def test_session_mode_selects_and_other_modes_select_nothing(self):
        fns = {n: extract_function(PALETTE_TEXT, n) for n in
               ("selectedSessionEntry", "sessionResumeSelected",
                "sessionHistorySelected", "sessionCopySelected",
                "sessionOpenSelected", "sessionDefaultAction")}
        script = f"""
const vm = require("vm");
const root = {{mode: "session", selectedIndex: 0,
  rows: [{{kind: "session", payload: {{session_id: "s", project_id: "p"}}}}],
  notice: "", handoffs: [],
  handoffToProjectPlanner(id, action) {{ this.handoffs.push([id, action]); return true; }},
  copyRawText() {{}}}};
const context = {{root}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
const sessionEntry = root.selectedSessionEntry();
const sessionAction = root.sessionDefaultAction();
root.mode = "inbox";
const inboxEntry = root.selectedSessionEntry();
const inboxAction = root.sessionDefaultAction();
root.mode = "resume";
const otherEntry = root.selectedSessionEntry();
const otherAction = root.sessionDefaultAction();
console.log(JSON.stringify({{sessionEntry, sessionAction, inboxEntry, inboxAction,
  otherEntry, otherAction}}));
"""
        value = json.loads(self.run_node(script))
        self.assertIsNotNone(value["sessionEntry"])
        self.assertTrue(value["sessionAction"])
        # inbox: no longer selects: the prefix is deleted.
        self.assertIsNone(value["inboxEntry"])
        self.assertFalse(value["inboxAction"])
        self.assertIsNone(value["otherEntry"])
        self.assertFalse(value["otherAction"])


class SessionStructuralTests(unittest.TestCase):
    def test_datasources_wiring(self):
        text = DATASOURCES_TEXT
        for name in ("shouldSearchSessions", "sessionRequestQuery",
                     "sessionListCommand", "validSessionList",
                     "sessionProjectIdOf",
                     "sessionRowForSearchEntry", "sessionRowForListEntry",
                     "finishSessionSearch", "startSessionSearch",
                     "scheduleSessionSearch", "stopSessionSearch",
                     "cancelSessionSearch", "sessionRangeLabel"):
            self.assertIn("function " + name + "(", text)
        self.assertIn("scripts/sessions.py", text)
        self.assertIn('"search"', text)
        self.assertIn('"list"', text)
        self.assertIn("sessionDelay", text)
        self.assertIn("interval: 180", text)
        self.assertIn("interval: 12000", text)
        self.assertIn('"Session search timed out; retry"', text)
        self.assertIn('"Session search failed"', text)
        # inbox: is deleted: no inbox mode, command, row builder, or
        # inbox-specific errors. filed_uri/Logseq plumbing is gone (L9).
        for gone in ('"inbox"', "sessionRowForInboxEntry",
                     "sessionFiledUri", "filed_uri",
                     '"Inbox unavailable"', '"Inbox timed out; retry"',
                     "histCleanText", "sessionOpenLogseqSelected"):
            self.assertNotIn(gone, text)

    def test_palette_wiring(self):
        text = PALETTE_TEXT
        self.assertIn("dataSources.sessionRows", text)
        self.assertIn('root.mode === "session"', text)
        self.assertIn("dataSources.scheduleSessionSearch()", text)
        self.assertIn('objectName: "sessionResumeButton"', text)
        self.assertIn('objectName: "sessionHistoryButton"', text)
        self.assertIn('objectName: "sessionCopyButton"', text)
        self.assertIn('objectName: "sessionOpenButton"', text)
        self.assertIn('"session: search work sessions · empty lists recent"', text)
        self.assertIn('"Searching work sessions…"', text)
        self.assertIn('"Showing recent work sessions"', text)
        self.assertIn("icons/copy.svg", text)
        # inbox: affordances and Logseq plumbing are gone with them.
        for gone in ('root.mode === "inbox"',
                     '"inbox: pending work sessions"',
                     '"Loading inbox…"',
                     '"No pending work sessions"',
                     'objectName: "sessionOpenLogseqButton"',
                     "sessionOpenLogseqSelected",
                     '"Open in Logseq"',
                     '"No filed Logseq page for this session"',
                     '"Could not open Logseq"'):
            self.assertNotIn(gone, text)

    def test_no_model_or_write_surface(self):
        # session: is read-only navigation: it must never touch
        # the agent, the annotations sidecar, or a Jev helper. Only the
        # resume/history/open handoff and the wl-copy path may be reached.
        for text in (PALETTE_TEXT, DATASOURCES_TEXT):
            for line in text.splitlines():
                lowered = line.lower()
                if "session" not in lowered:
                    continue
                self.assertNotIn("agent.prompt", line)
                self.assertNotIn("annotations", line)
                self.assertNotIn("jev", lowered)
                self.assertNotIn("memory_tick", line)


if __name__ == "__main__":
    unittest.main()
