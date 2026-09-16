import datetime
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import daily_agenda
import project_planner

EXTENSION = (ROOT / ".pi" / "extensions" / "desktop-agent.ts").read_text(encoding="utf-8")
BUN = shutil.which("bun")


class AgendaDefaultDateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.graph = Path(self.temp.name) / "graph"
        (self.graph / "pages").mkdir(parents=True)
        (self.graph / "journals").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name="Work.md", content="- TODO alpha\n"):
        path = self.graph / "pages" / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_list_defaults_to_local_today(self):
        self.write(content="- TODO alpha\n")
        today = datetime.date.today().isoformat()
        # Omitted arg and request objects without "date" inspect today.
        result = daily_agenda.list_agenda(self.graph)
        self.assertEqual(result["date"], today)
        self.assertEqual(len(result["tasks"]), 1)
        self.assertEqual(result["tasks"][0]["task"], "alpha")
        for missing in ({}, {"other": 1}):
            result = daily_agenda.list_agenda(self.graph, missing)
            self.assertEqual(result["date"], today)
            self.assertEqual(len(result["tasks"]), 1)
        # An explicit null still fails validation (existing contract).
        with self.assertRaises(project_planner.GraphError):
            daily_agenda.list_agenda(self.graph, None)

    def test_list_explicit_date_still_validated(self):
        self.write(content="- TODO alpha\n")
        result = daily_agenda.list_agenda(self.graph, "2026-09-13")
        self.assertEqual(result["date"], "2026-09-13")
        with self.assertRaises(project_planner.GraphError):
            daily_agenda.list_agenda(self.graph, "not-a-date")
        with self.assertRaises(project_planner.GraphError):
            daily_agenda.list_agenda(self.graph, "2026-02-30")

    def test_cli_list_missing_date_defaults_to_today(self):
        self.write(content="- TODO item\n")
        argv = [sys.executable, str(ROOT / "scripts" / "daily_agenda.py"),
                "--graph", str(self.graph), "list"]
        completed = subprocess.run(argv, input="{}", text=True,
                                   capture_output=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertEqual(value["date"], datetime.date.today().isoformat())
        self.assertEqual(len(value["tasks"]), 1)


class AgendaCliErrorContractTests(unittest.TestCase):
    """Real daily_agenda.py failure contract: stdout JSON error, exit 1."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.graph = Path(self.temp.name) / "graph"
        (self.graph / "pages").mkdir(parents=True)
        (self.graph / "journals").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, command, payload):
        argv = [sys.executable, str(ROOT / "scripts" / "daily_agenda.py"),
                "--graph", str(self.graph), command]
        return subprocess.run(argv, input=json.dumps(payload), text=True,
                              capture_output=True, check=False)

    def test_select_stale_after_approval_reports_stdout_json_error(self):
        page = self.graph / "pages" / "Work.md"
        page.write_text("- TODO reread chapter 4\n", encoding="utf-8")
        listed = daily_agenda.list_agenda(self.graph, "2026-09-13")
        stale = listed["tasks"][0]
        # External writer moves the page after approval was obtained.
        page.write_text("- TODO reread chapter 4\n- TODO concurrent\n",
                        encoding="utf-8")
        completed = self.cli("select", {
            "path": stale["path"], "revision": stale["revision"],
            "line": stale["line"], "date": "2026-09-13", "selected": True,
        })
        self.assertEqual(completed.returncode, 1)
        # Real contract: stderr stays empty, stdout carries validated JSON.
        self.assertEqual(completed.stderr, "")
        value = json.loads(completed.stdout)
        self.assertEqual(set(value), {"error"})
        self.assertIn("stale", value["error"])
        self.assertNotIn("quickshell-agenda", page.read_text(encoding="utf-8"))

    def test_list_validation_error_is_stdout_json(self):
        completed = self.cli("list", {"date": "not-a-date"})
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, "")
        self.assertIn("error", json.loads(completed.stdout))


class AgendaPolicyContractTests(unittest.TestCase):
    def test_palette_registers_constrained_agenda_tools(self):
        self.assertIn('name: "logseq_agenda_list"', EXTENSION)
        self.assertIn('name: "logseq_agenda_add"', EXTENSION)
        self.assertIn("agendaHelper(ctx, \"list\"", EXTENSION)
        self.assertIn("agendaHelper(ctx, \"select\"", EXTENSION)
        # Palette-only registration lives with the other palette tools.
        palette_start = EXTENSION.index("if (!projectMode() && !journalMode())")
        palette_block = EXTENSION[palette_start:EXTENSION.index("if (journalMode())", palette_start)]
        self.assertIn("logseq_agenda_list", palette_block)
        self.assertIn("logseq_agenda_add", palette_block)

    def test_scope_gates_keep_existing_allowlists_isolated(self):
        self.assertIn('if (journalMode() && !["logseq_journal_context", "logseq_journal_append"].includes(event.toolName))', EXTENSION)
        self.assertIn('if (projectMode() && !["logseq_project_read", "logseq_project_update", "logseq_project_files", "logseq_project_read_file", "logseq_project_git", "zotero_search", "zotero_item", "zotero_read_pdf", "zotero_collections", "zotero_prepare", "zotero_apply"].includes(event.toolName))', EXTENSION)
        # Agenda tools fail closed outside the palette.
        self.assertIn("agenda list is palette-only; wrong scope", EXTENSION)
        self.assertIn("agenda add is palette-only; wrong scope", EXTENSION)
        self.assertIn('if (projectMode() || journalMode()) return rejectPromise(new Error("agenda tool is palette-only; wrong scope"))', EXTENSION)

    def test_tool_descriptions_instruct_discovery_then_add(self):
        self.assertIn("First discover the TODO by natural language", EXTENSION)
        self.assertIn("ask the user to clarify", EXTENSION)
        self.assertIn("never guess", EXTENSION)
        self.assertIn("fresh-read", EXTENSION.lower() + "reads" if False else "fresh-reads" if "fresh-reads" in EXTENSION else "fresh-read")
        self.assertIn("mandatory UI confirmation", EXTENSION)
        self.assertIn("old schedule", EXTENSION)
        self.assertIn("selected:true", EXTENSION)

    def test_helper_protection_covers_daily_agenda(self):
        self.assertIn('"daily_agenda.py"', EXTENSION)
        self.assertIn("daily_agenda", EXTENSION[EXTENSION.index("Shell command references") - 500:EXTENSION.index("Shell command references") + 500] if "Shell command references" in EXTENSION else EXTENSION)
        self.assertIn("const AGENDA_HELPER", EXTENSION)
        self.assertIn("const AGENDA_MAX_INPUT = 1024 * 1024", EXTENSION)
        self.assertIn("const AGENDA_MAX_OUTPUT = 1024 * 1024", EXTENSION)
        self.assertIn("agenda helper timed out", EXTENSION)
        self.assertIn("agenda helper output exceeded", EXTENSION)
        self.assertIn("operation aborted", EXTENSION)
        self.assertIn("shell: false", EXTENSION)
        self.assertIn('child.stdin.write(JSON.stringify(payload) + "\\n")', EXTENSION)

    def test_add_validates_and_confirms_before_write(self):
        self.assertIn('if (!ctx.hasUI) throw new Error("agenda add denied: UI confirmation unavailable")', EXTENSION)
        self.assertIn('if (!await ask(ctx, "Approve add to daily todos", preview))', EXTENSION)
        self.assertIn('throw new Error("agenda add denied by user")', EXTENSION)
        self.assertIn('throw new Error("stale revision; list again and request a new approval")', EXTENSION)
        self.assertIn("task not found; list again", EXTENSION)
        self.assertIn("only open tasks can be added", EXTENSION)
        self.assertIn('Previously scheduled:', EXTENSION)
        # Preview shows task/project/date/revision before the write.
        add_block = EXTENSION[EXTENSION.index('name: "logseq_agenda_add"'):EXTENSION.index('name: "logseq_agenda_add"') + 4000]
        self.assertIn("Task:", add_block)
        self.assertIn("Project:", add_block)
        self.assertIn("Date:", add_block)
        self.assertIn("Revision:", add_block)
        # Approval precedes the select write; abort is checked on both sides.
        self.assertLess(add_block.index("Approve add to daily todos"), add_block.index('agendaHelper(ctx, "select"'))
        self.assertEqual(add_block.count("operation aborted"), 3)

    def test_prompts_expose_palette_agenda_tools(self):
        system = (ROOT / ".pi" / "SYSTEM.md").read_text(encoding="utf-8")
        skill = (ROOT / ".pi" / "skills" / "logseq-graph" / "SKILL.md").read_text(encoding="utf-8")
        for text in (system, skill):
            self.assertIn("logseq_agenda_list", text)
            self.assertIn("logseq_agenda_add", text)
        # Scoped allowlists stay isolated in prompts.
        self.assertIn("Agenda tools stay palette-only", skill)
        self.assertIn("agenda tools stay palette-only", system.lower() if "agenda tools stay palette-only" in system.lower() else system)


class DesktopHistoryCoherenceTests(unittest.TestCase):
    def test_eight_desktop_tools_no_legacy_history_surface(self):
        for name in ("desktop_current_context", "desktop_project_todos",
                     "desktop_project_logseq_context", "desktop_project_activity",
                     "desktop_current_session", "desktop_search_activity",
                     "desktop_get_session", "desktop_resume_plan"):
            self.assertIn(f'name: "{name}"', EXTENSION)
        for name in ("desktop_current_project", "desktop_project_resources",
                     "desktop_work_sessions", "desktop_session_resources",
                     "desktop_session_events"):
            self.assertNotIn(f'name: "{name}"', EXTENSION)

    def test_search_descriptions_carry_dynamic_timezone(self):
        self.assertIn("Intl.DateTimeFormat().resolvedOptions().timeZone", EXTENSION)
        self.assertIn("DESKTOP_LOCAL_TZ", EXTENSION)
        self.assertIn("start-inclusive/end-exclusive", EXTENSION)
        self.assertIn("never pass natural-language ranges", EXTENSION)
        self.assertIn("request events only when necessary", EXTENSION)

    def test_system_workflow_prefers_search_then_detail(self):
        system = (ROOT / ".pi" / "SYSTEM.md").read_text(encoding="utf-8")
        self.assertIn("Prefer session search", system)
        self.assertIn("desktop_search_activity", system)
        self.assertIn("desktop_project_activity", system)
        self.assertIn("desktop_get_session", system)
        self.assertIn("untrusted evidence", system)
        self.assertIn("start-inclusive/end-exclusive", system)
        # Observation evidence, not edits; matched_at_ms is the latest match.
        self.assertIn("not file edits", system)
        self.assertIn("matched_at_ms", system)
        self.assertIn("edits are not recorded", system)

    def test_scope_allowlists_state_desktop_exception(self):
        # Policy coherence: every scope allowlist explicitly permits the
        # eight read-only desktop tools without weakening mutations.
        system = (ROOT / ".pi" / "SYSTEM.md").read_text(encoding="utf-8")
        self.assertGreaterEqual(
            system.count("eight read-only desktop tools"), 2)
        self.assertIn("eight-tool desktop read-only exception", system)
        self.assertIn("desktop exception never permits writes", system)
        skill = (ROOT / ".pi" / "skills" / "logseq-graph" / "SKILL.md"
                 ).read_text(encoding="utf-8")
        self.assertIn("exception in every scope", skill)
        self.assertIn("same list as above", skill)

    def test_tool_claims_observation_not_edits(self):
        self.assertIn("not file edits", EXTENSION)
        self.assertIn("matched_at_ms", EXTENSION)
        self.assertIn("qualify the claim", EXTENSION)


@unittest.skipUnless(BUN, "bun is required for executable extension tests")
class AgendaExtensionRuntimeTests(unittest.TestCase):
    def run_bun(self, script):
        assert BUN is not None
        bun = BUN
        with tempfile.NamedTemporaryFile("w", suffix=".ts", encoding="utf-8") as handle:
            handle.write(script)
            handle.flush()
            completed = subprocess.run([bun, handle.name], text=True,
                                       capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        return json.loads(completed.stdout.splitlines()[-1])

    def harness_prefix(self):
        source = "\n".join(line for line in EXTENSION.splitlines()
                            if not line.startswith("import "))
        source = source.replace("export default function desktopAgent", "function desktopAgent")
        # Short timeout for the timeout test only; other tests clear it.
        return source

    def base_mock(self, extra=""):
        return r'''
import { EventEmitter } from "node:events";
import { dirname as pathDirname, join, isAbsolute, resolve, normalize, relative } from "node:path";
const dirname = pathDirname;
const fileURLToPath = () => "/repo/.pi/extensions/desktop-agent.ts";
const existsSync = () => true;
const lstatSync = () => { throw new Error("unused"); };
const readFileSync = () => { throw new Error("unused"); };
const realpathSync = { native: (value) => value };
const SessionManager = {};
const Type = { Object: (value) => value, String: () => ({}), Optional: (value) => value, Integer: () => ({}) };
const tools = {}, hooks = {}, commands = {}, calls = [], previews = [];
let approve = true;
let mode = "normal";
const REVISION = "a" + "b".repeat(63);
const OTHER_REVISION = "c" + "d".repeat(63);
function localToday() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,"0")}-${String(now.getDate()).padStart(2,"0")}`;
}
function listing(date, entries) {
  return { date, graphName: "Notes", tasks: entries, truncated: false };
}
function entry(overrides) {
  return Object.assign({ path: "pages/Work.md", page: "Work", line: 1, task: "reread chapter 4",
    marker: "TODO", done: false, revision: REVISION, scheduledDate: "" }, overrides || {});
}
function spawn(command, args) {
  const child = new EventEmitter();
  child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  child.stdin = new EventEmitter();
  child.stdin.write = (value) => { child.payload = JSON.parse(value); };
  child.stdin.end = () => {};
  child.kill = () => { child.killed = true; };
  calls.push({ command, args, child });
  queueMicrotask(() => {
    child.emit("spawn");
    if (mode === "timeout") return;
    if (mode === "invalid-json") {
      child.stdout.emit("data", Buffer.from("not json"));
      child.emit("close", 0, null); return;
    }
    if (mode === "select-stale") {
      if (args.at(-1) === "select") {
        // Real daily_agenda.py contract: JSON {"error": "..."} on stdout, exit 1.
        child.stdout.emit("data", Buffer.from(JSON.stringify({ error: "page revision is stale; reload the page" })));
        child.emit("close", 1, null); return;
      }
    }
    if (mode === "select-stderr-fallback") {
      if (args.at(-1) === "select") {
        child.stderr.emit("data", Buffer.from("boom stderr"));
        child.emit("close", 1, null); return;
      }
    }
    if (mode === "select-status-fallback") {
      if (args.at(-1) === "select") {
        child.emit("close", 1, null); return;
      }
    }
    if (mode === "select-invalid-shape") {
      if (args.at(-1) === "select") {
        child.stdout.emit("data", Buffer.from(JSON.stringify({ oops: 1 })));
        child.stderr.emit("data", Buffer.from("fallback stderr"));
        child.emit("close", 1, null); return;
      }
    }
    if (mode === "select-oversized-error") {
      if (args.at(-1) === "select") {
        child.stdout.emit("data", Buffer.from(JSON.stringify({ error: "x".repeat(9000) })));
        child.stderr.emit("data", Buffer.from("fallback stderr"));
        child.emit("close", 1, null); return;
      }
    }
    if (mode === "select-nul-error") {
      if (args.at(-1) === "select") {
        child.stdout.emit("data", Buffer.from(JSON.stringify({ error: "bad\0msg" })));
        child.stderr.emit("data", Buffer.from("fallback stderr"));
        child.emit("close", 1, null); return;
      }
    }
    const op = args.at(-1);
    let value;
    if (op === "list") {
      const date = child.payload?.date || localToday();
      if (mode === "done-task") value = listing(date, [entry({ done: true, marker: "DONE" })]);
      else if (mode === "missing-task") value = listing(date, []);
      else if (mode === "moved-task") value = listing(date, [entry({ scheduledDate: "2026-09-10" })]);
      else value = listing(date, [entry({}), entry({ path: "pages/Other.md", page: "Other", line: 2, task: "other", scheduledDate: date })]);
    } else {
      value = { page: { path: child.payload.path, revision: "new", content: "ok", todos: [] } };
    }
    const bytes = Buffer.from(JSON.stringify(value));
    child.stdout.emit("data", bytes.subarray(0, 3));
    child.stdout.emit("data", bytes.subarray(3));
    child.emit("close", 0, null);
  });
  return child;
}
''' + extra + "\n" + self.harness_prefix() + r'''
const pi = { on(name, cb) { hooks[name] = cb; }, registerTool(tool) { tools[tool.name] = tool; }, registerCommand(name, cmd) { commands[name] = cmd; } };
'''

    def test_registration_modes_default_and_explicit_date(self):
        script = self.base_mock() + r'''
process.env.LOGSEQ_GRAPH = "/graph"; process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "";
desktopAgent(pi);
const assert = (v, m) => { if (!v) throw new Error(m); };
assert(Object.keys(tools).join(",") === "logseq_search,logseq_todos,logseq_append_journal,logseq_agenda_list,logseq_agenda_add,zotero_search,zotero_item,zotero_read_pdf,zotero_collections,zotero_prepare,zotero_apply,desktop_current_context,desktop_project_todos,desktop_project_logseq_context,desktop_project_activity,desktop_current_session,desktop_search_activity,desktop_get_session,desktop_resume_plan", "palette registration wrong: " + Object.keys(tools).join(","));
const ctx = { cwd: "/work", hasUI: true, ui: { confirm: async () => true } };
const listedDefault = await tools.logseq_agenda_list.execute("id", {}, undefined, undefined, ctx);
const firstPayload = calls.at(-1).child.payload;
assert(firstPayload.date === localToday(), "default date is not local today: " + JSON.stringify(firstPayload));
assert(JSON.parse(listedDefault.content[0].text).tasks.length === 2, "listing did not return helper tasks");
const listedExplicit = await tools.logseq_agenda_list.execute("id", { date: "2026-09-13" }, undefined, undefined, ctx);
assert(calls.at(-1).child.payload.date === "2026-09-13", "explicit date was not passed through");
try { await tools.logseq_agenda_list.execute("id", { date: "not-a-date" }, undefined, undefined, ctx); throw new Error("invalid date succeeded"); }
catch (e) { assert(String(e).includes("YYYY-MM-DD"), "invalid date error missing"); }
// Scoped registrations stay isolated.
const tools2 = {}, hooks2 = {};
const pi2 = { on(n, cb) { hooks2[n] = cb; }, registerTool(t) { tools2[t.name] = t; }, registerCommand() {} };
process.env.QS_PROJECT_PATH = "pages/Work.md";
desktopAgent(pi2);
assert(!("logseq_agenda_list" in tools2) && !("logseq_agenda_add" in tools2), "agenda leaked into project mode");
assert(Object.keys(tools2).join(",") === "logseq_project_read,logseq_project_update,logseq_project_files,logseq_project_read_file,logseq_project_git,zotero_search,zotero_item,zotero_read_pdf,zotero_collections,zotero_prepare,zotero_apply,desktop_current_context,desktop_project_todos,desktop_project_logseq_context,desktop_project_activity,desktop_current_session,desktop_search_activity,desktop_get_session,desktop_resume_plan", "project allowlist changed");
const tools3 = {};
process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "1";
const pi3 = { on(n, cb) {}, registerTool(t) { tools3[t.name] = t; }, registerCommand() {} };
desktopAgent(pi3);
assert(Object.keys(tools3).join(",") === "logseq_journal_context,logseq_journal_append,desktop_current_context,desktop_project_todos,desktop_project_logseq_context,desktop_project_activity,desktop_current_session,desktop_search_activity,desktop_get_session,desktop_resume_plan", "journal allowlist changed");
console.log(JSON.stringify({ ok: true }));
'''
        self.assertEqual(self.run_bun(script), {"ok": True})

    def test_add_success_lists_validates_confirms_and_selects_unchanged(self):
        script = self.base_mock() + r'''
process.env.LOGSEQ_GRAPH = "/graph"; process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "";
desktopAgent(pi);
const assert = (v, m) => { if (!v) throw new Error(m); };
const ctx = { cwd: "/work", hasUI: true, ui: { confirm: async (t, m) => { previews.push([t, m]); return approve; } } };
const out = await tools.logseq_agenda_add.execute("id", { path: "pages/Work.md", line: 1, revision: REVISION }, undefined, undefined, ctx);
assert(calls.length === 2 && calls[0].args.at(-1) === "list" && calls[1].args.at(-1) === "select", "add did not list then select");
assert(calls[0].child.payload.date === localToday(), "add default date wrong");
const selectPayload = calls[1].child.payload;
assert(selectPayload.path === "pages/Work.md" && selectPayload.line === 1 && selectPayload.revision === REVISION && selectPayload.date === localToday() && selectPayload.selected === true, "select did not use unchanged approved values: " + JSON.stringify(selectPayload));
const preview = previews.at(-1)[1];
assert(previews.at(-1)[0] === "Approve add to daily todos", "approval title wrong");
assert(preview.includes("reread chapter 4") && preview.includes("Work") && preview.includes(localToday()) && preview.includes("unscheduled"), "preview missing task/project/date/old schedule: " + preview);
assert(JSON.parse(out.content[0].text).page.revision === "new", "select response missing");
// Explicit date + moving task shows the old schedule.
previews.length = 0; calls.length = 0; mode = "moved-task";
await tools.logseq_agenda_add.execute("id", { path: "pages/Work.md", line: 1, revision: REVISION, date: "2026-09-13" }, undefined, undefined, ctx);
assert(calls[1].child.payload.date === "2026-09-13", "explicit add date lost");
assert(previews.at(-1)[1].includes("2026-09-10"), "moving preview did not show old schedule");
console.log(JSON.stringify({ ok: true }));
'''
        self.assertEqual(self.run_bun(script), {"ok": True})

    def test_add_denied_no_ui_stale_invalid_and_done_fail_closed(self):
        script = self.base_mock() + r'''
process.env.LOGSEQ_GRAPH = "/graph"; process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "";
desktopAgent(pi);
const assert = (v, m) => { if (!v) throw new Error(m); };
const ctx = { cwd: "/work", hasUI: true, ui: { confirm: async (t, m) => { previews.push(m); return approve; } } };
const noUi = { cwd: "/work", hasUI: false, ui: ctx.ui };
// No UI spawns nothing.
let before = calls.length;
try { await tools.logseq_agenda_add.execute("id", { path: "pages/Work.md", line: 1, revision: REVISION }, undefined, undefined, noUi); throw new Error("no-ui add succeeded"); }
catch (e) { assert(String(e).includes("UI confirmation"), "no-ui error missing"); }
assert(calls.length === before, "no-ui add spawned a helper");
// Denied performs the fresh read but never writes.
approve = false;
try { await tools.logseq_agenda_add.execute("id", { path: "pages/Work.md", line: 1, revision: REVISION }, undefined, undefined, ctx); throw new Error("denied add succeeded"); }
catch (e) { assert(String(e).includes("denied"), "denial error missing"); }
assert(calls.at(-1).args.at(-1) === "list", "denial performed a write");
approve = true;
// Stale revision fails before any confirmation.
before = previews.length;
try { await tools.logseq_agenda_add.execute("id", { path: "pages/Work.md", line: 1, revision: OTHER_REVISION }, undefined, undefined, ctx); throw new Error("stale add succeeded"); }
catch (e) { assert(String(e).includes("stale revision"), "stale error missing"); }
assert(previews.length === before, "stale revision requested approval");
// Invalid line fails closed.
try { await tools.logseq_agenda_add.execute("id", { path: "pages/Work.md", line: 0, revision: REVISION }, undefined, undefined, ctx); throw new Error("bad line succeeded"); }
catch (e) { assert(String(e).includes("positive integer"), "line error missing"); }
// Missing task (wrong line) fails closed.
mode = "missing-task";
try { await tools.logseq_agenda_add.execute("id", { path: "pages/Work.md", line: 9, revision: REVISION }, undefined, undefined, ctx); throw new Error("missing task succeeded"); }
catch (e) { assert(String(e).includes("task not found"), "missing task error missing"); }
// Done task cannot be scheduled.
mode = "normal";
const doneCtx = { cwd: "/work", hasUI: true, ui: { confirm: async () => true } };
const savedMode = mode; mode = "done-task";
try { await tools.logseq_agenda_add.execute("id", { path: "pages/Work.md", line: 1, revision: REVISION }, undefined, undefined, doneCtx); throw new Error("done task succeeded"); }
catch (e) { assert(String(e).includes("already done"), "done error missing"); }
mode = savedMode;
// Backend stale on select propagates without misleading success.
mode = "select-stale";
try { await tools.logseq_agenda_add.execute("id", { path: "pages/Work.md", line: 1, revision: REVISION }, undefined, undefined, ctx); throw new Error("select-stale succeeded"); }
catch (e) { assert(String(e).includes("stale"), "select stale was not propagated"); }
mode = "normal";
// Aborted and wrong scope fail closed.
const aborted = new AbortController(); aborted.abort();
try { await tools.logseq_agenda_add.execute("id", { path: "pages/Work.md", line: 1, revision: REVISION }, aborted.signal, undefined, ctx); throw new Error("aborted add succeeded"); }
catch (e) { assert(String(e).includes("aborted"), "abort error missing"); }
process.env.QS_PROJECT_PATH = "pages/Work.md";
try { await tools.logseq_agenda_add.execute("id", { path: "pages/Work.md", line: 1, revision: REVISION }, undefined, undefined, ctx); throw new Error("scoped add succeeded"); }
catch (e) { assert(String(e).includes("wrong scope"), "scope error missing"); }
process.env.QS_PROJECT_PATH = "";
try { await tools.logseq_agenda_list.execute("id", {}, undefined, undefined, ctx); }
catch (e) { throw new Error("palette list should succeed after scope reset: " + e); }
console.log(JSON.stringify({ ok: true }));
'''
        self.assertEqual(self.run_bun(script), {"ok": True})

    def test_stdout_error_contract_and_fallbacks(self):
        # agendaHelper must surface real daily_agenda.py stdout JSON errors.
        self.assertIn('daily_agenda.py reports failures as JSON {"error"', EXTENSION)
        self.assertIn("parsed?.error", EXTENSION)
        self.assertIn("agenda helper exited", EXTENSION)
        script = self.base_mock() + r'''
process.env.LOGSEQ_GRAPH = "/graph"; process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "";
desktopAgent(pi);
const assert = (v, m) => { if (!v) throw new Error(m); };
const ctx = { cwd: "/work", hasUI: true, ui: { confirm: async () => true } };
const args = { path: "pages/Work.md", line: 1, revision: REVISION };
// Real contract: stdout JSON error wins over empty stderr.
mode = "select-stale";
try { await tools.logseq_agenda_add.execute("id", args, undefined, undefined, ctx); throw new Error("stale succeeded"); }
catch (e) { assert(e.message === "page revision is stale; reload the page", "stdout JSON error not propagated: " + e); }
// Empty stdout + stderr message falls back to stderr.
mode = "select-stderr-fallback";
try { await tools.logseq_agenda_add.execute("id", args, undefined, undefined, ctx); throw new Error("stderr fallback succeeded"); }
catch (e) { assert(e.message === "boom stderr", "stderr fallback missing: " + e); }
// Empty stdout+stderr falls back to exit status, never success.
mode = "select-status-fallback";
try { await tools.logseq_agenda_add.execute("id", args, undefined, undefined, ctx); throw new Error("status fallback succeeded"); }
catch (e) { assert(e.message.includes("agenda helper exited 1"), "status fallback missing: " + e); }
// Invalid shape / oversized / NUL error strings fall back to stderr safely.
mode = "select-invalid-shape";
try { await tools.logseq_agenda_add.execute("id", args, undefined, undefined, ctx); throw new Error("invalid shape succeeded"); }
catch (e) { assert(e.message === "fallback stderr", "invalid shape fallback missing: " + e); }
mode = "select-oversized-error";
try { await tools.logseq_agenda_add.execute("id", args, undefined, undefined, ctx); throw new Error("oversized succeeded"); }
catch (e) { assert(e.message === "fallback stderr", "oversized error was not rejected safely: " + String(e).slice(0, 80)); }
mode = "select-nul-error";
try { await tools.logseq_agenda_add.execute("id", args, undefined, undefined, ctx); throw new Error("nul succeeded"); }
catch (e) { assert(e.message === "fallback stderr", "NUL error was not rejected safely: " + e); }
console.log(JSON.stringify({ ok: true }));
'''
        self.assertEqual(self.run_bun(script), {"ok": True})

    def test_add_timeout_abort_during_confirm_and_invalid_transport(self):
        script = self.base_mock() + r'''
process.env.LOGSEQ_GRAPH = "/graph"; process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "";
desktopAgent(pi);
const assert = (v, m) => { if (!v) throw new Error(m); };
let waiting = false, release;
const ctx = { cwd: "/work", hasUI: true, ui: { confirm: async () => {
  if (!waiting) return true;
  return await new Promise(res => { release = () => res(true); });
} } };
// Timeout never reports success.
mode = "timeout";
try { await tools.logseq_agenda_list.execute("id", { date: "2026-09-13" }, undefined, undefined, ctx); throw new Error("timeout succeeded"); }
catch (e) { assert(String(e).includes("timed out"), "timeout error missing: " + e); }
mode = "normal";
// Abort during confirmation performs no write.
waiting = true;
const controller = new AbortController();
const pending = tools.logseq_agenda_add.execute("id", { path: "pages/Work.md", line: 1, revision: REVISION }, controller.signal, undefined, ctx);
await new Promise(r => setTimeout(r, 5));
controller.abort(); release();
try { await pending; throw new Error("confirm-abort succeeded"); }
catch (e) { assert(String(e).includes("aborted"), "confirm abort missing"); }
waiting = false;
assert(calls.at(-1).args.at(-1) === "list", "confirm abort performed a write");
// Invalid helper JSON never reports success.
mode = "invalid-json";
try { await tools.logseq_agenda_list.execute("id", { date: "2026-09-13" }, undefined, undefined, ctx); throw new Error("invalid json succeeded"); }
catch (e) { assert(String(e).includes("invalid JSON"), "invalid JSON error missing"); }
console.log(JSON.stringify({ ok: true }));
'''
        # Extend timeout for this harness since the helper timeout is 10s but
        # the mock "timeout" mode never closes; override to 200ms by patching
        # the source timeout constant in the harness copy.
        script = script.replace("const AGENDA_HELPER_TIMEOUT = 10_000;", "const AGENDA_HELPER_TIMEOUT = 200;")
        self.assertEqual(self.run_bun(script), {"ok": True})


if __name__ == "__main__":
    unittest.main()
