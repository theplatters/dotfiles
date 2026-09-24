"""Lifecycle tests for the tray overview popup and planner approval handoff.

Unlike the pure/structure tests, these drive the real QML functions
(extracted from the .qml sources) inside a node vm with mock workers,
planner, and helper processes, covering multi-step lifecycles:

- view-only popup (§2.3): open/fetch/recap/summary flows never resolve
  or prompt a project worker;
- isolated recap launch, caching, retry, and evidence chaining;
- project switch mid-fetch and fetch queueing (newest wins, launch
  identity preserved, drain after settle incl. close/reopen/failed-start);
- planner approval handoff ownership and bypass limits.
"""

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
POPUP = (ROOT / "widgets" / "ProjectOverviewPopup.qml").read_text(encoding="utf-8")
PLANNER = (ROOT / "widgets" / "ProjectPlanner.qml").read_text(encoding="utf-8")


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


def run_node(script):
    # The popup harness embeds every extracted QML function in one node
    # script, which exceeds the single-argv limit (MAX_ARG_STRLEN) when
    # passed via `node -e`. Run it from a temp file instead; the
    # executed script is identical.
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write(script)
        path = handle.name
    try:
        completed = subprocess.run(
            ["node", path],
            text=True, capture_output=True,
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


POPUP_FUNCTIONS = [
    "formatDurationMs", "summaryCacheKey", "shortCommit",
    "recapModelFor", "parseOverview", "parseRecap", "shouldAutoSummarize",
    "clampPopupX", "popupWidthFor", "popupHeightFor", "popupTopFor",
    "refreshPosition", "setOpen", "toggle", "openFor",
    "resyncSummaryVisible", "refreshOverview", "drainOverviewQueue",
    "finishOverview", "handleOverviewExited", "handleOverviewRunningChanged",
    "handleOverviewTimeout", "fireOverviewKillTimeout",
    "maybeAutoSummary", "sendRecap", "finishRecap",
    "handleRecapExited", "handleRecapRunningChanged",
    "handleRecapTimeout", "fireRecapKillTimeout", "settleRetiredRecap",
    "parseResumePlan", "resumePlanCommand", "resumeExecuteCommand",
    "resumeAvailableNames", "resumeUnavailableLines", "resumeUnavailableCount",
    "resumeFirstWarning", "validResumeExecutePayload", "resumeExecuteSummary",
    "requestResumePlan", "finishResumePlan", "settleRetiredResumePlan",
    "handleResumePlanExited", "handleResumePlanRunningChanged",
    "handleResumePlanTimeout", "fireResumePlanKillTimeout",
    "cancelResumePreview", "confirmResumeExecute", "finishResumeExecute",
    "settleRetiredResumeExecute", "handleResumeExecuteExited",
    "handleResumeExecuteRunningChanged", "handleResumeExecuteTimeout",
    "fireResumeExecuteKillTimeout", "handoffToProjectPlanner",
    "canRetrySummary", "retrySummary",
    "openFullPlanner", "closePopup",
]

POPUP_SOURCES = {name: extract_function(POPUP, name) for name in POPUP_FUNCTIONS}

MOCKS_JS = """
function mockWorker(id, opts) {
  const w = {
    projectId: id, ready: true, busy: false, compacting: false,
    stopping: false, controlPending: false, sessionSwitching: false,
    sessionRefreshPending: false, messagesAwaitingSessionState: false,
    pendingApproval: null, idleStopped: false, idleStopping: false,
    retryable: false, answer: "", status: "ready",
    sessionFile: "sess", messagesGeneration: 0,
    promptBehavior: "async", promptRid: "rid-1", promptCalls: [],
    prompt(text) { this.promptCalls.push(String(text));
      if (this.promptBehavior === "reject") return "";
      if (this.promptBehavior === "sync") return true;
      this.busy = true; return this.promptRid; },
  };
  Object.assign(w, opts || {});
  return w;
}
function mockPlanner() {
  return {
    workers: {}, agentCalls: [], opened: [],
    agentForId(id) { this.agentCalls.push(id);
      let w = this.workers[id];
      if (!w) { w = mockWorker(id); this.workers[id] = w; }
      if (w.idleStopped || w.idleStopping) {
        w.idleStopped = false; w.idleStopping = false; w.ready = true;
      }
      return w; },
    historyReadyForSend(w) {
      return !!w.ready && !w.sessionRefreshPending && !w.messagesAwaitingSessionState; },
    openProject(id, a, m) { this.opened.push(["openProject", id]); return true; },
    open() { this.opened.push(["open"]); return true; },
  };
}
function loadedOverview(id, key, evidence) {
  ctx.projectId = id; ctx.requestedOpen = true; ctx.closing = false;
  ctx.overviewProjectId = id; ctx.overviewLoaded = true; ctx.overviewError = "";
  ctx.overviewLastSessionId = "ssssssssssssssssssssssssssssssss";
  ctx.overviewLastSession = { session_id: "ssssssssssssssssssssssssssssssss" };
  ctx.overviewChangesAvailable = true;
  ctx.overviewHasBaseline = true;
  ctx.overviewBaselineCommit = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
  ctx.overviewLatestCommit = "cccccccccccccccccccccccccccccccccccccccc";
  ctx.overviewSummaryKey = key; ctx.overviewEvidence = evidence;
  ctx.overviewChangesReason = "";
  ctx.overviewFetchedAt = Date.now();
}
function recapResult(pid, key, summary) {
  return JSON.stringify({ available: true, reason: "",
    project_id: String(pid).toLowerCase(), session_id: ctx.overviewLastSessionId,
    baseline_commit: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    latest_commit: "cccccccccccccccccccccccccccccccccccccccc",
    summary_key: key, summary: summary, model: null });
}
"""


def run_popup(driver_js):
    script = f"""
const vm = require("vm");
const sources = {json.dumps(POPUP_SOURCES)};
const ctx = {{
  projectId: "", projectName: "", requestedOpen: false, closing: false,
  visible: false, anchorItem: null, projectPlanner: null,
  overviewBusy: false, overviewStarted: false, overviewStartFailed: false,
  overviewRetiring: false, overviewGeneration: 0, overviewLaunchGeneration: 0,
  overviewProjectId: "", overviewLoaded: false, overviewError: "",
  overviewTodos: [], overviewTodosOpen: 0, overviewTodosTotal: 0,
  overviewTodosReason: "",
  overviewWorkMs: 0, overviewWorkScope: "", overviewWorkCount: 0,
  overviewWorkReason: "",
  overviewLastSessionId: "", overviewLastSession: null, overviewLastReason: "",
  overviewChangesAvailable: false, overviewHasBaseline: false,
  overviewBaselineCommit: "",
  overviewLatestCommit: "", overviewSummaryKey: "", overviewEvidence: "",
  overviewChangesReason: "",
  overviewFetchedAt: 0, overviewTtlMs: 60000, overviewKeepStale: false,
  recapBusy: false, recapStarted: false, recapStartFailed: false,
  recapRetiring: false, recapGeneration: 0, recapLaunchGeneration: 0,
  recapLaunchProjectId: "", recapLaunchKey: "", recapLaunchSessionId: "",
  overviewFetchedAt: 0, overviewTtlMs: 60000, overviewKeepStale: false,
  recapBusy: false, recapStarted: false, recapStartFailed: false,
  recapRetiring: false, recapGeneration: 0, recapLaunchGeneration: 0,
  recapLaunchProjectId: "", recapLaunchKey: "", recapLaunchSessionId: "",
  summaries: {{}},
  summaryText: "", summaryState: "idle",
  statusText: "",
  sessionLogBusy: false, sessionLogStarted: false,
  sessionLogStartFailed: false, sessionLogRetiring: false,
  sessionLogGeneration: 0, sessionLogLaunchGeneration: 0,
  sessionLogLaunchProjectId: "", sessionLogLaunchSessionId: "",
  sessionLogLaunchDraftId: "",
  sessionLogQueuedProjectId: "", sessionLogQueuedSessionId: "",
  sessionLogPrepareBusy: false, sessionLogPrepareStarted: false,
  sessionLogPrepareStartFailed: false, sessionLogPrepareRetiring: false,
  sessionLogPrepareGeneration: 0, sessionLogPrepareLaunchGeneration: 0,
  sessionLogPrepareLaunchProjectId: "", sessionLogPrepareLaunchDraftId: "",
  sessionLogPrepareTarget: "",
  sessionLogApplyBusy: false, sessionLogApplyStarted: false,
  sessionLogApplyStartFailed: false, sessionLogApplyRetiring: false,
  sessionLogApplyGeneration: 0, sessionLogApplyLaunchGeneration: 0,
  sessionLogApplyLaunchProjectId: "", sessionLogApplyToken: "",
  sessionLogApplyLaunchSessionId: "",
  sessionLogPolishBusy: false, sessionLogPolishStarted: false,
  sessionLogPolishStartFailed: false, sessionLogPolishRetiring: false,
  sessionLogPolishGeneration: 0, sessionLogPolishLaunchGeneration: 0,
  sessionLogPolishLaunchProjectId: "", sessionLogPolishLaunchDraftId: "",
  sessionLogDrafts: {{}}, sessionLogDraft: null, sessionLogStatus: "",
  sessionLogPreview: null, sessionLogPreviewToken: "",
  sessionLogPreviewTarget: "",
  overviewQueuedProjectId: "", summaryAttemptedKey: "",
  _deferred: [],
  recapError: {{ text: "" }},
  sessionLogError: {{ text: "" }},
  sessionLogPrepareError: {{ text: "" }},
  sessionLogApplyError: {{ text: "" }},
  sessionLogPolishError: {{ text: "" }},
  Quickshell: {{ shellPath(p) {{ return "/repo/" + p; }} }},
  Qt: {{ callLater(fn) {{ ctx._deferred.push(fn); }} }},
  overviewProcess: {{ running: false, command: [], signaled: [],
    signal(s) {{ this.signaled.push(s); return true; }} }},
  overviewTimeout: {{ restarted: 0, stopped: 0,
    restart() {{ this.restarted++; }}, stop() {{ this.stopped++; }} }},
  overviewKillTimer: {{ restarted: 0, stopped: 0,
    restart() {{ this.restarted++; }}, stop() {{ this.stopped++; }} }},
  recapProcess: {{ running: false, command: [], signaled: [],
    signal(s) {{ this.signaled.push(s); return true; }} }},
  recapTimeout: {{ restarted: 0, stopped: 0,
    restart() {{ this.restarted++; }}, stop() {{ this.stopped++; }} }},
  recapKillTimer: {{ restarted: 0, stopped: 0,
    restart() {{ this.restarted++; }}, stop() {{ this.stopped++; }} }},
  sessionLogProcess: {{ running: false, command: [], signaled: [],
    signal(s) {{ this.signaled.push(s); return true; }} }},
  sessionLogTimeout: {{ restarted: 0, stopped: 0,
    restart() {{ this.restarted++; }}, stop() {{ this.stopped++; }} }},
  sessionLogKillTimer: {{ restarted: 0, stopped: 0,
    restart() {{ this.restarted++; }}, stop() {{ this.stopped++; }} }},
  sessionLogPrepareProcess: {{ running: false, command: [], signaled: [],
    signal(s) {{ this.signaled.push(s); return true; }} }},
  sessionLogPrepareTimeout: {{ restarted: 0, stopped: 0,
    restart() {{ this.restarted++; }}, stop() {{ this.stopped++; }} }},
  sessionLogPrepareKillTimer: {{ restarted: 0, stopped: 0,
    restart() {{ this.restarted++; }}, stop() {{ this.stopped++; }} }},
  sessionLogApplyProcess: {{ running: false, command: [], signaled: [],
    signal(s) {{ this.signaled.push(s); return true; }} }},
  sessionLogApplyTimeout: {{ restarted: 0, stopped: 0,
    restart() {{ this.restarted++; }}, stop() {{ this.stopped++; }} }},
  sessionLogApplyKillTimer: {{ restarted: 0, stopped: 0,
    restart() {{ this.restarted++; }}, stop() {{ this.stopped++; }} }},
  sessionLogPolishProcess: {{ running: false, command: [], signaled: [],
    signal(s) {{ this.signaled.push(s); return true; }} }},
  sessionLogPolishTimeout: {{ restarted: 0, stopped: 0,
    restart() {{ this.restarted++; }}, stop() {{ this.stopped++; }} }},
  sessionLogPolishKillTimer: {{ restarted: 0, stopped: 0,
    restart() {{ this.restarted++; }}, stop() {{ this.stopped++; }} }},
  sessionLogCopyProcess: {{ running: false, command: [], signaled: [],
    signal(s) {{ this.signaled.push(s); return true; }} }},
  sessionLogCopyStarted: false, sessionLogCopyLaunchProjectId: "",
  sessionLogCopyLaunchDraftId: "",
  overviewLedgerEntries: [], overviewLedgerStatus: "",
  overviewLedgerBusy: false, overviewLedgerStarted: false,
  overviewLedgerStartFailed: false, overviewLedgerRetiring: false,
  overviewLedgerGeneration: 0, overviewLedgerLaunchGeneration: 0,
  overviewLedgerLaunchProjectId: "", overviewLedgerQueuedProjectId: "",
  overviewLedgerProcess: {{ running: false, command: [], signaled: [],
    signal(s) {{ this.signaled.push(s); return true; }} }},
  overviewLedgerTimeout: {{ restarted: 0, stopped: 0,
    restart() {{ this.restarted++; }}, stop() {{ this.stopped++; }} }},
  overviewLedgerKillTimer: {{ restarted: 0, stopped: 0,
    restart() {{ this.restarted++; }}, stop() {{ this.stopped++; }} }},
  overviewLedgerError: {{ text: "" }},
  panel: {{ opacity: 0, scale: 1 }},
  enterMotion: {{ restarted: 0, stopped: 0,
    restart() {{ this.restarted++; }}, stop() {{ this.stopped++; }} }},
  exitMotion: {{ restarted: 0, stopped: 0,
    restart() {{ this.restarted++; }}, stop() {{ this.stopped++; }} }},
}};
ctx.root = ctx;
for (const prefix of ["enrich", "enrichConfirm", "enrichDismiss",
    "resumePlan", "resumeExecute"]) {{
  for (const flag of ["Busy", "Started", "StartFailed", "Retiring"]) ctx[prefix + flag] = false;
  for (const gen of ["Generation", "LaunchGeneration"]) ctx[prefix + gen] = 0;
  ctx[prefix + "LaunchSessionId"] = "";
  ctx[prefix + "LaunchProjectId"] = "";
  ctx[prefix + "Process"] = {{ running: false, command: [], signaled: [],
    signal(s) {{ this.signaled.push(s); }} }};
  ctx[prefix + "Error"] = {{ text: "" }};
  for (const timer of ["Timeout", "KillTimer"]) ctx[prefix + timer] = {{
    restarted: 0, stopped: 0, restart() {{ this.restarted++; }}, stop() {{ this.stopped++; }} }};
}}
Object.assign(ctx, {{ enrichDataSessionId: "", enrichQueuedSessionId: "",
  enrichLabels: null, enrichSuggestion: null, enrichOverride: null,
  enrichAssociationEnabled: false, enrichStatus: "",
  resumePlan: null, resumeStatus: "", handoffs: [] }});
ctx.projectPlanningRequested = function(pid, act, msg) {{ ctx.handoffs.push([pid, act, msg]); }};
vm.createContext(ctx);
for (const name of Object.keys(sources)) {{
  ctx[name] = vm.runInContext("(" + sources[name] + ")", ctx);
}}
ctx.root = ctx;
ctx.__drain = function() {{ while (ctx._deferred.length) ctx._deferred.shift()(); }};
{MOCKS_JS}
{driver_js}
"""
    return run_node(script)


def overview_payload(pid, key="k1", evidence="M a.py"):
    return {
        "requested_project_id": pid,
        "todos": {"available": True, "open": [], "open_count": 0,
                  "total_count": 0, "reason": ""},
        "work_time": {"available": True, "sessions": [], "count": 0,
                      "total_ms": 0,
                      "scope_label": "last 20 work sessions (showing 0)",
                      "reason": ""},
        "last_session": {
            "available": True,
            "session": {"session_id": "s" * 32, "duration_ms": 1},
            "changes": {"available": True,
                        "baseline_commit": "b" * 40,
                        "latest_commit": "c" * 40,
                        "has_baseline": True,
                        "summary_key": key, "evidence": evidence,
                        "reason": ""},
            "reason": ""},
    }


def payload_literal(payload):
    return json.dumps(json.dumps(payload))


ENRICH_SETUP = """
loadedOverview("A", "key", "evidence");
ctx.projectName = "Deterministic A";
const sid = ctx.overviewLastSessionId;
const pid = "12345678-1234-1234-1234-123456789abc";
function status(session = sid, extra = {}) {
  return JSON.stringify(Object.assign({session_id: session,
    labels: {activity_type: "coding", deep_work: 0.9},
    association_enabled: true, override: null,
    suggestion: {project_id: pid, name: "Suggested B", confidence: 0.82}}, extra));
}
function loadStatus() {
  ctx.refreshEnrichStatus(sid);
  ctx.enrichProcess.running = false;
  ctx.handleEnrichExited(0, status());
}
"""


@unittest.skipUnless(shutil.which("node"), "node is required for QML lifecycle coverage")
class RecapLifecycleTests(unittest.TestCase):
    """The popup is a view (§2.3): recap runs isolated, workers are
    never resolved or prompted, and there is no composer draft."""

    def test_recap_lifecycle_never_resolves_or_prompts_workers(self):
        result = run_popup("""
ctx.projectPlanner = mockPlanner();
loadedOverview("A", "kA", "M a.py");
const launched = ctx.maybeAutoSummary();
const cmd = ctx.recapProcess.command.join(" ");
ctx.recapProcess.running = false;
const finished = ctx.handleRecapExited(0, recapResult("A", "kA", "recap text"));
console.log(JSON.stringify({ launched, cmd, finished,
  agentCalls: ctx.projectPlanner.agentCalls.length,
  summaries: ctx.summaries, summaryText: ctx.summaryText,
  summaryState: ctx.summaryState, busy: ctx.recapBusy }));
""")
        self.assertTrue(result["launched"])
        self.assertIn("project_recap.py", result["cmd"])
        self.assertTrue(result["finished"])
        # View-only: the planner worker cache was never even consulted.
        self.assertEqual(result["agentCalls"], 0)
        self.assertEqual(len(result["summaries"]), 1)
        self.assertEqual(result["summaryText"], "recap text")
        self.assertEqual(result["summaryState"], "ready")
        self.assertFalse(result["busy"])

    def test_summary_launches_isolated_recap_once_then_caches(self):
        result = run_popup("""
ctx.projectPlanner = mockPlanner();
loadedOverview("A", "kA", "M a.py");
const first = ctx.maybeAutoSummary();
const cmd = ctx.recapProcess.command.join(" ");
const second = ctx.maybeAutoSummary();
const third = ctx.maybeAutoSummary();
ctx.recapProcess.running = false;
const finished = ctx.handleRecapExited(0, recapResult("A", "kA", "recap text"));
const fourth = ctx.maybeAutoSummary();
console.log(JSON.stringify({ first, cmd, second, third, finished, fourth,
  summaries: ctx.summaries, summaryText: ctx.summaryText,
  summaryState: ctx.summaryState, busy: ctx.recapBusy }));
""")
        self.assertTrue(result["first"])
        # Isolated helper gets project+session; no worker is involved.
        self.assertIn("project_recap.py", result["cmd"])
        self.assertIn("A", result["cmd"])
        self.assertIn("--session", result["cmd"])
        self.assertFalse(result["second"])
        self.assertFalse(result["third"])
        self.assertTrue(result["finished"])
        self.assertFalse(result["fourth"])
        self.assertEqual(len(result["summaries"]), 1)
        self.assertEqual(result["summaryText"], "recap text")
        self.assertEqual(result["summaryState"], "ready")
        self.assertFalse(result["busy"])

    def test_failed_recap_consumes_attempt_manual_retry_rearms(self):
        result = run_popup("""
ctx.projectPlanner = mockPlanner();
loadedOverview("A", "kA", "M a.py");
ctx.maybeAutoSummary();
ctx.recapError.text = "error: pi recap timed out";
ctx.recapProcess.running = false;
ctx.handleRecapExited(1, "");
const stateAfterFail = ctx.summaryState;
const statusAfterFail = ctx.statusText;
const autoAfterFail = ctx.maybeAutoSummary();
const launchesAfterAuto = ctx.recapTimeout.restarted;
const retried = ctx.retrySummary();
console.log(JSON.stringify({ stateAfterFail, statusAfterFail,
  autoAfterFail, launchesAfterAuto, retried,
  launches: ctx.recapTimeout.restarted,
  summaryState: ctx.summaryState, statusText: ctx.statusText }));
""")
        # No automatic repeat-prompt after failure...
        self.assertFalse(result["autoAfterFail"])
        self.assertEqual(result["launchesAfterAuto"], 1)
        self.assertIn("timed out", result["statusAfterFail"])
        self.assertEqual(result["stateAfterFail"], "idle")
        # ...but the header retry re-arms exactly one more attempt.
        self.assertTrue(result["retried"])
        self.assertEqual(result["launches"], 2)

    def test_isolated_recap_needs_no_worker(self):
        result = run_popup("""
ctx.projectPlanner = mockPlanner();
loadedOverview("A", "kA", "M a.py");
const launched = ctx.maybeAutoSummary();
console.log(JSON.stringify({ launched, busy: ctx.recapBusy,
  state: ctx.summaryState, agentCalls: ctx.projectPlanner.agentCalls.length }));
""")
        # The recap is an isolated subprocess: it launches without ever
        # resolving a project worker.
        self.assertTrue(result["launched"])
        self.assertTrue(result["busy"])
        self.assertEqual(result["state"], "sending")
        self.assertEqual(result["agentCalls"], 0)

    def test_switch_mid_recap_caches_background_answer_by_key(self):
        result = run_popup("""
ctx.projectPlanner = mockPlanner();
loadedOverview("A", "kA", "M a.py");
ctx.maybeAutoSummary();
const cmdA = ctx.recapProcess.command.join(" ");
ctx.openFor("B", "Bee", null);
ctx.__drain();
ctx.recapProcess.running = false;
const routed = ctx.handleRecapExited(0, recapResult("A", "kA", "A recap"));
console.log(JSON.stringify({ cmdA, routed, summaries: ctx.summaries,
  summaryText: ctx.summaryText,
  visibleProject: ctx.projectId, busy: ctx.recapBusy }));
""")
        self.assertIn("project_recap.py", result["cmdA"])
        self.assertTrue(result["routed"])
        # A's recap cached under A's key; B's visible state untouched.
        self.assertEqual(len(result["summaries"]), 1)
        self.assertEqual(result["summaryText"], "")
        self.assertEqual(result["visibleProject"], "B")
        self.assertFalse(result["busy"])


@unittest.skipUnless(shutil.which("node"), "node is required for QML lifecycle coverage")
class FetchQueueTests(unittest.TestCase):
    def test_open_for_b_queues_newest_and_drains_after_settle(self):
        payload_a = payload_literal(overview_payload("A", "kA"))
        payload_b = payload_literal(overview_payload("B", "kB"))
        result = run_popup(f"""
ctx.projectPlanner = mockPlanner();
ctx.openFor("A", "Ay", null);
ctx.__drain();
const cmdA = ctx.overviewProcess.command.join(" ");
ctx.openFor("B", "Bee", null);
ctx.__drain();
const queued = ctx.overviewQueuedProjectId;
const launchStillA = ctx.overviewProjectId;
ctx.overviewProcess.running = false;
const appliedA = ctx.handleOverviewExited(0, {payload_a});
const cmdB = ctx.overviewProcess.command.join(" ");
const busyAfterA = ctx.overviewBusy;
const queuedAfterA = ctx.overviewQueuedProjectId;
ctx.overviewProcess.running = false;
const appliedB = ctx.handleOverviewExited(0, {payload_b});
console.log(JSON.stringify({{ cmdA, queued, launchStillA, appliedA, cmdB,
  busyAfterA, queuedAfterA, appliedB, launchNow: ctx.overviewProjectId,
  loaded: ctx.overviewLoaded, visible: ctx.projectId }}));
""")
        self.assertIn("A", result["cmdA"])
        # Newest request queued; in-flight launch identity preserved.
        self.assertEqual(result["queued"], "B")
        self.assertEqual(result["launchStillA"], "A")
        self.assertTrue(result["appliedA"])
        # A's completion drains the queue into a B launch, and the exit
        # handler must not clobber the fresh launch's busy flags.
        self.assertIn("B", result["cmdB"])
        self.assertTrue(result["busyAfterA"])
        self.assertEqual(result["queuedAfterA"], "")
        # ...whose completion paints B.
        self.assertTrue(result["appliedB"])
        self.assertEqual(result["launchNow"], "B")
        self.assertTrue(result["loaded"])
        self.assertEqual(result["visible"], "B")

    def test_close_then_reopen_keeps_launch_and_applies(self):
        payload_a = payload_literal(overview_payload("A", "kA"))
        result = run_popup(f"""
ctx.projectPlanner = mockPlanner();
ctx.openFor("A", "Ay", null);
ctx.__drain();
const genBefore = ctx.overviewGeneration;
ctx.closePopup();
const genAfterClose = ctx.overviewGeneration;
const openAfterClose = ctx.requestedOpen;
ctx.openFor("A", "Ay", null);
ctx.__drain();
const queued = ctx.overviewQueuedProjectId;
ctx.overviewProcess.running = false;
const applied = ctx.handleOverviewExited(0, {payload_a});
console.log(JSON.stringify({{ genBefore, genAfterClose, openAfterClose,
  queued, applied, loaded: ctx.overviewLoaded, launch: ctx.overviewProjectId }}));
""")
        # Closing invalidates nothing...
        self.assertEqual(result["genBefore"], result["genAfterClose"])
        self.assertFalse(result["openAfterClose"])
        # ...the reopen queues while the launch runs, then applies.
        self.assertEqual(result["queued"], "A")
        self.assertTrue(result["applied"])
        self.assertTrue(result["loaded"])
        self.assertEqual(result["launch"], "A")

    def test_failed_start_surfaces_same_project_and_drains_queue(self):
        result = run_popup("""
ctx.projectPlanner = mockPlanner();
ctx.openFor("A", "Ay", null);
ctx.__drain();
ctx.openFor("B", "Bee", null);
ctx.__drain();
ctx.overviewProcess.running = false;
ctx.handleOverviewRunningChanged();
const cmdB = ctx.overviewProcess.command.join(" ");
console.log(JSON.stringify({ cmd: cmdB, queued: ctx.overviewQueuedProjectId,
  busy: ctx.overviewBusy, errorB: ctx.overviewError }));
""")
        # Failed A start must not paint B's view, but B is launched.
        self.assertIn("B", result["cmd"])
        self.assertEqual(result["queued"], "")
        self.assertTrue(result["busy"])
        self.assertEqual(result["errorB"], "")

    def test_failed_start_same_project_shows_error(self):
        result = run_popup("""
ctx.projectPlanner = mockPlanner();
ctx.openFor("A", "Ay", null);
ctx.__drain();
ctx.overviewProcess.running = false;
ctx.handleOverviewRunningChanged();
console.log(JSON.stringify({ busy: ctx.overviewBusy,
  error: ctx.overviewError, status: ctx.statusText }));
""")
        self.assertFalse(result["busy"])
        self.assertNotEqual(result["error"], "")


@unittest.skipUnless(shutil.which("node"), "node is required for QML lifecycle coverage")
class OverviewFreshnessTests(unittest.TestCase):
    def test_fresh_reopen_reuses_data_without_refetch(self):
        payload_a = payload_literal(overview_payload("A", "kA"))
        result = run_popup(f"""
ctx.projectPlanner = mockPlanner();
ctx.openFor("A", "Ay", null);
ctx.__drain();
ctx.overviewProcess.running = false;
ctx.handleOverviewExited(0, {payload_a});
const restartsAfterFirst = ctx.overviewTimeout.restarted;
ctx.closePopup();
ctx.openFor("A", "Ay", null);
ctx.__drain();
console.log(JSON.stringify({{ restartsAfterFirst,
  restartsNow: ctx.overviewTimeout.restarted,
  loaded: ctx.overviewLoaded, todos: ctx.overviewTodosOpen,
  busy: ctx.overviewBusy }}));
""")
        # Within the TTL the reopen reuses displayed data: no new launch.
        self.assertEqual(result["restartsNow"], result["restartsAfterFirst"])
        self.assertTrue(result["loaded"])
        self.assertFalse(result["busy"])

    def test_stale_reopen_refreshes_keeping_displayed_data(self):
        payload_a = payload_literal(overview_payload("A", "kA"))
        payload_a2 = payload_literal(overview_payload("A", "kA"))
        result = run_popup(f"""
ctx.projectPlanner = mockPlanner();
ctx.openFor("A", "Ay", null);
ctx.__drain();
ctx.overviewProcess.running = false;
ctx.handleOverviewExited(0, {payload_a});
ctx.overviewFetchedAt = Date.now() - 120000;
ctx.closePopup();
ctx.openFor("A", "Ay", null);
ctx.__drain();
const launched = ctx.overviewBusy;
const stillLoaded = ctx.overviewLoaded;
ctx.overviewProcess.running = false;
ctx.handleOverviewExited(0, {payload_a2});
console.log(JSON.stringify({{ launched, stillLoaded,
  loaded: ctx.overviewLoaded, busy: ctx.overviewBusy }}));
""")
        # Stale data refreshes in the background while staying visible.
        self.assertTrue(result["launched"])
        self.assertTrue(result["stillLoaded"])
        self.assertTrue(result["loaded"])
        self.assertFalse(result["busy"])

    def test_stale_refresh_failure_keeps_data_with_notice(self):
        payload_a = payload_literal(overview_payload("A", "kA"))
        result = run_popup(f"""
ctx.projectPlanner = mockPlanner();
ctx.openFor("A", "Ay", null);
ctx.__drain();
ctx.overviewProcess.running = false;
ctx.handleOverviewExited(0, {payload_a});
ctx.overviewFetchedAt = Date.now() - 120000;
ctx.closePopup();
ctx.openFor("A", "Ay", null);
ctx.__drain();
ctx.overviewProcess.running = false;
ctx.handleOverviewExited(1, "");
console.log(JSON.stringify({{ loaded: ctx.overviewLoaded,
  todosOpen: ctx.overviewTodosOpen, error: ctx.overviewError,
  status: ctx.statusText, busy: ctx.overviewBusy }}));
""")
        self.assertTrue(result["loaded"])
        self.assertEqual(result["error"], "")
        self.assertIn("last loaded data", result["status"])
        self.assertFalse(result["busy"])


@unittest.skipUnless(shutil.which("node"), "node is required for QML lifecycle coverage")
class RecapEngineTests(unittest.TestCase):
    def test_new_summary_key_rearms_and_drops_stale_text(self):
        payload_k1 = payload_literal(overview_payload("A", "k1"))
        payload_k2 = payload_literal(overview_payload("A", "k2"))
        result = run_popup(f"""
ctx.projectPlanner = mockPlanner();
ctx.openFor("A", "Ay", null);
ctx.__drain();
ctx.overviewProcess.running = false;
ctx.handleOverviewExited(0, {payload_k1});
ctx.recapProcess.running = false;
ctx.handleRecapExited(0, recapResult("A", "k1", "first recap"));
const cachedFirst = ctx.summaryText;
ctx.overviewFetchedAt = Date.now() - 120000;
ctx.closePopup();
ctx.openFor("A", "Ay", null);
ctx.__drain();
ctx.overviewProcess.running = false;
ctx.handleOverviewExited(0, {payload_k2});
const relaunched = ctx.recapBusy;
const textAfterNewKey = ctx.summaryText;
ctx.recapProcess.running = false;
ctx.handleRecapExited(0, recapResult("A", "k2", "second recap"));
console.log(JSON.stringify({{ cachedFirst, relaunched, textAfterNewKey,
  summaryText: ctx.summaryText, launches: ctx.recapTimeout.restarted }}));
""")
        self.assertEqual(result["cachedFirst"], "first recap")
        # New evidence key: stale text dropped, attempt re-armed, relaunch.
        self.assertEqual(result["textAfterNewKey"], "")
        self.assertTrue(result["relaunched"])
        self.assertEqual(result["summaryText"], "second recap")
        self.assertEqual(result["launches"], 2)

    def test_same_key_reload_never_resends(self):
        payload_k1 = payload_literal(overview_payload("A", "k1"))
        result = run_popup(f"""
ctx.projectPlanner = mockPlanner();
ctx.openFor("A", "Ay", null);
ctx.__drain();
ctx.overviewProcess.running = false;
ctx.handleOverviewExited(0, {payload_k1});
ctx.recapProcess.running = false;
ctx.handleRecapExited(0, recapResult("A", "k1", "first recap"));
ctx.overviewFetchedAt = Date.now() - 120000;
ctx.closePopup();
ctx.openFor("A", "Ay", null);
ctx.__drain();
ctx.overviewProcess.running = false;
ctx.handleOverviewExited(0, {payload_k1});
console.log(JSON.stringify({{ launches: ctx.recapTimeout.restarted,
  summaryText: ctx.summaryText, state: ctx.summaryState }}));
""")
        self.assertEqual(result["launches"], 1)
        self.assertEqual(result["summaryText"], "first recap")
        self.assertEqual(result["state"], "ready")

    def test_newer_evidence_chains_after_inflight_finish(self):
        payload_k1 = payload_literal(overview_payload("A", "k1"))
        payload_k2 = payload_literal(overview_payload("A", "k2"))
        result = run_popup(f"""
ctx.projectPlanner = mockPlanner();
ctx.openFor("A", "Ay", null);
ctx.__drain();
ctx.overviewProcess.running = false;
ctx.handleOverviewExited(0, {payload_k1});
const firstLaunch = ctx.recapBusy;
// Newer evidence paints while the first recap still runs.
ctx.overviewFetchedAt = Date.now() - 120000;
ctx.closePopup();
ctx.openFor("A", "Ay", null);
ctx.__drain();
ctx.overviewProcess.running = false;
ctx.handleOverviewExited(0, {payload_k2});
const duringFirst = ctx.recapBusy;
ctx.recapProcess.running = false;
ctx.handleRecapExited(0, recapResult("A", "k1", "stale recap"));
const chained = ctx.recapBusy;
const chainedKey = ctx.recapLaunchKey;
ctx.recapProcess.running = false;
ctx.handleRecapExited(0, recapResult("A", "k2", "fresh recap"));
console.log(JSON.stringify({{ firstLaunch, duringFirst, chained, chainedKey,
  summaryText: ctx.summaryText, launches: ctx.recapTimeout.restarted }}));
""")
        self.assertTrue(result["firstLaunch"])
        self.assertTrue(result["duringFirst"])
        # Stale finish caches without painting; the newer key chains.
        self.assertTrue(result["chained"])
        self.assertEqual(result["chainedKey"], "k2")
        self.assertEqual(result["summaryText"], "fresh recap")
        self.assertEqual(result["launches"], 2)

    def test_recap_failed_start_and_timeout_settle(self):
        result = run_popup("""
ctx.projectPlanner = mockPlanner();
loadedOverview("A", "kA", "M a.py");
ctx.maybeAutoSummary();
ctx.recapProcess.running = false;
ctx.handleRecapRunningChanged();
const failState = ctx.summaryState;
const retried = ctx.retrySummary();
ctx.recapProcess.running = false;
ctx.handleRecapTimeout();
const timeoutNotice = ctx.statusText;
console.log(JSON.stringify({ failState, retried, timeoutNotice,
  busy: ctx.recapBusy, retiring: ctx.recapRetiring }));
""")
        # Failed start is terminal for this attempt (no loop)...
        self.assertEqual(result["failState"], "idle")
        # ...manual retry relaunches, and the watchdog only warns.
        self.assertTrue(result["retried"])
        self.assertIn("longer than expected", result["timeoutNotice"])
        self.assertTrue(result["busy"])
        self.assertTrue(result["retiring"])

    def test_recap_uses_helper_default_model_without_worker(self):
        result = run_popup("""
ctx.projectPlanner = mockPlanner();
loadedOverview("A", "kA", "M a.py");
ctx.maybeAutoSummary();
const cmd = ctx.recapProcess.command.join(" ");
console.log(JSON.stringify({ cmd,
  agentCalls: ctx.projectPlanner.agentCalls.length }));
""")
        # View-only popup: no worker is resolved, so the isolated recap
        # launches without a --model override (helper default applies).
        self.assertIn("project_recap.py", result["cmd"])
        self.assertNotIn("--model", result["cmd"])
        self.assertEqual(result["agentCalls"], 0)

    def test_recap_mismatch_drops_without_painting(self):
        result = run_popup("""
ctx.projectPlanner = mockPlanner();
loadedOverview("A", "kA", "M a.py");
ctx.maybeAutoSummary();
ctx.recapProcess.running = false;
ctx.handleRecapExited(0, recapResult("A", "WRONG", "sneaky"));
console.log(JSON.stringify({ text: ctx.summaryText, state: ctx.summaryState,
  cached: Object.keys(ctx.summaries).length }));
""")
        self.assertEqual(result["text"], "")
        self.assertEqual(result["cached"], 0)

    def test_recap_key_mismatch_refreshes_and_recovers_fresh(self):
        payload_kb = payload_literal(overview_payload("A", "kB"))
        result = run_popup(f"""
ctx.projectPlanner = mockPlanner();
loadedOverview("A", "kA", "M a.py");
ctx.maybeAutoSummary();
const firstCmd = ctx.recapProcess.command.join(" ");
// The helper re-read the sidecar mid-flight and answered for key B.
ctx.recapProcess.running = false;
const mismatch = ctx.handleRecapExited(0, recapResult("A", "kB", "newer recap"));
const settledState = ctx.summaryState;
const refreshCmd = ctx.overviewProcess.command.join(" ");
const overviewBusy = ctx.overviewBusy;
// Fresh overview paints key B and triggers a fresh recap.
ctx.overviewProcess.running = false;
ctx.handleOverviewExited(0, {payload_kb});
const relaunched = ctx.recapBusy;
const relaunchKey = ctx.recapLaunchKey;
ctx.recapProcess.running = false;
ctx.handleRecapExited(0, recapResult("A", "kB", "fresh recap"));
console.log(JSON.stringify({{ firstCmd, mismatch, settledState, refreshCmd,
  overviewBusy, relaunched, relaunchKey,
  summaryText: ctx.summaryText, launches: ctx.recapTimeout.restarted }}));
""")
        self.assertIn("project_recap.py", result["firstCmd"])
        self.assertFalse(result["mismatch"])
        # Visible state settles (not stuck sending) and the overview
        # refresh is queued/launched identity-guarded.
        self.assertEqual(result["settledState"], "idle")
        self.assertIn("project_overview.py", result["refreshCmd"])
        self.assertTrue(result["overviewBusy"])
        self.assertTrue(result["relaunched"])
        self.assertEqual(result["relaunchKey"], "kB")
        self.assertEqual(result["summaryText"], "fresh recap")
        self.assertEqual(result["launches"], 2)

    def test_recap_timeout_exit_enables_manual_retry(self):
        result = run_popup("""
ctx.projectPlanner = mockPlanner();
loadedOverview("A", "kA", "M a.py");
ctx.maybeAutoSummary();
ctx.recapProcess.running = false;
ctx.handleRecapTimeout();
const retiringNotice = ctx.statusText;
ctx.handleRecapExited(0, "");
const stateAfterExit = ctx.summaryState;
const statusAfterExit = ctx.statusText;
const busyAfterExit = ctx.recapBusy;
const retiringAfterExit = ctx.recapRetiring;
const retried = ctx.retrySummary();
console.log(JSON.stringify({ retiringNotice,
  stateAfterExit, statusAfterExit, busyAfterExit, retiringAfterExit,
  retried, launches: ctx.recapTimeout.restarted,
  state: ctx.summaryState }));
""")
        self.assertIn("longer than expected", result["retiringNotice"])
        # Retirement completion settles to idle with a timeout diagnostic.
        self.assertEqual(result["stateAfterExit"], "idle")
        self.assertIn("timed out", result["statusAfterExit"])
        self.assertFalse(result["busyAfterExit"])
        self.assertFalse(result["retiringAfterExit"])
        # Manual retry works exactly once more.
        self.assertTrue(result["retried"])
        self.assertEqual(result["launches"], 2)

    def test_recap_timeout_exit_switched_project_clean(self):
        result = run_popup("""
ctx.projectPlanner = mockPlanner();
loadedOverview("A", "kA", "M a.py");
ctx.maybeAutoSummary();
ctx.recapProcess.running = false;
ctx.handleRecapTimeout();
ctx.openFor("B", "Bee", null);
ctx.__drain();
const bText = ctx.summaryText;
const bState = ctx.summaryState;
const bStatus = ctx.statusText;
ctx.recapProcess.running = false;
ctx.handleRecapExited(0, "");
console.log(JSON.stringify({ bText, bState, bStatus,
  text: ctx.summaryText, state: ctx.summaryState,
  status: ctx.statusText, visible: ctx.projectId,
  cachedA: Object.keys(ctx.summaries).length }));
""")
        # B's fresh view is untouched by A's retiring recap.
        self.assertEqual(result["bText"], "")
        self.assertEqual(result["text"], "")
        self.assertEqual(result["visible"], "B")
        self.assertEqual(result["cachedA"], 0)
        self.assertNotIn("timed out", result["status"])


@unittest.skipUnless(shutil.which("node"), "node is required for QML lifecycle coverage")
class WorkerOwnershipTests(unittest.TestCase):
    def test_visible_popup_never_resolves_workers(self):
        result = run_popup("""
ctx.projectPlanner = mockPlanner();
ctx.openFor("A", "Ay", null);
ctx.__drain();
const callsAfterOpen = ctx.projectPlanner.agentCalls.length;
loadedOverview("A", "kA", "M a.py");
ctx.maybeAutoSummary();
ctx.retrySummary();
const callsAfterRecap = ctx.projectPlanner.agentCalls.length;
console.log(JSON.stringify({ callsAfterOpen, callsAfterRecap,
  recapBusy: ctx.recapBusy }));
""")
        # View-only popup (§2.3): open/fetch/recap/summary flows never
        # consult the planner worker cache.
        self.assertEqual(result["callsAfterOpen"], 0)
        self.assertEqual(result["callsAfterRecap"], 0)
        self.assertTrue(result["recapBusy"])

    def test_hidden_popup_never_launches_recap(self):
        result = run_popup("""
ctx.projectPlanner = mockPlanner();
ctx.projectId = "A";
ctx.requestedOpen = false;
const auto = ctx.maybeAutoSummary();
console.log(JSON.stringify({ auto,
  agentCalls: ctx.projectPlanner.agentCalls.length,
  busy: ctx.recapBusy }));
""")
        self.assertFalse(result["auto"])
        self.assertEqual(result["agentCalls"], 0)
        self.assertFalse(result["busy"])

    def test_popup_owns_no_approval_handoff(self):
        # The planner owns approvals now: the dead popup handoff is gone
        # (no composer surface, no review function to exercise).
        self.assertNotIn("reviewApprovalInPlanner", POPUP)
        self.assertNotIn("Review approval", POPUP)
        self.assertNotIn("pendingApproval", POPUP)


PLANNER_FUNCTIONS = [
    "isUuid", "approvalHandoffBlockedReason", "openApprovalForProject",
]


def run_planner(driver_js):
    sources = {name: extract_function(PLANNER, name)
               for name in PLANNER_FUNCTIONS}
    script = f"""
const vm = require("vm");
const sources = {json.dumps(sources)};
const ctx = {{
  agentCache: {{}}, projects: [],
  toggleBusy: false, toggleRetiring: false, sendBusy: false,
  pageBusy: false, pageRetiring: false,
  projectWriteBusy: false, projectWriteRetiring: false,
  agenda: null, activeTab: "projects", requestedOpen: false,
  selectedProjectId: "", selectedPath: "", selectedProject: null,
  selectedAgent: null, currentPage: null,
  errorMessage: "", agentError: "", historyLoadError: "",
  historyRetryPending: false, staleToggle: false, notice: "",
  approvalRequest: null, approvalAgent: null, interactionGeneration: 0,
  pendingOpenProjectId: "", journalChild: null, agenda: null,
}};
ctx.root = ctx;
vm.createContext(ctx);
for (const name of Object.keys(sources)) {{
  ctx[name] = vm.runInContext("(" + sources[name] + ")", ctx);
}}
ctx.root = ctx;
ctx.projectById = function(id) {{
  for (const p of ctx.projects) if (p.id === id) return p;
  return null;
}};
ctx.projectNotePath = function(p) {{
  return (p && p.logseq_path) || "";
}};
ctx.pageFor = function(path) {{ return null; }};
ctx.clearPendingOpenProject = function() {{ ctx.pendingOpenProjectId = ""; }};
ctx.open = function() {{ ctx.requestedOpen = true; return true; }};
{driver_js}
"""
    return run_node(script)


RESUME_SETUP = """
ctx.projectPlanner = mockPlanner();
ctx.projectId = "A"; ctx.projectName = "Ay";
ctx.requestedOpen = true; ctx.closing = false;
function resumePlan(extra) {
  return JSON.stringify(Object.assign({version: 1,
    project: {id: "A", name: "Ay"},
    operations: [
      {id: "open_editor", kind: "open_editor", available: true, reason: "",
       params: {root: "/repo", files: ["src/a.py"]}},
      {id: "open_terminal", kind: "open_terminal", available: false,
       reason: "kitty is not available"}
    ],
    unavailable_resources: [], warnings: []}, extra || {}));
}
function resumeExecute(extraResults, extra) {
  return JSON.stringify(Object.assign({project: {id: "A", name: "Ay"},
    operations: [], count: (extraResults || []).length,
    results: extraResults || []}, extra || {}));
}
"""


@unittest.skipUnless(shutil.which("node"), "node is required for QML lifecycle coverage")
class ResumeOverviewTests(unittest.TestCase):
    """Node-vm lifecycle for the overview Resume button (mocked procs)."""

    def test_plan_launch_argv_list_form_and_running_guard(self):
        result = run_popup(RESUME_SETUP + """
const launched = ctx.requestResumePlan();
const cmd = ctx.resumePlanProcess.command;
const second = ctx.requestResumePlan();
console.log(JSON.stringify({ launched, cmd, second,
  busy: ctx.resumePlanBusy, gen: ctx.resumePlanGeneration,
  launch: ctx.resumePlanLaunchProjectId, status: ctx.resumeStatus }));
""")
        self.assertTrue(result["launched"])
        self.assertEqual(result["cmd"],
                         ["python3", "/repo/scripts/desktop_resume.py",
                          "plan", "--project", "A"])
        self.assertNotIn("--operations", result["cmd"])
        self.assertFalse(result["second"])
        self.assertTrue(result["busy"])
        self.assertEqual(result["gen"], 1)
        self.assertEqual(result["launch"], "A")
        self.assertIn("Loading", result["status"])

    def test_plan_error_and_invalid_plan_bounded_notice_no_card(self):
        result = run_popup(RESUME_SETUP + """
ctx.requestResumePlan();
ctx.resumePlanError.text = "error: " + "x".repeat(500);
ctx.resumePlanProcess.running = false;
const failed = ctx.handleResumePlanExited(1, "");
const failure = ctx.resumeStatus;
const cardAfterFail = ctx.resumePlan;
ctx.requestResumePlan();
ctx.resumePlanProcess.running = false;
const invalid = ctx.handleResumePlanExited(0, "garbage{{{");
const invalidNotice = ctx.resumeStatus;
console.log(JSON.stringify({ failed, failure, cardAfterFail,
  invalid, invalidNotice, cardAfterInvalid: ctx.resumePlan,
  busy: ctx.resumePlanBusy }));
""")
        self.assertFalse(result["failed"])
        self.assertLessEqual(len(result["failure"]), 300)
        self.assertTrue(result["failure"].startswith("error:"))
        self.assertIsNone(result["cardAfterFail"])
        self.assertFalse(result["invalid"])
        self.assertEqual(result["invalidNotice"],
                         "Resume returned invalid data")
        self.assertIsNone(result["cardAfterInvalid"])
        self.assertFalse(result["busy"])

    def test_plan_preview_bounded_unavailable_no_params_content_leak(self):
        plan = payload_literal({
            "version": 1,
            "project": {"id": "A", "name": "Ay"},
            "operations": [
                {"id": "op%d" % i, "kind": "open_editor",
                 "available": True, "reason": "",
                 "params": {"root": "/repo",
                            "files": ["secret%d.py" % i]}}
                for i in range(7)
            ] + [
                {"id": "blocked%d" % i, "kind": "open_terminal",
                 "available": False,
                 "reason": "reason-%d " % i + "y" * 200,
                 "params": {"workspace": "ws%d" % i}}
                for i in range(5)
            ],
            "unavailable_resources": [],
            "warnings": ["w" * 200],
            "open_todos": [{"task": "leaked todo"}],
            "content": "leaked page content",
        })
        result = run_popup(RESUME_SETUP + f"""
ctx.requestResumePlan();
ctx.resumePlanProcess.running = false;
const applied = ctx.handleResumePlanExited(0, {plan});
const available = ctx.resumeAvailableNames(ctx.resumePlan);
const unavailable = ctx.resumeUnavailableLines(ctx.resumePlan);
const total = ctx.resumeUnavailableCount(ctx.resumePlan);
const warning = ctx.resumeFirstWarning(ctx.resumePlan);
const dumped = JSON.stringify(ctx.resumePlan);
console.log(JSON.stringify({{ applied, available, unavailable, total,
  warning, status: ctx.resumeStatus,
  availableCount: available.length, unavailableCount: unavailable.length }}));
""")
        self.assertTrue(result["applied"])
        self.assertLessEqual(result["availableCount"], 5)
        self.assertLessEqual(result["unavailableCount"], 3)
        self.assertEqual(result["total"], 5)
        for line in result["unavailable"]:
            reason = line.split(": ", 1)[1] if ": " in line else line
            self.assertLessEqual(len(reason), 80)
        self.assertLessEqual(len(result["warning"]), 100)
        self.assertIn("Confirm", result["status"])
        # Preview helpers never surface params or page content.
        available_src = extract_function(POPUP, "resumeAvailableNames")
        unavailable_src = extract_function(POPUP, "resumeUnavailableLines")
        warning_src = extract_function(POPUP, "resumeFirstWarning")
        for src in (available_src, unavailable_src, warning_src):
            self.assertNotIn("params", src)
            self.assertNotIn("open_todos", src)
            self.assertNotIn("content", src)

    def test_confirm_execute_argv_and_guards(self):
        result = run_popup(RESUME_SETUP + """
ctx.requestResumePlan();
ctx.resumePlanProcess.running = false;
ctx.handleResumePlanExited(0, resumePlan());
const confirmed = ctx.confirmResumeExecute();
const cmd = ctx.resumeExecuteProcess.command;
const duplicate = ctx.confirmResumeExecute();
ctx.resumeExecuteProcess.running = false;
const done = ctx.handleResumeExecuteExited(0, resumeExecute([]));
console.log(JSON.stringify({ confirmed, cmd, duplicate, done,
  handoffs: ctx.handoffs, card: ctx.resumePlan,
  status: ctx.resumeStatus }));
""")
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["cmd"],
                         ["python3", "/repo/scripts/desktop_resume.py",
                          "execute", "--project", "A"])
        self.assertNotIn("--operations", result["cmd"])
        self.assertFalse(result["duplicate"])
        self.assertTrue(result["done"])
        self.assertEqual(result["handoffs"][0][:2], ["A", "resume"])
        self.assertIsNone(result["card"])
        self.assertEqual(result["status"], "")

    def test_confirm_without_preview_never_executes(self):
        result = run_popup(RESUME_SETUP + """
const confirmed = ctx.confirmResumeExecute();
console.log(JSON.stringify({ confirmed,
  cmd: ctx.resumeExecuteProcess.command,
  busy: ctx.resumeExecuteBusy }));
""")
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["cmd"], [])
        self.assertFalse(result["busy"])

    def test_execute_success_hands_off_with_bounded_summary(self):
        result = run_popup(RESUME_SETUP + """
ctx.requestResumePlan();
ctx.resumePlanProcess.running = false;
ctx.handleResumePlanExited(0, resumePlan());
ctx.confirmResumeExecute();
ctx.resumeExecuteProcess.running = false;
const payload = resumeExecute([
  {id: "open_editor", kind: "open_editor", status: "ok", reason: ""},
  {id: "open_terminal", kind: "open_terminal", status: "launched", reason: ""},
  {id: "open_project_agent", kind: "open_project_agent", status: "delegated", reason: ""}
]);
const done = ctx.handleResumeExecuteExited(0, payload);
console.log(JSON.stringify({ done, handoffs: ctx.handoffs,
  busy: ctx.resumeExecuteBusy }));
""")
        self.assertTrue(result["done"])
        self.assertEqual(len(result["handoffs"]), 1)
        self.assertEqual(result["handoffs"][0][:2], ["A", "resume"])
        self.assertLessEqual(len(result["handoffs"][0][2]), 300)
        self.assertIn("ok", result["handoffs"][0][2])
        self.assertFalse(result["busy"])

    def test_execute_partial_still_hands_off_but_failure_never_does(self):
        result = run_popup(RESUME_SETUP + """
ctx.requestResumePlan();
ctx.resumePlanProcess.running = false;
ctx.handleResumePlanExited(0, resumePlan());
ctx.confirmResumeExecute();
ctx.resumeExecuteProcess.running = false;
const partial = resumeExecute([
  {id: "open_editor", kind: "open_editor", status: "ok", reason: ""},
  {id: "open_terminal", kind: "open_terminal", status: "failed", reason: "kitty is not available"},
  {id: "focus_workspace", kind: "focus_workspace", status: "skipped", reason: "no work session"}
]);
const partialDone = ctx.handleResumeExecuteExited(0, partial);
const partialHandoffs = ctx.handoffs.slice();
// A non-zero exit never hands off.
ctx.requestResumePlan();
ctx.resumePlanProcess.running = false;
ctx.handleResumePlanExited(0, resumePlan());
ctx.confirmResumeExecute();
ctx.resumeExecuteError.text = "error: backend exploded";
ctx.resumeExecuteProcess.running = false;
const failed = ctx.handleResumeExecuteExited(1, "");
const failure = ctx.resumeStatus;
console.log(JSON.stringify({ partialDone, partialHandoffs, failed, failure,
  handoffs: ctx.handoffs }));
""")
        self.assertTrue(result["partialDone"])
        self.assertEqual(len(result["partialHandoffs"]), 1)
        self.assertEqual(result["partialHandoffs"][0][:2], ["A", "resume"])
        self.assertIn("1 failed", result["partialHandoffs"][0][2])
        self.assertFalse(result["failed"])
        self.assertEqual(result["failure"], "Resume failed")
        # No additional handoff on failure.
        self.assertEqual(len(result["handoffs"]), 1)

    def test_execute_id_mismatch_rejected(self):
        result = run_popup(RESUME_SETUP + """
ctx.requestResumePlan();
ctx.resumePlanProcess.running = false;
ctx.handleResumePlanExited(0, resumePlan());
ctx.confirmResumeExecute();
ctx.resumeExecuteProcess.running = false;
const foreign = resumeExecute([], {project: {id: "B", name: "Bee"}});
const applied = ctx.handleResumeExecuteExited(0, foreign);
console.log(JSON.stringify({ applied, handoffs: ctx.handoffs,
  notice: ctx.resumeStatus }));
""")
        self.assertFalse(result["applied"])
        self.assertEqual(result["handoffs"], [])
        self.assertEqual(result["notice"], "Resume returned invalid data")

    def test_stale_generation_and_project_switch_drop_late_finishes(self):
        plan_a = payload_literal({
            "version": 1, "project": {"id": "A", "name": "Ay"},
            "operations": [], "unavailable_resources": [], "warnings": [],
        })
        result = run_popup(RESUME_SETUP + f"""
ctx.requestResumePlan();
const stalePlan = ctx.finishResumePlan(0, resumePlan(), 999);
ctx.openFor("B", "Bee", null);
ctx.__drain();
const launchStillA = ctx.resumePlanLaunchProjectId;
ctx.resumePlanProcess.running = false;
const appliedA = ctx.handleResumePlanExited(0, {plan_a});
// Execute: stale generation dropped, switch drops the launch.
ctx.projectId = "A"; ctx.requestedOpen = true; ctx.closing = false;
ctx.requestResumePlan();
ctx.resumePlanProcess.running = false;
ctx.handleResumePlanExited(0, resumePlan());
ctx.confirmResumeExecute();
const staleExec = ctx.finishResumeExecute(0, resumeExecute([]), 999, "A");
ctx.openFor("B", "Bee", null);
ctx.__drain();
ctx.resumeExecuteProcess.running = false;
const switchedExec = ctx.handleResumeExecuteExited(0, resumeExecute([]));
console.log(JSON.stringify({{ stalePlan, launchStillA, appliedA,
  staleExec, switchedExec, visible: ctx.projectId,
  card: ctx.resumePlan, handoffs: ctx.handoffs }}));
""")
        self.assertFalse(result["stalePlan"])
        self.assertEqual(result["launchStillA"], "A")
        self.assertFalse(result["appliedA"])
        self.assertFalse(result["staleExec"])
        self.assertFalse(result["switchedExec"])
        self.assertEqual(result["visible"], "B")
        self.assertIsNone(result["card"])
        self.assertEqual(result["handoffs"], [])

    def test_timeout_resets_busy_with_retry_notice_and_kill_escalation(self):
        result = run_popup(RESUME_SETUP + """
ctx.requestResumePlan();
ctx.resumePlanProcess.running = true;
ctx.handleResumePlanTimeout();
const retiringNotice = ctx.resumeStatus;
ctx.resumePlanProcess.running = true;
const escalated = ctx.fireResumePlanKillTimeout();
ctx.handleResumePlanExited(0, "");
const statusAfterRetire = ctx.resumeStatus;
ctx.resumePlanProcess.running = false;
ctx.requestResumePlan();
ctx.resumePlanProcess.running = false;
ctx.handleResumePlanExited(0, resumePlan());
ctx.confirmResumeExecute();
ctx.resumeExecuteProcess.running = true;
ctx.handleResumeExecuteTimeout();
ctx.resumeExecuteProcess.running = true;
const execEscalated = ctx.fireResumeExecuteKillTimeout();
ctx.handleResumeExecuteExited(0, "");
const execStatus = ctx.resumeStatus;
console.log(JSON.stringify({ retiringNotice, escalated,
  signaled: ctx.resumePlanProcess.signaled,
  statusAfterRetire, busy: ctx.resumePlanBusy,
  retiring: ctx.resumePlanRetiring,
  execEscalated, execSignaled: ctx.resumeExecuteProcess.signaled,
  execStatus, execBusy: ctx.resumeExecuteBusy }));
""")
        self.assertIn("longer than expected", result["retiringNotice"])
        self.assertTrue(result["escalated"])
        self.assertIn(9, result["signaled"])
        self.assertEqual(result["statusAfterRetire"],
                         "Resume timed out; retry")
        self.assertFalse(result["busy"])
        self.assertFalse(result["retiring"])
        self.assertTrue(result["execEscalated"])
        self.assertIn(9, result["execSignaled"])
        self.assertEqual(result["execStatus"], "Resume timed out; retry")
        self.assertFalse(result["execBusy"])

    def test_close_popup_bumps_generations_and_stops(self):
        result = run_popup(RESUME_SETUP + """
ctx.requestResumePlan();
const genBefore = ctx.resumePlanGeneration;
const execBefore = ctx.resumeExecuteGeneration;
ctx.closePopup();
const stopped = !ctx.resumePlanProcess.running
  && !ctx.resumeExecuteProcess.running;
ctx.resumePlanProcess.running = false;
const latePlan = ctx.handleResumePlanExited(0, resumePlan());
console.log(JSON.stringify({ genBefore,
  genAfter: ctx.resumePlanGeneration,
  execAfter: ctx.resumeExecuteGeneration,
  stopped, busy: ctx.resumePlanBusy, latePlan,
  card: ctx.resumePlan, handoffs: ctx.handoffs }));
""")
        self.assertEqual(result["genAfter"], result["genBefore"] + 1)
        self.assertEqual(result["execAfter"], 1)
        self.assertTrue(result["stopped"])
        self.assertFalse(result["busy"])
        self.assertFalse(result["latePlan"])
        self.assertIsNone(result["card"])
        self.assertEqual(result["handoffs"], [])

    def test_no_automatic_plan_fetch_on_open(self):
        result = run_popup("""
ctx.projectPlanner = mockPlanner();
ctx.openFor("A", "Ay", null);
ctx.__drain();
const genAfterSwitch = ctx.resumePlanGeneration;
ctx.closePopup();
ctx.openFor("A", "Ay", null);
ctx.__drain();
console.log(JSON.stringify({ cmd: ctx.resumePlanProcess.command,
  genAfterSwitch, genAfterReopen: ctx.resumePlanGeneration,
  busy: ctx.resumePlanBusy, card: ctx.resumePlan }));
""")
        self.assertEqual(result["cmd"], [])
        # Project switch invalidates resume launches (bump); a
        # same-project reopen never fetches and never bumps.
        self.assertEqual(result["genAfterSwitch"], 1)
        self.assertEqual(result["genAfterReopen"], 2)
        self.assertFalse(result["busy"])
        self.assertIsNone(result["card"])


