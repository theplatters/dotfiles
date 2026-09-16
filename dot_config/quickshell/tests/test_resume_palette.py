"""Focused Resume integration tests (Phase 6 palette + planner handoff)."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
PALETTE = ROOT / "widgets" / "CommandPalette.qml"
DATASOURCES = ROOT / "widgets" / "PaletteDataSources.qml"
QUERY = ROOT / "widgets" / "PaletteQuery.js"
PLANNER = (ROOT / "widgets" / "ProjectPlanner.qml").read_text(encoding="utf-8")
PALETTE_TEXT = (ROOT / "widgets" / "CommandPalette.qml").read_text(encoding="utf-8")
DATASOURCES_TEXT = (ROOT / "widgets" / "PaletteDataSources.qml").read_text(encoding="utf-8")
SHELL = (ROOT / "shell.qml").read_text(encoding="utf-8")


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
class ResumeQueryTests(unittest.TestCase):
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

    def test_resume_and_project_alias(self):
        self.assertEqual(
            self.parse("resume BeforeIT", "project BeforeIT", "RESUME  x "),
            [
                {"mode": "resume", "text": "BeforeIT"},
                {"mode": "resume", "text": "BeforeIT"},
                {"mode": "resume", "text": "x"},
            ],
        )

    def test_special_modes_preserved(self):
        self.assertEqual(
            self.parse("> lock", "@ win", "ai: hi", "clip: c", "= 1+1", "file: n"),
            [
                {"mode": ">", "text": "lock"},
                {"mode": "@", "text": "win"},
                {"mode": "ai", "text": "hi"},
                {"mode": "clip", "text": "c"},
                {"mode": "=", "text": "1+1"},
                {"mode": "file", "text": "n"},
            ],
        )


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class ResumeDataSourceTests(unittest.TestCase):
    def run_node(self, script, *args):
        completed = subprocess.run(
            ["node", "-e", script, *args], text=True, capture_output=True,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr)
        return completed.stdout.strip()

    def test_list_command_omits_empty_query(self):
        source = DATASOURCES_TEXT
        for name in ("resumeListCommand", "shouldSearchResume", "finishResumeSearch",
                     "finishResumePlan", "ensureResumePlan", "startResumeSearch"):
            self.assertIn("function " + name + "(", source)
        fns = {n: extract_function(source, n) for n in
               ("resumeListCommand", "shouldSearchResume", "validResumeList", "validResumePlan")}
        script = f"""
const vm = require("vm");
const root = {{mode: "resume"}};
const context = {{ root, Quickshell: {{ shellPath(v) {{ return "/qs/" + v; }} }} }};
context.root = root;
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
const empty = context.resumeListCommand.call(root, "");
const withQuery = context.resumeListCommand.call(root, "BeforeIT");
console.log(JSON.stringify({{empty, withQuery,
  resumeOnly: context.shouldSearchResume.call(root),
  validList: context.validResumeList.call(root, {{entries: [{{id: "a"}}]}}),
  invalidList: context.validResumeList.call(root, {{entries: "x"}}),
  validPlan: context.validResumePlan.call(root, {{version: 1, project: {{id: "p"}}, operations: []}}),
  invalidPlan: context.validResumePlan.call(root, {{version: 1, project: {{}}, operations: []}})}}));
"""
        value = json.loads(self.run_node(script))
        self.assertEqual(value["empty"], ["python3",
                                          "/qs/scripts/desktop_resume.py", "list", "--limit", "20"])
        self.assertNotIn("--query", value["empty"])
        self.assertIn("--query", value["withQuery"])
        self.assertIn("BeforeIT", value["withQuery"])
        self.assertTrue(value["resumeOnly"])
        self.assertTrue(value["validList"])
        self.assertFalse(value["invalidList"])
        self.assertTrue(value["validPlan"])
        self.assertFalse(value["invalidPlan"])

    def test_list_current_installs_and_invalid_reports(self):
        source = DATASOURCES_TEXT
        fns = {n: extract_function(source, n) for n in
               ("shouldSearchResume", "isUnifiedSearch", "finishResumeSearch",
                "validResumeList", "resumeListCommand")}
        script = f"""
const vm = require("vm");
const root = {{
  openingGeneration: 1, requestedOpen: true, paletteOpen: true, mode: "resume", modeQuery: "new",
  resumeGeneration: 2, resumeProcessGeneration: 2, pendingResumeQuery: "new", resumeProcessQuery: "new",
  resumeBusy: true, resumeError: "",
  resumeRows: [{{title: "old"}}], calculatorOnly: false,
  isUnifiedSearch() {{ return this.mode === "" && !!this.modeQuery; }},
  shouldSearchFiles() {{ return false; }},
  loaded(s) {{ this.lastLoaded = s; }}, loadFailed(s, m) {{ this.failed = m; }},
  reloadRequested() {{}},
}};
root.shouldSearchResume = () => root.mode === "resume";
root.validResumeList = (p) => p && typeof p === "object" && Array.isArray(p.entries);
const resumeDelay = {{restarts: 0, restart() {{ this.restarts++; }}, stop() {{}}}};
const resumeTimeout = {{stop() {{}}}};
const context = {{root, resumeDelay, resumeTimeout}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  if (fn.name !== "isUnifiedSearch" && fn.name !== "shouldSearchResume") root[fn.name] = fn;
}}
root.finishResumeSearch = context.finishResumeSearch.bind(root);
// Current generation installs validated rows.
root.finishResumeSearch(0, JSON.stringify({{entries: [{{id: "p1", name: "Demo", logseq_path: "pages/D.md"}}]}}), 2, "new");
if (root.resumeRows.length !== 1 || root.resumeRows[0].kind !== "resume")
  throw new Error("current resume completion was not installed");
