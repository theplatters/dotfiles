"""Workstream D capture inbox UI: DailyAgenda state + CaptureInbox card.

Covers the plan section 7D UI half without a live Quickshell harness:
DailyAgenda capture Process/op ladder (list/prepare/apply/dismiss),
generation + selected-date staleness guards, bounded single-line
errors, scanNow delegation, plus the CaptureInbox presentation card
(objectNames, exact preview, Confirm gating), the DailyPlanner embed,
and the shell.qml scheduler wiring.
"""
import sys as _sys
_sys.dont_write_bytecode = True

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
AGENDA = (ROOT / "widgets" / "DailyAgenda.qml").read_text(encoding="utf-8")
INBOX = (ROOT / "widgets" / "CaptureInbox.qml").read_text(encoding="utf-8")
PLANNER_UI = (ROOT / "widgets" / "DailyPlanner.qml").read_text(encoding="utf-8")
SHELL = (ROOT / "shell.qml").read_text(encoding="utf-8")
SCHEDULER = (ROOT / "widgets" / "MemoryScheduler.qml").read_text(encoding="utf-8")
PANEL = (ROOT / "widgets" / "PreviewPanel.qml").read_text(encoding="utf-8")


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
        ["node", "-e", script], text=True, capture_output=True,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


CAPTURE_FUNCTIONS = [
    "captureBound", "captureFailure",
    "captureListCommand", "capturePrepareCommand",
    "captureApplyCommand", "captureDismissCommand",
    "launchCapture", "reloadCaptures",
    "finishCaptures", "prepareCapture", "finishPrepare",
    "applyCapture", "finishApply", "cancelCapturePreview",
    "dismissCapture", "finishDismiss", "scanNow",
    "handleCaptureExited", "handleCaptureRunningChanged",
    "handleCaptureTimeout", "fireCaptureKillTimeout",
    "isValidIso", "parseIsoDate",
    # Session scoping for the Session card: prepareCapture pins the
    # preview to a valid ledger session id (Phase 2b §4.5).
    "isLedgerSessionId",
]


def capture_harness(setup, exercise):
    functions = [extract_function(AGENDA, name) for name in CAPTURE_FUNCTIONS]
    sources = json.dumps(functions)
    return f"""
const vm = require("vm");
const context = {{
  selectedDate: "2026-09-13",
  captureItems: [], captureBusy: false, captureStarted: false,
  captureStartFailed: false, captureRetiring: false,
  captureGeneration: 0, captureLaunchGeneration: -1,
  captureOp: "", captureLaunchOp: "", captureLaunchDate: "",
  captureError: "", captureNotice: "",
  capturePreview: null, capturePreviewToken: "",
  captureApplying: false, captureApplyError: "",
  scheduler: null,
  captureBoundCalls: 0,
  reloadCalls: 0,
  reloadCaptures() {{ this.reloadCalls++; return true; }},
  captureWatchdog: {{ stopped: false, restarted: false,
    stop() {{ this.stopped = true; }}, restart() {{ this.restarted = true; }} }},
  captureKillTimer: {{ stopped: false, restarted: false,
    stop() {{ this.stopped = true; }}, restart() {{ this.restarted = true; }} }},
  captureProcess: {{ running: false, command: [], signaled: [],
    signal(sig) {{ this.signaled.push(sig); return true; }},
    closeWriteChannel() {{}} , stdinEnabled: false }},
  Quickshell: {{ shellPath(value) {{ return "/repo/" + value; }}, env(v) {{ return ""; }} }},
}};
{setup}
context.root = context;
// closeInput helper used by the timeout path.
context.closeInput = function(process) {{ }};
vm.createContext(context);
for (const value of {sources}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
context.root = context;
{exercise}
"""


def run_capture(setup, exercise):
    return run_node(capture_harness(setup, exercise))


def list_payload(items):
    return json.dumps({"day": "2026-09-13", "status": "new",
                       "count": len(items), "captures": items})


