"""ScopedAgent bridge-death / async-ack UI behaviour (no model/graph).

Exercises the REAL QML JS in widgets/ScopedAgent.qml,
widgets/ProjectPlanner.qml and widgets/JournalAssistant.qml through node.
No pi/model/notes are touched.
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
PLANNER = (ROOT / "widgets" / "ProjectPlanner.qml").read_text(encoding="utf-8")
JOURNAL = (ROOT / "widgets" / "JournalAssistant.qml").read_text(encoding="utf-8")


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
    "json", "buildErrorMessage", "bridgePath", "bridgeArgs",
    "trackPending", "clearPending", "dropPendingNoReplay",
    "refreshSpawnIdentity", "sendOp", "start", "stopIdle", "prompt",
    "abort", "newSession", "switchSession", "rename", "compact",
    "chooseModel", "respond", "requestMessages", "request",
    "applyState", "handleBridgeDead", "handleFailedToStart", "handleUpdate",
]


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class ScopedAgentUiTests(unittest.TestCase):
    def run_node(self, script):
        completed = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=8)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        return json.loads(completed.stdout.splitlines()[-1])

    def scoped_funcs(self):
        return {name: extract_function(SCOPED, name) for name in SCOPED_FNS}

    def test_static_contracts_lazy_retryable_and_ack_order(self):
        # LAZY launch: Component.onCompleted must not spawn the bridge.
        completed_block = SCOPED[SCOPED.index("Component.onCompleted:"):SCOPED.index("Component.onDestruction:")]
        self.assertNotIn("bridgeProc.running = true", completed_block)
        self.assertNotIn("bridgeProc.command", completed_block)
        # Explicit start is the only spawner and refreshes identity.
        start_src = extract_function(SCOPED, "start")
        self.assertIn("refreshSpawnIdentity()", start_src)
        self.assertIn("bridgeProc.command = bridgeArgs()", start_src)
        self.assertIn("bridgeProc.running = true", start_src)
        # sendOp never launches for late ops.
        send_src = extract_function(SCOPED, "sendOp")
        self.assertIn("if (!bridgeProc.running) return", send_src)
        self.assertNotIn("bridgeProc.running = true", send_src)
        # Actionable complete build command.
        self.assertIn(
            "cargo build --locked --release --manifest-path services/agent-orchestrator/Cargo.toml",
            SCOPED,
        )
        # Ack order: state before ack before signals.
        apply_at = SCOPED.index("applyState(value.state)")
        ack_at = SCOPED.index("clearPending(value.ack)")
        text_at = SCOPED.index('if (event.name === "textDelta")')
        self.assertLess(apply_at, ack_at)
        self.assertLess(apply_at, text_at)
        self.assertIn("ready: _bReady && _pendingCount === 0", SCOPED)
        self.assertIn("signal opFinished(", SCOPED)
        self.assertIn("signal bridgeDead(", SCOPED)
        # HIGH: bridge Process tracking per launch, never the Pi projection.
        self.assertIn("property bool _bridgeProcessStarted", SCOPED)
        self.assertIn("property bool _bridgeExitHandled", SCOPED)
        on_started = extract_handler(SCOPED, "onStarted:")
        self.assertIn("root._bridgeProcessStarted = true", on_started)
        running_changed = extract_handler(SCOPED, "onRunningChanged:")
        self.assertIn("_bridgeProcessStarted", running_changed)
        self.assertNotIn("!root.processStarted", running_changed)
        on_exited = extract_handler(SCOPED, "onExited:")
        self.assertIn("_bridgeExitHandled", on_exited)
        self.assertIn("_bridgeProcessStarted", on_exited)
        self.assertIn("handleBridgeDead()", on_exited)
        self.assertIn("handleFailedToStart()", on_exited)

    def test_bridge_dead_clears_gates_preserves_authoritative_and_requires_retry(self):
        funcs = self.scoped_funcs()
        script = f"""
