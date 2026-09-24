import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
POPUP = (ROOT / "widgets" / "ProjectOverviewPopup.qml").read_text(encoding="utf-8")
PLANNER = (ROOT / "widgets" / "ProjectPlanner.qml").read_text(encoding="utf-8")
MODULE = (ROOT / "widgets" / "CurrentProjectModule.qml").read_text(encoding="utf-8")
BAR = (ROOT / "widgets" / "Bar.qml").read_text(encoding="utf-8")
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


def run_node(script):
    completed = subprocess.run(
        ["node", "-e", script],
        text=True, capture_output=True,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


def call_pure(name, *args):
    fn = extract_function(POPUP, name)
    payload = ", ".join(json.dumps(a) for a in args)
    script = f"""
const fn = ({fn});
console.log(JSON.stringify(fn({payload})));
"""
    return run_node(script)


def call_pure_with_root(name, root_obj, *args):
    """Run a QML function that reads `root.*` with a stub root object."""
    fn = extract_function(POPUP, name)
    payload = ", ".join(json.dumps(a) for a in args)
    script = f"""
const root = {json.dumps(root_obj)};
const fn = ({fn});
console.log(JSON.stringify(fn({payload})));
"""
    return run_node(script)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class OverviewPureTests(unittest.TestCase):
    def test_format_duration_ms(self):
        self.assertEqual(call_pure("formatDurationMs", 0), "0m")
        self.assertEqual(call_pure("formatDurationMs", 60000), "1m")
        self.assertEqual(call_pure("formatDurationMs", 90 * 60000), "1h 30m")
        self.assertEqual(call_pure("formatDurationMs", 49 * 3600000), "2d 1h")
        self.assertEqual(call_pure("formatDurationMs", -5), "0m")

    def test_summary_cache_key_is_project_and_summary_key_scoped(self):
        same_a = call_pure("summaryCacheKey", "pid", "key-1")
        same_b = call_pure("summaryCacheKey", "pid", "key-1")
        other_project = call_pure("summaryCacheKey", "other", "key-1")
        other_key = call_pure("summaryCacheKey", "pid", "key-2")
        self.assertEqual(same_a, same_b)
        self.assertNotEqual(same_a, other_project)
        self.assertNotEqual(same_a, other_key)

    def test_short_commit(self):
        self.assertEqual(call_pure("shortCommit", "b" * 40), "b" * 7)
        self.assertEqual(call_pure("shortCommit", ""), "")
        self.assertTrue(call_pure("shortCommit", "abc"))

    def test_recap_model_override_uses_verified_shape_only(self):
        # View-only popup (§2.3): it never resolves the shared worker,
        # so the recap always uses the helper default model (no --model
        # override is ever derived from worker state).
        self.assertEqual(call_pure("recapModelFor"), "")

    def test_parse_recap_accepts_valid_rejects_bad(self):
        good = json.dumps({"summary": "did things", "summary_key": "k",
                           "project_id": "p", "session_id": "s"})
        parsed = call_pure("parseRecap", good, "", 0)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["data"]["summary"], "did things")
        failed = call_pure("parseRecap", "", "error: pi recap timed out", 1)
        self.assertFalse(failed["ok"])
        self.assertIn("timed out", failed["error"])
        self.assertFalse(call_pure("parseRecap", "garbage{{{", "", 0)["ok"])
        self.assertFalse(call_pure(
            "parseRecap", json.dumps({"summary_key": "k"}), "", 0)["ok"])
        self.assertFalse(call_pure(
            "parseRecap", json.dumps({"summary": "  ",
                                      "summary_key": "k"}), "", 0)["ok"])

    def test_parse_overview_accepts_valid_rejects_bad(self):
        good = json.dumps({"requested_project_id": "pid", "todos": {}})
        self.assertTrue(call_pure("parseOverview", good, 0)["ok"])
        self.assertFalse(call_pure("parseOverview", good, 1)["ok"])
        self.assertFalse(call_pure("parseOverview", "garbage{{{", 0)["ok"])
        self.assertFalse(call_pure("parseOverview",
                                   json.dumps({"todos": {}}), 0)["ok"])

    def test_should_auto_summarize_gates(self):
        base = {"popupOpen": True, "overviewLoaded": True,
                "hasChanges": True, "cached": False, "attempted": False,
                "recapBusy": False}
        self.assertTrue(call_pure("shouldAutoSummarize", base))
        busy = dict(base, recapBusy=True)
        self.assertFalse(call_pure("shouldAutoSummarize", busy))
        no_changes = dict(base, hasChanges=False)
        self.assertFalse(call_pure("shouldAutoSummarize", no_changes))
        cached = dict(base, cached=True)
        self.assertFalse(call_pure("shouldAutoSummarize", cached))
        attempted = dict(base, attempted=True)
        self.assertFalse(call_pure("shouldAutoSummarize", attempted))
        closed = dict(base, popupOpen=False)
        self.assertFalse(call_pure("shouldAutoSummarize", closed))


