"""Workstream E evening review / morning plan UI: DailyAgenda state + ReviewCard.

Covers the plan section 7E UI half without a live Quickshell harness:
DailyAgenda review Process/op ladder (get/refresh/prepare-save/
apply), generation + selected-date staleness guards, bounded single-line
errors, top-item scheduling through the existing agenda select op, plus
the ReviewCard presentation card (objectNames, Evening/Morning tabs,
exact journal preview, Confirm gating), the DailyPlanner embed, and
the scheduler refresh wiring.
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
CARD = (ROOT / "widgets" / "ReviewCard.qml").read_text(encoding="utf-8")
PLANNER_UI = (ROOT / "widgets" / "DailyPlanner.qml").read_text(encoding="utf-8")
PANEL = (ROOT / "widgets" / "PreviewPanel.qml").read_text(encoding="utf-8")
# SessionCard.qml replaced LedgerCard.qml (Phase 2b §4.5); the review
# surface only needs the shared planner/panel around it.


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


REVIEW_FUNCTIONS = [
    "reviewBound", "reviewFailure", "isReviewKind",
    "reviewGetCommand",
    "reviewPrepareCommand", "reviewApplyCommand",
    "launchReview", "reloadReview", "refreshReview", "setReviewKind",
    "reviewBoundMarkdown", "finishReview",
    "prepareReviewSave", "finishReviewPrepare",
    "applyReviewSave", "finishReviewApply", "cancelReviewPreview",
    "scheduleReviewItem",
    "handleReviewExited", "handleReviewRunningChanged",
    "handleReviewTimeout", "fireReviewKillTimeout",
    "handleReviewStartFailure",
    "isValidIso", "parseIsoDate",
    "launch", "graphArg",
]

# Stub that counting tests install in the exercise phase (after
# context.root exists) to observe reloads without launching.
COUNT_RELOAD = (
    "context.root.reloadReview = function() { this.reloadCalls++; return true; };\n"
    "context.reloadReview = context.root.reloadReview;\n"
)


def review_harness(setup, exercise):
    functions = [extract_function(AGENDA, name) for name in REVIEW_FUNCTIONS]
    sources = json.dumps(functions)
    return f"""