const vm = require("vm");
const signals = {{opFinished: [], uiRequest: [], failed: [], bridgeDead: 0, historyFailed: []}};
const bridgeProc = {{running: false, command: [], writes: [], write(v) {{ this.writes.push(JSON.parse(v)); }}}};
const context = {{
  projectPath: "pages/A.md", journalMode: false,
  _initSession: "", _initName: "", _initFresh: false, _bridgeLaunched: true,
  _pendingAcks: {{"ui-1": "prompt"}}, _pendingCount: 1, _uiSerial: 1, _queuedOps: [{{id: "ui-1", payload: {{}}}}],
  _optBusy: true, _optControl: false, _optSwitching: false, _optCompacting: false, _optStopping: true, _optRefresh: false,
  _bReady: true, _bBusy: true, _bCompacting: false, _bControl: false, _bSwitching: false, _bStopping: true, _bRefresh: false,
  sessionFile: "/scope/A.jsonl", sessionName: "A", freshSession: false,
  model: null, models: [], commands: [], stateOk: true, extensionOk: true,
  sessionRefreshFailed: false, sessionRefreshGeneration: 0,
  sessionChangeInFlight: false, sessionChangeKind: "", sessionChangeRequestId: "", sessionChangeCancelRequested: false,
  answer: "hi", messages: [{{role: "user", text: "hi"}}], messagesSessionFile: "/scope/A.jsonl",
  messagesGeneration: 3, messagesAwaitingSessionState: false,
  status: "Thinking…", pendingApproval: {{id: "appr-1"}}, pendingRequests: {{"x": 1}},
  serial: 1, generation: 1, lastRequestId: "r1",
  processStarted: true, processStartFailed: false, startRequested: true, launchAttempted: true,
  idleStopped: false, pendingResume: false, desiredRunning: true,
  _retryableBase: false, idleStopping: false, diagnostic: "", statsText: "",
  bridgeProc,
  Quickshell: {{shellPath(v) {{ return v; }}}},
  opFinished(id, op, accepted, message) {{ signals.opFinished.push([id, op, accepted, message]); }},
  uiRequest(v) {{ signals.uiRequest.push(v === null ? null : v); }},
  failed(m) {{ signals.failed.push(m); }},
  bridgeDead() {{ signals.bridgeDead++; }},
  historyFailed(m, f, g) {{ signals.historyFailed.push([m, f, g]); }},
}};
Object.defineProperty(context, "ready", {{get() {{ return this._bReady && this._pendingCount === 0; }}}});
Object.defineProperty(context, "busy", {{get() {{ return this._bBusy || this._optBusy; }}}});
Object.defineProperty(context, "retryable", {{get() {{ return this._retryableBase; }}}});
Object.defineProperty(context, "sessionRefreshPending", {{get() {{ return this._bRefresh || this._optRefresh; }}}});
Object.defineProperty(context, "sessionSwitching", {{get() {{ return this._bSwitching || this._optSwitching; }}}});
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(funcs.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
context.handleBridgeDead();
if (context._bReady || context._bBusy || context._bControl || context._bStopping) throw new Error("live gates survived death");
if (context._optBusy || context._optControl || context._optStopping || context._optRefresh) throw new Error("optimistic gates survived death");
if (context._pendingCount !== 0 || Object.keys(context._pendingAcks).length) throw new Error("pending survived death");
if (context.pendingApproval !== null) throw new Error("approval survived death");
if (context._queuedOps.length) throw new Error("queue replayed after death");
if (!context.retryable || !context.processStartFailed) throw new Error("death did not become retryable");
if (context.processStarted || context.desiredRunning) throw new Error("process flags survived death");
if (context.sessionFile !== "/scope/A.jsonl" || context.sessionName !== "A") throw new Error("authoritative cache was not preserved");
if (signals.opFinished.length !== 1 || signals.opFinished[0][0] !== "ui-1" || signals.opFinished[0][2] !== false) throw new Error("pending was not rejected via opFinished");
if (signals.uiRequest.length !== 1 || signals.uiRequest[0] !== null) throw new Error("uiRequest(null) missing");
if (!signals.failed.length || signals.bridgeDead !== 1) throw new Error("death did not notify");
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})

    def test_bridge_dead_clears_identity_when_refresh_unresolved(self):
        funcs = self.scoped_funcs()
        script = f"""
const vm = require("vm");
const signals = {{opFinished: [], uiRequest: [], failed: [], bridgeDead: 0}};
const bridgeProc = {{running: false, command: [], write(v) {{}}}};
const context = {{
  _pendingAcks: {{}}, _pendingCount: 0, _uiSerial: 0, _queuedOps: [],
  _optBusy: false, _optControl: true, _optSwitching: true, _optCompacting: false, _optStopping: false, _optRefresh: true,
  _bReady: false, _bBusy: false, _bCompacting: false, _bControl: false, _bSwitching: true, _bStopping: false, _bRefresh: true,
  sessionFile: "/scope/B.jsonl", sessionName: "B", freshSession: false,
  model: null, models: [], commands: [], stateOk: true, extensionOk: true,
  sessionRefreshFailed: false, sessionRefreshGeneration: 1,
  sessionChangeInFlight: true, sessionChangeKind: "restore", sessionChangeRequestId: "r", sessionChangeCancelRequested: false,
  answer: "", messages: [{{role: "user", text: "old"}}], messagesSessionFile: "/scope/B.jsonl",
  messagesGeneration: 2, messagesAwaitingSessionState: true,
  status: "Loading…", pendingApproval: null, pendingRequests: {{}},
  serial: 0, generation: 0, lastRequestId: "",
  processStarted: true, processStartFailed: false, startRequested: true, launchAttempted: true,
  idleStopped: false, pendingResume: false, desiredRunning: true,
  _retryableBase: false, idleStopping: false, diagnostic: "", statsText: "",
  projectPath: "pages/B.md", journalMode: false, _initSession: "", _initName: "", _initFresh: false, _bridgeLaunched: true,
  bridgeProc, Quickshell: {{shellPath(v) {{ return v; }}}},
  opFinished() {{}}, uiRequest() {{}}, failed() {{}}, bridgeDead() {{ signals.bridgeDead++; }}, historyFailed() {{}},
}};
Object.defineProperty(context, "retryable", {{get() {{ return this._retryableBase; }}}});
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(funcs.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
context.handleBridgeDead();
if (context.sessionFile !== "" || context.sessionName !== "" || context.messages.length !== 0) throw new Error("unresolved identity was reused");
if (!context.retryable) throw new Error("not retryable");
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})

    def test_no_auto_relaunch_late_ops_and_cached_resume(self):
        funcs = self.scoped_funcs()
        script = f"""