class OverviewStructureTests(unittest.TestCase):
    def test_popup_never_resolves_workers_never_owns_scoped_agent(self):
        # View-only popup (§2.3): it never resolves a project worker
        # (no agentForId, no worker.prompt) and never owns a second
        # same-project ScopedAgent process.
        self.assertNotIn("agentForId", POPUP)
        self.assertNotIn("worker.prompt", POPUP)
        self.assertNotIn("ScopedAgent {", POPUP)
        self.assertNotIn("createObject", POPUP)

    def test_popup_uses_bounded_helper_with_watchdog(self):
        self.assertIn("scripts/project_overview.py", POPUP)
        self.assertIn('"overview"', POPUP)
        self.assertIn('"--project"', POPUP)
        self.assertIn('"--sessions-limit"', POPUP)
        self.assertIn("interval: 12000", POPUP)
        self.assertIn("signal(9)", POPUP)
        self.assertIn("overviewLaunchGeneration", POPUP)
        self.assertIn("waitForEnd: true", POPUP)

    def test_popup_recap_is_isolated_subprocess(self):
        # Prompt framing lives in scripts/project_recap.py (tested
        # there); the popup only launches the bounded helper.
        self.assertIn("scripts/project_recap.py", POPUP)
        self.assertIn('"recap"', POPUP)
        self.assertIn("recapProcess", POPUP)
        self.assertIn("finishRecap", POPUP)
        self.assertIn("sendRecap", POPUP)
        self.assertIn("interval: 75000", POPUP)
        self.assertIn("recapModelFor", POPUP)
        # No shared-worker prompt carries session evidence anymore.
        self.assertNotIn("SESSION_CHANGES_JSON_BEGIN", POPUP)
        self.assertNotIn("overviewLastEvents", POPUP)
        self.assertNotIn("session_events", POPUP)
        self.assertNotIn("observed_at_ms", POPUP)
        self.assertNotIn('"--event-limit"', POPUP)

    def test_popup_labels_bounded_scope_and_todos(self):
        self.assertIn("overviewWorkScope", POPUP)
        self.assertIn("last 20 work sessions", POPUP)
        self.assertIn("overviewTodos", POPUP)
        # No fabricated all-time claim anywhere in the popup.
        lowered = POPUP.lower()
        self.assertNotIn("all-time", lowered)
        self.assertNotIn("all time", lowered)

    def test_popup_view_keeps_status_and_planner_entries(self):
        # View-only popup (§2.3): the status line plus the planner
        # entries stay; the composer response/approval UI is gone with
        # the sendbox (the planner owns approvals now: no approval
        # handoff, no review function in the popup).
        self.assertIn("overviewStatus", POPUP)
        self.assertIn("openFullPlanner", POPUP)
        self.assertIn('openProject(id, "", "")', POPUP)
        self.assertNotIn("reviewApprovalInPlanner", POPUP)
        self.assertIn("openApprovalForProject", PLANNER)
        self.assertNotIn("overviewResponse", POPUP)
        self.assertNotIn("overviewApproval", POPUP)
        self.assertNotIn("Review approval", POPUP)
        self.assertNotIn("overviewInstruction", POPUP)
        self.assertNotIn("pendingApproval", POPUP)

    def test_popup_has_no_composer_and_keeps_recap_cache(self):
        # §2.3: no textbox, no Send, no draft store, no turn records,
        # no worker send-gating. The recap cache + retry stay.
        for absent in ("overviewInstruction", "overviewSend",
                       "sendInstruction", "draftFor", "setDraftFor",
                       "pendingTurns", "turnKey", "storeTurn", "removeTurn",
                       "turnById", "turnForWorker", "turnForProject",
                       "ensureVisibleWorker", "workerFor", "historyReady",
                       "workerBlockedReason", "trackWorker",
                       "trackedWorkers", "overviewWorker", "responseText",
                       "handleOpFinished", "handleWorkerFinished",
                       "handleWorkerFailed", "handleWorkerBridgeDead",
                       "Instantiator"):
            self.assertNotIn(absent, POPUP)
        self.assertNotIn("property var drafts", POPUP)
        self.assertNotIn("property var responses", POPUP)
        for kept in ("summaryAttemptedKey", "retrySummary",
                     "canRetrySummary", "summaries", "statusText",
                     "overviewStatus"):
            self.assertIn(kept, POPUP)
        # No single-slot records nulled on ACK/switch either (there are
        # no turns at all anymore).
        self.assertNotIn("property var pendingUser", POPUP)
        self.assertNotIn("property var pendingSummary", POPUP)

    def test_popup_queues_fetches_without_worker_tracking(self):
        self.assertNotIn("trackedWorkers", POPUP)
        self.assertNotIn("trackWorker", POPUP)
        self.assertNotIn("Instantiator", POPUP)
        self.assertNotIn("ensureVisibleWorker", POPUP)
        self.assertNotIn("overviewWorkerProjectId", POPUP)
        self.assertIn("overviewQueuedProjectId", POPUP)
        self.assertIn("drainOverviewQueue", POPUP)
        self.assertIn("immutable launch identity", POPUP)
        self.assertIn("settleRetiredRecap", POPUP)
        self.assertIn("Recap timed out; retry from the recap header.", POPUP)

    def test_popup_review_uses_explicit_planner_handoff(self):
        # The planner owns approvals now: the popup keeps no approval UI
        # and no review handoff (no composer surface at all).
        self.assertNotIn("reviewApprovalInPlanner", POPUP)
        self.assertNotIn("Review approval", POPUP)
        self.assertNotIn("pendingApproval", POPUP)
        self.assertIn("function openApprovalForProject", PLANNER)
        self.assertIn("function approvalHandoffBlockedReason", PLANNER)
        # Ownership from the existing cache; the handoff never spawns.
        self.assertIn('agentCache["id:"', PLANNER)
        self.assertIn("clearPendingOpenProject", PLANNER)
        # Cross-tab handoff pauses idle journals, never strands busy ones,
        # and never steals another approval under review.
        self.assertIn("journalChild.pause", PLANNER)
        self.assertIn("Another approval is being reviewed.", PLANNER)

    def test_popup_close_never_kills_worker(self):
        # Closing hides only; the shared agent is never aborted/stopped.
        self.assertIn("closePopup", POPUP)
        self.assertNotIn(".abort()", POPUP)
        self.assertNotIn("stopIdle", POPUP)

    def test_popup_is_anchored_panel_window(self):
        self.assertIn("PanelWindow", POPUP)
        self.assertIn("refreshPosition", POPUP)
        self.assertIn("clampPopupX", POPUP)
        self.assertIn("OnDemand", POPUP)

    def test_popup_scrolls_variable_content_without_composer(self):
        # Long TODO/recap content scrolls inside a clipped region
        # instead of pushing the header out of the 600px card. The fixed
        # composer row is gone — the planner entries are the only
        # composer path.
        self.assertIn("overviewScroll", POPUP)
        self.assertIn("Flickable", POPUP)
        self.assertIn("scrollContent", POPUP)
        self.assertNotIn("overviewSend", POPUP)
        self.assertNotIn("overviewInstruction", POPUP)
        self.assertNotIn("overviewLedgerOpenPlanner", POPUP)
        self.assertIn("Full planner", POPUP)

    def test_tray_click_opens_popup_for_valid_project_only(self):
        self.assertIn("overviewPopup", MODULE)
        self.assertIn("shouldOpenOverview", MODULE)
        self.assertIn("activateCurrentProject", MODULE)
        self.assertIn("openFor(", MODULE)
        self.assertIn("onClicked: root.activateCurrentProject()", MODULE)
        # Safe fallback to the plain list is preserved (never a stale id).
        self.assertIn('openProject(target, "", "")', MODULE)

    def test_bar_and_shell_wire_popup(self):
        self.assertIn("property var projectOverviewPopup", BAR)
        self.assertIn("overviewPopup: bar.projectOverviewPopup", BAR)
        self.assertIn("ProjectOverviewPopup", SHELL)
        self.assertIn("id: projectOverview", SHELL)
        # The per-screen delegate cannot use an unqualified same-name
        # binding (it would resolve to the child's own null property),
        # so the popup goes through the delegate-root qualified ref.
        self.assertIn(
            "readonly property var projectPlannerRef: projectPlanner", SHELL)
        self.assertIn(
            "projectPlanner: barWindow.projectPlannerRef", SHELL)
        self.assertIn("projectOverviewPopup: projectOverview", SHELL)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class ResumePureTests(unittest.TestCase):
    def test_parse_resume_plan_accepts_valid_rejects_bad(self):
        good = json.dumps({"version": 1,
                           "project": {"id": "p1", "name": "Demo"},
                           "operations": []})
        parsed = call_pure("parseResumePlan", good, "", 0)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["data"]["project"]["id"], "p1")
        failed = call_pure("parseResumePlan", "", "error: nope", 1)
        self.assertFalse(failed["ok"])
        self.assertIn("nope", failed["error"])
        self.assertFalse(call_pure(
            "parseResumePlan", "garbage{{{", "", 0)["ok"])
        self.assertFalse(call_pure(
            "parseResumePlan",
            json.dumps({"version": 2,
                        "project": {"id": "p1"}, "operations": []}),
            "", 0)["ok"])
        self.assertFalse(call_pure(
            "parseResumePlan",
            json.dumps({"version": 1, "operations": []}), "", 0)["ok"])
        self.assertFalse(call_pure(
            "parseResumePlan",
            json.dumps({"version": 1, "project": {"id": "p1"}}),
            "", 0)["ok"])
        self.assertFalse(call_pure(
            "parseResumePlan", json.dumps(["array"]), "", 0)["ok"])
        long_err = "error: " + "x" * 500
        failed_long = call_pure("parseResumePlan", "", long_err, 1)
        self.assertFalse(failed_long["ok"])
        self.assertLessEqual(len(failed_long["error"]), 300)

    def test_resume_command_builders_list_form_no_operations_flag(self):
        plan = call_pure("resumePlanCommand", "p1")
        self.assertEqual(plan[:2], ["python3", "scripts/desktop_resume.py"])
        self.assertEqual(plan[2:], ["plan", "--project", "p1"])
        execute = call_pure("resumeExecuteCommand", "p1")
        self.assertEqual(execute[:2],
                         ["python3", "scripts/desktop_resume.py"])
        self.assertEqual(execute[2:], ["execute", "--project", "p1"])
        self.assertEqual(call_pure("resumePlanCommand", ""), [])
        self.assertEqual(call_pure("resumeExecuteCommand", ""), [])

    def test_resume_preview_helpers_bounded_and_never_leak_params(self):
        plan = {
            "version": 1, "project": {"id": "p1"},
            "operations": [
                {"id": "op%d" % i, "kind": "open_editor",
                 "available": True, "reason": "",
                 "params": {"root": "/repo"}}
                for i in range(7)
            ] + [
                {"id": "blocked%d" % i, "kind": "open_terminal",
                 "available": False,
                 "reason": "r%d " % i + "y" * 200,
                 "params": {"workspace": "ws"}}
                for i in range(5)
            ],
            "warnings": ["w" * 200],
        }
        available = call_pure("resumeAvailableNames", plan)
        self.assertLessEqual(len(available), 5)
        unavailable = call_pure("resumeUnavailableLines", plan)
        self.assertLessEqual(len(unavailable), 3)
        for line in unavailable:
            self.assertIn(": ", line)
            self.assertLessEqual(len(line.split(": ", 1)[1]), 80)
        self.assertEqual(call_pure("resumeUnavailableCount", plan), 5)
        self.assertLessEqual(len(call_pure("resumeFirstWarning", plan)),
                             100)
        self.assertEqual(call_pure("resumeAvailableNames", None), [])
        self.assertEqual(call_pure("resumeUnavailableLines", None), [])
        self.assertEqual(call_pure("resumeUnavailableCount", None), 0)
        self.assertEqual(call_pure("resumeFirstWarning", None), "")
        self.assertEqual(call_pure("resumeFirstWarning", {}), "")
        # Helpers never read params or page content.
        for name in ("resumeAvailableNames", "resumeUnavailableLines",
                     "resumeUnavailableCount", "resumeFirstWarning"):
            src = extract_function(POPUP, name)
            self.assertNotIn("params", src)
            self.assertNotIn("open_todos", src)
            self.assertNotIn("content", src)

    def test_resume_execute_summary_bounded_palette_semantics(self):
        payload = {
            "project": {"id": "p1"},
            "results": [
                {"id": "a", "kind": "open_editor", "status": "ok",
                 "reason": ""},
                {"id": "b", "kind": "open_terminal", "status": "failed",
                 "reason": "kitty is not available"},
                {"id": "c", "kind": "open_project_agent",
                 "status": "delegated", "reason": ""},
                {"id": "d", "kind": "focus_workspace", "status": "skipped",
                 "reason": "no work session"},
            ],
        }
        summary = call_pure("resumeExecuteSummary", payload)
        self.assertIn("1 failed", summary)
        self.assertIn("1 skipped", summary)
        self.assertIn("delegated", summary)
        self.assertLessEqual(len(summary), 300)
        self.assertTrue(call_pure("validResumeExecutePayload", payload))
        self.assertFalse(call_pure("validResumeExecutePayload", {}))
        self.assertFalse(call_pure("validResumeExecutePayload", None))
        self.assertFalse(call_pure(
            "validResumeExecutePayload",
            {"project": {}, "results": []}))
        self.assertFalse(call_pure(
            "validResumeExecutePayload",
            {"project": {"id": "p1"}}))


