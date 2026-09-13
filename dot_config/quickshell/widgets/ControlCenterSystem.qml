import QtQuick
import Quickshell.Io
import Quickshell.Wayland

/*
 * ControlCenterSystem.qml — invisible backend for the control-center system
 * section (brightness / night light / idle inhibit).
 *
 * Independent package: no UI, no shell wiring, no notification state.
 * The UI worker binds to the API below.
 *
 * API contract:
 * - property bool active (CC requestedOpen mirror; drives polling/refresh)
 * - property var inhibitWindow (persistent bar PanelWindow; NOT a popup)
 * - readonly brightnessAvailable, brightness (0-100), brightnessBusy
 * - function setBrightness(percent); property string brightnessError
 * - readonly nightLightAvailable, nightLightEnabled, nightLightBusy
 * - function toggleNightLight(); property string nightLightError
 * - property bool idleInhibited (writable; bound to IdleInhibitor)
 *
 * Verified against this machine / docs (2026-09-12):
 * - brightnessctl present; `brightnessctl -m` yields
 *   `device,class,current,percent%,max` (e.g. amdgpu_bl1,backlight,65535,100%,65535).
 * - hyprsunset absent, wlsunset absent. Flags verified via man/wiki:
 *   hyprsunset `-t/--temperature KELVIN` (default 6000);
 *   wlsunset `-T high` (default 6500) / `-t low` (default 4000).
 *   Forced-warm toggle uses `hyprsunset -t 4000` or
 *   `wlsunset -T 4001 -t 4000` (both ends ~4000K so the filter is on
 *   regardless of day/night calculation).
 * - Quickshell.Wayland IdleInhibitor API: `window` + `enabled`.
 * - Launch lifecycle (VERIFIED by actual `quickshell -p shell.qml` load):
 *   `onErrorOccurred` is NOT assignable in QML ("Cannot assign to
 *   non-existent property") despite appearing in the qmltypes. A helper
 *   that never starts (missing binary) emits no `exited`, so failed starts
 *   are detected via per-Process `_launched`/`_started` flags with
 *   `onStarted` + `onRunningChanged` — the ScopedAgent/NetworkPanel idiom.
 *   Completion handlers clear `_launched` first, making them idempotent.
 */