@unittest.skipUnless(shutil.which("node"), "node is required for QML lifecycle coverage")
class PopupReferenceTests(unittest.TestCase):
    """Static cross-reference check: every root.* call target exists.

    qmllint processes this file cleanly (verified in CI); this test
    additionally guards against typos introduced by surgical QML edits.
    """

    def test_called_functions_are_defined(self):
        defined = set(re.findall(r"function (\w+)\s*\(", POPUP))
        called = set(re.findall(r"root\.(\w+)\s*\(", POPUP))
        self.assertEqual(called - defined, set())
        for name in ("maybeAutoSummary", "sendRecap", "finishRecap",
                     "handleRecapExited", "refreshOverview", "finishOverview",
                     "drainOverviewQueue",
                     "retrySummary",
                     "openFullPlanner",
                     "parseResumePlan", "resumePlanCommand",
                     "resumeExecuteCommand", "resumeAvailableNames",
                     "resumeUnavailableLines", "resumeUnavailableCount",
                     "resumeFirstWarning", "validResumeExecutePayload",
                     "resumeExecuteSummary", "requestResumePlan",
                     "finishResumePlan", "handleResumePlanExited",
                     "cancelResumePreview", "confirmResumeExecute",
                     "finishResumeExecute", "handleResumeExecuteExited",
                     "handoffToProjectPlanner",
                     "canRetrySummary", "retrySummary"):
            self.assertIn(name, defined)

    def test_referenced_ids_exist(self):
        ids = set(re.findall(r"\bid:\s*(\w+)", POPUP))
        for name in ("overviewProcess", "overviewOutput", "overviewErrorOut",
                     "overviewTimeout", "overviewKillTimer",
                     "recapProcess", "recapOutput", "recapError",
                     "recapTimeout", "recapKillTimer",
                     "resumePlanProcess", "resumePlanOutput", "resumePlanError",
                     "resumePlanTimeout", "resumePlanKillTimer",
                     "resumeExecuteProcess", "resumeExecuteOutput",
                     "resumeExecuteError", "resumeExecuteTimeout",
                     "resumeExecuteKillTimer",
                     "overviewScroll", "scrollContent", "panel",
                     "enterMotion", "exitMotion"):
            self.assertIn(name, ids)


