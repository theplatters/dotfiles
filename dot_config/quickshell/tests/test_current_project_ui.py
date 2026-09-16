import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
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


def vm_harness(functions, setup, exercise):
    """Build a node vm harness exposing the module functions as root.*.

    functions: list of extracted QML JS function sources.
    setup: JS snippet defining `context` extra state (must not redefine
        `context` itself; appended after the base state).
    exercise: JS snippet run after function install; should console.log JSON.
    """
    sources = json.dumps(functions)
    return f"""
const vm = require("vm");
const context = {{
  projectId: "", projectName: "", projectStatus: "",
  hasProject: false, backendOk: true, currentGeneration: 0,
  launchGeneration: -1, retiring: false,
  _launched: false, _started: false,
  compact: false, maxLabelWidth: 140,
  displayLabel: "", tooltipText: "",
  projectWatchdog: {{ stopped: false, restarted: false,
    stop() {{ this.stopped = true; }}, restart() {{ this.restarted = true; }} }},
  projectKillTimer: {{ stopped: false, restarted: false,
    stop() {{ this.stopped = true; }}, restart() {{ this.restarted = true; }} }},
  currentProjectProcess: {{ running: false, command: [], signaled: [],
    signal(sig) {{ this.signaled.push(sig); return true; }} }},
  Quickshell: {{ shellPath(value) {{ return "/repo/" + value; }} }},
  projectPlanner: null,
}};
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


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class CurrentProjectParsingTests(unittest.TestCase):
    def parse(self, payload, code=0):
        fn = extract_function(MODULE, "parseCurrentProject")
        script = f"""
const parse = new Function("return (" + {json.dumps(fn)} + ")")();
console.log(JSON.stringify(parse({json.dumps(payload)}, {code})));
"""
        return run_node(script)

    def test_associated_identity_is_valid(self):
        value = self.parse(json.dumps({
            "project": {"id": "11111111-1111-1111-1111-111111111111",
                        "name": "Alpha", "matched_by": "cwd"},
            "registry": {"id": "11111111-1111-1111-1111-111111111111"},
            "status": "associated",
        }))
        self.assertTrue(value["ok"])
        self.assertTrue(value["hasProject"])
        self.assertEqual(value["id"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(value["name"], "Alpha")
        self.assertEqual(value["status"], "associated")

    def test_name_only_no_linkage_is_valid_without_logseq(self):
        value = self.parse(json.dumps({
            "project": {"id": "22222222-2222-2222-2222-222222222222",
                        "name": "Beta", "matched_by": "git"},
            "registry": {"id": "22222222-2222-2222-2222-222222222222",
                         "logseq_path": ""},
            "status": "name-only-no-linkage",
        }))
        self.assertTrue(value["ok"])
        self.assertTrue(value["hasProject"])
        self.assertEqual(value["id"], "22222222-2222-2222-2222-222222222222")
        self.assertEqual(value["status"], "name-only-no-linkage")

    def test_unassociated_null_project_is_valid_empty(self):
        value = self.parse(json.dumps({
            "project": None, "registry": None, "status": "unassociated",
        }))
        self.assertTrue(value["ok"])
        self.assertFalse(value["hasProject"])
        self.assertEqual(value["id"], "")
        self.assertEqual(value["status"], "unassociated")

    def test_stale_removed_null_project_is_valid_empty(self):
        value = self.parse(json.dumps({
            "project": None, "registry": None, "status": "stale-removed",
        }))
        self.assertTrue(value["ok"])
        self.assertFalse(value["hasProject"])

    def test_nonzero_exit_is_rejected(self):
        value = self.parse(json.dumps({"project": None, "status": "unassociated"}), code=1)
        self.assertFalse(value["ok"])

    def test_malformed_output_is_rejected(self):
        value = self.parse("not json{{{")
        self.assertFalse(value["ok"])
        empty = self.parse("")
        self.assertFalse(empty["ok"])

    def test_unknown_status_and_missing_id_are_rejected(self):
        weird = self.parse(json.dumps({"project": {"id": "x", "name": "X"}, "status": "mystery"}))
        self.assertFalse(weird["ok"])
        no_id = self.parse(json.dumps(
            {"project": {"id": "", "name": ""}, "status": "associated"}))
        self.assertFalse(no_id["ok"])
        # A non-null project alongside an empty status must not validate.
        mismatch = self.parse(json.dumps(
            {"project": {"id": "33333333-3333-3333-3333-333333333333", "name": "Z"},
             "status": "unassociated"}))
        self.assertFalse(mismatch["ok"])

    def test_non_string_id_or_name_is_rejected_without_coercion(self):
        # Objects/numbers/null must not coerce via String() into identities.
        for bad_id in ({"uuid": 1}, 12345, None, ["x"]):
            value = self.parse(json.dumps(
                {"project": {"id": bad_id, "name": "Alpha"}, "status": "associated"}))
            self.assertFalse(value["ok"], "accepted id: %r" % (bad_id,))
        for bad_name in (123, {"text": "Alpha"}, None, ["Alpha"]):
            value = self.parse(json.dumps(
                {"project": {"id": "11111111-1111-1111-1111-111111111111",
                             "name": bad_name}, "status": "associated"}))
            self.assertFalse(value["ok"], "accepted name: %r" % (bad_name,))
        # Empty-string name stays legitimate (UI falls back to the id).
        fallback = self.parse(json.dumps(
            {"project": {"id": "11111111-1111-1111-1111-111111111111", "name": ""},
             "status": "associated"}))
        self.assertTrue(fallback["ok"])
        self.assertTrue(fallback["hasProject"])
        self.assertEqual(fallback["name"], "")


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class CurrentProjectFinishTests(unittest.TestCase):
    FUNCTIONS = ["parseCurrentProject", "finishCurrentProject",
                 "handleCurrentProjectExited",
                 "handleCurrentProjectRunningChanged",
                 "targetProjectId", "handleCurrentProjectTimeout",
                 "fireCurrentProjectKillTimeout", "refreshCurrentProject"]

    def finish(self, setup, code, payload, generation):
        functions = [extract_function(MODULE, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, setup, f"""