def sample_capture(cid=3):
    return {"id": cid, "project_id": "11111111-1111-1111-1111-111111111111",
            "project_name": "Demo", "kind": "todo", "text": "Ship it",
            "status": "new", "actionable": 0.9, "durable": 0.8,
            "created_ms": 1, "applied_target": None}


class CaptureStructureTests(unittest.TestCase):
    def test_state_properties(self):
        for token in ("property var scheduler: null",
                      "property var captureItems: []",
                      "property bool captureBusy: false",
                      "property string captureError",
                      "property string captureNotice",
                      "property int captureGeneration: 0",
                      "property int captureLaunchGeneration",
                      "property string captureOp",
                      "property string captureLaunchOp",
                      "property bool captureRetiring: false",
                      "property string captureLaunchDate",
                      "property var capturePreview: null",
                      "property string capturePreviewToken",
                      "property bool captureApplying: false",
                      "property string captureApplyError"):
            self.assertIn(token, AGENDA)

    def test_process_watchdog_and_list_argv(self):
        self.assertIn("id: captureProcess", AGENDA)
        self.assertIn('Quickshell.shellPath("scripts/session_capture.py")', AGENDA)
        self.assertIn('"python3"', AGENDA)
        self.assertIn('"list"', AGENDA)
        self.assertIn('"--day"', AGENDA)
        self.assertIn('"--status"', AGENDA)
        self.assertIn('"new"', AGENDA)
        self.assertIn('"--limit"', AGENDA)
        self.assertIn('"20"', AGENDA)
        self.assertIn("workingDirectory: Quickshell.shellPath", AGENDA)
        self.assertIn("id: captureWatchdog", AGENDA)
        self.assertIn("interval: 12000", AGENDA)
        self.assertIn("id: captureKillTimer", AGENDA)
        self.assertIn("interval: 3000", AGENDA)
        self.assertIn("signal(9)", AGENDA)
        for name in ("reloadCaptures", "finishCaptures",
                     "prepareCapture", "finishPrepare",
                     "applyCapture", "cancelCapturePreview",
                     "dismissCapture", "scanNow"):
            self.assertIn("function " + name + "(", AGENDA)

    def test_prepare_apply_dismiss_argv(self):
        self.assertIn('"prepare"', AGENDA)
        self.assertIn('"--capture"', AGENDA)
        self.assertIn('"apply"', AGENDA)
        self.assertIn('"--prepared"', AGENDA)
        self.assertIn('"set-status"', AGENDA)
        self.assertIn('"--id"', AGENDA)
        self.assertIn('"dismissed"', AGENDA)
        self.assertNotIn("sh -c", AGENDA)
        # L1 single save target: the capture prepare ladder carries no
        # --target; session provenance rides on --session/--ref instead
        # (--target survives only on the unrelated work_log file ladder).
        prepare = extract_function(AGENDA, "capturePrepareCommand")
        self.assertNotIn("--target", prepare)
        self.assertIn('"--capture"', prepare)
        self.assertIn('"--session"', prepare)
        self.assertIn('"--ref"', prepare)

    def test_no_modal_errors_bounded_single_line(self):
        # Capture errors/notices stay inline text; never a modal dialog.
        self.assertNotIn("PopupWindow", AGENDA[max(0, AGENDA.index("captureItems") - 500):AGENDA.index("captureItems") + 2000] if "captureItems" in AGENDA else "")
        lowered = AGENDA.lower()
        # Bounded helpers exist; failure text is single-line truncated.
        self.assertIn("capturebound", lowered)
        self.assertIn("capturefailure", lowered)

    def test_refresh_wiring(self):
        self.assertIn("target: root.scheduler", AGENDA)
        self.assertIn("onDataChanged", AGENDA)
        self.assertIn("root.reloadCaptures()", AGENDA)
        # Date changes refresh the inbox list too.
        setter = extract_function(AGENDA, "setSelectedDate")
        self.assertIn("reloadCaptures", setter)

    def test_no_auto_network_calls_on_load(self):
        # DailyAgenda itself never auto-lists captures: the card drives
        # the initial load via its own visible/completed hooks.
        tail = AGENDA[AGENDA.index("Component.onCompleted"):]
        self.assertIn("root.startList()", tail)
        self.assertNotIn("reloadCaptures", tail)

    def test_scan_now_delegates_boundedly(self):
        body = extract_function(AGENDA, "scanNow")
        self.assertIn("requestScan", body)
        self.assertIn("Scan unavailable", body)
        self.assertIn("Scan running", body)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class CaptureListTests(unittest.TestCase):
    IN_FLIGHT = ("context.captureGeneration = 4; context.captureLaunchGeneration = 4; "
                 "context.captureOp = 'list'; context.captureLaunchOp = 'list'; "
                 "context.captureLaunchDate = '2026-09-13'; "
                 "context.selectedDate = '2026-09-13'; "
                 "context.captureBusy = true; context.captureRetiring = false; "
                 "context.captureProcess.running = false;")

    def test_list_argv(self):
        value = run_capture(
            "",
            """
const argv = context.captureListCommand("2026-09-13");
console.log(JSON.stringify({ argv }));
""")
        self.assertEqual(value["argv"],
                         ["python3", "/repo/scripts/session_capture.py",
                          "list", "--day", "2026-09-13",
                          "--status", "new", "--limit", "20"])

    def test_reload_noop_while_busy(self):
        value = run_capture(
            "context.captureBusy = true; context.captureProcess.running = false;",
            "console.log(JSON.stringify({ reloaded: context.reloadCaptures() }));")
        self.assertFalse(value["reloaded"])

    def test_finish_validates_and_bounds(self):
        items = [{"id": i, "project_id": "p", "project_name": "D",
                  "kind": "todo", "text": "item %d" % i, "status": "new",
                  "actionable": None, "durable": None,
                  "created_ms": i, "applied_target": None}
                 for i in range(25)]
        value = run_capture(
            self.IN_FLIGHT,
            f"""
const applied = context.finishCaptures(0, {json.dumps(list_payload(items))}, 4, "2026-09-13", "list");
console.log(JSON.stringify({{ applied, count: context.captureItems.length,
  err: context.captureError, busy: context.captureBusy }}));
""")
        self.assertTrue(value["applied"])
        self.assertEqual(value["count"], 20)
        self.assertEqual(value["err"], "")
        self.assertFalse(value["busy"])

    def test_finish_drops_stale_generation_and_date(self):
        payload = list_payload([sample_capture()])
        stale_gen = run_capture(
            self.IN_FLIGHT,
            f"""
const applied = context.finishCaptures(0, {json.dumps(payload)}, 3, "2026-09-13", "list");
console.log(JSON.stringify({{ applied }}));
""")
        self.assertFalse(stale_gen["applied"])
        stale_date = run_capture(
            self.IN_FLIGHT + "context.selectedDate = '2026-09-14';",
            f"""
const applied = context.finishCaptures(0, {json.dumps(payload)}, 4, "2026-09-13", "list");
console.log(JSON.stringify({{ applied }}));
""")
        self.assertFalse(stale_date["applied"])
        launch_mismatch = run_capture(
            self.IN_FLIGHT,
            f"""
const applied = context.finishCaptures(0, {json.dumps(payload)}, 4, "2026-09-14", "list");
console.log(JSON.stringify({{ applied }}));
""")
        self.assertFalse(launch_mismatch["applied"])

    def test_finish_rejects_bad_shapes(self):
        for payload in ('"not json"', '{}',
                        json.dumps({"captures": {}}),
                        json.dumps({"error": "boom"})):
            with self.subTest(payload=payload):
                value = run_capture(
                    self.IN_FLIGHT,
                    f"""
const applied = context.finishCaptures(1, {json.dumps(payload)}, 4, "2026-09-13", "list");
console.log(JSON.stringify({{ applied, err: context.captureError }}));
""")
                self.assertFalse(value["applied"])
                self.assertTrue(value["err"] != "")


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class CapturePrepareApplyTests(unittest.TestCase):
    PREP_FLIGHT = ("context.captureGeneration = 5; context.captureLaunchGeneration = 5; "
                   "context.captureOp = 'prepare'; context.captureLaunchOp = 'prepare'; "
                   "context.captureLaunchDate = '2026-09-13'; "
                   "context.selectedDate = '2026-09-13'; "
                   "context.captureBusy = true; context.captureRetiring = false; "
                   "context.captureProcess.running = false;")
    APPLY_FLIGHT = ("context.captureGeneration = 6; context.captureLaunchGeneration = 6; "
                    "context.captureOp = 'apply'; context.captureLaunchOp = 'apply'; "
                    "context.captureLaunchDate = '2026-09-13'; "
                    "context.selectedDate = '2026-09-13'; "
                    "context.captureBusy = true; context.captureApplying = true; "
                    "context.captureRetiring = false; "
                    "context.captureProcess.running = false; "
                    "context.capturePreview = {target: 'page', block: '- TODO x'}; "
                    "context.capturePreviewToken = 'cap_abc';")

    def test_prepare_argv_page_only_with_optional_session_ref(self):
        value = run_capture(
            "",
            """
const argv = context.capturePrepareCommand(7);
console.log(JSON.stringify({ argv }));
""")
        self.assertEqual(value["argv"],
                         ["python3", "/repo/scripts/session_capture.py",
                          "prepare", "--capture", "7"])
        sid = "b" * 32
        full = run_capture(
            "",
            f"""
const argv = context.capturePrepareCommand(7, "{sid}", "file:///tmp/work.py");
console.log(JSON.stringify({{ argv }}));
""")
        self.assertEqual(full["argv"],
                         ["python3", "/repo/scripts/session_capture.py",
                          "prepare", "--capture", "7",
                          "--session", sid, "--ref", "file:///tmp/work.py"])
        # Degraded: blank session/ref add no argv; malformed sid is refused.
        blank = run_capture(
            "",
            'console.log(JSON.stringify({ argv: context.capturePrepareCommand(7, "", "") }));')
        self.assertEqual(blank["argv"],
                         ["python3", "/repo/scripts/session_capture.py",
                          "prepare", "--capture", "7"])
        bad = run_capture(
            "",
            'console.log(JSON.stringify({ argv: context.capturePrepareCommand(7, "nope") }));')
        self.assertEqual(bad["argv"], [])
        empty = run_capture(
            "",
            'console.log(JSON.stringify({ argv: context.capturePrepareCommand("") }));')
        self.assertEqual(empty["argv"], [])

    def test_prepare_guards_busy_applying_and_session(self):
        busy = run_capture(
            "context.captureBusy = true;",
            'console.log(JSON.stringify({ ok: context.prepareCapture(1) }));')
        self.assertFalse(busy["ok"])
        applying = run_capture(
            "context.captureBusy = false; context.captureApplying = true;",
            'console.log(JSON.stringify({ ok: context.prepareCapture(1) }));')
        self.assertFalse(applying["ok"])
        bad_session = run_capture(
            "context.captureBusy = false; context.captureApplying = false; "
            "context.captureProcess.running = false; context.captureRetiring = false;",
            'console.log(JSON.stringify({ ok: context.prepareCapture(1, "nope") }));')
        self.assertFalse(bad_session["ok"])
        sid = "b" * 32
        ready = run_capture(
            "context.captureBusy = false; context.captureApplying = false; "
            "context.captureProcess.running = false; context.captureRetiring = false;",
            f'console.log(JSON.stringify({{ ok: context.prepareCapture(1, "{sid}", "ref") }}));')
        self.assertTrue(ready["ok"])

    def test_finish_prepare_sets_preview(self):
        preview = {"target": "page", "path": "pages/Demo.md",
                   "page": "Demo", "graphName": "Notes",
                   "revision": "r1", "block": "- TODO Ship it"}
        payload = json.dumps({"prepared": "cap_deadbeef", "target": "page",
                              "capture_id": 3, "preview": preview,
                              "expires_in": 600})
        value = run_capture(
            self.PREP_FLIGHT,
            f"""
const applied = context.finishPrepare(0, {json.dumps(payload)}, 5, "2026-09-13", "prepare");
console.log(JSON.stringify({{ applied, token: context.capturePreviewToken,
  block: context.capturePreview ? context.capturePreview.block : null,
  err: context.captureApplyError, busy: context.captureBusy }}));
""")
        self.assertTrue(value["applied"])
        self.assertEqual(value["token"], "cap_deadbeef")
        self.assertEqual(value["block"], "- TODO Ship it")
        self.assertEqual(value["err"], "")
        self.assertFalse(value["busy"])

    def test_finish_prepare_rejects_today_preview(self):
        preview = {"target": "today", "path": "pages/Demo.md",
                   "page": "Demo", "graphName": "Notes",
                   "revision": "r1", "block": "- TODO Ship it"}
        payload = json.dumps({"prepared": "cap_deadbeef", "target": "today",
                              "capture_id": 3, "preview": preview,
                              "expires_in": 600})
        value = run_capture(
            self.PREP_FLIGHT,
            f"""
const applied = context.finishPrepare(0, {json.dumps(payload)}, 5, "2026-09-13", "prepare");
console.log(JSON.stringify({{ applied, preview: context.capturePreview,
  err: context.captureApplyError }}));
""")
        self.assertFalse(value["applied"])
        self.assertIsNone(value["preview"])
        self.assertTrue(value["err"] != "")

    def test_apply_success_reports_project_page_only(self):
        payload = json.dumps({"applied": True, "target": "page",
                              "capture_id": 3, "path": "pages/Demo.md",
                              "revision": "r2"})
        applied = run_capture(
            self.APPLY_FLIGHT,
            f"""
context.root.reloadCaptures = function() {{ this.reloadCalls++; return true; }};
context.reloadCaptures = context.root.reloadCaptures;
const ok = context.finishApply(0, {json.dumps(payload)}, 6, "2026-09-13", "apply");
console.log(JSON.stringify({{ ok, note: context.captureNotice }}));
""")
        self.assertTrue(applied["ok"])
        self.assertEqual(applied["note"], "Saved to project page.")

    def test_apply_argv_and_success_clears_preview_and_reloads(self):
        value = run_capture(
            "",
            'console.log(JSON.stringify({ argv: context.captureApplyCommand("cap_abc") }));')
        self.assertEqual(value["argv"],
                         ["python3", "/repo/scripts/session_capture.py",
                          "apply", "--prepared", "cap_abc"])
        payload = json.dumps({"applied": True, "target": "page",
                              "capture_id": 3, "path": "pages/Demo.md",
                              "revision": "r2"})
        # finishApply calls reloadCaptures; stub it to count without launching.
        applied = run_capture(
            self.APPLY_FLIGHT,
            f"""
context.root.reloadCaptures = function() {{ this.reloadCalls++; return true; }};
context.reloadCaptures = context.root.reloadCaptures;
const ok = context.finishApply(0, {json.dumps(payload)}, 6, "2026-09-13", "apply");
console.log(JSON.stringify({{ ok, preview: context.capturePreview,
  token: context.capturePreviewToken, applying: context.captureApplying,
  reloads: context.reloadCalls }}));
""")
        self.assertTrue(applied["ok"])
        self.assertIsNone(applied["preview"])
        self.assertEqual(applied["token"], "")
        self.assertFalse(applied["applying"])
        self.assertEqual(applied["reloads"], 1)

    def test_apply_failure_keeps_preview_with_bounded_error(self):
        payload = json.dumps({"error": "stale revision; reload"})
        value = run_capture(
            self.APPLY_FLIGHT,
            f"""
const ok = context.finishApply(1, {json.dumps(payload)}, 6, "2026-09-13", "apply");
console.log(JSON.stringify({{ ok, kept: context.capturePreview !== null,
  err: context.captureApplyError, applying: context.captureApplying }}));
""")
        self.assertFalse(value["ok"])
        self.assertTrue(value["kept"])
        self.assertIn("stale", value["err"])
        self.assertFalse(value["applying"])

    def test_cancel_preview(self):
        value = run_capture(
            ("context.capturePreview = {target: 'page', block: 'x'}; "
             "context.capturePreviewToken = 'cap_x'; "
             "context.captureApplyError = 'boom'; context.captureApplying = false;"),
            """
const ok = context.cancelCapturePreview();
console.log(JSON.stringify({ ok, preview: context.capturePreview,
  token: context.capturePreviewToken, err: context.captureApplyError }));
""")
        self.assertTrue(value["ok"])
        self.assertIsNone(value["preview"])
        self.assertEqual(value["token"], "")
        self.assertEqual(value["err"], "")


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class CaptureDismissScanTests(unittest.TestCase):
    def test_dismiss_argv(self):
        value = run_capture(
            "",
            'console.log(JSON.stringify({ argv: context.captureDismissCommand(9) }));')
        self.assertEqual(value["argv"],
                         ["python3", "/repo/scripts/session_capture.py",
                          "set-status", "--id", "9",
                          "--status", "dismissed"])

    def test_scan_now_delegates(self):
        missing = run_capture(
            "context.scheduler = null;",
            "console.log(JSON.stringify({ ok: context.scanNow(), note: context.captureNotice }));")
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["note"], "Scan unavailable")
        busy = run_capture(
            "context.scheduler = { requestScan() { return false; } };",
            "console.log(JSON.stringify({ ok: context.scanNow(), note: context.captureNotice }));")
        self.assertFalse(busy["ok"])
        self.assertEqual(busy["note"], "Scan running")
        live = run_capture(
            "context.scheduler = { requestScan() { return true; } };",
            "console.log(JSON.stringify({ ok: context.scanNow(), note: context.captureNotice }));")
        self.assertTrue(live["ok"])
        self.assertEqual(live["note"], "")