@unittest.skipUnless(shutil.which("node"), "node is required for QML lifecycle coverage")
class PlannerHandoffTests(unittest.TestCase):
    PID = "11111111-1111-1111-1111-111111111111"
    OTHER = "22222222-2222-2222-2222-222222222222"

    def _driver(self, case):
        return f"""
const approval = {{ id: "req-1", method: "confirm", title: "Run tests?" }};
const worker = {{ pendingApproval: null, busy: true, compacting: false,
  controlPending: false, sessionSwitching: false, stopping: false,
  sessionRefreshPending: false, projectPath: "" }};
const other = {{ pendingApproval: null, busy: false, compacting: false,
  controlPending: false, sessionSwitching: false, stopping: false,
  sessionRefreshPending: false, projectPath: "" }};
ctx.projects = [{{ id: "{self.PID}", name: "Alpha", logseq_path: "" }}];
ctx.agentCache = {{ ["id:{self.PID}"]: worker, ["id:{self.OTHER}"]: other }};
let result = {{}};
{case}
console.log(JSON.stringify(result));
"""

    def test_handoff_adopts_live_approval_without_creating_workers(self):
        result = run_planner(self._driver("""
worker.pendingApproval = approval;
const keysBefore = Object.keys(ctx.agentCache).sort().join(",");
const ok = ctx.openApprovalForProject("%s".toUpperCase());
result = { ok, selected: ctx.selectedAgent === worker,
  request: ctx.approvalRequest === approval,
  agent: ctx.approvalAgent === worker,
  tab: ctx.activeTab, open: ctx.requestedOpen,
  keysAfter: Object.keys(ctx.agentCache).sort().join(","),
  keysBefore, notice: ctx.notice };
""" % self.PID))
        self.assertTrue(result["ok"])
        self.assertTrue(result["selected"])
        self.assertTrue(result["request"])
        self.assertTrue(result["agent"])
        self.assertEqual(result["tab"], "projects")
        self.assertTrue(result["open"])
        # Ownership without creation: no new worker spawned.
        self.assertEqual(result["keysBefore"], result["keysAfter"])
        self.assertEqual(result["notice"], "")

    def test_handoff_refuses_without_live_approval(self):
        result = run_planner(self._driver("""
const ok = ctx.openApprovalForProject("%s");
result = { ok, selected: ctx.selectedAgent !== null,
  request: ctx.approvalRequest !== null, notice: ctx.notice };
""" % self.PID))
        self.assertFalse(result["ok"])
        self.assertFalse(result["selected"])
        self.assertFalse(result["request"])
        self.assertIn("no pending approval", result["notice"].lower())

    def test_handoff_blocks_on_unrelated_write(self):
        result = run_planner(self._driver("""
worker.pendingApproval = approval;
ctx.toggleBusy = true;
const ok = ctx.openApprovalForProject("%s");
result = { ok, selected: ctx.selectedAgent !== null,
  request: ctx.approvalRequest !== null, notice: ctx.notice,
  tab: ctx.activeTab };
""" % self.PID))
        self.assertFalse(result["ok"])
        self.assertFalse(result["selected"])
        self.assertFalse(result["request"])
        self.assertIn("checkbox", result["notice"].lower())

    def test_handoff_rejected_daily_completion_leaves_daily_tab(self):
        result = run_planner(self._driver("""
worker.pendingApproval = approval;
ctx.requestedOpen = true;
ctx.activeTab = "daily";
ctx.agenda = { completionSaving: true };
const ok = ctx.openApprovalForProject("%s");
result = { ok, tab: ctx.activeTab, request: ctx.approvalRequest !== null,
  selected: ctx.selectedAgent !== null, notice: ctx.notice };
""" % self.PID))
        self.assertFalse(result["ok"])
        self.assertEqual(result["tab"], "daily")
        self.assertFalse(result["request"])
        self.assertFalse(result["selected"])
        self.assertIn("completion", result["notice"].lower())

    def test_handoff_blocks_on_other_workers_approval(self):
        result = run_planner(self._driver("""
worker.pendingApproval = approval;
other.pendingApproval = { id: "req-9", method: "confirm" };
const ok = ctx.openApprovalForProject("%s");
result = { ok, selected: ctx.selectedAgent !== null, notice: ctx.notice };
""" % self.PID))
        self.assertFalse(result["ok"])
        self.assertFalse(result["selected"])
        self.assertIn("another project", result["notice"].lower())

    def test_handoff_blocks_on_other_workers_busy(self):
        result = run_planner(self._driver("""
worker.pendingApproval = approval;
other.busy = true;
const ok = ctx.openApprovalForProject("%s");
result = { ok, selected: ctx.selectedAgent !== null, notice: ctx.notice };
""" % self.PID))
        self.assertFalse(result["ok"])
        self.assertFalse(result["selected"])
        self.assertIn("another project", result["notice"].lower())

    def test_handoff_rejects_unknown_identity(self):
        result = run_planner(self._driver("""
const ok = ctx.openApprovalForProject("not-a-uuid");
result = { ok, selected: ctx.selectedAgent !== null, notice: ctx.notice };
"""))
        self.assertFalse(result["ok"])
        self.assertFalse(result["selected"])
        self.assertNotEqual(result["notice"], "")

    def test_handoff_crosses_idle_daily_tab(self):
        result = run_planner(self._driver("""
worker.pendingApproval = approval;
ctx.requestedOpen = true;
ctx.activeTab = "daily";
ctx.agenda = { completionSaving: false };
const ok = ctx.openApprovalForProject("%s");
result = { ok, tab: ctx.activeTab, request: ctx.approvalRequest === approval,
  agent: ctx.approvalAgent === worker, selected: ctx.selectedAgent === worker };
""" % self.PID))
        self.assertTrue(result["ok"])
        self.assertEqual(result["tab"], "projects")
        self.assertTrue(result["request"])
        self.assertTrue(result["agent"])
        self.assertTrue(result["selected"])

    def test_handoff_crosses_idle_journal_tab(self):
        result = run_planner(self._driver("""
worker.pendingApproval = approval;
ctx.requestedOpen = true;
ctx.activeTab = "journal";
let paused = [];
ctx.journalChild = { pause() { paused.push(true); return true; },
  blockedReason() { return ""; } };
const ok = ctx.openApprovalForProject("%s");
result = { ok, tab: ctx.activeTab, paused,
  request: ctx.approvalRequest === approval };
""" % self.PID))
        self.assertTrue(result["ok"])
        self.assertEqual(result["paused"], [True])
        self.assertEqual(result["tab"], "projects")
        self.assertTrue(result["request"])

    def test_handoff_blocked_by_busy_journal(self):
        result = run_planner(self._driver("""
worker.pendingApproval = approval;
ctx.requestedOpen = true;
ctx.activeTab = "journal";
ctx.journalChild = { pause() { return false; },
  blockedReason() { return "journal thinking"; } };
const ok = ctx.openApprovalForProject("%s");
result = { ok, tab: ctx.activeTab, request: ctx.approvalRequest !== null,
  notice: ctx.notice };
""" % self.PID))
        self.assertFalse(result["ok"])
        self.assertEqual(result["tab"], "journal")
        self.assertFalse(result["request"])
        self.assertIn("journal thinking", result["notice"].lower())

    def test_handoff_blocked_by_other_approval_under_review(self):
        result = run_planner(self._driver("""
worker.pendingApproval = approval;
other.pendingApproval = { id: "req-9", method: "confirm" };
ctx.approvalRequest = other.pendingApproval;
ctx.approvalAgent = other;
const ok = ctx.openApprovalForProject("%s");
result = { ok, request: ctx.approvalRequest === other.pendingApproval,
  agent: ctx.approvalAgent === other, notice: ctx.notice };
""" % self.PID))
        self.assertFalse(result["ok"])
        self.assertTrue(result["request"])
        self.assertTrue(result["agent"])
        self.assertIn("another approval", result["notice"].lower())

    def test_handoff_crosses_journal_tab_without_child(self):
        result = run_planner(self._driver("""
worker.pendingApproval = approval;
ctx.requestedOpen = true;
ctx.activeTab = "journal";
const ok = ctx.openApprovalForProject("%s");
result = { ok, request: ctx.approvalRequest !== null,
  tab: ctx.activeTab };
""" % self.PID))
        # No journal child means nothing to strand: cross freely.
        self.assertTrue(result["ok"])
        self.assertTrue(result["request"])
        self.assertEqual(result["tab"], "projects")