// Invalid shape sets the error and rebuilds.
root.finishResumeSearch(0, '{{"nope":1}}', 2, "new");
if (root.resumeError !== "Project search returned invalid data" || root.lastLoaded !== "resume")
  throw new Error("invalid resume data was not reported");
console.log("ok");
"""
        self.assertEqual(self.run_node(script), "ok")

    def test_list_timeout_is_terminal_reschedule_only_for_newer_query(self):
        source = DATASOURCES_TEXT
        fns = {n: extract_function(source, n) for n in
               ("finishResumeSearch", "validResumeList", "shouldSearchResume")}
        script = f"""
const vm = require("vm");
const resumeDelay = {{restarts: 0, restart() {{ this.restarts++; }}, stop() {{}}}};
const resumeTimeout = {{stops: 0, stop() {{ this.stops++; }}}};
const context = {{resumeDelay, resumeTimeout, root: null}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
function makeRoot(over) {{
  const base = {{
    paletteOpen: true, mode: "resume", modeQuery: "q",
    resumeGeneration: 6, resumeProcessGeneration: 5, resumeProcessQuery: "q",
    pendingResumeQuery: "q", resumeBusy: false, resumeError: "Project search timed out; retry",
    resumeRows: [],
    loaded(s) {{ this.lastLoaded = s; }}, loadFailed(s, m) {{ this.failed = m; }},
  }};
  const r = Object.assign(base, over);
  r.shouldSearchResume = context.shouldSearchResume.bind(r);
  r.validResumeList = (p) => p && typeof p === "object" && Array.isArray(p.entries);
  r.finishResumeSearch = context.finishResumeSearch.bind(r);
  return r;
}}
context.root = makeRoot({{}});
let root = context.root;
root.finishResumeSearch(0, JSON.stringify({{entries: [{{id: "p1"}}]}}), 5, "q");
if (resumeDelay.restarts !== 0) throw new Error("timeout exit rescheduled");
if (root.resumeRows.length !== 0) throw new Error("timeout exit installed payload");
if (root.resumeError !== "Project search timed out; retry") throw new Error("timeout error was clobbered");
// Superseded by a newer query (pending differs): reschedules exactly once.
let root2 = makeRoot({{resumeGeneration: 7, resumeProcessGeneration: 5, resumeProcessQuery: "old",
  pendingResumeQuery: "new", modeQuery: "new", resumeBusy: true}});
context.root = root2;
root2.finishResumeSearch(0, JSON.stringify({{entries: [{{id: "p1"}}]}}), 5, "old");
if (resumeDelay.restarts !== 1) throw new Error("newer query did not reschedule");
if (root2.resumeRows.length !== 0) throw new Error("superseded payload was installed");
console.log("ok");
"""
        self.assertEqual(self.run_node(script), "ok")

    def test_plan_loads_only_for_selected_project(self):
        source = DATASOURCES_TEXT
        fns = {n: extract_function(source, n) for n in ("finishResumePlan", "validResumePlan")}
        script = f"""
const vm = require("vm");
const root = {{
  requestedOpen: true, paletteOpen: true, mode: "resume", modeQuery: "x",
  resumePlanGeneration: 5, resumePlanProcessGeneration: 5,
  pendingResumePlanId: "p1", resumePlanProcessId: "p1",
  resumePlan: null, resumePlanProjectId: "", resumePlanBusy: true, resumePlanError: "",
  resumePlanRetiring: false,
  shouldSearchResume() {{ return this.mode === "resume"; }},
  validResumePlan(p) {{ return p && p.version === 1 && p.project && p.project.id && Array.isArray(p.operations); }},
  loaded(s) {{ this.lastLoaded = s; }},
}};
const resumePlanTimeout = {{stop() {{}}}};
const context = {{root, resumePlanTimeout}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  root[fn.name] = fn.name === "validResumePlan" ? root.validResumePlan : fn.bind(root);
}}
const other = JSON.stringify({{version: 1, project: {{id: "p2", name: "Other"}}, operations: [], session: null}});
root.finishResumePlan(0, other, 5, "p2");
if (root.resumePlan !== null) throw new Error("plan for unselected project was installed");
const current = JSON.stringify({{version: 1, project: {{id: "p1", name: "Demo"}}, operations: []}});
root.resumePlanBusy = true;
root.finishResumePlan(0, current, 5, "p1");
if (!root.resumePlan || root.resumePlanProjectId !== "p1" || root.lastLoaded !== "resumePlan")
  throw new Error("selected plan was not installed");
console.log("ok");
"""
        self.assertEqual(self.run_node(script), "ok")

    def test_plan_stale_never_touches_busy_timer_error(self):
        source = DATASOURCES_TEXT
        fns = {n: extract_function(source, n) for n in ("finishResumePlan", "validResumePlan")}
        script = f"""