const vm = require("vm");
const bridgeProc = {{running: false, command: [], writes: [], write(v) {{ this.writes.push(JSON.parse(v)); }}}};
const context = {{
  projectPath: "pages/A.md", journalMode: false,
  _initSession: "", _initName: "", _initFresh: false, _bridgeLaunched: true,
  _pendingAcks: {{}}, _pendingCount: 0, _uiSerial: 0, _queuedOps: [],
  _optBusy: false, _optControl: false, _optSwitching: false, _optCompacting: false, _optStopping: false, _optRefresh: false,
  _bReady: false, _bBusy: false, _bCompacting: false, _bControl: false, _bSwitching: false, _bStopping: false, _bRefresh: false,
  sessionFile: "/scope/cached.jsonl", sessionName: "cached", freshSession: false,
  model: null, models: [], commands: [], stateOk: false, extensionOk: false,
  sessionRefreshFailed: false, sessionRefreshGeneration: 0,
  sessionChangeInFlight: false, sessionChangeKind: "", sessionChangeRequestId: "", sessionChangeCancelRequested: false,
  answer: "", messages: [], messagesSessionFile: "", messagesGeneration: 0, messagesAwaitingSessionState: false,
  status: "Agent bridge stopped — Retry to restart", pendingApproval: null, pendingRequests: {{}},
  serial: 0, generation: 0, lastRequestId: "",
  processStarted: false, processStartFailed: true, startRequested: false, launchAttempted: true,
  idleStopped: false, pendingResume: false, desiredRunning: false,
  _retryableBase: true, idleStopping: false, diagnostic: "", statsText: "",
  bridgeProc, Quickshell: {{shellPath(v) {{ return v; }}}},
  opFinished() {{}}, uiRequest() {{}}, failed() {{}}, bridgeDead() {{}}, historyFailed() {{}},
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
// Late ops must not relaunch or queue.
const launchesBefore = bridgeProc.writes.length;
if (context.prompt("late hello")) throw new Error("late prompt relaunched");
if (context.respond("appr-1", {{}}) !== false) throw new Error("late respond relaunched");
if (context.requestMessages() !== false) throw new Error("late requestMessages relaunched");
if (bridgeProc.running) throw new Error("bridge auto-relaunched");
if (context._queuedOps.length) throw new Error("late op queued for replay");
if (context._optBusy) throw new Error("late prompt left optimistic busy");
// Explicit start resumes with the cached identity frozen into the command.
const id = context.start();
if (!id) throw new Error("explicit start did not return id");
if (!bridgeProc.running) throw new Error("explicit start did not launch");
if (context._initSession !== "/scope/cached.jsonl" || context._initName !== "cached") throw new Error("cached identity was not refreshed");
if (!JSON.stringify(bridgeProc.command).includes("/scope/cached.jsonl")) throw new Error("frozen command missed cached session");
if (context._queuedOps.length !== 1) throw new Error("start op was not queued");
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})

    def test_immediate_gates_survive_unrelated_snapshots_until_ack(self):
        funcs = self.scoped_funcs()
        script = f"""
const vm = require("vm");
const bridgeProc = {{running: true, write(v) {{}}}};
const context = {{
  projectPath: "pages/A.md", journalMode: false, _initSession: "", _initName: "", _initFresh: false, _bridgeLaunched: true,
  _pendingAcks: {{}}, _pendingCount: 0, _uiSerial: 0, _queuedOps: [],
  _optBusy: false, _optControl: false, _optSwitching: false, _optCompacting: false, _optStopping: false, _optRefresh: false,
  _bReady: true, _bBusy: false, _bCompacting: false, _bControl: false, _bSwitching: false, _bStopping: false, _bRefresh: false,
  sessionFile: "A", sessionName: "A", freshSession: false,
  model: null, models: [], commands: [], stateOk: true, extensionOk: true,
  sessionRefreshFailed: false, sessionRefreshGeneration: 0,
  sessionChangeInFlight: false, sessionChangeKind: "", sessionChangeRequestId: "", sessionChangeCancelRequested: false,
  answer: "", messages: [], messagesSessionFile: "", messagesGeneration: 0, messagesAwaitingSessionState: false,
  status: "Ready", pendingApproval: null, pendingRequests: {{}},
  serial: 0, generation: 0, lastRequestId: "",
  processStarted: true, processStartFailed: false, startRequested: true, launchAttempted: true,
  idleStopped: false, pendingResume: false, desiredRunning: true,
  _retryableBase: false, idleStopping: false, diagnostic: "", statsText: "",
  bridgeProc, Quickshell: {{shellPath(v) {{ return v; }}}},
  opFinished() {{}}, uiRequest() {{}}, failed() {{}}, bridgeDead() {{}}, historyFailed() {{}},
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
const pid = context.prompt("hello");
if (!pid) throw new Error("prompt was not accepted");
if (!context.busy) throw new Error("immediate busy gate missing");
// An unrelated snapshot (no busy) must not clear the optimistic gate.
context.handleUpdate({{version: 1, type: "update", ack: null, events: [],
  state: {{ready: true, busy: false, sessionFile: "A", sessionName: "A", status: "Thinking…",
    processStarted: true, desiredRunning: true, retryable: false}}}});
if (!context.busy) throw new Error("optimistic busy cleared by unrelated snapshot");
// The ack clears it.
context.handleUpdate({{version: 1, type: "update", ack: pid, accepted: true, events: [],
  state: {{ready: true, busy: true, sessionFile: "A", sessionName: "A", status: "Thinking…",
    processStarted: true, desiredRunning: true, retryable: false}}}});
if (context.busy !== true) throw new Error("bridge busy was not projected after ack");
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})

    def test_ack_reject_emits_opfinished_and_historyfailed(self):
        funcs = self.scoped_funcs()
        script = f"""