const applied = context.finishCurrentProject({code}, {json.dumps(payload)}, {generation});
console.log(JSON.stringify({{ applied, backendOk: context.backendOk,
  hasProject: context.hasProject, projectId: context.projectId,
  projectName: context.projectName, projectStatus: context.projectStatus,
  launched: context._launched, launchGeneration: context.launchGeneration,
  watchdogStopped: context.projectWatchdog.stopped }}));
""")
        return run_node(script)

    VALID = json.dumps({
        "project": {"id": "11111111-1111-1111-1111-111111111111",
                    "name": "Alpha", "matched_by": "cwd"},
        "status": "associated",
    })

    IN_FLIGHT = ("context.currentGeneration = 3; context.launchGeneration = 3; "
                 "context._launched = true; context._started = true; "
                 "context.retiring = false;")

    STALE_IDENTITY = ("context.backendOk = true; context.hasProject = true; "
                      "context.projectId = '11111111-1111-1111-1111-111111111111'; "
                      "context.projectName = 'Alpha'; context.projectStatus = 'associated';")

    def test_valid_payload_applies_identity(self):
        value = self.finish(self.IN_FLIGHT, 0, self.VALID, 3)
        self.assertTrue(value["applied"])
        self.assertTrue(value["backendOk"] and value["hasProject"])
        self.assertEqual(value["projectId"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(value["projectName"], "Alpha")
        self.assertEqual(value["projectStatus"], "associated")
        self.assertTrue(value["watchdogStopped"])

    def test_malformed_payload_clears_stale_identity(self):
        value = self.finish(self.IN_FLIGHT + self.STALE_IDENTITY, 0, "garbage{{{", 3)
        self.assertFalse(value["applied"])
        self.assertFalse(value["backendOk"])
        self.assertFalse(value["hasProject"])
        self.assertEqual(value["projectId"], "")
        self.assertEqual(value["projectName"], "")
        self.assertEqual(value["projectStatus"], "")

    def test_nonzero_exit_clears_stale_identity(self):
        payload = json.dumps({"project": None, "status": "unassociated"})
        value = self.finish(self.IN_FLIGHT + self.STALE_IDENTITY, 1, payload, 3)
        self.assertFalse(value["applied"])
        self.assertFalse(value["backendOk"])
        self.assertEqual(value["projectId"], "")

    def test_foreign_generation_is_ignored(self):
        # A completion for a generation that is not the frozen in-flight
        # launch must drop, even with a valid payload.
        payload = self.VALID
        value = self.finish(self.IN_FLIGHT + self.STALE_IDENTITY, 0, payload, 2)
        self.assertFalse(value["applied"])
        self.assertTrue(value["backendOk"] and value["hasProject"])
        self.assertEqual(value["projectId"], "11111111-1111-1111-1111-111111111111")

    def test_exit_wrapper_applies_fresh_success(self):
        functions = [extract_function(MODULE, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, self.IN_FLIGHT, f"""