const vm = require("vm");
const root = {{
  paletteOpen: true, mode: "resume",
  resumePlanGeneration: 8, resumePlanProcessGeneration: 8,
  pendingResumePlanId: "pB", resumePlanProcessId: "pB",
  resumePlan: null, resumePlanProjectId: "", resumePlanBusy: true, resumePlanError: "keep",
  resumePlanRetiring: false,
  shouldSearchResume() {{ return this.mode === "resume"; }},
  validResumePlan(p) {{ return true; }},
  loaded(s) {{ this.lastLoaded = s; }},
}};
let stops = 0;
const resumePlanTimeout = {{stop() {{ stops++; }}}};
const context = {{root, resumePlanTimeout}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  root[fn.name] = fn.bind(root);
}}
// Truly stale launch (different process id+generation): must not touch state.
root.finishResumePlan(0, JSON.stringify({{version:1, project:{{id:"pA"}}, operations:[]}}), 7, "pA");
if (!root.resumePlanBusy || root.resumePlanError !== "keep" || stops !== 0)
  throw new Error("stale exit touched busy/timer/error");
if (root.resumePlan !== null) throw new Error("stale payload installed");
console.log("ok");
"""
        self.assertEqual(self.run_node(script), "ok")

    def test_plan_rapid_A_B_A_and_cached_authoritative(self):
        source = DATASOURCES_TEXT
        fns = {n: extract_function(source, n) for n in
               ("ensureResumePlan", "finishResumePlan", "launchDesiredResumePlan", "validResumePlan")}
        script = f"""
const vm = require("vm");
const root = {{
  paletteOpen: true, mode: "resume",
  resumePlanGeneration: 0, resumePlanProcessGeneration: 0,
  pendingResumePlanId: "", resumePlanProcessId: "",
  resumePlan: null, resumePlanProjectId: "", resumePlanBusy: false, resumePlanError: "",
  resumePlanRetiring: false,
  shouldSearchResume() {{ return this.mode === "resume"; }},
  validResumePlan(p) {{ return p && p.version === 1 && p.project && p.project.id && Array.isArray(p.operations); }},
  loaded(s) {{ this.lastLoaded = s; this.loads = (this.loads || 0) + 1; }},
}};
const resumePlanProcess = {{running: false, command: []}};
const resumePlanTimeout = {{restart() {{}}, stop() {{}}}};
const Quickshell = {{shellPath(v) {{ return "/qs/" + v; }}}};
const context = {{root, resumePlanProcess, resumePlanTimeout, Quickshell}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn.bind(root);
}}
// A -> B -> A serialization with a single Process.
if (!root.ensureResumePlan("pA")) throw new Error("ensure A failed");
const genA = root.resumePlanGeneration, procA = root.resumePlanProcessGeneration;
if (root.pendingResumePlanId !== "pA" || root.resumePlanProcessId !== "pA" || !resumePlanProcess.running)
  throw new Error("A was not launched");
if (!root.ensureResumePlan("pB")) throw new Error("ensure B failed");
if (root.pendingResumePlanId !== "pB") throw new Error("B was not queued");
if (!root.ensureResumePlan("pA")) throw new Error("ensure A2 failed");
if (root.pendingResumePlanId !== "pA") throw new Error("A2 was not queued");
// Delayed old B success must not install (stale vs current A launch).
const bPayload = JSON.stringify({{version:1, project:{{id:"pB"}}, operations:[]}});
const bGen = root.resumePlanProcessGeneration === genA ? 99 : root.resumePlanProcessGeneration;
root.finishResumePlan(0, bPayload, 999, "pB");
if (root.resumePlan && root.resumePlanProjectId === "pB") throw new Error("stale B overwrote");
// Current A success installs.
const aPayload = JSON.stringify({{version:1, project:{{id:"pA"}}, operations:[]}});
root.finishResumePlan(0, aPayload, root.resumePlanProcessGeneration, root.resumePlanProcessId);
if (!root.resumePlan || root.resumePlanProjectId !== "pA") throw new Error("current A was not installed");
// Cached A reselected while B retires stays authoritative.
root.resumePlanBusy = false;
if (!root.ensureResumePlan("pB2")) throw new Error("ensure B2 failed");
// Simulate B2 in flight with different id while cache is A.
root.resumePlanProcessId = "pB2"; root.pendingResumePlanId = "pB2";
resumePlanProcess.running = true;
if (!root.ensureResumePlan("pA")) throw new Error("reselect cached A failed");
if (!root.resumePlan || root.resumePlanProjectId !== "pA") throw new Error("cached A lost authority");
if (root.resumePlanBusy) throw new Error("cached reselect should be idle");
// Delayed old B2 error must not clobber the cache.
root.finishResumePlan(1, "{{}}", root.resumePlanProcessGeneration, "pB2");
if (!root.resumePlan || root.resumePlanProjectId !== "pA") throw new Error("old error clobbered cached A");
console.log("ok");
"""
        self.assertEqual(self.run_node(script), "ok")

    def test_backend_argv_uses_shell_path_list_form_no_shell(self):
        self.assertIn('Quickshell.shellPath("scripts/desktop_resume.py")', DATASOURCES_TEXT)
        self.assertIn('"plan", "--project"', DATASOURCES_TEXT)
        self.assertIn('"list", "--limit", "20"', DATASOURCES_TEXT)
        self.assertNotIn("shell=True", DATASOURCES_TEXT)
        self.assertNotIn("sh -c", DATASOURCES_TEXT)
        self.assertIn("interval: 12000", DATASOURCES_TEXT)
        self.assertIn("resumeTimeout", DATASOURCES_TEXT)
        self.assertIn("resumePlanTimeout", DATASOURCES_TEXT)
        self.assertIn("resumePlanRetiring", DATASOURCES_TEXT)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class ResumePaletteTests(unittest.TestCase):
    def run_node(self, script):
        completed = subprocess.run(["node", "-e", script], text=True, capture_output=True)
        if completed.returncode:
            raise AssertionError(completed.stderr)
        return json.loads(completed.stdout)

    def test_rows_preview_and_action_routing(self):
        fns = {n: extract_function(PALETTE_TEXT, n) for n in
               ("selectedResumeEntry", "resumePlanForSelection", "requestResumePlanForSelection",
                "resumePreviewText", "resumeSelectedProject", "askResumeProject",
                "historyResumeProject", "startResumeExecute", "validResumeExecutePayload",
                "resumeExecuteSummary", "finishResumeExecute")}
        script = f"""
