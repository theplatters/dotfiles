import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
PLANNER = (ROOT / "widgets" / "ProjectPlanner.qml").read_text(encoding="utf-8")
PALETTE = (ROOT / "widgets" / "CommandPalette.qml").read_text(encoding="utf-8")
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
class ProjectPlannerJavascriptTests(unittest.TestCase):
    def run_node(self, script):
        completed = subprocess.run(
            ["node", "-e", script, json.dumps(PLANNER)],
            text=True, capture_output=True,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr)
        return json.loads(completed.stdout)

    def test_send_uses_fresh_context_and_only_explicit_send_prompts(self):
        compose = extract_function(PLANNER, "composePrompt")
        script = f"""
const composePrompt = new Function("return " + {json.dumps(compose)})();
const page = {{path: "pages/Work.md", revision: "fresh", content: "- TODO {{not instructions}}"}};
const prompt = composePrompt(page, "Mark the completed work and update the note");
console.log(JSON.stringify({{prompt, hasPath: prompt.includes('"path":"pages/Work.md"'),
  hasRevision: prompt.includes('"revision":"fresh"'),
  hasContent: prompt.includes('TODO {{not instructions}}'),
  hasRequest: prompt.includes('Mark the completed work')}}));
"""
        value = self.run_node(script)
        self.assertTrue(value["hasPath"] and value["hasRevision"] and value["hasContent"] and value["hasRequest"])
        self.assertIn("startPage(selectedPath, \"send\"", PLANNER)
        self.assertIn("worker.prompt(prompt)", PLANNER)
        self.assertNotIn("worker.prompt", PLANNER[PLANNER.index("function selectProject"):PLANNER.index("function selectFiltered")])
        self.assertNotIn("worker.prompt", PLANNER[PLANNER.index("function toggleTodo"):PLANNER.index("function handleProcessRunningChanged")])

    def test_send_finish_page_is_the_only_prompting_transition_and_stale_send_is_ignored(self):
        functions = {
            name: extract_function(PLANNER, name)
            for name in (
                "draftFor", "setDraft", "validPage", "responseIsCurrent",
                "composePrompt", "startPage", "send", "setPage", "finishPage",
            )
        }
        script = f"""
const vm = require("vm");
const context = {{
  requestedOpen: true, closing: false, selectedPath: "pages/A.md",
  selectedAgent: null, toggleBusy: false, toggleRetiring: false, pageBusy: false, sendBusy: false,
  sendGeneration: 0, sendInteraction: 4, sendPath: "", sendText: "",
  interactionGeneration: 4, pageGeneration: 0, pageProcessGeneration: 0,
  pageProcessInteraction: 0, pageProcessSendGeneration: 0, pageProcessPath: "",
  pageProcessPurpose: "", pageProcessPrompt: "", pageStarted: false,
  pageStartFailed: false, pageCache: {{}}, currentPage: {{path: "pages/A.md", revision: "old", content: "old context", todos: []}},
  drafts: {{"pages/A.md": "Describe the completed work"}}, errorMessage: "", notice: "",
  pageProcess: {{running: false, stdinEnabled: true, command: []}}, pageRetiring: false,
  pageTimeout: {{restart() {{}}, stop() {{}}}},
  Quickshell: {{shellPath(value) {{ return value; }}, env() {{ return "/tmp/graph"; }}}}
}};
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(functions.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
context.approvalOpen = () => false;
context.hasBusyAgent = () => false;
context.selectedAgent = {{ready: true, busy: false, compacting: false, stopping: false,
  controlPending: false, sessionSwitching: false, status: "Ready", prompts: [],
  prompt(value) {{ this.prompts.push(value); return true; }}}};
if (!context.send()) throw new Error("send did not start");
if (context.selectedAgent.prompts.length !== 0 || context.pageProcessPurpose !== "send")
  throw new Error("send prompted before PAGE completed");
const firstGeneration = context.pageProcessGeneration;
const firstInteraction = context.pageProcessInteraction;
context.finishPage(0, JSON.stringify({{path: "pages/A.md", revision: "fresh", content: "- TODO fresh page", todos: []}}), "",
  firstGeneration, firstInteraction, "pages/A.md", "send", "Describe the completed work");
if (context.selectedAgent.prompts.length !== 1 || !context.selectedAgent.prompts[0].includes("fresh page"))
  throw new Error("fresh page was not sent after PAGE");
if (context.drafts["pages/A.md"] !== "") throw new Error("draft was not consumed");

context.drafts["pages/A.md"] = "second request";
context.send();
const staleGeneration = context.pageProcessGeneration;
const staleInteraction = context.pageProcessInteraction;
context.interactionGeneration = 5; // close/switch race wins over the old read
context.finishPage(0, JSON.stringify({{path: "pages/A.md", revision: "stale", content: "must not apply", todos: []}}), "",
  staleGeneration, staleInteraction, "pages/A.md", "send", "second request");
console.log(JSON.stringify({{prompts: context.selectedAgent.prompts.length,
  content: context.currentPage.content, sendBusy: context.sendBusy}}));
"""
        self.assertEqual(self.run_node(script), {
            "prompts": 1, "content": "- TODO fresh page", "sendBusy": False,
        })

    def test_conversation_plan_keeps_a_stable_live_row_until_history_arrives(self):
        functions = {
            name: extract_function(PLANNER, name)
            for name in ("conversationWantsLive", "conversationPlan",
                          "conversationBoundary", "clampContentY")
        }
        script = f"""
const wantsLive = new Function("return " + {json.dumps(functions["conversationWantsLive"])})();
const plan = new Function("conversationWantsLive",
  "return (" + {json.dumps(functions["conversationPlan"])} + ")")(wantsLive);
const boundaryOf = new Function("return " + {json.dumps(functions["conversationBoundary"])})();
const clampY = new Function("return " + {json.dumps(functions["clampContentY"])})();
const results = {{}};
const streaming = [{{role: "user", text: "hi"}}];
results.streaming = plan(streaming, "streaming reply", 1);
results.streamingKeys = results.streaming.map(row => row.key);
const before = JSON.stringify(streaming);
results.sourceUntouched = JSON.stringify(streaming) === before && streaming.length === 1;
// Exact echo beyond the boundary: no extra live row.
results.deduped = plan([{{role: "user", text: "hi"}}, {{role: "assistant", text: "same text"}}],
  "same text", 1).length;
// Settled gap: agent finished but async history has not arrived yet.
results.settledGap = wantsLive([{{role: "user", text: "hi"}}], "done reply", 1);
// History arrived containing the answer: the live row retires.
const arrived = [{{role: "user", text: "hi"}}, {{role: "assistant", text: "prefix done reply suffix"}}];
results.historyArrived = wantsLive(arrived, "done reply", 1);
// Failure with stale history: the completed answer is retained like the old
// standalone answer instead of vanishing.
results.failure = wantsLive([{{role: "user", text: "hi"}}], "failed reply", 1);
// New send / session change clears the answer at the bridge.
results.cleared = wantsLive([{{role: "user", text: "hi"}}], "", 1);
results.finishing = wantsLive([], "finishing reply", 0);
// Repeated response: old assistant "Yes completed", new live "Yes" while
// history still ends with the old turn. Boundary 2 admits no old row.
const oldTurn = [{{role: "user", text: "old q"}}, {{role: "assistant", text: "Yes completed"}}];
results.repeatStreaming = wantsLive(oldTurn, "Yes", 2);
results.repeatSettled = wantsLive(oldTurn, "Yes", 2);
// Repeated identical question and answer, still stale: shown until the
// fetch arrives (old behavior), never hidden by the previous turn.
const sameTurn = [{{role: "user", text: "same q"}}, {{role: "assistant", text: "same answer"}}];
results.repeatIdentical = wantsLive(sameTurn, "same answer", 2);
// Escaped/multiline user text is irrelevant: no user-text matching exists.
const tricky = [{{role: "user", text: 'price (USD) $5 [sale]?\\n"quoted"'}},
  {{role: "user", text: "old q"}}, {{role: "assistant", text: "Yes completed"}}];
results.trickyUserText = wantsLive(tricky, "Yes", 3);
// Once the fetch arrives with the new turn, its closing assistant retires it.
const newTurn = oldTurn.concat([{{role: "user", text: "new question"}},
  {{role: "assistant", text: "Yes, booked"}}]);
results.repeatArrived = wantsLive(newTurn, "Yes, booked", 2);
// Boundary beyond a short (stale) snapshot: nothing beyond it, row is kept.
results.shortSnapshot = wantsLive(oldTurn, "Yes", 10);
// No locally tracked turn (restored worker): trailing-assistant fallback.
results.fallbackHit = wantsLive(arrived, "done reply", -1);
results.fallbackUser = wantsLive([{{role: "user", text: "hi"}}], "done reply", -1);
// Boundary helper: per-path record, session/generation invalidation.
const worker = {{sessionFile: "s", messagesGeneration: 7}};
const turns = {{"p": {{count: 2, sessionFile: "s", generation: 7}}}};
globalThis._convTurns = turns;
results.boundaryHit = boundaryOf(worker, "p");
results.boundaryOtherPath = boundaryOf(worker, "q");
results.boundarySession = boundaryOf({{sessionFile: "other", messagesGeneration: 7}}, "p");
results.boundaryGeneration = boundaryOf({{sessionFile: "s", messagesGeneration: 8}}, "p");
results.boundaryNoWorker = boundaryOf(null, "p");
results.clamped = [clampY(-10, 1000, 200), clampY(5000, 1000, 200),
  clampY(300, 1000, 200), clampY(50, 100, 200)];
console.log(JSON.stringify(results));
"""
        value = self.run_node(script)
        self.assertEqual(value["streamingKeys"], ["m0", "live"])
        self.assertEqual(value["streaming"][1], {
            "key": "live", "role": "assistant", "text": "streaming reply", "pending": True,
        })
        self.assertTrue(value["sourceUntouched"])
        self.assertEqual(value["deduped"], 2)
        self.assertTrue(value["settledGap"])
        self.assertFalse(value["historyArrived"])
        self.assertTrue(value["failure"])
        self.assertFalse(value["cleared"])
        self.assertTrue(value["finishing"])
        self.assertTrue(value["repeatStreaming"])
        self.assertTrue(value["repeatSettled"])
        self.assertTrue(value["repeatIdentical"])
        self.assertTrue(value["trickyUserText"])
        self.assertFalse(value["repeatArrived"])
        self.assertTrue(value["shortSnapshot"])
        self.assertFalse(value["fallbackHit"])
        self.assertTrue(value["fallbackUser"])
        self.assertEqual(value["boundaryHit"], 2)
        self.assertEqual(value["boundaryOtherPath"], -1)
        self.assertEqual(value["boundarySession"], -1)
        self.assertEqual(value["boundaryGeneration"], -1)
        self.assertEqual(value["boundaryNoWorker"], -1)
        self.assertEqual(value["clamped"], [0, 800, 300, 0])

    def test_timeout_retirement_blocks_cross_project_send_and_late_exit(self):
        functions = {
            name: extract_function(PLANNER, name)
            for name in (
                "draftFor", "selectProject", "cancelPageRead", "startPage", "send", "validPage",
                "responseIsCurrent", "setPage", "finishPage",
            )
        }
        script = f"""
const vm = require("vm");
const context = {{
  requestedOpen: true, closing: false, selectedPath: "pages/A.md",
  selectedAgent: null, toggleBusy: false, toggleRetiring: false, pageBusy: true, sendBusy: true,
  sendGeneration: 1, sendInteraction: 4, sendPath: "pages/A.md", sendText: "A",
  interactionGeneration: 4, pageGeneration: 1, pageProcessGeneration: 1,
  pageProcessInteraction: 4, pageProcessSendGeneration: 1,
  pageProcessPath: "pages/A.md", pageProcessPurpose: "send", pageProcessPrompt: "A",
  pageLaunchGeneration: 1, pageLaunchInteraction: 4, pageLaunchSendGeneration: 1,
  pageLaunchPath: "pages/A.md", pageLaunchPurpose: "send", pageLaunchPrompt: "A",
  pageRetiring: false, pageStarted: true, pageStartFailed: false,
  pageCache: {{}}, currentPage: {{path: "pages/A.md", revision: "old", content: "old", todos: []}},
  drafts: {{"pages/B.md": "B request"}}, errorMessage: "", notice: "",
  pageProcess: {{running: true, stdinEnabled: true, command: []}}, pageRetiring: false,
  pageTimeout: {{restart() {{}}, stop() {{}}}},
  Quickshell: {{shellPath(value) {{ return value; }}, env() {{ return "/tmp/graph"; }}}}
}};
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(functions.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
context.approvalOpen = () => false;
context.hasBusyAgent = () => false;
context.failure = () => "failure";
context.safeText = value => String(value);
const agentA = {{ready: true, busy: false, prompts: [], prompt(value) {{ this.prompts.push(value); return true; }}}};
const agentB = {{ready: true, busy: false, prompts: [], prompt(value) {{ this.prompts.push(value); return true; }}}};
context.selectedAgent = agentA;
context.agentFor = () => agentB;
if (context.selectProject({{path: "pages/B.md"}})) throw new Error("switched during retirement");
context.startPage = context.startPage;
context.cancelPageRead();
if (!context.pageRetiring || context.pageBusy || context.sendBusy)
  throw new Error("timeout did not retire the old read");
if (context.selectProject({{path: "pages/B.md"}}) || context.selectedPath !== "pages/A.md")
  throw new Error("selection was not blocked while the old process retired");
context.selectedPath = "pages/B.md";
context.selectedAgent = agentB;
context.send(); // old Process is still retiring: no B prompt and no metadata overwrite
context.finishPage(0, JSON.stringify({{path: "pages/A.md", revision: "late", content: "A late", todos: []}}), "",
  1, 4, "pages/A.md", "send", "A");
console.log(JSON.stringify({{aPrompts: agentA.prompts.length, bPrompts: agentB.prompts.length,
  selectedPath: context.selectedPath, pageGeneration: context.pageGeneration,
  pageRetiring: context.pageRetiring, sendBusy: context.sendBusy}}));
"""
        self.assertEqual(self.run_node(script), {
            "aPrompts": 0, "bPrompts": 0, "selectedPath": "pages/B.md",
            "pageGeneration": 2, "pageRetiring": True, "sendBusy": False,
        })
        self.assertIn("pageProcess.running || pageRetiring", PLANNER)
        self.assertIn("listProcess.running || listRetiring", PLANNER)
        self.assertIn('String(data.path) !== String(path)', PLANNER)
        self.assertIn("let path = expectedPath || togglePath", PLANNER)

    def test_failed_to_start_callbacks_release_retirement_and_allow_retry(self):
        functions = {
            name: extract_function(PLANNER, name)
            for name in (
                "handleProcessRunningChanged", "handleListStartFailure",
                "handlePageStartFailure", "handleToggleStartFailure",
                "startList", "startPage", "toggleTodo",
            )
        }
        script = f"""
const vm = require("vm");
const texts = {json.dumps(functions)};
function load(overrides) {{
  const context = Object.assign({{
    listProcess: {{running: false, command: []}}, pageProcess: {{running: false, command: []}},
    toggleProcess: {{running: false, command: []}}, listTimeout: {{stop() {{}}, restart() {{}}}},
    pageTimeout: {{stop() {{}}, restart() {{}}}}, toggleWarningTimer: {{stop() {{}}, restart() {{}}}},
    failure() {{ return "failure"; }}, Quickshell: {{shellPath(value) {{ return value; }}, env() {{ return "/tmp/graph"; }}}},
    listBusy: false, listStarted: false, listStartFailed: false, listRetiring: false,
    listGeneration: 0, listProcessGeneration: 0, listLaunchGeneration: 0,
    pageBusy: false, pageStarted: false, pageStartFailed: false, pageRetiring: false,
    pageGeneration: 0, pageProcessGeneration: 0, pageProcessInteraction: 0,
    pageProcessSendGeneration: 0, pageProcessPath: "", pageProcessPurpose: "",
    pageProcessPrompt: "", pageLaunchGeneration: 0, pageLaunchInteraction: 0,
    pageLaunchSendGeneration: 0, pageLaunchPath: "", pageLaunchPurpose: "",
    pageLaunchPrompt: "", toggleBusy: false, toggleStarted: false,
    toggleStartFailed: false, toggleRetiring: false, toggleGeneration: 0,
    toggleProcessGeneration: 0, toggleInteraction: 0, togglePath: "",
    toggleRevision: "", toggleLine: 0, toggleDone: false, toggleLaunchGeneration: 0,
    toggleLaunchInteraction: 0, toggleLaunchPath: "", toggleLaunchRevision: "",
    toggleLaunchLine: 0, toggleLaunchDone: false, toggleTimedOut: false,
    selectedPath: "pages/A.md", currentPage: {{revision: "rev"}}, requestedOpen: true, sendBusy: false,
    sendGeneration: 0, interactionGeneration: 1, pageCache: {{}}, drafts: {{}},
    approvalOpen() {{ return false; }}, hasBusyAgent() {{ return false; }}
  }}, overrides);
  vm.createContext(context);
  for (const [name, text] of Object.entries(texts))
    context[name] = vm.runInContext("(" + text + ")", context);
  return context;
}}
const list = load({{listBusy: true}});
list.handleProcessRunningChanged("list");
if (list.listRetiring || list.listBusy || !list.startList()) throw new Error("list retry stayed blocked");

const page = load({{pageBusy: true, pageGeneration: 7, pageProcessGeneration: 7,
  pageProcessInteraction: 1, pageProcessPurpose: "select"}});
page.handleProcessRunningChanged("page");
if (page.pageRetiring || page.pageBusy || !page.startPage("pages/A.md", "select", ""))
  throw new Error("page retry stayed blocked");

const toggle = load({{toggleBusy: true, toggleGeneration: 3, toggleProcessGeneration: 3,
  toggleInteraction: 1}});
toggle.handleProcessRunningChanged("toggle");
if (toggle.toggleRetiring || toggle.toggleBusy || !toggle.toggleTodo({{line: 1, done: false}}))
  throw new Error("toggle retry stayed blocked");
console.log(JSON.stringify({{list: list.listBusy, page: page.pageBusy, toggle: toggle.toggleBusy}}));
"""
        self.assertEqual(self.run_node(script), {"list": True, "page": True, "toggle": True})

    def test_navigation_and_checkoff_paths_do_not_prompt(self):
        self.assertNotIn("prompt(", PLANNER[PLANNER.index("function selectProject"):PLANNER.index("function selectFiltered")])
        self.assertNotIn("prompt(", PLANNER[PLANNER.index("function toggleTodo"):PLANNER.index("function handleProcessRunningChanged")])
        self.assertNotIn("prompt(", PLANNER[PLANNER.index("function reloadSelected"):PLANNER.index("function startList")])
        navigation = extract_function(PLANNER, "projectIndexAfter")
        script = f"""
const projectIndexAfter = new Function("return " + {json.dumps(navigation)})();
let index = -1;
const seen = [];
for (let i = 0; i < 5; i++) {{ index = projectIndexAfter(index, 1, 4); seen.push(index); }}
index = projectIndexAfter(index, -1, 4); seen.push(index);
console.log(JSON.stringify(seen));
"""
        self.assertEqual(self.run_node(script), [0, 1, 2, 3, 3, 2])
        self.assertIn("projectList.currentIndex = next", PLANNER)
        self.assertIn("item.forceActiveFocus()", PLANNER)
        self.assertIn("projectList.positionViewAtIndex(next, ListView.Contain)", PLANNER)

    def test_actual_selection_checkoff_and_reload_flow_never_prompts(self):
        functions = {
            name: extract_function(PLANNER, name)
            for name in (
                "filteredProjects", "pageFor", "selectProject", "startPage",
                "toggleTodo", "validPage", "setPage", "finishToggle",
                "reloadSelected", "responseIsCurrent", "finishPage",
            )
        }
        script = f"""
const vm = require("vm");
const context = {{
  requestedOpen: true, closing: false, projects: [{{path: "pages/A.md", page: "A"}}],
  projectFilter: "", projectList: {{currentIndex: -1, positionViewAtIndex() {{}}}},
  selectedPath: "", selectedProject: null, selectedAgent: null, currentPage: null,
  pageCache: {{}}, agentCache: {{}}, interactionGeneration: 1,
  pageBusy: false, toggleBusy: false, toggleRetiring: false, sendBusy: false, pageGeneration: 0,
  pageProcessGeneration: 0, pageProcessInteraction: 0, pageProcessSendGeneration: 0,
  pageProcessPath: "", pageProcessPurpose: "", pageProcessPrompt: "",
  pageStarted: false, pageStartFailed: false, toggleGeneration: 0,
  toggleProcessGeneration: 0, toggleInteraction: 0, togglePath: "",
  toggleRevision: "rev", toggleLine: 0, toggleDone: false, togglePath: "pages/A.md", toggleTimedOut: false,
  staleToggle: false, errorMessage: "", notice: "", sendGeneration: 0,
  pageProcess: {{running: false, stdinEnabled: true, command: []}}, pageRetiring: false,
  toggleProcess: {{running: false, stdinEnabled: true, command: []}},
  pageTimeout: {{restart() {{}}, stop() {{}}}},
  toggleWarningTimer: {{restart() {{}}, stop() {{}}}},
  ListView: {{Contain: 0}},
  Quickshell: {{shellPath(value) {{ return value; }}, env() {{ return "/tmp/graph"; }}}}
}};
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(functions.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
const worker = {{prompts: [], prompt(value) {{ this.prompts.push(value); return true; }}}};
context.agentFor = () => worker;
context.pauseIdleAgents = () => {{}};
context.blockedReason = () => "";
context.approvalOpen = () => false;
context.hasBusyAgent = () => false;
context.failure = () => "failure";
context.safeText = value => String(value);
context.selectProject(context.projects[0]);
const page = {{path: "pages/A.md", revision: "rev", content: "- TODO work", todos: [{{line: 1, task: "work", marker: "TODO", done: false}}]}};
context.finishPage(0, JSON.stringify(page), "", context.pageProcessGeneration,
  context.pageProcessInteraction, "pages/A.md", "select", "");
context.currentPage = page;
context.pageBusy = false;
context.toggleTodo(page.todos[0]);
context.finishToggle(0, JSON.stringify({{path: "pages/A.md", revision: "done", content: "- DONE work", todos: [{{line: 1, task: "work", marker: "DONE", done: true}}]}}), "",
  context.toggleProcessGeneration, context.toggleInteraction);
context.reloadSelected();
context.finishPage(0, JSON.stringify(page), "", context.pageProcessGeneration,
  context.pageProcessInteraction, "pages/A.md", "reload", "");
console.log(JSON.stringify({{prompts: worker.prompts.length, done: context.currentPage.todos[0].done}}));
"""
        self.assertEqual(self.run_node(script), {"prompts": 0, "done": False})

    def test_failed_agent_requires_retry_but_paused_agent_can_resume(self):
        retry = extract_function(PLANNER, "retryAgent")
        script = f"""
const retryAgent = new Function("return " + {json.dumps(retry)})();
let agentError = "", notice = "";
let selectedAgent = {{retryable: true, starts: 0, start() {{ this.starts++; return true; }}}};
console.log(JSON.stringify([retryAgent(), selectedAgent.starts]));
"""
        self.assertEqual(self.run_node(script), [True, 1])
        self.assertIn("if (existing.idleStopped || existing.idleStopping)", PLANNER)

    def test_project_context_and_drafts_are_isolated(self):
        draft = extract_function(PLANNER, "draftFor")
        set_draft = extract_function(PLANNER, "setDraft")
        script = f"""
let drafts = {{}};
const draftFor = new Function("return " + {json.dumps(draft)})();
const setDraft = new Function("return " + {json.dumps(set_draft)})();
setDraft("pages/A.md", "A work");
setDraft("pages/B.md", "B work");
console.log(JSON.stringify([draftFor("pages/A.md"), draftFor("pages/B.md"), draftFor("pages/C.md")]));
"""
        self.assertEqual(self.run_node(script), ["A work", "B work", ""])
        self.assertIn("projectAgentComponent.createObject(root, { projectPath: path })", PLANNER)
        self.assertIn("worker.start()", PLANNER)
        self.assertIn("property var agentCache: ({})", PLANNER)

    def test_late_page_and_toggle_replies_are_ignored(self):
        current = extract_function(PLANNER, "responseIsCurrent")
        script = f"""
const responseIsCurrent = new Function("return " + {json.dumps(current)})();
console.log(JSON.stringify([
  responseIsCurrent(2, 2, 4, 4, "pages/A.md", "pages/A.md"),
  responseIsCurrent(2, 3, 4, 4, "pages/A.md", "pages/A.md"),
  responseIsCurrent(2, 2, 4, 5, "pages/A.md", "pages/A.md"),
  responseIsCurrent(2, 2, 4, 4, "pages/A.md", "pages/B.md")
]));
"""
        self.assertEqual(self.run_node(script), [True, False, False, False])
        self.assertIn("if (generation !== pageGeneration) return", PLANNER)
        self.assertIn("if (generation !== toggleGeneration) return", PLANNER)
        self.assertIn("staleToggle = stale", PLANNER)
        self.assertIn('text: "Reload page"', PLANNER)

    def test_approval_uses_request_ids_for_all_inline_ui_types(self):
        approval = PLANNER[PLANNER.index("function reject()"):PLANNER.index("Keys.onPressed", PLANNER.index("function reject()"))]
        self.assertIn("worker.respond(current.id, ({ cancelled: true }))", approval)
        self.assertIn("worker.respond(current.id, fields)", approval)
        for method in ("confirm", "select", "input", "editor"):
            self.assertIn(method, PLANNER)
        self.assertIn("acceptedButtons: Qt.AllButtons", PLANNER)
        self.assertIn("FocusScope", PLANNER)
        self.assertIn("textFormat: TextEdit.PlainText", PLANNER)
        self.assertIn("selectByMouse: true", PLANNER)
        self.assertIn("function boundedText(value, limit)", PLANNER)
        self.assertIn("function titleFor(value)", PLANNER)

    def test_toggle_is_authoritative_and_agent_finish_refreshes(self):
        self.assertIn("root.writeJson(toggleProcess", PLANNER)
        self.assertIn("The response is authoritative", PLANNER)
        self.assertIn("root.refreshAfterAgent(root.selectedAgent)", PLANNER)
        self.assertIn("property bool toggleBusy: false", PLANNER)

    def test_toggle_flow_persists_the_full_helper_response(self):
        functions = {
            name: extract_function(PLANNER, name)
            for name in ("validPage", "setPage", "finishToggle")
        }
        script = f"""
let toggleGeneration = 4, toggleInteraction = 0, toggleBusy = true, currentPage = {{revision: "old"}};
let selectedPath = "pages/Work.md", togglePath = "pages/Work.md", pageCache = {{}}, staleToggle = false;
let errorMessage = "", notice = "", toggleTimedOut = false;
let toggleWarningTimer = {{stop() {{}}}};
const root = {{toggleTimedOut: false}};
const values = {json.dumps(list(functions.values()))};
for (const value of values) globalThis[value.match(/function (\\w+)/)[1]] = new Function("return " + value)();
finishToggle(0, JSON.stringify({{path: "pages/Work.md", revision: "new", content: "- DONE shipped\\n", todos: [{{line: 1, task: "shipped", marker: "DONE", done: true}}]}}), "", 4, 0);
console.log(JSON.stringify({{busy: toggleBusy, revision: currentPage.revision,
  done: currentPage.todos[0].done, cache: pageCache["pages/Work.md"].content}}));
"""
        self.assertEqual(self.run_node(script), {
            "busy": False, "revision": "new", "done": True, "cache": "- DONE shipped\n",
        })

    def test_session_controls_route_only_to_the_selected_worker_without_prompting(self):
        functions = {
            name: extract_function(PLANNER, name)
            for name in ("sessionControlBlockedReason", "sessionControlBlocked", "sessionControlsEnabled", "newSession",
                         "renameSession", "restoreSession", "boundedSessionName")
        }
        script = f"""
const vm = require("vm");
const calls = [];
function worker(name) {{
  return {{name, ready: true, busy: false, compacting: false, stopping: false,
    controlPending: false, sessionSwitching: false, status: "Ready", sessionName: "",
    newSession() {{ calls.push([name, "new"]); return true; }},
    rename(value) {{ calls.push([name, "rename", value]); return true; }},
    switchSession() {{ calls.push([name, "restore"]); return true; }},
    prompt() {{ throw new Error("session controls must not prompt"); }} }};
}}
const a = worker("A"), b = worker("B");
const context = {{selectedPath: "pages/A.md", selectedAgent: a, pageBusy: false,
  pageRetiring: false, sendBusy: false, toggleBusy: false, toggleRetiring: false,
  sessionNameLimit: 120, notice: "", approvalOpen: () => false}};
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(functions.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
if (!context.sessionControlsEnabled()) throw new Error("empty worker was incorrectly gated");
if (!context.newSession() || !context.renameSession("  A name  ") || !context.restoreSession())
  throw new Error("selected worker rejected a control");
context.selectedAgent = b;
if (!context.renameSession("B name")) throw new Error("second worker was not routed");
console.log(JSON.stringify({{calls, a: a.name, b: b.name, notice: context.notice}}));
"""
        self.assertEqual(self.run_node(script), {
            "calls": [["A", "new"], ["A", "rename", "A name"],
                      ["A", "restore"], ["B", "rename", "B name"]],
            "a": "A", "b": "B", "notice": "Renaming session…",
        })

    def test_session_controls_are_gated_by_page_and_worker_transitions(self):
        functions = {
            name: extract_function(PLANNER, name)
            for name in ("sessionControlBlockedReason", "sessionControlBlocked",
                         "sessionControlsEnabled", "newSession")
        }
        script = f"""
const vm = require("vm");
const worker = {{ready: true, busy: false, compacting: false, stopping: false,
  controlPending: false, sessionSwitching: false, status: "Ready", calls: 0,
  newSession() {{ this.calls++; return true; }}}};
const context = {{selectedPath: "pages/A.md", selectedAgent: worker,
  pageBusy: false, pageRetiring: false, sendBusy: false, toggleBusy: false,
  toggleRetiring: false, notice: "", approval: false,
  approvalOpen() {{ return context.approval; }}}};
vm.createContext(context);
for (const value of {json.dumps(list(functions.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
const checks = ["pageBusy", "sendBusy", "toggleBusy", "approval",
  "busy", "compacting", "stopping", "controlPending", "sessionSwitching",
  "sessionRefreshPending"];
for (const key of checks) {{
  context.pageBusy = context.sendBusy = context.toggleBusy = false;
  context.approval = false;
  worker.busy = worker.compacting = worker.stopping = worker.controlPending = worker.sessionSwitching = worker.sessionRefreshPending = false;
  if (["pageBusy", "sendBusy", "toggleBusy"].includes(key)) context[key] = true;
  else if (key === "approval") context.approval = true;
  else worker[key] = true;
  if (context.sessionControlsEnabled() || context.newSession())
    throw new Error("control was allowed during " + key);
}}
console.log(JSON.stringify(worker.calls));
"""
        self.assertEqual(self.run_node(script), 0)

    def test_session_names_refresh_and_rename_cancel_preserves_worker_state(self):
        functions = {
            name: extract_function(PLANNER, name)
            for name in ("sessionLabel", "sessionSaveStatus", "boundedSessionName", "cancelRename")
        }
        script = f"""
const vm = require("vm");
const worker = {{sessionName: "", messages: [], messagesAwaitingSessionState: true,
  sessionRefreshPending: true}};
const context = {{selectedAgent: null, sessionNameLimit: 8,
  renameDialogOpen: true, renameDraft: "typed but cancelled"}};
vm.createContext(context);
for (const value of {json.dumps(list(functions.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
const labels = [context.sessionLabel()];
context.selectedAgent = worker;
labels.push(context.sessionLabel());
const loading = context.sessionSaveStatus();
worker.sessionName = "Restored";
worker.messagesAwaitingSessionState = false;
worker.sessionRefreshPending = false;
labels.push(context.sessionLabel());
const empty = context.sessionSaveStatus();
worker.messages = [{{role: "user", text: "saved"}}];
const saved = context.sessionSaveStatus();
if (context.boundedSessionName("  a\\nvery long name  ") !== "a very l")
  throw new Error("name was not bounded safely");
context.cancelRename();
    console.log(JSON.stringify({{labels, loading, empty, saved,
      open: context.renameDialogOpen, draft: context.renameDraft, name: worker.sessionName}}));
"""
        self.assertEqual(self.run_node(script), {
            "labels": ["New session", "Unnamed session", "Restored"],
            "loading": "Loading session history…",
            "empty": "Empty session — retained while this shell runs; not saved to Restore yet.",
            "saved": "Saved session",
            "open": False, "draft": "", "name": "Restored",
        })

    def test_failed_restore_refresh_recovers_the_previous_history(self):
        functions = {
            name: extract_function(PLANNER, name)
            for name in ("recoverRestoreHistory", "finishRestoreState")
        }
        script = f"""
const vm = require("vm");
const worker = {{sessionFile: "old", messages: [], messagesSessionFile: "old",
  messagesAwaitingSessionState: true, messagesGeneration: 3}};
const context = {{restoreHistoryBackup: {{worker, sessionFile: "old", messages: [
  {{role: "user", text: "keep this"}}]}}, restoreStateSeen: true,
  restoreStateSessionFile: "old"}};
vm.createContext(context);
for (const value of {json.dumps(list(functions.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
if (!context.recoverRestoreHistory(worker) || context.restoreHistoryBackup !== null)
  throw new Error("restore failure was not recovered");
console.log(JSON.stringify({{messages: worker.messages, session: worker.messagesSessionFile,
  awaiting: worker.messagesAwaitingSessionState, generation: worker.messagesGeneration}}));
"""
        self.assertEqual(self.run_node(script), {
            "messages": [{"role": "user", "text": "keep this"}],
            "session": "old", "awaiting": False, "generation": 4,
        })

    def test_cancelled_restore_keeps_the_unchanged_current_session(self):
        functions = {
            name: extract_function(PLANNER, name)
            for name in ("sessionControlBlockedReason", "restoreSession")
        }
        script = f"""
const vm = require("vm");
const worker = {{ready: true, busy: false, compacting: false, stopping: false,
  controlPending: false, sessionSwitching: false, sessionRefreshPending: false,
  sessionFile: "A", messages: [{{role: "user", text: "A remains"}}], status: "Cancelled",
  switchSession() {{ return false; }}}};
const context = {{selectedPath: "pages/A.md", selectedAgent: worker, pageBusy: false,
  pageRetiring: false, sendBusy: false, toggleBusy: false, toggleRetiring: false,
  approvalOpen: () => false, notice: "", restoreHistoryBackup: null,
  restoreStateSeen: false, restoreStateSessionFile: "", historyLoadError: "",
  historyRetryPending: false}};
vm.createContext(context);
for (const value of {json.dumps(list(functions.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
if (context.restoreSession()) throw new Error("cancelled picker was accepted");
console.log(JSON.stringify({{messages: worker.messages, backup: context.restoreHistoryBackup,
  name: worker.sessionFile}}));
"""
        self.assertEqual(self.run_node(script), {
            "messages": [{"role": "user", "text": "A remains"}],
            "backup": None, "name": "A",
        })

    def test_restore_history_failure_never_leaks_old_project_and_can_retry_current(self):
        functions = {
            name: extract_function(PLANNER, name)
            for name in ("recoverRestoreHistory", "retrySessionHistory", "finishHistoryRetry")
        }
        script = f"""
const vm = require("vm");
const calls = [];
const b = {{sessionFile: "B", messages: [], messagesGeneration: 9,
  messagesAwaitingSessionState: false, sessionRefreshPending: false,
  sessionSwitching: false, requestMessages() {{ calls.push("B"); return "qs-1"; }}}};
const context = {{selectedAgent: b, selectedPath: "pages/B.md", notice: "",
  historyLoadError: "", historyRetryPending: false, restoreStateSeen: true,
  restoreStateSessionFile: "B", restoreHistoryBackup: {{worker: b,
    sessionFile: "A", messages: [{{role: "user", text: "A only"}}]}}}};
vm.createContext(context);
for (const value of {json.dumps(list(functions.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
if (context.recoverRestoreHistory(b)) throw new Error("copied A into B");
if (b.messages.length !== 0 || b.messagesGeneration !== 9)
  throw new Error("B history was mutated by the failed fetch");
if (!context.historyLoadError.includes("Retry history")) throw new Error("no retry error");
if (!context.retrySessionHistory() || calls.length !== 1 || context.historyRetryPending !== true)
  throw new Error("retry was not scoped to B");
b.messages = [{{role: "user", text: "B only"}}];
context.finishHistoryRetry(b);
console.log(JSON.stringify({{calls, messages: b.messages, error: context.historyLoadError,
  pending: context.historyRetryPending}}));
"""
        self.assertEqual(self.run_node(script), {
            "calls": ["B"], "messages": [{"role": "user", "text": "B only"}],
            "error": "", "pending": False,
        })

    def test_unknown_restore_identity_refuses_to_restore_or_bump_generation(self):
        functions = {"recoverRestoreHistory": extract_function(PLANNER, "recoverRestoreHistory")}
        script = f"""
const vm = require("vm");
const worker = {{sessionFile: "A", messages: [], messagesGeneration: 4}};
const context = {{restoreHistoryBackup: {{worker, sessionFile: "A", messages: [
  {{role: "user", text: "old"}}]}}, restoreStateSeen: false,
  restoreStateSessionFile: "", historyLoadError: "", historyRetryPending: false}};
vm.createContext(context);
const fn = vm.runInContext("(" + {json.dumps(functions["recoverRestoreHistory"])} + ")", context);
context.recoverRestoreHistory = fn;
if (context.recoverRestoreHistory(worker)) throw new Error("restored with unknown identity");
console.log(JSON.stringify({{messages: worker.messages, generation: worker.messagesGeneration,
  error: context.historyLoadError}}));
"""
        self.assertEqual(self.run_node(script), {
            "messages": [], "generation": 4,
            "error": "Session history could not be loaded safely. Retry history for the current session.",
        })


