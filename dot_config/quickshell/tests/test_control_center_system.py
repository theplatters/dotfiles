import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SYSTEM = (ROOT / "widgets" / "ControlCenterSystem.qml").read_text(encoding="utf-8")


def strip_qml_comments(source):
    lines = []
    in_block = False
    for line in source.splitlines():
        if in_block:
            if "*/" in line:
                in_block = False
                line = line.split("*/", 1)[1]
            else:
                continue
        while "/*" in line:
            before, _, rest = line.partition("/*")
            if "*/" in rest:
                after = rest.split("*/", 1)[1]
                line = before + after
            else:
                in_block = True
                line = before
                break
        code = line.split("//", 1)[0]
        lines.append(code)
    return "\n".join(lines)


STRIPPED = strip_qml_comments(SYSTEM)


def extract_function(source, name):
    # Skip comment-embedded API mentions (e.g. "function setBrightness(...);"
    # in the header contract): a real definition is followed by "{", not ";".
    needle = "function " + name + "("
    pos = 0
    while True:
        start = source.index(needle, pos)
        depth = 0
        paren = 1
        i = start + len(needle)
        while i < len(source) and paren > 0:
            if source[i] == "(":
                paren += 1
            elif source[i] == ")":
                paren -= 1
            i += 1
        j = i
        while j < len(source) and source[j] in " \t\r\n":
            j += 1
        if j < len(source) and source[j] == "{":
            opening = j
            break
        pos = start + len(needle)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError("unterminated function: " + name)


def process_block(source, pid):
    start = source.index("id: " + pid)
    # Block starts at the enclosing "Process {"; ends at the next top-level
    # "Timer {", "Process {", or root-level closing. Slice generously and cut
    # at the next sibling component.
    head = source.rindex("Process {", 0, start)
    tail = len(source)
    for marker in ("\n    Process {", "\n    Timer {", "\n    onActiveChanged",
                   "\n    Component.onCompleted"):
        nxt = source.find(marker, start)
        if nxt != -1:
            tail = min(tail, nxt)
    return source[head:tail]


