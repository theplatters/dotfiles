import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
AGENDA = (ROOT / "widgets" / "DailyAgenda.qml").read_text(encoding="utf-8")
PLANNER_UI = (ROOT / "widgets" / "DailyPlanner.qml").read_text(encoding="utf-8")
POPOUT = (ROOT / "widgets" / "CalendarPopout.qml").read_text(encoding="utf-8")
PLANNER = (ROOT / "widgets" / "ProjectPlanner.qml").read_text(encoding="utf-8")
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


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class DailyAgendaLogicTests(unittest.TestCase):
    def run_node(self, script):
        completed = subprocess.run(
            ["node", "-e", script], text=True, capture_output=True,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr)
        return json.loads(completed.stdout)

    def agenda_functions(self, *names):
        return {name: extract_function(AGENDA, name) for name in names}

    def test_iso_date_helpers_validate_real_calendar_days(self):
        fns = self.agenda_functions("pad2", "isoDate", "todayIso", "parseIsoDate", "isValidIso")
        script = f"""
const pad2 = new Function("return " + {json.dumps(fns["pad2"])})();
const isoDate = new Function("root", "return (" + {json.dumps(fns["isoDate"])} + ")")({{pad2}});
const todayIso = new Function("root", "return (" + {json.dumps(fns["todayIso"])} + ")")({{isoDate}});
const parseIsoDate = new Function("return " + {json.dumps(fns["parseIsoDate"])})();
const isValidIso = new Function("root", "return (" + {json.dumps(fns["isValidIso"])} + ")")({{parseIsoDate}});
const now = new Date();
const today = todayIso();
const expected = now.getFullYear() + "-" + pad2(now.getMonth() + 1) + "-" + pad2(now.getDate());
const parsed = parseIsoDate("2026-09-13");
console.log(JSON.stringify({{
  today, expected, parsed,
  leap: parseIsoDate("2024-02-29"),
  nonLeap: parseIsoDate("2026-02-29"),
  bad: [parseIsoDate("2026-13-01"), parseIsoDate("nope"), parseIsoDate(" 2026-09-13"),
    parseIsoDate(5), parseIsoDate(null)],
  valid: [isValidIso("2026-09-13"), isValidIso("2026-02-30"), isValidIso(""), isValidIso(null)],
  iso: isoDate(2026, 9, 5)
}}));
"""
        value = self.run_node(script)
        self.assertEqual(value["today"], value["expected"])
        self.assertRegex(value["today"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(value["parsed"], {"year": 2026, "month": 9, "day": 13})
        self.assertEqual(value["leap"], {"year": 2024, "month": 2, "day": 29})
        self.assertEqual(value["bad"], [None, None, None, None, None])
        self.assertEqual(value["valid"], [True, False, False, False])
        self.assertEqual(value["iso"], "2026-09-05")

    def test_month_grid_is_monday_start_with_42_cells(self):
        fns = self.agenda_functions("pad2", "isoDate", "monthGrid", "monthLabel")
        script = f"""
const pad2 = new Function("return " + {json.dumps(fns["pad2"])})();
const isoDate = new Function("root", "return (" + {json.dumps(fns["isoDate"])} + ")")({{pad2}});
const monthGrid = new Function("root", "return (" + {json.dumps(fns["monthGrid"])} + ")")({{isoDate}});
const monthLabel = new Function("return " + {json.dumps(fns["monthLabel"])})();
const cells = monthGrid(2026, 8);
const first = new Date(2026, 8, 1);
const offset = (first.getDay() + 6) % 7;
const probe = new Date(2026, 8, 1 - offset);
const expectFirst = probe.getFullYear() + "-" + pad2(probe.getMonth() + 1) + "-" + pad2(probe.getDate());
const mondays = cells.filter(c => new Date(c.y, c.m - 1, c.d).getDay() === 1).length;
console.log(JSON.stringify({{
  count: cells.length, first: cells[0].iso, expectFirst,
  inMonth: cells.filter(c => c.inMonth).length,
  mondays, label: monthLabel(2026, 8),
  contiguous: cells.every((c, i) => i === 0 || (
    new Date(c.y, c.m - 1, c.d) - new Date(cells[i-1].y, cells[i-1].m - 1, cells[i-1].d) === 86400000))
}}));
"""
        value = self.run_node(script)
        self.assertEqual(value["count"], 42)
        self.assertEqual(value["first"], value["expectFirst"])
        self.assertEqual(value["inMonth"], 30)  # September 2026
        self.assertEqual(value["mondays"], 6)
        self.assertEqual(value["label"], "September 2026")
        self.assertTrue(value["contiguous"])

    def test_agenda_and_picker_filters_match_the_backend_contract(self):
        fns = self.agenda_functions("agendaItemsFor", "pickerItemsFor")
        script = f"""
const agendaItemsFor = new Function("return " + {json.dumps(fns["agendaItemsFor"])})();
const pickerItemsFor = new Function("return " + {json.dumps(fns["pickerItemsFor"])})();
const tasks = [
  {{path: "pages/Work.md", page: "Work", line: 1, task: "Ship the thing", done: false, scheduledDate: "2026-09-13"}},
  {{path: "pages/Work.md", page: "Work", line: 2, task: "Old done", done: true, scheduledDate: "2026-09-13"}},
  {{path: "pages/Home.md", page: "Home", line: 1, task: "Unscheduled", done: false, scheduledDate: ""}},
  {{path: "pages/Home.md", page: "Home", line: 5, task: "Done elsewhere", done: true, scheduledDate: "2026-09-14"}},
  {{path: "pages/Home.md", page: "Home", line: 9, task: "Done unscheduled", done: true, scheduledDate: ""}}
];
const agenda = agendaItemsFor(tasks, "2026-09-13");
const pickerAll = pickerItemsFor(tasks, "");
const pickerSearch = pickerItemsFor(tasks, "ship");
const pickerPage = pickerItemsFor(tasks, "home");
console.log(JSON.stringify({{
  agenda: agenda.map(e => e.line),
  pickerAll: pickerAll.map(e => e.task),
  pickerSearch: pickerSearch.map(e => e.task),
  pickerPage: pickerPage.map(e => e.task),
  empty: agendaItemsFor(null, "2026-09-13")
}}));
"""
        value = self.run_node(script)
        # Agenda: open + done scheduled on the date; done elsewhere excluded.
        self.assertEqual(value["agenda"], [1, 2])
        # Picker: open tasks only (done never offered).
        self.assertEqual(value["pickerAll"], ["Ship the thing", "Unscheduled"])
        self.assertEqual(value["pickerSearch"], ["Ship the thing"])
        self.assertEqual(value["pickerPage"], ["Unscheduled"])
        self.assertEqual(value["empty"], [])

    def test_pomodoro_tick_uses_wall_clock_and_switches_phases(self):
        fns = self.agenda_functions("pomoPhaseSeconds", "pomoFormatted")
        tick = extract_function(AGENDA, "pomoTick")
        script = f"""
const pomoPhaseSeconds = new Function("root", "return (" + {json.dumps(fns["pomoPhaseSeconds"])} + ")")(
  {{focusMinutes: 25, breakMinutes: 5}});
const pomoFormatted = new Function("root", "return (" + {json.dumps(fns["pomoFormatted"])} + ")")(
  {{pomoRemainingSec: 1500}});
const context = {{focusMinutes: 25, breakMinutes: 5, pomoPhase: "focus",
  pomoRunning: true, pomoDeadline: Date.now() - 1000, pomoRemainingSec: 5,
  pomoCompleted: 0, pomoMessage: "",
  pomoPhaseSeconds(phase) {{ return phase === "break" ? this.breakMinutes * 60 : this.focusMinutes * 60; }},
  pomoTimer: {{stop() {{}}}}}};
const vm = require("vm");
vm.createContext(context);
context.root = context;
context.pomoTick = vm.runInContext("(" + {json.dumps(tick)} + ")", context);
context.pomoTick();
const afterFocus = {{phase: context.pomoPhase, running: context.pomoRunning,
  remaining: context.pomoRemainingSec, completed: context.pomoCompleted, message: context.pomoMessage}};
context.pomoDeadline = Date.now() - 1000;
context.pomoRunning = true;
context.pomoTick();
console.log(JSON.stringify({{afterFocus,
  afterBreak: {{phase: context.pomoPhase, running: context.pomoRunning,
    remaining: context.pomoRemainingSec, message: context.pomoMessage}},
  formatted: pomoFormatted(), focusSecs: pomoPhaseSeconds("focus"), breakSecs: pomoPhaseSeconds("break")}}));
"""
        value = self.run_node(script)
        self.assertEqual(value["afterFocus"]["phase"], "break")
        self.assertFalse(value["afterFocus"]["running"])
        self.assertEqual(value["afterFocus"]["remaining"], 300)
        self.assertEqual(value["afterFocus"]["completed"], 1)
        self.assertIn("Focus complete", value["afterFocus"]["message"])
        self.assertEqual(value["afterBreak"]["phase"], "idle")
        self.assertIn("Break over", value["afterBreak"]["message"])
        self.assertEqual(value["formatted"], "25:00")
        self.assertEqual(value["focusSecs"], 1500)
        self.assertEqual(value["breakSecs"], 300)


class DailyAgendaStructureTests(unittest.TestCase):
    def test_backend_contract_uses_process_stdin_without_shell(self):
        self.assertIn('scripts/daily_agenda.py', AGENDA)
        for command in ('"list"', '"select"', '"complete"', '"toggle"'):
            self.assertIn(command, AGENDA)
        self.assertIn("writeJson(agendaProcess", AGENDA)
        self.assertIn("JSON.stringify(value)", AGENDA)
        self.assertIn("stdinEnabled = true", AGENDA)
        self.assertIn("workingDirectory: Quickshell.shellPath", AGENDA)
        self.assertIn("StdioCollector", AGENDA)
        self.assertNotIn("sh -c", AGENDA)
        self.assertNotIn("shellPath(\"scripts/daily_agenda.py\") +", AGENDA)

    def test_single_operation_is_serialized_and_failures_surface(self):
        self.assertIn("property bool agendaBusy: false", AGENDA)
        self.assertIn("property bool agendaRetiring: false", AGENDA)
        self.assertIn("property int agendaGeneration: 0", AGENDA)
        self.assertIn("property int agendaLaunchGeneration", AGENDA)
        self.assertIn("if (generation !== root.agendaGeneration) return", AGENDA)
        self.assertIn("function handleAgendaStartFailure(generation)", AGENDA)
        self.assertIn("process could not start", AGENDA)
        self.assertIn("function busyReason()", AGENDA)
        self.assertIn("function cancelListRead()", AGENDA)
        self.assertIn("agendaTimeout", AGENDA)
        # Writes are never killed: warning only, like the planner toggle.
        self.assertIn("agendaWriteWarning", AGENDA)
        self.assertIn("it will not be cancelled", AGENDA)
        # Mutations reload the day view.
        finish = extract_function(AGENDA, "finishAgenda")
        self.assertIn("root.pageWritten(page)", finish)
        self.assertIn("root.startList()", finish)

    def test_completion_freezes_snapshot_and_preserves_drafts(self):
        self.assertIn("property var completionTask: null", AGENDA)
        self.assertIn("property string completionNote", AGENDA)
        self.assertIn("property string completionError", AGENDA)
        self.assertIn("property bool completionSaving: false", AGENDA)
        begin = extract_function(AGENDA, "beginCompletion")
        self.assertIn("revision", begin)
        self.assertIn("completionTask = {", begin)
        self.assertIn("completionNote = \"\"", begin)
        save = extract_function(AGENDA, "saveCompletion")
        self.assertIn("frozen", save)
        self.assertIn("completionSaving = true", save)
        self.assertIn("trim()", save)
        self.assertIn("Write a progress note", AGENDA)
        self.assertIn("function cancelCompletion()", AGENDA)
        # Stale/failed writes keep the frozen draft and note.
        finish = extract_function(AGENDA, "finishAgenda")
        self.assertIn("completionSaving = false", finish)
        self.assertIn("completionTask = null", finish)
        self.assertIn("stale", finish.lower())
        # Date changes and reloads are blocked while saving.
        for name in ("setSelectedDate", "reload"):
            body = extract_function(AGENDA, name)
            self.assertIn("completionSaving", body)

    def test_pomodoro_state_lives_in_the_shared_component(self):
        for token in ("property int focusMinutes: 25", "property int breakMinutes: 5",
                      "property string pomoPhase", "property bool pomoRunning",
                      "property double pomoDeadline", "property int pomoRemainingSec",
                      "property int pomoCompleted", "property string pomoMessage"):
            self.assertIn(token, AGENDA)
        for name in ("pomoStart", "pomoPause", "pomoReset", "pomoTick",
                     "pomoFormatted", "pomoPhaseSeconds"):
            self.assertIn("function " + name + "(", AGENDA)
        # Wall-clock deadlines (survive popup close); no auto task writes.
        self.assertIn("Date.now()", AGENDA)
        self.assertIn("pomoDeadline", AGENDA)
        for name in ("pomoStart", "pomoPause", "pomoReset", "pomoTick"):
            body = extract_function(AGENDA, name)
            self.assertNotIn("daily_agenda", body)
            self.assertNotIn("agendaProcess", body)


class DailyPlannerUiTests(unittest.TestCase):
    def test_planner_binds_the_shared_agenda_with_shared_controls(self):
        self.assertIn("property var agenda", PLANNER_UI)
        self.assertIn("WidgetButton", PLANNER_UI)
        self.assertIn("WidgetIconButton", PLANNER_UI)
        self.assertNotIn("StyledButton", PLANNER_UI)

    def test_calendar_grid_has_prev_next_today_and_clickable_days(self):
        self.assertIn("monthGrid(root.viewYear, root.viewMonth)", PLANNER_UI)
        self.assertIn("monthLabel(root.viewYear, root.viewMonth)", PLANNER_UI)
        self.assertIn("shiftMonth(-1)", PLANNER_UI)
        self.assertIn("shiftMonth(1)", PLANNER_UI)
        self.assertIn("goToday()", PLANNER_UI)
        self.assertIn("todayIso()", PLANNER_UI)
        self.assertIn("setSelectedDate(modelData.iso)", PLANNER_UI)
        self.assertIn("weekdayNames()", PLANNER_UI)
        self.assertIn("Accessible.name: modelData.iso", PLANNER_UI)

    def test_picker_searches_open_tasks_and_schedules(self):
        self.assertIn("pickerItems(root.pickerFilter)", PLANNER_UI)
        self.assertIn("Search open tasks", PLANNER_UI)
        self.assertIn("pickerFilter", PLANNER_UI)
        self.assertIn("selectEntry(modelData, true)", PLANNER_UI)
        self.assertIn("Add to day", PLANNER_UI)

    def test_scheduled_section_shows_done_and_removes(self):
        self.assertIn("agendaItems()", PLANNER_UI)
        self.assertIn("toggleEntry(modelData)", PLANNER_UI)
        self.assertIn("selectEntry(modelData, false)", PLANNER_UI)
        self.assertIn("Remove", PLANNER_UI)
        self.assertIn("Scheduled", PLANNER_UI)

    def test_completion_editor_requires_a_note_and_saves_explicitly(self):
        self.assertIn("completionTask", PLANNER_UI)
        self.assertIn("beginCompletion(modelData)", PLANNER_UI)
        self.assertIn("Progress note", PLANNER_UI)
        self.assertIn("completionNote", PLANNER_UI)
        self.assertIn("Complete & save", PLANNER_UI)
        self.assertIn("saveCompletion()", PLANNER_UI)
        self.assertIn("cancelCompletion()", PLANNER_UI)
        self.assertIn("trim()", PLANNER_UI)
        self.assertIn("completionError", PLANNER_UI)

    def test_planner_guards_busy_saving_and_reloads(self):
        self.assertIn("agendaBusy", PLANNER_UI)
        self.assertIn("completionSaving", PLANNER_UI)
        self.assertIn("agenda.reload()", PLANNER_UI)
        self.assertIn("Reload", PLANNER_UI)
        self.assertIn("agendaError", PLANNER_UI)
        self.assertIn("agendaNotice", PLANNER_UI)
        self.assertIn("agendaTruncated", PLANNER_UI)

    def test_pomodoro_controls_are_present_without_new_assets(self):
        self.assertIn("pomoFormatted()", PLANNER_UI)
        self.assertIn("pomoStart()", PLANNER_UI)
        self.assertIn("pomoPause()", PLANNER_UI)
        self.assertIn("pomoReset()", PLANNER_UI)
        self.assertIn("pomoMessage", PLANNER_UI)
        self.assertIn("focusMinutes", PLANNER_UI)
        self.assertIn("breakMinutes", PLANNER_UI)

    def test_theme_tokens_are_used_without_hardcoded_colors(self):
        for token in ("Theme.text", "Theme.subtext0", "Theme.subtext1",
                      "Theme.base", "Theme.mantle", "Theme.surface0",
                      "Theme.border", "Theme.focusBorder", "Theme.red",
                      "Theme.controlRadius", "Theme.fontFamily"):
            self.assertIn(token, PLANNER_UI)
        self.assertNotIn("#070707", PLANNER_UI)
        self.assertNotIn("#0C0C0C", PLANNER_UI)


class CalendarPopoutTests(unittest.TestCase):
    def test_popout_anchors_to_the_clock_and_shares_state(self):
        self.assertIn("PopupWindow", POPOUT)
        self.assertIn("property var anchorItem", POPOUT)
        self.assertIn("property var agenda", POPOUT)
        self.assertIn("anchorItem.mapToGlobal", POPOUT)
        self.assertIn("gravity: Edges.Bottom", POPOUT)
        self.assertIn("property bool requestedOpen", POPOUT)
        self.assertIn("property bool closing", POPOUT)
        self.assertIn("visible: false", POPOUT)
        self.assertIn("function setOpen(open)", POPOUT)
        self.assertIn("function toggle(item)", POPOUT)
        self.assertIn("exitMotion", POPOUT)
        self.assertIn("root.visible = false", POPOUT)

    def test_popout_embeds_the_reusable_daily_planner(self):
        self.assertIn("DailyPlanner {", POPOUT)
        self.assertIn("agenda: root.agenda", POPOUT)
        self.assertIn("syncViewToDate()", POPOUT)
        self.assertIn("WidgetIconButton", POPOUT)
        self.assertIn('iconSource: "icons/x.svg"', POPOUT)


class ShellAndBarWiringTests(unittest.TestCase):
    def test_shell_owns_one_agenda_shared_by_popout_and_planner(self):
        self.assertIn("DailyAgenda {", SHELL)
        self.assertIn("id: dailyAgenda", SHELL)
        self.assertIn("CalendarPopout {", SHELL)
        self.assertIn("anchorItem: bar.clockAnchor", SHELL)
        self.assertIn("agenda: dailyAgenda", SHELL)
        self.assertIn("calendarPopout: calendarPopout", SHELL)
        self.assertIn("agenda: dailyAgenda", SHELL)

    def test_bar_clock_click_opens_the_popout_without_losing_hover(self):
        self.assertIn("property var calendarPopout", BAR)
        self.assertIn("property alias clockAnchor: clockContainer", BAR)
        clock = BAR[BAR.index('id: clockContainer'):BAR.index('id: centerSlot')]
        self.assertIn("onEntered: clockContainer.color = Theme.surface0", clock)
        self.assertIn("onExited: clockContainer.color = Theme.mantle", clock)
        self.assertIn("bar.calendarPopout.toggle(clockContainer)", clock)
        self.assertIn("cursorShape: Qt.PointingHandCursor", clock)


class ProjectPlannerDailyTabTests(unittest.TestCase):
    def test_daily_tab_uses_shared_icon_controls(self):
        self.assertIn('text: "Daily"', PLANNER)
        self.assertIn('iconSource: "icons/history.svg"', PLANNER)
        self.assertIn('Accessible.name: "Daily planner tab"', PLANNER)
        self.assertIn('onClicked: root.selectTab("daily")', PLANNER)
        self.assertIn('checked: root.activeTab === "daily"', PLANNER)

    def test_daily_tab_fits_the_existing_tab_architecture(self):
        select = extract_function(PLANNER, "selectTab")
        self.assertIn('"daily"', select)
        self.assertIn('"journal"', select)
        self.assertIn("tabBlockedReason(target)", select)
        self.assertIn("pauseIdleAgents()", select)
        self.assertIn('root.activeTab = "daily"', select)
        self.assertIn("property var agenda", PLANNER)
        self.assertIn("DailyPlanner {", PLANNER)
        self.assertIn("agenda: root.agenda", PLANNER)
        self.assertIn('Accessible.name: "Daily planner"', PLANNER)
        # Existing tabs keep their routing and guards.
        self.assertIn('onClicked: root.selectTab("projects")', PLANNER)
        self.assertIn('onClicked: root.selectTab("journal")', PLANNER)
        self.assertIn("function tabBlockedReason(target)", PLANNER)
        self.assertIn("function blockedReason()", PLANNER)

    def test_agenda_writes_refresh_the_page_cache_safely(self):
        self.assertIn("function applyAgendaPage(page)", PLANNER)
        self.assertIn("onPageWritten(page)", PLANNER)
        self.assertIn("root.applyAgendaPage(page)", PLANNER)
        apply = extract_function(PLANNER, "applyAgendaPage")
        self.assertIn("pageCache", apply)
        self.assertIn("currentPage = page", apply)
        self.assertIn("staleToggle = false", apply)
        self.assertNotIn("worker.prompt", apply)
        self.assertNotIn("startPage", apply)
        blocked = extract_function(PLANNER, "blockedReason")
        self.assertIn("completionSaving", blocked)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class DailyAgendaDraftRecoveryTests(unittest.TestCase):
    def run_node(self, script):
        completed = subprocess.run(
            ["node", "-e", script], text=True, capture_output=True,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr)
        return json.loads(completed.stdout)

    def agenda_harness(self, extra=""):
        fns = {name: extract_function(AGENDA, name) for name in (
            "sameCompletionTarget", "isCompletionStale",
            "beginCompletion", "reselectCompletion")}
        preamble = "\n".join(
            f"const {name}Src = {json.dumps(src)};"
            for name, src in fns.items()
        )
        return preamble + "\n" + extra

    def test_same_task_finish_is_noop_preserving_note(self):
        script = self.agenda_harness() + """
const vm = require("vm");
const ctx = {completionTask: null, completionNote: "", completionError: "",
  completionSaving: false, agendaBusy: false, agendaRetiring: false};
ctx.root = ctx;
vm.createContext(ctx);
for (const n of ["sameCompletionTarget", "isCompletionStale", "beginCompletion", "reselectCompletion"])
  ctx[n] = vm.runInContext("(" + eval(n + "Src") + ")", ctx);
vm.runInContext(`result = (function() {
  const task = {path: "pages/Work.md", revision: "r1", line: 1, task: "alpha", page: "Work"};
  root.beginCompletion(task);
  root.completionNote = "draft note";
  const same = root.beginCompletion({path: "pages/Work.md", revision: "r1", line: 1, task: "alpha", page: "Work"});
  return {same, note: root.completionNote, line: root.completionTask.line, err: root.completionError};
})()`, ctx);
console.log(JSON.stringify(ctx.result));
"""
        value = self.run_node(script)
        self.assertTrue(value["same"])
        self.assertEqual(value["note"], "draft note")
        self.assertEqual(value["line"], 1)

    def test_different_task_finish_blocked_preserving_draft(self):
        script = self.agenda_harness() + """
const vm = require("vm");
const ctx = {completionTask: null, completionNote: "", completionError: "",
  completionSaving: false, agendaBusy: false, agendaRetiring: false};
ctx.root = ctx;
vm.createContext(ctx);
for (const n of ["sameCompletionTarget", "isCompletionStale", "beginCompletion", "reselectCompletion"])
  ctx[n] = vm.runInContext("(" + eval(n + "Src") + ")", ctx);
vm.runInContext(`result = (function() {
  root.beginCompletion({path: "pages/Work.md", revision: "r1", line: 1, task: "alpha", page: "Work"});
  root.completionNote = "keep me";
  const switched = root.beginCompletion({path: "pages/Work.md", revision: "r1", line: 2, task: "beta", page: "Work"});
  return {switched, note: root.completionNote, line: root.completionTask.line, err: root.completionError};
})()`, ctx);
console.log(JSON.stringify(ctx.result));
"""
        value = self.run_node(script)
        self.assertFalse(value["switched"])
        self.assertEqual(value["note"], "keep me")
        self.assertEqual(value["line"], 1)
        self.assertIn("Cancel", value["err"])
        self.assertIn("Reselect", value["err"])

    def test_reselect_after_stale_preserves_note_and_retargets(self):
        script = self.agenda_harness() + """
const vm = require("vm");
const ctx = {completionTask: null, completionNote: "", completionError: "",
  completionSaving: false, agendaBusy: false, agendaRetiring: false};
ctx.root = ctx;
vm.createContext(ctx);
for (const n of ["sameCompletionTarget", "isCompletionStale", "beginCompletion", "reselectCompletion"])
  ctx[n] = vm.runInContext("(" + eval(n + "Src") + ")", ctx);
vm.runInContext(`result = (function() {
  root.beginCompletion({path: "pages/Work.md", revision: "old", line: 1, task: "alpha", page: "Work"});
  root.completionNote = "my draft";
  root.completionError = "page revision is stale; reload the page";
  const staleBefore = root.isCompletionStale();
  const ok = root.reselectCompletion({path: "pages/Work.md", revision: "new", line: 1, task: "alpha", page: "Work"});
  return {staleBefore, ok, note: root.completionNote,
    revision: root.completionTask.revision, err: root.completionError,
    staleAfter: root.isCompletionStale()};
})()`, ctx);
console.log(JSON.stringify(ctx.result));
"""
        value = self.run_node(script)
        self.assertTrue(value["staleBefore"])
        self.assertTrue(value["ok"])
        self.assertEqual(value["note"], "my draft")
        self.assertEqual(value["revision"], "new")
        self.assertEqual(value["err"], "")
        self.assertFalse(value["staleAfter"])

    def test_reselect_never_clears_note_silently(self):
        reselect = extract_function(AGENDA, "reselectCompletion")
        self.assertNotIn('completionNote = ""', reselect)
        begin = extract_function(AGENDA, "beginCompletion")
        # Guarded re-entry: same-target no-op and different-target block.
        self.assertIn("sameCompletionTarget", begin)
        self.assertIn("completionTask !== null", begin)
        self.assertIn("Reselect", begin)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class DailyAgendaCliErrorTests(unittest.TestCase):
    def run_node(self, script):
        completed = subprocess.run(
            ["node", "-e", script], text=True, capture_output=True,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr)
        return json.loads(completed.stdout)

    def finish_harness(self):
        finish = extract_function(AGENDA, "finishAgenda")
        return f"const finishSrc = {json.dumps(finish)};"

    def test_complete_cli_error_exit1_preserves_draft_and_survives_list(self):
        script = self.finish_harness() + """
const vm = require("vm");
const ctx = {agendaGeneration: 1, selectedDate: "2026-09-13", agendaTasks: [],
  graphName: "Notes", agendaTruncated: false, agendaError: "", agendaNotice: "",
  agendaSlow: false, agendaBusy: false, agendaStickyError: "",
  completionTask: {path: "pages/Work.md", revision: "old", line: 1, task: "alpha", page: "Work"},
  completionNote: "my draft", completionError: "", completionSaving: true,
  failure(label, code, detail) { return label + " failed (exit " + code + "): " + detail; },
  startListCalled: 0, startList() { this.startListCalled++; return true; },
  pageWrittenCalled: 0, pageWritten(p) { this.pageWrittenCalled++; },
  agendaTimeout: {stop() {}}, agendaWriteWarning: {stop() {}}};
ctx.root = ctx;
vm.createContext(ctx);
ctx.finishAgenda = vm.runInContext("(" + finishSrc + ")", ctx);
vm.runInContext(`result = (function() {
  // Actual CLI shape: {"error": "..."} on stdout, exit 1, empty stderr.
  root.finishAgenda(1, JSON.stringify({error: "page revision is stale; reload the page"}), "", 1, "complete");
  const afterFail = {saving: root.completionSaving, err: root.completionError,
    kept: root.completionTask !== null, note: root.completionNote, listed: root.startListCalled};
  root.agendaGeneration = 2; root.agendaBusy = false;
  root.finishAgenda(0, JSON.stringify({date: "2026-09-13", graphName: "Notes", tasks: [], truncated: false}), "", 2, "list");
  return {afterFail, errAfterList: root.completionError, noteAfterList: root.completionNote,
    taskAfterList: root.completionTask !== null};
})()`, ctx);
console.log(JSON.stringify(ctx.result));
"""
        value = self.run_node(script)
        self.assertFalse(value["afterFail"]["saving"])
        self.assertIn("stale", value["afterFail"]["err"])
        self.assertTrue(value["afterFail"]["kept"])
        self.assertEqual(value["afterFail"]["note"], "my draft")
        self.assertEqual(value["afterFail"]["listed"], 1)
        self.assertIn("stale", value["errAfterList"])
        self.assertEqual(value["noteAfterList"], "my draft")
        self.assertTrue(value["taskAfterList"])

    def test_select_mutation_error_survives_recovery_list(self):
        script = self.finish_harness() + """
const vm = require("vm");
const ctx = {agendaGeneration: 3, selectedDate: "2026-09-13", agendaTasks: [],
  graphName: "Notes", agendaTruncated: false, agendaError: "", agendaNotice: "",
  agendaSlow: false, agendaBusy: false, agendaStickyError: "",
  completionTask: null, completionNote: "", completionError: "", completionSaving: false,
  failure(label, code, detail) { return label + " failed (exit " + code + "): " + detail; },
  startListCalled: 0, startList() { this.startListCalled++; return true; },
  pageWrittenCalled: 0, pageWritten(p) { this.pageWrittenCalled++; },
  agendaTimeout: {stop() {}}, agendaWriteWarning: {stop() {}}};
ctx.root = ctx;
vm.createContext(ctx);
ctx.finishAgenda = vm.runInContext("(" + finishSrc + ")", ctx);
vm.runInContext(`result = (function() {
  root.finishAgenda(1, JSON.stringify({error: "page revision is stale; reload the page"}), "", 3, "select");
  const afterFail = {err: root.agendaError, sticky: root.agendaStickyError, listed: root.startListCalled};
  root.agendaGeneration = 4; root.agendaBusy = false;
  root.finishAgenda(0, JSON.stringify({date: "2026-09-13", graphName: "Notes", tasks: [], truncated: false}), "", 4, "list");
  return {afterFail, errAfterList: root.agendaError, stickyAfter: root.agendaStickyError};
})()`, ctx);
console.log(JSON.stringify(ctx.result));
"""
        value = self.run_node(script)
        self.assertIn("stale", value["afterFail"]["err"])
        self.assertIn("stale", value["afterFail"]["sticky"])
        self.assertIn("stale", value["errAfterList"])

    def test_finish_parses_stdout_regardless_of_exit_and_requires_code0(self):
        finish = extract_function(AGENDA, "finishAgenda")
        # Errors are parsed even on nonzero exit; success needs code 0.
        self.assertIn('try { data = JSON.parse(output', finish)
        self.assertNotIn("if (code === 0) {\n            try { data = JSON.parse", finish)
        self.assertIn("code === 0 && data", finish)
        self.assertIn("code === 0 && shapeValid", finish)
        self.assertIn("agendaStickyError", finish)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class DailyPlannerGoTodayTests(unittest.TestCase):
    def run_node(self, script):
        completed = subprocess.run(
            ["node", "-e", script], text=True, capture_output=True,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr)
        return json.loads(completed.stdout)

    def test_go_today_from_other_month_lands_on_today(self):
        go = extract_function(PLANNER_UI, "goToday")
        sync = extract_function(PLANNER_UI, "syncViewToDate")
        script = f"""
const vm = require("vm");
const goSrc = {json.dumps(go)};
const syncSrc = {json.dumps(sync)};
const agenda = {{selectedDate: "2026-01-15",
  todayIso() {{ return "2026-09-13"; }},
  parseIsoDate(v) {{ const m = /^(\\d{{4}})-(\\d{{2}})-(\\d{{2}})$/.exec(v);
    if (!m) return null; return {{year: Number(m[1]), month: Number(m[2]), day: Number(m[3])}}; }},
  setSelectedDate(d) {{ this.selectedDate = d; return true; }}}};
const ctx = {{agenda, viewYear: 2026, viewMonth: 0}};
ctx.root = ctx;
vm.createContext(ctx);
ctx.syncViewToDate = vm.runInContext("(" + syncSrc + ")", ctx);
ctx.goToday = vm.runInContext("(" + goSrc + ")", ctx);
vm.runInContext("result = (function() {{ root.goToday(); return {{viewYear: root.viewYear, viewMonth: root.viewMonth, sel: root.agenda.selectedDate}}; }})()", ctx);
console.log(JSON.stringify(ctx.result));
"""
        value = self.run_node(script)
        self.assertEqual(value["sel"], "2026-09-13")
        self.assertEqual(value["viewYear"], 2026)
        self.assertEqual(value["viewMonth"], 8)

    def test_go_today_blocked_keeps_current_view(self):
        go = extract_function(PLANNER_UI, "goToday")
        sync = extract_function(PLANNER_UI, "syncViewToDate")
        script = f"""
const vm = require("vm");
const goSrc = {json.dumps(go)};
const syncSrc = {json.dumps(sync)};
const agenda = {{selectedDate: "2026-01-15",
  todayIso() {{ return "2026-09-13"; }},
  parseIsoDate(v) {{ const m = /^(\\d{{4}})-(\\d{{2}})-(\\d{{2}})$/.exec(v);
    if (!m) return null; return {{year: Number(m[1]), month: Number(m[2]), day: Number(m[3])}}; }},
  setSelectedDate(d) {{ return false; }}}};
const ctx = {{agenda, viewYear: 2026, viewMonth: 0}};
ctx.root = ctx;
vm.createContext(ctx);
ctx.syncViewToDate = vm.runInContext("(" + syncSrc + ")", ctx);
ctx.goToday = vm.runInContext("(" + goSrc + ")", ctx);
vm.runInContext("result = (function() {{ root.goToday(); return {{viewYear: root.viewYear, viewMonth: root.viewMonth, sel: root.agenda.selectedDate}}; }})()", ctx);
console.log(JSON.stringify(ctx.result));
"""
        value = self.run_node(script)
        self.assertEqual(value["sel"], "2026-01-15")
        self.assertEqual(value["viewMonth"], 0)

    def test_go_today_sets_before_syncing(self):
        go = extract_function(PLANNER_UI, "goToday")
        self.assertLess(go.index("setSelectedDate"), go.index("syncViewToDate"))
        self.assertIn("if (root.agenda.setSelectedDate", go)


class ProjectPlannerDailyRefreshTests(unittest.TestCase):
    def test_select_tab_daily_reloads_respecting_busy_and_draft(self):
        select = extract_function(PLANNER, "selectTab")
        daily = select[select.index('if (target === "daily")'):select.index('if (target === "journal")')]
        self.assertIn("agenda.reload()", daily)
        self.assertIn("agendaBusy", daily)
        self.assertIn("completionSaving", daily)
        self.assertIn("agendaRetiring", daily)

    def test_daily_planner_offers_explicit_reselect_preserving_note(self):
        self.assertIn("reselectCompletion(modelData)", PLANNER_UI)
        self.assertIn('"Reselect"', PLANNER_UI)
        self.assertIn("Reselect on the current task", PLANNER_UI)
        self.assertIn("isCompletionStale()", PLANNER_UI)
        self.assertIn("Cancel first", PLANNER_UI)
        reselect = extract_function(AGENDA, "reselectCompletion")
        self.assertIn("completionTask = {", reselect)
        self.assertNotIn('completionNote = ""', reselect)


if __name__ == "__main__":
    unittest.main()