const vm = require("vm");
const plan = {{version: 1,
  project: {{id: "p1", name: "Demo"}},
  session: {{session_id: "s", end_ms: 1700000000000}},
  session_reason: "",
  files: [{{relative: "src/a.py"}}, {{relative: "src/b.py"}}],
  repository: {{available: true, branch: "main"}},
  logseq: {{available: true, page: "Demo", path: "pages/Demo.md", open_count: 2}},
  pi_session: {{available: true}}, operations: []}};
const context = {{
  mode: "resume", selectedIndex: 0, requestedOpen: true, notice: "",
  rows: [{{kind: "resume", payload: {{id: "p1", name: "Demo"}}}}],
  resumeExecuteBusy: false, resumeExecuteGeneration: 0, resumeExecuteProcessGeneration: 0,
  resumeExecuteProjectId: "", resumeExecuteProcessProjectId: "",
  resumeExecuteProcess: {{running: false, command: []}},
  resumeExecuteTimeout: {{restart() {{}}, stop() {{}}}},
  Quickshell: {{shellPath(v) {{ return v; }}}},
  dataSources: {{
    resumePlan: plan, resumePlanProjectId: "p1", resumePlanBusy: false, resumePlanError: "",
    ensureResumePlan(id) {{ this.requested = id; return true; }},
  }},
  handoffs: [],
  handoffToProjectPlanner(pid, act, msg) {{ this.handoffs.push([pid, act, msg || ""]); }},
  requestResumePlanForSelection: null, resumePlanForSelection: null,
}};
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  context.root[fn.name] = fn;
}}
const out = {{}};
out.selected = context.selectedResumeEntry().id;
out.preview = context.resumePreviewText();
if (!out.preview.includes("Demo") || !out.preview.includes("Files: src/a.py")
    || !out.preview.includes("Repo: main") || !out.preview.includes("Logseq: Demo")
    || !out.preview.includes("TODOs: 2 open") || !out.preview.includes("Pi session: saved"))
  throw new Error("preview missing fields: " + out.preview);
// Ask/History hand off without desktop execution.
context.askResumeProject();
context.historyResumeProject();
out.handoffs = context.handoffs.slice();
if (out.handoffs[0][0] !== "p1" || out.handoffs[0][1] !== "ask") throw new Error("ask handoff broken");
if (out.handoffs[1][0] !== "p1" || out.handoffs[1][1] !== "history") throw new Error("history handoff broken");
// Default Enter with a loaded plan starts backend execute.
context.handoffs = [];
if (!context.resumeSelectedProject()) throw new Error("resume did not start");
if (context.resumeExecuteProcess.command.join(" ").indexOf("desktop_resume.py") < 0
    || !context.resumeExecuteProcess.command.includes("execute")
    || !context.resumeExecuteProcess.command.includes("p1"))
  throw new Error("execute did not use stable UUID argv");
// No-plan Enter requests the plan and shows Loading, never executes.
context.dataSources.resumePlan = null;
context.resumeExecuteProcess.running = false;
context.dataSources.requested = "";
context.notice = "";
context.resumeExecuteBusy = false;
if (context.resumeSelectedProject()) throw new Error("resumed without a plan");
if (context.dataSources.requested !== "p1" || context.notice !== "Loading project plan…")
  throw new Error("missing plan was not requested as Loading");
console.log(JSON.stringify({{selected: out.selected, handoffs: out.handoffs,
  cmd: context.resumeExecuteProcess.command, notice: context.notice}}));
"""
        value = self.run_node(script)
        self.assertEqual(value["selected"], "p1")
        self.assertEqual(value["handoffs"][0][:2], ["p1", "ask"])
        self.assertEqual(value["handoffs"][1][:2], ["p1", "history"])
        self.assertIn("execute", value["cmd"])
        self.assertEqual(value["notice"], "Loading project plan…")

    def test_preview_includes_unavailable_and_warnings(self):
        fns = {n: extract_function(PALETTE_TEXT, n) for n in
               ("selectedResumeEntry", "resumePlanForSelection", "resumePreviewText")}
        script = f"""
const vm = require("vm");
const plan = {{version: 1, project: {{id: "p1", name: "Demo"}},
  session: null, session_reason: "no work session",
  files: [], repository: {{available: false}}, logseq: {{available: false, reason: "no linked Logseq page"}},
  pi_session: {{available: false}},
  operations: [{{id: "open_editor", kind: "open_editor", available: false, reason: "kitty is not available"}}],
  warnings: ["device identity unavailable"]}};
