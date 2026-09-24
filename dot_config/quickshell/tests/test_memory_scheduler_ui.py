"""Workstream A scheduler UI: MemoryScheduler.qml structure + tick protocol.

Covers the plan §7A QML contract without a live Quickshell harness:
structural presence (one Process, repeating tick Timer, watchdog/kill
escalation, invisible Item by DailyAgenda), the status-bootstrap → tick
protocol with the shared `enabled` settings dictionary, the generation
guard (dataChanged only on changed === true), disabled-stops-timer, no
auto-tick error surfaces, and no direct settings.json parsing in QML.
"""
import sys as _sys
_sys.dont_write_bytecode = True

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
SCHEDULER = (REPO_ROOT / "widgets" / "MemoryScheduler.qml").read_text(encoding="utf-8")
SHELL = (REPO_ROOT / "shell.qml").read_text(encoding="utf-8")

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import quickshell_settings as settings


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


FUNCTIONS = ["parseSchedulerPayload", "finishScheduler",
             "scanNoticeFor", "finishScan",
             "bootstrapRetryDelayMs", "scheduleBootstrapRetry",
             "cancelBootstrapRetry", "retryBootstrapStatus",
             "handleSchedulerExited", "handleSchedulerRunningChanged",
             "launchScheduler", "requestStatus", "requestTick",
             "requestScan",
             "handleSchedulerTimeout", "fireSchedulerKillTimeout"]


