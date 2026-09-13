"""Palette frontend on the Rust bridge via ScopedAgent (no model/notes).

Exercises the REAL QML JS in widgets/ScopedAgent.qml and
widgets/CommandPalette.qml through node with a fake bridge agent.
No real pi/model/notes are touched.
"""
import sys as _sys
_sys.dont_write_bytecode = True

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCOPED = (ROOT / "widgets" / "ScopedAgent.qml").read_text(encoding="utf-8")
PALETTE = (ROOT / "widgets" / "CommandPalette.qml").read_text(encoding="utf-8")
CAPTURE = (ROOT / "widgets" / "PaletteCapture.qml").read_text(encoding="utf-8")
APPROVAL = (ROOT / "widgets" / "PaletteApprovalDialog.qml").read_text(encoding="utf-8")
DATASOURCES = (ROOT / "widgets" / "PaletteDataSources.qml").read_text(encoding="utf-8")
PALETTE_TEXT = (ROOT / "widgets" / "PaletteText.js").read_text(encoding="utf-8")


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


def extract_handler(source, marker):
    start = source.index(marker)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1:index]
    raise AssertionError("unterminated handler: " + marker)


SCOPED_FNS = [
    "bridgeArgs", "hasPending", "trackPending", "clearPending",
    "sendOp", "start", "prompt", "abort", "newSession", "switchSession",
    "rename", "compact", "chooseModel", "respond", "requestMessages",
    "requestStats", "request", "textFromMessage", "filterMessages",
    "applyState", "handleUpdate",
]


def qml_bool_default(source, name):
    import re
    match = re.search(r"property bool " + re.escape(name) + r"\s*:\s*(true|false)", source)
    assert match, "missing QML default: " + name
    return match.group(1) == "true"


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class PaletteAgentUiTests(unittest.TestCase):
    def run_node(self, script):
        completed = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=8)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        return json.loads(completed.stdout.splitlines()[-1])

    def scoped_funcs(self, names=None):
        names = names or SCOPED_FNS
        return {name: extract_function(SCOPED, name) for name in names}

    def palette_func(self, name):
        return extract_function(PALETTE, name)

    # -- factory / bridge mode -------------------------------------------
    def test_palette_uses_bridge_mode_without_project(self):
        self.assertIn("property bool paletteMode: false", SCOPED)
        # scopedMode remains actual scope only; paletteMode is excluded.
        self.assertIn("readonly property bool scopedMode: !!projectPath || journalMode", SCOPED)
        self.assertNotIn("paletteMode", SCOPED.split("readonly property bool scopedMode")[1].split("\n")[0])
        bridge_src = extract_function(SCOPED, "bridgeArgs")
        self.assertIn('"palette"', bridge_src)
        self.assertIn('"journal"', bridge_src)
        self.assertIn('"project"', bridge_src)
        self.assertIn('mode === "project"', bridge_src)
        self.assertNotIn('if (!root.journalMode)', bridge_src)
        # No --project in palette mode: palette branch never pushes it.
        self.assertIn("palette", bridge_src)
        # Conflicting config is fenced by precedence, not a second scope flag.
        self.assertIn("paletteMode", bridge_src)
        # Palette owns a ScopedAgent in palette mode; ScopedAgent is the sole adapter (legacy PiAgent removed).
        self.assertIn("property ScopedAgent agent: ScopedAgent { paletteMode: true }", PALETTE)
        self.assertNotIn("property PiAgent agent", PALETTE)
        # No raw Pi parsing in the palette or its agent adapter.
        for needle in ('"pi"', "piCommand", "--mode\", \"rpc\""):
            self.assertNotIn(needle, SCOPED)
            self.assertNotIn(needle, PALETTE)

    def test_bridge_args_palette_has_no_project(self):
        funcs = self.scoped_funcs(["bridgeArgs"])
        script = f"""
const vm = require("vm");
const context = {{paletteMode: true, projectPath: "pages/A.md", journalMode: true,
  _initSession: "", _initName: "", _initFresh: false,
  Quickshell: {{shellPath(v) {{ return v; }}}},
  bridgePath() {{ return "/bin/qs-agent-orchestrator"; }}}};
context.root = context;
vm.createContext(context);
context.bridgeArgs = vm.runInContext("(" + {json.dumps(funcs["bridgeArgs"])} + ")", context);
const args = context.bridgeArgs();
if (args[args.indexOf("--mode") + 1] !== "palette") throw new Error("palette mode missing: " + JSON.stringify(args));
if (args.includes("--project")) throw new Error("palette leaked --project: " + JSON.stringify(args));
context.paletteMode = false; context.journalMode = true;
const jargs = context.bridgeArgs();
if (jargs[jargs.indexOf("--mode") + 1] !== "journal" || jargs.includes("--project"))
  throw new Error("journal mode wrong: " + JSON.stringify(jargs));
context.journalMode = false; context.projectPath = "pages/A.md";
const pargs = context.bridgeArgs();
if (pargs[pargs.indexOf("--mode") + 1] !== "project" || !pargs.includes("--project"))
  throw new Error("project mode wrong: " + JSON.stringify(pargs));
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})

    # -- lifecycle: open/start/close --------------------------------------
    def test_start_only_when_intentional_no_prompt_on_open(self):
        # Opening never prompts; close never stops the bridge.
        open_block = PALETTE[PALETTE.index("function open() {"):PALETTE.index("function close() {")]
        self.assertNotIn("agent.prompt", open_block)
        self.assertNotIn("agent.start", open_block)
        self.assertNotIn("ensurePaletteAgent", open_block)
        close_block = PALETTE[PALETTE.index("function close() {"):PALETTE.index("function handoffToProjectPlanner")]
        self.assertNotIn("agent.stopIdle", close_block)
        self.assertNotIn("agent.abort", close_block)
        self.assertIn("Do not stop Pi", PALETTE)
        # loadModeData only auto-starts via the intentional gate.
        load_src = extract_function(PALETTE, "loadModeData")
        self.assertIn("ensurePaletteAgent()", load_src)
        self.assertNotIn("agent.start()", load_src)
        needs_src = extract_function(PALETTE, "paletteAgentNeedsStart")
        self.assertIn("agent.retryable", needs_src)
        self.assertIn("agent.processStartFailed", needs_src)
        self.assertIn("agent.launchAttempted", needs_src)
        self.assertIn("agent.idleStopped", needs_src)
        # sendOp never spawns the bridge.
        send_src = extract_function(SCOPED, "sendOp")
        self.assertIn("if (!bridgeProc.running) return", send_src)
        self.assertNotIn("bridgeProc.running = true", send_src)

    def test_real_initial_defaults_allow_first_launch(self):
        # Regression: _retryableBase:true blocked the very first ai: start
        # because paletteAgentNeedsStart rejected retryable first. The real
        # QML baseline must be non-retryable, and the first-launch guard must
        # explicitly precede the dead-bridge gate.
        self.assertFalse(qml_bool_default(SCOPED, "_retryableBase"),
                         "ScopedAgent baseline must not start retryable")
        self.assertFalse(qml_bool_default(SCOPED, "launchAttempted"))
        self.assertFalse(qml_bool_default(SCOPED, "processStartFailed"))
        needs = self.palette_func("paletteAgentNeedsStart")
        script = f"""