const context = {{
  mode: "resume", selectedIndex: 0,
  rows: [{{kind: "resume", payload: {{id: "p1"}}}}],
  dataSources: {{resumePlan: plan, resumePlanProjectId: "p1", resumePlanBusy: false, resumePlanError: ""}},
}};
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
const text = context.resumePreviewText();
if (text.indexOf("Unavailable") < 0 || text.indexOf("kitty") < 0) throw new Error("missing unavailable hint: " + text);
if (text.indexOf("device identity") < 0) throw new Error("missing warning hint: " + text);
console.log(JSON.stringify({{text}}));
"""
        value = self.run_node(script)
        self.assertIn("Unavailable", value["text"])

    def test_preview_never_embeds_page_content(self):
        self.assertNotIn("content", extract_function(PALETTE_TEXT, "resumePreviewText"))
        preview = extract_function(PALETTE_TEXT, "resumePreviewText")
        self.assertIn("open_count", preview)
        self.assertIn("pi_session", preview)
        self.assertIn("repository", preview)

    def test_execute_summarizes_partial_and_hands_off_with_message(self):
        self.assertIn('"execute", "--project"', PALETTE_TEXT)
        self.assertIn('Quickshell.shellPath("scripts/desktop_resume.py")', PALETTE_TEXT)
        self.assertNotIn("shell=True", PALETTE_TEXT)
        fns = {n: extract_function(PALETTE_TEXT, n) for n in
               ("validResumeExecutePayload", "resumeExecuteSummary", "finishResumeExecute")}
        script = f"""
const vm = require("vm");
const context = {{
  requestedOpen: true, notice: "", resumeExecuteBusy: true,
  resumeExecuteGeneration: 3, resumeExecuteProcessGeneration: 3,
  resumeExecuteProjectId: "p1", resumeExecuteProcessProjectId: "p1",
  resumeExecuteTimeout: {{stop() {{}}}},
  handoffs: [],
  handoffToProjectPlanner(pid, act, msg) {{ this.handoffs.push([pid, act, msg || ""]); }},
}};
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
const partial = {{project: {{id: "p1"}}, results: [
  {{id: "open_editor", kind: "open_editor", status: "ok", reason: ""}},
  {{id: "open_terminal", kind: "open_terminal", status: "failed", reason: "kitty is not available"}},
  {{id: "open_project_agent", kind: "open_project_agent", status: "delegated", reason: ""}},
  {{id: "focus_workspace", kind: "focus_workspace", status: "skipped", reason: "no work session"}}]}};
const summary = context.resumeExecuteSummary(partial);
if (summary.indexOf("1 failed") < 0 || summary.indexOf("1 skipped") < 0 || summary.indexOf("delegated") < 0)
  throw new Error("summary missing counts: " + summary);
context.finishResumeExecute(0, JSON.stringify(partial), 3, "p1");
if (context.handoffs.length !== 1 || context.handoffs[0][0] !== "p1" || context.handoffs[0][1] !== "resume")
  throw new Error("partial failure blocked the handoff");
if (!context.handoffs[0][2] || context.handoffs[0][2].indexOf("1 failed") < 0)
  throw new Error("restoration notice was not passed through: " + JSON.stringify(context.handoffs));
// Mismatched result id never hands off.
context.handoffs = [];
context.resumeExecuteBusy = true;
context.finishResumeExecute(0, JSON.stringify({{project: {{id: "p2"}}, results: []}}), 3, "p1");
if (context.handoffs.length !== 0) throw new Error("mismatched project id handed off");
console.log(JSON.stringify({{summary, handoffs: context.handoffs}}));
"""
        value = self.run_node(script)
        self.assertIn("1 failed", value["summary"])

    def test_execute_success_hands_off_no_arbitrary_commands(self):
        self.assertIn('"execute", "--project"', PALETTE_TEXT)
        self.assertIn('Quickshell.shellPath("scripts/desktop_resume.py")', PALETTE_TEXT)
        self.assertNotIn("shell=True", PALETTE_TEXT)
        fns = {n: extract_function(PALETTE_TEXT, n) for n in
               ("validResumeExecutePayload", "resumeExecuteSummary", "finishResumeExecute")}
        script = f"""
const vm = require("vm");
const context = {{
  requestedOpen: true, notice: "", resumeExecuteBusy: true,
  resumeExecuteGeneration: 3, resumeExecuteProcessGeneration: 3,
  resumeExecuteProjectId: "p1", resumeExecuteProcessProjectId: "p1",
  resumeExecuteTimeout: {{stop() {{}}}},
  handoffs: [],
  handoffToProjectPlanner(pid, act, msg) {{ this.handoffs.push([pid, act, msg || ""]); }},
}};
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
context.finishResumeExecute(0, JSON.stringify({{project: {{id: "p1"}}, results: []}}), 3, "p1");
if (context.handoffs.length !== 1 || context.handoffs[0][0] !== "p1" || context.handoffs[0][1] !== "resume")
  throw new Error("execute success did not hand off to planner");
context.handoffs = [];
context.finishResumeExecute(1, "{{}}", 3, "p1");
if (context.notice !== "Resume failed") throw new Error("execute failure was not surfaced");
console.log(JSON.stringify({{handoffs: context.handoffs, notice: context.notice}}));
"""
        value = self.run_node(script)
        self.assertEqual(value["notice"], "Resume failed")

    def test_execute_close_invalidates_delayed_completion(self):
        # The palette close() (not the IpcHandler stub) must bump the
        # execute generation before stopping the process.
        close_start = PALETTE_TEXT.rindex("    function close() {")
        close_fn = PALETTE_TEXT[close_start:close_start + 2000]
        self.assertIn("resumeExecuteGeneration++", close_fn)
        # close must bump before stopping so a delayed exit is stale.
        self.assertLess(close_fn.index("resumeExecuteGeneration++"),
                        close_fn.index("resumeExecuteProcess.running = false"))
        fns = {n: extract_function(PALETTE_TEXT, n) for n in
               ("finishResumeExecute", "validResumeExecutePayload", "resumeExecuteSummary")}
        script = f"""