const applied = context.handleCurrentProjectExited(0, {json.dumps(self.VALID)});
console.log(JSON.stringify({{ applied, backendOk: context.backendOk,
  hasProject: context.hasProject, projectId: context.projectId,
  launched: context._launched, launchGeneration: context.launchGeneration,
  retiring: context.retiring,
  watchdogStopped: context.projectWatchdog.stopped,
  killStopped: context.projectKillTimer.stopped }}));
""")
        value = run_node(script)
        self.assertTrue(value["applied"])
        self.assertTrue(value["backendOk"] and value["hasProject"])
        self.assertEqual(value["projectId"], "11111111-1111-1111-1111-111111111111")
        self.assertFalse(value["launched"])
        self.assertEqual(value["launchGeneration"], -1)
        self.assertFalse(value["retiring"])
        self.assertTrue(value["watchdogStopped"] and value["killStopped"])

    def test_late_success_after_timeout_is_dropped_by_wrapper(self):
        # Timeout froze launchGeneration and cleared the identity; the late
        # successful exit for that same launch must NOT relabel fresh.
        functions = [extract_function(MODULE, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, (
            "context.currentGeneration = 4; context.launchGeneration = 4; "
            "context._launched = true; context._started = true; "
            "context.retiring = false; "
            "context.currentProjectProcess.running = true;"
        ), f"""
const timedOut = context.handleCurrentProjectTimeout();
const frozen = context.launchGeneration;
const late = context.handleCurrentProjectExited(0, {json.dumps(self.VALID)});
console.log(JSON.stringify({{ timedOut, frozen, late,
  backendOk: context.backendOk, hasProject: context.hasProject,
  projectId: context.projectId, launched: context._launched,
  launchGeneration: context.launchGeneration, retiring: context.retiring }}));
""")
        value = run_node(script)
        self.assertTrue(value["timedOut"])
        # Timeout keeps the frozen launch so the late exit is recognized...
        self.assertEqual(value["frozen"], 4)
        # ...and dropped, never relabeled fresh.
        self.assertFalse(value["late"])
        self.assertFalse(value["backendOk"] or value["hasProject"])
        self.assertEqual(value["projectId"], "")
        self.assertFalse(value["launched"])
        self.assertEqual(value["launchGeneration"], -1)
        self.assertFalse(value["retiring"])

    def test_async_termination_blocks_overlap_and_escalates(self):
        # The running setter does NOT immediately stop a live process:
        # SIGTERM is recorded but the helper stays alive; the poller must
        # refuse to overlap while retiring, the kill timer must escalate
        # with signal(9), and only the actual exit ends retiring.
        functions = [extract_function(MODULE, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, (
            "context.currentGeneration = 4; context.launchGeneration = 4; "
            "context._launched = true; context._started = true; "
            "context.retiring = false; context.backendOk = true; "
            "let live = true; context.runningWrites = []; "
            "Object.defineProperty(context.currentProjectProcess, 'running', {"
            "  get() { return live; },"
            "  set(v) { context.runningWrites.push(v); if (v === true) live = true; },"
            "  configurable: true });"
            "context.currentProjectProcess.signal = (sig) => {"
            "  context.currentProjectProcess.signaled.push(sig);"
            "  if (sig === 9) live = false; return true; };"
        ), f"""