Item {
    id: root
    visible: false

    property bool active: false
    property var inhibitWindow: null
    property bool idleInhibited: false

    // ---- Brightness state ----
    readonly property bool brightnessAvailable: _brightnessSeen && !_brightnessToolMissing && !_brightnessNoDevice
    readonly property int brightness: _brightnessValue
    readonly property bool brightnessBusy: brightnessWriteProcess.running || brightnessDebounce.running
    property string brightnessError: ""

    property int _brightnessValue: 100
    property bool _brightnessSeen: false
    property bool _brightnessToolMissing: false
    property bool _brightnessNoDevice: false
    property int _pendingBrightness: -1
    property int _writingBrightness: -1
    // Read/write generations: a read captures _brightnessWriteSeq at launch;
    // a completion whose seq no longer matches was started before a newer
    // write and must not install its stale value.
    property int _brightnessWriteSeq: 0
    property int _brightnessReadWriteSeq: -1
    property bool _brightnessRefreshQueued: false
    property bool _brightnessWriteFailed: false

    // ---- Night light state ----
    readonly property bool nightLightAvailable: _nightLightTool !== ""
    readonly property bool nightLightEnabled: nightLightProcess.running
    // Busy covers discovery AND the async stop/start transition
    // (SIGTERM takes effect asynchronously; intent != actual while in flight).
    readonly property bool nightLightBusy: nightDiscoverProcess.running || (root._nightLightDesired !== nightLightProcess.running)
    property string nightLightError: ""

    property string _nightLightTool: ""
    // Intent vs actual: _nightLightDesired is what the user last asked for,
    // nightLightProcess.running is what is actually alive. Toggling flips
    // intent (never blindly pokes `running`), so rapid off/on cannot lose
    // the latest request; onExited reconciles the two.
    property bool _nightLightDesired: false
    property bool _nightLightStopPending: false

    // Idle inhibit binds the PERSISTENT bar window so the inhibit survives
    // popup closure. Never bind a popup window here.
    IdleInhibitor {
        id: idleInhibitor
        window: root.inhibitWindow
        enabled: root.idleInhibited
    }

    // ---- Brightness helpers (pure JS, unit-tested via node) ----
    function clampBrightnessPercent(value) {
        var v = Math.round(Number(value));
        if (!isFinite(v))
            return -1;
        return Math.max(1, Math.min(100, v));
    }

    function parseBrightnessMachineOutput(output) {
        if (!output)
            return -1;
        var lines = String(output).split("\n");
        for (var i = 0; i < lines.length; ++i) {
            var line = lines[i].trim();
            if (!line || line.indexOf(",") === -1)
                continue;
            var parts = line.split(",");
            if (parts.length < 5)
                continue;
            var current = Number(parts[2]);
            var max = Number(parts[4]);
            var percent = Number(String(parts[3]).replace("%", "").trim());
            if (!isFinite(percent)) {
                if (isFinite(current) && isFinite(max) && max > 0)
                    percent = Math.round(current / max * 100);
                else
                    continue;
            }
            if (percent < 0 || percent > 100) {
                if (isFinite(current) && isFinite(max) && max > 0)
                    percent = Math.round(current / max * 100);
                else
                    continue;
            }
            return Math.max(0, Math.min(100, Math.round(percent)));
        }
        return -1;
    }

    function setBrightness(percent) {
        var target = root.clampBrightnessPercent(percent);
        if (target < 0) {
            root.brightnessError = "Invalid brightness value";
            return;
        }
        // Coalesce: keep only the last requested value. The debounce timer
        // and completion handlers below guarantee the last value is never
        // lost when a write is already in flight.
        root._pendingBrightness = target;
        root._brightnessWriteFailed = false;
        root.brightnessError = "";
        // Optimistic display; stale reads are discarded via generations (see
        // handleBrightnessReadExited) so a pre-write poll cannot clobber this.
        root._brightnessValue = target;
        brightnessDebounce.restart();
    }

    function launchBrightnessRead() {
        if (brightnessReadProcess.running || brightnessReadProcess._launched)
            return false;
        brightnessReadProcess._launched = true;
        brightnessReadProcess._started = false;
        root._brightnessReadWriteSeq = root._brightnessWriteSeq;
        brightnessReadProcess.command = ["brightnessctl", "-m"];
        brightnessReadProcess.running = true;
        return true;
    }

    function refreshBrightness() {
        if (brightnessReadProcess.running || brightnessReadProcess._launched || root.brightnessBusy) {
            // Defer: the queued flag is consumed by settleBrightnessQueue
            // once the in-flight read/write settles.
            root._brightnessRefreshQueued = true;
            return;
        }
        root._brightnessRefreshQueued = false;
        root.launchBrightnessRead();
    }

    function settleBrightnessQueue() {
        if (root._pendingBrightness >= 0 || root._writingBrightness >= 0
                || brightnessWriteProcess.running || brightnessDebounce.running
                || brightnessReadProcess.running || brightnessReadProcess._launched)
            return;
        if (!root._brightnessRefreshQueued)
            return;
        root._brightnessRefreshQueued = false;
        root.launchBrightnessRead();
    }

    function fireBrightnessDebounce() {
        if (root._pendingBrightness < 0)
            return;
        // Coalesce while a write is in flight: wait for the completion
        // handler, which restarts this timer and picks up the newest value.
        if (brightnessWriteProcess.running || root._writingBrightness >= 0) {
            brightnessDebounce.restart();
            return;
        }
        root._writingBrightness = root._pendingBrightness;
        root._pendingBrightness = -1;
        root._brightnessWriteSeq++;
        root._brightnessWriteFailed = false;
        brightnessWriteProcess._launched = true;
        brightnessWriteProcess._started = false;
        brightnessWriteProcess.command = ["brightnessctl", "-q", "set", root._writingBrightness + "%"];
        brightnessWriteProcess.running = true;
    }

    function classifyBrightnessReadFailure(exitCode, stderrText) {
        var err = String(stderrText || "").trim();
        if (exitCode === 127 || err.indexOf("No such file") !== -1 || err.indexOf("not found") !== -1 || err.indexOf("command not found") !== -1) {
            root._brightnessToolMissing = true;
            return "brightnessctl not found (install brightnessctl)";
        }
        if (exitCode === 126) {
            root._brightnessToolMissing = true;
            return "brightnessctl not executable (check permission rules)";
        }
        if (err.indexOf("No backlight") !== -1 || err.indexOf("No device") !== -1 || err.indexOf("no device") !== -1) {
            root._brightnessNoDevice = true;
            return "No backlight device found";
        }
        if (err !== "")
            return err.split("\n")[0].slice(0, 160);
        if (exitCode !== 0)
            return "Brightness read failed (exit " + exitCode + ")";
        return "Brightness read failed";
    }

    function handleBrightnessReadExited(exitCode, stdoutText, stderrText) {
        var fresh = brightnessReadProcess._launched;
        brightnessReadProcess._launched = false;
        if (!fresh && exitCode !== 0) {
            // Already reported via onRunningChanged (never-started helper
            // emits no exited); do not overwrite the error, just settle.
            root.settleBrightnessQueue();
            return;
        }
        if (exitCode === 0) {
            var parsed = root.parseBrightnessMachineOutput(stdoutText);
            if (parsed >= 0) {
                root._brightnessSeen = true;
                root._brightnessToolMissing = false;
                root._brightnessNoDevice = false;
                // Discard reads started before a newer write (generation
                // mismatch) or superseded by pending/in-flight write work,
                // and queue a fresh confirmation read instead.
                var stale = (root._brightnessReadWriteSeq !== root._brightnessWriteSeq)
                    || root._pendingBrightness >= 0 || root._writingBrightness >= 0
                    || brightnessWriteProcess.running || brightnessDebounce.running;
                if (stale) {
                    root._brightnessRefreshQueued = true;
                } else {
                    root._brightnessValue = parsed;
                    // A failed write's error stays visible until the next
                    // set/write succeeds; a clean read must not erase it.
                    if (!root._brightnessWriteFailed)
                        root.brightnessError = "";
                }
            } else {
                root.brightnessError = "Could not parse brightness output";
            }
        } else {
            root.brightnessError = root.classifyBrightnessReadFailure(exitCode, stderrText);
        }
        root.settleBrightnessQueue();
    }

    function handleBrightnessReadRunningChanged() {
        // A helper that never starts (missing binary) emits no exited in
        // this Quickshell version: running going false without started is
        // the failed-start signal (verified ScopedAgent/NetworkPanel idiom).
        if (brightnessReadProcess.running || !brightnessReadProcess._launched || brightnessReadProcess._started)
            return;
        brightnessReadProcess._launched = false;
        root._brightnessToolMissing = true;
        root.brightnessError = "brightnessctl could not be started (install brightnessctl)";
        root.settleBrightnessQueue();
    }

    function handleBrightnessWriteExited(exitCode, stderrText) {
        var finished = root._writingBrightness;
        root._writingBrightness = -1;
        var fresh = brightnessWriteProcess._launched;
        brightnessWriteProcess._launched = false;
        if (!fresh) {
            // Already reported via onRunningChanged; just settle.
            root.settleBrightnessQueue();
            return;
        }
        if (exitCode !== 0) {
            root._brightnessWriteFailed = true;
            var detail = String(stderrText || "").trim().split("\n")[0] || ("exit " + exitCode);
            if (exitCode === 127 || detail.indexOf("not found") !== -1 || detail.indexOf("No such file") !== -1) {
                root._brightnessToolMissing = true;
                root.brightnessError = "brightnessctl not found (install brightnessctl)";
            } else if (exitCode === 126 || detail.indexOf("Permission") !== -1 || detail.indexOf("permission denied") !== -1 || detail.indexOf("not permitted") !== -1) {
                root.brightnessError = "Brightness write permission denied (" + detail.slice(0, 120) + ")";
            } else if (detail.indexOf("No backlight") !== -1 || detail.indexOf("No device") !== -1) {
                root._brightnessNoDevice = true;
                root.brightnessError = "No backlight device found";
            } else {
                root.brightnessError = "Brightness write failed (" + detail.slice(0, 120) + ")";
            }
            // Keep the pending last value and retry it; never drop it.
            if (root._pendingBrightness >= 0) {
                brightnessDebounce.restart();
            } else {
                // Re-read the truth: the optimistic value was not applied.
                root._brightnessRefreshQueued = true;
            }
            root.settleBrightnessQueue();
            return;
        }
        root._brightnessWriteFailed = false;
        root.brightnessError = "";
        // A newer request arrived while writing: run it now instead of
        // declaring idle, so the last value always wins.
        if (root._pendingBrightness >= 0) {
            brightnessDebounce.restart();
            return;
        }
        // Confirm the applied value with a fresh read (queued if a poll read
        // started before this write is still running).
        root._brightnessRefreshQueued = true;
        root.settleBrightnessQueue();
    }

    function handleBrightnessWriteRunningChanged() {
        if (brightnessWriteProcess.running || !brightnessWriteProcess._launched || brightnessWriteProcess._started)
            return;
        brightnessWriteProcess._launched = false;
        var failed = root._writingBrightness;
        root._writingBrightness = -1;
        root._brightnessWriteFailed = true;
        // Never lose the last value: restore it to pending. No auto-retry
        // here — a launch failure needs user action (install/fix the tool);
        // it flushes on the next setBrightness/active cycle.
        if (root._pendingBrightness < 0)
            root._pendingBrightness = failed;
        root._brightnessToolMissing = true;
        root.brightnessError = "brightnessctl could not be started (install brightnessctl)";
        root.settleBrightnessQueue();
    }

    Process {
        id: brightnessReadProcess
        // Launch tracking: a helper that never starts (missing binary)
        // emits no exited in this Quickshell version, so detect it via
        // running going false without started (verified ScopedAgent idiom).
        property bool _launched: false
        property bool _started: false
        command: ["brightnessctl", "-m"]
        stdout: StdioCollector {
            id: brightnessReadOutput
        }
        stderr: StdioCollector {
            id: brightnessReadError
        }
        onStarted: {
            brightnessReadProcess._started = true;
        }
        onRunningChanged: {
            root.handleBrightnessReadRunningChanged();
        }
        onExited: (exitCode) => {
            root.handleBrightnessReadExited(exitCode, brightnessReadOutput.text, brightnessReadError.text);
        }
    }

    Process {
        id: brightnessWriteProcess
        property bool _launched: false
        property bool _started: false
        stdout: StdioCollector {
            id: brightnessWriteOutput
        }
        stderr: StdioCollector {
            id: brightnessWriteError
        }
        onStarted: {
            brightnessWriteProcess._started = true;
        }
        onRunningChanged: {
            root.handleBrightnessWriteRunningChanged();
        }
        onExited: (exitCode) => {
            root.handleBrightnessWriteExited(exitCode, brightnessWriteError.text);
        }
    }

    Timer {
        id: brightnessDebounce
        interval: 120
        repeat: false
        onTriggered: root.fireBrightnessDebounce()
    }

    Timer {
        id: brightnessPoll
        interval: 3000
        repeat: true
        running: root.active
        onTriggered: root.refreshBrightness()
    }

    // ---- Night light helpers (pure JS, unit-tested via node) ----
    function parseNightLightTool(output) {
        var text = String(output || "");
        if (text.indexOf("hyprsunset") !== -1)
            return "hyprsunset";
        if (text.indexOf("wlsunset") !== -1)
            return "wlsunset";
        return "";
    }

    function nightLightCommandFor(tool) {
        if (tool === "hyprsunset")
            return ["hyprsunset", "-t", "4000"];
        if (tool === "wlsunset")
            return ["wlsunset", "-T", "4001", "-t", "4000"];
        return [];
    }

    function discoverNightLight() {
        if (nightDiscoverProcess.running || nightDiscoverProcess._launched)
            return;
        nightDiscoverProcess._launched = true;
        nightDiscoverProcess._started = false;
        nightDiscoverProcess.running = true;
    }

    function handleNightDiscoverExited(exitCode, stdoutText) {
        nightDiscoverProcess._launched = false;
        // Prefer hyprsunset over wlsunset; empty output means neither
        // executable is installed (graceful: available stays false).
        root._nightLightTool = root.parseNightLightTool(stdoutText);
    }

    function handleNightDiscoverRunningChanged() {
        if (nightDiscoverProcess.running || !nightDiscoverProcess._launched || nightDiscoverProcess._started)
            return;
        // Discovery itself runs under sh, so a launch failure only means
        // discovery is unavailable: stay gracefully unavailable. Toggling
        // still reports the actionable install hint.
        nightDiscoverProcess._launched = false;
        root._nightLightTool = "";
    }

    function launchNightLight() {
        var cmd = root.nightLightCommandFor(root._nightLightTool);
        if (cmd.length === 0) {
            root.nightLightError = "No night light tool found (install hyprsunset or wlsunset)";
            root._nightLightDesired = false;
            return false;
        }
        root.nightLightError = "";
        nightLightKillTimer.stop();
        nightLightProcess._launched = true;
        nightLightProcess._started = false;
        nightLightProcess.command = cmd;
        nightLightProcess.running = true;
        return true;
    }

    function requestNightLightStop() {
        root._nightLightStopPending = true;
        // Terminate ONLY the owned process (SIGTERM); never pkill or touch
        // an externally started daemon.
        nightLightProcess.running = false;
        // Escalate to SIGKILL (owned PID only) if it ignores SIGTERM.
        nightLightKillTimer.restart();
    }

    function syncNightLight() {
        if (root._nightLightDesired) {
            nightLightKillTimer.stop();
            if (nightLightProcess.running)
                return; // Already on; a cancelled stop's exit reconciles below.
            if (!root.nightLightAvailable) {
                root.nightLightError = "No night light tool found (install hyprsunset or wlsunset)";
                root._nightLightDesired = false;
                return;
            }
            root.launchNightLight();
        } else {
            if (!nightLightProcess.running) {
                root._nightLightStopPending = false;
                nightLightKillTimer.stop();
                return;
            }
            root.requestNightLightStop();
        }
    }

    function toggleNightLight() {
        if (nightDiscoverProcess.running)
            return;
        // Flip INTENT, not actual state: SIGTERM is asynchronous, so flipping
        // `running` directly would lose a rapid off/on (or on/off) request.
        root._nightLightDesired = !root._nightLightDesired;
        root.syncNightLight();
    }

    function fireNightLightKillTimeout() {
        if (!root._nightLightDesired && nightLightProcess.running)
            nightLightProcess.signal(9); // SIGKILL, owned process only.
    }

    function handleNightLightExited(exitCode, stderrText) {
        var fresh = nightLightProcess._launched;
        nightLightProcess._launched = false;
        if (!fresh) {
            // Already reconciled via onRunningChanged (never-started helper
            // emits no exited); keep that actionable error, not this one.
            return;
        }
        nightLightKillTimer.stop();
        var wasStopping = root._nightLightStopPending;
        root._nightLightStopPending = false;
        // Actual state reflects running: exit always clears enabled.
        if (root._nightLightDesired) {
            var err = String(stderrText || "").trim().split("\n")[0] || "";
            var missing = (exitCode === 127) || err.indexOf("not found") !== -1 || err.indexOf("No such file") !== -1;
            if (missing) {
                var tool = root._nightLightTool || "Night light tool";
                root._nightLightTool = "";
                root._nightLightDesired = false;
                root.nightLightError = tool + " could not be started (install hyprsunset or wlsunset)";
                return;
            }
            if (wasStopping) {
                // A stop was requested, then cancelled by a rapid re-toggle,
                // and the SIGTERM won the race: honor the latest intent by
                // relaunching instead of reporting a spurious failure.
                root.launchNightLight();
                return;
            }
            // The owned process died while desired: reconcile to stopped and
            // report; no auto-restart (a persistent failure must not loop).
            root._nightLightDesired = false;
            var detail = err || ("exit " + exitCode);
            root.nightLightError = "Night light exited (" + detail.slice(0, 140) + ")";
            return;
        }
        // Intentional stop achieved; clear stale errors.
        root.nightLightError = "";
    }

    function handleNightLightRunningChanged() {
        if (nightLightProcess.running || !nightLightProcess._launched || nightLightProcess._started)
            return;
        // Launch failed (missing binary emits no exited): reconcile like
        // ScopedAgent. Only report when still desired; a cancelled toggle
        // reconciles silently and the next toggle retries.
        nightLightProcess._launched = false;
        nightLightKillTimer.stop();
        root._nightLightStopPending = false;
        if (!root._nightLightDesired)
            return;
        root._nightLightDesired = false;
        var tool = root._nightLightTool || "Night light tool";
        root._nightLightTool = "";
        root.nightLightError = tool + " could not be started (install hyprsunset or wlsunset)";
    }

    Process {
        id: nightDiscoverProcess
        property bool _launched: false
        property bool _started: false
        command: ["sh", "-c", "command -v hyprsunset; command -v wlsunset"]
        stdout: StdioCollector {
            id: nightDiscoverOutput
        }
        stderr: StdioCollector {
            id: nightDiscoverError
        }
        onStarted: {
            nightDiscoverProcess._started = true;
        }
        onRunningChanged: {
            root.handleNightDiscoverRunningChanged();
        }
        onExited: (exitCode) => {
            root.handleNightDiscoverExited(exitCode, nightDiscoverOutput.text);
        }
    }

    Process {
        id: nightLightProcess
        property bool _launched: false
        property bool _started: false
        stdout: StdioCollector {
            id: nightLightOutput
        }
        stderr: StdioCollector {
            id: nightLightErrorOutput
        }
        onStarted: {
            nightLightProcess._started = true;
        }
        onRunningChanged: {
            root.handleNightLightRunningChanged();
        }
        onExited: (exitCode) => {
            root.handleNightLightExited(exitCode, nightLightErrorOutput.text);
        }
    }

    Timer {
        id: nightLightKillTimer
        interval: 3000
        repeat: false
        onTriggered: root.fireNightLightKillTimeout()
    }

    onActiveChanged: {
        if (root.active) {
            root.refreshBrightness();
            if (!root.nightLightAvailable)
                root.discoverNightLight();
        } else {
            brightnessDebounce.stop();
            // Never abandon a coalesced write on close: flush the last value.
            if (root._pendingBrightness >= 0 && !brightnessWriteProcess.running)
                brightnessDebounce.restart();
        }
    }

    Component.onCompleted: {
        root.refreshBrightness();
        root.discoverNightLight();
    }
}