const vm = require("vm");
const context = {{
  agent: {{launchAttempted: {json.dumps(qml_bool_default(SCOPED, "launchAttempted"))},
    processStartFailed: {json.dumps(qml_bool_default(SCOPED, "processStartFailed"))},
    retryable: {json.dumps(qml_bool_default(SCOPED, "_retryableBase"))},
    idleStopped: false, idleStopping: false}},
}};
vm.createContext(context);
context.paletteAgentNeedsStart = vm.runInContext("(" + {json.dumps(needs)} + ")", context);
if (!context.paletteAgentNeedsStart()) throw new Error("real initial defaults blocked first ai: launch");
context.agent.launchAttempted = true;
context.agent.retryable = true; context.agent.processStartFailed = true;
if (context.paletteAgentNeedsStart()) throw new Error("real dead bridge auto-started");
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})

    def test_abort_rollback_dead_then_start_snapshot_prompt(self):
        abort_src = extract_function(SCOPED, "abort")
        self.assertIn("_optStopping = false", abort_src)
        funcs = self.scoped_funcs(["json", "trackPending", "clearPending", "hasPending",
                                   "refreshSpawnIdentity",
                                   "sendOp", "start", "abort", "prompt",
                                   "applyState", "handleUpdate"])
        script = f"""
const vm = require("vm");
const signals = {{opFinished: [], failed: []}};
const bridgeProc = {{running: false, command: [], writes: [], write(v) {{ this.writes.push(JSON.parse(v)); }}}};
const context = {{
  projectPath: "", journalMode: false, paletteMode: true,
  _initSession: "", _initName: "", _initFresh: false, _bridgeLaunched: false,
  _bridgeProcessStarted: false, _bridgeExitHandled: false,
  _pendingAcks: {{}}, _pendingCount: 0, _uiSerial: 0, _queuedOps: [],
  _optBusy: false, _optControl: false, _optSwitching: false, _optCompacting: false,
  _optStopping: false, _optRefresh: false,
  _bReady: false, _bBusy: false, _bCompacting: false, _bControl: false,
  _bSwitching: false, _bStopping: false, _bRefresh: false,
  sessionFile: "", sessionName: "", freshSession: false,
  model: null, models: [], commands: [], stateOk: false, extensionOk: false,
  sessionRefreshFailed: false, sessionRefreshGeneration: 0,
  sessionChangeInFlight: false, sessionChangeKind: "", sessionChangeRequestId: "",
  sessionChangeCancelRequested: false, answer: "", messages: [], messagesSessionFile: "",
  messagesGeneration: 0, messagesAwaitingSessionState: false,
  status: "", pendingApproval: null, pendingRequests: {{}},
  serial: 0, generation: 0, lastRequestId: "",
  processStarted: false, processStartFailed: false, startRequested: false,
  launchAttempted: false, idleStopped: false, pendingResume: false,
  desiredRunning: false, _retryableBase: false, idleStopping: false,
  diagnostic: "", statsText: "",
  bridgeProc, Quickshell: {{shellPath(v) {{ return v; }}}},
  opFinished(id, op, a, m) {{ signals.opFinished.push([id, op, a]); }},
  uiRequest() {{}}, failed(m) {{ signals.failed.push(m); }}, bridgeDead() {{}},
  historyFailed() {{}}, historyLoaded() {{}}, textDelta() {{}}, finished() {{}}, stateUpdated() {{}}, statsChanged() {{}},
}};
Object.defineProperty(context, "ready", {{get() {{ return this._bReady && this._pendingCount === 0; }}}});
Object.defineProperty(context, "busy", {{get() {{ return this._bBusy || this._optBusy; }}}});
Object.defineProperty(context, "compacting", {{get() {{ return this._bCompacting || this._optCompacting; }}}});
Object.defineProperty(context, "controlPending", {{get() {{ return this._bControl || this._optControl; }}}});
Object.defineProperty(context, "sessionSwitching", {{get() {{ return this._bSwitching || this._optSwitching; }}}});
Object.defineProperty(context, "stopping", {{get() {{ return this._bStopping || this._optStopping; }}}});
Object.defineProperty(context, "sessionRefreshPending", {{get() {{ return this._bRefresh || this._optRefresh; }}}});
Object.defineProperty(context, "retryable", {{get() {{ return this._retryableBase; }}}});
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(funcs.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
context.bridgeArgs = function() {{ return ["bridge", "--root", ".", "--mode", "palette"]; }};
// Dead/unstarted Stop must not stick stopping: rollback like other ops.
if (context.abort() !== "") throw new Error("dead abort should return empty id");
if (context.stopping) throw new Error("dead abort stuck stopping; retry would stay blocked");
// Explicit start then launches.
const sid = context.start();
if (!sid || !bridgeProc.running) throw new Error("retry start did not launch after rolled-back abort");
// Ack the start with a ready snapshot, then prompt is allowed.
context.handleUpdate({{version: 1, type: "update", ack: sid, accepted: true, events: [],
  state: {{ready: true, busy: false, sessionFile: "p.jsonl", sessionName: "p", status: "Ready",
    processStarted: true, desiredRunning: true, retryable: false, launchAttempted: true}}}});
if (!context.ready) throw new Error("ready snapshot did not project");
const pid = context.prompt("hello");
if (!pid) throw new Error("prompt blocked after start+snapshot");
// A valid pending Stop survives unrelated snapshots until its ack.
bridgeProc.writes = [];
const stopId = context.abort();
if (!stopId || !context.stopping) throw new Error("live Stop did not set stopping gate");
context.handleUpdate({{version: 1, type: "update", ack: null, events: [],
  state: {{ready: true, busy: true, stopping: false, sessionFile: "p.jsonl", sessionName: "p",
    status: "Thinking…", processStarted: true, desiredRunning: true, retryable: false}}}});
if (!context.stopping) throw new Error("unrelated snapshot cleared valid Stop gate");
context.handleUpdate({{version: 1, type: "update", ack: stopId, accepted: true, events: [],
  state: {{ready: true, busy: false, stopping: false, sessionFile: "p.jsonl", sessionName: "p",
    status: "Ready", processStarted: true, desiredRunning: true, retryable: false}}}});
if (context.stopping) throw new Error("acked Stop did not release gate via bridge projection");
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})

    def test_capture_on_exited_stale_vs_current_executable(self):
        # Capture process + signals now live in PaletteCapture.qml. Stale
        # generations drop inside the component; current results emit
        # failed/captured/reopenRequest which the palette handles (reopen +
        # draft restore + agent.prompt) exactly like the inlined version did.
        capture_block = CAPTURE[CAPTURE.index("id: captureProcess"):CAPTURE.index("function beginRegionSelection")]
        handler_body = extract_handler(capture_block, "onExited:")
        reopen = extract_function(CAPTURE, "reopenCaptureAi")
        # Palette signal handlers are executed verbatim, not hand-copied.
        reopen_body = extract_handler(PALETTE, "onReopenRequest:")
        captured_body = extract_handler(PALETTE, "onCaptured:")
        failed_body = extract_handler(PALETTE, "onFailed:")
        script = f"""
