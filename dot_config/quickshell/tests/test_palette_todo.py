"""Palette `todo:` quick-add ladder tests (Phase 2b §4.3).

Runs the real CommandPalette.qml JavaScript in node: no QML engine, no
network, no store access. The backends (project_planner page/update,
journal_assistant context/prepare/append) are stubbed at the todoLaunch
boundary; here the argv shapes, payloads, preview discipline, and the
Confirm/Cancel contract are pinned.

`todo: <text>` routes to the current project page's task list, falling
back to today's journal as a plain `- TODO` block. Prepare → exact
preview → Confirm → apply with revision recheck; no silent writes, and
the palette stays open on Confirm for serial entry.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
PALETTE = ROOT / "widgets" / "CommandPalette.qml"
QUERY = ROOT / "widgets" / "PaletteQuery.js"
PALETTE_TEXT = PALETTE.read_text(encoding="utf-8")


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


TODO_FNS = (
    "todoSingleLineError", "todoSafeRef", "todoSessionIdOf",
    "todoProvenance", "todoTargetLabel", "todoComposerSubtitle",
    "todoBegin", "todoLaunch", "todoValidRevision",
    "todoComposePageBlock", "finishTodoStage", "todoConfirmApply",
    "todoCancelConfirm", "cancelTodoStage", "todoReset",
)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class TodoGateTests(unittest.TestCase):
    def run_node(self, script):
        completed = subprocess.run(
            ["node", "-e", script], text=True, capture_output=True,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr)
        return completed.stdout.strip()

    def functions(self, names):
        return {n: extract_function(PALETTE_TEXT, n) for n in names}

    def test_safe_ref_gate_table(self):
        fns = self.functions(("todoSafeRef",))
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
const gate = (v) => root.todoSafeRef(v);
console.log(JSON.stringify({{
  plain: gate("file:/x/a.py"),
  trimmed: gate("  file:/x/a.py  "),
  overlong: gate("x".repeat(161)),
  atLimit: gate("x".repeat(160)),
  token: gate("my api token value"),
  secret: gate("password=hunter2"),
  bearer: gate("Authorization: Bearer abc"),
  ssh: gate("/home/u/.ssh/id_rsa"),
  gnupg: gate("/home/u/.gnupg/pubring"),
  aws: gate("/home/u/.aws/credentials"),
  env: gate("/home/u/.env"),
  piDir: gate("/home/u/.pi/agent"),
  // Substring decoys that merely contain the letters stay valid.
  envMentions: gate("environment setup notes"),
  control: gate("a\\u0001b"),
  newline: gate("a\\nb"),
  encodedNull: gate("%00"),
  blank: gate("   "),
  empty: gate(""),
  nonString: gate(42),
  nullValue: gate(null)
}}));
"""
        value = json.loads(self.run_node(script))
        self.assertEqual(value["plain"], "file:/x/a.py")
        self.assertEqual(value["trimmed"], "file:/x/a.py")
        self.assertEqual(value["overlong"], "")
        self.assertEqual(value["atLimit"], "x" * 160)
        for key in ("token", "secret", "bearer", "ssh", "gnupg", "aws",
                    "env", "piDir", "control", "newline", "encodedNull",
                    "blank", "empty", "nonString", "nullValue"):
            self.assertEqual(value[key], "", key)
        self.assertEqual(value["envMentions"], "environment setup notes")

    def test_session_id_and_revision_gates(self):
        fns = self.functions(("todoSessionIdOf", "todoValidRevision"))
        sid = "a" * 32
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
  valid: root.todoSessionIdOf("{sid}"),
  upper: root.todoSessionIdOf("{sid}".toUpperCase()),
  short: root.todoSessionIdOf("abc"),
  nonHex: root.todoSessionIdOf("g".repeat(32)),
  blank: root.todoSessionIdOf("  "),
  nullId: root.todoSessionIdOf(null),
  revision: root.todoValidRevision("f".repeat(64)),
  missing: root.todoValidRevision("missing"),
  shortRev: root.todoValidRevision("abc"),
  emptyRev: root.todoValidRevision(""),
  nullRev: root.todoValidRevision(null)
}}));
"""
        value = json.loads(self.run_node(script))
        self.assertEqual(value["valid"], "a" * 32)
        self.assertEqual(value["upper"], "a" * 32)
        self.assertEqual(value["short"], "")
        self.assertEqual(value["nonHex"], "")
        self.assertEqual(value["blank"], "")
        self.assertEqual(value["nullId"], "")
        self.assertEqual(value["revision"], "f" * 64)
        self.assertEqual(value["missing"], "missing")
        self.assertEqual(value["shortRev"], "")
        self.assertEqual(value["emptyRev"], "")
        self.assertEqual(value["nullRev"], "")

    def test_page_block_composition(self):
        fns = self.functions(("todoComposePageBlock",))
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
  bare: root.todoComposePageBlock("fix the backoff", "", ""),
  full: root.todoComposePageBlock("fix the backoff",
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "file:/x/a.py"),
  sessionOnly: root.todoComposePageBlock("fix it", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "")
}}));
"""
        value = json.loads(self.run_node(script))
        self.assertEqual(value["bare"], "- TODO fix the backoff")
        self.assertEqual(value["full"],
                         "- TODO fix the backoff\n"
                         "  quickshell-session:: " + "a" * 32 + "\n"
                         "  quickshell-ref:: file:/x/a.py")
        self.assertEqual(value["sessionOnly"],
                         "- TODO fix it\n"
                         "  quickshell-session:: " + "b" * 32)

    def test_single_line_error(self):
        fns = self.functions(("todoSingleLineError",))
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
  prefixed: root.todoSingleLineError("error: Page changed\\nsecond line", "fallback"),
  bare: root.todoSingleLineError("boom", "fallback"),
  empty: root.todoSingleLineError("", "fallback"),
  capped: root.todoSingleLineError("error: " + "x".repeat(500), "fallback").length
}}));
"""
        value = json.loads(self.run_node(script))
        self.assertEqual(value["prefixed"], "Page changed")
        self.assertEqual(value["bare"], "boom")
        self.assertEqual(value["empty"], "fallback")
        self.assertEqual(value["capped"], 160)

    def test_provenance_prefers_seen_override_then_ambient(self):
        fns = self.functions(("todoSafeRef", "todoSessionIdOf", "todoProvenance"))
        script = f"""