const vm = require("vm");
const signals = {{opFinished: [], failed: [], historyFailed: []}};
const bridgeProc = {{running: true, write(v) {{}}}};
const context = {{
  projectPath: "pages/A.md", journalMode: false, _initSession: "", _initName: "", _initFresh: false, _bridgeLaunched: true,
  _pendingAcks: {{}}, _pendingCount: 0, _uiSerial: 0, _queuedOps: [],
  _optBusy: false, _optControl: false, _optSwitching: false, _optCompacting: false, _optStopping: false, _optRefresh: false,
  _bReady: true, _bBusy: false, _bCompacting: false, _bControl: false, _bSwitching: false, _bStopping: false, _bRefresh: false,
  sessionFile: "A", sessionName: "A", freshSession: false,
  model: null, models: [], commands: [], stateOk: true, extensionOk: true,
  sessionRefreshFailed: false, sessionRefreshGeneration: 0,
  sessionChangeInFlight: false, sessionChangeKind: "", sessionChangeRequestId: "", sessionChangeCancelRequested: false,
  answer: "", messages: [], messagesSessionFile: "", messagesGeneration: 7, messagesAwaitingSessionState: false,
  status: "Ready", pendingApproval: null, pendingRequests: {{}},
  serial: 0, generation: 0, lastRequestId: "",
  processStarted: true, processStartFailed: false, startRequested: true, launchAttempted: true,
  idleStopped: false, pendingResume: false, desiredRunning: true,
  _retryableBase: false, idleStopping: false, diagnostic: "", statsText: "",
  bridgeProc, Quickshell: {{shellPath(v) {{ return v; }}}},
  opFinished(id, op, accepted, message) {{ signals.opFinished.push([id, op, accepted, message]); }},
  uiRequest() {{}}, failed(m) {{ signals.failed.push(m); }}, bridgeDead() {{}}, historyFailed(m, f, g) {{ signals.historyFailed.push([m, f, g]); }},
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
const pid = context.prompt("hello");
context.handleUpdate({{version: 1, type: "update", ack: pid, accepted: false,
  events: [{{name: "failed", args: ["busy rejected"]}}],
  state: {{ready: true, busy: false, sessionFile: "A", sessionName: "A", status: "busy rejected",
    processStarted: true, desiredRunning: true, retryable: false}}}});
if (signals.opFinished.length !== 1 || signals.opFinished[0][2] !== false) throw new Error("prompt reject missing opFinished(false)");
if (context.busy) throw new Error("rejected prompt left busy gate");
const rid = context.requestMessages();
if (!rid) throw new Error("requestMessages was not accepted");
context.handleUpdate({{version: 1, type: "update", ack: rid, accepted: false, events: [],
  state: {{ready: true, busy: false, sessionFile: "A", sessionName: "A", messagesGeneration: 7, status: "history boom",
    processStarted: true, desiredRunning: true, retryable: false}}}});
if (signals.historyFailed.length !== 1 || signals.historyFailed[0][1] !== "A" || signals.historyFailed[0][2] !== 7)
  throw new Error("requestMessages reject did not emit historyFailed identity");
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})

    def test_stop_sets_stopping_immediately_and_requestmessages_guarded(self):
        funcs = self.scoped_funcs()
        script = f"""
const vm = require("vm");
const bridgeProc = {{running: true, writes: [], write(v) {{ this.writes.push(JSON.parse(v)); }}}};
const context = {{
  projectPath: "pages/A.md", journalMode: false, _initSession: "", _initName: "", _initFresh: false, _bridgeLaunched: true,
  _pendingAcks: {{}}, _pendingCount: 0, _uiSerial: 0, _queuedOps: [],
  _optBusy: true, _optControl: false, _optSwitching: false, _optCompacting: false, _optStopping: false, _optRefresh: false,
  _bReady: false, _bBusy: true, _bCompacting: false, _bControl: false, _bSwitching: false, _bStopping: false, _bRefresh: false,
  sessionFile: "A", sessionName: "A", freshSession: false,
  model: null, models: [], commands: [], stateOk: true, extensionOk: true,
  sessionRefreshFailed: false, sessionRefreshGeneration: 0,
  sessionChangeInFlight: false, sessionChangeKind: "", sessionChangeRequestId: "", sessionChangeCancelRequested: false,
  answer: "", messages: [], messagesSessionFile: "", messagesGeneration: 0, messagesAwaitingSessionState: false,
  status: "Thinking…", pendingApproval: null, pendingRequests: {{}},
  serial: 0, generation: 0, lastRequestId: "",
  processStarted: true, processStartFailed: false, startRequested: true, launchAttempted: true,
  idleStopped: false, pendingResume: false, desiredRunning: true,
  _retryableBase: false, idleStopping: false, diagnostic: "", statsText: "",
  bridgeProc, Quickshell: {{shellPath(v) {{ return v; }}}},
  opFinished() {{}}, uiRequest() {{}}, failed() {{}}, bridgeDead() {{}}, historyFailed() {{}},
}};
Object.defineProperty(context, "stopping", {{get() {{ return this._bStopping || this._optStopping; }}}});
Object.defineProperty(context, "busy", {{get() {{ return this._bBusy || this._optBusy; }}}});
Object.defineProperty(context, "retryable", {{get() {{ return this._retryableBase; }}}});
Object.defineProperty(context, "controlPending", {{get() {{ return this._bControl || this._optControl; }}}});
Object.defineProperty(context, "sessionSwitching", {{get() {{ return this._bSwitching || this._optSwitching; }}}});
Object.defineProperty(context, "sessionRefreshPending", {{get() {{ return this._bRefresh || this._optRefresh; }}}});
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(funcs.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
context.abort();
if (!context.stopping) throw new Error("Stop did not set stopping immediately");
if (context.requestMessages() !== false) throw new Error("requestMessages escaped stopping guard");
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})

    def test_failed_to_start_is_actionable(self):
        funcs = self.scoped_funcs()
        script = f"""