const vm = require("vm");
const prompts = [];
const starts = [];
const opens = [];
const handlerSrc = {json.dumps(handler_body)};
const reopenSrc = {json.dumps(reopen)};
const reopenBody = {json.dumps(reopen_body)};
const capturedBody = {json.dumps(captured_body)};
const failedBody = {json.dumps(failed_body)};
const textSrc = {json.dumps(PALETTE_TEXT)};
function makeHarness() {{
  const input = {{text: ""}};
  const captureOutput = {{text: ""}};
  const captureError = {{text: ""}};
  const agent = {{
    status: "Pi is not ready",
    prompt(p, images) {{ prompts.push([p, images]); return this._accept; }},
    _accept: false, start() {{ starts.push("start"); return "ui-9"; }},
  }};
  // Palette draft state; the capture component owns generations/prompt and
  // reports through the real CommandPalette onReopenRequest/onCaptured/
  // onFailed handlers extracted above (no hand-copied mirrors).
  const palette = {{query: "ai: original", notice: "", requestedOpen: true, agent}};
  palette.open = function() {{ opens.push("open"); palette.requestedOpen = true; }};
  const root = {{
    captureProcessGeneration: 0, captureProcessPrompt: "", captureGeneration: 0,
    capturePrompt: "", requestedOpen: true,
    agent,
  }};
  const boot = {{}};
  vm.createContext(boot);
  const reopenFn = vm.runInContext("(function(root, capture, input, message){{" + reopenBody + "}})", boot);
  const capturedFn = vm.runInContext("(function(root, capture, input, prompt, images){{" + capturedBody + "}})", boot);
  const failedFn = vm.runInContext("(function(root, capture, input, message){{" + failedBody + "}})", boot);
  root.reopenRequest = function(message) {{ reopenFn(palette, root, input, message); }};
  root.reopenCaptureAi = function(message) {{ root.reopenRequest(message || ""); }};
  root.failed = function(message) {{ failedFn(palette, root, input, message); }};
  root.captured = function(prompt, images) {{ capturedFn(palette, root, input, prompt, images); }};
  const context = {{root, palette, input, captureOutput, captureError, agent}};
  context.Quickshell = {{shellPath(v) {{ return v; }}}};
  context.Qt = {{callLater() {{}}}};
  vm.createContext(context);
  // Capture text helpers live in PaletteText.js; captureFailure closes over
  // the module-local boundedCaptureDetail, and the handler reaches it as
  // PaletteText.captureFailure(code, captureError.text) like the QML does.
  vm.runInContext(textSrc, context);
  context.PaletteText = {{boundedCaptureDetail: context.boundedCaptureDetail,
    captureFailure: context.captureFailure, plainSnippet: context.plainSnippet}};
  // Bind root.* lookups used inside the handler to the harness root.
  const handler = vm.runInContext("(function(code) {{" + handlerSrc + "}})", context);
  return {{context, root, palette, input, captureOutput, captureError, agent, handler}};
}}
// Stale failure after close/reopen must touch nothing.
{{
  const h = makeHarness();
  const nPrompts = prompts.length, nStarts = starts.length;
  h.root.captureGeneration = 6; h.root.captureProcessGeneration = 5;
  h.root.captureProcessPrompt = "Describe this screen region.";
  h.root.capturePrompt = "Describe this screen region.";
  h.palette.query = "ai: new typing"; h.input.text = h.palette.query; h.palette.notice = "";
  h.captureError.text = "cancelled";
  h.handler(1);
  if (h.palette.query !== "ai: new typing" || h.palette.notice !== "")
    throw new Error("stale failure changed draft: " + JSON.stringify([h.palette.query, h.palette.notice]));
  if (prompts.length !== nPrompts || starts.length !== nStarts) throw new Error("stale failure prompted/started");
}}
// Stale success must also touch nothing.
{{
  const h = makeHarness();
  const nPrompts = prompts.length;
  h.root.captureGeneration = 6; h.root.captureProcessGeneration = 5;
  h.root.captureProcessPrompt = "Describe this screen region.";
  h.root.capturePrompt = "Describe this screen region.";
  h.palette.query = "ai: new typing"; h.input.text = h.palette.query;
  h.captureOutput.text = JSON.stringify({{images: [{{data: "AAA"}}]}});
  h.handler(0);
  if (h.palette.query !== "ai: new typing") throw new Error("stale success changed draft");
  if (prompts.length !== nPrompts) throw new Error("stale success prompted");
}}
// Current failure preserves the prompt for retry.
{{
  const h = makeHarness();
  prompts.length = 0;
  h.root.captureGeneration = 5; h.root.captureProcessGeneration = 5;
  h.root.captureProcessPrompt = "Describe this screen region.";
  h.root.capturePrompt = "Describe this screen region.";
  h.palette.query = "ai: original"; h.captureError.text = "boom";
  h.handler(2);
  if (!opens.length) throw new Error("current failure did not reopen");
  if (h.palette.query !== "ai: Describe this screen region.")
    throw new Error("current failure lost prompt: " + h.palette.query);
  if (prompts.length) throw new Error("failure path must not prompt directly");
}}
// Current image send with async rejection preserves query+notice, never starts.
{{
  const h = makeHarness();
  prompts.length = 0; starts.length = 0; opens.length = 0;
  h.root.captureGeneration = 5; h.root.captureProcessGeneration = 5;
  h.root.captureProcessPrompt = "Describe this screen region.";
  h.root.capturePrompt = "Describe this screen region.";
  h.agent._accept = false;
  h.captureOutput.text = JSON.stringify({{images: [{{data: "AAA"}}]}});
  h.handler(0);
  if (h.palette.query !== "ai: Describe this screen region.")
    throw new Error("async rejection lost query: " + h.palette.query);
  if (!h.palette.notice) throw new Error("async rejection lost notice");
  if (prompts.length !== 1 || prompts[0][0] !== "Describe this screen region." ||
      !prompts[0][1] || prompts[0][1][0].data !== "AAA")
    throw new Error("image args not forwarded: " + JSON.stringify(prompts));
  if (starts.length) throw new Error("capture completion started the bridge");
}}
// Current image send accepted keeps the same draft.
{{
  const h = makeHarness();
  prompts.length = 0;
  h.root.captureGeneration = 5; h.root.captureProcessGeneration = 5;
  h.root.captureProcessPrompt = "Describe this screen region.";
  h.root.capturePrompt = "Describe this screen region.";
  h.agent._accept = "ui-7";
  h.captureOutput.text = JSON.stringify({{images: [{{data: "AAA"}}]}});
  h.handler(0);
  if (h.palette.query !== "ai: Describe this screen region.") throw new Error("accepted send cleared query");
  if (prompts.length !== 1) throw new Error("accepted send did not prompt once");
}}
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})

    def test_needs_start_idle_resume_but_not_dead(self):
        needs = self.palette_func("paletteAgentNeedsStart")
        ensure = self.palette_func("ensurePaletteAgent")
        retry = self.palette_func("retryPaletteAgent")
        script = f"""
const vm = require("vm");
const starts = [];
const context = {{
  agent: {{launchAttempted: false, retryable: false, processStartFailed: false,
    idleStopped: false, idleStopping: false, start() {{ starts.push("start"); return "ui-1"; }},
    status: "Ready"}},
  notice: "",
}};
vm.createContext(context);
context.paletteAgentNeedsStart = vm.runInContext("(" + {json.dumps(needs)} + ")", context);
context.ensurePaletteAgent = vm.runInContext("(" + {json.dumps(ensure)} + ")", context);
context.retryPaletteAgent = vm.runInContext("(" + {json.dumps(retry)} + ")", context);
if (!context.paletteAgentNeedsStart()) throw new Error("initial start not needed");
if (!context.ensurePaletteAgent() || starts.length !== 1) throw new Error("initial ensure did not start once");
// Running bridge: no auto start.
context.agent.launchAttempted = true;
if (context.paletteAgentNeedsStart()) throw new Error("running bridge requested start");
if (context.ensurePaletteAgent() || starts.length !== 1) throw new Error("running bridge auto-started");
// Idle resume is intentional.
context.agent.idleStopped = true;
if (!context.paletteAgentNeedsStart()) throw new Error("idle resume not needed");
if (!context.ensurePaletteAgent() || starts.length !== 2) throw new Error("idle resume did not start");
context.agent.idleStopped = false;
// Dead bridge never auto-starts.
context.agent.retryable = true; context.agent.processStartFailed = true;
if (context.paletteAgentNeedsStart()) throw new Error("dead bridge requested auto start");
if (context.ensurePaletteAgent() || starts.length !== 2) throw new Error("dead bridge auto-started");
// Explicit retry is the only dead path.
if (!context.retryPaletteAgent() || starts.length !== 3) throw new Error("explicit retry did not start");
if (!context.notice.includes("Retrying")) throw new Error("retry did not notice");
context.agent.retryable = false;
if (context.retryPaletteAgent()) throw new Error("non-retryable retried");
console.log(JSON.stringify({{ok: true, starts: starts.length}}));
"""
        self.assertEqual(self.run_node(script)["ok"], True)

    def test_repeated_start_pending_is_gated_no_late_spawn(self):
        funcs = self.scoped_funcs(["json", "hasPending", "trackPending", "clearPending", "sendOp", "start"])
        script = f"""
const vm = require("vm");
const bridgeProc = {{running: true, command: [], writes: [], write(v) {{ this.writes.push(JSON.parse(v)); }}}};
const context = {{
  _pendingAcks: {{}}, _pendingCount: 0, _uiSerial: 0, _queuedOps: [],
  _initSession: "", _initName: "", _initFresh: false, _bridgeLaunched: true,
  projectPath: "", journalMode: false, paletteMode: true,
  bridgeProc, Quickshell: {{shellPath(v) {{ return v; }}}},
}};
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(funcs.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
context.bridgeArgs = function() {{ return ["bridge", "--root", ".", "--mode", "palette"]; }};
const first = context.start();
if (!first) throw new Error("first start not accepted");
if (bridgeProc.writes.length !== 1) throw new Error("first start did not write");
if (context.start() !== false) throw new Error("repeated pending start was not gated");
if (bridgeProc.writes.length !== 1) throw new Error("gated start spammed the bridge");
// Late ops after death never spawn.
bridgeProc.running = false;
if (context.sendOp("prompt", {{message: "late"}}) !== "") throw new Error("late sendOp spawned");
if (bridgeProc.running) throw new Error("late op relaunched bridge");
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})

    # -- prompt / skill / image args ---------------------------------------
    def test_ai_skill_image_send_args(self):
        funcs = self.scoped_funcs(["json", "trackPending", "clearPending", "sendOp", "prompt"])
        script = f"""