def vm_harness(functions, setup, exercise):
    sources = json.dumps(functions)
    return f"""
const vm = require("vm");
const context = {{
  schedulerEnabled: false, tickSeconds: 60, bootstrapped: false,
  schedulerGeneration: 0, schedulerLaunchGeneration: -1,
  schedulerOp: "", schedulerLaunchOp: "",
  retiring: false, _launched: false, _started: false,
  bootstrapFailures: 0, scanNotice: "",
  dataChangedCalls: 0,
  dataChanged() {{ this.dataChangedCalls++; }},
  schedulerWatchdog: {{ stopped: false, restarted: false,
    stop() {{ this.stopped = true; }}, restart() {{ this.restarted = true; }} }},
  schedulerKillTimer: {{ stopped: false, restarted: false,
    stop() {{ this.stopped = true; }}, restart() {{ this.restarted = true; }} }},
  bootstrapRetryTimer: {{ interval: 10000, running: false,
    restartCount: 0, stopCount: 0,
    restart() {{ this.restartCount++; this.running = true; }},
    stop() {{ this.stopCount++; this.running = false; }} }},
  schedulerProcess: {{ running: false, command: [], signaled: [],
    signal(sig) {{ this.signaled.push(sig); return true; }} }},
  Quickshell: {{ shellPath(value) {{ return "/repo/" + value; }} }},
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


def run_harness(setup, exercise):
    functions = [extract_function(SCHEDULER, name) for name in FUNCTIONS]
    return run_node(vm_harness(functions, setup, exercise))


def status_payload(**overrides):
    enabled = {"enabled": True, "tickSeconds": 60,
               "workLog": True, "dailyReview": True,
               "jev": {"model": "typesafe/jev-1.13",
                       "maxCallsPerHour": 6, "maxCallsPerDay": 40}}
    enabled.update(overrides.get("enabled", {}))
    payload = {"available": True, "enabled": enabled,
               "changed": overrides.get("changed", False)}
    payload.update({k: v for k, v in overrides.items()
                    if k not in ("enabled", "changed")})
    return json.dumps(payload)


class SchedulerStructureTests(unittest.TestCase):
    def test_invisible_resident_item_with_data_changed(self):
        self.assertIn("signal dataChanged()", SCHEDULER)
        self.assertIn("visible: false", SCHEDULER)
        self.assertIn("property bool schedulerEnabled", SCHEDULER)
        self.assertIn("property int tickSeconds: 60", SCHEDULER)
        self.assertIn("property bool bootstrapped", SCHEDULER)
        self.assertIn("schedulerGeneration", SCHEDULER)
        self.assertIn("schedulerLaunchGeneration", SCHEDULER)
        self.assertIn("retiring", SCHEDULER)

    def test_single_process_with_bootstrap_and_tick_commands(self):
        self.assertEqual(SCHEDULER.count("Process {"), 1)
        self.assertIn('Quickshell.shellPath("scripts/memory_tick.py")', SCHEDULER)
        self.assertIn('"python3"', SCHEDULER)
        self.assertIn('"status"', SCHEDULER)
        self.assertIn('"tick"', SCHEDULER)
        self.assertIn("stdinEnabled: false", SCHEDULER)
        self.assertEqual(SCHEDULER.count("waitForEnd: true"), 2)
        self.assertIn("StdioCollector", SCHEDULER)
        self.assertIn("onStarted", SCHEDULER)
        self.assertIn("onRunningChanged", SCHEDULER)
        self.assertIn("onExited", SCHEDULER)
        self.assertIn("signal(9)", SCHEDULER)
        self.assertIn("Component.onCompleted: root.requestStatus()", SCHEDULER)

    def test_tick_timer_repeats_only_when_bootstrapped_and_enabled(self):
        self.assertIn("id: tickTimer", SCHEDULER)
        self.assertIn("repeat: true", SCHEDULER)
        self.assertIn("root.tickSeconds * 1000", SCHEDULER)
        self.assertIn("running: root.schedulerEnabled && root.bootstrapped", SCHEDULER)
        # Disabled stops the timer: no unconditional `running: true` tick.
        tick = SCHEDULER[SCHEDULER.index("id: tickTimer"):SCHEDULER.index("id: schedulerWatchdog")]
        self.assertNotIn("running: true", tick)

    def test_watchdog_fits_bounded_pipeline_budget(self):
        # 5 sessions × (15 s Jev + one 15 s retry) = 150 s + local margin.
        self.assertIn("id: schedulerWatchdog", SCHEDULER)
        self.assertIn("interval: 180000", SCHEDULER)
        self.assertIn("id: schedulerKillTimer", SCHEDULER)
        self.assertIn("interval: 10000", SCHEDULER)
        self.assertNotIn("interval: 12000", SCHEDULER)
        self.assertIn("180 s", SCHEDULER)
        self.assertIn("240 s", SCHEDULER)

    def test_generation_guard_and_changed_gating(self):
        self.assertIn("schedulerLaunchGeneration", SCHEDULER)
        self.assertIn("Number(generation) !== Number(root.schedulerLaunchGeneration)", SCHEDULER)
        self.assertIn("parsed.changed === true", SCHEDULER)
        self.assertIn("root.dataChanged()", SCHEDULER)
        self.assertIn("handleSchedulerExited", SCHEDULER)
        self.assertIn("handleSchedulerRunningChanged", SCHEDULER)
        self.assertIn("handleSchedulerTimeout", SCHEDULER)
        self.assertIn("fireSchedulerKillTimeout", SCHEDULER)

    def test_no_direct_settings_parsing_in_qml(self):
        self.assertNotIn("settings.json", SCHEDULER)
        self.assertNotIn("QUICKSHELL_SETTINGS", SCHEDULER)
        self.assertNotIn("logseqGraph", SCHEDULER)

    def test_no_auto_tick_error_surface(self):
        lowered = SCHEDULER.lower()
        self.assertNotIn("errortext", lowered)
        self.assertNotIn("agendaslow", lowered)
        # The deliberate manual-scan notice is the only allowed notice:
        # strip dataChanged + the scanNotice property/usages first.
        scrubbed = lowered.replace("datachanged", "")
        scrubbed = scrubbed.replace("scannotice", "").replace("scannoticefor", "")
        self.assertNotIn("notice", scrubbed)
        self.assertIn('property string scanNotice: ""', SCHEDULER)
        self.assertIn("readonly property bool scanBusy", SCHEDULER)

    def test_shell_hosts_scheduler_by_daily_agenda(self):
        self.assertIn("MemoryScheduler", SHELL)
        self.assertIn("id: memoryScheduler", SHELL)
        agenda = SHELL.index("id: dailyAgenda")
        scheduler = SHELL.index("id: memoryScheduler")
        variants = SHELL.index("Variants {")
        self.assertLess(agenda, scheduler)
        self.assertLess(scheduler, variants)
        # Pre-existing multi-monitor layout is preserved verbatim.
        self.assertIn("model: Quickshell.screens", SHELL)
        self.assertIn("screen: modelData", SHELL)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class SchedulerPayloadTests(unittest.TestCase):
    def parse(self, payload, code=0):
        fn = extract_function(SCHEDULER, "parseSchedulerPayload")
        script = f"""
const parse = new Function("return (" + {json.dumps(fn)} + ")")();
console.log(JSON.stringify(parse({json.dumps(payload)}, {code})));
"""
        return run_node(script)

    def test_valid_status_and_tick_shapes(self):
        value = self.parse(status_payload(changed=False))
        self.assertTrue(value["ok"])
        self.assertTrue(value["available"])
        self.assertTrue(value["enabled"])
        self.assertEqual(value["tickSeconds"], 60)
        self.assertFalse(value["changed"])
        ticked = self.parse(status_payload(changed=True))
        self.assertTrue(ticked["changed"])

    def test_tick_seconds_clamped_to_safe_range(self):
        low = self.parse(status_payload(enabled={"enabled": True, "tickSeconds": 2}))
        self.assertEqual(low["tickSeconds"], 10)
        high = self.parse(status_payload(enabled={"enabled": True, "tickSeconds": 99999}))
        self.assertEqual(high["tickSeconds"], 3600)

    def test_rejects_bad_shapes(self):
        self.assertFalse(self.parse("not json{{{")["ok"])
        self.assertFalse(self.parse("")["ok"])
        self.assertFalse(
            self.parse(status_payload(), code=1)["ok"])
        self.assertFalse(self.parse(json.dumps({"changed": True}))["ok"])
        self.assertFalse(
            self.parse(json.dumps({"enabled": {"enabled": "yes", "tickSeconds": 60},
                                   "changed": True}))["ok"])
        self.assertFalse(
            self.parse(json.dumps({"enabled": {"enabled": True}}))["ok"])

    def test_soft_fail_available_false(self):
        value = self.parse(json.dumps({"available": False, "reason": "no_key"}))
        self.assertTrue(value["ok"])
        self.assertFalse(value["available"])
        self.assertFalse(value["changed"])

    def test_changed_is_strictly_true(self):
        for changed in (1, "true", {}, [], 0):
            value = self.parse(json.dumps(
                {"available": True,
                 "enabled": {"enabled": True, "tickSeconds": 60},
                 "changed": changed}))
            self.assertTrue(value["ok"])
            self.assertFalse(value["changed"], changed)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class SchedulerLifecycleTests(unittest.TestCase):
    IN_FLIGHT = ("context.schedulerGeneration = 3; context.schedulerLaunchGeneration = 3; "
                 "context.schedulerOp = 'tick'; context.schedulerLaunchOp = 'tick'; "
                 "context._launched = true; context._started = true; "
                 "context.retiring = false;")

    def test_finish_applies_enabled_dict_and_emits_only_on_changed(self):
        value = run_harness(
            self.IN_FLIGHT + "context.bootstrapped = true; context.schedulerEnabled = false;",
            f"""