const relaunched = {{}};
context.handleCurrentProjectTimeout();
relaunched.duringRetire = context.refreshCurrentProject();
const escalated = context.fireCurrentProjectKillTimeout();
relaunched.afterKillBeforeExit = context.refreshCurrentProject();
// The actual exit finally arrives (waitForEnd output captured, exit 0
// with a valid payload) -- still dropped, but retiring ends.
const late = context.handleCurrentProjectExited(0, {json.dumps(self.VALID)});
relaunched.afterExit = context.refreshCurrentProject();
console.log(JSON.stringify({{ sigtermWritten: context.runningWrites.indexOf(false) !== -1,
  stillLiveAfterTerm: live, escalated,
  signaled: context.currentProjectProcess.signaled,
  duringRetire: relaunched.duringRetire,
  afterKillBeforeExit: relaunched.afterKillBeforeExit, late,
  retiring: context.retiring, backendOk: context.backendOk,
  hasProject: context.hasProject, projectId: context.projectId,
  relaunchedAfterExit: relaunched.afterExit,
  launchGeneration: context.launchGeneration }}));
""")
        value = run_node(script)
        self.assertTrue(value["sigtermWritten"])
        self.assertTrue(value["stillLiveAfterTerm"])
        self.assertTrue(value["escalated"])
        self.assertIn(9, value["signaled"])
        self.assertFalse(value["duringRetire"])
        self.assertFalse(value["afterKillBeforeExit"])
        self.assertFalse(value["late"])
        self.assertFalse(value["retiring"])
        self.assertFalse(value["backendOk"] or value["hasProject"])
        self.assertEqual(value["projectId"], "")
        self.assertTrue(value["relaunchedAfterExit"])
        self.assertEqual(value["launchGeneration"], 5)

    def test_failed_start_uses_frozen_launch_and_clears(self):
        functions = [extract_function(MODULE, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, (
            "context.currentGeneration = 6; context.launchGeneration = 6; "
            "context._launched = true; context._started = false; "
            "context.retiring = false; " + self.STALE_IDENTITY +
            "context.currentProjectProcess.running = false;"
        ), """
const reconciled = context.handleCurrentProjectRunningChanged();
console.log(JSON.stringify({ reconciled, backendOk: context.backendOk,
  hasProject: context.hasProject, projectId: context.projectId,
  launched: context._launched, launchGeneration: context.launchGeneration }));
""")
        value = run_node(script)
        self.assertFalse(value["reconciled"])
        self.assertFalse(value["backendOk"] or value["hasProject"])
        self.assertEqual(value["projectId"], "")
        self.assertFalse(value["launched"])
        self.assertEqual(value["launchGeneration"], -1)

    def test_running_changed_before_exit_blocks_relaunch(self):
        # running already reads false while _launched is still set (the
        # onExited has not arrived yet): the poller must not start a new
        # process in that window.
        functions = [extract_function(MODULE, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, (
            "context.currentGeneration = 6; context.launchGeneration = 6; "
            "context._launched = true; context._started = true; "
            "context.retiring = false; "
            "context.currentProjectProcess.running = false;"
        ), """
const attempted = context.refreshCurrentProject();
console.log(JSON.stringify({ attempted,
  generation: context.currentGeneration,
  launchGeneration: context.launchGeneration }));
""")
        value = run_node(script)
        self.assertFalse(value["attempted"])
        self.assertEqual(value["generation"], 6)
        self.assertEqual(value["launchGeneration"], 6)

    def test_timeout_clears_identity_and_arms_kill_timer(self):
        functions = [extract_function(MODULE, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, (
            "context.currentGeneration = 4; context.launchGeneration = 4; "
            "context._launched = true; context._started = true; "
            "context.retiring = false; context.backendOk = true; "
            "context.hasProject = true; "
            "context.projectId = '11111111-1111-1111-1111-111111111111'; "
            "context.projectName = 'Alpha'; context.projectStatus = 'associated'; "
            "context.currentProjectProcess.running = true;"
        ), """