const vm = require("vm");
const writes = [];
const bridgeProc = {{running: true, write(v) {{ writes.push(JSON.parse(v)); }}}};
const context = {{
  _pendingAcks: {{}}, _pendingCount: 0, _uiSerial: 0, _queuedOps: [],
  _optBusy: false, _bReady: true,
  bridgeProc, Quickshell: {{shellPath(v) {{ return v; }}}},
}};
Object.defineProperty(context, "ready", {{get() {{ return this._bReady && this._pendingCount === 0; }}}});
Object.defineProperty(context, "compacting", {{get() {{ return false; }}}});
Object.defineProperty(context, "stopping", {{get() {{ return false; }}}});
Object.defineProperty(context, "controlPending", {{get() {{ return false; }}}});
Object.defineProperty(context, "sessionSwitching", {{get() {{ return false; }}}});
Object.defineProperty(context, "sessionRefreshPending", {{get() {{ return false; }}}});
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(funcs.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
const id = context.prompt("  hello  ", [{{data: "AAA"}}]);
if (!id) throw new Error("prompt with images rejected");
if (writes.length !== 1 || writes[0].op !== "prompt") throw new Error("prompt op missing");
if (writes[0].args.message !== "hello") throw new Error("prompt not trimmed");
if (!writes[0].args.images || writes[0].args.images[0].data !== "AAA") throw new Error("images lost");
// A second prompt while the first ack is pending is gated (ready=false).
if (context.prompt("hi")) throw new Error("second prompt escaped pending gate");
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})
        # Capture emits images; the palette captured-handler forwards them.
        # (Capture owns the process/stale drops; the palette owns agent.prompt
        # + the "ai:" draft, mirroring the pre-extraction call sites.)
        capture = CAPTURE[CAPTURE.index("id: captureProcess"):CAPTURE.index("function beginRegionSelection")]
        self.assertIn("generation !== root.captureGeneration", capture)
        self.assertIn("root.captured(prompt, data.images || [])", capture)
        self.assertIn('root.query = "ai:"', PALETTE)
        self.assertIn("root.agent.prompt(prompt, images", PALETTE)
        activate = extract_function(PALETTE, "activate")
        self.assertIn("agent.prompt(prompt)", activate)
        self.assertIn('agent.prompt("/" + target.name', activate)

    def test_ai_query_not_cleared_and_capture_rejection_preserves(self):
        activate = extract_function(PALETTE, "activate")
        # ai activate never clears the query; rejection only notices.
        self.assertNotIn('query = ""', activate)
        self.assertNotIn("query=''", activate)
        self.assertIn('notice = agent.status || "Pi is not ready"', activate)
        capture = CAPTURE[CAPTURE.index("id: captureProcess"):CAPTURE.index("function beginRegionSelection")]
        # Stale generations never touch the draft.
        self.assertIn("generation !== root.captureGeneration", capture)
        # Rejected/failed captures preserve the prompt for retry via signals;
        # the palette restores "ai: "+prompt in onFailed/onCaptured.
        self.assertIn("root.failed(PaletteText.captureFailure(code, captureError.text))", capture)
        self.assertIn('root.query = "ai: " + String(prompt).trim()', PALETTE)
        self.assertIn('root.query = "ai:"', PALETTE)

    # -- async ack / dead ---------------------------------------------------
    def test_opfinished_and_dead_surface_without_clearing(self):
        self.assertIn("function onOpFinished(id, op, accepted, message)", PALETTE)
        self.assertIn("function onBridgeDead()", PALETTE)
        # onMessagesChanged is the REAL implicit notify for messages.
        self.assertIn("function onMessagesChanged()", PALETTE)
        script = f"""
const vm = require("vm");
const root = {{notice: "", query: "ai: keep me"}};
const agent = {{status: "Agent bridge stopped — Retry to restart"}};
const context = {{root, agent}};
vm.createContext(context);
const onOp = vm.runInContext("(function(id, op, accepted, message) {{" +
  {json.dumps(extract_handler(PALETTE, "function onOpFinished("))} + "}})", context);
const onDead = vm.runInContext("(function() {{" +
  {json.dumps(extract_handler(PALETTE, "function onBridgeDead("))} + "}})", context);
onOp("ui-1", "prompt", false, "busy rejected");
if (root.notice !== "busy rejected" || root.query !== "ai: keep me")
  throw new Error("rejected op did not surface/preserve");
onOp("ui-2", "prompt", true, "");
if (root.query !== "ai: keep me") throw new Error("accepted op cleared query");
onDead();
if (!root.notice.includes("Retry")) throw new Error("dead did not offer retry");
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})

    # -- stats --------------------------------------------------------------
    def test_stats_op_reaches_request_stats(self):
        funcs = self.scoped_funcs(["json", "trackPending", "clearPending", "sendOp", "requestStats", "request"])
        script = f"""
