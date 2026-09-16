"""Pi integration tests for the read-only desktop_resume_plan tool (Phase 6).

Covers routing, argument validation, omitted behavior by scope, exact
pinned-page registry resolution, ambiguity/no-mapping, abort/timeout/
output-cap/group cleanup, and policy coherence. No execute tool exists.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
EXTENSION = (ROOT / ".pi" / "extensions" / "desktop-agent.ts").read_text(encoding="utf-8")
BUN = shutil.which("bun")

PALETTE_TOOLS = (
    "logseq_search,logseq_todos,logseq_append_journal,logseq_agenda_list,"
    "logseq_agenda_add,zotero_search,zotero_item,zotero_read_pdf,"
    "zotero_collections,zotero_prepare,zotero_apply,desktop_current_context,"
    "desktop_project_todos,"
    "desktop_project_logseq_context,desktop_project_activity,"
    "desktop_current_session,desktop_search_activity,desktop_get_session,"
    "desktop_resume_plan"
)
PROJECT_TOOLS = (
    "logseq_project_read,logseq_project_update,logseq_project_files,"
    "logseq_project_read_file,logseq_project_git,zotero_search,zotero_item,"
    "zotero_read_pdf,zotero_collections,zotero_prepare,zotero_apply,"
    "desktop_current_context,"
    "desktop_project_todos,desktop_project_logseq_context,"
    "desktop_project_activity,desktop_current_session,"
    "desktop_search_activity,desktop_get_session,desktop_resume_plan"
)
JOURNAL_TOOLS = (
    "logseq_journal_context,logseq_journal_append,desktop_current_context,"
    "desktop_project_todos,desktop_project_logseq_context,"
    "desktop_project_activity,desktop_current_session,"
    "desktop_search_activity,desktop_get_session,desktop_resume_plan"
)
DESKTOP_EIGHT = (
    "desktop_current_context", "desktop_project_todos",
    "desktop_project_logseq_context", "desktop_project_activity",
    "desktop_current_session", "desktop_search_activity",
    "desktop_get_session", "desktop_resume_plan",
)


class ResumeStaticContractTests(unittest.TestCase):
    def test_read_allowlist_has_eight_tools(self):
        allowlist = EXTENSION.split("const DESKTOP_READ_TOOLS")[1].split("]")[0]
        for name in DESKTOP_EIGHT:
            self.assertIn(f'"{name}"', allowlist)
        for name in ("desktop_current_project", "desktop_project_resources",
                     "desktop_work_sessions", "desktop_session_resources",
                     "desktop_session_events"):
            self.assertNotIn(f'"{name}"', allowlist)

    def test_registers_resume_plan_tool(self):
        self.assertIn('name: "desktop_resume_plan"', EXTENSION)
        self.assertIn("desktopResumePlanSchema", EXTENSION)
        self.assertIn("desktopResumeHelper(ctx, \"plan\"", EXTENSION)
        self.assertIn("DESKTOP_RESUME_HELPER", EXTENSION)
        self.assertIn("desktop_resume.py", EXTENSION)

    def test_no_execute_tool_exposed(self):
        for name in ("desktop_resume_execute", "desktop_execute",
                     "resume_execute", "resume_run", "desktop_resume_run"):
            self.assertNotIn(f'name: "{name}"', EXTENSION)
        self.assertNotIn('desktopResumeHelper(ctx, "execute"', EXTENSION)
        # The plan tool never routes an execute command.
        block = EXTENSION[EXTENSION.index('name: "desktop_resume_plan"'):
                          EXTENSION.index('name: "desktop_resume_plan"') + 3000]
        self.assertIn('"plan"', block)
        self.assertNotIn('"execute"', block)
        # No shell anywhere in the resume path.
        self.assertNotIn("desktop_resume.py plan --project", EXTENSION.replace(
            "scripts/desktop_resume.py plan", ""))

    def test_helper_uses_bounded_group_kill_architecture(self):
        self.assertIn("const DESKTOP_RESUME_HELPER", EXTENSION)
        self.assertIn("const PROJECTS_LIST_HELPER", EXTENSION)
        self.assertIn("DESKTOP_RESUME_HELPER_TIMEOUT", EXTENSION)
        self.assertIn("DESKTOP_RESUME_MAX_OUTPUT", EXTENSION)
        self.assertIn("PROJECTS_LIST_TIMEOUT", EXTENSION)
        self.assertIn("PROJECTS_LIST_MAX_OUTPUT", EXTENSION)
        self.assertIn("desktop helper timed out", EXTENSION)
        self.assertIn("desktop helper output exceeded", EXTENSION)
        self.assertIn("desktop helper returned invalid JSON", EXTENSION)
        self.assertIn("shell: false", EXTENSION)
        self.assertIn("operation aborted", EXTENSION)
        self.assertIn("killDesktopGroup", EXTENSION)
        self.assertIn("detached: process.platform", EXTENSION)
        self.assertIn('killDesktopGroup(child, "SIGKILL")', EXTENSION)
        self.assertIn("process.kill(-pid", EXTENSION)
        self.assertIn("beginTermination", EXTENSION)
        self.assertIn("terminationBegun", EXTENSION)
        self.assertIn("desktop request exceeds 8 KiB", EXTENSION)

    def test_argument_validation_strings(self):
        self.assertIn("project must be nonempty (1..256 chars)", EXTENSION)
        self.assertIn("project must not contain NUL", EXTENSION)
        self.assertIn("project must be 1..256 chars", EXTENSION)
        self.assertIn("--project=${", EXTENSION)
        self.assertIn("project is required outside project mode", EXTENSION)
        self.assertIn("pinned project page is not registered", EXTENSION)
        self.assertIn("pinned project page is ambiguous", EXTENSION)

    def test_pinned_resolution_uses_exact_registry_match(self):
        self.assertIn("resolvePinnedResumeProjectId", EXTENSION)
        self.assertIn("projectsListHelper", EXTENSION)
        self.assertIn("process.env.QS_PROJECT_PATH", EXTENSION[
            EXTENSION.index("resolvePinnedResumeProjectId"):
            EXTENSION.index("resolvePinnedResumeProjectId") + 2500])
        # Exact logseq_path match, never a model-supplied path.
        pinned_block = EXTENSION[EXTENSION.index("resolvePinnedResumeProjectId"):
                                 EXTENSION.index("resolvePinnedResumeProjectId") + 2500]
        self.assertIn("logseq_path", pinned_block)
        self.assertIn("===", pinned_block)

    def test_tool_description_states_preview_contract(self):
        block = EXTENSION[EXTENSION.index('name: "desktop_resume_plan"'):
                          EXTENSION.index('name: "desktop_resume_plan"') + 4000]
        lowered = block.lower()
        for phrase in ("deterministic structured context",
                       "current registry metadata",
                       "latest current-device work session",
                       "selected files",
                       "repository",
                       "repository/observed branch",
                       "Logseq reference/open TODOs",
                       "safe Pi session association",
                       "operations availability/warnings",
                       "preview/read-only",
                       "does not execute",
                       "write repo contents",
                       "switch Pi sessions",
                       "generate a summary",
                       "observational"):
            self.assertIn(phrase.lower(), lowered)

    def test_protected_paths_cover_resume_helper(self):
        self.assertIn('"desktop_resume.py"', EXTENSION)
        self.assertIn("desktop_resume|projects", EXTENSION)
        self.assertIn("desktop_projects|desktop_resume|projects", EXTENSION)

    def test_policy_coherence_eight_tools(self):
        system = (ROOT / ".pi" / "SYSTEM.md").read_text(encoding="utf-8")
        for name in DESKTOP_EIGHT:
            self.assertIn(name, system)
        self.assertGreaterEqual(system.count("eight read-only desktop tools"), 2)
        self.assertIn("eight-tool desktop read-only exception", system)
        self.assertIn("desktop exception never permits writes", system)
        self.assertIn("desktop_resume_plan", system)
        # Resume workflow guidance.
        for phrase in ("inspect `desktop_resume_plan`",
                       "without `project` to",
                       "user-driven Quickshell",
                       "ProjectPlanner/session infrastructure",
                       "no automatic AI summary"):
            self.assertIn(phrase, system)
        skill = (ROOT / ".pi" / "skills" / "logseq-graph" / "SKILL.md"
                 ).read_text(encoding="utf-8")
        for name in DESKTOP_EIGHT:
            self.assertIn(name, skill)
        self.assertIn("exception in every scope", skill)
        self.assertIn("desktop_resume_plan", skill)
        self.assertIn("pinned", skill)
        readme = (ROOT / ".pi" / "README.md").read_text(encoding="utf-8")
        for name in DESKTOP_EIGHT:
            self.assertIn(name, readme)
        self.assertIn("desktop_resume_plan", readme)
        self.assertIn("No execute tool is exposed", readme)

    def test_existing_seven_tools_unchanged(self):
        for name in ("desktop_current_context", "desktop_project_todos",
                     "desktop_project_logseq_context", "desktop_project_activity",
                     "desktop_current_session", "desktop_search_activity",
                     "desktop_get_session"):
            self.assertIn(f'name: "{name}"', EXTENSION)
        self.assertIn('"current-context"', EXTENSION)
        self.assertIn('"search-activity"', EXTENSION)
        self.assertIn('"get-session"', EXTENSION)
        self.assertIn('"project-activity"', EXTENSION)


@unittest.skipUnless(BUN, "bun is required for executable extension tests")
class ResumeExtensionRuntimeTests(unittest.TestCase):
    def run_bun(self, script):
        assert BUN is not None
        with tempfile.NamedTemporaryFile("w", suffix=".ts", encoding="utf-8") as handle:
            handle.write(script)
            handle.flush()
            completed = subprocess.run([BUN, handle.name], text=True,
                                       capture_output=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        return json.loads(completed.stdout.splitlines()[-1])

    def harness_prefix(self):
        source = "\n".join(line for line in EXTENSION.splitlines()
                            if not line.startswith("import "))
        return source.replace("export default function desktopAgent",
                              "function desktopAgent")

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
const Type = { Object: (value) => value, String: () => ({}), Optional: (value) => value, Integer: () => ({}), Boolean: () => ({}) };
const tools = {}, hooks = {}, commands = {}, calls = [];
let mode = "normal";
let registryEntries = [{ id: "11111111-1111-4111-8111-111111111111", name: "Work", logseq_path: "pages/Work.md" }];
let registryInvalid = false;
function planValue(project) {
  return { version: 1, project: { id: "x", name: project || "Work" }, operations: [] };
}
function spawn(command, args) {
  const child = new EventEmitter();
  child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  child.stdin = new EventEmitter();
  child.stdin.write = () => {}; child.stdin.end = () => {};
  child.kill = (sig) => { child.killed = (child.killed || "") + String(sig || ""); queueMicrotask(() => child.emit("close", null, sig || "SIGTERM")); };
  child.detachedOpt = args;
  calls.push({ command, args, child, shell: false, detached: true });
  queueMicrotask(() => {
    child.emit("spawn");
    const helper = String(args[0] || "");
    if (mode === "timeout") return;
    if (mode === "invalid-json") {
      child.stdout.emit("data", Buffer.from("not json"));
      child.emit("close", 0, null); return;
    }
    if (mode === "interleaved-ok") {
      let payload;
      if (helper.endsWith("/projects.py")) {
        payload = JSON.stringify(registryInvalid ? { nope: 1 } : { projects: registryEntries });
      } else if (helper.endsWith("/desktop_resume.py")) {
        const projArg = (args.find((a) => String(a).startsWith("--project=")) || "").slice("--project=".length);
        payload = JSON.stringify(planValue(projArg));
      } else {
        payload = JSON.stringify({ ok: true });
      }
      const bytes = Buffer.from(payload);
      const third = Math.max(1, Math.floor(bytes.length / 3));
      child.stdout.emit("data", bytes.subarray(0, third));
      child.stderr.emit("data", Buffer.alloc(40, "e"));
      child.stdout.emit("data", bytes.subarray(third, 2 * third));
      child.stderr.emit("data", Buffer.alloc(40, "e"));
      child.stdout.emit("data", bytes.subarray(2 * third));
      child.emit("close", 0, null); return;
    }
    if (mode === "interleaved-overflow") {
      child.stdout.emit("data", Buffer.alloc(150, "x"));
      child.stderr.emit("data", Buffer.alloc(150, "e"));
      child.emit("close", 0, null); return;
    }
    if (mode === "output-overflow") {
      child.stdout.emit("data", Buffer.alloc(1024 * 1024 + 8, "x"));
      child.emit("close", 0, null); return;
    }
    if (helper.endsWith("/projects.py")) {
      if (mode === "registry-error") {
        child.stderr.emit("data", Buffer.from("registry boom"));
        child.emit("close", 1, null); return;
      }
      const value = registryInvalid ? { nope: 1 } : { projects: registryEntries };
      const bytes = Buffer.from(JSON.stringify(value));
      child.stdout.emit("data", bytes); child.emit("close", 0, null); return;
    }
    if (helper.endsWith("/desktop_resume.py")) {
      if (mode === "resume-error") {
        child.stderr.emit("data", Buffer.from("project 'nope' is unknown (no registry entry)"));
        child.emit("close", 1, null); return;
      }
      const projArg = (args.find((a) => String(a).startsWith("--project=")) || "").slice("--project=".length);
      const bytes = Buffer.from(JSON.stringify(planValue(projArg)));
      child.stdout.emit("data", bytes.subarray(0, 3));
      child.stdout.emit("data", bytes.subarray(3));
      child.emit("close", 0, null); return;
    }
    const bytes = Buffer.from(JSON.stringify({ ok: true }));
    child.stdout.emit("data", bytes); child.emit("close", 0, null);
  });
  return child;
}
''' + extra + "\n" + self.harness_prefix() + r'''
const pi = { on(name, cb) { hooks[name] = cb; }, registerTool(tool) { tools[tool.name] = tool; }, registerCommand(name, cmd) { commands[name] = cmd; } };
'''

    def test_registration_in_all_scopes(self):
        script = self.base_mock() + r'''
process.env.LOGSEQ_GRAPH = "/graph"; process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "";
desktopAgent(pi);
const assert = (v, m) => { if (!v) throw new Error(m); };
assert(Object.keys(tools).join(",") === "''' + PALETTE_TOOLS + r'''", "palette registration wrong: " + Object.keys(tools).join(","));
const tools2 = {};
const pi2 = { on(n, cb) {}, registerTool(t) { tools2[t.name] = t; }, registerCommand() {} };
process.env.QS_PROJECT_PATH = "pages/Work.md";
desktopAgent(pi2);
assert(Object.keys(tools2).join(",") === "''' + PROJECT_TOOLS + r'''", "project registration wrong: " + Object.keys(tools2).join(","));
const tools3 = {};
process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "1";
const pi3 = { on(n, cb) {}, registerTool(t) { tools3[t.name] = t; }, registerCommand() {} };
desktopAgent(pi3);
assert(Object.keys(tools3).join(",") === "''' + JOURNAL_TOOLS + r'''", "journal registration wrong: " + Object.keys(tools3).join(","));
// No execute tool in any scope.
for (const names of [Object.keys(tools), Object.keys(tools2), Object.keys(tools3)]) {
  assert(!names.some((n) => n.includes("execute")), "execute tool leaked: " + names.join(","));
}
// Gate passes the plan tool in every scope.
const ctx = { cwd: "/work", hasUI: false, ui: {} };
process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "";
assert(!(await hooks.tool_call({ toolName: "desktop_resume_plan", input: {} }, ctx))?.block, "plan blocked in palette");
process.env.QS_PROJECT_PATH = "pages/Work.md";
assert(!(await hooks.tool_call({ toolName: "desktop_resume_plan", input: {} }, ctx))?.block, "plan blocked in project");
process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "1";
assert(!(await hooks.tool_call({ toolName: "desktop_resume_plan", input: {} }, ctx))?.block, "plan blocked in journal");
console.log(JSON.stringify({ ok: true }));
'''
        self.assertEqual(self.run_bun(script), {"ok": True})

    def test_explicit_routing_and_safe_transport(self):
        script = self.base_mock() + r'''
process.env.LOGSEQ_GRAPH = "/graph"; process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "";
desktopAgent(pi);
const assert = (v, m) => { if (!v) throw new Error(m); };
const ctx = { cwd: "/work", hasUI: false, ui: {} };
const UUID = "00000000-0000-4000-8000-000000000000";
calls.length = 0;
await tools.desktop_resume_plan.execute("id", { project: UUID }, undefined, undefined, ctx);
assert(calls.length === 1, "explicit UUID spawned " + calls.length + " helpers");
assert(calls[0].command === "python3" && String(calls[0].args[0]).includes("desktop_resume.py"), "wrong helper: " + JSON.stringify(calls[0].args));
assert(JSON.stringify(calls[0].args.slice(1)) === JSON.stringify(["plan", "--project=" + UUID]), "UUID argv wrong: " + JSON.stringify(calls[0].args));
// Registry name/unique prefix passes through; backend resolves ambiguity.
calls.length = 0;
await tools.desktop_resume_plan.execute("id", { project: "BeforeIT" }, undefined, undefined, ctx);
assert(JSON.stringify(calls.at(-1).args.slice(1)) === JSON.stringify(["plan", "--project=BeforeIT"]), "name argv wrong");
// Leading-hyphen value stays one argv item, never a flag.
calls.length = 0;
await tools.desktop_resume_plan.execute("id", { project: "--help" }, undefined, undefined, ctx);
assert(JSON.stringify(calls.at(-1).args.slice(1)) === JSON.stringify(["plan", "--project=--help"]), "hyphen argv wrong: " + JSON.stringify(calls.at(-1).args));
// Explicit project is allowed in project scope too.
process.env.QS_PROJECT_PATH = "pages/Work.md";
calls.length = 0;
await tools.desktop_resume_plan.execute("id", { project: UUID }, undefined, undefined, ctx);
assert(JSON.stringify(calls.at(-1).args.slice(1)) === JSON.stringify(["plan", "--project=" + UUID]), "scoped explicit argv wrong");
console.log(JSON.stringify({ ok: true }));
'''
        self.assertEqual(self.run_bun(script), {"ok": True})

    def test_argument_validation_fails_before_spawn(self):
        script = self.base_mock() + r'''
process.env.LOGSEQ_GRAPH = "/graph"; process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "";
desktopAgent(pi);
const assert = (v, m) => { if (!v) throw new Error(m); };
const ctx = { cwd: "/work", hasUI: false, ui: {} };
for (const bad of ["", "   ", "x".repeat(257), "a\0b"]) {
  const before = calls.length;
  try { await tools.desktop_resume_plan.execute("id", { project: bad }, undefined, undefined, ctx); throw new Error("bad project succeeded: " + JSON.stringify(bad).slice(0, 30)); }
  catch (e) { assert(!String(e).includes("bad project succeeded"), String(e)); }
  assert(calls.length === before, "invalid project spawned for: " + JSON.stringify(bad).slice(0, 30));
}
// Backend unknown-project error propagates (no success).
mode = "resume-error";
try { await tools.desktop_resume_plan.execute("id", { project: "nope" }, undefined, undefined, ctx); throw new Error("unknown succeeded"); }
catch (e) { assert(String(e).includes("unknown"), "backend error not propagated: " + e); }
console.log(JSON.stringify({ ok: true }));
'''
        self.assertEqual(self.run_bun(script), {"ok": True})

    def test_omitted_behavior_by_scope(self):
        script = self.base_mock() + r'''
process.env.LOGSEQ_GRAPH = "/graph"; process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "";
desktopAgent(pi);
const assert = (v, m) => { if (!v) throw new Error(m); };
const ctx = { cwd: "/work", hasUI: false, ui: {} };
// Palette omitted errors with no fallback to the current desktop.
let before = calls.length;
try { await tools.desktop_resume_plan.execute("id", {}, undefined, undefined, ctx); throw new Error("palette omitted succeeded"); }
catch (e) { assert(String(e).includes("project is required outside project mode"), "palette omitted error wrong: " + e); }
assert(calls.length === before, "palette omitted spawned a helper");
// Journal omitted errors the same way.
process.env.QS_JOURNAL_MODE = "1";
before = calls.length;
try { await tools.desktop_resume_plan.execute("id", {}, undefined, undefined, ctx); throw new Error("journal omitted succeeded"); }
catch (e) { assert(String(e).includes("project is required outside project mode"), "journal omitted error wrong: " + e); }
assert(calls.length === before, "journal omitted spawned a helper");
process.env.QS_JOURNAL_MODE = "";
// Project omitted resolves the pinned page to its stable id.
process.env.QS_PROJECT_PATH = "pages/Work.md";
calls.length = 0;
const out = await tools.desktop_resume_plan.execute("id", {}, undefined, undefined, ctx);
assert(calls.length === 2, "pinned resolution did not list then plan: " + calls.length);
assert(String(calls[0].args[0]).includes("projects.py") && calls[0].args[1] === "list", "first call was not registry list");
assert(JSON.stringify(calls[1].args.slice(1)) === JSON.stringify(["plan", "--project=11111111-1111-4111-8111-111111111111"]), "pinned plan argv wrong: " + JSON.stringify(calls[1].args));
assert(JSON.parse(out.content[0].text).project.name === "11111111-1111-4111-8111-111111111111", "plan response missing");
console.log(JSON.stringify({ ok: true }));
'''
        self.assertEqual(self.run_bun(script), {"ok": True})

    def test_pinned_resolution_no_mapping_ambiguity_invalid(self):
        script = self.base_mock() + r'''
process.env.LOGSEQ_GRAPH = "/graph"; process.env.QS_JOURNAL_MODE = "";
desktopAgent(pi);
const assert = (v, m) => { if (!v) throw new Error(m); };
const ctx = { cwd: "/work", hasUI: false, ui: {} };
process.env.QS_PROJECT_PATH = "pages/Work.md";
// No mapping errors without planning.
registryEntries = [{ id: "22222222-2222-4222-8222-222222222222", name: "Other", logseq_path: "pages/Other.md" }];
let before = calls.length;
try { await tools.desktop_resume_plan.execute("id", {}, undefined, undefined, ctx); throw new Error("unmapped succeeded"); }
catch (e) { assert(String(e).includes("not registered"), "no-mapping error wrong: " + e); }
assert(calls.length === before + 1 && String(calls.at(-1).args[0]).includes("projects.py"), "no-mapping did not stop after list");
// Ambiguous mapping errors (duplicate logseq_path must never pick one).
registryEntries = [
  { id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", name: "A", logseq_path: "pages/Work.md" },
  { id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", name: "B", logseq_path: "pages/Work.md" },
];
before = calls.length;
try { await tools.desktop_resume_plan.execute("id", {}, undefined, undefined, ctx); throw new Error("ambiguous succeeded"); }
catch (e) { assert(String(e).includes("ambiguous"), "ambiguity error wrong: " + e); }
// Invalid registry shape fails closed.
registryEntries = [{ id: "x", name: "Work", logseq_path: "pages/Work.md" }];
registryInvalid = true;
before = calls.length;
try { await tools.desktop_resume_plan.execute("id", {}, undefined, undefined, ctx); throw new Error("invalid shape succeeded"); }
catch (e) { assert(String(e).includes("unusable"), "invalid shape error wrong: " + e); }
registryInvalid = false;
// Registry helper failure propagates.
mode = "registry-error";
try { await tools.desktop_resume_plan.execute("id", {}, undefined, undefined, ctx); throw new Error("registry error succeeded"); }
catch (e) { assert(String(e).includes("boom"), "registry error not propagated: " + e); }
console.log(JSON.stringify({ ok: true }));
'''
        self.assertEqual(self.run_bun(script), {"ok": True})

    def test_abort_timeout_output_cap_invalid_json(self):
        script = self.base_mock() + r'''
process.env.LOGSEQ_GRAPH = "/graph"; process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "";
desktopAgent(pi);
const assert = (v, m) => { if (!v) throw new Error(m); };
const ctx = { cwd: "/work", hasUI: false, ui: {} };
// Aborted before spawn rejects.
const aborted = new AbortController(); aborted.abort();
try { await tools.desktop_resume_plan.execute("id", { project: "Work" }, aborted.signal, undefined, ctx); throw new Error("aborted succeeded"); }
catch (e) { assert(String(e).includes("aborted"), "abort error missing: " + e); }
// Timeout never reports success.
mode = "timeout";
try { await tools.desktop_resume_plan.execute("id", { project: "Work" }, undefined, undefined, ctx); throw new Error("timeout succeeded"); }
catch (e) { assert(String(e).includes("timed out"), "timeout error missing: " + e); }
mode = "normal";
// Output cap never reports success.
mode = "output-overflow";
try { await tools.desktop_resume_plan.execute("id", { project: "Work" }, undefined, undefined, ctx); throw new Error("overflow succeeded"); }
catch (e) { assert(String(e).includes("exceeded"), "output cap error missing: " + e); }
mode = "normal";
// Invalid JSON never reports success.
mode = "invalid-json";
try { await tools.desktop_resume_plan.execute("id", { project: "Work" }, undefined, undefined, ctx); throw new Error("invalid json succeeded"); }
catch (e) { assert(String(e).includes("invalid JSON"), "invalid JSON error missing: " + e); }
mode = "normal";
// Group cleanup architecture is present in source (checked statically too).
console.log(JSON.stringify({ ok: true }));
'''
        # Shorten only the resume/list timeouts so the timeout case is fast.
        script = script.replace("const DESKTOP_RESUME_HELPER_TIMEOUT = 10_000;",
                                "const DESKTOP_RESUME_HELPER_TIMEOUT = 200;")
        script = script.replace("const PROJECTS_LIST_TIMEOUT = 10_000;",
                                "const PROJECTS_LIST_TIMEOUT = 200;")
        self.assertEqual(self.run_bun(script), {"ok": True})

    def test_interleaved_below_cap_accepted(self):
        script = self.base_mock() + r'''
process.env.LOGSEQ_GRAPH = "/graph"; process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "";
desktopAgent(pi);
const assert = (v, m) => { if (!v) throw new Error(m); };
const ctx = { cwd: "/work", hasUI: false, ui: {} };
mode = "interleaved-ok";
// Resume helper: interleaved stdout JSON + stderr noise below the
// combined cap must be accepted (no false double-count overflow).
const plan = await tools.desktop_resume_plan.execute("id", { project: "Work" }, undefined, undefined, ctx);
assert(plan.content[0].text.includes('"version": 1'), "resume interleaved-ok not accepted");
// Desktop projects helper: same combined-cap accounting.
const current = await tools.desktop_current_context.execute("id", {}, undefined, undefined, ctx);
assert(current.content[0].text.includes('"ok": true'), "desktop projects interleaved-ok not accepted");
// Projects list helper via pinned project resolution: registry list with
// interleaved stderr noise below cap must resolve then plan.
process.env.QS_PROJECT_PATH = "pages/Work.md";
calls.length = 0;
const pinned = await tools.desktop_resume_plan.execute("id", {}, undefined, undefined, ctx);
assert(calls.length === 2, "pinned interleaved-ok did not list then plan: " + calls.length);
assert(JSON.parse(pinned.content[0].text).project.name === "11111111-1111-4111-8111-111111111111", "pinned plan response missing");
console.log(JSON.stringify({ ok: true }));
'''
        # Small combined caps so interleaving exercises the shared counter
        # without large allocations. Totals in interleaved-ok (~150 bytes)
        # stay below 256; the old double-counting code falsely overflowed.
        script = script.replace("const DESKTOP_RESUME_MAX_OUTPUT = 1024 * 1024;",
                                "const DESKTOP_RESUME_MAX_OUTPUT = 256;")
        script = script.replace("const DESKTOP_MAX_OUTPUT = 1024 * 1024;",
                                "const DESKTOP_MAX_OUTPUT = 256;")
        script = script.replace("const PROJECTS_LIST_MAX_OUTPUT = 1024 * 1024;",
                                "const PROJECTS_LIST_MAX_OUTPUT = 256;")
        self.assertEqual(self.run_bun(script), {"ok": True})

    def test_interleaved_above_combined_cap_rejected(self):
        script = self.base_mock() + r'''
process.env.LOGSEQ_GRAPH = "/graph"; process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "";
desktopAgent(pi);
const assert = (v, m) => { if (!v) throw new Error(m); };
const ctx = { cwd: "/work", hasUI: false, ui: {} };
mode = "interleaved-overflow";
// Each stream alone (150 bytes) is below the 256-byte combined cap, but
// the combined 300 bytes exceed it and must be rejected exactly once.
try { await tools.desktop_resume_plan.execute("id", { project: "Work" }, undefined, undefined, ctx); throw new Error("resume overflow succeeded"); }
catch (e) { assert(String(e).includes("exceeded"), "resume combined cap not enforced: " + e); }
try { await tools.desktop_current_context.execute("id", {}, undefined, undefined, ctx); throw new Error("desktop overflow succeeded"); }
catch (e) { assert(String(e).includes("exceeded"), "desktop combined cap not enforced: " + e); }
// Projects list helper overflow surfaces through pinned resolution.
process.env.QS_PROJECT_PATH = "pages/Work.md";
try { await tools.desktop_resume_plan.execute("id", {}, undefined, undefined, ctx); throw new Error("projects list overflow succeeded"); }
catch (e) { assert(String(e).includes("exceeded"), "projects list combined cap not enforced: " + e); }
console.log(JSON.stringify({ ok: true }));
'''
        script = script.replace("const DESKTOP_RESUME_MAX_OUTPUT = 1024 * 1024;",
                                "const DESKTOP_RESUME_MAX_OUTPUT = 256;")
        script = script.replace("const DESKTOP_MAX_OUTPUT = 1024 * 1024;",
                                "const DESKTOP_MAX_OUTPUT = 256;")
        script = script.replace("const PROJECTS_LIST_MAX_OUTPUT = 1024 * 1024;",
                                "const PROJECTS_LIST_MAX_OUTPUT = 256;")
        self.assertEqual(self.run_bun(script), {"ok": True})

    def test_output_accounting_uses_combined_counter(self):
        # Static guard against regressing to per-stream aggregate
        # assignment (outBytes = next / errBytes = next double-counts
        # interleaved chunks). The three desktop helpers must share one
        # combined total counter.
        for helper in ("function desktopProjectsHelper",
                       "function desktopResumeHelper",
                       "function projectsListHelper"):
            start = EXTENSION.index(helper)
            # Bound the block at the next top-level helper/tool boundary.
            end = EXTENSION.index("function ", start + len(helper))
            block = EXTENSION[start:end]
            self.assertIn("totalBytes", block, helper)
            self.assertNotIn("outBytes + errBytes", block, helper)
            self.assertNotIn("outBytes = next", block, helper)
            self.assertNotIn("errBytes = next", block, helper)

    def test_protected_helper_paths_blocked(self):
        script = self.base_mock() + r'''
process.env.LOGSEQ_GRAPH = "/graph"; process.env.QS_PROJECT_PATH = ""; process.env.QS_JOURNAL_MODE = "";
desktopAgent(pi);
const assert = (v, m) => { if (!v) throw new Error(m); };
const ctx = { cwd: "/work", hasUI: false, ui: {} };
const gate = hooks.tool_call;
for (const tool of ["read", "write", "edit"]) {
  const r = await gate({ toolName: tool, input: { path: "scripts/desktop_resume.py" } }, ctx);
  assert(r?.block === true, tool + " desktop_resume.py was not blocked");
}
const shell = await gate({ toolName: "bash", input: { command: "cat scripts/desktop_resume.py" } }, ctx);
assert(shell?.block === true, "shell desktop_resume.py was not blocked");
console.log(JSON.stringify({ ok: true }));
'''
        self.assertEqual(self.run_bun(script), {"ok": True})


if __name__ == "__main__":
    unittest.main()