const applied = context.finishScheduler(0, {json.dumps(status_payload(changed=True))}, 3);
console.log(JSON.stringify({{ applied, enabled: context.schedulerEnabled,
  tick: context.tickSeconds, bootstrapped: context.bootstrapped,
  calls: context.dataChangedCalls }}));
""")
        self.assertTrue(value["applied"])
        self.assertTrue(value["enabled"])
        self.assertEqual(value["tick"], 60)
        self.assertTrue(value["bootstrapped"])
        self.assertEqual(value["calls"], 1)

        quiet = run_harness(
            self.IN_FLIGHT,
            f"""
const applied = context.finishScheduler(0, {json.dumps(status_payload(changed=False))}, 3);
console.log(JSON.stringify({{ applied, calls: context.dataChangedCalls,
  bootstrapped: context.bootstrapped }}));
""")
        self.assertFalse(quiet["applied"])
        self.assertEqual(quiet["calls"], 0)
        self.assertTrue(quiet["bootstrapped"])

    def test_finish_applies_disabled_dict_without_emitting(self):
        payload = status_payload(
            enabled={"enabled": False, "tickSeconds": 120}, changed=False)
        value = run_harness(
            self.IN_FLIGHT,
            f"""
const applied = context.finishScheduler(0, {json.dumps(payload)}, 3);
console.log(JSON.stringify({{ applied, enabled: context.schedulerEnabled,
  tick: context.tickSeconds, bootstrapped: context.bootstrapped,
  calls: context.dataChangedCalls }}));
""")
        self.assertFalse(value["applied"])
        self.assertFalse(value["enabled"])
        self.assertEqual(value["tick"], 120)
        self.assertTrue(value["bootstrapped"])
        self.assertEqual(value["calls"], 0)

    def test_foreign_generation_is_ignored(self):
        value = run_harness(
            self.IN_FLIGHT + "context.schedulerEnabled = false;",
            f"""
const applied = context.finishScheduler(0, {json.dumps(status_payload(changed=True))}, 2);
console.log(JSON.stringify({{ applied, enabled: context.schedulerEnabled,
  calls: context.dataChangedCalls }}));
""")
        self.assertFalse(value["applied"])
        self.assertFalse(value["enabled"])
        self.assertEqual(value["calls"], 0)

    def test_late_success_after_timeout_is_dropped(self):
        value = run_harness(
            ("context.schedulerGeneration = 4; context.schedulerLaunchGeneration = 4; "
             "context._launched = true; context._started = true; context.retiring = false; "
             "context.schedulerProcess.running = true;"),
            f"""
const timedOut = context.handleSchedulerTimeout();
const frozen = context.schedulerLaunchGeneration;
const late = context.handleSchedulerExited(0, {json.dumps(status_payload(changed=True))});
console.log(JSON.stringify({{ timedOut, frozen, late, calls: context.dataChangedCalls,
  launched: context._launched, launch: context.schedulerLaunchGeneration,
  retiring: context.retiring }}));
""")
        self.assertTrue(value["timedOut"])
        self.assertEqual(value["frozen"], 4)
        self.assertFalse(value["late"])
        self.assertEqual(value["calls"], 0)
        self.assertFalse(value["launched"])
        self.assertEqual(value["launch"], -1)
        self.assertFalse(value["retiring"])

    def test_failed_start_reconciles_without_emitting(self):
        value = run_harness(
            ("context.schedulerGeneration = 6; context.schedulerLaunchGeneration = 6; "
             "context._launched = true; context._started = false; context.retiring = false; "
             "context.schedulerProcess.running = false;"),
            """
const reconciled = context.handleSchedulerRunningChanged();
console.log(JSON.stringify({ reconciled, launched: context._launched,
  launch: context.schedulerLaunchGeneration, calls: context.dataChangedCalls }));