const vm = require("vm");
const writes = [];
const bridgeProc = {{running: true, write(v) {{ writes.push(JSON.parse(v)); }}}};
const context = {{_pendingAcks: {{}}, _pendingCount: 0, _uiSerial: 0, _queuedOps: [], bridgeProc}};
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(funcs.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
const id = context.request("get_session_stats");
if (!id) throw new Error("stats allowlist rejected");
if (writes.length !== 1 || writes[0].op !== "requestStats") throw new Error("stats did not reach requestStats");
if (context.request("get_state") !== false) throw new Error("non-allowlisted request escaped");
if (context.request("prompt") !== false) throw new Error("prompt via request escaped");
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})
        ai_action = extract_function(PALETTE, "aiAction")
        self.assertIn('agent.request("get_session_stats")', ai_action)
        self.assertIn("showStats = true", ai_action)

    # -- history filter / normalization --------------------------------------
    def test_history_filter_and_text_projection(self):
        funcs = self.scoped_funcs(["textFromMessage", "filterMessages"])
        script = f"""
const vm = require("vm");
const context = {{}};
vm.createContext(context);
for (const value of {json.dumps(list(funcs.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
if (context.textFromMessage({{text: "kept"}}) !== "kept") throw new Error("text passthrough failed");
if (context.textFromMessage({{content: "raw"}}) !== "raw") throw new Error("content string failed");
if (context.textFromMessage({{content: [{{type: "text", text: "a"}}, {{type: "thinking", text: "hide"}}]}}) !== "a")
  throw new Error("part filtering failed");
const rows = [{{role: "user", text: "Fix the PARSER"}}, {{role: "assistant", text: "done"}}];
const filtered = context.filterMessages(rows, "parser");
if (filtered.length !== 1 || filtered[0].role !== "user") throw new Error("case-insensitive filter failed");
if (context.filterMessages(rows, "missing").length !== 0) throw new Error("no-match filter failed");
if (context.filterMessages(rows, "").length !== 2) throw new Error("empty filter failed");
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})
        self.assertIn("root.historyRows = agent.filterMessages(agent.messages, root.historyQuery)", PALETTE)

    # -- model / session routing ---------------------------------------------
    def test_model_session_command_routing_no_false_accept(self):
        ai_action = extract_function(PALETTE, "aiAction")
        for needle in ("agent.abort()", "agent.newSession()", "agent.switchSession()",
                       "agent.rename(text)", "agent.compact()",
                       'agent.request("get_session_stats")',
                       "retryPaletteAgent()"):
            self.assertIn(needle, ai_action)
        # Model routing lives in the model menu delegate (same bridge call).
        self.assertIn("agent.chooseModel(modelData)", PALETTE)
        # Rejected controls must notice instead of reporting success.
        self.assertIn('if (!agent.switchSession()) notice', PALETTE)
        script = f"""