const timedOut = context.handleCurrentProjectTimeout();
console.log(JSON.stringify({ timedOut, retiring: context.retiring,
  running: context.currentProjectProcess.running,
  killArmed: context.projectKillTimer.restarted,
  frozenLaunch: context.launchGeneration,
  backendOk: context.backendOk,
  hasProject: context.hasProject, projectId: context.projectId,
  launched: context._launched }));
""")
        value = run_node(script)
        self.assertTrue(value["timedOut"])
        self.assertTrue(value["retiring"])
        self.assertFalse(value["running"])
        self.assertTrue(value["killArmed"])
        # Frozen launch is kept so the late exit is recognized and dropped.
        self.assertEqual(value["frozenLaunch"], 4)
        self.assertTrue(value["launched"])
        self.assertFalse(value["backendOk"] or value["hasProject"])
        self.assertEqual(value["projectId"], "")

    def test_kill_escalation_is_idle_without_retiring_process(self):
        functions = [extract_function(MODULE, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, (
            "context.retiring = false; "
            "context.currentProjectProcess.running = false;"
        ), """
const fired = context.fireCurrentProjectKillTimeout();
console.log(JSON.stringify({ fired,
  signaled: context.currentProjectProcess.signaled }));
""")
        value = run_node(script)
        self.assertFalse(value["fired"])
        self.assertEqual(value["signaled"], [])


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class CurrentProjectNavigationTests(unittest.TestCase):
    FUNCTIONS = ["parseCurrentProject", "finishCurrentProject",
                 "targetProjectId", "openCurrentProject"]

    def navigate(self, setup):
        functions = [extract_function(MODULE, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, setup, """
const target = context.openCurrentProject();
console.log(JSON.stringify({ target, calls: context.calls || [] }));
""")
        return run_node(script)

    CAPTURE = """
context.calls = [];
context.projectPlanner = { openProject(id, action, message) {
  context.calls.push({ id, action, message }); return true; } };