class ResumeStructureTests(unittest.TestCase):
    def test_resume_object_names_and_explicit_actions(self):
        self.assertIn('objectName: "resumeButton"', POPUP)
        self.assertIn('objectName: "resumeCard"', POPUP)
        self.assertIn('objectName: "resumeConfirm"', POPUP)
        self.assertIn('objectName: "resumeCancel"', POPUP)
        self.assertIn("Resume preview", POPUP)
        self.assertIn("Resuming…", POPUP)
        self.assertIn("requestResumePlan", POPUP)
        self.assertIn("confirmResumeExecute", POPUP)
        self.assertIn("cancelResumePreview", POPUP)
        self.assertIn("finishResumePlan", POPUP)
        self.assertIn("finishResumeExecute", POPUP)
        self.assertIn("handoffToProjectPlanner", POPUP)

    def test_resume_plan_execute_argv_list_form_no_operations(self):
        self.assertIn("scripts/desktop_resume.py", POPUP)
        self.assertIn('"plan", "--project"', POPUP)
        self.assertIn('"execute", "--project"', POPUP)
        self.assertIn('Quickshell.shellPath("scripts/desktop_resume.py")',
                      POPUP)
        self.assertIn('workingDirectory: Quickshell.shellPath(".")', POPUP)
        self.assertNotIn('"--operations"', POPUP)

    def test_resume_signal_and_shell_connection(self):
        self.assertIn(
            "signal projectPlanningRequested(string projectId, string action, string message)",
            POPUP)
        self.assertIn("projectPlanningRequested(pid", POPUP)
        self.assertIn('"resume"', POPUP)
        self.assertIn("onProjectPlanningRequested(projectId, action",
                      SHELL)
        self.assertIn("target: projectOverview", SHELL)
        self.assertIn("projectPlanner.openProject(projectId, action", SHELL)

    def test_resume_preview_never_renders_params_or_page_content(self):
        card_start = POPUP.index('objectName: "resumeCard"')
        card_end = POPUP.index('objectName: "overviewStatus"',
                               card_start)
        card = POPUP[card_start:card_end]
        self.assertNotIn("params", card)
        self.assertNotIn("open_todos", card)
        self.assertNotIn("content", card)
        self.assertIn("resumeAvailableNames", card)
        self.assertIn("resumeUnavailableLines", card)
        self.assertIn("resumeFirstWarning", card)

    def test_resume_never_auto_fetches_and_never_modal(self):
        open_for = extract_function(POPUP, "openFor")
        self.assertNotIn("requestResumePlan", open_for)
        self.assertNotIn("resumePlanProcess.command", open_for)
        self.assertNotIn("resumeExecuteProcess.command", open_for)
        self.assertNotIn("confirmResumeExecute", open_for)
        refresh = extract_function(POPUP, "refreshOverview")
        self.assertNotIn("requestResumePlan", refresh)
        self.assertNotIn("ApplicationModal", POPUP)
        self.assertNotIn("WindowModal", POPUP)

    def test_resume_watchdog_intervals_and_escalation(self):
        self.assertIn("resumePlanProcess", POPUP)
        self.assertIn("resumeExecuteProcess", POPUP)
        self.assertIn("resumePlanTimeout", POPUP)
        self.assertIn("resumeExecuteTimeout", POPUP)

        def timer_block(timer_id):
            idx = POPUP.index("id: " + timer_id)
            return POPUP[idx:idx + 200]

        for timer_id in ("resumePlanTimeout", "resumeExecuteTimeout"):
            self.assertIn("interval: 12000", timer_block(timer_id))
        for timer_id in ("resumePlanKillTimer", "resumeExecuteKillTimer"):
            self.assertIn("interval: 3000", timer_block(timer_id))
        for fn in ("fireResumePlanKillTimeout",
                   "fireResumeExecuteKillTimeout"):
            self.assertIn("signal(9)", extract_function(POPUP, fn))

    def test_resume_generation_guards(self):
        self.assertIn("resumePlanGeneration", POPUP)
        self.assertIn("resumePlanLaunchGeneration", POPUP)
        self.assertIn("resumePlanLaunchProjectId", POPUP)
        self.assertIn("resumeExecuteGeneration", POPUP)
        self.assertIn("resumeExecuteLaunchGeneration", POPUP)
        self.assertIn("resumeExecuteLaunchProjectId", POPUP)
        close_fn = extract_function(POPUP, "closePopup")
        self.assertIn("resumePlanGeneration++", close_fn)
        self.assertIn("resumeExecuteGeneration++", close_fn)