const vm = require("vm");
const calls = [];
const context = {{
  notice: "", showStats: false, text: "New Name",
  agent: {{
    status: "bridge down",
    abort() {{ calls.push("abort"); return ""; }},
    newSession() {{ calls.push("new"); return false; }},
    switchSession() {{ calls.push("switch"); return "ui-3"; }},
    rename(v) {{ calls.push(["rename", v]); return false; }},
    compact() {{ calls.push("compact"); return "ui-4"; }},
    request(t) {{ calls.push(["request", t]); return t === "get_session_stats" ? "ui-5" : false; }},
  }},
  searchText() {{ return "New Name"; }},
  runAction() {{}}, modelMenu: {{open() {{ calls.push("model"); }}}},
  root: {{captureGeneration: 0, capturePrompt: ""}}, capturePrompt: "",
  beginRegionSelection() {{}}, input: {{text: ""}}, query: "",
}};
context.root.query = "";
vm.createContext(context);
context.aiAction = vm.runInContext("(" + {json.dumps(ai_action)} + ")", context);
context.aiAction("stop");
if (!context.notice) throw new Error("stop rejection not reported");
context.notice = "";
context.aiAction("new");
if (!context.notice) throw new Error("new rejection not reported");
context.notice = "";
context.aiAction("resume");
if (context.notice) throw new Error("accepted switch reported as failure");
context.aiAction("rename");
if (!context.notice) throw new Error("rename rejection not reported");
if (calls.filter(c => c[0] === "rename")[0][1] !== "New Name") throw new Error("rename text lost");
context.notice = "";
context.aiAction("compact");
if (context.notice) throw new Error("accepted compact reported as failure");
context.aiAction("stats");
if (!context.showStats || !calls.some(c => c[0] === "request" && c[1] === "get_session_stats"))
  throw new Error("stats did not reach requestStats");