const vm = require("vm");
const root = {{
  todoRefOverride: "",
  ambientBlock: {{
    project: {{id: "p", name: "Demo"}},
    session: {{id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}},
    recent_resources: [{{identity: "file:/x/a.py"}}, {{identity: "file:/x/b.py"}}]
  }}
}};
const context = {{root}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
const ambient = root.todoProvenance();
root.todoRefOverride = "url:https://example.com/a";
const override = root.todoProvenance();
// A gated-out override falls back to the ambient resource, never raw.
root.todoRefOverride = "/home/u/.ssh/id_rsa";
const gated = root.todoProvenance();
root.ambientBlock = null;
root.todoRefOverride = "";
const empty = root.todoProvenance();
console.log(JSON.stringify({{ambient, override, gated, empty}}));
"""
        value = json.loads(self.run_node(script))
        self.assertEqual(value["ambient"],
                         {"session_id": "a" * 32, "ref": "file:/x/a.py"})
        self.assertEqual(value["override"]["ref"], "url:https://example.com/a")
        self.assertEqual(value["override"]["session_id"], "a" * 32)
        self.assertEqual(value["gated"]["ref"], "file:/x/a.py")
        self.assertEqual(value["empty"], {"session_id": "", "ref": ""})


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class TodoStageTests(unittest.TestCase):
    def run_node(self, script):
        completed = subprocess.run(
            ["node", "-e", script], text=True, capture_output=True,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr)
        return completed.stdout.strip()

    def harness(self, extra_root, exercise):
        fns = {n: extract_function(PALETTE_TEXT, n) for n in TODO_FNS}
        root_json = json.dumps(extra_root)
        return f"""
const vm = require("vm");
const launches = [];
const root = Object.assign({{
  requestedOpen: true, mode: "todo", modeQuery: "buy milk",
  todoBusy: false, todoConfirming: false, todoText: "", todoRequestText: "",
  todoTarget: "", todoTargetName: "", todoPreview: "", todoRevision: "",
  todoContent: "", todoPath: "", todoProjectId: "", todoDate: "",
  todoAddition: "", todoSessionId: "", todoRef: "", todoRefOverride: "",
  todoTargetProjectOverride: "", todoTargetProjectNameOverride: "",
  todoGeneration: 0, todoProcessGeneration: 0, todoProcessStage: "",
  todoStdinPayload: null, query: "todo: buy milk",
  ambientBlock: null,
  todoLaunch(stage, argv, payload) {{
    launches.push({{stage, argv, payload}});
    return true;
  }},
  rebuildModel() {{ this.rebuilt = (this.rebuilt || 0) + 1; }},
}}, {root_json});
const todoTimeout = {{stops: 0, restarts: 0,
  stop() {{ this.stops++; }}, restart() {{ this.restarts++; }}}};
const todoProcess = {{running: false, command: null, stdinEnabled: false}};
const todoConfirmButton = {{focused: 0, forceActiveFocus() {{ this.focused++; }}}};
const input = {{text: "", forced: 0, forceActiveFocus() {{ this.forced++; }}}};
const Qt = {{calls: 0, callLater(fn) {{ this.calls++; try {{ fn(); }} catch (ignored) {{}} }}}};
const Quickshell = {{shellPath(v) {{ return "/qs/" + v; }}}};
const context = {{root, launches, todoTimeout, todoProcess,
  todoConfirmButton, input, Qt, Quickshell, notice: ""}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
// Keep the launch boundary stubbed: the real todoLaunch drives the
// palette Process, which has no node equivalent. The stub mirrors its
// observable state (busy + stage) and captures argv for assertions.
root.todoLaunch = function(stage, argv, payload) {{
  launches.push({{stage, argv, payload}});
  this.todoBusy = true;
  this.todoProcessStage = String(stage || "");
  return true;
}};
{exercise}
"""

    def test_begin_launches_project_lookup(self):
        script = self.harness({}, """
root.todoProvenance = function() { return {session_id: "", ref: ""}; };
const ok = root.todoBegin();
console.log(JSON.stringify({ok, launches, busy: root.todoBusy,
  text: root.todoText, notice: context.notice}));
""")
        value = json.loads(self.run_node(script))
        self.assertTrue(value["ok"])
        self.assertEqual(len(value["launches"]), 1)
        self.assertEqual(value["launches"][0]["stage"], "project")
        self.assertEqual(value["launches"][0]["argv"],
                         ["python3", "/qs/scripts/desktop_projects.py",
                          "current-project"])
        self.assertIsNone(value["launches"][0]["payload"])
        self.assertTrue(value["busy"])
        self.assertEqual(value["text"], "buy milk")

    def test_begin_refuses_empty_and_armed_preview(self):
        script = self.harness({"modeQuery": "   "}, """
const empty = root.todoBegin();
root.modeQuery = "buy milk";
root.todoConfirming = true;
const armed = root.todoBegin();
console.log(JSON.stringify({empty, armed, launches,
  notice: context.notice}));
""")
        value = json.loads(self.run_node(script))
        self.assertFalse(value["empty"])
        self.assertFalse(value["armed"])
        self.assertEqual(value["launches"], [])

    def test_project_stage_routes_page_or_journal(self):
        script = self.harness({"todoRequestText": "buy milk"}, """
const pid = "11111111-1111-4111-8111-111111111111";
root.todoProcessGeneration = 1;
root.todoProcessStage = "project";
root.finishTodoStage(0, JSON.stringify({project: {id: pid, name: "Demo"},
  has_logseq_linkage: true}), "", 1, "project");
const pageLaunch = launches.slice();
launches.length = 0;
root.todoProcessGeneration = 2;
root.todoProcessStage = "project";
root.finishTodoStage(0, JSON.stringify({project: null,
  has_logseq_linkage: false}), "", 2, "project");
const journalLaunch = launches.slice();
console.log(JSON.stringify({pageLaunch, journalLaunch,
  projectId: root.todoProjectId, targetName: root.todoTargetName}));
""")
        value = json.loads(self.run_node(script))
        # project_planner page/update with {project_id, revision, content}.
        self.assertEqual(len(value["pageLaunch"]), 1)
        self.assertEqual(value["pageLaunch"][0]["stage"], "page")
        self.assertEqual(value["pageLaunch"][0]["argv"],
                         ["python3", "/qs/scripts/project_planner.py", "page"])
        self.assertEqual(value["pageLaunch"][0]["payload"],
                         {"project_id": "11111111-1111-4111-8111-111111111111"})
        # No current project (or no linked note): journal context/prepare/append.
        self.assertEqual(len(value["journalLaunch"]), 1)
        self.assertEqual(value["journalLaunch"][0]["stage"], "context")
        self.assertEqual(value["journalLaunch"][0]["argv"],
                         ["python3", "/qs/scripts/journal_assistant.py", "context"])
        self.assertEqual(value["journalLaunch"][0]["payload"], {"query": ""})

    def test_page_stage_arms_exact_preview(self):
        revision = "f" * 64
        script = self.harness({"todoRequestText": "buy milk",
                               "todoText": "buy milk",
                               "todoProjectId": "11111111-1111-4111-8111-111111111111"}, """
root.todoProcessGeneration = 3;
root.todoProcessStage = "page";
root.finishTodoStage(0, JSON.stringify({revision: "REV",
  content: "Tasks:\\n", path: "/g/Demo.md"}), "", 3, "page");
console.log(JSON.stringify({target: root.todoTarget,
  preview: root.todoPreview, confirming: root.todoConfirming,
  revision: root.todoRevision, content: root.todoContent,
  focused: todoConfirmButton.focused, rebuilt: root.rebuilt || 0}));
""".replace("REV", revision))
        value = json.loads(self.run_node(script))
        self.assertEqual(value["target"], "page")
        self.assertEqual(value["preview"], "- TODO buy milk")
        self.assertTrue(value["confirming"])
        self.assertEqual(value["revision"], revision)
        self.assertEqual(value["content"], "Tasks:\n- TODO buy milk\n")
        self.assertEqual(value["focused"], 1)

    def test_journal_chain_context_prepare_arms_preview(self):
        script = self.harness({"todoRequestText": "buy milk",
                               "todoText": "buy milk"}, """
root.todoProcessGeneration = 4;
root.todoProcessStage = "context";
root.finishTodoStage(0, JSON.stringify({date: "2026_09_22",
  revision: "missing"}), "", 4, "context");
const prepareLaunch = launches.slice();
launches.length = 0;
root.todoProcessGeneration = 5;
root.todoProcessStage = "prepare";
root.finishTodoStage(0, JSON.stringify({addition: "- TODO buy milk",
  revision: "missing"}), "", 5, "prepare");
console.log(JSON.stringify({prepareLaunch, target: root.todoTarget,
  preview: root.todoPreview, confirming: root.todoConfirming,
  date: root.todoDate}));
""")
        value = json.loads(self.run_node(script))
        self.assertEqual(len(value["prepareLaunch"]), 1)
        self.assertEqual(value["prepareLaunch"][0]["stage"], "prepare")
        self.assertEqual(value["prepareLaunch"][0]["argv"],
                         ["python3", "/qs/scripts/journal_assistant.py", "prepare"])
        self.assertEqual(value["prepareLaunch"][0]["payload"],
                         {"date": "2026_09_22", "revision": "missing",
                          "text": "- TODO buy milk",
                          "session_id": "", "ref": ""})
        self.assertEqual(value["target"], "journal")
        self.assertEqual(value["preview"], "- TODO buy milk")
        self.assertTrue(value["confirming"])
        self.assertEqual(value["date"], "2026_09_22")

    def test_confirm_apply_page_and_journal_argv(self):
        revision = "f" * 64
        script = self.harness(
            {"todoRequestText": "buy milk", "todoConfirming": True,
             "todoTarget": "page", "todoTargetName": "Demo",
             "todoProjectId": "11111111-1111-4111-8111-111111111111",
             "todoRevision": revision,
             "todoContent": "Tasks:\n- TODO buy milk\n"}, """
const pageOk = root.todoConfirmApply();
const pageLaunch = launches.slice();
launches.length = 0;
root.todoBusy = false;
root.todoConfirming = true;
root.todoTarget = "journal";
root.todoTargetName = "journal 2026_09_22";
root.todoDate = "2026_09_22";
root.todoRevision = "missing";
root.todoAddition = "- TODO buy milk";
const journalOk = root.todoConfirmApply();
const journalLaunch = launches.slice();
console.log(JSON.stringify({pageOk, pageLaunch, journalOk, journalLaunch}));
""")
        value = json.loads(self.run_node(script))
        self.assertTrue(value["pageOk"])
        self.assertEqual(value["pageLaunch"][0]["stage"], "apply")
        self.assertEqual(value["pageLaunch"][0]["argv"],
                         ["python3", "/qs/scripts/project_planner.py", "update"])
        self.assertEqual(value["pageLaunch"][0]["payload"],
                         {"project_id": "11111111-1111-4111-8111-111111111111",
                          "revision": revision,
                          "content": "Tasks:\n- TODO buy milk\n"})
        self.assertTrue(value["journalOk"])
        self.assertEqual(value["journalLaunch"][0]["stage"], "apply")
        self.assertEqual(value["journalLaunch"][0]["argv"],
                         ["python3", "/qs/scripts/journal_assistant.py", "append"])
        self.assertEqual(value["journalLaunch"][0]["payload"],
                         {"date": "2026_09_22", "revision": "missing",
                          "addition": "- TODO buy milk",
                          "session_id": "", "ref": ""})

    def test_pinned_project_skips_current_lookup(self):
        # S-045: a seen-pinned project goes straight to the page stage
        # for the pinned id, never the current-project lookup.
        pid = "11111111-1111-4111-8111-111111111111"
        script = self.harness(
            {"todoTargetProjectOverride": pid,
             "todoTargetProjectNameOverride": "Demo"}, """
root.todoProvenance = function() { return {session_id: "", ref: "file:/x/a.py"}; };
const ok = root.todoBegin();
console.log(JSON.stringify({ok, launches, projectId: root.todoProjectId,
  targetName: root.todoTargetName, busy: root.todoBusy}));
""")
        value = json.loads(self.run_node(script))
        self.assertTrue(value["ok"])
        self.assertEqual(len(value["launches"]), 1)
        self.assertEqual(value["launches"][0]["stage"], "page")
        self.assertEqual(value["launches"][0]["argv"],
                         ["python3", "/qs/scripts/project_planner.py", "page"])
        self.assertEqual(value["launches"][0]["payload"], {"project_id": pid})
        self.assertEqual(value["projectId"], pid)
        self.assertEqual(value["targetName"], "Demo")

    def test_project_stage_prefers_pin_over_current(self):
        # S-045: even if the current-project lookup raced the pin, the
        # pinned project wins.
        pid = "11111111-1111-4111-8111-111111111111"
        other = "22222222-2222-4222-8222-222222222222"
        script = self.harness(
            {"todoRequestText": "buy milk",
             "todoTargetProjectOverride": pid,
             "todoTargetProjectNameOverride": "Demo"}, """
root.todoProcessGeneration = 1;
root.todoProcessStage = "project";
root.finishTodoStage(0, JSON.stringify({project: {id: "OTHER", name: "Current"},
  has_logseq_linkage: true}), "", 1, "project");
console.log(JSON.stringify({launches, projectId: root.todoProjectId,
  targetName: root.todoTargetName}));
""".replace("OTHER", other))
        value = json.loads(self.run_node(script))
        self.assertEqual(len(value["launches"]), 1)
        self.assertEqual(value["launches"][0]["stage"], "page")
        self.assertEqual(value["launches"][0]["payload"], {"project_id": pid})
        self.assertEqual(value["projectId"], pid)
        self.assertEqual(value["targetName"], "Demo")

    def test_target_label_prefers_pin_and_reset_clears_it(self):
        # S-045: the composer names the pinned target, and the pin is
        # single-use: apply and reset both burn it.
        fns = {n: extract_function(PALETTE_TEXT, n) for n in
               ("todoTargetLabel", "todoReset")}
        script = f"""
const vm = require("vm");
const root = {{
  todoTargetProjectOverride: "11111111-1111-4111-8111-111111111111",
  todoTargetProjectNameOverride: "Demo",
  ambientBlock: {{project: {{id: "p", name: "Current"}}}},
  todoBusy: true, todoConfirming: true, todoText: "x",
  todoRequestText: "x", todoTarget: "page", todoTargetName: "Demo",
  todoPreview: "p", todoRevision: "r", todoContent: "c", todoPath: "p",
  todoProjectId: "pid", todoDate: "d", todoAddition: "a",
  todoSessionId: "s", todoRef: "ref", todoRefOverride: "ref",
  todoStdinPayload: null, todoProcessStage: "page", todoGeneration: 0
}};
const todoTimeout = {{stop() {{}}}};
const context = {{root, todoTimeout}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
const pinnedLabel = root.todoTargetLabel();
root.todoTargetProjectOverride = "";
root.todoTargetProjectNameOverride = "";
const ambientLabel = root.todoTargetLabel();
root.ambientBlock = null;
const journalLabel = root.todoTargetLabel();
root.todoTargetProjectOverride = "pid";
root.todoTargetProjectNameOverride = "Demo";
root.todoRefOverride = "ref";
root.todoReset();
console.log(JSON.stringify({{pinnedLabel, ambientLabel, journalLabel,
  project: root.todoTargetProjectOverride,
  name: root.todoTargetProjectNameOverride,
  ref: root.todoRefOverride, confirming: root.todoConfirming}}));
"""
        value = json.loads(self.run_node(script))
        self.assertEqual(value["pinnedLabel"], "Demo")
        self.assertEqual(value["ambientLabel"], "Current")
        self.assertEqual(value["journalLabel"], "today's journal")
        self.assertEqual(value["project"], "")
        self.assertEqual(value["name"], "")
        self.assertEqual(value["ref"], "")
        self.assertFalse(value["confirming"])

    def test_apply_targets_pin_and_burns_it(self):
        # S-045: Confirm writes to the pinned project id, then the pin
        # clears single-use with the ref.
        revision = "f" * 64
        pid = "11111111-1111-4111-8111-111111111111"
        script = self.harness(
            {"todoRequestText": "buy milk", "todoConfirming": True,
             "todoTarget": "page", "todoTargetName": "Demo",
             "todoProjectId": pid,
             "todoRevision": revision,
             "todoContent": "Tasks:\n- TODO buy milk\n",
             "todoTargetProjectOverride": pid,
             "todoTargetProjectNameOverride": "Demo",
             "todoRefOverride": "file:/x/a.py"}, """
const ok = root.todoConfirmApply();
const launch = launches.slice();
root.todoBusy = false;
root.todoProcessGeneration = 9;
root.todoProcessStage = "apply";
root.finishTodoStage(0, "{}", "", 9, "apply");
console.log(JSON.stringify({ok, launch, project: root.todoTargetProjectOverride,
  name: root.todoTargetProjectNameOverride, ref: root.todoRefOverride,
  notice: context.notice}));
""")
        value = json.loads(self.run_node(script))
        self.assertTrue(value["ok"])
        self.assertEqual(value["launch"][0]["payload"]["project_id"], pid)
        self.assertEqual(value["project"], "")
        self.assertEqual(value["name"], "")
        self.assertEqual(value["ref"], "")
        self.assertEqual(value["notice"], "TODO added to Demo")

    def test_confirm_rejects_changed_text_and_apply_stays_open(self):
        script = self.harness(
            {"modeQuery": "buy oat milk", "todoRequestText": "buy milk",
             "todoConfirming": True, "todoTarget": "page",
             "todoTargetName": "Demo", "todoProjectId": "p",
             "todoRevision": "r", "todoContent": "c"}, """
const changed = root.todoConfirmApply();
const changedNotice = context.notice;
// Apply success: notice names the target, the palette stays open for
// serial entry, and the single-use seen ref burns.
root.modeQuery = "buy milk";
root.todoRequestText = "buy milk";
root.todoConfirming = true;
root.todoRefOverride = "file:/x/a.py";
root.todoProcessGeneration = 6;
root.todoProcessStage = "apply";
root.finishTodoStage(0, "{}", "", 6, "apply");
console.log(JSON.stringify({changed, launches,
  notice: changedNotice, confirming: root.todoConfirming,
  query: root.query, inputText: input.text,
  refOverride: root.todoRefOverride, applyNotice: context.notice}));
""")
        value = json.loads(self.run_node(script))
        self.assertFalse(value["changed"])
        self.assertEqual(value["launches"], [])
        self.assertEqual(value["notice"], "Text changed; press Enter to prepare again")
        self.assertFalse(value["confirming"])
        self.assertEqual(value["query"], "todo: ")
        self.assertEqual(value["inputText"], "todo: ")
        self.assertEqual(value["refOverride"], "")
        self.assertEqual(value["applyNotice"], "TODO added to Demo")

    def test_apply_revision_conflict_drops_preview(self):
        script = self.harness(
            {"todoRequestText": "buy milk", "todoConfirming": True,
             "todoTarget": "page"}, """
root.todoProcessGeneration = 7;
root.todoProcessStage = "apply";
root.finishTodoStage(1, "{}", "error: revision changed underneath", 7, "apply");
const conflictNotice = context.notice;
const conflictConfirming = root.todoConfirming;
root.todoConfirming = true;
root.todoProcessGeneration = 8;
root.todoProcessStage = "apply";
root.finishTodoStage(1, "{}", "error: boom", 8, "apply");
console.log(JSON.stringify({conflictNotice, conflictConfirming,
  otherNotice: context.notice, otherConfirming: root.todoConfirming}));
""")
        value = json.loads(self.run_node(script))
        self.assertEqual(value["conflictNotice"], "Page changed; press Enter to retry")
        self.assertFalse(value["conflictConfirming"])
        self.assertEqual(value["otherNotice"], "boom")
        self.assertTrue(value["otherConfirming"])

    def test_cancel_and_timeout_never_kill_a_write(self):
        script = self.harness(
            {"todoConfirming": True, "todoBusy": True,
             "todoProcessStage": "apply"}, """
root.todoCancelConfirm("custom");
const cancelNotice = context.notice;
const cancelConfirming = root.todoConfirming;
root.todoBusy = true;
root.todoConfirming = true;
root.todoProcessStage = "apply";
todoProcess.running = true;
root.cancelTodoStage();
const applyBusy = root.todoBusy;
const applyRunning = todoProcess.running;
const applyNotice = context.notice;
root.todoProcessStage = "prepare";
root.cancelTodoStage();
console.log(JSON.stringify({cancelNotice, cancelConfirming, applyBusy,
  applyRunning, applyNotice, prepareBusy: root.todoBusy,
  prepareNotice: context.notice}));
""")
        value = json.loads(self.run_node(script))
        self.assertEqual(value["cancelNotice"], "custom")
        self.assertFalse(value["cancelConfirming"])
        # A write is never killed on a wall-clock timeout: it may already
        # have committed. Only warned.
        self.assertTrue(value["applyBusy"])
        self.assertTrue(value["applyRunning"])
        self.assertIn("will not be cancelled", value["applyNotice"])
        self.assertFalse(value["prepareBusy"])
        self.assertEqual(value["prepareNotice"], "TODO prepare timed out; retry")


class TodoStructuralTests(unittest.TestCase):
    def test_palette_wiring(self):
        text = PALETTE_TEXT
        for name in ("todoSafeRef", "todoSessionIdOf", "todoProvenance",
                     "todoTargetLabel", "todoComposerSubtitle", "todoBegin",
                     "todoLaunch", "todoValidRevision", "todoComposePageBlock",
                     "finishTodoStage", "todoConfirmApply",
                     "todoCancelConfirm", "cancelTodoStage", "todoReset",
                     "todoSingleLineError", "seenAddTodoHere",
                     "selectedSeenEntry", "seenOpenTarget"):
            self.assertIn("function " + name + "(", text)
        self.assertIn("property string todoTargetProjectOverride", text)
        self.assertIn("property string todoTargetProjectNameOverride", text)
        self.assertIn('objectName: "todoConfirmButton"', text)
        self.assertIn('objectName: "todoCancelButton"', text)
        self.assertIn('"todo: Type a TODO · Enter previews, Confirm writes"', text)
        self.assertIn('"Add TODO: "', text)
        self.assertIn('"TODO added to "', text)
        self.assertIn('"Page changed; press Enter to retry"', text)
        self.assertIn('"Text changed; press Enter to prepare again"', text)
        self.assertIn('"TODO prepare timed out; retry"', text)
        # Exact-preview discipline: user-authored text renders PlainText,
        # only the explicit Confirm button writes, and the palette stays
        # open after apply for serial entry.
        self.assertIn("textFormat: Text.PlainText", text)
        self.assertIn('"Write this TODO (palette stays open)"', text)
        self.assertIn('root.query = "todo: "', text)
        # Stage argv: project_planner page/update and journal_assistant
        # context/prepare/append, revision-checked on both paths.
        self.assertIn('"scripts/project_planner.py"', text)
        self.assertIn('"update"', text)
        self.assertIn('"scripts/journal_assistant.py"', text)
        self.assertIn('"context"', text)
        self.assertIn('"prepare"', text)
        self.assertIn('"append"', text)

    def test_no_model_or_auto_send_surface(self):
        # todo: is a deterministic write ladder: it must never prompt the
        # agent, auto-send, or bypass the Confirm button. Enter starts
        # prepare; only todoConfirmApply writes.
        for line in PALETTE_TEXT.splitlines():
            if "todo" not in line.lower():
                continue
            self.assertNotIn("agent.prompt", line)


if __name__ == "__main__":
    unittest.main()