class ProjectPlannerStructureTests(unittest.TestCase):
    def test_manual_project_routing_uses_the_new_surface(self):
        self.assertIn("ProjectPlanner {", SHELL)
        self.assertIn("function onProjectPlanningRequested()", SHELL)
        self.assertIn("projectPlanner.open()", SHELL)
        self.assertIn("signal projectPlanningRequested()", PALETTE)
        self.assertIn('["Project planner", "projectPlanner"]', PALETTE)
        self.assertIn("function handoffToProjectPlanner()", PALETTE)
        self.assertIn("root.projectPlanningRequested()", PALETTE)

    def test_manual_surface_has_no_automatic_timer_or_ledger(self):
        self.assertNotIn("interval: 1500", PLANNER)
        self.assertNotIn("automatic", PLANNER.lower())
        self.assertNotIn("ledger", PLANNER.lower())
        self.assertIn('name: "projectPlanner"', PLANNER)
        self.assertIn('target: "projectPlanner"', PLANNER)
        self.assertIn("listTimeout", PLANNER)
        self.assertIn("pageTimeout", PLANNER)
        self.assertIn("function cancelListRead()", PLANNER)
        self.assertIn("function cancelPageRead()", PLANNER)
        self.assertIn("toggleWarningTimer", PLANNER)
        self.assertNotIn("toggleProcess.running = false", PLANNER)

    def test_accessible_scrolling_and_stop_guards_are_present(self):
        self.assertIn("activeFocusOnTab: true", PLANNER)
        self.assertIn("projectList.positionViewAtIndex(index, ListView.Contain)", PLANNER)
        self.assertIn("todoList.positionViewAtIndex(index, ListView.Contain)", PLANNER)
        self.assertIn("ScrollBar.vertical", PLANNER)
        self.assertIn("function stopAgent()", PLANNER)
        self.assertIn("function blockedReason()", PLANNER)