class CaptureInboxStructureTests(unittest.TestCase):
    def test_card_binds_agenda_and_hides_when_null(self):
        self.assertIn("property var agenda", INBOX)
        self.assertIn("visible: !!root.agenda", INBOX)
        self.assertIn("Captured", INBOX)
        self.assertIn("captureItems", INBOX)
        self.assertIn("reloadCaptures()", INBOX)
        self.assertIn("onVisibleChanged", INBOX)
        self.assertIn("Component.onCompleted", INBOX)

    def test_completed_hook_gated_on_visible(self):
        start = INBOX.index("Component.onCompleted")
        opening = INBOX.index("{", start)
        depth = 0
        for index in range(opening, len(INBOX)):
            if INBOX[index] == "{":
                depth += 1
            elif INBOX[index] == "}":
                depth -= 1
                if depth == 0:
                    block = INBOX[start:index + 1]
                    break
        else:
            self.fail("unterminated Component.onCompleted")
        self.assertIn("reloadCaptures", block)
        self.assertIn("visible", block)

    def test_scan_button_and_notices(self):
        self.assertIn('objectName: "captureScanButton"', INBOX)
        self.assertIn("Scan now", INBOX)
        self.assertIn("scanNow()", INBOX)
        self.assertIn("captureBusy", INBOX)
        self.assertIn("scanBusy", INBOX)
        self.assertIn("captureNotice", INBOX)
        self.assertIn("captureError", INBOX)
        # P4 card chrome: every agenda-backed card offers Refresh with
        # Loading… while busy; Scan now stays supplemental.
        self.assertIn('objectName: "captureRefreshButton"', INBOX)
        self.assertIn('"Refresh captures"', INBOX)
        self.assertIn("reloadCaptures()", INBOX)
        refresh_at = INBOX.index('objectName: "captureRefreshButton"')
        refresh_block = INBOX[max(0, refresh_at - 200):refresh_at + 600]
        self.assertIn('"Loading…"', refresh_block)
        self.assertIn('"Refresh"', refresh_block)
        self.assertIn("captureApplying", refresh_block)
        self.assertIn("scanBusy", refresh_block)

    def test_list_rows_with_actions(self):
        self.assertIn("slice(0, 20)", INBOX)
        self.assertIn('objectName: "captureAddPage"', INBOX)
        self.assertNotIn('objectName: "captureAddToday"', INBOX)
        self.assertIn('objectName: "captureDismiss"', INBOX)
        # Slimmed verbs: one Accept (project page) + Dismiss.
        self.assertIn('"Accept"', INBOX)
        self.assertNotIn('"Add to page"', INBOX)
        self.assertNotIn('"Add to today"', INBOX)
        self.assertIn('"Dismiss"', INBOX)
        self.assertIn("without writing to Logseq", INBOX)
        self.assertIn("prepareCapture(modelData.id", INBOX)
        # L1 single target: Accept passes no target argument (the
        # session-scoped SessionCard will supply session/ref later).
        self.assertNotIn('prepareCapture(modelData.id, "page")', INBOX)
        self.assertNotIn('prepareCapture(modelData.id, "today")', INBOX)
        self.assertIn("dismissCapture(modelData.id", INBOX)
        self.assertIn("modelData.kind", INBOX)
        self.assertIn("modelData.text", INBOX)
        self.assertIn("index", INBOX)

    def test_exact_preview_panel_uses_shared_component(self):
        self.assertIn("capturePreview", INBOX)
        self.assertIn("capturePreviewToken", INBOX)
        self.assertIn("captureApplying", INBOX)
        self.assertIn("captureApplyError", INBOX)
        self.assertIn("PreviewPanel {", INBOX)
        panel_at = INBOX.index("PreviewPanel {")
        panel_block = INBOX[panel_at:panel_at + 2500]
        self.assertIn('confirmObjectName: "captureConfirm"', panel_block)
        self.assertIn('cancelObjectName: "captureCancel"', panel_block)
        self.assertIn("capturePreviewToken", panel_block)
        self.assertIn("onConfirmRequested", panel_block)
        self.assertIn("applyCapture()", panel_block)
        self.assertIn("onCancelRequested", panel_block)
        self.assertIn("cancelCapturePreview()", panel_block)
        # Exact block in the shared bounded readonly surface.
        self.assertIn("capturePreview.block", INBOX)
        self.assertIn(".block", INBOX)
        self.assertIn("readOnly: true", PANEL)
        self.assertIn("monospace", PANEL)
        # Destination line: page name/path (page-only target).
        self.assertNotIn("Today", INBOX)
        self.assertIn("preview.page", INBOX)
        self.assertIn("preview.path", INBOX)

    def test_no_backend_or_modal_strings(self):
        lowered = INBOX.lower()
        self.assertNotIn("scopedagent", lowered)
        self.assertNotIn("annotations.py", lowered)
        self.assertNotIn("session_capture.py", lowered)
        self.assertNotIn("memory_tick.py", lowered)
        self.assertNotIn("shellpath", lowered)
        self.assertNotIn("process {", lowered)
        # No pop-up dialogs; inline text only (the word "modal" itself
        # must not appear even in comments).
        self.assertNotIn("dialog", lowered)
        self.assertNotIn("execute", lowered)


class PlannerAndShellWiringTests(unittest.TestCase):
    def test_planner_embeds_capture_inbox(self):
        self.assertIn("CaptureInbox {", PLANNER_UI)
        self.assertIn("agenda: root.agenda", PLANNER_UI)

    def test_shell_passes_scheduler(self):
        self.assertIn("DailyAgenda {", SHELL)
        self.assertIn("id: dailyAgenda", SHELL)
        self.assertIn("scheduler: memoryScheduler", SHELL)
        self.assertIn("MemoryScheduler", SHELL)
        self.assertIn("id: memoryScheduler", SHELL)


if __name__ == "__main__":
    unittest.main()