const vm = require("vm");
const signals = {{failed: [], ui: 0, dead: 0, ack: []}};
const bridgeProc = {{running: false, command: [], write(v) {{}}}};
const context = {{
  projectPath: "pages/A.md", journalMode: false, _initSession: "", _initName: "", _initFresh: false, _bridgeLaunched: true,
  _pendingAcks: {{}}, _pendingCount: 0, _uiSerial: 0, _queuedOps: [],
  _optBusy: false, _optControl: false, _optSwitching: false, _optCompacting: false, _optStopping: false, _optRefresh: false,
  _bReady: false, _bBusy: false, _bCompacting: false, _bControl: false, _bSwitching: false, _bStopping: false, _bRefresh: false,
  sessionFile: "", sessionName: "", freshSession: false,
  model: null, models: [], commands: [], stateOk: false, extensionOk: false,
  sessionRefreshFailed: false, sessionRefreshGeneration: 0,
  sessionChangeInFlight: false, sessionChangeKind: "", sessionChangeRequestId: "", sessionChangeCancelRequested: false,
  answer: "", messages: [], messagesSessionFile: "", messagesGeneration: 0, messagesAwaitingSessionState: false,
  status: "Starting pi…", pendingApproval: null, pendingRequests: {{}},
  serial: 0, generation: 0, lastRequestId: "",
  processStarted: false, processStartFailed: false, startRequested: true, launchAttempted: true,
  idleStopped: false, pendingResume: false, desiredRunning: false,
  _retryableBase: false, idleStopping: false, diagnostic: "", statsText: "",
  bridgeProc, Quickshell: {{shellPath(v) {{ return v; }}}},
  opFinished(id, op, a, m) {{ signals.ack.push([id, a]); }},
  uiRequest(v) {{ signals.ui++; }}, failed(m) {{ signals.failed.push(m); }}, bridgeDead() {{ signals.dead++; }}, historyFailed() {{}},
}};
Object.defineProperty(context, "retryable", {{get() {{ return this._retryableBase; }}}});
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(funcs.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
context.handleFailedToStart();
if (!context.retryable || !context.processStartFailed) throw new Error("not retryable");
if (!signals.failed.length || !signals.failed[0].includes("cargo build --locked --release --manifest-path services/agent-orchestrator/Cargo.toml"))
  throw new Error("build command not actionable: " + JSON.stringify(signals.failed));
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})

    def test_project_prompt_preserved_until_accepted_ack(self):
        fns = {
            name: extract_function(PLANNER, name)
            for name in ("draftFor", "setDraft", "handlePromptAck", "handleRenameAck")
        }
        script = f"""
const vm = require("vm");
const workerA = {{id: "A"}};
const workerB = {{id: "B"}};
const context = {{
  drafts: {{"pages/A.md": "first draft"}}, agentCache: {{"pages/A.md": workerA, "pages/B.md": workerB}},
  selectedAgent: workerA, notice: "", pendingPrompt: null, pendingRename: null,
  renameDraft: "", renameDialogOpen: false,
}};
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
// Submit without clearing.
context.pendingPrompt = {{worker: workerA, path: "pages/A.md", text: "first draft", id: "ui-9"}};
if (context.drafts["pages/A.md"] !== "first draft") throw new Error("draft cleared before ack");
// Rejected preserves.
context.handlePromptAck("ui-9", "prompt", false, "busy rejected");
if (context.drafts["pages/A.md"] !== "first draft" || context.notice !== "busy rejected") throw new Error("rejected draft lost");
// Resubmit then edit before ack: new edits win.
context.pendingPrompt = {{worker: workerA, path: "pages/A.md", text: "first draft", id: "ui-10"}};
context.drafts["pages/A.md"] = "first draft edited";
context.handlePromptAck("ui-10", "prompt", true, "");
if (context.drafts["pages/A.md"] !== "first draft edited") throw new Error("new edits overwritten");
// Unchanged draft clears for the originating project only.
context.drafts["pages/A.md"] = "second";
context.pendingPrompt = {{worker: workerA, path: "pages/A.md", text: "second", id: "ui-11"}};
context.selectedAgent = workerB;
context.handlePromptAck("ui-11", "prompt", true, "");
if (context.drafts["pages/A.md"] !== "") throw new Error("originating draft was not cleared");
// Wrong id is ignored.
context.drafts["pages/A.md"] = "keep";
context.pendingPrompt = {{worker: workerA, path: "pages/A.md", text: "keep", id: "ui-12"}};
context.handlePromptAck("ui-99", "prompt", true, "");
if (!context.pendingPrompt || context.drafts["pages/A.md"] !== "keep") throw new Error("wrong id cleared pending");
// Rename preserves on reject.
context.pendingRename = {{worker: workerA, name: "Try", id: "ui-20"}};
context.renameDraft = "Try"; context.renameDialogOpen = false;
context.selectedAgent = workerA;
context.handleRenameAck("ui-20", "rename", false, "denied");
if (context.renameDraft !== "Try" || !context.renameDialogOpen) throw new Error("rename rejected destroyed name");
context.pendingRename = {{worker: workerA, name: "Try2", id: "ui-21"}};
context.renameDraft = "Try2"; context.renameDialogOpen = false;
context.handleRenameAck("ui-21", "rename", true, "");
if (context.renameDraft !== "" || context.renameDialogOpen) throw new Error("rename accept did not close");
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})

    def test_journal_thought_preserved_until_accepted_ack(self):
        fns = {
            name: extract_function(JOURNAL, name)
            for name in ("handlePromptAck", "handleRenameAck")
        }
        script = f"""