"""

    def test_navigation_captures_current_stable_id(self):
        setup = (self.CAPTURE + "context.backendOk = true; context.hasProject = true; "
                 "context.projectId = '11111111-1111-1111-1111-111111111111'; "
                 "context.projectName = 'Alpha';")
        value = self.navigate(setup)
        self.assertEqual(value["target"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(len(value["calls"]), 1)
        self.assertEqual(value["calls"][0]["id"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(value["calls"][0]["action"], "")
        self.assertEqual(value["calls"][0]["message"], "")

    def test_no_project_opens_blank_list(self):
        setup = (self.CAPTURE + "context.backendOk = true; context.hasProject = false; "
                 "context.projectId = ''; context.projectStatus = 'unassociated';")
        value = self.navigate(setup)
        self.assertEqual(value["target"], "")
        self.assertEqual(value["calls"][0]["id"], "")

    def test_stale_cleared_state_never_reuses_stale_id(self):
        setup = (self.CAPTURE + "context.backendOk = false; context.hasProject = false; "
                 "context.projectId = ''; context.projectStatus = '';")
        value = self.navigate(setup)
        self.assertEqual(value["target"], "")
        self.assertEqual(value["calls"][0]["id"], "")


class CurrentProjectWiringTests(unittest.TestCase):
    def test_module_uses_authoritative_helper_over_list_form_argv(self):
        self.assertIn('Quickshell.shellPath("scripts/desktop_projects.py")', MODULE)
        self.assertIn('"current-project"', MODULE)
        self.assertIn('"python3"', MODULE)
        # No inferred window/planner identity.
        self.assertNotIn("selectedProject", MODULE)
        self.assertNotIn("activeWindow", MODULE.lower())

    def test_module_polls_and_watchdogs_with_overlap_guard(self):
        self.assertIn("interval: 3000", MODULE)
        self.assertIn("interval: 12000", MODULE)
        # Kill escalation grace timer (SIGTERM -> SIGKILL, owned process).
        self.assertIn("id: projectKillTimer", MODULE)
        self.assertIn("interval: 3000", MODULE)
        self.assertIn("signal(9)", MODULE)
        # Frozen in-flight generation + retiring window; the poller guards
        # on all three so runningChanged-before-exited cannot relaunch.
        self.assertIn("launchGeneration", MODULE)
        self.assertIn("retiring", MODULE)
        self.assertIn("currentProjectProcess.running || root.retiring || root._launched", MODULE)
        self.assertIn("waitForEnd: true", MODULE)
        self.assertIn("StdioCollector", MODULE)
        self.assertIn("onExited", MODULE)
        self.assertIn("onStarted", MODULE)
        self.assertIn("onRunningChanged", MODULE)
        self.assertIn("handleCurrentProjectExited", MODULE)
        self.assertIn("handleCurrentProjectRunningChanged", MODULE)
        self.assertIn("fireCurrentProjectKillTimeout", MODULE)
        self.assertIn("Component.onCompleted", MODULE)
        self.assertIn("refreshCurrentProject", MODULE)

    def test_exit_handler_uses_frozen_launch_not_current_state(self):
        exited = MODULE[MODULE.index("onExited: (code)"):MODULE.index("Timer {")]
        # The thin handler delegates with captured stdout only...
        self.assertIn("handleCurrentProjectExited(code", exited)
        self.assertIn("currentProjectOutput.text", exited)
        # ...and never re-reads the mutable generation at completion time.
        self.assertNotIn("currentGeneration", exited)

    def test_module_keeps_bounded_label_and_accessible_button(self):
        self.assertIn("140", MODULE)
        self.assertIn("80", MODULE)
        self.assertIn("maxLabelWidth", MODULE)
        self.assertIn("Math.min(implicitWidth, root.maxLabelWidth)", MODULE)
        self.assertIn("elide: Text.ElideRight", MODULE)
        self.assertIn("import QtQuick.Controls", MODULE)
        self.assertIn("focusPolicy: Qt.StrongFocus", MODULE)
        self.assertIn("Accessible.name", MODULE)
        self.assertIn("ToolTip.text", MODULE)
        self.assertIn("ToolTip.visible", MODULE)
        self.assertIn("hovered || activeFocus", MODULE)
        self.assertIn("No project", MODULE)
        self.assertIn("Project unavailable", MODULE)
        self.assertIn('openProject(target, "", "")', MODULE)
        # Compactness is a plain width threshold via Bar, never the
        # layout-measured tightSides (would feed back into sideReserve).
        # (Comments document the avoidance; no binding may read it.)
        self.assertNotIn("bar.tightSides", MODULE)
        self.assertNotIn("root.tightSides", MODULE)

    def test_bar_hosts_module_in_unified_tray_after_battery(self):
        self.assertIn("property var projectPlanner", BAR)
        self.assertIn("CurrentProjectModule", BAR)
        self.assertIn("id: currentProjectModule", BAR)
        self.assertIn("projectPlanner: bar.projectPlanner", BAR)
        battery = BAR.index("id: batteryModule")
        module = BAR.index("id: currentProjectModule")
        tray = BAR.index("id: trayFlick")
        self.assertLess(battery, module)
        self.assertLess(module, tray)
        module_slice = BAR[module:module + 600]
        self.assertIn("compact: bar.compactNetwork", module_slice)
        self.assertNotIn("tightSides", module_slice)

    def test_shell_binds_shared_planner_into_bar(self):
        self.assertIn("projectPlanner: projectPlanner", SHELL)
        bar = SHELL[SHELL.index("Bar {"):SHELL.index("Bar {") + 400]
        self.assertIn("projectPlanner: projectPlanner", bar)


if __name__ == "__main__":
    unittest.main()