class ControlCenterSystemApiTests(unittest.TestCase):
    def test_invisible_backend_with_required_api(self):
        for marker in (
            "property bool active",
            "property var inhibitWindow",
            "readonly property bool brightnessAvailable",
            "readonly property int brightness",
            "readonly property bool brightnessBusy",
            "function setBrightness(",
            "property string brightnessError",
            "readonly property bool nightLightAvailable",
            "readonly property bool nightLightEnabled",
            "readonly property bool nightLightBusy",
            "function toggleNightLight()",
            "property string nightLightError",
            "property bool idleInhibited",
        ):
            self.assertIn(marker, SYSTEM, marker)
        self.assertIn("visible: false", SYSTEM)
        self.assertTrue(SYSTEM.lstrip().startswith("import QtQuick"))

    def test_imports_cover_process_and_idle_inhibitor(self):
        self.assertIn("import Quickshell.Io", SYSTEM)
        self.assertIn("import Quickshell.Wayland", SYSTEM)
        self.assertIn("IdleInhibitor {", SYSTEM)
        self.assertIn("window: root.inhibitWindow", SYSTEM)
        self.assertIn("enabled: root.idleInhibited", SYSTEM)

    def test_no_notification_state_and_no_popup_binding(self):
        self.assertNotIn("NotificationServer", STRIPPED)
        self.assertNotIn("NotificationModule", STRIPPED)
        self.assertNotIn("notifServer", STRIPPED)
        self.assertNotIn("PopupWindow {", STRIPPED)
        self.assertNotIn("pkill", STRIPPED)
        self.assertNotIn("killall", STRIPPED)
        self.assertNotIn("systemctl", STRIPPED)

    def test_brightness_uses_machine_output_debounce_polling_and_coalescing(self):
        self.assertIn('"brightnessctl", "-m"', SYSTEM)
        self.assertIn('"brightnessctl", "-q", "set"', SYSTEM)
        self.assertIn("interval: 120", SYSTEM)
        self.assertIn("interval: 3000", SYSTEM)
        self.assertIn("running: root.active", SYSTEM)
        # Debounced/coalesced writes: pending last value survives a write in
        # flight instead of being dropped by a concurrent race.
        self.assertIn("_pendingBrightness", SYSTEM)
        self.assertIn("_writingBrightness", SYSTEM)
        self.assertIn("if (brightnessWriteProcess.running || root._writingBrightness >= 0)", SYSTEM)
        # Polling only when active and never launches overlapping reads.
        self.assertIn("brightnessReadProcess.running", SYSTEM)
        self.assertIn("root.refreshBrightness()", SYSTEM)
        # Graceful degradation paths.
        self.assertIn("No backlight", SYSTEM)
        self.assertIn("brightnessctl not found", SYSTEM)
        self.assertIn("Permission", SYSTEM)

    def test_night_light_discovers_prefers_hyprsunset_and_owns_process(self):
        self.assertIn("command -v hyprsunset", SYSTEM)
        self.assertIn("command -v wlsunset", SYSTEM)
        # hyprsunset preferred: checked first in parseNightLightTool.
        first = SYSTEM.index('text.indexOf("hyprsunset")')
        second = SYSTEM.index('text.indexOf("wlsunset")')
        self.assertLess(first, second)
        self.assertIn('"hyprsunset", "-t", "4000"', SYSTEM)
        self.assertIn('"wlsunset", "-T", "4001", "-t", "4000"', SYSTEM)
        # Off terminates only the owned Process; exit clears enabled.
        self.assertIn("nightLightProcess.running = false", SYSTEM)
        self.assertIn("readonly property bool nightLightEnabled: nightLightProcess.running", SYSTEM)
        self.assertIn("_nightLightDesired", SYSTEM)
        self.assertIn("No night light tool found", SYSTEM)

    def test_all_processes_handle_failed_start_without_exited(self):
        # onErrorOccurred is NOT assignable in QML (verified by actual
        # quickshell load: "Cannot assign to non-existent property").
        self.assertNotIn("onErrorOccurred", STRIPPED)
        # A helper that never starts (missing binary) emits no exited, so
        # every launcher tracks _launched/_started with onStarted plus
        # onRunningChanged (verified ScopedAgent/NetworkPanel idiom).
        for pid in ("brightnessReadProcess", "brightnessWriteProcess",
                    "nightDiscoverProcess", "nightLightProcess"):
            block = process_block(STRIPPED, pid)
            self.assertIn("property bool _launched", block, pid)
            self.assertIn("property bool _started", block, pid)
            self.assertIn("onStarted:", block, pid)
            self.assertIn("onRunningChanged:", block, pid)
            self.assertIn("onExited", block, pid)
        self.assertIn("could not be started", SYSTEM)
        self.assertIn("install brightnessctl", SYSTEM)

    def test_read_write_generations_and_queued_confirmation(self):
        self.assertIn("_brightnessWriteSeq", SYSTEM)
        self.assertIn("_brightnessReadWriteSeq", SYSTEM)
        self.assertIn("function launchBrightnessRead()", SYSTEM)
        self.assertIn("function settleBrightnessQueue()", SYSTEM)
        self.assertIn("function fireBrightnessDebounce()", SYSTEM)
        # A failed write's error must survive the confirming read.
        self.assertIn("_brightnessWriteFailed", SYSTEM)

    def test_night_light_transition_model_and_owned_kill_escalation(self):
        # Busy covers discovery AND the async stop/start transition.
        self.assertIn("root._nightLightDesired !== nightLightProcess.running", SYSTEM)
        self.assertIn("_nightLightStopPending", SYSTEM)
        self.assertIn("function syncNightLight()", SYSTEM)
        self.assertIn("function launchNightLight()", SYSTEM)
        self.assertIn("function requestNightLightStop()", SYSTEM)
        self.assertIn("function fireNightLightKillTimeout()", SYSTEM)
        self.assertIn("id: nightLightKillTimer", SYSTEM)
        # Toggle flips intent so rapid off/on cannot lose the latest request.
        self.assertIn("root._nightLightDesired = !root._nightLightDesired", SYSTEM)
        # Kill escalation targets the owned PID only via Process.signal.
        self.assertIn("nightLightProcess.signal(9)", SYSTEM)