const vm = require("vm");
const worker = {{id: "J"}};
const context = {{journalAgent: worker, draft: "my thought", notice: "",
  pendingThought: {{worker, text: "my thought", id: "ui-5"}},
  pendingRename: null, renameDraft: "Try", renameDialogOpen: false}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
context.handlePromptAck("ui-5", "prompt", false, "rejected boom");
if (context.draft !== "my thought") throw new Error("journal draft lost on reject");
context.pendingThought = {{worker, text: "my thought", id: "ui-6"}};
context.draft = "my thought edited";
context.handlePromptAck("ui-6", "prompt", true, "");
if (context.draft !== "my thought edited") throw new Error("journal edit overwritten");
context.draft = "same";
context.pendingThought = {{worker, text: "same", id: "ui-7"}};
context.handlePromptAck("ui-7", "prompt", true, "");
if (context.draft !== "") throw new Error("unchanged journal draft was not cleared");
context.pendingRename = {{worker, name: "Keep", id: "ui-8"}};
context.handleRenameAck("ui-8", "rename", false, "denied");
if (context.renameDraft !== "Keep" || !context.renameDialogOpen) throw new Error("journal rename lost");
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})

    def test_applystate_identity_before_messages_setter_consistency(self):
        apply_src = extract_function(SCOPED, "applyState")
        # Static order: gates/identity/generation before the messages array.
        for marker in ("_bReady", "sessionFile", "messagesSessionFile",
                       "messagesGeneration", "messagesAwaitingSessionState"):
            self.assertLess(apply_src.index(marker), apply_src.index("messages = state.messages"),
                            f"{marker} must be projected before messages")
        # Dynamic: a QML messages setter firing mid-applyState must observe
        # the already-updated identity (no temporary inconsistent state).
        funcs = self.scoped_funcs()
        script = f"""
const vm = require("vm");
const seen = {{}};
const context = {{
  _bReady: false, _bBusy: false, _bCompacting: false, _bControl: false,
  _bSwitching: false, _bStopping: false, _bRefresh: false,
  sessionFile: "old", sessionName: "old", freshSession: false,
  model: null, models: [], commands: [], stateOk: false, extensionOk: false,
  sessionRefreshFailed: false, sessionRefreshGeneration: 0,
  sessionChangeInFlight: false, sessionChangeKind: "", sessionChangeRequestId: "",
  sessionChangeCancelRequested: false, answer: "",
  _messages: [], messagesSessionFile: "old", messagesGeneration: 1,
  messagesAwaitingSessionState: true,
  status: "", pendingApproval: null, pendingRequests: {{}},
  serial: 0, generation: 0, lastRequestId: "",
  processStarted: false, processStartFailed: false, startRequested: false,
  launchAttempted: false, idleStopped: false, pendingResume: false,
  desiredRunning: false, _retryableBase: false, idleStopping: false,
  diagnostic: "", statsText: "",
}};
Object.defineProperty(context, "messages", {{
  get() {{ return this._messages; }},
  set(v) {{
    seen.sessionFile = this.sessionFile;
    seen.messagesSessionFile = this.messagesSessionFile;
    seen.messagesGeneration = this.messagesGeneration;
    seen.awaiting = this.messagesAwaitingSessionState;
    this._messages = v;
  }}
}});
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(funcs.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
context.applyState({{ready: true, sessionFile: "new", sessionName: "n",
  messages: [{{role: "user", text: "hi"}}], messagesSessionFile: "new",
  messagesGeneration: 8, messagesAwaitingSessionState: false, status: "Ready"}});
if (seen.sessionFile !== "new" || seen.messagesSessionFile !== "new" ||
    seen.messagesGeneration !== 8 || seen.awaiting !== false)
  throw new Error("messages setter saw stale identity: " + JSON.stringify(seen));
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})

    def test_project_history_ack_does_not_complete(self):
        # Unrelated ACK/stats snapshots assign a fresh messages array; they
        # must not retire history retry or the restore backup.
        block = PLANNER[PLANNER.index("function onMessagesChanged()"):PLANNER.index("function onUiRequest(request)")]
        self.assertNotIn("finishHistoryRetry", block)
        self.assertNotIn("finishRestoreMessages", block)
        self.assertNotIn("recoverRestoreHistory", block)
        self.assertIn("historyRevision++", block)
        # requestMessages ACK (accepted) carries no history completion.
        fns = {
            name: extract_function(PLANNER, name)
            for name in ("retrySessionHistory", "historySignalMatches",
                         "handleHistoryFailed", "handleHistoryLoaded",
                         "clearHistoryRetryTracking")
        }
        script = f"""
const vm = require("vm");
const worker = {{sessionFile: "A", messagesGeneration: 4, messages: [],
  sessionSwitching: false, sessionRefreshPending: false,
  requestMessages() {{ return "ui-ack-1"; }}}};
const context = {{selectedAgent: worker, selectedPath: "pages/A.md",
  historyLoadError: "", historyRetryPending: false,
  historyRetrySessionFile: "", historyRetryGeneration: 0,
  agentError: "", notice: "", restoreHistoryBackup: null,
  restoreStateSeen: false, restoreStateSessionFile: ""}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
if (!context.retrySessionHistory()) throw new Error("retry not started");
if (!context.historyRetryPending) throw new Error("retry gate missing");
// ACK alone (no historyLoaded) must not clear the gate.
if (context.historyRetryPending !== true) throw new Error("ACK cleared retry");
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})

    def test_project_delayed_failed_retains_backup_until_success(self):
        fns = {
            name: extract_function(PLANNER, name)
            for name in ("historySignalMatches", "boundedHistoryMessage",
                         "handleHistoryFailed", "handleHistoryLoaded",
                         "clearHistoryRetryTracking", "finishRestoreMessages")
        }
        script = f"""
const vm = require("vm");
const worker = {{sessionFile: "A", messages: [], messagesSessionFile: "A",
  messagesGeneration: 4, messagesAwaitingSessionState: false,
  sessionSwitching: false, sessionRefreshPending: false}};
const backupMessages = [{{role: "user", text: "keep this"}}];
const context = {{selectedAgent: worker,
  historyLoadError: "", historyRetryPending: true,
  historyRetrySessionFile: "A", historyRetryGeneration: 4,
  agentError: "", notice: "",
  restoreHistoryBackup: {{worker, sessionFile: "A", messages: backupMessages}},
  restoreStateSeen: true, restoreStateSessionFile: "A"}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
// Delayed failure: gate clears to allow Retry, error shown, but the
// backup is RETAINED (not nulled) and projected for display.
if (!context.handleHistoryFailed("history boom", "A", 4)) throw new Error("correlated failure ignored");
if (context.historyRetryPending) throw new Error("failed retry gate stuck");
if (!context.historyLoadError.includes("boom")) throw new Error("failure message lost");
if (!context.restoreHistoryBackup) throw new Error("backup dropped before success");
if (!worker.messages.length || worker.messages[0].text !== "keep this")
  throw new Error("backup not projected for display");
// Actual success retires the backup and clears the error.
worker.messagesGeneration = 4;
if (!context.handleHistoryLoaded("A", 4)) throw new Error("correlated success ignored");
if (context.restoreHistoryBackup !== null) throw new Error("backup not retired on success");
if (context.historyLoadError) throw new Error("success did not clear error");
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})

    def test_project_stale_history_never_poisons_current(self):
        fns = {
            name: extract_function(PLANNER, name)
            for name in ("historySignalMatches", "boundedHistoryMessage",
                         "handleHistoryFailed", "handleHistoryLoaded",
                         "clearHistoryRetryTracking", "finishRestoreMessages")
        }
        script = f"""