""")
        self.assertFalse(value["reconciled"])
        self.assertFalse(value["launched"])
        self.assertEqual(value["launch"], -1)
        self.assertEqual(value["calls"], 0)

    def test_launch_overlap_guard_and_bootstrap(self):
        value = run_harness(
            "context.bootstrapped = false; context.schedulerEnabled = false;",
            """
const status = context.requestStatus();
const overlap = context.requestTick();
console.log(JSON.stringify({ status, overlap,
  command: context.schedulerProcess.command,
  op: context.schedulerOp, launchOp: context.schedulerLaunchOp,
  watchRestarted: context.schedulerWatchdog.restarted,
  running: context.schedulerProcess.running }));
""")
        self.assertTrue(value["status"])
        self.assertFalse(value["overlap"])
        self.assertEqual(value["command"], ["python3", "/repo/scripts/memory_tick.py", "status"])
        self.assertEqual(value["op"], "status")
        self.assertTrue(value["watchRestarted"])

    def test_tick_gated_on_bootstrap_and_enabled(self):
        blocked = run_harness(
            ("context.bootstrapped = false; context.schedulerEnabled = false; "
             "context.schedulerProcess.running = false;"),
            "console.log(JSON.stringify({ tick: context.requestTick() }));")
        self.assertFalse(blocked["tick"])
        disabled = run_harness(
            ("context.bootstrapped = true; context.schedulerEnabled = false; "
             "context.schedulerProcess.running = false;"),
            "console.log(JSON.stringify({ tick: context.requestTick() }));")
        self.assertFalse(disabled["tick"])
        live = run_harness(
            ("context.bootstrapped = true; context.schedulerEnabled = true; "
             "context.schedulerProcess.running = false;"),
            """
const tick = context.requestTick();
console.log(JSON.stringify({ tick, command: context.schedulerProcess.command }));
""")
        self.assertTrue(live["tick"])
        self.assertEqual(live["command"], ["python3", "/repo/scripts/memory_tick.py", "tick"])

    def test_timeout_keeps_frozen_launch_and_kill_escalates(self):
        # SIGTERM is asynchronous: writing running=false must not
        # synchronously stop a live helper (mirrors the currentProject
        # async-termination coverage), so the kill timer still escalates.
        value = run_harness(
            ("context.schedulerGeneration = 4; context.schedulerLaunchGeneration = 4; "
             "context._launched = true; context._started = true; context.retiring = false; "
             "let live = true; context.runningWrites = []; "
             "Object.defineProperty(context.schedulerProcess, 'running', {"
             "  get() { return live; },"
             "  set(v) { context.runningWrites.push(v); if (v === true) live = true; },"
             "  configurable: true });"
             "context.schedulerProcess.signal = (sig) => {"
             "  context.schedulerProcess.signaled.push(sig);"
             "  if (sig === 9) live = false; return true; };"),
            """
const timedOut = context.handleSchedulerTimeout();
const frozen = context.schedulerLaunchGeneration;
const escalated = context.fireSchedulerKillTimeout();
console.log(JSON.stringify({ timedOut, frozen, retiring: context.retiring,
  escalated, signaled: context.schedulerProcess.signaled,
  sigtermWritten: context.runningWrites.indexOf(false) !== -1, live }));
""")
        self.assertTrue(value["timedOut"])
        self.assertEqual(value["frozen"], 4)
        self.assertTrue(value["retiring"])
        self.assertTrue(value["escalated"])
        self.assertIn(9, value["signaled"])

    def test_kill_idle_without_retiring_process(self):
        value = run_harness(
            "context.retiring = false; context.schedulerProcess.running = false;",
            """
const fired = context.fireSchedulerKillTimeout();
console.log(JSON.stringify({ fired, signaled: context.schedulerProcess.signaled }));
""")
        self.assertFalse(value["fired"])
        self.assertEqual(value["signaled"], [])


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class SchedulerBootstrapRetryTests(unittest.TestCase):
    STATUS_IN_FLIGHT = ("context.schedulerGeneration = 7; context.schedulerLaunchGeneration = 7; "
                        "context.schedulerOp = 'status'; context.schedulerLaunchOp = 'status'; "
                        "context._launched = true; context._started = true; "
                        "context.retiring = false; context.bootstrapped = false; "
                        "context.bootstrapFailures = 0; "
                        "context.schedulerProcess.running = false;")

    def test_structure_indefinite_rate_bounded_retry(self):
        self.assertIn("id: bootstrapRetryTimer", SCHEDULER)
        self.assertIn('objectName: "schedulerBootstrapRetry"', SCHEDULER)
        self.assertIn("repeat: false", SCHEDULER)
        self.assertIn("onTriggered: root.retryBootstrapStatus()", SCHEDULER)
        self.assertIn("property int bootstrapFailures: 0", SCHEDULER)
        self.assertIn("function scheduleBootstrapRetry()", SCHEDULER)
        self.assertIn("function cancelBootstrapRetry()", SCHEDULER)
        self.assertIn("function bootstrapRetryDelayMs()", SCHEDULER)
        self.assertIn("function retryBootstrapStatus()", SCHEDULER)
        # Indefinite retry: no permanent max-attempt stop, no typeof hacks.
        self.assertNotIn("bootstrapMaxRetries", SCHEDULER)
        self.assertNotIn("typeof bootstrapRetryTimer", SCHEDULER)
        self.assertIn("failures = 5", SCHEDULER)

    def test_backoff_delay_ms(self):
        value = run_harness(
            "context.bootstrapFailures = 0;",
            """