const vm = require("vm");
const context = {{
  requestedOpen: true, notice: "", resumeExecuteBusy: true,
  resumeExecuteGeneration: 4, resumeExecuteProcessGeneration: 4,
  resumeExecuteProjectId: "p1", resumeExecuteProcessProjectId: "p1",
  resumeExecuteTimeout: {{stop() {{}}}},
  handoffs: [],
  handoffToProjectPlanner(pid, act, msg) {{ this.handoffs.push([pid, act]); }},
}};
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
// Simulate close(): generation bumped before stop, then reopen.
context.resumeExecuteGeneration++;
context.requestedOpen = false;
context.resumeExecuteBusy = false;
context.requestedOpen = true;
context.resumeExecuteGeneration++;
// Delayed old completion (generation 4) must be stale after reopen.
context.finishResumeExecute(0, JSON.stringify({{project: {{id: "p1"}}, results: []}}), 4, "p1");
if (context.handoffs.length !== 0) throw new Error("close/reopen did not invalidate delayed completion");
console.log(JSON.stringify({{handoffs: context.handoffs}}));
"""
        value = self.run_node(script)
        self.assertEqual(value["handoffs"], [])

    def test_handoff_payload_after_exit_and_visible_actions(self):
        self.assertIn("pendingPlannerProjectId", PALETTE_TEXT)
        self.assertIn("pendingPlannerAction", PALETTE_TEXT)
        self.assertIn("pendingPlannerMessage", PALETTE_TEXT)
        self.assertIn("Resuming…", PALETTE_TEXT)
        self.assertIn("Resume", PALETTE_TEXT)
        self.assertIn('text: "Ask Pi"', PALETTE_TEXT)
        self.assertIn('text: "History"', PALETTE_TEXT)
        self.assertIn("root.projectPlanningRequested(pid, act", PALETTE_TEXT)
        self.assertIn("function onProjectPlanningRequested(projectId, action", SHELL)
        self.assertIn("projectPlanner.openProject(projectId, action", SHELL)

    def test_rebuild_model_uses_authoritative_resume_cache(self):
        source = PALETTE_TEXT
        block = source[source.index("function rebuildModel("):source.index("function rebuild(")]
        self.assertIn('root.mode === "resume"', block)
        self.assertIn("dataSources.resumeRows", block)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class ResumePlannerTests(unittest.TestCase):
    def run_node(self, script):
        completed = subprocess.run(["node", "-e", script], text=True, capture_output=True)
        if completed.returncode:
            raise AssertionError(completed.stderr)
        return json.loads(completed.stdout)

    def test_open_project_selects_by_id_and_applies_action(self):
        fns = {n: extract_function(PLANNER, n) for n in
               ("openProject", "consumePendingOpenProject", "clearPendingOpenProject",
                "resumeContinuationText", "projectById", "projectId", "projectName",
                "projectNotePath")}
        script = f"""
const vm = require("vm");
const context = {{
  projects: [{{id: "p1", name: "Demo", logseq_path: "pages/D.md"}},
             {{id: "p2", name: "Other", logseq_path: ""}}],
  selectedProjectId: "", selectedProject: null, selectedPath: "",
  selectedAgent: null, currentPage: null, notice: "",
  pendingOpenProjectId: "", pendingOpenAction: "", pendingOpenMessage: "", pendingOpenInteraction: -1,
  interactionGeneration: 0,
  requestedOpen: true, closing: false, activeTab: "projects",
  listBusy: false, listRetiring: false,
  drafts: {{}},
  focused: "",
  selectCalls: [],
}};
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
context.blockedReason = () => "";
context.open = () => {{}};
context.startList = () => true;
context.filteredProjects = () => context.projects;
context.pageFor = () => null;
context.agentFor = () => ({{tag: "agent"}});
context.hasBusyAgent = () => false;
context.pageBusy = false; context.pageRetiring = false;
context.toggleBusy = false; context.toggleRetiring = false; context.sendBusy = false;
context.clearInspectPrompt = () => {{}};
context.selectProject = (p) => {{
  context.selectCalls.push(p.id);
  context.selectedProjectId = p.id; context.selectedProject = p;
  context.selectedPath = context.projectNotePath(p);
  context.selectedAgent = context.projectNotePath(p) ? {{tag: "agent"}} : null;
  return true;
}};
context.draftFor = (p) => context.drafts[p] || "";
context.setDraft = (p, v) => {{ context.drafts[p] = v; }};
context.focusComposer = () => {{ context.focused = "composer"; }};
context.focusTranscript = () => {{ context.focused = "transcript"; }};
context.openProject("p1", "ask");
if (context.selectCalls[0] !== "p1")
  throw new Error("did not select by stable id");
if (!String(context.drafts["pages/D.md"] || "").includes("Demo"))
  throw new Error("ask did not prefill a deterministic continuation");
