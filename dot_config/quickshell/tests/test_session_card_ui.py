"""Session card UI: DailyAgenda session ladders + SessionCard (Phase 2b §4.5).

Covers the §4.5 surface only, without a live Quickshell harness:
DailyAgenda thought/organise/dismiss/unmapped Process/op ladders
(argv builders, generation + selected-date staleness guards, bounded
single-line errors, chaining), the session helpers the card binds to
(sort, captures, projects, drafts), plus the widgets/SessionCard.qml
and widgets/UnmappedFoldersCard.qml presentation cards (objectNames,
signals, exact previews, Confirm gating) and the planner Open-session
handoff receiver.

Six actions total: Accept TODO, Dismiss TODO, Save thought, Organise,
Dismiss session, Open project. Expanding a row never triggers a write
or a model call. The card lists every session for the selected day —
pending first, then time descending; `attended` drives the row marker
only, never visibility.

Style mirrors tests/test_review_card_ui.py: functions are extracted
from widgets/DailyAgenda.qml / widgets/SessionCard.qml and driven
under node ``vm`` with stubbed processes/timers, plus source
assertions for the process/timer wiring and object contracts.
"""
import sys as _sys
_sys.dont_write_bytecode = True

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
AGENDA = (ROOT / "widgets" / "DailyAgenda.qml").read_text(encoding="utf-8")
CARD = (ROOT / "widgets" / "SessionCard.qml").read_text(encoding="utf-8")
UNMAPPED = (ROOT / "widgets" / "UnmappedFoldersCard.qml").read_text(encoding="utf-8")
PLANNER = (ROOT / "widgets" / "ProjectPlanner.qml").read_text(encoding="utf-8")
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
    tmpdir = "/tmp/opencode"
    if not Path(tmpdir).is_dir():
        tmpdir = None
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, dir=tmpdir,
        encoding="utf-8")
    try:
        handle.write(script)
        handle.close()
        completed = subprocess.run(
            ["node", handle.name], text=True, capture_output=True,
        )
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


SID_A = "a" * 32
SID_B = "b" * 32
PID_A = "11111111-1111-4111-8111-111111111111"


def agenda_harness(functions, setup="", exercise="", extra=""):
    sources = json.dumps([extract_function(AGENDA, name) for name in functions])
    return f"""
const vm = require("vm");
const launches = [];
const context = {{
  selectedDate: "2026-09-13",
  ledgerEntries: [],
  ledgerBusy: false, ledgerSelectedId: "",
  ledgerError: "", ledgerNotice: "",
  captureItems: [],
  thoughtBusy: false, thoughtStarted: false, thoughtStartFailed: false,
  thoughtRetiring: false, thoughtGeneration: 0, thoughtLaunchGeneration: -1,
  thoughtOp: "", thoughtLaunchOp: "", thoughtLaunchDate: "",
  thoughtLaunchSession: "", thoughtPayload: null,
  thoughtError: "", thoughtNotice: "",
  thoughtJournalRevision: "", thoughtSessionId: "", thoughtText: "",
  thoughtPreview: null, thoughtApplyRevision: "", thoughtApplyAddition: "",
  thoughtApplying: false, thoughtApplyError: "",
  organiseBusy: false, organiseStarted: false, organiseStartFailed: false,
  organiseRetiring: false, organiseGeneration: 0, organiseLaunchGeneration: -1,
  organiseOp: "", organiseLaunchOp: "", organiseLaunchDate: "",
  organiseLaunchSession: "", organiseText: "",
  organisePreview: null, organiseToken: "",
  organiseApplying: false, organiseApplyError: "", organiseError: "",
  dismissBusy: false, dismissStarted: false, dismissStartFailed: false,
  dismissRetiring: false, dismissGeneration: 0, dismissLaunchGeneration: -1,
  dismissLaunchDate: "", dismissSessionId: "", dismissPayload: null,
  dismissError: "",
  unmappedRows: [], unmappedBusy: false, unmappedStarted: false,
  unmappedStartFailed: false, unmappedRetiring: false,
  unmappedGeneration: 0, unmappedLaunchGeneration: -1,
  unmappedLaunchDate: "", unmappedError: "", unmappedNotice: "",
  unmappedIgnored: [],
  reloadLedgerCalls: 0,
  reloadLedger() {{ this.reloadLedgerCalls++; return true; }},
  thoughtWatchdog: {{ stop() {{ this.stops = (this.stops || 0) + 1; }},
    restart() {{ this.restarts = (this.restarts || 0) + 1; }} }},
  thoughtKillTimer: {{ restart() {{}} }},
  thoughtProcess: {{ running: false, command: null, stdinEnabled: false }},
  organiseWatchdog: {{ interval: 12000, stops: 0, restarts: 0,
    stop() {{ this.stops++; }}, restart() {{ this.restarts++; }} }},
  organiseKillTimer: {{ restart() {{}} }},
  organiseProcess: {{ running: false, command: null }},
  dismissWatchdog: {{ stop() {{}}, restart() {{}} }},
  dismissKillTimer: {{ restart() {{}} }},
  dismissProcess: {{ running: false, command: null, stdinEnabled: false }},
  unmappedWatchdog: {{ stop() {{}}, restart() {{}} }},
  unmappedKillTimer: {{ restart() {{}} }},
  unmappedProcess: {{ running: false, command: null }},
  Quickshell: {{ shellPath(value) {{ return "/repo/" + value; }}, env(v) {{ return ""; }} }},
  {extra}
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
const root = context;
{exercise}
"""