const delays = [];
for (const n of [0, 1, 2, 3, 4, 5, 6, 10]) {
  context.bootstrapFailures = n;
  delays.push(context.bootstrapRetryDelayMs());
}
console.log(JSON.stringify({ delays }));
""")
        self.assertEqual(value["delays"],
                         [10000, 10000, 20000, 40000, 80000, 120000, 120000, 120000])

    def test_schedule_increments_capped_and_arms_timer(self):
        value = run_harness(
            "context.bootstrapped = false; context.bootstrapFailures = 0;",
            """
const steps = [];
for (let i = 0; i < 7; i++) {
  const scheduled = context.scheduleBootstrapRetry();
  steps.push({ scheduled, failures: context.bootstrapFailures,
    interval: context.bootstrapRetryTimer.interval });
}
console.log(JSON.stringify({ steps, restarts: context.bootstrapRetryTimer.restartCount }));
""")
        intervals = [step["interval"] for step in value["steps"]]
        failures = [step["failures"] for step in value["steps"]]
        self.assertTrue(all(step["scheduled"] for step in value["steps"]))
        self.assertEqual(failures, [1, 2, 3, 4, 5, 5, 5])
        self.assertEqual(intervals, [10000, 20000, 40000, 80000, 120000, 120000, 120000])
        self.assertEqual(value["restarts"], 7)

    def test_schedule_noop_once_bootstrapped(self):
        value = run_harness(
            ("context.bootstrapped = true; context.bootstrapFailures = 2; "
             "context.bootstrapRetryTimer.running = false;"),
            """
const scheduled = context.scheduleBootstrapRetry();
console.log(JSON.stringify({ scheduled, failures: context.bootstrapFailures,
  interval: context.bootstrapRetryTimer.interval,
  restarts: context.bootstrapRetryTimer.restartCount }));
""")
        self.assertFalse(value["scheduled"])
        self.assertEqual(value["failures"], 2)
        self.assertEqual(value["restarts"], 0)

    def test_cancel_stops_timer(self):
        value = run_harness(
            "context.bootstrapRetryTimer.running = true;",
            """
context.cancelBootstrapRetry();
console.log(JSON.stringify({ running: context.bootstrapRetryTimer.running,
  stops: context.bootstrapRetryTimer.stopCount }));
""")
        self.assertFalse(value["running"])
        self.assertEqual(value["stops"], 1)

    def test_unsuccessful_exit_schedules_retry(self):
        value = run_harness(
            self.STATUS_IN_FLIGHT,
            """
const applied = context.handleSchedulerExited(1, "");
console.log(JSON.stringify({ applied, bootstrapped: context.bootstrapped,
  failures: context.bootstrapFailures, interval: context.bootstrapRetryTimer.interval,
  restarts: context.bootstrapRetryTimer.restartCount, launched: context._launched }));
""")
        self.assertFalse(value["applied"])
        self.assertFalse(value["bootstrapped"])
        self.assertEqual(value["failures"], 1)
        self.assertEqual(value["interval"], 10000)
        self.assertEqual(value["restarts"], 1)
        self.assertFalse(value["launched"])

    def test_malformed_output_schedules_retry(self):
        value = run_harness(
            self.STATUS_IN_FLIGHT,
            """
const applied = context.handleSchedulerExited(0, "not json{{{");
console.log(JSON.stringify({ applied, bootstrapped: context.bootstrapped,
  failures: context.bootstrapFailures, interval: context.bootstrapRetryTimer.interval,
  restarts: context.bootstrapRetryTimer.restartCount }));
""")
        self.assertFalse(value["applied"])
        self.assertFalse(value["bootstrapped"])
        self.assertEqual(value["failures"], 1)
        self.assertEqual(value["interval"], 10000)
        self.assertEqual(value["restarts"], 1)

    def test_available_false_schedules_retry(self):
        value = run_harness(
            self.STATUS_IN_FLIGHT,
            """
const applied = context.handleSchedulerExited(0, '{"available": false, "reason": "no_key"}');
console.log(JSON.stringify({ applied, bootstrapped: context.bootstrapped,
  failures: context.bootstrapFailures, interval: context.bootstrapRetryTimer.interval,
  restarts: context.bootstrapRetryTimer.restartCount }));