@unittest.skipUnless(shutil.which("node"), "node is required for executable backend coverage")
class ControlCenterSystemLogicTests(unittest.TestCase):
    def run_node(self, script):
        completed = subprocess.run(
            ["node", "-e", script], text=True, capture_output=True
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def load_functions(self, *names):
        return {name: extract_function(SYSTEM, name) for name in names}

    HARNESS = """
function makeHarness() {
  const calls = {debounceRestart: 0, debounceStop: 0, killRestart: 0, killStop: 0, signals: []};
  const ctx = {
    _brightnessValue: 50, _brightnessSeen: true, _brightnessToolMissing: false, _brightnessNoDevice: false,
    _pendingBrightness: -1, _writingBrightness: -1, _brightnessWriteSeq: 0, _brightnessReadWriteSeq: -1,
    _brightnessRefreshQueued: false, _brightnessWriteFailed: false,
    brightnessError: "",
    _nightLightTool: "hyprsunset", _nightLightDesired: false, _nightLightStopPending: false,
    nightLightError: "", active: true,
    brightnessReadProcess: {running: false, command: [], _launched: false, _started: false},
    brightnessWriteProcess: {running: false, command: [], _launched: false, _started: false},
    nightLightProcess: {running: false, command: [], _launched: false, _started: false, signal(s) { calls.signals.push(s); }},
    nightDiscoverProcess: {running: false, _launched: false, _started: false},
    brightnessDebounce: {running: false,
      restart() { calls.debounceRestart++; this.running = true; },
      stop() { calls.debounceStop++; this.running = false; }},
    nightLightKillTimer: {restart() { calls.killRestart++; }, stop() { calls.killStop++; }},
  };
  Object.defineProperty(ctx, "brightnessBusy", {get() { return ctx.brightnessWriteProcess.running || ctx.brightnessDebounce.running; }});
  Object.defineProperty(ctx, "nightLightAvailable", {get() { return ctx._nightLightTool !== ""; }});
  ctx.root = ctx;
  return {ctx, calls};
}
function loadAll(vm, ctx, functions) {
  for (const value of functions) {
    const fn = vm.runInContext("(" + value + ")", ctx);
    ctx[fn.name] = fn;
  }
}
"""

    def test_brightness_parse_clamp_and_night_tool_helpers(self):
        functions = self.load_functions(
            "clampBrightnessPercent",
            "parseBrightnessMachineOutput",
            "parseNightLightTool",
            "nightLightCommandFor",
        )
        script = f"""
const vm = require("vm");
const context = {{}};
vm.createContext(context);
for (const value of {json.dumps(list(functions.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
const checks = {{
  machine: context.parseBrightnessMachineOutput("amdgpu_bl1,backlight,65535,100%,65535\\n"),
  half: context.parseBrightnessMachineOutput("intel_backlight,backlight,960,50%,1920\\n"),
  ratioFallback: context.parseBrightnessMachineOutput("dev,backlight,30,xx,100\\n"),
  invalid: context.parseBrightnessMachineOutput("not machine output"),
  empty: context.parseBrightnessMachineOutput(""),
  clampLow: context.clampBrightnessPercent(0),
  clampHigh: context.clampBrightnessPercent(250),
  clampMid: context.clampBrightnessPercent(42.4),
  clampBad: context.clampBrightnessPercent("nope"),
  hypr: context.parseNightLightTool("/usr/bin/hyprsunset\\n/usr/bin/wlsunset\\n"),
  wlOnly: context.parseNightLightTool("/usr/bin/wlsunset\\n"),
  none: context.parseNightLightTool(""),
  hyprCmd: context.nightLightCommandFor("hyprsunset"),
  wlCmd: context.nightLightCommandFor("wlsunset"),
  unknownCmd: context.nightLightCommandFor("other"),
}};
console.log(JSON.stringify(checks));
"""
        self.assertEqual(self.run_node(script), {
            "machine": 100,
            "half": 50,
            "ratioFallback": 30,
            "invalid": -1,
            "empty": -1,
            "clampLow": 1,
            "clampHigh": 100,
            "clampMid": 42,
            "clampBad": -1,
            "hypr": "hyprsunset",
            "wlOnly": "wlsunset",
            "none": "",
            "hyprCmd": ["hyprsunset", "-t", "4000"],
            "wlCmd": ["wlsunset", "-T", "4001", "-t", "4000"],
            "unknownCmd": [],
        })

    def test_coalescing_keeps_last_write_when_busy(self):
        debounce = SYSTEM[SYSTEM.index("function fireBrightnessDebounce()"):SYSTEM.index("function classifyBrightnessReadFailure")]
        # The debounce handler re-arms itself while a write runs and the
        # write exit handler restarts it when a newer value arrived.
        self.assertIn("brightnessDebounce.restart()", debounce)
        write_exit = SYSTEM[SYSTEM.index("function handleBrightnessWriteExited"):SYSTEM.index("function handleBrightnessWriteRunningChanged")]
        self.assertIn("if (root._pendingBrightness >= 0)", write_exit)

    def test_stale_read_never_overwrites_newer_write(self):
        functions = self.load_functions(
            "clampBrightnessPercent", "setBrightness", "parseBrightnessMachineOutput",
            "launchBrightnessRead", "refreshBrightness", "settleBrightnessQueue",
            "fireBrightnessDebounce", "classifyBrightnessReadFailure",
            "handleBrightnessReadExited", "handleBrightnessWriteExited",
        )
        script = f"""
const vm = require("vm");
{self.HARNESS}
const context = makeHarness().ctx;
vm.createContext(context);
context.calls = {{debounceRestart: 0}};
loadAll(vm, context, {json.dumps(list(functions.values()))});
const seen = {{}};
// Poll read launches (generation 0).
context.refreshBrightness();
seen.readStarted = context.brightnessReadProcess.running === true;
seen.readSeq = context._brightnessReadWriteSeq;
// Slider moves to 80 while the poll read is delayed.
context.setBrightness(80);
context.fireBrightnessDebounce();
context.brightnessDebounce.running = false; // single-shot timer fired
seen.writeLaunched = context.brightnessWriteProcess.running === true;
seen.writeSeq = context._brightnessWriteSeq;
// Stale poll read (started before the write) completes late with the old 50%.
context.brightnessReadProcess.running = false;
context.handleBrightnessReadExited(0, "dev,backlight,32768,50%,65535\\n", "");
seen.valueAfterStale = context._brightnessValue;
seen.queuedAfterStale = context._brightnessRefreshQueued;
// Write completes; confirmation must launch a FRESH read, not reuse the stale one.
context.brightnessWriteProcess.running = false;
context.handleBrightnessWriteExited(0, "");
seen.confirmLaunched = context.brightnessReadProcess.running === true;
seen.confirmSeq = context._brightnessReadWriteSeq;
// Fresh confirmation read completes with the new 80%.
context.brightnessReadProcess.running = false;
context.handleBrightnessReadExited(0, "dev,backlight,52428,80%,65535\\n", "");
seen.finalValue = context._brightnessValue;
seen.finalError = context.brightnessError;
seen.finalQueued = context._brightnessRefreshQueued;
seen.readSettled = context.brightnessReadProcess._launched;
console.log(JSON.stringify(seen));
"""
        self.assertEqual(self.run_node(script), {
            "readStarted": True,
            "readSeq": 0,
            "writeLaunched": True,
            "writeSeq": 1,
            "valueAfterStale": 80,
            "queuedAfterStale": True,
            "confirmLaunched": True,
            "confirmSeq": 1,
            "finalValue": 80,
            "finalError": "",
            "finalQueued": False,
            "readSettled": False,
        })

    def test_write_confirmation_not_skipped_when_poll_read_running(self):
        functions = self.load_functions(
            "clampBrightnessPercent", "setBrightness", "parseBrightnessMachineOutput",
            "launchBrightnessRead", "refreshBrightness", "settleBrightnessQueue",
            "fireBrightnessDebounce", "classifyBrightnessReadFailure",
            "handleBrightnessReadExited", "handleBrightnessWriteExited",
        )
        script = f"""
const vm = require("vm");
{self.HARNESS}
const h = makeHarness();
const context = h.ctx;
vm.createContext(context);
loadAll(vm, context, {json.dumps(list(functions.values()))});
// Read in flight (generation 0); write for 80 starts and finishes first.
context.refreshBrightness();
context.setBrightness(80);
context.fireBrightnessDebounce();
context.brightnessDebounce.running = false;
context.brightnessWriteProcess.running = false;
context.handleBrightnessWriteExited(0, "");
const queuedWhileReadRunning = context._brightnessRefreshQueued;
const seqWhileReadRunning = context._brightnessReadWriteSeq;
// The pre-write read completes late: stale, must not install 50, must re-queue.
context.brightnessReadProcess.running = false;
context.handleBrightnessReadExited(0, "dev,backlight,32768,50%,65535\\n", "");
const result = {{
  queuedWhileReadRunning, seqWhileReadRunning,
  value: context._brightnessValue,
  queued: context._brightnessRefreshQueued,
  readRunning: context.brightnessReadProcess.running,
  seq: context._brightnessReadWriteSeq,
}};
console.log(JSON.stringify(result));
"""
        # Write exit while the old read runs: queue, do not launch over it;
        # the stale completion then triggers the fresh confirmation read.
        self.assertEqual(self.run_node(script), {
            "queuedWhileReadRunning": True,
            "seqWhileReadRunning": 0,
            "value": 80,
            "queued": False,
            "readRunning": True,
            "seq": 1,
        })

    def test_write_failed_start_preserves_value_and_reports(self):
        functions = self.load_functions(
            "clampBrightnessPercent", "setBrightness", "parseBrightnessMachineOutput",
            "launchBrightnessRead", "refreshBrightness", "settleBrightnessQueue",
            "fireBrightnessDebounce", "classifyBrightnessReadFailure",
            "handleBrightnessReadExited", "handleBrightnessWriteExited", "handleBrightnessWriteRunningChanged",
        )
        script = f"""
const vm = require("vm");
{self.HARNESS}
const h = makeHarness();
const context = h.ctx;
vm.createContext(context);
loadAll(vm, context, {json.dumps(list(functions.values()))});
context.setBrightness(70);
context.fireBrightnessDebounce();
context.brightnessDebounce.running = false;
// Never-started helper emits no exited: running false without started.
context.brightnessWriteProcess.running = false;
context.handleBrightnessWriteRunningChanged();
const afterError = {{
  writing: context._writingBrightness,
  pending: context._pendingBrightness,
  error: context.brightnessError,
  toolMissing: context._brightnessToolMissing,
  value: context._brightnessValue,
}};
// A duplicate/late exited must not clobber the actionable error.
context.handleBrightnessWriteExited(1, "");
afterError.errorAfterDupExit = context.brightnessError;
afterError.pendingAfterDupExit = context._pendingBrightness;
console.log(JSON.stringify(afterError));
"""
        result = self.run_node(script)
        self.assertEqual(result["writing"], -1)
        self.assertEqual(result["pending"], 70)
        self.assertEqual(result["value"], 70)
        self.assertTrue(result["toolMissing"])
        self.assertIn("could not be started", result["error"])
        self.assertIn("install brightnessctl", result["error"])
        self.assertEqual(result["errorAfterDupExit"], result["error"])
        self.assertEqual(result["pendingAfterDupExit"], 70)

    def test_read_failed_start_reports_actionable_error(self):
        functions = self.load_functions(
            "parseBrightnessMachineOutput", "launchBrightnessRead", "refreshBrightness",
            "settleBrightnessQueue", "classifyBrightnessReadFailure",
            "handleBrightnessReadExited", "handleBrightnessReadRunningChanged",
        )
        script = f"""
const vm = require("vm");
{self.HARNESS}
const h = makeHarness();
const context = h.ctx;
vm.createContext(context);
loadAll(vm, context, {json.dumps(list(functions.values()))});
context.refreshBrightness();
context.brightnessReadProcess.running = false;
context.handleBrightnessReadRunningChanged();
console.log(JSON.stringify({{
  launched: context.brightnessReadProcess._launched,
  toolMissing: context._brightnessToolMissing,
  error: context.brightnessError,
}}));
"""
        result = self.run_node(script)
        self.assertFalse(result["launched"])
        self.assertTrue(result["toolMissing"])
        self.assertIn("could not be started", result["error"])
        self.assertIn("install brightnessctl", result["error"])

    def test_rapid_off_on_reconciles_intent(self):
        functions = self.load_functions(
            "parseNightLightTool", "nightLightCommandFor",
            "launchNightLight", "requestNightLightStop", "syncNightLight",
            "toggleNightLight", "fireNightLightKillTimeout",
            "handleNightLightExited", "handleNightLightRunningChanged",
        )
        script = f"""
const vm = require("vm");
{self.HARNESS}
const h = makeHarness();
const context = h.ctx;
vm.createContext(context);
loadAll(vm, context, {json.dumps(list(functions.values()))});
const log = {{}};
// Steady ON (as if launched earlier: started, completion not yet handled),
// then toggle OFF (SIGTERM is async: process still alive).
context._nightLightDesired = true;
context.nightLightProcess.running = true;
context.nightLightProcess._launched = true;
context.nightLightProcess._started = true;
context.toggleNightLight();
log.desiredAfterOff = context._nightLightDesired;
log.stopPending = context._nightLightStopPending;
log.killRestarted = h.calls.killRestart === 1;
// SIGTERM still in flight: process reports running. Rapid toggle ON flips
// intent back instead of losing the request.
context.nightLightProcess.running = true;
context.toggleNightLight();
log.desiredAfterRapidOn = context._nightLightDesired;
log.stillRunning = context.nightLightProcess.running;
// The earlier SIGTERM wins the race: exit while desired must RELAUNCH,
// not report a spurious failure.
context.nightLightProcess.running = false;
context.handleNightLightExited(143, "");
log.relaunched = context.nightLightProcess.running === true;
log.relaunchCmd = context.nightLightProcess.command;
log.errorAfterRace = context.nightLightError;
// A genuine crash while desired reconciles to stopped with an error.
context.nightLightProcess.running = false;
context.handleNightLightExited(1, "boom");
log.desiredAfterCrash = context._nightLightDesired;
log.errorAfterCrash = context.nightLightError;
console.log(JSON.stringify(log));
"""
        result = self.run_node(script)
        self.assertFalse(result["desiredAfterOff"])
        self.assertTrue(result["stopPending"])
        self.assertTrue(result["killRestarted"])
        self.assertTrue(result["desiredAfterRapidOn"])
        self.assertTrue(result["stillRunning"])
        self.assertTrue(result["relaunched"])
        self.assertEqual(result["relaunchCmd"], ["hyprsunset", "-t", "4000"])
        self.assertEqual(result["errorAfterRace"], "")
        self.assertFalse(result["desiredAfterCrash"])
        self.assertIn("Night light exited", result["errorAfterCrash"])
        self.assertIn("boom", result["errorAfterCrash"])

    def test_night_light_failed_start_and_stop_timeout(self):
        functions = self.load_functions(
            "parseNightLightTool", "nightLightCommandFor",
            "launchNightLight", "requestNightLightStop", "syncNightLight",
            "toggleNightLight", "fireNightLightKillTimeout",
            "handleNightLightExited", "handleNightLightRunningChanged",
        )
        script = f"""
const vm = require("vm");
{self.HARNESS}
const h = makeHarness();
const context = h.ctx;
vm.createContext(context);
loadAll(vm, context, {json.dumps(list(functions.values()))});
const log = {{}};
// Launch fails (binary removed after discovery): never-started helper
// emits no exited; running goes false without started.
context.toggleNightLight();
context.nightLightProcess.running = false;
context.handleNightLightRunningChanged();
log.desiredAfterFailedStart = context._nightLightDesired;
log.availableAfterFailedStart = context._nightLightTool !== "";
log.errorAfterFailedStart = context.nightLightError;
// Lingering stop escalates to SIGKILL on the owned PID only.
context._nightLightTool = "wlsunset";
context._nightLightDesired = true;
context.nightLightProcess.running = true;
context.toggleNightLight();
context.nightLightProcess.running = true; // SIGTERM ignored, still alive
context.fireNightLightKillTimeout();
log.killSignals = h.calls.signals;
// Cancelled stop must NOT kill the now-desired process.
h.calls.signals = [];
context._nightLightDesired = true;
context.fireNightLightKillTimeout();
log.signalsAfterCancel = h.calls.signals;
console.log(JSON.stringify(log));
"""
        result = self.run_node(script)
        self.assertFalse(result["desiredAfterFailedStart"])
        self.assertFalse(result["availableAfterFailedStart"])
        self.assertIn("could not be started", result["errorAfterFailedStart"])
        self.assertIn("hyprsunset", result["errorAfterFailedStart"])
        self.assertEqual(result["killSignals"], [9])
        self.assertEqual(result["signalsAfterCancel"], [])


if __name__ == "__main__":
    unittest.main()