def card_harness(functions, agenda_setup="", exercise=""):
    sources = json.dumps([extract_function(CARD, name) for name in functions])
    return f"""
const vm = require("vm");
const calls = [];
const agenda = {{
  ledgerEntries: [],
  ledgerSelectedId: "",
  captureItems: [],
  reloadLedger() {{ calls.push("reloadLedger"); }},
  reloadCaptures() {{ calls.push("reloadCaptures"); }},
  selectLedgerEntry(sid) {{ calls.push(["selectLedgerEntry", sid]); return true; }},
  prepareCapture(id, sid, ref) {{ calls.push(["prepareCapture", id]); return true; }},
  dismissCapture(id) {{ calls.push(["dismissCapture", id]); return true; }},
  saveThought(sid, text) {{ calls.push(["saveThought", sid]); return true; }},
  prepareOrganise(sid, text) {{ calls.push(["prepareOrganise", sid]); return true; }},
  dismissSession(sid) {{ calls.push(["dismissSession", sid]); return true; }},
  sessionCapturesFor(sid) {{ return []; }},
  sessionTodoCount(sid) {{ return 0; }},
  sessionProjects() {{ return []; }},
  {agenda_setup}
}};
const root = {{
  agenda: agenda,
  thoughtDrafts: {{}},
  attributionOverrides: {{}},
  activeEditor: null,
  activeSession: "",
}};
const context = {{root, agenda, calls}};
vm.createContext(context);
for (const value of {sources}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
{exercise}
"""


CARD_FUNCTIONS_PURE = [
    "boundLine", "entrySession", "sessionId", "entryMeta",
    "isAttended", "isPending", "rowTime", "startMs",
    "effectiveProject", "rowProjectName", "projectIdOf",
    "sortedEntries", "dayLabel",
    "draftFor", "setDraft", "clearDraft", "applyOrganisedText",
    "setAttribution", "optionIndexFor",
    "isSelected", "toggleEntry", "statusText", "statusIsError",
    "thoughtPreviewFor", "organisePreviewFor", "capturePreviewFor",
]

THOUGHT_FUNCTIONS = [
    "thoughtBound", "thoughtFailure", "isLedgerSessionId",
    "thoughtContextCommand", "thoughtPrepareCommand", "thoughtApplyCommand",
    "launchThought", "saveThought",
    "finishThoughtContext", "finishThoughtPrepare",
    "applyThought", "finishThoughtApply", "cancelThoughtPreview",
    "todayIso", "isoDate", "pad2",
]

ORGANISE_FUNCTIONS = [
    "organiseBound", "organiseFailure", "isLedgerSessionId",
    "organisePrepareCommand", "organiseApplyCommand",
    "launchOrganise", "prepareOrganise",
    "finishOrganisePrepare", "applyOrganise", "finishOrganiseApply",
    "cancelOrganisePreview",
]

DISMISS_FUNCTIONS = [
    "ledgerBound", "ledgerFailure", "isLedgerSessionId", "ledgerFindEntry",
    "dismissSessionCommand", "launchDismiss",
    "dismissSession", "finishDismissSession",
]

