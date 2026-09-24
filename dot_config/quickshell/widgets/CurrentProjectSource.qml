import QtQuick
import Quickshell
import Quickshell.Io

// Single shell-level current-project source (S-054). One instance lives in
// shell.qml and owns exactly ONE resident `desktop_projects.py watch`
// process for the whole session; every per-bar CurrentProjectModule binds
// to these values. The watcher's stdout is an LF-delimited JSON line
// stream (one line at startup, one per change); lines are attributed to
// the live generation only, so a dying child's late/partial lines never
// relabel state after a restart. When the watcher exits for any reason
// the stream is over: the identity clears to the honest-unavailable state
// (backendOk=false) and the whole process restarts with bounded backoff
// (5 s -> 15 s -> 60 s cap). A separate slower 60 s timer refreshes the
// session-ledger badge via the shared DailyAgenda; the project state
// itself arrives on the resident stream, never on a poll. Popup/content
// behavior stays in CurrentProjectModule.
//
// Honest-unavailable semantics: backendOk===false means the stream is
// down or a line failed validation (identity cleared); hasProject===false
// with backendOk===true is a valid "No project".
Item {
    id: root

    // Shared DailyAgenda for the guarded badge refresh.
    property var agenda: null

    property string projectId: ""
    property string projectName: ""
    property string projectStatus: ""
    property bool hasProject: false
    // False only when the stream is down or a line failed validation.
    // Distinct from hasProject===false, which is a valid "No project".
    property bool backendOk: true
    // Monotonic start counter, bumped on every (re)start of the resident
    // watcher (diagnostics only). Stream lines carry the generation they
    // were read under; a line for a stale generation is dropped.
    property int currentGeneration: 0
    // True while the resident child is the authority for this state.
    // Cleared the moment the child dies (or fails to start) so late or
    // partial lines can never relabel state; set again on (re)start.
    property bool watchLive: false
    // Consecutive unexpected deaths without sustained liveness between
    // them; drives the bounded restart backoff. Reset ONLY when a dead
    // generation survived >= 30 s (see handleWatchDeath): a valid line
    // alone must not reset the streak, or a child that emits its startup
    // line then exits every time would hold the backoff at 5 s forever
    // (emit-then-crash spawn loop).
    property int consecutiveFailures: 0
    // Wall-clock ms of the last (re)start, for the sustained-liveness
    // rule above. 30000 ms: comfortably above a crash loop's lifetime,
    // comfortably below a healthy resident's.
    property double watchStartedAt: 0
    property bool _watchStarted: false
    property bool _startRequested: false

    // Pure object validator mirroring CurrentProjectModule so both
    // surfaces accept the same identities: status must be one of the
    // four known shapes with a matching project slot; anything else
    // (wrong/missing type tag, unknown status, bad id/name, non-null
    // project alongside an empty status) => {ok:false}. Extras allowed.
    function parseCurrentProjectData(data) {
        if (!data || typeof data !== "object" || Array.isArray(data)) return { ok: false };
        if (data.hasOwnProperty("type") && data.type !== "current-project") return { ok: false };
        if (typeof data.status !== "string") return { ok: false };
        let status = data.status;
        let project = data.hasOwnProperty("project") ? data.project : undefined;
        if (status === "associated" || status === "name-only-no-linkage") {
            if (!project || typeof project !== "object" || Array.isArray(project)) return { ok: false };
            if (typeof project.id !== "string" || project.id === "") return { ok: false };
            if (typeof project.name !== "string") return { ok: false };
            return { ok: true, hasProject: true, id: project.id, name: project.name, status: status };
        }
        if (status === "unassociated" || status === "stale-removed") {
            if (project !== null && project !== undefined) return { ok: false };
            return { ok: true, hasProject: false, id: "", name: "", status: status };
        }
        return { ok: false };
    }

    // Pure line parser: one LF-delimited stream line => validation
    // result. Malformed JSON or a non-object payload => {ok:false} with
    // the same strictness as parseCurrentProjectData.
    function parseCurrentProject(line) {
        let data = null;
        try {
            data = JSON.parse(String(line));
        } catch (error) {
            return { ok: false };
        }
        return root.parseCurrentProjectData(data);
    }

    // Bounded restart backoff from the consecutive-death count:
    // 5 s after the first death, 15 s after the second, 60 s cap after.
    function restartDelayMs(failures) {
        let count = Number(failures);
        if (!(count >= 1)) return 5000;
        if (count === 1) return 5000;
        if (count === 2) return 15000;
        return 60000;
    }

    // Generation-guarded line handling: only the live generation while
    // the watch is live may relabel state. Blank lines (a trailing
    // newline's empty chunk) are ignored without touching state. A valid
    // payload applies the identity and marks the backend ok; an invalid
    // line clears the stale identity to the honest-unavailable state.
    // A valid line deliberately does NOT reset consecutiveFailures (see
    // the property comment): only sustained liveness in handleWatchDeath
    // proves the stream healthy. Returns true only when a valid payload
    // was applied.
    function handleCurrentProjectLine(line, generation) {
        if (Number(generation) !== Number(root.currentGeneration)) return false;
        if (!root.watchLive) return false;
        let text = String(line === undefined || line === null ? "" : line);
        if (text.trim() === "") return false;
        let parsed = root.parseCurrentProject(text);
        if (!parsed.ok) {
            root.backendOk = false;
            root.hasProject = false;
            root.projectId = "";
            root.projectName = "";
            root.projectStatus = "";
            return false;
        }
        root.backendOk = true;
        root.hasProject = parsed.hasProject;
        root.projectId = parsed.hasProject ? String(parsed.id || "") : "";
        root.projectName = parsed.hasProject ? String(parsed.name || "") : "";
        root.projectStatus = String(parsed.status || "");
        return true;
    }

    // (Re)start the single resident watcher. Never overlaps a running
    // child; bumps the generation so pre-restart lines are stale, and
    // stamps watchStartedAt for the sustained-liveness rule.
    function startWatchProcess() {
        if (watchProcess.running) return false;
        root.currentGeneration = Number(root.currentGeneration) + 1;
        root.watchLive = true;
        root.watchStartedAt = Date.now();
        root._watchStarted = false;
        root._startRequested = true;
        watchRestartTimer.stop();
        watchProcess.command = ["python3", "-u", Quickshell.shellPath("scripts/desktop_projects.py"), "watch"];
        watchProcess.running = true;
        return true;
    }

    // Stream-over handling (idempotent via watchLive): any exit, for any
    // reason, ends the stream, so the identity clears to the
    // honest-unavailable state and the whole process restarts with
    // bounded backoff. The death count resets only on SUSTAINED liveness:
    // when the dead generation survived >= 30 s this death starts a fresh
    // streak (an emit-then-crash loop never survives that long, so its
    // streak keeps escalating 5 s -> 15 s -> 60 s cap instead). The
    // optional `now` override is a test seam; live callers pass nothing
    // and the wall clock is used. Returns true only on the first call
    // per death.
    function handleWatchDeath(now) {
        if (!root.watchLive) return false;
        root.watchLive = false;
        root._startRequested = false;
        root._watchStarted = false;
        root.backendOk = false;
        root.hasProject = false;
        root.projectId = "";
        root.projectName = "";
        root.projectStatus = "";
        let at = (now === undefined || now === null) ? Date.now() : Number(now);
        if (at - Number(root.watchStartedAt) >= 30000) root.consecutiveFailures = 0;
        root.consecutiveFailures = Number(root.consecutiveFailures) + 1;
        watchRestartTimer.interval = root.restartDelayMs(root.consecutiveFailures);
        watchRestartTimer.restart();
        return true;
    }

    // Failed-start reconciliation (thin onRunningChanged delegates here):
    // running went false without ever starting after we asked for it.
    // A started-then-stopped child is owned by onExited instead.
    function handleWatchRunningChanged() {
        if (watchProcess.running) return false;
        if (!root._startRequested || root._watchStarted) return false;
        return root.handleWatchDeath();
    }

    // Slower badge cadence: delegates to the shared DailyAgenda's guarded
    // inbox read. Never throws; false when there is no agenda.
    function refreshLedgerInboxBadge() {
        if (!root.agenda) return false;
        try {
            if (typeof root.agenda.refreshLedgerInboxBadge === "function") return !!root.agenda.refreshLedgerInboxBadge();
            return false;
        } catch (error) {
            return false;
        }
    }

    Process {
        id: watchProcess
        workingDirectory: Quickshell.shellPath(".")
        // Hold a stdin pipe (never written): quickshell teardown closes it,
        // the bridge sees EOF and SIGTERMs its Rust child, so a supervisor
        // that dies without SIGTERM can no longer orphan either half.
        stdinEnabled: true
        stdout: SplitParser {
            id: watchSplitter
            splitMarker: "\n"
            onRead: (data) => {
                root.handleCurrentProjectLine(data, root.currentGeneration);
            }
        }
        // Bounded stderr: log-and-drop per line. Accumulating the whole
        // lifetime stderr of the resident child in memory is unbounded;
        // the watcher is quiet on stderr, so each line is surfaced once
        // in the quickshell log and released.
        stderr: SplitParser {
            onRead: data => console.warn("current-project watch stderr: " + data)
        }
        onStarted: {
            root._watchStarted = true;
        }
        onRunningChanged: {
            root.handleWatchRunningChanged();
        }
        onExited: {
            root.handleWatchDeath();
        }
    }

    // Bounded-backoff restart for the resident watcher. Interval is set
    // per death by handleWatchDeath (5 s -> 15 s -> 60 s cap).
    Timer {
        id: watchRestartTimer
        objectName: "watchRestartTimer"
        interval: 5000
        repeat: false
        onTriggered: root.startWatchProcess()
    }

    // Ledger badge refresh on its own slower cadence (60 s); the project
    // state itself arrives on the resident stream, never on a poll.
    Timer {
        id: ledgerBadgePoll
        objectName: "ledgerBadgePoll"
        interval: 60000
        repeat: true
        running: true
        onTriggered: root.refreshLedgerInboxBadge()
    }

    Component.onCompleted: {
        root.startWatchProcess();
        root.refreshLedgerInboxBadge();
    }
}
