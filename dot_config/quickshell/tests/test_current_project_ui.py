import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
MODULE = (ROOT / "widgets" / "CurrentProjectModule.qml").read_text(encoding="utf-8")
SOURCE = (ROOT / "widgets" / "CurrentProjectSource.qml").read_text(encoding="utf-8")
BAR = (ROOT / "widgets" / "Bar.qml").read_text(encoding="utf-8")
SHELL = (ROOT / "shell.qml").read_text(encoding="utf-8")
AMBIENT_QML = (ROOT / "widgets" / "AmbientContext.qml").read_text(encoding="utf-8")
AMBIENT_JS = (ROOT / "widgets" / "AmbientContext.js").read_text(encoding="utf-8")
PALETTE = (ROOT / "widgets" / "CommandPalette.qml").read_text(encoding="utf-8")
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
  watchLive: false, consecutiveFailures: 0, watchStartedAt: 0,
  _watchStarted: false, _startRequested: false,
  compact: false, maxLabelWidth: 140,
  displayLabel: "", tooltipText: "",
  watchRestartTimer: {{ stopped: false, restarted: false, interval: 5000,
    stop() {{ this.stopped = true; }}, restart() {{ this.restarted = true; }} }},
  watchProcess: {{ running: false, command: [] }},
  Quickshell: {{ shellPath(value) {{ return "/repo/" + value; }} }},
  projectPlanner: null,
  agenda: null,
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
class CurrentProjectStreamTests(unittest.TestCase):
    # Stream machinery lives in the shell-level CurrentProjectSource (one
    # resident watcher per desktop); the per-bar module is a view.
    FUNCTIONS = ["parseCurrentProjectData", "parseCurrentProject",
                 "restartDelayMs", "handleCurrentProjectLine",
                 "startWatchProcess", "handleWatchDeath",
                 "handleWatchRunningChanged", "refreshLedgerInboxBadge"]

    def apply_line(self, setup, payload, generation):
        functions = [extract_function(SOURCE, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, setup, f"""
const applied = context.handleCurrentProjectLine({json.dumps(payload)}, {generation});
console.log(JSON.stringify({{ applied, backendOk: context.backendOk,
  hasProject: context.hasProject, projectId: context.projectId,
  projectName: context.projectName, projectStatus: context.projectStatus,
  failures: context.consecutiveFailures, live: context.watchLive }}));
""")
        return run_node(script)

    VALID = json.dumps({
        "type": "current-project",
        "project": {"id": "11111111-1111-1111-1111-111111111111",
                    "name": "Alpha", "matched_by": "cwd"},
        "status": "associated",
    })

    # Old-shape line without the type tag stays valid (extras allowed,
    # tag required only when present).
    BARE = json.dumps({
        "project": {"id": "11111111-1111-1111-1111-111111111111",
                    "name": "Alpha", "matched_by": "cwd"},
        "status": "associated",
    })

    LIVE = ("context.currentGeneration = 3; context.watchLive = true; "
            "context.consecutiveFailures = 0;")

    STALE_IDENTITY = ("context.backendOk = true; context.hasProject = true; "
                      "context.projectId = '11111111-1111-1111-1111-111111111111'; "
                      "context.projectName = 'Alpha'; context.projectStatus = 'associated';")

    def test_valid_line_applies_identity(self):
        value = self.apply_line(self.LIVE, self.VALID, 3)
        self.assertTrue(value["applied"])
        self.assertTrue(value["backendOk"] and value["hasProject"])
        self.assertEqual(value["projectId"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(value["projectName"], "Alpha")
        self.assertEqual(value["projectStatus"], "associated")
        self.assertTrue(value["live"])
        self.assertEqual(value["failures"], 0)

    def test_bare_line_without_type_tag_still_applies(self):
        value = self.apply_line(self.LIVE, self.BARE, 3)
        self.assertTrue(value["applied"])
        self.assertTrue(value["backendOk"] and value["hasProject"])
        self.assertEqual(value["projectId"], "11111111-1111-1111-1111-111111111111")

    def test_unassociated_null_project_applies_empty(self):
        payload = json.dumps({"type": "current-project",
                              "project": None, "status": "unassociated"})
        value = self.apply_line(self.LIVE, payload, 3)
        self.assertTrue(value["applied"])
        self.assertTrue(value["backendOk"])
        self.assertFalse(value["hasProject"])
        self.assertEqual(value["projectId"], "")
        self.assertEqual(value["projectStatus"], "unassociated")

    def test_wrong_type_tag_clears_stale_identity(self):
        payload = json.dumps({"type": "ambient",
                              "project": None, "status": "unassociated"})
        value = self.apply_line(self.LIVE + self.STALE_IDENTITY, payload, 3)
        self.assertFalse(value["applied"])
        self.assertFalse(value["backendOk"])
        self.assertFalse(value["hasProject"])
        self.assertEqual(value["projectId"], "")
        self.assertEqual(value["projectName"], "")
        self.assertEqual(value["projectStatus"], "")

    def test_malformed_line_clears_stale_identity(self):
        value = self.apply_line(self.LIVE + self.STALE_IDENTITY, "garbage{{{", 3)
        self.assertFalse(value["applied"])
        self.assertFalse(value["backendOk"])
        self.assertFalse(value["hasProject"])
        self.assertEqual(value["projectId"], "")
        self.assertEqual(value["projectName"], "")
        self.assertEqual(value["projectStatus"], "")

    def test_unknown_status_and_bad_identity_are_rejected(self):
        weird = self.apply_line(
            self.LIVE + self.STALE_IDENTITY,
            json.dumps({"type": "current-project",
                        "project": {"id": "x", "name": "X"},
                        "status": "mystery"}), 3)
        self.assertFalse(weird["applied"])
        self.assertFalse(weird["backendOk"])
        self.assertEqual(weird["projectId"], "")
        # A non-null project alongside an empty status must not validate.
        mismatch = self.apply_line(
            self.LIVE + self.STALE_IDENTITY,
            json.dumps({"type": "current-project",
                        "project": {"id": "33333333-3333-3333-3333-333333333333",
                                    "name": "Z"},
                        "status": "unassociated"}), 3)
        self.assertFalse(mismatch["applied"])
        self.assertFalse(mismatch["backendOk"])
        # Non-string ids must not coerce into identities.
        coerced = self.apply_line(
            self.LIVE + self.STALE_IDENTITY,
            json.dumps({"type": "current-project",
                        "project": {"id": 12345, "name": "Alpha"},
                        "status": "associated"}), 3)
        self.assertFalse(coerced["applied"])
        self.assertFalse(coerced["backendOk"])

    def test_blank_line_is_ignored_without_touching_state(self):
        for blank in ("", "   ", "\n"):
            value = self.apply_line(self.LIVE + self.STALE_IDENTITY, blank, 3)
            self.assertFalse(value["applied"], "applied blank: %r" % (blank,))
            self.assertTrue(value["backendOk"] and value["hasProject"])
            self.assertEqual(value["projectId"], "11111111-1111-1111-1111-111111111111")
            self.assertEqual(value["projectName"], "Alpha")

    def test_foreign_generation_is_ignored(self):
        # A line for a generation that is not live must drop, even with a
        # valid payload (a dying child's late lines never relabel fresh).
        value = self.apply_line(self.LIVE + self.STALE_IDENTITY, self.VALID, 2)
        self.assertFalse(value["applied"])
        self.assertTrue(value["backendOk"] and value["hasProject"])
        self.assertEqual(value["projectId"], "11111111-1111-1111-1111-111111111111")

    def test_line_after_death_is_dropped(self):
        # The stream is over (watchLive cleared by the death): even a
        # live-generation line must not relabel the cleared state.
        setup = ("context.currentGeneration = 3; context.watchLive = false; "
                 "context.consecutiveFailures = 1; "
                 "context.backendOk = false; context.hasProject = false; "
                 "context.projectId = ''; context.projectName = ''; "
                 "context.projectStatus = '';")
        value = self.apply_line(setup, self.VALID, 3)
        self.assertFalse(value["applied"])
        self.assertFalse(value["backendOk"] or value["hasProject"])
        self.assertEqual(value["projectId"], "")

    def test_valid_line_does_not_reset_death_count(self):
        # Sustained-liveness rule: a single valid line never resets the
        # streak (an emit-then-crash child emits one per generation);
        # only a death after >= 30 s of liveness does.
        setup = ("context.currentGeneration = 3; context.watchLive = true; "
                 "context.consecutiveFailures = 2; context.watchStartedAt = 1000000;")
        value = self.apply_line(setup, self.VALID, 3)
        self.assertTrue(value["applied"])
        self.assertEqual(value["failures"], 2)

    def test_emit_then_crash_cycles_escalate_to_cap(self):
        # Each generation emits its startup line then exits after ~1 s:
        # the streak must escalate 5 s -> 15 s -> 60 s cap, never reset.
        functions = [extract_function(SOURCE, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, "", """
const line = %s;
const intervals = [];
let failures = [];
for (let cycle = 0; cycle < 4; ++cycle) {
  context.watchProcess.running = false;
  context.startWatchProcess();
  // Stage "child started at t", then its startup line, then a quick death.
  const t = 1000000 + cycle * 100000;
  context.watchStartedAt = t;
  const applied = context.handleCurrentProjectLine(line, context.currentGeneration);
  if (!applied) throw new Error("cycle " + cycle + " startup line dropped");
  context.handleWatchDeath(t + 1000);
  failures.push(context.consecutiveFailures);
  intervals.push(context.watchRestartTimer.interval);
  context.watchRestartTimer = { stopped: false, restarted: false, interval: 0,
    stop() { this.stopped = true; }, restart() { this.restarted = true; } };
}
console.log(JSON.stringify({ failures, intervals }));
""" % json.dumps(self.VALID))
        value = run_node(script)
        self.assertEqual(value["failures"], [1, 2, 3, 4])
        self.assertEqual(value["intervals"], [5000, 15000, 60000, 60000])

    def test_sustained_liveness_resets_the_streak(self):
        # A generation that survived >= 30 s proves the stream healthy:
        # its death starts a fresh streak (back to the 5 s first delay).
        functions = [extract_function(SOURCE, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, (
            "context.currentGeneration = 4; context.watchLive = true; "
            "context.consecutiveFailures = 2; "
            "context.watchStartedAt = 1000000; "
            "context._startRequested = true; context._watchStarted = true; "
            "context.backendOk = true; context.hasProject = true; "
            "context.projectId = '11111111-1111-1111-1111-111111111111'; "
            "context.projectName = 'Alpha'; context.projectStatus = 'associated';"
        ), """
context.handleWatchDeath(1000000 + 60000);
console.log(JSON.stringify({ failures: context.consecutiveFailures,
  interval: context.watchRestartTimer.interval,
  backendOk: context.backendOk, live: context.watchLive }));
""")
        value = run_node(script)
        self.assertEqual(value["failures"], 1)
        self.assertEqual(value["interval"], 5000)
        self.assertFalse(value["backendOk"])
        self.assertFalse(value["live"])

    def test_death_clears_identity_and_arms_backoff(self):
        functions = [extract_function(SOURCE, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, (
            "context.currentGeneration = 4; context.watchLive = true; "
            "context.consecutiveFailures = 0; "
            "context.watchStartedAt = Date.now(); "
            "context._startRequested = true; context._watchStarted = true; "
            "context.backendOk = true; context.hasProject = true; "
            "context.projectId = '11111111-1111-1111-1111-111111111111'; "
            "context.projectName = 'Alpha'; context.projectStatus = 'associated';"
        ), """
const first = context.handleWatchDeath();
console.log(JSON.stringify({ first, live: context.watchLive,
  restartArmed: context.watchRestartTimer.restarted,
  interval: context.watchRestartTimer.interval,
  failures: context.consecutiveFailures,
  backendOk: context.backendOk,
  hasProject: context.hasProject, projectId: context.projectId,
  startRequested: context._startRequested }));
""")
        value = run_node(script)
        self.assertTrue(value["first"])
        self.assertFalse(value["live"])
        self.assertTrue(value["restartArmed"])
        # First death backs off 5 s.
        self.assertEqual(value["interval"], 5000)
        self.assertEqual(value["failures"], 1)
        self.assertFalse(value["backendOk"] or value["hasProject"])
        self.assertEqual(value["projectId"], "")
        self.assertFalse(value["startRequested"])

    def test_death_backoff_progresses_to_cap(self):
        functions = [extract_function(SOURCE, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, "", """
const delays = [];
for (const start of [0, 1, 2, 9]) {
  context.currentGeneration = 3; context.watchLive = true;
  context.consecutiveFailures = start;
  context.watchStartedAt = Date.now();
  context._startRequested = true;
  context.watchRestartTimer = { stopped: false, restarted: false, interval: 0,
    stop() { this.stopped = true; }, restart() { this.restarted = true; } };
  context.handleWatchDeath();
  delays.push(context.watchRestartTimer.interval);
}
const direct = [context.restartDelayMs(0), context.restartDelayMs(1),
  context.restartDelayMs(2), context.restartDelayMs(3),
  context.restartDelayMs(10)];
console.log(JSON.stringify({ delays, direct }));
""")
        value = run_node(script)
        # 5 s -> 15 s -> 60 s cap.
        self.assertEqual(value["delays"], [5000, 15000, 60000, 60000])
        self.assertEqual(value["direct"], [5000, 5000, 15000, 60000, 60000])

    def test_death_is_idempotent(self):
        # A second death report without an intervening (re)start changes
        # nothing: no double-count, no re-armed timer.
        functions = [extract_function(SOURCE, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, (
            "context.currentGeneration = 4; context.watchLive = false; "
            "context.consecutiveFailures = 1; "
            "context.watchRestartTimer.interval = 7777;"
        ), """
const second = context.handleWatchDeath();
console.log(JSON.stringify({ second,
  failures: context.consecutiveFailures,
  interval: context.watchRestartTimer.interval,
  restarted: context.watchRestartTimer.restarted }));
""")
        value = run_node(script)
        self.assertFalse(value["second"])
        self.assertEqual(value["failures"], 1)
        self.assertEqual(value["interval"], 7777)
        self.assertFalse(value["restarted"])

    def test_failed_start_reconciles_as_death(self):
        functions = [extract_function(SOURCE, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, (
            "context.currentGeneration = 2; context.watchLive = true; "
            "context.consecutiveFailures = 0; "
            "context.watchStartedAt = Date.now(); "
            "context._startRequested = true; context._watchStarted = false; "
            "context.watchProcess.running = false; " + self.STALE_IDENTITY
        ), """
const reconciled = context.handleWatchRunningChanged();
console.log(JSON.stringify({ reconciled, backendOk: context.backendOk,
  hasProject: context.hasProject, projectId: context.projectId,
  live: context.watchLive, failures: context.consecutiveFailures,
  armed: context.watchRestartTimer.restarted,
  interval: context.watchRestartTimer.interval }));
""")
        value = run_node(script)
        self.assertTrue(value["reconciled"])
        self.assertFalse(value["backendOk"] or value["hasProject"])
        self.assertEqual(value["projectId"], "")
        self.assertFalse(value["live"])
        self.assertEqual(value["failures"], 1)
        self.assertTrue(value["armed"])
        self.assertEqual(value["interval"], 5000)

    def test_running_changed_ignored_when_running_or_started(self):
        functions = [extract_function(SOURCE, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, (
            "context._startRequested = true; context._watchStarted = false; "
            "context.watchLive = true; context.consecutiveFailures = 0; "
            "context.watchProcess.running = true;"
        ), """
const whileRunning = context.handleWatchRunningChanged();
context.watchProcess.running = false;
context._watchStarted = true;
const afterStarted = context.handleWatchRunningChanged();
context._watchStarted = false;
context._startRequested = false;
const idle = context.handleWatchRunningChanged();
console.log(JSON.stringify({ whileRunning, afterStarted, idle,
  live: context.watchLive, failures: context.consecutiveFailures,
  armed: context.watchRestartTimer.restarted }));
""")
        value = run_node(script)
        # Running child, started-then-stopped child (onExited owns it),
        # and an idle change with no start requested all stay silent.
        self.assertFalse(value["whileRunning"])
        self.assertFalse(value["afterStarted"])
        self.assertFalse(value["idle"])
        self.assertTrue(value["live"])
        self.assertEqual(value["failures"], 0)
        self.assertFalse(value["armed"])

    def test_start_never_overlaps_a_running_child(self):
        functions = [extract_function(SOURCE, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, (
            "context.currentGeneration = 6; context.watchLive = true; "
            "context.watchProcess.running = true;"
        ), """
const attempted = context.startWatchProcess();
console.log(JSON.stringify({ attempted,
  generation: context.currentGeneration, live: context.watchLive }));
""")
        value = run_node(script)
        self.assertFalse(value["attempted"])
        self.assertEqual(value["generation"], 6)
        self.assertTrue(value["live"])

    def test_start_launches_single_watch_bridge(self):
        functions = [extract_function(SOURCE, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, (
            "context.currentGeneration = 0; context.watchLive = false; "
            "context.consecutiveFailures = 0; "
            "context.watchProcess.running = false;"
        ), """
const ok = context.startWatchProcess();
console.log(JSON.stringify({ ok,
  generation: context.currentGeneration, live: context.watchLive,
  command: context.watchProcess.command,
  running: context.watchProcess.running,
  timerStopped: context.watchRestartTimer.stopped,
  startRequested: context._startRequested,
  started: context._watchStarted }));
""")
        value = run_node(script)
        self.assertTrue(value["ok"])
        self.assertEqual(value["generation"], 1)
        self.assertTrue(value["live"])
        self.assertEqual(value["command"],
                         ["python3", "-u", "/repo/scripts/desktop_projects.py", "watch"])
        self.assertTrue(value["running"])
        self.assertTrue(value["timerStopped"])
        self.assertTrue(value["startRequested"])
        self.assertFalse(value["started"])

    def test_restart_after_death_bumps_generation_and_keeps_count(self):
        # The death count survives the restart (only a death after >= 30 s
        # of liveness resets the streak); the generation bump stales
        # pre-restart lines.
        functions = [extract_function(SOURCE, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, (
            "context.currentGeneration = 4; context.watchLive = false; "
            "context.consecutiveFailures = 2; "
            "context.watchProcess.running = false;"
        ), """
const ok = context.startWatchProcess();
console.log(JSON.stringify({ ok,
  generation: context.currentGeneration, live: context.watchLive,
  failures: context.consecutiveFailures }));
""")
        value = run_node(script)
        self.assertTrue(value["ok"])
        self.assertEqual(value["generation"], 5)
        self.assertTrue(value["live"])
        self.assertEqual(value["failures"], 2)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class CurrentProjectNavigationTests(unittest.TestCase):
    # Navigation stays in the per-bar view module.
    FUNCTIONS = ["parseCurrentProject",
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
    def test_source_spawns_single_resident_watch_bridge(self):
        self.assertIn('Quickshell.shellPath("scripts/desktop_projects.py")', SOURCE)
        # The single resident child is the streaming watcher (unbuffered).
        self.assertIn('"watch"', SOURCE)
        self.assertIn('"-u"', SOURCE)
        self.assertIn('"python3"', SOURCE)
        self.assertEqual(SOURCE.count("Process {"), 1)
        # No per-poll one-shot fork remains; "current-project" survives
        # only as the stream line type tag, never as a spawn argv.
        self.assertNotIn('"current-project"]', SOURCE)
        # No inferred window/planner identity.
        self.assertNotIn("selectedProject", SOURCE)
        self.assertNotIn("activeWindow", SOURCE.lower())
        # The view module runs no backend of its own (only a doc comment may
        # name the helper).
        self.assertNotIn('"watch"', MODULE)
        self.assertNotIn("shellPath", MODULE)
        self.assertNotIn("Process {", MODULE)

    def test_source_streams_with_backoff_restart_and_generation_guard(self):
        # Resident line stream: exactly one Process feeding a SplitParser.
        self.assertIn("SplitParser", SOURCE)
        self.assertIn('splitMarker: "\\n"', SOURCE)
        self.assertIn("onRead", SOURCE)
        self.assertIn("handleCurrentProjectLine", SOURCE)
        self.assertIn("watchLive", SOURCE)
        self.assertIn("currentGeneration", SOURCE)
        # Bounded-backoff restart, not a poll/watchdog/kill ladder.
        self.assertIn("id: watchRestartTimer", SOURCE)
        self.assertIn('objectName: "watchRestartTimer"', SOURCE)
        self.assertIn("restartDelayMs", SOURCE)
        self.assertIn("consecutiveFailures", SOURCE)
        self.assertIn("watchStartedAt", SOURCE)
        self.assertIn("handleWatchDeath", SOURCE)
        self.assertIn("handleWatchRunningChanged", SOURCE)
        self.assertIn("startWatchProcess", SOURCE)
        self.assertNotIn("projectPoll", SOURCE)
        self.assertNotIn("projectWatchdog", SOURCE)
        self.assertNotIn("projectKillTimer", SOURCE)
        self.assertNotIn("interval: 3000", SOURCE)
        self.assertNotIn("interval: 12000", SOURCE)
        self.assertNotIn("signal(9)", SOURCE)
        self.assertNotIn("launchGeneration", SOURCE)
        self.assertNotIn("retiring", SOURCE)
        self.assertNotIn("currentProjectProcess", SOURCE)
        self.assertNotIn("currentProjectOutput", SOURCE)
        self.assertIn("onExited", SOURCE)
        self.assertIn("onStarted", SOURCE)
        self.assertIn("onRunningChanged", SOURCE)
        self.assertIn("Component.onCompleted", SOURCE)
        completed = SOURCE[SOURCE.index("Component.onCompleted"):]
        self.assertIn("startWatchProcess", completed)
        # Slower ledger-badge cadence lives alongside the resident stream.
        self.assertIn("interval: 60000", SOURCE)
        self.assertIn("id: ledgerBadgePoll", SOURCE)
        # The per-bar view runs nothing.
        self.assertNotIn("Timer {", MODULE)
        self.assertNotIn("Process {", MODULE)
        self.assertNotIn("currentGeneration", MODULE)
        self.assertNotIn("startWatchProcess", MODULE)
        self.assertIn("property var projectSource", MODULE)

    def test_stream_handoff_uses_live_generation_and_thin_exit(self):
        reader = SOURCE[SOURCE.index("stdout: SplitParser"):SOURCE.index("stderr:")]
        # Each line is handled with the live generation attached...
        self.assertIn("handleCurrentProjectLine(data, root.currentGeneration)", reader)
        exited = SOURCE[SOURCE.index("onExited:"):SOURCE.index("Timer {")]
        # ...while any exit ends the stream unconditionally: no exit code
        # or captured output is inspected, the death path just runs.
        self.assertIn("handleWatchDeath()", exited)
        self.assertNotIn("code", exited)
        self.assertNotIn("currentGeneration", exited)
        # stderr is bounded (log-and-drop per line): no lifetime
        # accumulator over the resident child.
        stderr = SOURCE[SOURCE.index("stderr:"):SOURCE.index("onStarted:")]
        self.assertIn("SplitParser", stderr)
        self.assertNotIn("StdioCollector {", stderr)
        self.assertNotIn("waitForEnd", stderr)

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
        # Valid-project clicks open the compact overview popup anchored to
        # the tray button; the plain openProject path stays as the safe
        # fallback for no-project/backend-unavailable (never a stale id).
        self.assertIn("property var overviewPopup", MODULE)
        self.assertIn("shouldOpenOverview", MODULE)
        self.assertIn("activateCurrentProject", MODULE)
        self.assertIn("overviewPopup.openFor", MODULE)
        self.assertIn("onClicked: root.activateCurrentProject()", MODULE)
        # Compactness is a plain width threshold via Bar, never the
        # layout-measured tightSides (would feed back into sideReserve).
        # (Comments document the avoidance; no binding may read it.)
        self.assertNotIn("bar.tightSides", MODULE)
        self.assertNotIn("root.tightSides", MODULE)

    def test_bar_hosts_module_in_unified_tray_after_battery(self):
        self.assertIn("property var projectPlanner", BAR)
        self.assertIn("property var projectOverviewPopup", BAR)
        self.assertIn("property var projectSource", BAR)
        self.assertIn("CurrentProjectModule", BAR)
        self.assertIn("id: currentProjectModule", BAR)
        self.assertIn("projectPlanner: bar.projectPlanner", BAR)
        self.assertIn("overviewPopup: bar.projectOverviewPopup", BAR)
        self.assertIn("projectSource: bar.projectSource", BAR)
        battery = BAR.index("id: batteryModule")
        module = BAR.index("id: currentProjectModule")
        tray = BAR.index("id: trayFlick")
        self.assertLess(battery, module)
        self.assertLess(module, tray)
        module_slice = BAR[module:module + 700]
        self.assertIn("compact: bar.compactNetwork", module_slice)
        self.assertNotIn("tightSides", module_slice)

    def test_shell_binds_shared_planner_and_source_into_bar(self):
        # Unqualified same-name bindings resolve to the child's own null
        # property inside the per-screen delegate, so Bar goes through
        # the delegate-root qualified ref.
        self.assertIn(
            "readonly property var projectPlannerRef: projectPlanner", SHELL)
        self.assertIn(
            "projectPlanner: barWindow.projectPlannerRef", SHELL)
        self.assertIn("CurrentProjectSource {", SHELL)
        self.assertIn(
            "readonly property var projectSourceRef: currentProjectSource", SHELL)
        self.assertIn(
            "projectSource: barWindow.projectSourceRef", SHELL)
        bar = SHELL[SHELL.index("Bar {"):SHELL.index("Bar {") + 700]
        self.assertIn("projectPlanner: barWindow.projectPlannerRef", bar)
        self.assertIn("projectSource: barWindow.projectSourceRef", bar)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class LedgerInboxBadgeParseTests(unittest.TestCase):
    def check(self, expression, expected):
        fn = extract_function(MODULE, "parseLedgerInboxBadge")
        script = f"""
const fn = new Function("return (" + {json.dumps(fn)} + ")")();
console.log(JSON.stringify({{ result: fn({expression}) }}));
"""
        return run_node(script)["result"]

    def test_rejects_non_numbers_without_coercion(self):
        self.assertEqual(self.check("undefined", None), 0)
        self.assertEqual(self.check("null", None), 0)
        self.assertEqual(self.check('"5"', None), 0)
        self.assertEqual(self.check('"0"', None), 0)
        self.assertEqual(self.check("''", None), 0)
        self.assertEqual(self.check("true", None), 0)
        self.assertEqual(self.check("false", None), 0)
        self.assertEqual(self.check("NaN", None), 0)
        self.assertEqual(self.check("Infinity", None), 0)
        self.assertEqual(self.check("-Infinity", None), 0)
        self.assertEqual(self.check("{}", None), 0)
        self.assertEqual(self.check("[]", None), 0)
        self.assertEqual(self.check("-1", None), 0)
        self.assertEqual(self.check("-0.5", None), 0)

    def test_floors_floats(self):
        self.assertEqual(self.check("5.9", None), 5)
        self.assertEqual(self.check("0.9", None), 0)
        self.assertEqual(self.check("999.9", None), 999)

    def test_passes_valid_range(self):
        self.assertEqual(self.check("0", None), 0)
        self.assertEqual(self.check("1", None), 1)
        self.assertEqual(self.check("42", None), 42)
        self.assertEqual(self.check("999", None), 999)

    def test_caps_at_999(self):
        self.assertEqual(self.check("1000", None), 999)
        self.assertEqual(self.check("5000", None), 999)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class LedgerInboxBadgeRefreshTests(unittest.TestCase):
    # The guarded badge refresh stays on the view module (on-demand reads);
    # the steady cadence moved to the shell-level source's 60 s timer.
    FUNCTIONS = ["parseLedgerInboxBadge", "refreshLedgerInboxBadge"]

    def exercise_refresh(self, setup):
        functions = [extract_function(MODULE, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, setup, """
let result = null;
let threw = false;
try {
  result = context.refreshLedgerInboxBadge();
} catch (error) {
  threw = true;
}
console.log(JSON.stringify({ result, threw, calls: context.calls || 0 }));
""")
        return run_node(script)

    def test_false_with_no_agenda(self):
        value = self.exercise_refresh("context.agenda = null; context.calls = 0;")
        self.assertFalse(value["threw"])
        self.assertFalse(value["result"])
        self.assertEqual(value["calls"], 0)

    def test_false_when_agenda_lacks_method(self):
        value = self.exercise_refresh("context.agenda = {}; context.calls = 0;")
        self.assertFalse(value["threw"])
        self.assertFalse(value["result"])

    def test_false_and_no_throw_when_method_throws(self):
        value = self.exercise_refresh(
            "context.calls = 0; context.agenda = {"
            " refreshLedgerInboxBadge() { context.calls += 1; throw new Error('boom'); } };")
        self.assertFalse(value["threw"])
        self.assertFalse(value["result"])
        self.assertEqual(value["calls"], 1)

    def test_true_and_exactly_one_call_when_method_returns_true(self):
        value = self.exercise_refresh(
            "context.calls = 0; context.agenda = {"
            " refreshLedgerInboxBadge() { context.calls += 1; return true; } };")
        self.assertFalse(value["threw"])
        self.assertTrue(value["result"])
        self.assertEqual(value["calls"], 1)

    def test_false_when_agenda_method_returns_false(self):
        value = self.exercise_refresh(
            "context.calls = 0; context.agenda = {"
            " refreshLedgerInboxBadge() { context.calls += 1; return false; } };")
        self.assertFalse(value["threw"])
        self.assertFalse(value["result"])
        self.assertEqual(value["calls"], 1)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class LedgerInboxBadgePollWiringTests(unittest.TestCase):
    # The resident stream never piggybacks a badge read: (re)starts never
    # touch the agenda; the badge refreshes on the source's own 60 s timer.
    FUNCTIONS = ["parseCurrentProject", "startWatchProcess",
                 "handleCurrentProjectLine", "handleWatchDeath",
                 "handleWatchRunningChanged", "refreshLedgerInboxBadge"]

    def launch(self, setup):
        functions = [extract_function(SOURCE, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, setup, """
const ok = context.startWatchProcess();
console.log(JSON.stringify({ ok,
  calls: (context.agenda && typeof context.agenda.calls === "number") ? context.agenda.calls : (context.calls || 0),
  generation: context.currentGeneration, live: context.watchLive }));
""")
        return run_node(script)

    def test_successful_start_triggers_no_badge_refresh(self):
        value = self.launch(
            "context.agenda = { calls: 0,"
            " refreshLedgerInboxBadge() { this.calls += 1; return true; } };")
        self.assertTrue(value["ok"])
        self.assertEqual(value["calls"], 0)
        self.assertEqual(value["generation"], 1)
        self.assertTrue(value["live"])

    def test_start_succeeds_with_null_agenda(self):
        value = self.launch("context.agenda = null; context.calls = 0;")
        self.assertTrue(value["ok"])
        self.assertEqual(value["generation"], 1)
        self.assertTrue(value["live"])

    def test_start_succeeds_when_agenda_method_throws(self):
        value = self.launch(
            "context.calls = 0; context.agenda = {"
            " refreshLedgerInboxBadge() { throw new Error('boom'); } };")
        self.assertTrue(value["ok"])
        self.assertEqual(value["generation"], 1)
        self.assertTrue(value["live"])

    def test_blocked_start_stays_blocked(self):
        value = self.launch(
            "context.currentGeneration = 6; context.watchLive = true; "
            "context.watchProcess.running = true; "
            "context.agenda = { calls: 0,"
            " refreshLedgerInboxBadge() { this.calls += 1; return true; } };")
        self.assertFalse(value["ok"])
        self.assertEqual(value["calls"], 0)
        self.assertEqual(value["generation"], 6)
        self.assertTrue(value["live"])

    def test_badge_refresh_has_own_slower_cadence(self):
        self.assertIn("interval: 60000", SOURCE)
        self.assertIn("id: ledgerBadgePoll", SOURCE)
        self.assertIn("refreshLedgerInboxBadge", SOURCE)


class LedgerInboxBadgeStructureTests(unittest.TestCase):
    def test_badge_overlay_present_exactly_once(self):
        self.assertEqual(MODULE.count('objectName: "currentProjectLedgerBadge"'), 1)
        self.assertEqual(MODULE.count('objectName: "currentProjectLedgerBadgeText"'), 1)

    def test_badge_visible_gating_and_content(self):
        self.assertIn("visible: root.ledgerInboxBadge > 0", MODULE)
        self.assertIn("String(root.ledgerInboxBadge)", MODULE)
        self.assertIn("Math.max(14, ledgerBadgeText.implicitWidth + 6)", MODULE)
        self.assertIn("height: 14", MODULE)
        self.assertIn("radius: Theme.chipRadius", MODULE)
        self.assertIn("color: Theme.accent", MODULE)
        self.assertIn("color: Theme.base", MODULE)
        self.assertIn("font.pixelSize: 9", MODULE)
        # Overlay renders above and does not affect layout.
        badge = MODULE[MODULE.index('objectName: "currentProjectLedgerBadge"') - 400:
                       MODULE.index('objectName: "currentProjectLedgerBadge"') + 800]
        self.assertIn("z: 1", badge)

    def test_accessible_description_includes_count(self):
        self.assertIn("Accessible.description", MODULE)
        description = MODULE[MODULE.index("Accessible.description"):
                             MODULE.index("Accessible.description") + 400]
        self.assertIn("ledgerInboxBadge", description)
        self.assertIn("session needing attention", MODULE)
        self.assertIn("sessions needing attention", MODULE)
        self.assertIn("=== 1", description)
        # Existing accessibility and tooltip behavior stays intact.
        self.assertIn("Accessible.name", MODULE)
        self.assertIn("ToolTip.text: root.tooltipText", MODULE)

    def test_no_per_monitor_poller_in_view(self):
        self.assertEqual(MODULE.count("Timer {"), 0)
        self.assertNotIn("Process {", MODULE)
        self.assertNotIn("id: projectPoll", MODULE)
        self.assertNotIn("launchGeneration", MODULE)
        self.assertNotIn("currentProjectProcess", MODULE)
        self.assertIn("property var projectSource", MODULE)
        # The shell-level source owns the single resident stream + badge cadence.
        self.assertIn("SplitParser", SOURCE)
        self.assertIn("watchLive", SOURCE)
        self.assertNotIn("id: projectPoll", SOURCE)
        self.assertNotIn("interval: 3000", SOURCE)
        self.assertIn("id: ledgerBadgePoll", SOURCE)
        self.assertIn("interval: 60000", SOURCE)
        self.assertIn("property var agenda", MODULE)
        self.assertIn("property var agenda", SOURCE)
        self.assertIn("refreshLedgerInboxBadge", MODULE)

    def test_bar_forwards_shared_agenda(self):
        self.assertIn("property var agenda", BAR)
        self.assertIn("agenda: bar.agenda", BAR)

    def test_shell_passes_daily_agenda_into_bar(self):
        bar = SHELL[SHELL.index("Bar {"):SHELL.index("Bar {") + 900]
        self.assertIn("agenda: dailyAgenda", bar)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class AmbientLadderTests(unittest.TestCase):
    """Phase 1 §2.1: the shared AmbientContext ladder (existing commands only)."""
    FUNCTIONS = ["parseAmbientContext", "parseAmbientSession",
                 "parseAmbientActivity", "reassembleAmbient",
                 "ambientRefreshDay", "refreshAmbient", "drainAmbientQueue",
                 "cancelAmbientRefresh",
                 "handleAmbientExited", "handleAmbientRunningChanged",
                 "handleAmbientTimeout", "fireAmbientKillTimeout",
                 "finishAmbientPart"]

    def test_ladder_composes_existing_commands_only(self):
        # Exactly the three existing read-only commands; no new backend,
        # no schema, no model call.
        self.assertIn('"current-context"', AMBIENT_QML)
        self.assertIn('"current-session"', AMBIENT_QML)
        self.assertIn('"project-activity", "--limit", "1"', AMBIENT_QML)
        self.assertIn('scripts/desktop_projects.py', AMBIENT_QML)
        self.assertEqual(AMBIENT_QML.count('Quickshell.shellPath("scripts/desktop_projects.py")'), 3)
        self.assertNotIn("current-project", AMBIENT_QML)

    def test_bounded_watchdog_and_failsoft(self):
        # 12 s watchdog + 3 s kill escalation, mirroring the QML ladder
        # conventions; failures keep the last-known cache (fail-soft).
        self.assertIn("interval: 12000", AMBIENT_QML)
        self.assertIn("interval: 3000", AMBIENT_QML)
        self.assertIn("signal(9)", AMBIENT_QML)
        self.assertIn("waitForEnd: true", AMBIENT_QML)
        self.assertIn("StdioCollector", AMBIENT_QML)
        self.assertIn("last-known", AMBIENT_QML)
        # A nonzero exit drops the part without clearing the cache.
        self.assertIn("if (code !== 0) return false;", AMBIENT_QML)

    def test_never_blocks_send(self):
        # refreshAmbient is fire-and-forget; no send path waits for it.
        self.assertIn("fire-and-forget", AMBIENT_QML)
        # S-057: one shell-level resolver; both consumers bind via
        # ambientSource instead of owning a ladder.
        self.assertEqual(SHELL.count("AmbientContext {"), 1)
        self.assertEqual(PALETTE.count("AmbientContext {"), 0)
        self.assertEqual(PLANNER.count("AmbientContext {"), 0)
        self.assertIn("ambientSource: ambientContext", SHELL)
        self.assertIn("property var ambientSource", PALETTE)
        self.assertIn("property var ambientSource", PLANNER)
        # Palette ai: reads the cache synchronously, never launches.
        branch = PALETTE[PALETTE.index('if (root.mode === "ai")'):
                         PALETTE.index('if (root.mode === "ai")') + 400]
        self.assertIn("palettePromptWithAmbient", branch)
        self.assertNotIn("refreshAmbient", branch)
        # Planner send paths read the cache; only open() refreshes.
        self.assertIn("plannerAmbientForSend()", PLANNER)
        self.assertNotIn("refreshAmbient", PLANNER[PLANNER.index("function send()"):
                                                   PLANNER.index("function send()") + 300])
        # Journal stays day-only: no ladder instance, no block markers.
        self.assertNotIn("AmbientContext {", JOURNAL)
        self.assertNotIn("recent_resources", JOURNAL)
        self.assertIn("formatAmbientDay", JOURNAL)

    def test_day_is_local_date(self):
        self.assertIn("ambientDayKey", AMBIENT_QML)
        self.assertIn("interval: 60000", AMBIENT_QML)

    def exercise(self, setup, code):
        functions = [extract_function(AMBIENT_QML, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, setup, code)
        return run_node(script)

    def test_parse_context_associated_and_unassociated(self):
        functions = [extract_function(AMBIENT_QML, name) for name in
                     ("parseAmbientContext", "parseAmbientSession",
                      "parseAmbientActivity", "reassembleAmbient")]
        script = vm_harness(functions, "", """
context.parseAmbientContext(JSON.stringify({project: {id: "pid-1", name: "Alpha"}}));
const has = {id: context._ambientProjectId, name: context._ambientProjectName};
context.parseAmbientContext(JSON.stringify({project: null, status: "unassociated"}));
console.log(JSON.stringify({has,
  cleared: context._ambientProjectId === "" && context._ambientProjectName === ""}));
""")
        value = run_node(script)
        self.assertEqual(value["has"], {"id": "pid-1", "name": "Alpha"})
        self.assertTrue(value["cleared"])

    def test_parse_activity_bounds_resources(self):
        functions = [extract_function(AMBIENT_QML, name) for name in
                     ("parseAmbientContext", "parseAmbientSession",
                      "parseAmbientActivity", "reassembleAmbient")]
        script = f"""
const vm = require("vm");
const ambient = {json.dumps(AMBIENT_JS)};
const context = {{
  _ambientProjectId: "pid-1", _ambientProjectName: "Alpha",
  _ambientSessionId: "s1", _ambientSessionStartMs: 7,
  _ambientResources: [], ambientDay: "2026-09-21",
  ambientBlock: null, ambientError: "",
}};
context.root = context;
vm.createContext(context);
vm.runInContext(ambient, context);
context.Ambient = {{buildAmbientBlock: context.buildAmbientBlock,
  ambientResourcesFromActivity: context.ambientResourcesFromActivity}};
for (const value of {json.dumps(functions)}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
const resources = [];
for (let i = 0; i < 10; i++)
  resources.push({{kind: "file", matched_at_ms: i,
    resource: {{file: "/f" + i}}, portable_identity: "", local_identity: "", resource_key: ""}});
context.parseAmbientActivity(JSON.stringify({{sessions: [{{resources}}]}}));
context.reassembleAmbient();
console.log(JSON.stringify({{count: context.ambientBlock.recent_resources.length,
  day: context.ambientBlock.day, project: context.ambientBlock.project.id}}));
"""
        value = run_node(script)
        self.assertEqual(value["count"], 8)
        self.assertEqual(value["day"], "2026-09-21")
        self.assertEqual(value["project"], "pid-1")

    def test_timeout_retires_without_clearing_cache(self):
        functions = [extract_function(AMBIENT_QML, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, (
            "context.ambientBusy = true; context.ambientGeneration = 0; "
            "context.ambientBlock = {day: '2026-09-21'}; "
            "context.ambientWatchdog = {stop() {}}; "
            "context.ambientKillTimer = {restart() {}}; "
            "context.ambientContextProcess = {running: true}; "
            "context.ambientSessionProcess = {running: false}; "
            "context.ambientActivityProcess = {running: false};"
        ), """
const timedOut = context.handleAmbientTimeout();
console.log(JSON.stringify({ timedOut, retiring: context.ambientRetiring,
  busy: context.ambientBusy,
  kept: context.ambientBlock && context.ambientBlock.day === '2026-09-21' }));
""")
        value = run_node(script)
        self.assertTrue(value["timedOut"] and value["retiring"])
        self.assertFalse(value["busy"])
        self.assertTrue(value["kept"])

    def test_first_exit_keeps_watchdog_armed_while_siblings_run(self):
        # Watchdog regression: one fast exit plus hung siblings must not
        # disarm the 12 s watchdog (busy stays until the timeout clears
        # it); only the last exit stops the timers.
        functions = [extract_function(AMBIENT_QML, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, (
            "context.ambientBusy = true; context.ambientRetiring = false; "
            "context.ambientProcessGeneration = 7; "
            "context.ambientWatchdog = {stopped: false, restarted: false, "
            "  stop() { this.stopped = true; }, restart() { this.restarted = true; }}; "
            "context.ambientKillTimer = {stopped: false, "
            "  stop() { this.stopped = true; }, restart() {}}; "
            "context.ambientContextProcess = {running: false}; "
            "context.ambientSessionProcess = {running: true}; "
            "context.ambientActivityProcess = {running: true};"
        ), """
const applied = context.handleAmbientExited("context", 1, "", 7);
console.log(JSON.stringify({ applied, busy: context.ambientBusy,
  watchdogRestarted: context.ambientWatchdog.restarted,
  watchdogStopped: context.ambientWatchdog.stopped,
  killStopped: context.ambientKillTimer.stopped }));
""")
        value = run_node(script)
        self.assertFalse(value["applied"])
        self.assertTrue(value["busy"])
        self.assertTrue(value["watchdogRestarted"])
        self.assertFalse(value["watchdogStopped"])
        self.assertFalse(value["killStopped"])

    def test_last_exit_stops_timers_and_clears_busy(self):
        functions = [extract_function(AMBIENT_QML, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, (
            "context.ambientBusy = true; context.ambientRetiring = false; "
            "context.ambientProcessGeneration = 7; "
            "context.ambientWatchdog = {stopped: false, restart() {}}; "
            "context.ambientWatchdog.stop = function() { this.stopped = true; }; "
            "context.ambientKillTimer = {stopped: false, "
            "  stop() { this.stopped = true; }, restart() {}}; "
            "context.ambientContextProcess = {running: false}; "
            "context.ambientSessionProcess = {running: false}; "
            "context.ambientActivityProcess = {running: false};"
        ), """
const applied = context.handleAmbientExited("activity", 1, "", 7);
console.log(JSON.stringify({ applied, busy: context.ambientBusy,
  retiring: context.ambientRetiring,
  watchdogStopped: context.ambientWatchdog.stopped,
  killStopped: context.ambientKillTimer.stopped }));
""")
        value = run_node(script)
        self.assertFalse(value["busy"] or value["retiring"])
        self.assertTrue(value["watchdogStopped"] and value["killStopped"])

    def test_never_started_running_changed_clears_busy(self):
        # Spawn-failure recovery: runningChanged(false) with no onExited
        # and no onStarted degrades to day-only/fail-soft (busy cleared,
        # generation bumped) instead of refusing refreshes forever.
        functions = [extract_function(AMBIENT_QML, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, (
            "context.ambientBusy = true; context.ambientRetiring = false; "
            "context._ambientStarted = false; context.ambientGeneration = 3; "
            "context.ambientWatchdog = {stopped: false, stop() { this.stopped = true; }}; "
            "context.ambientKillTimer = {stopped: false, stop() { this.stopped = true; }}; "
            "context.ambientContextProcess = {running: false}; "
            "context.ambientSessionProcess = {running: false}; "
            "context.ambientActivityProcess = {running: false};"
        ), """
const recovered = context.handleAmbientRunningChanged();
console.log(JSON.stringify({ recovered, busy: context.ambientBusy,
  retiring: context.ambientRetiring, generation: context.ambientGeneration,
  watchdogStopped: context.ambientWatchdog.stopped,
  killStopped: context.ambientKillTimer.stopped }));
""")
        value = run_node(script)
        self.assertTrue(value["recovered"])
        self.assertFalse(value["busy"] or value["retiring"])
        self.assertEqual(value["generation"], 4)
        self.assertTrue(value["watchdogStopped"] and value["killStopped"])

    def test_running_changed_ignored_when_started_or_running(self):
        functions = [extract_function(AMBIENT_QML, name) for name in self.FUNCTIONS]
        script = vm_harness(functions, (
            "context.ambientWatchdog = {stop() {}}; "
            "context.ambientKillTimer = {stop() {}}; "
            "context.ambientContextProcess = {running: false}; "
            "context.ambientSessionProcess = {running: false}; "
            "context.ambientActivityProcess = {running: false};"
        ), """
context.ambientBusy = true; context._ambientStarted = true;
const startedIgnored = context.handleAmbientRunningChanged();
const busyAfterStarted = context.ambientBusy;
context._ambientStarted = false;
context.ambientSessionProcess = {running: true};
const runningIgnored = context.handleAmbientRunningChanged();
console.log(JSON.stringify({ startedIgnored, busyAfterStarted, runningIgnored,
  busy: context.ambientBusy }));
""")
        value = run_node(script)
        self.assertFalse(value["startedIgnored"])
        self.assertTrue(value["busyAfterStarted"])
        self.assertFalse(value["runningIgnored"])
        self.assertTrue(value["busy"])

    def test_pinned_refresh_scopes_activity_and_applies_identity(self):
        # Pinned selection: the block's project becomes the registry
        # entry synchronously, resources are fetched via
        # `project-activity --project <pinned>`, and stale resources
        # are dropped immediately (never misattributed).
        functions = [extract_function(AMBIENT_QML, name) for name in
                     ("ambientRefreshDay", "reassembleAmbient",
                      "parseAmbientContext", "parseAmbientActivity",
                      "refreshAmbient")]
        script = f"""
const vm = require("vm");
const ambient = {json.dumps(AMBIENT_JS)};
const context = {{
  ambientDay: "", _ambientProjectId: "", _ambientProjectName: "",
  _ambientSessionId: "s", _ambientSessionStartMs: 1,
  _ambientResources: [{{kind: "file", label: "stale", identity: "/stale", last_seen_ms: 1}}],
  _ambientPinnedId: "", _ambientPinnedName: "", _ambientResourcesProjectId: "",
  ambientBlock: null, ambientError: "", ambientGeneration: 0,
  ambientProcessGeneration: 0, ambientBusy: false, ambientRetiring: false,
  _ambientStarted: false,
  ambientWatchdog: {{restart() {{}}}},
  ambientContextProcess: {{running: false, command: []}},
  ambientSessionProcess: {{running: false, command: []}},
  ambientActivityProcess: {{running: false, command: []}},
  Quickshell: {{shellPath(value) {{ return "/repo/" + value; }}}},
}};
context.root = context;
vm.createContext(context);
vm.runInContext(ambient, context);
context.Ambient = {{ambientDayKey: context.ambientDayKey,
  buildAmbientBlock: context.buildAmbientBlock,
  ambientResourcesFromActivity: context.ambientResourcesFromActivity}};
for (const value of {json.dumps(functions)}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
const pin = "11111111-1111-1111-1111-111111111111";
const launched = context.refreshAmbient(pin, "Alpha");
const activityCmd = context.ambientActivityProcess.command;
const scoped = activityCmd.indexOf("--project") >= 0 && activityCmd.indexOf(pin) >= 0;
// Compositor-current identity must not overwrite the pin.
context.parseAmbientContext(JSON.stringify({{project: {{id: "other", name: "Other"}}}}));
console.log(JSON.stringify({{ launched,
  pinned: context._ambientPinnedId === pin,
  identity: context._ambientProjectId === pin && context._ambientProjectName === "Alpha",
  staleDropped: context._ambientResources.length === 0,
  scoped,
  keptPin: context._ambientProjectId === pin,
  projectInBlock: context.ambientBlock && context.ambientBlock.project &&
    context.ambientBlock.project.id === pin,
  noStaleInBlock: context.ambientBlock && context.ambientBlock.recent_resources.length === 0,
  sessionKept: context.ambientBlock && context.ambientBlock.session &&
    context.ambientBlock.session.id === "s" }}));
"""
        value = run_node(script)
        for key, flag in value.items():
            self.assertTrue(flag, key)

    def test_unresolvable_pin_omits_project_and_resources(self):
        functions = [extract_function(AMBIENT_QML, name) for name in
                     ("ambientRefreshDay", "reassembleAmbient", "refreshAmbient")]
        script = f"""
const vm = require("vm");
const ambient = {json.dumps(AMBIENT_JS)};
const context = {{
  ambientDay: "", _ambientProjectId: "old", _ambientProjectName: "Old",
  _ambientSessionId: "s", _ambientSessionStartMs: 1,
  _ambientResources: [{{kind: "file", label: "stale", identity: "/stale", last_seen_ms: 1}}],
  _ambientPinnedId: "", _ambientPinnedName: "", _ambientResourcesProjectId: "",
  ambientBlock: null, ambientError: "", ambientGeneration: 0,
  ambientProcessGeneration: 0, ambientBusy: false, ambientRetiring: false,
  _ambientStarted: false,
  ambientWatchdog: {{restart() {{}}}},
  ambientContextProcess: {{running: false, command: []}},
  ambientSessionProcess: {{running: false, command: []}},
  ambientActivityProcess: {{running: false, command: []}},
  Quickshell: {{shellPath(value) {{ return "/repo/" + value; }}}},
}};
context.root = context;
vm.createContext(context);
vm.runInContext(ambient, context);
context.Ambient = {{ambientDayKey: context.ambientDayKey,
  buildAmbientBlock: context.buildAmbientBlock,
  ambientResourcesFromActivity: context.ambientResourcesFromActivity}};
for (const value of {json.dumps(functions)}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
context.refreshAmbient("22222222-2222-2222-2222-222222222222", "");
console.log(JSON.stringify({{
  projectOmitted: context.ambientBlock && context.ambientBlock.project === null,
  resourcesOmitted: context.ambientBlock && context.ambientBlock.recent_resources.length === 0,
  sessionKept: context.ambientBlock && context.ambientBlock.session &&
    context.ambientBlock.session.id === "s",
  dayKept: context.ambientBlock && typeof context.ambientBlock.day === "string" }}));
"""
        value = run_node(script)
        for key, flag in value.items():
            self.assertTrue(flag, key)


    def test_shared_owner_no_boot_fork_and_scope_tag(self):
        # S-057: exactly one AmbientContext instantiation (shell-level),
        # no self-start fork at boot, day freshness without forks, and a
        # scope tag on the resolved block that consumers check.
        self.assertEqual(SHELL.count("AmbientContext {"), 1)
        self.assertNotIn("Component.onCompleted", AMBIENT_QML)
        self.assertIn("ambientDayPoll", AMBIENT_QML)
        self.assertIn("interval: 60000", AMBIENT_QML)
        self.assertIn("ambientScopeId", AMBIENT_QML)
        self.assertIn("drainAmbientQueue", AMBIENT_QML)
        self.assertIn("_ambientHasQueued", AMBIENT_QML)
        self.assertIn("ambientScopeId", PALETTE)
        self.assertIn("paletteSyncAmbient", PALETTE)
        self.assertIn('String(source.ambientScopeId || "")', PALETTE)
        self.assertIn("ambientScopeId", PLANNER)

    def test_queued_refresh_runs_latest_wins_on_drain(self):
        # Serialized ladder: a refresh while busy parks latest-wins and
        # runs it when the ladder drains; superseded requests are dropped
        # and the resolved block carries the latest scope.
        functions = [extract_function(AMBIENT_QML, name) for name in
                     ("ambientRefreshDay", "reassembleAmbient",
                      "refreshAmbient", "drainAmbientQueue")]
        script = f"""
const vm = require("vm");
const ambient = {json.dumps(AMBIENT_JS)};
const context = {{
  ambientDay: "", _ambientProjectId: "", _ambientProjectName: "",
  _ambientSessionId: "s", _ambientSessionStartMs: 1,
  _ambientResources: [], _ambientPinnedId: "", _ambientPinnedName: "",
  _ambientResourcesProjectId: "", ambientScopeId: "",
  ambientBlock: null, ambientError: "", ambientGeneration: 0,
  ambientProcessGeneration: 0, ambientBusy: false, ambientRetiring: false,
  _ambientStarted: false, _ambientQueuedPin: "", _ambientQueuedPinName: "",
  _ambientHasQueued: false,
  ambientWatchdog: {{restart() {{}}, stop() {{}}}},
  ambientKillTimer: {{stop() {{}}, restart() {{}}}},
  ambientContextProcess: {{running: false, command: []}},
  ambientSessionProcess: {{running: false, command: []}},
  ambientActivityProcess: {{running: false, command: []}},
  Quickshell: {{shellPath(value) {{ return "/repo/" + value; }}}},
}};
context.root = context;
vm.createContext(context);
vm.runInContext(ambient, context);
context.Ambient = {{ambientDayKey: context.ambientDayKey,
  buildAmbientBlock: context.buildAmbientBlock,
  ambientResourcesFromActivity: context.ambientResourcesFromActivity}};
for (const value of {json.dumps(functions)}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
const pinA = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
const pinB = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";
const first = context.refreshAmbient();
const busyQueuedA = context.refreshAmbient(pinA, "Alpha");
const busyQueuedB = context.refreshAmbient(pinB, "Beta");
context.ambientContextProcess.running = false;
context.ambientSessionProcess.running = false;
context.ambientActivityProcess.running = false;
context.ambientBusy = false;
context.ambientRetiring = false;
const drained = context.drainAmbientQueue();
const activityCmd = context.ambientActivityProcess.command;
console.log(JSON.stringify({{ first, busyQueuedA, busyQueuedB,
  queueCleared: context._ambientHasQueued === false,
  drained,
  scopedToLatest: activityCmd.indexOf("--project") >= 0 && activityCmd.indexOf(pinB) >= 0,
  droppedSuperseded: activityCmd.indexOf(pinA) < 0,
  scopeIsLatest: context.ambientScopeId === pinB }}));
"""
        value = run_node(script)
        self.assertTrue(value["first"])
        self.assertFalse(value["busyQueuedA"])
        self.assertFalse(value["busyQueuedB"])
        self.assertTrue(value["queueCleared"])
        self.assertTrue(value["drained"])
        self.assertTrue(value["scopedToLatest"])
        self.assertTrue(value["droppedSuperseded"])
        self.assertTrue(value["scopeIsLatest"])


if __name__ == "__main__":
    unittest.main()