""")
        self.assertFalse(value["applied"])
        self.assertFalse(value["bootstrapped"])
        self.assertEqual(value["failures"], 1)
        self.assertEqual(value["interval"], 10000)
        self.assertEqual(value["restarts"], 1)

    def test_failed_start_schedules_retry(self):
        value = run_harness(
            ("context.schedulerGeneration = 6; context.schedulerLaunchGeneration = 6; "
             "context.schedulerOp = 'status'; context.schedulerLaunchOp = 'status'; "
             "context._launched = true; context._started = false; context.retiring = false; "
             "context.bootstrapped = false; context.bootstrapFailures = 0; "
             "context.schedulerProcess.running = false;"),
            """
const reconciled = context.handleSchedulerRunningChanged();
console.log(JSON.stringify({ reconciled, failures: context.bootstrapFailures,
  interval: context.bootstrapRetryTimer.interval,
  restarts: context.bootstrapRetryTimer.restartCount, launched: context._launched }));
""")
        self.assertFalse(value["reconciled"])
        self.assertEqual(value["failures"], 1)
        self.assertEqual(value["interval"], 10000)
        self.assertEqual(value["restarts"], 1)
        self.assertFalse(value["launched"])

    def test_watchdog_retiring_completion_schedules_retry(self):
        value = run_harness(
            ("context.schedulerGeneration = 4; context.schedulerLaunchGeneration = 4; "
             "context.schedulerOp = 'status'; context.schedulerLaunchOp = 'status'; "
             "context._launched = true; context._started = true; context.retiring = false; "
             "context.bootstrapped = false; context.bootstrapFailures = 0; "
             "context.schedulerProcess.running = true;"),
            f"""
const timedOut = context.handleSchedulerTimeout();
const late = context.handleSchedulerExited(0, {json.dumps(status_payload(changed=True))});
console.log(JSON.stringify({{ timedOut, late, calls: context.dataChangedCalls,
  failures: context.bootstrapFailures, interval: context.bootstrapRetryTimer.interval,
  restarts: context.bootstrapRetryTimer.restartCount,
  launched: context._launched, retiring: context.retiring }}));
""")
        self.assertTrue(value["timedOut"])
        self.assertFalse(value["late"])
        self.assertEqual(value["calls"], 0)
        self.assertEqual(value["failures"], 1)
        self.assertEqual(value["interval"], 10000)
        self.assertEqual(value["restarts"], 1)
        self.assertFalse(value["launched"])
        self.assertFalse(value["retiring"])

    def test_repeated_failures_past_five_still_schedule_capped(self):
        value = run_harness(
            self.STATUS_IN_FLIGHT + "context.bootstrapFailures = 5;",
            """
const applied = context.handleSchedulerExited(1, "");
console.log(JSON.stringify({ applied, failures: context.bootstrapFailures,
  interval: context.bootstrapRetryTimer.interval,
  restarts: context.bootstrapRetryTimer.restartCount, bootstrapped: context.bootstrapped }));
""")
        self.assertFalse(value["applied"])
        self.assertFalse(value["bootstrapped"])
        self.assertEqual(value["failures"], 5)
        self.assertEqual(value["interval"], 120000)
        self.assertEqual(value["restarts"], 1)

    def test_valid_enabled_and_disabled_cancel_retry(self):
        enabled = run_harness(
            self.STATUS_IN_FLIGHT,
            f"""
const applied = context.handleSchedulerExited(0, {json.dumps(status_payload(changed=True))});
console.log(JSON.stringify({{ applied, bootstrapped: context.bootstrapped,
  enabled: context.schedulerEnabled, failures: context.bootstrapFailures,
  stops: context.bootstrapRetryTimer.stopCount,
  restarts: context.bootstrapRetryTimer.restartCount, calls: context.dataChangedCalls }}));
""")
        self.assertTrue(enabled["applied"])
        self.assertTrue(enabled["bootstrapped"])
        self.assertTrue(enabled["enabled"])
        self.assertEqual(enabled["failures"], 0)
        self.assertEqual(enabled["stops"], 1)
        self.assertEqual(enabled["restarts"], 0)
        self.assertEqual(enabled["calls"], 1)

        disabled_payload = status_payload(
            enabled={"enabled": False, "tickSeconds": 120}, changed=False)
        disabled = run_harness(
            self.STATUS_IN_FLIGHT,
            f"""
const applied = context.handleSchedulerExited(0, {json.dumps(disabled_payload)});
console.log(JSON.stringify({{ applied, bootstrapped: context.bootstrapped,
  enabled: context.schedulerEnabled, tick: context.tickSeconds,
  failures: context.bootstrapFailures, stops: context.bootstrapRetryTimer.stopCount,
  restarts: context.bootstrapRetryTimer.restartCount, calls: context.dataChangedCalls }}));
""")
        self.assertFalse(disabled["applied"])
        self.assertTrue(disabled["bootstrapped"])
        self.assertFalse(disabled["enabled"])
        self.assertEqual(disabled["tick"], 120)
        self.assertEqual(disabled["failures"], 0)
        self.assertEqual(disabled["stops"], 1)
        self.assertEqual(disabled["restarts"], 0)
        self.assertEqual(disabled["calls"], 0)

    def test_retry_serializes_launch_and_requeues_when_blocked(self):
        idle = run_harness(
            ("context.bootstrapped = false; context.bootstrapFailures = 0; "
             "context.retiring = false; context._launched = false; "
             "context.schedulerProcess.running = false;"),
            """