@unittest.skipUnless(shutil.which("node"), "node is required for QML lifecycle coverage")
class ExclusivityClosePathTests(unittest.TestCase):
    """The exclusivity/catcher dismissal for the overview is closePopup().

    shell.qml's closeOthers must route the overview through closePopup()
    (not setOpen(false)) so an in-flight resume plan/execute is aborted
    via generation bumps + process kills, exactly like Escape.
    """

    def test_shell_exclusivity_closes_overview_via_close_popup(self):
        shell = (ROOT / "shell.qml").read_text(encoding="utf-8")
        start = shell.index("function closeOthers(except)")
        body = shell[start:shell.index("MediaPopout {", start)]
        self.assertIn(
            "if (except !== projectOverview && projectOverview.requestedOpen)"
            " projectOverview.closePopup();", body)
        self.assertNotIn("projectOverview.setOpen(false)", body)

    def test_close_popup_source_aborts_resume_launches(self):
        source = extract_function(POPUP, "closePopup")
        self.assertIn("requestedOpen = false", source)
        self.assertIn("resumePlanGeneration++", source)
        self.assertIn("resumeExecuteGeneration++", source)
        self.assertIn("resumePlanProcess.running = false", source)
        self.assertIn("resumeExecuteProcess.running = false", source)

    def test_close_popup_drops_late_execute_finish(self):
        result = run_popup(RESUME_SETUP + """
ctx.requestResumePlan();
ctx.resumePlanProcess.running = false;
ctx.handleResumePlanExited(0, resumePlan());
ctx.confirmResumeExecute();
const execLaunched = ctx.resumeExecuteBusy;
const execGen = ctx.resumeExecuteGeneration;
ctx.closePopup();
const closed = !ctx.requestedOpen;
ctx.resumeExecuteProcess.running = false;
const lateExec = ctx.handleResumeExecuteExited(0, resumeExecute([]));
console.log(JSON.stringify({ execLaunched, execGen,
  execGenAfter: ctx.resumeExecuteGeneration, closed, lateExec,
  handoffs: ctx.handoffs, busy: ctx.resumeExecuteBusy }));
""")
        self.assertTrue(result["execLaunched"])
        self.assertEqual(result["execGenAfter"], result["execGen"] + 1)
        self.assertTrue(result["closed"])
        self.assertFalse(result["lateExec"])
        self.assertEqual(result["handoffs"], [])
        self.assertFalse(result["busy"])


if __name__ == "__main__":
    unittest.main()