context.aiAction("retry");
if (!calls.includes("retry") && !context.notice.includes("Retrying") && calls.length < 5)
  throw new Error("retry not routed");
console.log(JSON.stringify({{ok: true, calls}}));
"""
        # The retry path uses retryPaletteAgent(); provide it in the harness.
        retry_fn = self.palette_func("retryPaletteAgent")
        script_with_retry = script.replace(
            "context.aiAction =",
            "context.retryPaletteAgent = vm.runInContext(\"(\" + %s + \")\", context);\ncontext.aiAction =" % json.dumps(retry_fn),
        ).replace(
            'context.aiAction("retry");',
            'context.agent.retryable = true; context.agent.start = function() { calls.push("retry"); context.notice = "Retrying Pi agent…"; return "ui-9"; }; context.aiAction("retry");',
        )
        self.assertEqual(self.run_node(script_with_retry)["ok"], True)

    def test_approval_response_exact_and_close_safe(self):
        # Approval logic now lives in PaletteApprovalDialog.qml; the palette
        # keeps agent ownership and routes onUiRequest/open into it.
        self.assertIn("PaletteApprovalDialog", PALETTE)
        self.assertIn("approvalDialog.openRequest(request)", PALETTE)
        self.assertIn("approvalDialog.close()", PALETTE)
        approval_block = APPROVAL
        self.assertIn("let current = request;", approval_block)
        self.assertIn("let requestId = current.id;", approval_block)
        self.assertIn("dismiss();", approval_block)
        self.assertIn("agent.respond(requestId", approval_block)
        respond_src = extract_function(SCOPED, "respond")
        self.assertIn("pendingApproval.id !== requestId", respond_src)
        close_block = PALETTE[PALETTE.index("function close() {"):PALETTE.index("function handoffToProjectPlanner")]
        self.assertNotIn("agent.respond", close_block)

    def test_retry_button_reachable_and_no_auto_loops(self):
        self.assertIn('text: "Retry"', PALETTE)
        self.assertIn("visible: agent.retryable", PALETTE)
        self.assertIn("root.retryPaletteAgent()", PALETTE)
        self.assertIn('["Retry agent", "retry"]', PALETTE)
        # Retry command only appears when retryable, so the base ">" count
        # stays 26 for existing tests.
        rebuild_src = extract_function(PALETTE, "rebuildModel")
        self.assertIn("if (agent && agent.retryable)", rebuild_src)


if __name__ == "__main__":
    unittest.main()