const launched = context.retryBootstrapStatus();
console.log(JSON.stringify({ launched, failures: context.bootstrapFailures,
  command: context.schedulerProcess.command,
  restarts: context.bootstrapRetryTimer.restartCount }));
""")
        self.assertTrue(idle["launched"])
        self.assertEqual(idle["command"], ["python3", "/repo/scripts/memory_tick.py", "status"])
        self.assertEqual(idle["failures"], 0)
        self.assertEqual(idle["restarts"], 0)

        for setup in (
                "context.schedulerProcess.running = true;",
                "context._launched = true; context._started = true;",
                "context.retiring = true;",
        ):
            with self.subTest(blocked=setup):
                value = run_harness(
                    ("context.bootstrapped = false; context.bootstrapFailures = 0; "
                     "context.retiring = false; context._launched = false; "
                     "context.schedulerProcess.running = false; "
                     "context.schedulerGeneration = 0; context.schedulerLaunchGeneration = -1; ") + setup,
                    """
const launched = context.retryBootstrapStatus();
console.log(JSON.stringify({ launched, failures: context.bootstrapFailures,
  interval: context.bootstrapRetryTimer.interval,
  restarts: context.bootstrapRetryTimer.restartCount }));
""")
                self.assertFalse(value["launched"])
                self.assertEqual(value["failures"], 1)
                self.assertEqual(value["interval"], 10000)
                self.assertEqual(value["restarts"], 1)

    def test_retry_bootstrapped_cancels(self):
        value = run_harness(
            "context.bootstrapped = true;",
            """
const launched = context.retryBootstrapStatus();
console.log(JSON.stringify({ launched, stops: context.bootstrapRetryTimer.stopCount,
  restarts: context.bootstrapRetryTimer.restartCount }));
""")
        self.assertFalse(value["launched"])
        self.assertEqual(value["stops"], 1)
        self.assertEqual(value["restarts"], 0)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class SchedulerScanTests(unittest.TestCase):
    def scan_payload(self, status="skipped", reason="none", changed=False):
        return json.dumps({"available": True,
                           "enabled": {"enabled": True, "tickSeconds": 60},
                           "job": "scan", "status": status,
                           "reason": reason, "jev_calls": 0,
                           "changed": changed})

    def test_structure_scan_op(self):
        self.assertIn('function requestScan()', SCHEDULER)
        self.assertIn('function scanNoticeFor(', SCHEDULER)
        self.assertIn('function finishScan(', SCHEDULER)
        self.assertIn('property string scanNotice', SCHEDULER)
        self.assertIn('readonly property bool scanBusy', SCHEDULER)
        self.assertIn('schedulerLaunchOp === "scan"', SCHEDULER)
        self.assertIn('"scan"', SCHEDULER)
        self.assertIn('"run"', SCHEDULER)
        self.assertIn('"--job"', SCHEDULER)
        # Single Process is still shared (no second Process).
        self.assertEqual(SCHEDULER.count("Process {"), 1)

    def test_launch_scan_exact_argv(self):
        value = run_harness(
            ("context.bootstrapped = true; context.schedulerEnabled = true; "
             "context.schedulerProcess.running = false; context.retiring = false; "
             "context._launched = false;"),
            """
const launched = context.launchScheduler("scan");
const busy = context._launched && context.schedulerLaunchOp === "scan";
console.log(JSON.stringify({ launched, command: context.schedulerProcess.command,
  op: context.schedulerOp, launchOp: context.schedulerLaunchOp,
  busy, notice: context.scanNotice }));
""")
        self.assertTrue(value["launched"])
        self.assertEqual(value["command"],
                         ["python3", "/repo/scripts/memory_tick.py",
                          "run", "--job", "scan"])
        self.assertEqual(value["op"], "scan")
        self.assertEqual(value["launchOp"], "scan")
        self.assertTrue(value["busy"])
        self.assertEqual(value["notice"], "")

    def test_request_scan_refuses_while_unbootstrapped(self):
        blocked = run_harness(
            "context.bootstrapped = false; context.schedulerProcess.running = false;",
            "console.log(JSON.stringify({ launched: context.requestScan() }));")
        self.assertFalse(blocked["launched"])
        live = run_harness(
            ("context.bootstrapped = true; context.schedulerProcess.running = false; "
             "context.retiring = false; context._launched = false;"),
            "console.log(JSON.stringify({ launched: context.requestScan() }));")
        self.assertTrue(live["launched"])

    def test_scan_serialization_guards(self):
        for setup in (
                "context.schedulerProcess.running = true;",
                "context._launched = true; context._started = true;",
                "context.retiring = true;",
        ):
            with self.subTest(blocked=setup):
                value = run_harness(
                    ("context.bootstrapped = true; context.retiring = false; "
                     "context._launched = false; context.schedulerProcess.running = false; "
                     "context.schedulerGeneration = 0; context.schedulerLaunchGeneration = -1; "
                     "context.schedulerLaunchOp = '';") + setup,
                    "console.log(JSON.stringify({ launched: context.requestScan() }));")
                self.assertFalse(value["launched"])

    def test_scan_changed_true_fires_and_sets_bounded_notice(self):
        value = run_harness(
            ("context.schedulerGeneration = 9; context.schedulerLaunchGeneration = 9; "
             "context.schedulerOp = 'scan'; context.schedulerLaunchOp = 'scan'; "
             "context._launched = true; context._started = true; "
             "context.retiring = false; context.scanNotice = ''; "
             "context.schedulerProcess.running = false;"),
            f"""