if (context.focused !== "composer") throw new Error("ask did not focus composer");
// history focuses the transcript
context.drafts = {{}}; context.focused = ""; context.selectCalls = [];
context.openProject("p1", "history");
if (context.focused !== "transcript") throw new Error("history did not focus transcript");
if (Object.keys(context.drafts).length !== 0) throw new Error("history must not prefill");
// already-selected same id/path still applies the action without reselecting.
context.focused = ""; context.selectCalls = [];
context.selectedProjectId = "p1"; context.selectedPath = "pages/D.md";
context.selectedProject = context.projects[0];
context.pendingOpenProjectId = "p1"; context.pendingOpenAction = "history";
context.pendingOpenMessage = ""; context.pendingOpenInteraction = context.interactionGeneration;
context.blockedReason = () => "";
context.filteredProjects = () => context.projects;
context.pageFor = () => null;
context.agentFor = () => ({{tag: "agent"}});
context.hasBusyAgent = () => false;
context.pageBusy = false; context.pageRetiring = false;
context.toggleBusy = false; context.toggleRetiring = false; context.sendBusy = false;
context.selectProject = () => {{ throw new Error("same selected target was reselected"); }};
if (!context.consumePendingOpenProject() || context.focused !== "transcript")
  throw new Error("already-selected handoff action was dropped");
// note-less project degrades to metadata only, no agent
context.focused = ""; context.selectCalls = [];
context.selectProject = (p) => {{
  context.selectCalls.push(p.id);
  context.selectedProjectId = p.id; context.selectedProject = p;
  context.selectedPath = context.projectNotePath(p);
  context.selectedAgent = context.projectNotePath(p) ? {{tag: "agent"}} : null;
  return true;
}};
context.openProject("p2", "ask");
if (context.selectedAgent !== null) throw new Error("note-less project spawned an agent");
console.log(JSON.stringify({{selects: context.selectCalls, focused: context.focused,
  agent: context.selectedAgent}}));
"""
        value = self.run_node(script)
        self.assertIsNone(value["agent"])
        self.assertEqual(value["focused"], "")

    def test_finish_list_retains_then_consumes_different_project(self):
        fns = {n: extract_function(PLANNER, n) for n in
               ("finishList", "applyRegistryList", "consumePendingOpenProject", "selectProject",
                "clearPendingOpenProject", "projectById", "projectId", "projectNotePath",
                "projectName", "resumeContinuationText", "validRegistryList", "filteredProjects")}
        script = f"""
const vm = require("vm");
const listData = {{revision: "r2", file: "f", graphName: "Notes",
  projects: [{{id: "p1", name: "One", logseq_path: "pages/A.md"}},
             {{id: "p2", name: "Two", logseq_path: "pages/B.md"}}]}};
const context = {{
  projects: [{{id: "p1", name: "One", logseq_path: "pages/A.md"}}],
  selectedProjectId: "p1", selectedProject: {{id: "p1", name: "One", logseq_path: "pages/A.md"}},
  selectedPath: "pages/A.md", selectedAgent: {{tag: "old"}}, currentPage: {{path: "pages/A.md"}},
  selectedIndex: 0, projectFilter: "", notice: "", errorMessage: "",
  pendingOpenProjectId: "p2", pendingOpenAction: "ask", pendingOpenMessage: "Restored 1 ok",
  pendingOpenInteraction: 0, interactionGeneration: 0,
  requestedOpen: true, activeTab: "projects",
  listGeneration: 7, listBusy: true,
  pageBusy: false, pageRetiring: false, toggleBusy: false, toggleRetiring: false, sendBusy: false,
  listTimeout: {{stop() {{}}}},
  pauseCalls: [], agentCalls: [], startPageCalls: [], drafts: {{}}, focused: "",
  agentCache: {{}},
  projectFilter: "",
}};
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
context.blockedReason = () => "";
context.hasBusyAgent = () => false;
context.pauseIdleAgents = (p) => {{ context.pauseCalls.push(p || ""); }};
context.agentFor = (p) => {{ context.agentCalls.push(p); return {{tag: "agent-" + p}}; }};
context.pageFor = (p) => null;
context.startPage = (p, purpose) => {{ context.startPageCalls.push([p, purpose]); return true; }};
context.draftFor = (p) => context.drafts[p] || "";
context.setDraft = (p, v) => {{ context.drafts[p] = v; }};
context.focusComposer = () => {{ context.focused = "composer"; }};
context.focusTranscript = () => {{ context.focused = "transcript"; }};
context.clearInspectPrompt = () => {{}};
context.finishList(0, JSON.stringify(listData), "", 7);
// Different project must go through guarded selectProject: pause old, new generation, page read.
if (context.selectedProjectId !== "p2") throw new Error("did not switch to pending project");
if (context.pauseCalls.length < 1 || context.pauseCalls[0] !== "pages/B.md") throw new Error("old agents were not paused: " + JSON.stringify(context.pauseCalls));
if (context.interactionGeneration !== 1) throw new Error("selection did not increment generation");
if (!context.startPageCalls.length || context.startPageCalls[0][0] !== "pages/B.md") throw new Error("page read was not started");
if (context.focused !== "composer") throw new Error("ask action did not focus composer");
if (!String(context.drafts["pages/B.md"] || "").includes("Two")) throw new Error("ask prefill missing");
if (context.pendingOpenProjectId !== "") throw new Error("pending was not consumed");
if (context.notice !== "Restored 1 ok") throw new Error("restoration message lost: " + context.notice);
console.log(JSON.stringify({{selected: context.selectedProjectId, pause: context.pauseCalls,
  startPage: context.startPageCalls, notice: context.notice}}));