class ProjectPlannerChatLayoutTests(unittest.TestCase):
    def test_history_and_live_answer_share_one_stable_scrolling_store(self):
        self.assertIn("property var _convTurns: ({})", PLANNER)
        self.assertIn("function trackConversationTurn(worker, path, count)", PLANNER)
        self.assertIn("function dropConversationTurn(path)", PLANNER)
        self.assertIn("function conversationBoundary(worker, path)", PLANNER)
        self.assertIn("function conversationWantsLive(messages, answer, boundary)", PLANNER)
        self.assertIn("function conversationPlan(messages, answer, boundary)", PLANNER)
        self.assertIn("let preCount = worker && Array.isArray(worker.messages) ? worker.messages.length : 0", PLANNER)
        self.assertIn("root.trackConversationTurn(worker, path, preCount)", PLANNER)
        self.assertIn("root.dropConversationTurn(rec.path)", PLANNER)
        self.assertIn("turn.sessionFile", PLANNER)
        self.assertIn("turn.generation", PLANNER)
        self.assertIn("delete _convTurns[path]", PLANNER)
        self.assertNotIn("conversationTurnAssistantIndex", PLANNER)
        self.assertNotIn("turnText", PLANNER)
        self.assertIn("function syncConversation()", PLANNER)
        self.assertIn("ListModel { id: conversationStore }", PLANNER)
        self.assertIn("model: conversationStore", PLANNER)
        self.assertIn("conversationStore.set(i, row)", PLANNER)
        self.assertIn("conversationStore.append(plan[j])", PLANNER)
        self.assertIn("conversationStore.remove(conversationStore.count - 1)", PLANNER)
        self.assertIn("onSelectedAgentChanged", PLANNER)
        # The old standalone content-sized answer Text pushed the composer
        # away; the live answer must only exist as a store row.
        self.assertNotIn('visible: root.selectedAgent && root.selectedAgent.answer !== ""', PLANNER)
        self.assertNotIn("Math.min(210", PLANNER)
        history = PLANNER[PLANNER.index("id: historyList"):PLANNER.index("id: composerBar")]
        self.assertIn("clip: true", history)
        self.assertIn("ScrollBar.vertical", history)
        self.assertIn("model.role", history)
        self.assertIn("model.text", history)
        frame = PLANNER[PLANNER.index("id: transcriptFrame"):PLANNER.index("id: composerBar")]
        self.assertIn("Layout.fillHeight: true", frame)
        self.assertIn("Layout.minimumHeight: 0", frame)
        self.assertIn("historyList.activeFocus ? Theme.focusBorder : Theme.border", frame)

    def test_transcript_preserves_position_and_follows_only_at_bottom(self):
        sync = extract_function(PLANNER, "syncConversation")
        self.assertIn("atBottom", sync)
        self.assertIn("positionViewAtEnd()", sync)
        self.assertIn("clampContentY(historyList.contentY", sync)

    def test_transcript_keyboard_scroll_and_focus_cue(self):
        history = PLANNER[PLANNER.index("id: historyList"):PLANNER.index("id: composerBar")]
        self.assertIn("activeFocusOnTab: true", history)
        self.assertIn('Accessible.name: "Conversation transcript"', history)
        self.assertIn("root.scrollTranscriptBy(80)", history)
        self.assertIn("root.scrollTranscriptBy(-80)", history)
        self.assertIn("root.scrollTranscriptBy(historyList.height)", history)
        self.assertIn("root.scrollTranscriptBy(-historyList.height)", history)
        self.assertIn("root.scrollTranscriptHome()", history)
        self.assertIn("root.scrollTranscriptEnd()", history)
        self.assertIn("function scrollTranscriptBy(pixels)", PLANNER)
        self.assertIn("function scrollTranscriptHome()", PLANNER)
        self.assertIn("function scrollTranscriptEnd()", PLANNER)
        self.assertIn("function clampContentY(y, contentH, height)", PLANNER)

    def test_streaming_updates_refresh_the_transcript(self):
        self.assertIn("root.syncConversation()", PLANNER)
        self.assertIn("function onTextDelta() { root.historyRevision++; root.syncConversation() }", PLANNER)
        self.assertIn("function onAnswerChanged() { root.historyRevision++; root.syncConversation() }", PLANNER)
        self.assertIn("function onStatusChanged() { root.historyRevision++; root.syncConversation() }", PLANNER)

    def test_composer_stays_fixed_below_the_transcript(self):
        self.assertGreater(PLANNER.index("id: composerBar"), PLANNER.index("id: historyList"))
        composer = PLANNER[PLANNER.index("id: composerScroll"):PLANNER.index('text: "Stop"')]
        self.assertIn("Layout.preferredHeight: 72", composer)
        self.assertIn("Layout.minimumWidth: 0", composer)
        self.assertIn("width: composerScroll.availableWidth", composer)
        self.assertIn("clip: true", composer)
        self.assertIn("ScrollBar.vertical", composer)
        self.assertIn("id: composer", PLANNER)
        self.assertIn("text: root.draftFor(root.selectedPath)", PLANNER)
        self.assertIn("onTextChanged: root.setDraft(root.selectedPath, text)", PLANNER)
        self.assertIn("Qt.ControlModifier", PLANNER)
        self.assertIn("root.send(); event.accepted = true", PLANNER)
        self.assertIn('text: root.sendBusy ? "Loading…" : "Send"', PLANNER)
        self.assertIn("root.selectedAgent.pendingApproval", PLANNER)
        self.assertIn("onClicked: root.stopAgent()", PLANNER)

    def test_tasks_sidebar_toggle_is_animated_and_keyboard_accessible(self):
        self.assertIn("property bool tasksOpen: true", PLANNER)
        self.assertIn("id: tasksToggle", PLANNER)
        self.assertIn("checked: root.tasksOpen", PLANNER)
        self.assertIn("checkable: true", PLANNER[PLANNER.index("id: tasksToggle"):PLANNER.index("id: chatArea")])
        self.assertIn('Accessible.name: "Toggle tasks panel"', PLANNER)
        self.assertIn("onClicked: root.tasksOpen = !root.tasksOpen", PLANNER)
        panel = PLANNER[PLANNER.index("id: tasksPanel"):PLANNER.index("id: tasksOverlayPanel")]
        self.assertIn("Layout.preferredWidth: !root.tasksOverlay && root.tasksOpen ? root.tasksTargetWidth : 0", panel)
        self.assertIn("Layout.maximumWidth: 300", panel)
        self.assertIn("Layout.minimumWidth: 0", panel)
        self.assertIn("clip: true", panel)
        self.assertIn("Behavior on Layout.preferredWidth", panel)
        self.assertIn("duration: Theme.motionPanel", panel)
        self.assertIn("easing.type: Easing.OutCubic", panel)
        self.assertIn("visible: !root.tasksOverlay && (root.tasksOpen || tasksPanel.width > 1)", panel)
        self.assertIn("enabled: !root.tasksOverlay && root.tasksOpen", panel)

    def test_tasks_sidebar_is_responsive_with_an_accessible_overlay(self):
        self.assertIn("property int tasksTargetWidth:", PLANNER)
        self.assertIn("chatRow.width * 0.34", PLANNER)
        self.assertIn("property bool tasksOverlay: chatRow.width > 0 && chatRow.width < 600", PLANNER)
        self.assertIn("property int tasksOverlayWidth:", PLANNER)
        self.assertIn("property int tasksInnerWidth:", PLANNER)
        overlay = PLANNER[PLANNER.index("id: tasksOverlayPanel"):PLANNER.index("id: journalAssistant",
            PLANNER.index("id: tasksOverlayPanel"))]
        self.assertIn("visible: root.tasksOverlay && root.tasksOpen", overlay)
        self.assertIn("enabled: root.tasksOverlay && root.tasksOpen", overlay)
        self.assertIn("width: root.tasksOverlayWidth", overlay)
        self.assertIn('Accessible.name: "Tasks overlay"', overlay)
        self.assertIn("active: root.tasksOverlay", overlay)
        self.assertIn("active: !root.tasksOverlay", PLANNER)
        self.assertIn("sourceComponent: tasksContent", PLANNER)
        # Overlay Escape closes the overlay instead of the whole planner.
        self.assertIn("if (root.tasksOverlay && root.tasksOpen) root.tasksOpen = false", PLANNER)

    def test_tasks_inner_stays_fixed_width_and_clipped_while_collapsing(self):
        content = PLANNER[PLANNER.index("id: tasksContent"):PLANNER.index("id: tasksToggle")]
        # Loader-sized plain Item wrapper, then the nested fixed-width column.
        wrapper = content[content.index("id: tasksContent"):content.index("ColumnLayout")]
        self.assertIn("Item {", wrapper)
        self.assertIn("anchors.fill: parent", wrapper)
        self.assertIn("width: root.tasksInnerWidth", content)
        nested = content[content.index("ColumnLayout"):content.index("id: todoList")]
        self.assertIn("anchors.top: parent.top", nested)
        self.assertIn("anchors.bottom: parent.bottom", nested)
        self.assertIn("anchors.left: parent.left", nested)
        self.assertIn("enabled: root.tasksOpen", content)
        # No visibility gate on the inner content: it stays visible (but
        # disabled and clipped) through the collapse animation.
        self.assertNotIn("visible: root.tasksOpen", nested)

    def test_tasks_content_and_guards_are_preserved(self):
        self.assertIn("Tasks (including completed)", PLANNER)
        self.assertIn("Loading selected page…", PLANNER)
        self.assertIn("No TODOs on this project page.", PLANNER)
        self.assertIn("model: root.currentPage ? root.currentPage.todos : []", PLANNER)
        self.assertIn("root.toggleTodo(modelData)", PLANNER)
        self.assertIn("todoList.positionViewAtIndex(index, ListView.Contain)", PLANNER)
        self.assertIn('"Completed: "', PLANNER)
        self.assertIn('"Open: "', PLANNER)
        todos = PLANNER[PLANNER.index("id: todoList"):PLANNER.index("No TODOs on this project page.")]
        self.assertIn("Layout.fillHeight: true", todos)
        self.assertIn("Layout.minimumHeight: 0", todos)
        self.assertIn("clip: true", todos)
        self.assertIn("ScrollBar.vertical", todos)


if __name__ == "__main__":
    unittest.main()