const applied = context.handleSchedulerExited(0, {json.dumps(self.scan_payload(status="ok", reason="ok", changed=True))});
console.log(JSON.stringify({{ applied, calls: context.dataChangedCalls,
  notice: context.scanNotice, launched: context._launched }}));
""")
        self.assertTrue(value["applied"])
        self.assertEqual(value["calls"], 1)
        self.assertTrue(value["notice"] != "")
        self.assertLessEqual(len(value["notice"]), 64)
        self.assertFalse(value["launched"])

    def test_scan_changed_false_does_not_fire(self):
        value = run_harness(
            ("context.schedulerGeneration = 9; context.schedulerLaunchGeneration = 9; "
             "context.schedulerOp = 'scan'; context.schedulerLaunchOp = 'scan'; "
             "context._launched = true; context._started = true; "
             "context.retiring = false; context.scanNotice = ''; "
             "context.schedulerProcess.running = false;"),
            f"""
const applied = context.handleSchedulerExited(0, {json.dumps(self.scan_payload(status="skipped", reason="none", changed=False))});
console.log(JSON.stringify({{ applied, calls: context.dataChangedCalls,
  notice: context.scanNotice }}));
""")
        self.assertFalse(value["applied"])
        self.assertEqual(value["calls"], 0)
        self.assertEqual(value["notice"], "Nothing new")

    def test_scan_notice_mapping(self):
        cases = [
            (self.scan_payload(status="skipped", reason="disabled"), "Capture disabled"),
            (self.scan_payload(status="skipped", reason="no_key"), "Capture needs an API key"),
            (self.scan_payload(status="deferred", reason="capped"), "Capture capped"),
            (self.scan_payload(status="deferred", reason="failed"), "Capture scan deferred"),
            (self.scan_payload(status="unavailable", reason="x"), "Capture scan failed"),
        ]
        for payload, expected in cases:
            with self.subTest(expected=expected):
                value = run_harness(
                    "context.bootstrapped = true;",
                    f"""
const notice = context.scanNoticeFor({json.dumps(payload)}, 0);
console.log(JSON.stringify({{ notice }}));
""")
                self.assertEqual(value["notice"], expected)
                self.assertLessEqual(len(value["notice"]), 64)
        # Unknown/failure reads as empty.
        for payload, code in (("not json{{{", 0), ("", 1),
                              (json.dumps({"available": False}), 0),
                              (json.dumps({"available": True, "job": "scan",
                                           "status": "mystery", "changed": False}), 0)):
            with self.subTest(payload=payload):
                value = run_harness(
                    "context.bootstrapped = true;",
                    f"""
const notice = context.scanNoticeFor({json.dumps(payload)}, {code});
console.log(JSON.stringify({{ notice }}));
""")
                self.assertEqual(value["notice"], "")

    def test_scan_cleared_on_new_launch(self):
        value = run_harness(
            ("context.bootstrapped = true; context.scanNotice = 'Old line'; "
             "context.schedulerProcess.running = false; context.retiring = false; "
             "context._launched = false;"),
            """
const launched = context.launchScheduler("scan");
console.log(JSON.stringify({ launched, notice: context.scanNotice }));
""")
        self.assertTrue(value["launched"])
        self.assertEqual(value["notice"], "")


class SchedulerSettingsTests(unittest.TestCase):
    def test_example_memory_block_matches_status_contract(self):
        example = json.loads((REPO_ROOT / "settings.example.json").read_text(encoding="utf-8"))
        memory = example.get("memory")
        self.assertIsInstance(memory, dict)
        self.assertTrue(memory["enabled"])
        self.assertEqual(memory["tickSeconds"], 60)
        self.assertNotIn("jev", memory)

    def test_memory_settings_defaults_and_clamps(self):
        defaults = settings.memory_settings({})
        self.assertTrue(defaults["enabled"])
        self.assertEqual(defaults["tickSeconds"], 60)
        self.assertNotIn("jev", defaults)
        clamped = settings.memory_settings({"memory": {"tickSeconds": 1}})
        self.assertEqual(clamped["tickSeconds"], 10)
        strict = settings.memory_settings({"memory": {"enabled": "yes"}})
        self.assertTrue(strict["enabled"])


if __name__ == "__main__":
    unittest.main()
