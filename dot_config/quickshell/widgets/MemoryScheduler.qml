import QtQuick
import Quickshell
import Quickshell.Io

// Resident Work → Memory / Memory → Work scheduler (plan §7A).
// Responsibility: bootstrap scheduler flags via
// `memory_tick.py status` (settings are read by Python via
// quickshell_settings.memory_settings(); QML never parses the settings
// file directly),
// then run `memory_tick.py tick` every tickSeconds while enabled.
// Contract with shell.qml: one invisible Item next to DailyAgenda; emits
// signal dataChanged() only when a generation-guarded payload reports
// changed === true. Auto ticks never surface errors (fail soft to the
// current behavior); manual surfaces read memory_tick.py status/tick
// stdout directly. Owned state: schedulerEnabled/tickSeconds/bootstrapped,
// schedulerGeneration/schedulerLaunchGeneration, schedulerOp/LaunchOp,
// retiring, _launched/_started, bootstrapFailures (capped backoff count
// for indefinite rate-bounded status retry while unbootstrapped; see
// bootstrapRetryTimer).
Item {
    id: root
    visible: false
    width: 0
    height: 0

    // Emitted when a guarded tick/status payload reports changed === true
    // so DailyAgenda / ProjectOverviewPopup can refresh without polling.
    signal dataChanged()

    property bool schedulerEnabled: false
    property int tickSeconds: 60
    property bool bootstrapped: false
    property int schedulerGeneration: 0
    property int schedulerLaunchGeneration: -1
    property string schedulerOp: ""
    property string schedulerLaunchOp: ""
    property bool retiring: false
    property bool _launched: false
    property bool _started: false
    // Indefinite rate-bounded bootstrap retry: counts failed `status`
    // attempts while still unbootstrapped (failed start, nonzero exit,
    // malformed payload, available === false, watchdog retirement).
    // Retries never exhaust: bootstrapFailures caps at 5 so the interval
    // stays at 120s (10s, 20s, 40s, 80s, then 120s cap). Any valid status
    // (enabled or disabled both set bootstrapped) stops the timer via
    // cancelBootstrapRetry(). No modal errors: failures fail soft and retry.
    property int bootstrapFailures: 0
    // Bounded human line for the last manual `run --job scan` completion.
    // Set only when the completed op was "scan" (the `run` payload
    // carries {available, enabled, job, status, reason, jev_calls,
    // changed}); empty when unknown/failure. Cleared when a new scan
    // starts. Never a pop-up surface: auto ticks never touch it.
    property string scanNotice: ""
    readonly property bool scanBusy: root._launched && root.schedulerLaunchOp === "scan"

    // Bounded human line for a `run --job scan` completion (no root
    // refs except via the caller): code !== 0, empty, malformed,
    // available === false, job mismatch, or unknown status => "".
    // Known shapes map status/reason to a ≤64 char line: ok with
    // changed => new captures; ok/skipped without change => Nothing
    // new; disabled/no_key/capped reasons => Capture disabled/needs
    // key/capped; unavailable/not_implemented/failed => scan failed.
    // Pure (no root refs) so unit tests can exercise it via node.
    function scanNoticeFor(output, code) {
        if (code !== 0) return "";
        let data = null;
        try {
            data = JSON.parse(output || "");
        } catch (error) {
            return "";
        }
        if (!data || typeof data !== "object" || Array.isArray(data)) return "";
        if (data.available === false) return "";
        if (data.job !== undefined && data.job !== "scan") return "";
        let status = String(data.status || "");
        let reason = String(data.reason || "");
        let changed = data.changed === true;
        if (status === "ok") return changed ? "New captures available" : "Nothing new";
        if (status === "skipped") {
            if (reason === "disabled") return "Capture disabled";
            if (reason === "no_key") return "Capture needs an API key";
            if (reason === "capped" || reason === "tick_budget" || reason === "budget") return "Capture capped";
            return "Nothing new";
        }
        if (status === "deferred") {
            if (reason === "disabled") return "Capture disabled";
            if (reason === "no_key") return "Capture needs an API key";
            if (reason === "capped" || reason === "tick_budget" || reason === "budget") return "Capture capped";
            return "Capture scan deferred";
        }
        if (status === "unavailable" || status === "not_implemented") return "Capture scan failed";
        if (status === "failed") return "Capture scan failed";
        return "";
    }

    // Guarded scan completion: the generation must match the frozen
    // schedulerLaunchGeneration and the completed op must be "scan".
    // Parses the `run` payload safely, sets the bounded line
    // (empty on unknown/failure), and emits dataChanged() only when
    // the payload reports changed === true. Never surfaces errors.
    function finishScan(code, output, generation) {
        if (Number(generation) !== Number(root.schedulerLaunchGeneration)) return false;
        if (!root._launched) return false;
        if (root.schedulerLaunchOp !== "scan") return false;
        schedulerWatchdog.stop();
        if (code !== 0) {
            root.scanNotice = "";
            return false;
        }
        let data = null;
        try {
            data = JSON.parse(output || "");
        } catch (error) {
            root.scanNotice = "";
            return false;
        }
        if (!data || typeof data !== "object" || Array.isArray(data)) {
            root.scanNotice = "";
            return false;
        }
        if (data.available === false) {
            root.scanNotice = "";
            return false;
        }
        if (data.job !== undefined && data.job !== "scan") {
            root.scanNotice = "";
            return false;
        }
        let line = root.scanNoticeFor(output, code);
        // Unknown status with changed true still refreshes, but the
        // line stays empty (unknown/failure reads as empty).
        if (line === "" && data.status !== undefined
                && data.status !== "ok" && data.status !== "skipped"
                && data.status !== "deferred" && data.status !== "unavailable"
                && data.status !== "not_implemented" && data.status !== "failed") {
            root.scanNotice = "";
            if (data.changed === true) root.dataChanged();
            return data.changed === true;
        }
        root.scanNotice = line.substring(0, 64);
        if (data.changed === true) root.dataChanged();
        return data.changed === true;
    }

    // Pure payload parser (no root refs) so unit tests can exercise it via
    // node. Accepts both `status` and `tick` shapes: each carries the same
    // `enabled` dictionary with all settings (enabled.enabled,
    // enabled.tickSeconds, ..., enabled.jev). code !== 0, empty, malformed,
    // or missing/invalid enabled dict => {ok:false}. available === false
    // (soft-fail) => {ok:true, available:false, changed:false}. changed is
    // strictly boolean; anything else reads as false so dataChanged only
    // fires on an explicit changed === true.
    function parseSchedulerPayload(output, code) {
        if (code !== 0) return { ok: false };
        let data = null;
        try {
            data = JSON.parse(output || "");
        } catch (error) {
            return { ok: false };
        }
        if (!data || typeof data !== "object" || Array.isArray(data)) return { ok: false };
        if (data.available === false) return { ok: true, available: false, changed: false };
        let enabled = data.enabled;
        if (!enabled || typeof enabled !== "object" || Array.isArray(enabled)) return { ok: false };
        if (typeof enabled.enabled !== "boolean") return { ok: false };
        let tickSeconds = enabled.tickSeconds;
        if (typeof tickSeconds !== "number" || !isFinite(tickSeconds)) return { ok: false };
        tickSeconds = Math.floor(tickSeconds);
        if (tickSeconds < 10) tickSeconds = 10;
        if (tickSeconds > 3600) tickSeconds = 3600;
        let changed = data.changed === true;
        return { ok: true, available: true, enabled: enabled.enabled, tickSeconds: tickSeconds, changed: changed };
    }

    // Guarded completion: the generation must match the frozen
    // schedulerLaunchGeneration. On success the enabled dictionary is
    // applied (even when disabled) and dataChanged() fires only when the
    // payload reports changed === true. Soft failures leave state
    // untouched and never surface errors.
    function finishScheduler(code, output, generation) {
        if (Number(generation) !== Number(root.schedulerLaunchGeneration)) return false;
        if (!root._launched) return false;
        schedulerWatchdog.stop();
        let parsed = root.parseSchedulerPayload(output, code);
        if (!parsed.ok) return false;
        if (parsed.available === false) return false;
        root.schedulerEnabled = parsed.enabled;
        root.tickSeconds = parsed.tickSeconds;
        root.bootstrapped = true;
        // Any valid status (enabled or disabled) ends bootstrap retry.
        root.cancelBootstrapRetry();
        if (parsed.changed === true) root.dataChanged();
        return parsed.changed === true;
    }

    // Next bootstrap retry delay from the capped failure count:
    // 10s, 20s, 40s, 80s, then 120s cap (failures capped at 5, so indefinite
    // retries stay at 120s). Pure (no Timer refs) so unit tests can exercise
    // it via node. Failures of 0 reads as the first 10s delay.
    function bootstrapRetryDelayMs() {
        let attempt = Number(root.bootstrapFailures);
        if (!(attempt >= 1)) attempt = 1;
        let delay = 10000 * Math.pow(2, attempt - 1);
        if (delay > 120000) delay = 120000;
        return Math.floor(delay);
    }

    // Single schedule helper: while unbootstrapped, count one failure
    // (capped at 5 so the rate stays bounded at 120s), arm the dedicated
    // one-shot with backoff, and return true. Returns false once
    // bootstrapped. Retries are indefinite and bounded by rate, never by
    // attempt exhaustion.
    function scheduleBootstrapRetry() {
        if (root.bootstrapped) return false;
        let failures = Number(root.bootstrapFailures);
        if (!(failures >= 0)) failures = 0;
        failures += 1;
        if (failures > 5) failures = 5;
        root.bootstrapFailures = failures;
        bootstrapRetryTimer.interval = root.bootstrapRetryDelayMs();
        bootstrapRetryTimer.restart();
        return true;
    }

    function cancelBootstrapRetry() {
        bootstrapRetryTimer.stop();
    }

    // Dedicated retry launch: no-op once bootstrapped (and stops the
    // timer), otherwise serialized through launchScheduler so a running
    // helper, a retiring window, or the runningChanged-before-exited window
    // is never overlapped. When the launch guard blocks (still busy), queue
    // the next backoff via scheduleBootstrapRetry so the one-shot is not
    // lost. The Timer owns the backoff; this owns the guard.
    function retryBootstrapStatus() {
        if (root.bootstrapped) {
            root.cancelBootstrapRetry();
            return false;
        }
        let launched = root.launchScheduler("status");
        if (!launched && !root.bootstrapped) root.scheduleBootstrapRetry();
        return launched;
    }

    // Actual exit handler (thin onExited delegates here so tests exercise
    // the real logic). Uses the frozen schedulerLaunchGeneration captured
    // at launch, never re-read current state: after a timeout the late
    // exit still matches its launch, but retiring drops the success instead
    // of emitting a stale dataChanged. Either path consumes the launch and
    // ends retiring.
    function handleSchedulerExited(code, output) {
        schedulerWatchdog.stop();
        schedulerKillTimer.stop();
        if (!root._launched) return false;
        if (root.retiring) {
            let retiredOp = root.schedulerLaunchOp;
            root._launched = false;
            root._started = false;
            root.schedulerLaunchGeneration = -1;
            root.retiring = false;
            // Watchdog retirement of a status launch never bootstrapped:
            // count it as one backoff retry while unbootstrapped.
            if (!root.bootstrapped && retiredOp === "status") root.scheduleBootstrapRetry();
            return false;
        }
        let generation = root.schedulerLaunchGeneration;
        let completedOp = root.schedulerLaunchOp;
        let applied = completedOp === "scan"
            ? root.finishScan(code, output, generation)
            : root.finishScheduler(code, output, generation);
        root._launched = false;
        root._started = false;
        root.schedulerLaunchGeneration = -1;
        // Bootstrap failure (nonzero/malformed/available:false) leaves
        // bootstrapped false: count one backoff retry. Valid statuses
        // (incl disabled) already set bootstrapped and stopped the timer
        // in finishScheduler.
        if (!root.bootstrapped && completedOp === "status") root.scheduleBootstrapRetry();
        return applied;
    }

    // Failed-start reconciliation (thin onRunningChanged delegates here).
    // A helper that never starts (missing binary) emits no exited, so
    // running going false without started is the only signal. Uses the
    // frozen launchGeneration like the exit path; a retiring window owns
    // reconciliation instead.
    function handleSchedulerRunningChanged() {
        if (schedulerProcess.running) return false;
        if (!root._launched || root._started) return false;
        schedulerWatchdog.stop();
        schedulerKillTimer.stop();
        if (root.retiring) {
            let retiredOp = root.schedulerLaunchOp;
            root._launched = false;
            root.schedulerLaunchGeneration = -1;
            root.retiring = false;
            // Failed start during a retiring window still owns
            // reconciliation: count one backoff retry while unbootstrapped.
            if (!root.bootstrapped && retiredOp === "status") root.scheduleBootstrapRetry();
            return false;
        }
        let generation = root.schedulerLaunchGeneration;
        let completedOp = root.schedulerLaunchOp;
        let applied = completedOp === "scan"
            ? root.finishScan(1, "", generation)
            : root.finishScheduler(1, "", generation);
        root._launched = false;
        root.schedulerLaunchGeneration = -1;
        // Failed start (missing binary emits no exited) while
        // unbootstrapped: same backoff as a failed exit.
        if (!root.bootstrapped && completedOp === "status") root.scheduleBootstrapRetry();
        return applied;
    }

    // Serialized launch: never overlap a running helper, a retiring one,
    // or the runningChanged-before-exited window. Bootstrap (status),
    // periodic (tick), and manual scan share this one Process with
    // list-form argv.
    function launchScheduler(op) {
        if (schedulerProcess.running || root.retiring || root._launched) return false;
        if (op !== "status" && op !== "tick" && op !== "scan") return false;
        root.schedulerGeneration = Number(root.schedulerGeneration) + 1;
        root.schedulerOp = op;
        root.schedulerLaunchGeneration = Number(root.schedulerGeneration);
        root.schedulerLaunchOp = op;
        root._launched = true;
        root._started = false;
        if (op === "scan") {
            root.scanNotice = "";
            schedulerProcess.command = ["python3", Quickshell.shellPath("scripts/memory_tick.py"), "run", "--job", "scan"];
        } else {
            schedulerProcess.command = ["python3", Quickshell.shellPath("scripts/memory_tick.py"), op];
        }
        schedulerWatchdog.restart();
        schedulerProcess.running = true;
        return true;
    }

    // Bootstrap: read settings via Python (status returns the enabled
    // dictionary with all settings). Called once on startup; ticks start
    // only after it applies.
    function requestStatus() {
        return root.launchScheduler("status");
    }

    // Periodic tick: no-op while unbootstrapped, disabled, retiring, or
    // in flight. Disabled stops the timer (see tickTimer.running), so this
    // guard is the second layer for direct calls.
    function requestTick() {
        if (!root.bootstrapped || !root.schedulerEnabled) return false;
        return root.launchScheduler("tick");
    }

    // Manual capture scan: only when bootstrapped (settings known);
    // serialized by the single Process/guards via launchScheduler.
    // Clears scanNotice at launch; completion sets the bounded line.
    function requestScan() {
        if (!root.bootstrapped) return false;
        return root.launchScheduler("scan");
    }

    // Bounded watchdog: the tick pipeline may legitimately run long —
    // up to 5 closed sessions per tick (plan §6), each with one bounded
    // Jev call (15 s timeout) plus one bounded retry on 429/5xx, i.e.
    // worst case 5 × (15 s + 15 s) = 150 s plus local evidence/draft work.
    // The watchdog therefore allows 180 s; the generic 12 s helper timeout
    // used elsewhere would abort legitimate ticks. Mark retiring, send
    // SIGTERM (asynchronous: the process may outlive this call). The frozen
    // launchGeneration is kept so the late exit recognizes itself and drops
    // its dataChanged; the kill timer escalates if SIGTERM is ignored. No
    // error is surfaced. Coordinator contract: memory_tick.py must fit its
    // overall tick budget inside this watchdog, while any coordinator-side
    // stuck-tick/overlap lease must be LONGER than the watchdog (≥ 240 s —
    // or lease-free and released by process death, which trivially holds)
    // so the QML watchdog stays the timeout owner and a killed tick is
    // never mistaken for a fresh completed one.
    function handleSchedulerTimeout() {
        if (!root._launched) {
            schedulerWatchdog.stop();
            // No helper in flight while still unbootstrapped (e.g. the
            // initial launch was overlapped away): queue a backoff retry so
            // bootstrap cannot stall.
            if (!root.bootstrapped) {
                let pendingOp = root.schedulerLaunchOp;
                // Only retry status bootstraps; ticks never run
                // unbootstrapped. An empty launchOp before any launch still
                // counts as a pending bootstrap.
                if (pendingOp === "status" || pendingOp === "") root.scheduleBootstrapRetry();
            }
            return false;
        }
        schedulerWatchdog.stop();
        root.retiring = true;
        schedulerProcess.running = false;
        schedulerKillTimer.restart();
        return true;
    }

    // SIGKILL escalation for a retiring helper that ignored SIGTERM. Owned
    // process only (Process.signal API, as in ControlCenterSystem); a no-op
    // once the actual exit has reconciled.
    function fireSchedulerKillTimeout() {
        if (root.retiring && schedulerProcess.running) {
            try {
                schedulerProcess.signal(9);
            } catch (error) {}
            return true;
        }
        return false;
    }

    Process {
        id: schedulerProcess
        workingDirectory: Quickshell.shellPath(".")
        stdinEnabled: false
        stdout: StdioCollector { id: schedulerOutput; waitForEnd: true }
        stderr: StdioCollector { id: schedulerError; waitForEnd: true }
        onStarted: {
            root._started = true;
        }
        onRunningChanged: {
            root.handleSchedulerRunningChanged();
        }
        onExited: (code) => {
            // waitForEnd collectors are complete here: hand the captured
            // stdout to the completion wrapper, which owns the frozen
            // schedulerLaunchGeneration. Never re-read the mutable counter
            // here: after a timeout it no longer identifies this launch.
            root.handleSchedulerExited(code, schedulerOutput.text);
        }
    }

    // Repeating tick: only while the bootstrapped status says enabled.
    // Disabled stops the timer entirely (running goes false).
    Timer {
        id: tickTimer
        objectName: "schedulerTick"
        interval: Math.max(10000, root.tickSeconds * 1000)
        repeat: true
        running: root.schedulerEnabled && root.bootstrapped
        onTriggered: root.requestTick()
    }

    Timer {
        id: schedulerWatchdog
        objectName: "schedulerWatchdog"
        interval: 180000
        repeat: false
        onTriggered: root.handleSchedulerTimeout()
    }

    // SIGTERM grace: escalate a retiring helper that ignores the watchdog
    // stop. Stopped by the actual exit (or failed-start) reconciliation.
    // 10 s grace (not the generic 3 s): a tick killed at the 180 s budget
    // may still be flushing short WAL sidecar writes; SIGKILL must not win
    // while that bounded flush is in flight, but must still fire before
    // the next tick interval can overlap it.
    Timer {
        id: schedulerKillTimer
        objectName: "schedulerKillTimer"
        interval: 10000
        repeat: false
        onTriggered: root.fireSchedulerKillTimeout()
    }

    // Dedicated bootstrap retry (one-shot, exponential backoff). The
    // repeating tickTimer only runs while bootstrapped && enabled (plan
    // §7A tick timer), so it can never retry the initial `status`: this
    // timer owns that window instead. It fires only while unbootstrapped,
    // serializes through the single shared Process via retryBootstrapStatus
    // (launch/retiring guards prevent overlap; a blocked launch requeues
    // the next backoff), stops after any valid status (enabled or disabled
    // both set bootstrapped and stop it in finishScheduler), and retries
    // indefinitely bounded by rate (failures capped at 5). Backoff:
    // 10s, 20s, 40s, 80s, 120s cap. Never surfaces modal errors.
    Timer {
        id: bootstrapRetryTimer
        objectName: "schedulerBootstrapRetry"
        interval: 10000
        repeat: false
        running: false
        onTriggered: root.retryBootstrapStatus()
    }

    Component.onCompleted: root.requestStatus()
}