UNMAPPED_FUNCTIONS = [
    "unmappedBound", "unmappedListCommand", "launchUnmapped",
    "reloadUnmapped", "unmappedSanitizeRows", "finishUnmapped",
    "ignoreUnmapped", "unmappedVisibleRows",
]


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class SessionCardStructureTests(unittest.TestCase):
    def test_object_names_and_signals(self):
        for token in ("sessionRefreshButton", "sessionStatus", "sessionEmpty",
                      "sessionList",
                      "sessionAcceptTodo", "sessionDismissTodo",
                      "sessionSaveThought", "sessionOrganise",
                      "sessionDismiss", "sessionOpenProject",
                      "sessionProjectPicker", "sessionThoughtEditor",
                      "sessionCapturePreview", "sessionCaptureConfirm",
                      "sessionCaptureCancel",
                      "sessionOrganisePreview", "sessionOrganiseConfirm",
                      "sessionOrganiseCancel",
                      "sessionThoughtPreview", "sessionThoughtConfirm",
                      "sessionThoughtCancel"):
            self.assertIn('"' + token + '"', CARD)
        self.assertIn("signal openProjectRequested(string projectId)", CARD)
        # Saved-organised round trips arrive on the shared agenda
        # signals; the card only mirrors drafts, never writes.
        self.assertIn("onThoughtSaved(sessionId)", CARD)
        self.assertIn("onOrganiseDone(sessionId, text)", CARD)
        self.assertIn("signal thoughtSaved(string sessionId)", AGENDA)
        self.assertIn("signal organiseDone(string sessionId, string text)", AGENDA)

    def test_deleted_surface_absent(self):
        for token in ("next_step", "filed_uri", "logseq://",
                      "Open in Logseq", "Merge with", "Keep as unit",
                      "Split unit", "week", "polish", "Polish",
                      "histResumeButton", "sessionOpenLogseqButton",
                      "LedgerCard", "drainLedgerRefresh",
                      "ledgerWeekPending", "focusTitleInput"):
            self.assertNotIn(token, CARD)

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
        self.assertIn("reloadLedger", block)
        self.assertIn("reloadCaptures", block)
        self.assertIn("visible", block)

    def test_preview_panels_and_header_rule(self):
        self.assertEqual(CARD.count("PreviewPanel {"), 3)
        self.assertIn('"Sessions · " + root.dayLabel()', CARD)
        self.assertIn('"Loading…" : "Refresh"', CARD)
        self.assertIn("Layout.preferredHeight: 280", CARD)
        # Generated markdown renders as markdown from the start; the
        # card carries no raw-markdown Text.PlainText bodies for logs.
        # The expanded editor is one shape for every session, including
        # ones with no captures and no thought.
        self.assertIn('"No captured TODOs for this session."', CARD)
        self.assertIn('"Captured from your agent session"', CARD)

    def test_planner_embeds_session_card(self):
        planner = (ROOT / "widgets" / "DailyPlanner.qml").read_text(encoding="utf-8")
        self.assertIn("SessionCard {", planner)
        self.assertIn("onOpenProjectRequested", planner)
        self.assertNotIn("LedgerCard", planner)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class SessionCardHelperTests(unittest.TestCase):
    def test_row_time_project_and_attended(self):
        script = card_harness(
            ["boundLine", "entrySession", "sessionId", "entryMeta",
             "isAttended", "isPending", "rowTime", "startMs",
             "effectiveProject", "rowProjectName", "projectIdOf"],
            exercise="""
const entry = {session: {session_id: "SESSION", start_ms: 1700000000000,
  end_ms: 1700003600000, project: {id: "PID", name: "  Demo  "}},
  meta: {}, pending: true, attended: 0};
const unmapped = {session: {session_id: "OTHER", project: null}, pending: false};
const badProject = {session: {session_id: "OTHER2",
  project: {id: "not-a-uuid", name: "Nope"}}, pending: false};
console.log(JSON.stringify({
  time: root.rowTime(entry),
  startOnly: root.rowTime({session: {start_ms: 1000}}),
  noTime: root.rowTime({session: {}}),
  name: root.rowProjectName(entry),
  unmappedName: root.rowProjectName(unmapped),
  pid: root.projectIdOf(entry),
  badPid: root.projectIdOf(badProject),
  pending: root.isPending(entry),
  attended: root.isAttended({attended: 1}),
  attendedFalse: root.isAttended(entry),
  bounded: root.boundLine("a  b\\n c", 4)
}));""".replace("SESSION", SID_A).replace("OTHER2", SID_B)
            .replace("OTHER", "c" * 32).replace("PID", PID_A))
        value = run_node(script)
        self.assertRegex(value["time"], r"^\d\d:\d\d–\d\d:\d\d$")
        self.assertRegex(value["startOnly"], r"^\d\d:\d\d$")
        self.assertEqual(value["noTime"], "")
        self.assertEqual(value["name"], "Demo")
        self.assertEqual(value["unmappedName"], "(unmapped)")
        self.assertEqual(value["pid"], PID_A)
        self.assertEqual(value["badPid"], "")
        self.assertTrue(value["pending"])
        self.assertTrue(value["attended"])
        self.assertFalse(value["attendedFalse"])
        self.assertEqual(value["bounded"], "a b…")

    def test_sorted_entries_pending_first_time_descending(self):
        script = card_harness(
            ["entrySession", "isPending", "startMs", "sortedEntries"],
            agenda_setup="ledgerEntries: [],",
            exercise="""
root.agenda.ledgerEntries = [
  {session: {session_id: "A", start_ms: 1000}, pending: false, attended: true},
  {session: {session_id: "B", start_ms: 3000}, pending: true, attended: false},
  {session: {session_id: "C", start_ms: 2000}, pending: false, attended: false},
  {session: {session_id: "D", start_ms: 4000}, pending: true, attended: false}
];
const order = root.sortedEntries().map(e => e.session.session_id);
console.log(JSON.stringify({order}));""".replace('"A"', '"%s"' % SID_A)
            .replace('"B"', '"%s"' % SID_B)
            .replace('"C"', '"%s"' % ("c" * 32)).replace('"D"', '"%s"' % ("d" * 32)))
        value = run_node(script)
        # Pending first (D newer than B), then time descending (C, A).
        # `attended` never hides: all four rows stay visible.
        self.assertEqual(value["order"], ["d" * 32, SID_B, "c" * 32, SID_A])

    def test_thought_drafts_and_attribution_are_ephemeral(self):
        script = card_harness(
            ["draftFor", "setDraft", "clearDraft", "applyOrganisedText",
             "setAttribution", "projectOptions", "optionIndexFor",
             "effectiveProject", "sessionId", "entrySession",
             "projectIdOf"],
            agenda_setup="sessionProjects() { return [{id: 'PID', name: 'Demo'}]; },"
            .replace("PID", PID_A),
            exercise="""
root.setDraft("SID", "rough thought");
const before = root.draftFor("SID");
root.clearDraft("SID");
const after = root.draftFor("SID");
root.setDraft("SID2", "organised text");
root.applyOrganisedText("SID2", "replaced");
const entry = {session: {session_id: "SID3",
  project: {id: "not-a-uuid", name: "Wrong"}}};
const beforePick = root.projectIdOf(entry);
root.setAttribution("SID3", "PID");
const afterPick = root.projectIdOf(entry);
root.setAttribution("SID3", "no-such-project");
const badPick = root.projectIdOf(entry);
console.log(JSON.stringify({before, after,
  replaced: root.draftFor("SID2"),
  beforePick, afterPick, badPick}));"""
            .replace("SID3", SID_B).replace("SID2", "c" * 32)
            .replace("SID", SID_A).replace("PID", PID_A))
        value = run_node(script)
        self.assertEqual(value["before"], "rough thought")
        self.assertEqual(value["after"], "")
        self.assertEqual(value["replaced"], "replaced")
        # Explicit attribution pick wins for display + Open project;
        # unknown ids never stick.
        self.assertEqual(value["beforePick"], "")
        self.assertEqual(value["afterPick"], PID_A)
        self.assertEqual(value["badPick"], PID_A)

    def test_toggle_entry_expands_without_writing(self):
        script = card_harness(
            ["entrySession", "sessionId", "isSelected", "toggleEntry"],
            agenda_setup="""
  ledgerSelectedId: "",
  selectLedgerEntry(sid) { calls.push(["selectLedgerEntry", sid]);
    this.ledgerSelectedId = sid; return true; },""",
            exercise="""
const entry = {session: {session_id: "SID"}};
root.toggleEntry(entry);
root.toggleEntry(null);
root.toggleEntry({session: {}});
const writes = calls.filter(c =>
  Array.isArray(c) && /^(prepareCapture|dismissCapture|saveThought|prepareOrganise|dismissSession)$/
    .test(c[0]));
console.log(JSON.stringify({calls, writes}));""".replace("SID", SID_A))
        value = run_node(script)
        # Expanding selects (read-only) and ensures captures are loaded;
        # no prepare/apply/dismiss/organise is ever staged.
        self.assertIn(["selectLedgerEntry", SID_A], value["calls"])
        self.assertIn("reloadCaptures", value["calls"])
        self.assertEqual(value["writes"], [])

    def test_day_label_and_status(self):
        script = card_harness(
            ["dayLabel", "statusText", "statusIsError"],
            agenda_setup="""
  selectedDate: "2026-09-21",
  ledgerError: "", captureError: "boom", thoughtError: "",
  organiseError: "", captureNotice: "", thoughtNotice: "",
  ledgerNotice: "hi",""",
            exercise="""
const label = root.dayLabel();
const text = root.statusText();
const isError = root.statusIsError();
root.agenda.captureError = "";
const idle = root.statusText();
console.log(JSON.stringify({label, text, isError, idle}));""")
        value = run_node(script)
        self.assertEqual(value["label"], "Mon 21 Sep")
        self.assertEqual(value["text"], "boom")
        self.assertTrue(value["isError"])
        self.assertEqual(value["idle"], "hi")

    def test_thought_and_organise_preview_scoping(self):
        script = card_harness(
            ["thoughtPreviewFor", "organisePreviewFor", "capturePreviewFor"],
            agenda_setup="""
  thoughtPreview: {addition: "t"},
  thoughtSessionId: "SID",
  organisePreview: {text: "o"},
  organiseSession: "SID",
  capturePreview: {block: "c"},
  capturePreviewSession: "OTHER",""".replace("SID", SID_A).replace("OTHER", SID_B),
            exercise="""
console.log(JSON.stringify({
  thought: root.thoughtPreviewFor("SID"),
  thoughtOther: root.thoughtPreviewFor("OTHER"),
  organise: root.organisePreviewFor("SID"),
  capture: root.capturePreviewFor("SID"),
  captureOther: root.capturePreviewFor("OTHER")
}));""".replace("SID", SID_A).replace("OTHER", SID_B))
        value = run_node(script)
        self.assertEqual(value["thought"], {"addition": "t"})
        self.assertIsNone(value["thoughtOther"])
        self.assertEqual(value["organise"], {"text": "o"})
        self.assertIsNone(value["capture"])
        self.assertEqual(value["captureOther"], {"block": "c"})


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class ThoughtLadderTests(unittest.TestCase):
    def test_argv_builders_are_list_form(self):
        script = agenda_harness(
            ["thoughtContextCommand", "thoughtPrepareCommand", "thoughtApplyCommand"],
            exercise="""
console.log(JSON.stringify({
  context: root.thoughtContextCommand(),
  prepare: root.thoughtPrepareCommand(),
  apply: root.thoughtApplyCommand()
}));""",
            extra="graphArg() { return '/g'; },")
        value = run_node(script)
        for key in ("context", "prepare", "apply"):
            self.assertEqual(value[key][:2], ["python3", "/repo/scripts/journal_assistant.py"])
        self.assertEqual(value["context"][-1], "context")
        self.assertEqual(value["prepare"][-1], "prepare")
        self.assertEqual(value["apply"][-1], "append")

    def test_save_thought_validates_then_stages_context(self):
        script = agenda_harness(
            THOUGHT_FUNCTIONS,
            exercise="""
const badId = root.saveThought("nope", "hello");
const badIdError = root.thoughtError;
const empty = root.saveThought("SID", "   ");
const emptyError = root.thoughtError;
const tooLong = root.saveThought("SID", "x".repeat(4097));
const longError = root.thoughtError;
root.launchThought = function(op, date, argv, payload) {
  launches.push({op, date, argv, payload}); return true; };
const ok = root.saveThought("SID", "rough thought");
console.log(JSON.stringify({badId, badIdError, empty, emptyError,
  tooLong, longError, ok, launches,
  session: root.thoughtSessionId, text: root.thoughtText}));"""
            .replace("SID", SID_A),
            extra="graphArg() { return '/g'; },")
        value = run_node(script)
        self.assertFalse(value["badId"])
        self.assertIn("valid session", value["badIdError"])
        self.assertFalse(value["empty"])
        self.assertIn("Write a thought", value["emptyError"])
        self.assertFalse(value["tooLong"])
        self.assertIn("too long", value["longError"])
        self.assertTrue(value["ok"])
        self.assertEqual(len(value["launches"]), 1)
        self.assertEqual(value["launches"][0]["op"], "context")
        self.assertEqual(value["launches"][0]["date"], "2026-09-13")
        self.assertEqual(value["launches"][0]["payload"], {})
        self.assertEqual(value["session"], SID_A)
        self.assertEqual(value["text"], "rough thought")

    def test_context_chains_prepare_with_today_only(self):
        script = agenda_harness(
            THOUGHT_FUNCTIONS,
            exercise="""
root.launchThought = function(op, date, argv, payload) {
  launches.push({op, date, argv, payload}); return true; };
root.thoughtLaunchGeneration = 1;
root.thoughtLaunchDate = "2026-09-13";
root.thoughtLaunchOp = "context";
root.thoughtText = "rough thought";
root.thoughtSessionId = "SID";
// The card may show another day; the journal destination is always
// today (todayIso), even though the launch guards on selectedDate.
const chained = root.finishThoughtContext(0,
  JSON.stringify({revision: "rev-1"}), 1, "2026-09-13", "context", "");
const today = root.todayIso();
console.log(JSON.stringify({chained, launches, today,
  journalRevision: root.thoughtJournalRevision}));"""
            .replace("SID", SID_A),
            extra="graphArg() { return '/g'; },")
        value = run_node(script)
        self.assertTrue(value["chained"])
        self.assertRegex(value["today"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(len(value["launches"]), 1)
        chained = value["launches"][0]
        self.assertEqual(chained["op"], "prepare")
        self.assertEqual(chained["argv"][-1], "prepare")
        self.assertEqual(chained["payload"]["date"], value["today"])
        self.assertEqual(chained["payload"]["revision"], "rev-1")
        self.assertEqual(chained["payload"]["text"], "rough thought")
        self.assertEqual(chained["payload"]["session_id"], SID_A)
        self.assertEqual(value["journalRevision"], "rev-1")

    def test_context_guards_and_prepare_installs_preview(self):
        script = agenda_harness(
            THOUGHT_FUNCTIONS,
            exercise="""
root.launchThought = function(op, date, argv, payload) {
  launches.push({op}); return true; };
root.thoughtLaunchGeneration = 1;
root.thoughtLaunchDate = "2026-09-13";
root.thoughtLaunchOp = "context";
// Stale generation, wrong date, wrong op: all dropped.
const stale = root.finishThoughtContext(0,
  JSON.stringify({revision: "r"}), 0, "2026-09-13", "context", "");
const wrongDate = root.finishThoughtContext(0,
  JSON.stringify({revision: "r"}), 1, "2026-09-14", "context", "");
const wrongOp = root.finishThoughtContext(0,
  JSON.stringify({revision: "r"}), 1, "2026-09-13", "prepare", "");
const noRevision = root.finishThoughtContext(0, JSON.stringify({}), 1,
  "2026-09-13", "context", "");
const noRevisionError = root.thoughtError;
root.thoughtLaunchGeneration = 2;
root.thoughtLaunchDate = "2026-09-13";
root.thoughtLaunchOp = "prepare";
const preview = root.finishThoughtPrepare(0, JSON.stringify(
  {addition: "- TODO x", path: "/g/2026_09_13.md", revision: "rev-2"}),
  2, "2026-09-13", "prepare", "");
const oversized = root.finishThoughtPrepare(0, JSON.stringify(
  {addition: "x".repeat(70000), path: "/g/d.md", revision: "r"}),
  2, "2026-09-13", "prepare", "");
console.log(JSON.stringify({stale, wrongDate, wrongOp, noRevision,
  noRevisionError, launches, preview,
  staged: root.thoughtPreview, applyRevision: root.thoughtApplyRevision,
  oversized, oversizedError: root.thoughtApplyError}));""",
            extra="graphArg() { return '/g'; },")
        value = run_node(script)
        self.assertFalse(value["stale"])
        self.assertFalse(value["wrongDate"])
        self.assertFalse(value["wrongOp"])
        self.assertFalse(value["noRevision"])
        self.assertTrue(value["noRevisionError"])
        self.assertEqual(value["launches"], [])
        self.assertTrue(value["preview"])
        self.assertEqual(value["staged"]["addition"], "- TODO x")
        self.assertEqual(value["applyRevision"], "rev-2")
        self.assertFalse(value["oversized"])
        self.assertIn("too large", value["oversizedError"])

    def test_apply_thought_guards(self):
        script = agenda_harness(
            THOUGHT_FUNCTIONS,
            exercise="""
root.launchThought = function(op, date, argv, payload) {
  launches.push({op, argv, payload}); return true; };
const noPreview = root.applyThought();
root.thoughtPreview = {addition: "- TODO x", path: "/g/d.md"};
root.thoughtApplyRevision = "rev";
root.thoughtApplyAddition = "- TODO x";
root.thoughtSessionId = "SID";
const applied = root.applyThought();
console.log(JSON.stringify({noPreview, applied, launches}));"""
            .replace("SID", SID_A),
            extra="graphArg() { return '/g'; },")
        value = run_node(script)
        self.assertFalse(value["noPreview"])
        self.assertTrue(value["applied"])
        self.assertEqual(len(value["launches"]), 1)
        self.assertEqual(value["launches"][0]["op"], "apply")
        self.assertEqual(value["launches"][0]["argv"][-1], "append")
        self.assertEqual(value["launches"][0]["payload"]["session_id"], SID_A)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class OrganiseLadderTests(unittest.TestCase):
    def test_prepare_command_bounds_and_hex(self):
        script = agenda_harness(
            ["organisePrepareCommand", "organiseApplyCommand"],
            exercise="""
console.log(JSON.stringify({
  empty: root.organisePrepareCommand("", "SID"),
  blank: root.organisePrepareCommand("   ", ""),
  tooLong: root.organisePrepareCommand("x".repeat(4097), ""),
  atLimit: root.organisePrepareCommand("x".repeat(4096), "").length > 0,
  badSession: root.organisePrepareCommand("hello", "not-hex"),
  shortSession: root.organisePrepareCommand("hello", "abc"),
  valid: root.organisePrepareCommand("hello", "SID"),
  upperSession: root.organisePrepareCommand("hello", "SID".toUpperCase()),
  noSession: root.organisePrepareCommand("hello", ""),
  nullSession: root.organisePrepareCommand("hello", null),
  applyEmpty: root.organiseApplyCommand(""),
  applyToken: root.organiseApplyCommand("orgt_abc")
}));""".replace("SID", SID_A))
        value = run_node(script)
        self.assertEqual(value["empty"], [])
        self.assertEqual(value["blank"], [])
        self.assertEqual(value["tooLong"], [])
        self.assertTrue(value["atLimit"])
        self.assertEqual(value["badSession"], [])
        self.assertEqual(value["shortSession"], [])
        self.assertEqual(value["valid"][:3],
                         ["python3", "/repo/scripts/thought_organise.py", "prepare"])
        self.assertIn("--text", value["valid"])
        self.assertEqual(value["valid"][-2:], ["--session", SID_A])
        self.assertEqual(value["upperSession"][-2:], ["--session", SID_A])
        self.assertNotIn("--session", value["noSession"])
        self.assertNotIn("--session", value["nullSession"])
        self.assertEqual(value["applyEmpty"], [])
        self.assertEqual(value["applyToken"][-2:], ["--prepared", "orgt_abc"])

    def test_prepare_watchdog_is_65s_deviation(self):
        # Every other ladder uses the 12 s watchdog; the one Pi call may
        # take 60 s, so organise prepare gets 65 s. Apply stays 12 s.
        script = agenda_harness(
            ["launchOrganise"],
            exercise="""
const prepareOk = root.launchOrganise("prepare", "2026-09-13", ["python3", "x"]);
const prepareInterval = context.organiseWatchdog.interval;
root.organiseBusy = false;
context.organiseProcess.running = false;
const applyOk = root.launchOrganise("apply", "2026-09-13", ["python3", "x"]);
const applyInterval = context.organiseWatchdog.interval;
console.log(JSON.stringify({prepareOk, prepareInterval, applyOk,
  applyInterval}));""")
        value = run_node(script)
        self.assertTrue(value["prepareOk"])
        self.assertEqual(value["prepareInterval"], 65000)
        self.assertTrue(value["applyOk"])
        self.assertEqual(value["applyInterval"], 12000)

    def test_prepare_organise_validates_and_stages(self):
        script = agenda_harness(
            ORGANISE_FUNCTIONS,
            exercise="""
const badId = root.prepareOrganise("nope", "hello");
const badIdError = root.organiseError;
const empty = root.prepareOrganise("SID", "  ");
const emptyError = root.organiseError;
root.launchOrganise = function(op, date, argv) {
  launches.push({op, date, argv}); this.organiseBusy = true; return true; };
const ok = root.prepareOrganise("SID", "rough thought");
const busy = root.prepareOrganise("SID", "again");
console.log(JSON.stringify({badId, badIdError, empty, emptyError,
  ok, busy, launches, session: root.organiseLaunchSession}));"""
            .replace("SID", SID_A))
        value = run_node(script)
        self.assertFalse(value["badId"])
        self.assertIn("valid session", value["badIdError"])
        self.assertFalse(value["empty"])
        self.assertIn("empty or too long", value["emptyError"])
        self.assertTrue(value["ok"])
        # Serialized: a second prepare while busy is refused.
        self.assertFalse(value["busy"])
        self.assertEqual(len(value["launches"]), 1)
        self.assertEqual(value["launches"][0]["op"], "prepare")
        self.assertEqual(value["session"], SID_A)

    def test_finish_prepare_installs_token_and_preview(self):
        script = agenda_harness(
            ORGANISE_FUNCTIONS,
            exercise="""
root.organiseLaunchGeneration = 1;
root.organiseLaunchDate = "2026-09-13";
root.organiseLaunchOp = "prepare";
const ok = root.finishOrganisePrepare(0, JSON.stringify(
  {prepared: "orgt_abc", preview: "tidy thought"}),
  1, "2026-09-13", "prepare", "");
const emptyPreview = root.finishOrganisePrepare(0, JSON.stringify(
  {prepared: "orgt_x", preview: "  "}),
  1, "2026-09-13", "prepare", "");
const failure = root.finishOrganisePrepare(1, "", 1,
  "2026-09-13", "prepare", "error: overloaded");
const stale = root.finishOrganisePrepare(0, JSON.stringify(
  {prepared: "orgt_y", preview: "z"}),
  0, "2026-09-13", "prepare", "");
console.log(JSON.stringify({ok, preview: root.organisePreview,
  token: root.organiseToken, emptyPreview,
  failure, failureError: root.organiseApplyError, stale}));""")
        value = run_node(script)
        self.assertTrue(value["ok"])
        self.assertEqual(value["preview"], {"text": "tidy thought"})
        self.assertEqual(value["token"], "orgt_abc")
        self.assertFalse(value["emptyPreview"])
        self.assertFalse(value["failure"])
        self.assertEqual(value["failureError"], "overloaded")
        self.assertFalse(value["stale"])


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class DismissLadderTests(unittest.TestCase):
    def test_dismiss_carries_revision_when_meta_exists(self):
        script = agenda_harness(
            DISMISS_FUNCTIONS,
            exercise="""
const noEntry = root.dismissSession("SID");
root.ledgerEntries = [
  {session: {session_id: "SID", project: null}, meta: {revision: "rev-9"}},
  {session: {session_id: "OTHER", project: null}, meta: null}
];
root.launchDismiss = function(date, argv, payload) {
  launches.push({date, argv, payload}); return true; };
const withRevision = root.dismissSession("SID");
const withPayload = launches[launches.length - 1].payload;
launches.length = 0;
const withoutRevision = root.dismissSession("OTHER");
const withoutPayload = launches[launches.length - 1].payload;
const badId = root.dismissSession("nope");
console.log(JSON.stringify({noEntry, withRevision, withPayload,
  withoutRevision, withoutPayload, badId,
  error: root.dismissError, command: root.dismissSessionCommand()}));"""
            .replace("SID", SID_A).replace("OTHER", SID_B))
        value = run_node(script)
        self.assertFalse(value["noEntry"])
        self.assertTrue(value["withRevision"])
        # Revision rechecked by the backend; carried when a meta row
        # exists, omitted only when none exists yet.
        self.assertEqual(value["withPayload"],
                         {"session": SID_A, "attended": 1, "revision": "rev-9"})
        self.assertTrue(value["withoutRevision"])
        self.assertEqual(value["withoutPayload"],
                         {"session": SID_B, "attended": 1})
        self.assertFalse(value["badId"])
        self.assertIn("valid session", value["error"])
        self.assertEqual(value["command"],
                         ["python3", "/repo/scripts/sessions.py", "annotate"])

    def test_finish_dismiss_reloads_and_notices(self):
        script = agenda_harness(
            DISMISS_FUNCTIONS,
            exercise="""
root.dismissLaunchGeneration = 1;
root.dismissLaunchDate = "2026-09-13";
root.dismissSessionId = "SID";
const ok = root.finishDismissSession(0, JSON.stringify({ok: true}),
  1, "2026-09-13", "");
const failure = root.finishDismissSession(1, "", 1,
  "2026-09-13", "error: stale");
console.log(JSON.stringify({ok, notice: root.ledgerNotice,
  reloads: root.reloadLedgerCalls, cleared: root.dismissSessionId,
  failure, failureError: root.dismissError}));"""
            .replace("SID", SID_A))
        value = run_node(script)
        self.assertTrue(value["ok"])
        self.assertIn("dismissed", value["notice"].lower())
        self.assertEqual(value["reloads"], 1)
        self.assertEqual(value["cleared"], "")
        self.assertFalse(value["failure"])
        self.assertEqual(value["failureError"], "stale")


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class UnmappedLadderTests(unittest.TestCase):
    def test_list_command_defaults_and_bounds(self):
        script = agenda_harness(
            ["unmappedListCommand"],
            exercise="""
console.log(JSON.stringify({
  defaults: root.unmappedListCommand(7, 10),
  omitted: root.unmappedListCommand(undefined, undefined),
  nulled: root.unmappedListCommand(null, null),
  scoped: root.unmappedListCommand(30, 5),
  zeroDays: root.unmappedListCommand(0, 10),
  overDays: root.unmappedListCommand(32, 10),
  overLimit: root.unmappedListCommand(7, 11),
  zeroLimit: root.unmappedListCommand(7, 0),
  fractional: root.unmappedListCommand(7.5, 10),
  textDays: root.unmappedListCommand("x", 10)
}));""")
        value = run_node(script)
        # Defaults 7 days / 10 rows; days clamp 1..31, limit 1..10.
        self.assertEqual(value["defaults"],
                         ["python3", "/repo/scripts/desktop_projects.py",
                          "unmapped-folders", "--days", "7", "--limit", "10"])
        self.assertEqual(value["omitted"], value["defaults"])
        self.assertEqual(value["nulled"], value["defaults"])
        self.assertEqual(value["scoped"][-4:], ["--days", "30", "--limit", "5"])
        for key in ("zeroDays", "overDays", "overLimit", "zeroLimit",
                    "fractional", "textDays"):
            self.assertEqual(value[key], [], key)

    def test_sanitize_rows_cap_and_tolerance(self):
        script = agenda_harness(
            ["unmappedSanitizeRows"],
            exercise="""
const rows = [
  {git_root_or_cwd: "/x/a", observation_count: 3, last_seen_ms: 100},
  {path: "/x/b", count: 2},
  {folder: "  /x/c  "},
  null, 42, "nope", [],
  {git_root_or_cwd: "   "},
  {git_root_or_cwd: "/x/d", observation_count: -1, last_seen_ms: -5}
];
const capped = Array.from({length: 15}, (_, i) =>
  ({git_root_or_cwd: "/x/" + i, observation_count: 1}));
console.log(JSON.stringify({
  cleaned: root.unmappedSanitizeRows(rows),
  capped: root.unmappedSanitizeRows(capped).length,
  nonList: root.unmappedSanitizeRows(null)
}));""")
        value = run_node(script)
        cleaned = value["cleaned"]
        self.assertEqual(len(cleaned), 4)
        self.assertEqual(cleaned[0],
                         {"folder": "/x/a", "observation_count": 3,
                          "last_seen_ms": 100})
        # Tolerant alternates (path/folder, count) for forward compat.
        self.assertEqual(cleaned[1]["folder"], "/x/b")
        self.assertEqual(cleaned[1]["observation_count"], 2)
        self.assertEqual(cleaned[2]["folder"], "/x/c")
        self.assertEqual(cleaned[2]["observation_count"], 0)
        # Negative counters floor at zero, never negative.
        self.assertEqual(cleaned[3]["observation_count"], 0)
        self.assertEqual(cleaned[3]["last_seen_ms"], 0)
        self.assertEqual(value["capped"], 10)
        self.assertEqual(value["nonList"], [])

    def test_reload_uses_defaults_and_finish_keeps_rows_on_error(self):
        script = agenda_harness(
            UNMAPPED_FUNCTIONS,
            exercise="""
root.launchUnmapped = function(date, argv) {
  launches.push({date, argv}); return true; };
const reloaded = root.reloadUnmapped();
root.unmappedLaunchGeneration = 1;
root.unmappedLaunchDate = "2026-09-13";
const installed = root.finishUnmapped(0, JSON.stringify({rows: [
  {git_root_or_cwd: "/x/a", observation_count: 1}]}),
  1, "2026-09-13", "");
const before = root.unmappedRows.slice();
const failed = root.finishUnmapped(1, "", 1, "2026-09-13", "");
console.log(JSON.stringify({reloaded, launches, installed,
  rows: root.unmappedRows, kept: root.unmappedRows,
  failed, error: root.unmappedError, before}));""")
        value = run_node(script)
        self.assertTrue(value["reloaded"])
        self.assertEqual(value["launches"][0]["argv"][-4:],
                         ["--days", "7", "--limit", "10"])
        self.assertTrue(value["installed"])
        self.assertEqual(len(value["rows"]), 1)
        # Failures keep the previous rows (inline bounded error, never
        # modal).
        self.assertFalse(value["failed"])
        self.assertEqual(value["kept"], value["before"])
        self.assertTrue(value["error"])

    def test_ignore_is_ephemeral_and_bounded(self):
        script = agenda_harness(
            ["ignoreUnmapped", "unmappedVisibleRows"],
            exercise="""
root.unmappedRows = [{folder: "/x/a"}, {folder: "/x/b"}];
const added = root.ignoreUnmapped("/x/a");
const duplicate = root.ignoreUnmapped("/x/a");
const blank = root.ignoreUnmapped("   ");
const visible = root.unmappedVisibleRows();
for (let i = 0; i < 150; i++) root.ignoreUnmapped("/x/" + i);
console.log(JSON.stringify({added, duplicate, blank, visible,
  ignored: root.unmappedIgnored.length}));""")
        value = run_node(script)
        self.assertTrue(value["added"])
        self.assertTrue(value["duplicate"])
        self.assertFalse(value["blank"])
        self.assertEqual(value["visible"], [{"folder": "/x/b"}])
        self.assertEqual(value["ignored"], 100)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class UnmappedCardStructureTests(unittest.TestCase):
    def test_object_names_signal_and_helpers(self):
        for token in ("unmappedRefreshButton", "unmappedList",
                      "unmappedIgnoreButton", "unmappedAddButton",
                      "unmappedStatus"):
            self.assertIn('"' + token + '"', UNMAPPED)
        self.assertIn("signal addToProjectRequested(string folder)", UNMAPPED)
        self.assertIn('"Unclaimed folders"', UNMAPPED)
        self.assertIn('"You worked in these folders but no project claims them."',
                      UNMAPPED)
        # Ephemeral ignore, never durable content; no inference, no
        # auto-create, no writes from this card.
        self.assertIn("root.agenda.ignoreUnmapped", UNMAPPED)
        self.assertIn("root.addToProjectRequested", UNMAPPED)
        self.assertNotIn("shellPath", UNMAPPED)
        self.assertIn("no writes", UNMAPPED)
        # Hidden when there is nothing to fix.
        self.assertIn("visibleRows().length > 0", UNMAPPED)

    def test_completed_hook_gated_on_visible(self):
        start = UNMAPPED.index("Component.onCompleted")
        opening = UNMAPPED.index("{", start)
        depth = 0
        for index in range(opening, len(UNMAPPED)):
            if UNMAPPED[index] == "{":
                depth += 1
            elif UNMAPPED[index] == "}":
                depth -= 1
                if depth == 0:
                    block = UNMAPPED[start:index + 1]
                    break
        else:
            self.fail("unterminated Component.onCompleted")
        self.assertIn("reloadUnmapped", block)
        self.assertIn("visible", block)

    def test_card_helpers(self):
        script = f"""
const vm = require("vm");
const sources = {json.dumps([extract_function(UNMAPPED, name)
                              for name in ("isBusy", "visibleRows", "boundLine",
                                           "statusText", "countLabel")])};
const agenda = {{
  unmappedBusy: false,
  unmappedError: "boom",
  unmappedVisibleRows() {{ return [{{folder: "/x/a"}}]; }}
}};
const root = {{agenda: agenda}};
const context = {{root}};
vm.createContext(context);
for (const value of sources) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
  root[fn.name] = fn;
}}
console.log(JSON.stringify({{
  busy: root.isBusy(),
  rows: root.visibleRows(),
  status: root.statusText(),
  one: root.countLabel(1),
  many: root.countLabel(3),
  bounded: root.boundLine("a  b", 120)
}}));
"""
        value = run_node(script)
        self.assertFalse(value["busy"])
        self.assertEqual(value["rows"], [{"folder": "/x/a"}])
        self.assertEqual(value["status"], "boom")
        self.assertEqual(value["one"], "1 session")
        self.assertEqual(value["many"], "3 sessions")
        self.assertEqual(value["bounded"], "a b")


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class OpenSessionHandoffTests(unittest.TestCase):
    def test_focus_daily_session_selects_or_degrades(self):
        script = f"""
const vm = require("vm");
const source = {json.dumps(extract_function(PLANNER, "focusDailySession"))};
const selected = [];
const root = {{
  agenda: {{
    selectLedgerEntry(sid) {{ selected.push(sid); return sid === "ab".repeat(16); }}
  }}
}};
const context = {{root, selected, activeTab: "projects"}};
vm.createContext(context);
const fn = vm.runInContext("(" + source + ")", context);
context[fn.name] = fn;
root[fn.name] = fn;
const known = root.focusDailySession("ab".repeat(16));
const knownTab = context.activeTab;
context.activeTab = "projects";
const unknown = root.focusDailySession("cd".repeat(16));
const malformed = root.focusDailySession("nope");
const empty = root.focusDailySession("");
root.agenda = null;
const noAgenda = root.focusDailySession("ab".repeat(16));
console.log(JSON.stringify({{known, knownTab, selected, unknown,
  malformed, empty, noAgenda, tab: context.activeTab}}));
"""
        value = run_node(script)
        self.assertTrue(value["known"])
        self.assertEqual(value["knownTab"], "daily")
        # Unknown/unloaded sessions degrade to the Daily tab with no
        # selection and never an error (no throw, false return).
        self.assertFalse(value["unknown"])
        self.assertFalse(value["malformed"])
        self.assertFalse(value["empty"])
        self.assertFalse(value["noAgenda"])
        self.assertEqual(value["tab"], "daily")

    def test_open_new_project_prefills_folder(self):
        script = f"""
const vm = require("vm");
const source = {json.dumps(extract_function(PLANNER, "openNewProjectWithFolder"))};
const root = {{
  projectFormFolder: "",
  openNewProject() {{ this.opened = true; return true; }}
}};
const context = {{root, notice: ""}};
vm.createContext(context);
const fn = vm.runInContext("(" + source + ")", context);
context[fn.name] = fn;
root[fn.name] = fn;
const ok = root.openNewProjectWithFolder("/x/work");
const prefilled = context.projectFormFolder;
const long = root.openNewProjectWithFolder("/x/" + "a".repeat(600));
const truncated = context.projectFormFolder;
const empty = root.openNewProjectWithFolder("   ");
console.log(JSON.stringify({{ok, prefilled, long, truncated,
  empty, notice: context.notice}}));
"""
        value = run_node(script)
        self.assertTrue(value["ok"])
        self.assertEqual(value["prefilled"], "/x/work")
        self.assertTrue(value["long"])
        self.assertEqual(len(value["truncated"]), 512)
        self.assertFalse(value["empty"])
        self.assertTrue(value["notice"])

    def test_planner_routes_session_action(self):
        text = PLANNER
        self.assertIn("function focusDailySession(sessionId)", text)
        self.assertIn('activeTab = "daily"', text)
        self.assertIn('act !== "session") act = ""', text)
        self.assertIn("root.focusDailySession(msg)", text)
        self.assertIn('if (msg && act !== "session") notice = msg', text)
        self.assertIn("function openNewProjectWithFolder(folder)", text)


if __name__ == "__main__":
    unittest.main()