"""
        value = self.run_node(script)
        self.assertEqual(value["selected"], "p2")
        self.assertEqual(value["notice"], "Restored 1 ok")

    def test_finish_list_same_project_refreshes_and_applies_action(self):
        fns = {n: extract_function(PLANNER, n) for n in
               ("finishList", "applyRegistryList", "consumePendingOpenProject", "selectProject",
                "clearPendingOpenProject", "projectById", "projectId", "projectNotePath",
                "projectName", "resumeContinuationText", "validRegistryList", "filteredProjects")}
        script = f"""
const vm = require("vm");
const listData = {{revision: "r2", file: "f",
  projects: [{{id: "p1", name: "One", logseq_path: "pages/A.md"}}]}};
const context = {{
  projects: [{{id: "p1", name: "One", logseq_path: "pages/A.md"}}],
  selectedProjectId: "p1", selectedProject: {{id: "p1", name: "One", logseq_path: "pages/A.md"}},
  selectedPath: "pages/A.md", selectedAgent: {{tag: "old"}}, currentPage: null,
  selectedIndex: 0, projectFilter: "", notice: "", errorMessage: "",
  pendingOpenProjectId: "p1", pendingOpenAction: "history", pendingOpenMessage: "",
  pendingOpenInteraction: 3, interactionGeneration: 3,
  requestedOpen: true, activeTab: "projects",
  listGeneration: 9, listBusy: true,
  pageBusy: false, pageRetiring: false, toggleBusy: false, toggleRetiring: false, sendBusy: false,
  listTimeout: {{stop() {{}}}},
  pauseCalls: [], startPageCalls: [], focused: "", drafts: {{}}, agentCache: {{}},
}};
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
context.blockedReason = () => "";
context.hasBusyAgent = () => false;
context.pauseIdleAgents = (p) => {{ context.pauseCalls.push(p || ""); }};
context.agentFor = (p) => {{ return {{tag: "agent"}}; }};
context.pageFor = (p) => null;
context.startPage = (p, purpose) => {{ context.startPageCalls.push([p, purpose]); return true; }};
context.selectProject = () => {{ throw new Error("same target must not reselect"); }};
context.focusTranscript = () => {{ context.focused = "transcript"; }};
context.focusComposer = () => {{ context.focused = "composer"; }};
context.draftFor = () => "";
context.setDraft = () => {{}};
context.clearInspectPrompt = () => {{}};
context.finishList(0, JSON.stringify(listData), "", 9);
if (context.focused !== "transcript") throw new Error("same-project history action was dropped");
if (context.pendingOpenProjectId !== "") throw new Error("pending was not consumed");
console.log(JSON.stringify({{focused: context.focused, startPage: context.startPageCalls}}));
"""
        value = self.run_node(script)
        self.assertEqual(value["focused"], "transcript")

    def test_pending_invalidated_on_close_failure_and_superseding(self):
        self.assertIn("clearPendingOpenProject()", PLANNER)
        self.assertIn("pendingOpenInteraction", PLANNER)
        close_start = PLANNER.rindex("    function close() {")
        close_fn = PLANNER[close_start:close_start + 2000]
        self.assertIn("clearPendingOpenProject", close_fn)
        cancel_fn = extract_function(PLANNER, "cancelListRead")
        self.assertIn("clearPendingOpenProject", cancel_fn)
        fail_fn = extract_function(PLANNER, "handleListStartFailure")
        self.assertIn("clearPendingOpenProject", fail_fn)
        finish_fn = extract_function(PLANNER, "finishList")
        # finishList must retain the prior id, never preselect the pending id
        # through applyRegistryList.
        self.assertIn("retainedSelected", finish_fn)
        self.assertNotIn("previouslySelected = String(root.pendingOpenProjectId", finish_fn)
        self.assertIn("consumePendingOpenProject", finish_fn)
        # consume binds to the interaction generation.
        consume_fn = extract_function(PLANNER, "consumePendingOpenProject")
        self.assertIn("pendingOpenInteraction", consume_fn)
        self.assertIn("interactionGeneration", consume_fn)
        # selectProject invalidates a superseding different id.
        select_fn = extract_function(PLANNER, "selectProject")
        self.assertIn("clearPendingOpenProject", select_fn)
        # plain open clears the queue so an old ask cannot run later.
        open_fn = extract_function(PLANNER, "openProject")
        self.assertIn("clearPendingOpenProject", open_fn)

    def test_no_direct_spawn_or_autosend(self):
        consume = extract_function(PLANNER, "consumePendingOpenProject")
        opener = extract_function(PLANNER, "openProject")
        for text in (opener, consume):
            self.assertNotIn("createObject", text)
            self.assertNotIn(".prompt(", text)
            self.assertNotIn(".start()", text)
            self.assertNotIn("worker.start", text)
        self.assertNotIn("send()", opener + consume)
        self.assertIn("selectProject", opener + consume)
        self.assertIn("projectById", opener + consume)
        # consumePendingOpenProject reuses selectProject/agentFor via selectProject only
        self.assertIn("agentFor", PLANNER[PLANNER.index("function selectProject"):PLANNER.index("function selectFiltered")])
        # ask path prefills but never sends
        self.assertIn("setDraft", consume)
        self.assertNotIn("composePrompt", consume)


if __name__ == "__main__":
    unittest.main()