const vm = require("vm");
const worker = {{sessionFile: "B", messages: [], messagesSessionFile: "B",
  messagesGeneration: 9, messagesAwaitingSessionState: false,
  sessionSwitching: false, sessionRefreshPending: false}};
const context = {{selectedAgent: worker,
  historyLoadError: "current error", historyRetryPending: true,
  historyRetrySessionFile: "B", historyRetryGeneration: 9,
  agentError: "", notice: "",
  restoreHistoryBackup: {{worker, sessionFile: "B", messages: [{{role: "user", text: "B"}}]}},
  restoreStateSeen: true, restoreStateSessionFile: "B"}};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
if (context.handleHistoryFailed("stale boom", "A", 4)) throw new Error("stale failure accepted");
if (context.handleHistoryLoaded("A", 4)) throw new Error("stale success accepted");
if (context.historyLoadError !== "current error" || !context.historyRetryPending)
  throw new Error("stale signal poisoned current retry");
if (!context.restoreHistoryBackup) throw new Error("stale signal dropped backup");
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})

    def test_terminal_transport_failure_invalidates_stale_restore(self):
        # Actual Rust sequence: terminal failure emits processStarted:false
        # while Restore refresh is pending with sessionFile still A. The old
        # code classified via the Pi flag as failedToStart and kept A, so
        # Retry resumed stale A. With per-launch bridge tracking the
        # once-started death must reach handleBridgeDead and invalidate A.
        funcs = self.scoped_funcs()
        running_changed = extract_handler(SCOPED, "onRunningChanged:")
        exited = extract_handler(SCOPED, "onExited:")
        script = f"""
const vm = require("vm");
const signals = {{failed: [], ui: [], dead: 0, ack: []}};
const bridgeProc = {{running: true, command: ["qs-agent-orchestrator", "--session", "/scope/A.jsonl"], writes: [],
  write(v) {{ this.writes.push(JSON.parse(v)); }}}};
const context = {{
  projectPath: "pages/A.md", journalMode: false,
  _initSession: "/scope/A.jsonl", _initName: "", _initFresh: false,
  _bridgeLaunched: true, _bridgeProcessStarted: true, _bridgeExitHandled: false,
  _pendingAcks: {{"ui-7": "requestMessages"}}, _pendingCount: 1, _uiSerial: 7,
  _queuedOps: [],
  _optBusy: false, _optControl: false, _optSwitching: true, _optCompacting: false,
  _optStopping: false, _optRefresh: true,
  _bReady: false, _bBusy: false, _bCompacting: false, _bControl: false,
  _bSwitching: true, _bStopping: false, _bRefresh: true,
  sessionFile: "/scope/A.jsonl", sessionName: "A", freshSession: false,
  model: null, models: [], commands: [], stateOk: true, extensionOk: true,
  sessionRefreshFailed: false, sessionRefreshGeneration: 3,
  sessionChangeInFlight: true, sessionChangeKind: "restore",
  sessionChangeRequestId: "r1", sessionChangeCancelRequested: false,
  answer: "", messages: [], messagesSessionFile: "/scope/A.jsonl", messagesGeneration: 4,
  messagesAwaitingSessionState: true,
  status: "Loading…", pendingApproval: null, pendingRequests: {{}},
  serial: 0, generation: 0, lastRequestId: "",
  processStarted: true, processStartFailed: false, startRequested: true,
  launchAttempted: true, idleStopped: false, pendingResume: false,
  desiredRunning: true, _retryableBase: false, idleStopping: false,
  diagnostic: "", statsText: "",
  bridgeProc, Quickshell: {{shellPath(v) {{ return v; }}}},
  opFinished(id, op, a, m) {{ signals.ack.push([id, op, a]); }},
  uiRequest(v) {{ signals.ui.push(v); }},
  failed(m) {{ signals.failed.push(m); }},
  bridgeDead() {{ signals.dead++; }},
  historyFailed() {{}},
}};
Object.defineProperty(context, "retryable", {{get() {{ return this._retryableBase; }}}});
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(funcs.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
const onRunningChanged = vm.runInContext("(function() {{" + {json.dumps(running_changed)} + "}})", context);
const onExited = vm.runInContext("(function(code, status) {{" + {json.dumps(exited)} + "}})", context);
// Terminal Rust snapshot: Pi dead, refresh still pending, file still A.
context.applyState({{ready: false, busy: false, sessionFile: "/scope/A.jsonl", sessionName: "A",
  sessionRefreshPending: true, sessionSwitching: true,
  messages: [], messagesSessionFile: "/scope/A.jsonl", messagesGeneration: 4,
  messagesAwaitingSessionState: true, status: "transport failed",
  processStarted: false, desiredRunning: true, retryable: false}});
// Bridge process exits: runningChanged fires first, then exited.
bridgeProc.running = false;
onRunningChanged();
if (signals.failed.length) throw new Error("runningChanged misclassified once-started death as failedToStart");
if (context.sessionFile !== "/scope/A.jsonl") throw new Error("identity invalidated before exit evidence");
onExited(1, "exit");
if (signals.dead !== 1) throw new Error("once-started death did not reach bridgeDead");
if (context.sessionFile !== "") throw new Error("stale A survived unresolved refresh death");
if (!context.retryable) throw new Error("death not retryable");
// Explicit Retry must not resume stale A.
bridgeProc.running = false;
bridgeProc.command = [];
const nid = context.start();
if (!nid) throw new Error("retry did not return id");
if (JSON.stringify(bridgeProc.command).includes("/scope/A.jsonl"))
  throw new Error("retry resumed stale A: " + JSON.stringify(bridgeProc.command));
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})

    def test_normal_death_keeps_authoritative_session(self):
        funcs = self.scoped_funcs()
        exited = extract_handler(SCOPED, "onExited:")
        script = f"""