const vm = require("vm");
const context = {{
  selectedDate: "2026-09-13",
  reviewKind: "evening", reviewPayload: null,
  reviewBusy: false, reviewStarted: false, reviewStartFailed: false,
  reviewRetiring: false, reviewGeneration: 0, reviewLaunchGeneration: -1,
  reviewOp: "", reviewLaunchOp: "", reviewLaunchDate: "",
  reviewError: "", reviewNotice: "",
  reviewPreview: null, reviewPreviewToken: "",
  reviewApplying: false, reviewApplyError: "",
  agendaBusy: false, agendaRetiring: false, agendaGeneration: 0,
  agendaLaunchGeneration: 0, agendaOp: "", agendaLaunchOp: "",
  agendaPayload: null, agendaError: "", agendaNotice: "",
  agendaStarted: false, agendaStartFailed: false, agendaSlow: false,
  completionSaving: false,
  reloadCalls: 0,
  reviewWatchdog: {{ stopped: false, restarted: false,
    stop() {{ this.stopped = true; }}, restart() {{ this.restarted = true; }} }},
  reviewKillTimer: {{ stopped: false, restarted: false,
    stop() {{ this.stopped = true; }}, restart() {{ this.restarted = true; }} }},
  reviewProcess: {{ running: false, command: [], signaled: [],
    signal(sig) {{ this.signaled.push(sig); return true; }},
    closeWriteChannel() {{}} , stdinEnabled: false }},
  agendaProcess: {{ running: false, command: [], signaled: [],
    signal(sig) {{ this.signaled.push(sig); return true; }},
    closeWriteChannel() {{}} , stdinEnabled: false }},
  agendaTimeout: {{ restarted: false, restart() {{ this.restarted = true; }} }},
  agendaWriteWarning: {{ restarted: false, restart() {{ this.restarted = true; }} }},
  Quickshell: {{ shellPath(value) {{ return "/repo/" + value; }}, env(v) {{ return ""; }} }},
}};
{setup}
context.root = context;
context.closeInput = function(process) {{ }};
vm.createContext(context);
for (const value of {sources}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
context.root = context;
{exercise}
"""


def run_review(setup, exercise):
    return run_node(review_harness(setup, exercise))


def card_harness(setup, exercise, functions=("boundMarkdown",)):
    extracted = [extract_function(CARD, name) for name in functions]
    sources = json.dumps(extracted)
    return f"""
const vm = require("vm");
const context = {{}};
{setup}
context.root = context;
vm.createContext(context);
for (const value of {sources}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
context.root = context;
{exercise}
"""


def run_card(setup, exercise, functions=("boundMarkdown",)):
    return run_node(card_harness(setup, exercise, functions))


def sample_sections():
    return {
        "projects": [{"project_id": "p", "name": "Demo",
                      "sessions": 2, "minutes": 90}],
        "changes": [{"project_id": "p", "name": "Demo",
                     "commits": "abc1234..def5678", "note": "ship"}],
        "captures": {"new": 1, "accepted": 2, "applied": 0},
        "todos": {"scheduled": 3, "completed": 1},
        "journal": {"present": True},
    }


def sample_top(count=2):
    return [{"task": "task %d" % i, "path": "pages/A.md", "line": i + 1,
             "page": "A", "revision": "r%d" % i, "score": None,
             "target_date": "2026-09-14"} for i in range(count)]


def sample_review(markdown="hello", top=None, polish_enabled=True):
    return {"generated_ms": 5, "polished": False, "saved_ms": None,
            "prioritized": False, "polish_enabled": polish_enabled,
            "sections": sample_sections(),
            "top": sample_top() if top is None else top,
            "markdown": markdown}


def found_payload(review=None, found=True, reason="ok"):
    payload = {"kind": "evening", "day": "2026-09-13",
               "found": found, "reason": reason}
    if review is not None:
        payload["review"] = review
    return json.dumps(payload)


class ReviewStructureTests(unittest.TestCase):
    def test_state_properties(self):
        for token in ("property string reviewKind: \"evening\"",
                      "property var reviewPayload: null",
                      "property bool reviewBusy: false",
                      "property bool reviewRetiring: false",
                      "property int reviewGeneration: 0",
                      "property int reviewLaunchGeneration",
                      "property string reviewOp",
                      "property string reviewLaunchOp",
                      "property string reviewLaunchDate",
                      "property string reviewError",
                      "property string reviewNotice",
                      "property var reviewPreview: null",
                      "property string reviewPreviewToken",
                      "property bool reviewApplying: false",
                      "property string reviewApplyError"):
            self.assertIn(token, AGENDA)

    def test_process_watchdog_and_argv(self):
        self.assertIn("id: reviewProcess", AGENDA)
        self.assertIn('Quickshell.shellPath("scripts/daily_review.py")', AGENDA)
        self.assertIn('"python3"', AGENDA)
        self.assertIn('"get"', AGENDA)
        self.assertIn('"--kind"', AGENDA)
        self.assertIn('"--date"', AGENDA)
        self.assertIn('"--refresh"', AGENDA)
        self.assertNotIn('"polish"', AGENDA)
        self.assertIn('"prepare-save"', AGENDA)
        self.assertIn('"apply"', AGENDA)
        self.assertIn('"--prepared"', AGENDA)
        self.assertIn('"evening"', AGENDA)
        self.assertIn('"morning"', AGENDA)
        self.assertIn("workingDirectory: Quickshell.shellPath", AGENDA)
        self.assertIn("id: reviewWatchdog", AGENDA)
        self.assertIn("interval: 12000", AGENDA)
        self.assertIn("id: reviewKillTimer", AGENDA)
        self.assertIn("interval: 3000", AGENDA)
        self.assertIn("signal(9)", AGENDA)
        for name in ("reloadReview", "refreshReview", "setReviewKind",
                     "finishReview",
                     "prepareReviewSave", "finishReviewPrepare",
                     "applyReviewSave", "finishReviewApply",
                     "cancelReviewPreview", "scheduleReviewItem"):
            self.assertIn("function " + name + "(", AGENDA)
        self.assertNotIn("sh -c", AGENDA)

    def test_bounded_single_line_errors_never_modal(self):
        lowered = AGENDA.lower()
        self.assertIn("reviewbound", lowered)
        self.assertIn("reviewfailure", lowered)
        # The review section adds no pop-up surface.
        review_region = AGENDA[AGENDA.index("reviewKind"):]
        self.assertNotIn("PopupWindow", review_region)

    def test_refresh_wiring(self):
        self.assertIn("target: root.scheduler", AGENDA)
        start = AGENDA.index("target: root.scheduler")
        block = AGENDA[start:start + 600]
        self.assertIn("onDataChanged", block)
        self.assertIn("root.reloadCaptures()", block)
        self.assertIn("root.reloadReview()", block)
        setter = extract_function(AGENDA, "setSelectedDate")
        self.assertIn("reloadCaptures", setter)
        self.assertIn("reloadReview", setter)

    def test_no_auto_review_load_on_completed(self):
        # DailyAgenda itself never auto-loads the review: the card drives
        # the initial load via its own visible/completed hooks.
        tail = AGENDA[AGENDA.index("Component.onCompleted"):]
        self.assertIn("root.startList()", tail)
        self.assertNotIn("reloadReview", tail)

    def test_schedule_reuses_the_shared_select_op(self):
        body = extract_function(AGENDA, "scheduleReviewItem")
        self.assertIn('root.launch("select"', body)
        self.assertIn("target_date", body)
        self.assertIn("selected: true", body)
        # No second backend process for scheduling: only the shared
        # agenda select op is used.
        self.assertNotIn("launchReview", body)
        self.assertNotIn("daily_review.py", body)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class ReviewGetTests(unittest.TestCase):
    def test_get_argv_exact(self):
        value = run_review(
            "",
            """
const argv = context.reviewGetCommand("evening", "2026-09-13", false);
console.log(JSON.stringify({ argv }));
""")
        self.assertEqual(value["argv"],
                         ["python3", "/repo/scripts/daily_review.py",
                          "get", "--kind", "evening",
                          "--date", "2026-09-13"])

    def test_refresh_argv_appends_refresh(self):
        value = run_review(
            "",
            """
const argv = context.reviewGetCommand("morning", "2026-09-13", true);
console.log(JSON.stringify({ argv }));
""")
        self.assertEqual(value["argv"],
                         ["python3", "/repo/scripts/daily_review.py",
                          "get", "--kind", "morning",
                          "--date", "2026-09-13", "--refresh"])

    def test_argv_rejects_bad_kind_and_date(self):
        for kind, date in (("noon", "2026-09-13"), ("evening", "nope"),
                           ("", "2026-09-13"), ("evening", "")):
            value = run_review(
                "",
                f"""
const argv = context.reviewGetCommand({json.dumps(kind)}, {json.dumps(date)}, false);
console.log(JSON.stringify({{ argv }}));
""")
            self.assertEqual(value["argv"], [], (kind, date))

    def test_prepare_apply_argv(self):
        prepare = run_review(
            "",
            'console.log(JSON.stringify({ argv: context.reviewPrepareCommand("morning", "2026-09-13") }));')
    def test_reload_launches_get_with_active_kind(self):
        value = run_review(
            "context.reviewKind = 'morning';",
            """
const ok = context.reloadReview();
console.log(JSON.stringify({ ok, op: context.reviewLaunchOp,
  kind: context.reviewProcess.command[4], busy: context.reviewBusy }));
""")
        self.assertTrue(value["ok"])
        self.assertEqual(value["op"], "get")
        self.assertEqual(value["kind"], "morning")
        self.assertTrue(value["busy"])

    def test_refresh_launches_refresh_op(self):
        value = run_review(
            "",
            """
const ok = context.refreshReview();
console.log(JSON.stringify({ ok, op: context.reviewLaunchOp,
  flag: context.reviewProcess.command[context.reviewProcess.command.length - 1] }));
""")
        self.assertTrue(value["ok"])
        self.assertEqual(value["op"], "refresh")
        self.assertEqual(value["flag"], "--refresh")

    def test_reload_noop_while_busy_or_applying(self):
        busy = run_review(
            "context.reviewBusy = true;",
            "console.log(JSON.stringify({ ok: context.reloadReview() }));")
        self.assertFalse(busy["ok"])
        applying = run_review(
            "context.reviewBusy = false; context.reviewApplying = true;",
            "console.log(JSON.stringify({ ok: context.refreshReview() }));")
        self.assertFalse(applying["ok"])

    def test_set_review_kind_switches_and_reloads(self):
        value = run_review(
            "context.reviewKind = 'evening';",
            COUNT_RELOAD + """
const ok = context.setReviewKind("morning");
console.log(JSON.stringify({ ok, kind: context.reviewKind,
  reloads: context.reloadCalls, err: context.reviewError }));
""")
        self.assertTrue(value["ok"])
        self.assertEqual(value["kind"], "morning")
        self.assertEqual(value["reloads"], 1)
        self.assertEqual(value["err"], "")

    def test_set_review_kind_rejects_unknown(self):
        value = run_review(
            "context.reviewKind = 'evening';",
            """
const ok = context.setReviewKind("noon");
console.log(JSON.stringify({ ok, kind: context.reviewKind, err: context.reviewError }));
""")
        self.assertFalse(value["ok"])
        self.assertEqual(value["kind"], "evening")
        self.assertTrue(value["err"] != "")


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class FinishReviewTests(unittest.TestCase):
    IN_FLIGHT = ("context.reviewGeneration = 4; context.reviewLaunchGeneration = 4; "
                 "context.reviewOp = 'get'; context.reviewLaunchOp = 'get'; "
                 "context.reviewLaunchDate = '2026-09-13'; "
                 "context.selectedDate = '2026-09-13'; "
                 "context.reviewBusy = true; context.reviewRetiring = false; "
                 "context.reviewProcess.running = false;")

    def test_finish_stores_bounded_payload(self):
        top = sample_top(5)
        payload = found_payload(sample_review(markdown="x" * 9000, top=top))
        value = run_review(
            self.IN_FLIGHT,
            f"""
const applied = context.finishReview(0, {json.dumps(payload)}, 4, "2026-09-13", "get");
console.log(JSON.stringify({{ applied, err: context.reviewError,
  busy: context.reviewBusy, found: context.reviewPayload ? context.reviewPayload.found : null,
  markdownLen: context.reviewPayload.review.markdown.length,
  top: context.reviewPayload.review.top.length }}));
""")
        self.assertTrue(value["applied"])
        self.assertEqual(value["err"], "")
        self.assertFalse(value["busy"])
        self.assertTrue(value["found"])
        self.assertLessEqual(value["markdownLen"], 8000)
        self.assertEqual(value["top"], 3)

    def test_finish_found_false_keeps_reason(self):
        payload = found_payload(found=False, reason="not_due")
        value = run_review(
            self.IN_FLIGHT,
            f"""
const applied = context.finishReview(0, {json.dumps(payload)}, 4, "2026-09-13", "get");
console.log(JSON.stringify({{ applied, err: context.reviewError,
  found: context.reviewPayload ? context.reviewPayload.found : null,
  reason: context.reviewPayload ? context.reviewPayload.reason : null }}));
""")
        self.assertTrue(value["applied"])
        self.assertEqual(value["err"], "")
        self.assertFalse(value["found"])
        self.assertEqual(value["reason"], "not_due")

    def test_finish_drops_stale_generation_date_and_op(self):
        payload = found_payload(sample_review())
        stale_gen = run_review(
            self.IN_FLIGHT,
            f"""
const applied = context.finishReview(0, {json.dumps(payload)}, 3, "2026-09-13", "get");
console.log(JSON.stringify({{ applied }}));
""")
        self.assertFalse(stale_gen["applied"])
        stale_date = run_review(
            self.IN_FLIGHT + "context.selectedDate = '2026-09-14';",
            f"""
const applied = context.finishReview(0, {json.dumps(payload)}, 4, "2026-09-13", "get");
console.log(JSON.stringify({{ applied }}));
""")
        self.assertFalse(stale_date["applied"])
        wrong_op = run_review(
            self.IN_FLIGHT,
            f"""
const applied = context.finishReview(0, {json.dumps(payload)}, 4, "2026-09-13", "polish");
console.log(JSON.stringify({{ applied }}));
""")
        self.assertFalse(wrong_op["applied"])

    def test_finish_rejects_bad_shapes_boundedly(self):
        bad = ['"not json"', '{}',
               json.dumps({"found": True}),
               json.dumps({"found": True, "review": {"markdown": "", "top": [], "sections": {}}}),
               json.dumps({"found": True, "review": {"markdown": "x", "top": {}, "sections": {}}}),
               json.dumps({"error": "boom"})]
        for payload in bad:
            with self.subTest(payload=payload):
                value = run_review(
                    self.IN_FLIGHT,
                    f"""
const applied = context.finishReview(1, {json.dumps(payload)}, 4, "2026-09-13", "get");
console.log(JSON.stringify({{ applied, err: context.reviewError }}));
""")
                self.assertFalse(value["applied"])
                self.assertTrue(value["err"] != "")
                self.assertLessEqual(len(value["err"]), 200)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class ReviewPrepareApplyTests(unittest.TestCase):
    PREP_FLIGHT = ("context.reviewGeneration = 6; context.reviewLaunchGeneration = 6; "
                   "context.reviewOp = 'prepare'; context.reviewLaunchOp = 'prepare'; "
                   "context.reviewLaunchDate = '2026-09-13'; "
                   "context.selectedDate = '2026-09-13'; "
                   "context.reviewBusy = true; context.reviewRetiring = false; "
                   "context.reviewProcess.running = false;")
    APPLY_FLIGHT = ("context.reviewGeneration = 7; context.reviewLaunchGeneration = 7; "
                    "context.reviewOp = 'apply'; context.reviewLaunchOp = 'apply'; "
                    "context.reviewLaunchDate = '2026-09-13'; "
                    "context.selectedDate = '2026-09-13'; "
                    "context.reviewBusy = true; context.reviewApplying = true; "
                    "context.reviewRetiring = false; "
                    "context.reviewProcess.running = false; "
                    "context.reviewPreview = {target: 'journal', addition: '- review'}; "
                    "context.reviewPreviewToken = 'rev_abc';")

    def test_prepare_requires_a_loaded_review(self):
        missing = run_review(
            "context.reviewPayload = null;",
            "console.log(JSON.stringify({ ok: context.prepareReviewSave() }));")
        self.assertFalse(missing["ok"])
        not_due = run_review(
            "context.reviewPayload = {found: false, reason: 'not_due'};",
            "console.log(JSON.stringify({ ok: context.prepareReviewSave() }));")
        self.assertFalse(not_due["ok"])

    def test_finish_prepare_sets_preview_and_token(self):
        preview = {"target": "journal", "path": "journals/2026_09_13.md",
                   "date": "2026-09-13", "revision": "r1",
                   "addition": "- Evening review"}
        payload = json.dumps({"prepared": "rev_deadbeef", "target": "journal",
                              "kind": "evening", "day": "2026-09-13",
                              "preview": preview, "revision": "r1",
                              "expires_in": 600})
        value = run_review(
            self.PREP_FLIGHT,
            f"""
const applied = context.finishReviewPrepare(0, {json.dumps(payload)}, 6, "2026-09-13", "prepare");
console.log(JSON.stringify({{ applied, token: context.reviewPreviewToken,
  addition: context.reviewPreview ? context.reviewPreview.addition : null,
  err: context.reviewApplyError, busy: context.reviewBusy }}));
""")
        self.assertTrue(value["applied"])
        self.assertEqual(value["token"], "rev_deadbeef")
        self.assertEqual(value["addition"], "- Evening review")
        self.assertEqual(value["err"], "")
        self.assertFalse(value["busy"])

    def test_finish_prepare_rejects_wrong_target(self):
        preview = {"target": "page", "path": "pages/X.md",
                   "date": "2026-09-13", "revision": "r1",
                   "addition": "- Evening review"}
        payload = json.dumps({"prepared": "rev_x", "preview": preview})
        value = run_review(
            self.PREP_FLIGHT,
            f"""
const applied = context.finishReviewPrepare(0, {json.dumps(payload)}, 6, "2026-09-13", "prepare");
console.log(JSON.stringify({{ applied, err: context.reviewApplyError }}));
""")
        self.assertFalse(value["applied"])
        self.assertTrue(value["err"] != "")

    def test_apply_success_clears_preview_and_reloads(self):
        payload = json.dumps({"applied": True, "target": "journal",
                              "kind": "evening", "day": "2026-09-13",
                              "path": "journals/2026_09_13.md",
                              "revision": "r2"})
        value = run_review(
            self.APPLY_FLIGHT,
            COUNT_RELOAD + f"""
const ok = context.finishReviewApply(0, {json.dumps(payload)}, 7, "2026-09-13", "apply");
console.log(JSON.stringify({{ ok, preview: context.reviewPreview,
  token: context.reviewPreviewToken, applying: context.reviewApplying,
  reloads: context.reloadCalls }}));
""")
        self.assertTrue(value["ok"])
        self.assertIsNone(value["preview"])
        self.assertEqual(value["token"], "")
        self.assertFalse(value["applying"])
        self.assertEqual(value["reloads"], 1)

    def test_apply_failure_keeps_preview_with_bounded_error(self):
        payload = json.dumps({"error": "stale revision; reload"})
        value = run_review(
            self.APPLY_FLIGHT,
            f"""
const ok = context.finishReviewApply(1, {json.dumps(payload)}, 7, "2026-09-13", "apply");
console.log(JSON.stringify({{ ok, kept: context.reviewPreview !== null,
  err: context.reviewApplyError, applying: context.reviewApplying }}));
""")
        self.assertFalse(value["ok"])
        self.assertTrue(value["kept"])
        self.assertIn("stale", value["err"])
        self.assertLessEqual(len(value["err"]), 200)
        self.assertFalse(value["applying"])

    def test_cancel_preview(self):
        value = run_review(
            ("context.reviewPreview = {target: 'journal', addition: 'x'}; "
             "context.reviewPreviewToken = 'rev_x'; "
             "context.reviewApplyError = 'boom'; context.reviewApplying = false;"),
            """
const ok = context.cancelReviewPreview();
console.log(JSON.stringify({ ok, preview: context.reviewPreview,
  token: context.reviewPreviewToken, err: context.reviewApplyError }));
""")
        self.assertTrue(value["ok"])
        self.assertIsNone(value["preview"])
        self.assertEqual(value["token"], "")
        self.assertEqual(value["err"], "")


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class ScheduleReviewItemTests(unittest.TestCase):
    def test_builds_the_exact_select_payload(self):
        item = {"task": "Follow up", "path": "pages/Work.md",
                "line": 4, "page": "Work", "revision": "rev9",
                "score": None, "target_date": "2026-09-14"}
        value = run_review(
            "",
            f"""
const ok = context.scheduleReviewItem({json.dumps(item)});
console.log(JSON.stringify({{ ok, op: context.agendaLaunchOp,
  payload: context.agendaPayload, command: context.agendaProcess.command }}));
""")
        self.assertTrue(value["ok"])
        self.assertEqual(value["op"], "select")
        self.assertEqual(value["payload"],
                         {"path": "pages/Work.md", "revision": "rev9",
                          "line": 4, "date": "2026-09-14",
                          "selected": True})
        self.assertEqual(value["command"][-1], "select")

    def test_refuses_while_agenda_busy(self):
        item = {"task": "Follow up", "path": "pages/Work.md",
                "line": 4, "revision": "rev9",
                "target_date": "2026-09-14"}
        busy = run_review(
            "context.agendaBusy = true;",
            f"""
const ok = context.scheduleReviewItem({json.dumps(item)});
console.log(JSON.stringify({{ ok, err: context.agendaError }}));
""")
        self.assertFalse(busy["ok"])
        self.assertTrue(busy["err"] != "")
        saving = run_review(
            "context.completionSaving = true;",
            f"""
const ok = context.scheduleReviewItem({json.dumps(item)});
console.log(JSON.stringify({{ ok, err: context.agendaError }}));
""")
        self.assertFalse(saving["ok"])
        self.assertTrue(saving["err"] != "")

    def test_refuses_items_without_a_target(self):
        for item in ({}, {"task": "x"},
                     {"path": "", "target_date": "2026-09-14"},
                     {"path": "pages/Work.md", "target_date": "nope"}):
            with self.subTest(item=item):
                value = run_review(
                    "",
                    f"""
const ok = context.scheduleReviewItem({json.dumps(item)});
console.log(JSON.stringify({{ ok, err: context.agendaError }}));
""")
                self.assertFalse(value["ok"])
                self.assertTrue(value["err"] != "")


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class ReviewExitTimeoutTests(unittest.TestCase):
    def test_exited_routes_get_through_finish(self):
        payload = found_payload(sample_review())
        value = run_review(
            ("context.reviewGeneration = 8; context.reviewLaunchGeneration = 8; "
             "context.reviewOp = 'get'; context.reviewLaunchOp = 'get'; "
             "context.reviewLaunchDate = '2026-09-13'; "
             "context.selectedDate = '2026-09-13'; "
             "context.reviewBusy = true; context.reviewRetiring = false; "
             "context.reviewProcess.running = false;"),
            f"""
const applied = context.handleReviewExited(0, {json.dumps(payload)});
console.log(JSON.stringify({{ applied, found: context.reviewPayload.found,
  launch: context.reviewLaunchGeneration }}));
""")
        self.assertTrue(value["applied"])
        self.assertTrue(value["found"])
        self.assertEqual(value["launch"], -1)

    def test_exited_drops_late_results_when_idle(self):
        value = run_review(
            ("context.reviewBusy = false; context.reviewRetiring = false; "
             "context.reviewStarted = false; context.reviewLaunchGeneration = -1;"),
            """
const applied = context.handleReviewExited(0, "{}");
console.log(JSON.stringify({ applied }));
""")
        self.assertFalse(value["applied"])

    def test_timeout_retires_a_busy_review(self):
        value = run_review(
            "context.reviewBusy = true; context.reviewRetiring = false;",
            """
const retired = context.handleReviewTimeout();
console.log(JSON.stringify({ retired, retiring: context.reviewRetiring,
  running: context.reviewProcess.running }));
""")
        self.assertTrue(value["retired"])
        self.assertTrue(value["retiring"])
        self.assertFalse(value["running"])
        idle = run_review(
            "context.reviewBusy = false;",
            "console.log(JSON.stringify({ retired: context.handleReviewTimeout() }));")
        self.assertFalse(idle["retired"])

    def test_kill_escalates_only_while_retiring(self):
        live = run_review(
            ("context.reviewRetiring = true; context.reviewProcess.running = true;"),
            "console.log(JSON.stringify({ killed: context.fireReviewKillTimeout(), "
            "signaled: context.reviewProcess.signaled }));")
        self.assertTrue(live["killed"])
        self.assertEqual(live["signaled"], [9])
        idle = run_review(
            ("context.reviewRetiring = false; context.reviewProcess.running = false;"),
            "console.log(JSON.stringify({ killed: context.fireReviewKillTimeout() }));")
        self.assertFalse(idle["killed"])


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class BoundMarkdownTests(unittest.TestCase):
    REVIEW_MD = ("# Evening review \u2014 2026-09-22\n\n## Projects\n"
                 "- other \u2014 16 session(s), 253 min\n\n## Top 3\n1. Task one\n")

    def bound(self, value_json, limit="2000"):
        return run_card(
            "",
            f"""
const out = context.boundMarkdown({value_json}, {limit});
console.log(JSON.stringify({{ out }}));
""")

    def test_multiline_markdown_round_trips_unchanged(self):
        # Direct regression test for the flattening bug: the old
        # boundLine(entry.markdown, 800) collapsed every newline into a
        # space, turning the whole document into one giant H1.
        value = self.bound(json.dumps(self.REVIEW_MD))
        self.assertEqual(value["out"], self.REVIEW_MD.strip())
        self.assertIn("\n", value["out"])
        self.assertEqual(value["out"].count("\n"),
                         self.REVIEW_MD.strip().count("\n"))
        self.assertTrue(value["out"].startswith("# Evening review"))

    def test_crlf_and_cr_normalized_to_lf(self):
        value = self.bound(json.dumps("a\r\nb\rc\nd"))
        self.assertEqual(value["out"], "a\nb\nc\nd")

    def test_control_chars_stripped_newline_survives(self):
        value = self.bound(json.dumps("a\x00b\x07c\x7fd\ne"))
        self.assertEqual(value["out"], "abcd\ne")

    def test_tab_becomes_a_space(self):
        value = self.bound(json.dumps("a\tb"))
        self.assertEqual(value["out"], "a b")

    def test_truncation_cuts_at_a_line_boundary(self):
        lines = ["line %d with some text here" % i for i in range(20)]
        text = "\n".join(lines)
        limit = 100
        value = self.bound(json.dumps(text), str(limit))
        out = value["out"]
        self.assertLessEqual(len(out), limit)
        self.assertTrue(out.endswith("\u2026"))
        body = out[:-1].rstrip("\n")
        for line in body.split("\n"):
            self.assertIn(line, lines)

    def test_truncation_without_newlines_falls_back_to_hard_cut(self):
        value = self.bound(json.dumps("x" * 3000), "2000")
        self.assertLessEqual(len(value["out"]), 2000)
        self.assertTrue(value["out"].endswith("\u2026"))

    def test_long_first_line_without_newline_falls_back_to_hard_cut(self):
        markdown = "# " + "h" * 3000 + "\nbody line"
        value = self.bound(json.dumps(markdown), "2000")
        out = value["out"]
        self.assertEqual(out, markdown[:1999] + "\u2026")
        self.assertLessEqual(len(out), 2000)
        self.assertTrue(out.endswith("\u2026"))
        self.assertNotIn("body line", out)
        self.assertTrue(out.startswith("# hh"))

    def test_default_limit_applies_without_a_valid_limit(self):
        value = self.bound(json.dumps("y" * 2500), "0")
        self.assertLessEqual(len(value["out"]), 2000)
        self.assertEqual(value["out"], "y" * 1999 + "\u2026")
        self.assertEqual(len(value["out"]), 2000)

    def test_empty_null_undefined_yield_empty(self):
        value = run_card(
            "",
            """
const outs = [context.boundMarkdown("", 2000),
              context.boundMarkdown(null, 2000),
              context.boundMarkdown(undefined, 2000)];
console.log(JSON.stringify({ outs }));
""")
        self.assertEqual(value["outs"], ["", "", ""])


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class CardMarkdownTextTests(unittest.TestCase):
    def test_markdown_text_delegates_to_bound_markdown(self):
        markdown = ("# Evening review \u2014 2026-09-22\n\n## Projects\n"
                    "- other \u2014 16 session(s), 253 min\n")
        value = run_card(
            f"context.review = function() {{ return {{markdown: {json.dumps(markdown)}}}; }};",
            """
const out = context.markdownText();
console.log(JSON.stringify({ out }));
""",
            functions=("boundMarkdown", "markdownText"))
        self.assertEqual(value["out"], markdown.strip())
        self.assertIn("\n", value["out"])

    def test_markdown_text_empty_without_a_review(self):
        value = run_card(
            "context.review = function() { return null; };",
            "console.log(JSON.stringify({ out: context.markdownText() }));",
            functions=("boundMarkdown", "markdownText"))
        self.assertEqual(value["out"], "")
        missing = run_card(
            "context.review = function() { return {}; };",
            "console.log(JSON.stringify({ out: context.markdownText() }));",
            functions=("boundMarkdown", "markdownText"))
        self.assertEqual(missing["out"], "")


class ReviewCardStructureTests(unittest.TestCase):
    def test_card_binds_agenda_and_hides_when_null(self):
        self.assertIn("property var agenda", CARD)
        self.assertIn("visible: !!root.agenda", CARD)
        self.assertIn("Review", CARD)
        self.assertIn("reviewPayload", CARD)
        self.assertIn("reloadReview()", CARD)
        self.assertIn("onVisibleChanged", CARD)
        self.assertIn("onAgendaChanged", CARD)
        self.assertIn("Component.onCompleted", CARD)

    def test_completed_hook_gated_on_visible(self):
        start = CARD.index("Component.onCompleted")
        opening = CARD.index("{", start)
        depth = 0
        for index in range(opening, len(CARD)):
            if CARD[index] == "{":
                depth += 1
            elif CARD[index] == "}":
                depth -= 1
                if depth == 0:
                    block = CARD[start:index + 1]
                    break
        else:
            self.fail("unterminated Component.onCompleted")
        self.assertIn("reloadReview", block)
        self.assertIn("visible", block)

    def test_tabs_switch_kind_with_active_highlight(self):
        self.assertIn('objectName: "reviewTabEvening"', CARD)
        self.assertIn('objectName: "reviewTabMorning"', CARD)
        self.assertIn("Evening", CARD)
        self.assertIn("Morning", CARD)
        self.assertIn('setReviewKind("evening")', CARD)
        self.assertIn('setReviewKind("morning")', CARD)
        self.assertIn("reviewKind", CARD)
        self.assertIn('objectName: "reviewRefreshButton"', CARD)
        self.assertIn("refreshReview()", CARD)
        self.assertIn("reviewBusy", CARD)

    def test_sections_render_as_bounded_lines_not_json(self):
        self.assertIn("sectionLines", CARD)
        self.assertIn("sessions", CARD)
        self.assertIn("commits", CARD)
        self.assertIn("accepted", CARD)
        self.assertIn("scheduled", CARD)
        self.assertIn("Journal", CARD)
        self.assertIn("markdownText", CARD)
        # §6.1: the generated review body renders through the shared
        # MarkdownBody (bounded there); the card keeps no raw-markdown
        # PlainText body of its own.
        self.assertIn("MarkdownBody {", CARD)
        self.assertNotIn("Text.PlainText", CARD[
            CARD.index("MarkdownBody {"):CARD.index("MarkdownBody {") + 400])
        self.assertIn("elide", CARD)
        self.assertNotIn("JSON.stringify", CARD)

    def test_markdown_uses_bound_markdown_not_bound_line(self):
        # Regression guard for the flattening bug: boundLine collapses
        # newlines, so the review document must never pass through it.
        self.assertIn("boundMarkdown", CARD)
        self.assertIn("root.boundMarkdown(entry.markdown", CARD)
        self.assertNotIn("boundLine(entry.markdown", CARD)
        body_at = CARD.index("MarkdownBody {")
        body_block = CARD[body_at:body_at + 400]
        self.assertIn("root.markdownText()", body_block)

    def test_top_rows_schedule_through_agenda(self):
        self.assertIn('objectName: "reviewTopList"', CARD)
        self.assertIn("slice(0, 3)", CARD)
        self.assertIn('objectName: "reviewAddButton"', CARD)
        self.assertIn("scheduleReviewItem(modelData)", CARD)
        self.assertIn("Add to tomorrow", CARD)
        self.assertIn("Add to today", CARD)
        self.assertIn("target_date", CARD)
        self.assertIn("modelData.task", CARD)
        self.assertIn("index", CARD)
        self.assertIn("agendaBusy", CARD)

    def test_save_hidden_when_saved(self):
        self.assertIn('objectName: "reviewSaveButton"', CARD)
        self.assertIn("prepareReviewSave()", CARD)
        self.assertIn("saved_ms", CARD)
        self.assertIn("isSaved", CARD)
        self.assertNotIn('objectName: "reviewPolishButton"', CARD)
        self.assertNotIn("polishReview()", CARD)
        self.assertIn("Saved", CARD)

    def test_filing_verbs_and_status_use_the_glossary(self):
        # One filing pair: Save to journal (review files to the journal).
        self.assertIn('"Save to journal"', CARD)
        self.assertNotIn("File journal", CARD)
        self.assertNotIn("File page", CARD)
        # Retired generic notice is gone; the saved state names its
        # destination.
        self.assertIn('"Saved to journal."', CARD)
        self.assertNotIn('"Saved ✓"', CARD)

    def test_card_header_uses_text_title(self):
        header_at = CARD.index('"Review"')
        header_block = CARD[max(0, header_at - 200):header_at + 200]
        self.assertIn("Theme.text", header_block)

    def test_exact_preview_panel_uses_shared_component(self):
        self.assertIn("reviewPreview", CARD)
        self.assertIn("reviewPreviewToken", CARD)
        self.assertIn("reviewApplying", CARD)
        self.assertIn("reviewApplyError", CARD)
        self.assertIn("PreviewPanel {", CARD)
        panel_at = CARD.index("PreviewPanel {")
        panel_block = CARD[panel_at:panel_at + 2500]
        self.assertIn('confirmObjectName: "reviewConfirm"', panel_block)
        self.assertIn('cancelObjectName: "reviewCancel"', panel_block)
        self.assertIn("reviewPreviewToken", panel_block)
        self.assertIn("onConfirmRequested", panel_block)
        self.assertIn("applyReviewSave()", panel_block)
        self.assertIn("onCancelRequested", panel_block)
        self.assertIn("cancelReviewPreview()", panel_block)
        self.assertIn("reviewPreview.addition", CARD)
        self.assertIn("Journal", CARD)
        self.assertIn("preview.date", CARD)
        self.assertIn("preview.path", CARD)
        self.assertIn("preview.revision", CARD)
        # Shared panel owns the bounded readonly surface.
        self.assertIn("readOnly: true", PANEL)
        self.assertIn("monospace", PANEL)

    def test_not_due_text_without_settings_parsing(self):
        self.assertIn("Not due yet", CARD)
        self.assertIn("18:00", CARD)
        self.assertIn("07:00", CARD)
        self.assertNotIn("settings", CARD.lower())
        self.assertIn("reviewError", CARD)
        self.assertIn("reviewNotice", CARD)

    def test_no_backend_or_modal_strings(self):
        lowered = CARD.lower()
        self.assertNotIn("scopedagent", lowered)
        self.assertNotIn("annotations.py", lowered)
        self.assertNotIn("session_capture.py", lowered)
        self.assertNotIn("daily_review.py", lowered)
        self.assertNotIn("memory_tick.py", lowered)
        self.assertNotIn("shellpath", lowered)
        self.assertNotIn("process {", lowered)
        self.assertNotIn("dialog", lowered)
        self.assertNotIn("execute", lowered)

class PlannerEmbedTests(unittest.TestCase):
    def test_planner_embeds_review_card_after_captures(self):
        self.assertIn("ReviewCard {", PLANNER_UI)
        self.assertIn("agenda: root.agenda", PLANNER_UI)
        self.assertLess(PLANNER_UI.index("CaptureInbox {"),
                        PLANNER_UI.index("ReviewCard {"))