const vm = require("vm");
const signals = {{failed: [], dead: 0}};
const bridgeProc = {{running: true, command: [], writes: [], write(v) {{}}}};
const context = {{
  projectPath: "pages/A.md", journalMode: false,
  _initSession: "A", _initName: "", _initFresh: false,
  _bridgeLaunched: true, _bridgeProcessStarted: true, _bridgeExitHandled: false,
  _pendingAcks: {{}}, _pendingCount: 0, _uiSerial: 0, _queuedOps: [],
  _optBusy: false, _optControl: false, _optSwitching: false, _optCompacting: false,
  _optStopping: false, _optRefresh: false,
  _bReady: true, _bBusy: false, _bCompacting: false, _bControl: false,
  _bSwitching: false, _bStopping: false, _bRefresh: false,
  sessionFile: "/scope/A.jsonl", sessionName: "A", freshSession: false,
  model: null, models: [], commands: [], stateOk: true, extensionOk: true,
  sessionRefreshFailed: false, sessionRefreshGeneration: 0,
  sessionChangeInFlight: false, sessionChangeKind: "", sessionChangeRequestId: "",
  sessionChangeCancelRequested: false, answer: "",
  messages: [{{role: "user", text: "hi"}}], messagesSessionFile: "/scope/A.jsonl",
  messagesGeneration: 5, messagesAwaitingSessionState: false,
  status: "Ready", pendingApproval: null, pendingRequests: {{}},
  serial: 0, generation: 0, lastRequestId: "",
  processStarted: true, processStartFailed: false, startRequested: true,
  launchAttempted: true, idleStopped: false, pendingResume: false,
  desiredRunning: true, _retryableBase: false, idleStopping: false,
  diagnostic: "", statsText: "",
  bridgeProc, Quickshell: {{shellPath(v) {{ return v; }}}},
  opFinished() {{}}, uiRequest(v) {{}}, failed(m) {{ signals.failed.push(m); }},
  bridgeDead() {{ signals.dead++; }}, historyFailed() {{}},
}};
Object.defineProperty(context, "retryable", {{get() {{ return this._retryableBase; }}}});
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(funcs.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
const onExited = vm.runInContext("(function(code, status) {{" + {json.dumps(exited)} + "}})", context);
bridgeProc.running = false;
onExited(1, "exit");
if (context.sessionFile !== "/scope/A.jsonl") throw new Error("authoritative session not preserved");
bridgeProc.command = [];
context.start();
if (!JSON.stringify(bridgeProc.command).includes("/scope/A.jsonl"))
  throw new Error("resume did not reuse authoritative session");
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})

    def test_missing_binary_failed_to_start_no_double(self):
        funcs = self.scoped_funcs()
        running_changed = extract_handler(SCOPED, "onRunningChanged:")
        exited = extract_handler(SCOPED, "onExited:")
        script = f"""
const vm = require("vm");
const signals = {{failed: [], dead: 0}};
const bridgeProc = {{running: false, command: [], write(v) {{}}}};
const context = {{
  projectPath: "pages/A.md", journalMode: false,
  _initSession: "", _initName: "", _initFresh: false,
  _bridgeLaunched: true, _bridgeProcessStarted: false, _bridgeExitHandled: false,
  _pendingAcks: {{}}, _pendingCount: 0, _uiSerial: 0, _queuedOps: [],
  _optBusy: false, _optControl: false, _optSwitching: false, _optCompacting: false,
  _optStopping: false, _optRefresh: false,
  _bReady: false, _bBusy: false, _bCompacting: false, _bControl: false,
  _bSwitching: false, _bStopping: false, _bRefresh: false,
  sessionFile: "", sessionName: "", freshSession: false,
  model: null, models: [], commands: [], stateOk: false, extensionOk: false,
  sessionRefreshFailed: false, sessionRefreshGeneration: 0,
  sessionChangeInFlight: false, sessionChangeKind: "", sessionChangeRequestId: "",
  sessionChangeCancelRequested: false, answer: "", messages: [],
  messagesSessionFile: "", messagesGeneration: 0, messagesAwaitingSessionState: false,
  status: "Starting pi…", pendingApproval: null, pendingRequests: {{}},
  serial: 0, generation: 0, lastRequestId: "",
  processStarted: false, processStartFailed: false, startRequested: true,
  launchAttempted: true, idleStopped: false, pendingResume: false,
  desiredRunning: false, _retryableBase: false, idleStopping: false,
  diagnostic: "", statsText: "",
  bridgeProc, Quickshell: {{shellPath(v) {{ return v; }}}},
  opFinished() {{}}, uiRequest(v) {{}}, failed(m) {{ signals.failed.push(m); }},
  bridgeDead() {{ signals.dead++; }}, historyFailed() {{}},
}};
Object.defineProperty(context, "retryable", {{get() {{ return this._retryableBase; }}}});
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(funcs.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
const onRunningChanged = vm.runInContext("(function() {{" + {json.dumps(running_changed)} + "}})", context);
const onExited = vm.runInContext("(function(code, status) {{" + {json.dumps(exited)} + "}})", context);
onRunningChanged();
if (signals.failed.length !== 1) throw new Error("failedToStart did not notify once");
if (!signals.failed[0].includes("cargo build --locked --release --manifest-path services/agent-orchestrator/Cargo.toml"))
  throw new Error("not actionable");
onExited(1, "exit");
if (signals.failed.length !== 1 || signals.dead !== 1) throw new Error("exited doubled the error");
console.log(JSON.stringify({{ok: true}}));
"""
        self.assertEqual(self.run_node(script), {"ok": True})


if __name__ == "__main__":
    unittest.main()
