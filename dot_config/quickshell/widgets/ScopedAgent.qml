import QtQuick
import Quickshell
import Quickshell.Io

// Scoped agent command adapter over the Rust orchestrator bridge.
// The bridge owns raw Pi JSONL framing, request correlation, the scoped
// session/history refresh state machine, streaming normalization, approval
// expiry and the Pi child lifecycle. This file only projects snapshots,
// tracks per-UI ack pending overlays with immediate local guards, and
// forwards signals. No Pi RPC parsing lives here.
Item {
    id: root
    visible: false

    // Factory configuration (frozen for the bridge command).
    // paletteMode is an explicit unscoped general session. scopedMode stays
    // !!projectPath||journalMode (actual scope); paletteMode must not be
    // combined with projectPath/journalMode.
    property string projectPath: ""
    property bool journalMode: false
    property bool paletteMode: false
    readonly property bool scopedMode: !!projectPath || journalMode

    // Frozen spawn identity for the bridge CLI. Refreshed from the latest
    // authoritative session on every deliberate start() spawn, then frozen
    // for the lifetime of that process. Live sessionFile below is a
    // snapshot projection and must never feed a running command.
    property string _initSession: ""
    property string _initName: ""
    property bool _initFresh: false
    property bool _bridgeLaunched: false
    // Actual bridge Process tracking per launch. Never derived from the Pi
    // projection (processStarted): Rust terminal transport failures emit
    // processStarted:false while a restore refresh is still pending, which
    // must NOT be classified as failed-to-start.
    property bool _bridgeProcessStarted: false
    property bool _bridgeExitHandled: false

    // Ack pending overlay: local acceptance means queued. A snapshot that
    // arrives before its ack must not clear these optimistic gates.
    property var _pendingAcks: ({})
    property int _pendingCount: 0
    property int _uiSerial: 0
    property var _queuedOps: []

    // Optimistic in-flight gates (cleared only on ack or bridge death).
    property bool _optBusy: false
    property bool _optControl: false
    property bool _optSwitching: false
    property bool _optCompacting: false
    property bool _optStopping: false
    property bool _optRefresh: false

    // Bridge-projected base values (assigned from snapshots only).
    property bool _bReady: false
    property bool _bBusy: false
    property bool _bCompacting: false
    property bool _bControl: false
    property bool _bSwitching: false
    property bool _bStopping: false
    property bool _bRefresh: false

    // Effective gates consumed by ProjectPlanner/JournalAssistant.
    property bool busy: _bBusy || _optBusy
    property bool compacting: _bCompacting || _optCompacting
    property bool controlPending: _bControl || _optControl
    property bool sessionSwitching: _bSwitching || _optSwitching
    property bool stopping: _bStopping || _optStopping
    property bool sessionRefreshPending: _bRefresh || _optRefresh
    readonly property bool ready: _bReady && _pendingCount === 0

    // Plain snapshot projections (writable so planner restore fallbacks keep working).
    property string sessionFile: ""
    property string sessionName: ""
    property bool freshSession: false
    property var model: null
    property var models: []
    property var commands: []
    property bool stateOk: false
    property bool extensionOk: false
    property bool sessionRefreshFailed: false
    property int sessionRefreshGeneration: 0
    property bool sessionChangeInFlight: false
    property string sessionChangeKind: ""
    property string sessionChangeRequestId: ""
    property bool sessionChangeCancelRequested: false
    property string answer: ""
    property var messages: []
    property string messagesSessionFile: ""
    property int messagesGeneration: 0
    property bool messagesAwaitingSessionState: false
    property string status: "Press Retry to start the agent…"
    property var pendingApproval: null
    property var pendingRequests: ({})
    // Authoritative history signal: get_state clears
    // messagesAwaitingSessionState/sessionRefreshPending BEFORE the queued
    // get_messages responds, so an empty messages cache after stateUpdated
    // must never be treated as a fresh session. historyLoadedValid is true
    // only after a correlated successful get_messages for the CURRENT
    // (sessionFile, messagesGeneration); any historyFailed clears it.
    // Per-worker (not planner-global) so background/cached workers and
    // startup each gate independently.
    property string historyLoadedSessionFile: ""
    property int historyLoadedGeneration: -1
    property bool historyLoadedValid: false
    property int serial: 0
    property int generation: 0
    property string lastRequestId: ""
    property bool processStarted: false
    property bool processStartFailed: false
    property bool startRequested: false
    property bool launchAttempted: false
    property bool idleStopped: false
    property bool pendingResume: false
    property bool desiredRunning: false
    readonly property bool retryable: _retryableBase
    property bool _retryableBase: false
    property bool idleStopping: false
    property string diagnostic: ""
    property string statsText: ""

    signal textDelta(string text)
    signal finished()
    signal failed(string message)
    signal uiRequest(var request)
    signal stateUpdated()
    signal statsChanged(var stats)
    signal historyFailed(string message, string sessionFile, int generation)
    signal historyLoaded(string sessionFile, int generation)
    // Operation completion keyed by the UI id returned from prompt() and
    // the other command functions. Callers keep their submitted draft until
    // an accepted ack arrives; rejected acks and bridge death preserve it.
    signal opFinished(string id, string op, bool accepted, string message)
    signal bridgeDead()

    function json(value) {
        return JSON.stringify(value) + "\n";
    }

    function buildErrorMessage() {
        return "Agent bridge missing — run: cargo build --locked --release --manifest-path services/agent-orchestrator/Cargo.toml";
    }

    function bridgePath() {
        return Quickshell.shellPath("services/agent-orchestrator/target/release/qs-agent-orchestrator");
    }

    function bridgeArgs() {
        // Palette takes precedence when conflicting flags are combined, so a
        // misconfigured factory can never leak --project into palette mode.
        let mode = root.paletteMode ? "palette" : (root.journalMode ? "journal" : "project");
        let args = [bridgePath(), "--root", Quickshell.shellPath("."),
            "--mode", mode];
        if (mode === "project") args.push("--project", root.projectPath);
        if (root._initSession) args.push("--session", root._initSession);
        if (root._initName) args.push("--pending-name", root._initName);
        if (root._initFresh) args.push("--new-session");
        return args;
    }

    function hasPending(op) {
        for (let key of Object.keys(_pendingAcks)) if (_pendingAcks[key] === op) return true;
        return false;
    }

    function trackPending(op) {
        _uiSerial++;
        let id = "ui-" + _uiSerial;
        let copy = Object.assign({}, _pendingAcks);
        copy[id] = op;
        _pendingAcks = copy;
        _pendingCount = Object.keys(copy).length;
        return id;
    }

    function clearPending(id) {
        if (!_pendingAcks[id]) return "";
        let op = _pendingAcks[id];
        let copy = Object.assign({}, _pendingAcks);
        delete copy[id];
        _pendingAcks = copy;
        _pendingCount = Object.keys(copy).length;
        if (op === "prompt") _optBusy = false;
        else if (op === "newSession" || op === "rename") { _optControl = false; _optRefresh = false; }
        else if (op === "switchSession") { _optSwitching = false; _optBusy = false; _optRefresh = false; }
        else if (op === "compact") { _optControl = false; _optCompacting = false; }
        else if (op === "chooseModel") _optControl = false;
        else if (op === "abort" || op === "stopIdle") _optStopping = false;
        return op;
    }

    function dropPendingNoReplay() {
        // Death must never replay queued commands: drop them silently.
        // Callers are notified via opFinished(false) in handleBridgeDead.
        _queuedOps = [];
    }

    function refreshSpawnIdentity() {
        // Refresh from the latest authoritative cache before each deliberate
        // spawn. When a refresh was unresolved the identity was already
        // cleared by handleBridgeDead, so this never reuses a stale file.
        _initSession = sessionFile;
        _initName = sessionName;
        _initFresh = freshSession;
    }

    function sendOp(op, args) {
        // Never launches the bridge. Only start() may spawn/restart; late
        // prompt/respond/requestMessages after death simply return "".
        if (!bridgeProc.running) return "";
        let id = trackPending(op);
        let payload = { version: 1, id: id, op: op, args: args || {} };
        try { bridgeProc.write(json(payload)); } catch (error) {
            clearPending(id);
            return "";
        }
        return id;
    }

    function start() {
        // The ONLY explicit spawner. Never auto-starts Pi otherwise; the
        // bridge does that on this op. Command args are frozen per process
        // via refreshSpawnIdentity() above. A repeated start while one is
        // already pending is gated so idle-startup callers cannot spam ops.
        if (bridgeProc.running) {
            if (hasPending("start")) return false;
            let live = sendOp("start", {});
            return live;
        }
        refreshSpawnIdentity();
        let id = trackPending("start");
        let payload = { version: 1, id: id, op: "start", args: {} };
        let queue = _queuedOps.slice();
        queue.push({ id: id, payload: payload });
        _queuedOps = queue;
        _bridgeLaunched = true;
        _bridgeProcessStarted = false;
        _bridgeExitHandled = false;
        launchAttempted = true;
        startRequested = true;
        processStartFailed = false;
        _retryableBase = false;
        status = "Starting pi…";
        bridgeProc.command = bridgeArgs();
        bridgeProc.running = true;
        return id;
    }

    function stopIdle() {
        if (busy || pendingApproval !== null || controlPending || stopping ||
                compacting || sessionSwitching || sessionRefreshPending)
            return false;
        if (_pendingCount > 0) return false;
        let id = sendOp("stopIdle", {});
        if (!id) return false;
        return id;
    }

    function prompt(message, images) {
        message = (message || "").trim();
        if (!ready || compacting || stopping || controlPending || sessionSwitching ||
                !message || sessionRefreshPending) return false;
        let args = { message: message };
        if (images && images.length) args.images = images;
        _optBusy = true;
        let id = sendOp("prompt", args);
        if (!id) { _optBusy = false; return false; }
        return id;
    }

    function abort() {
        // Stop sets the local stopping gate immediately so the overlay
        // cannot be dismissed by an unrelated snapshot before the ack.
        _optStopping = true;
        let id = sendOp("abort", {});
        if (!id) { _optStopping = false; return ""; }
        return id;
    }

    function newSession() {
        if (busy || compacting || stopping || controlPending || sessionSwitching ||
                sessionRefreshPending || !ready) return false;
        _optControl = true;
        _optRefresh = true;
        let id = sendOp("newSession", {});
        if (!id) { _optControl = false; _optRefresh = false; return false; }
        return id;
    }

    function switchSession() {
        if (busy || compacting || stopping || controlPending || sessionSwitching ||
                sessionRefreshPending || !ready) return false;
        _optSwitching = true;
        _optBusy = true;
        _optRefresh = true;
        let id = sendOp("switchSession", {});
        if (!id) { _optSwitching = false; _optBusy = false; _optRefresh = false; return false; }
        return id;
    }

    function rename(name) {
        name = (name || "").trim();
        if (!name || busy || compacting || stopping || controlPending || sessionSwitching ||
                sessionRefreshPending || !ready) return false;
        _optControl = true;
        _optRefresh = true;
        let id = sendOp("rename", { name: name });
        if (!id) { _optControl = false; _optRefresh = false; return false; }
        return id;
    }

    function compact() {
        if (busy || compacting || stopping || controlPending || sessionSwitching ||
                sessionRefreshPending || !ready) return false;
        _optControl = true;
        _optCompacting = true;
        let id = sendOp("compact", {});
        if (!id) { _optControl = false; _optCompacting = false; return false; }
        return id;
    }

    function chooseModel(item) {
        if (!item || busy || compacting || stopping || controlPending || sessionSwitching ||
                sessionRefreshPending || !ready) return false;
        _optControl = true;
        let id = sendOp("chooseModel", { item: item });
        if (!id) { _optControl = false; return false; }
        return id;
    }

    function respond(requestId, fields) {
        if (!requestId || !pendingApproval || pendingApproval.id !== requestId) return false;
        let out = {};
        for (let key of Object.keys(fields || {})) out[key] = fields[key];
        let id = sendOp("respond", { requestId: requestId, fields: out });
        if (!id) return false;
        return id;
    }

    function requestMessages() {
        // Guard: only a live desired worker without stop/refresh may ask.
        if (!bridgeProc.running || !processStarted || !desiredRunning) return false;
        if (idleStopping || idleStopped || stopping) return false;
        if (controlPending || sessionSwitching || sessionRefreshPending) return false;
        if (retryable) return false;
        let id = sendOp("requestMessages", {});
        if (!id) return false;
        return id;
    }

    function requestStats() {
        let id = sendOp("requestStats", {});
        if (!id) return false;
        return id;
    }

    function noteHistoryLoaded(sessionFile, generation) {
        historyLoadedSessionFile = String(sessionFile || "");
        historyLoadedGeneration = Number(generation || 0);
        historyLoadedValid = true;
    }

    function noteHistoryFailed() {
        historyLoadedValid = false;
    }

    function request(type, fields) {
        // Allowlisted palette compatibility: only get_session_stats maps to
        // the bridge requestStats op. No raw Pi RPC is constructed here.
        if (type === "get_session_stats") return requestStats();
        return false;
    }

    function textFromMessage(message) {
        if (!message) return "";
        if (typeof message.text === "string" && message.text) return message.text;
        if (typeof message.content === "string") return message.content;
        let out = "";
        for (let part of (message.content || []))
            if (part && part.type === "text" && typeof part.text === "string") out += part.text;
        return out;
    }

    // Pure local display filter for the scoped worker/bridge. Never touches the
    // bridge; only projects already-normalized history for the palette.
    function filterMessages(values, query) {
        let needle = String(query || "").trim().toLowerCase();
        return (values || []).filter(message => !needle ||
            String(message.text !== undefined && message.text !== null
                ? message.text : textFromMessage(message) || "")
                .toLowerCase().indexOf(needle) >= 0);
    }

    function applyState(state) {
        if (!state || typeof state !== "object") return;
        // Render the snapshot before emitting any signal. Identity,
        // generation and gates are projected BEFORE the messages array so
        // QML property notifies never observe messages with stale identity
        // (every snapshot assigns a fresh messages array, including
        // ACK/stats snapshots unrelated to history).
        _bReady = !!state.ready;
        _bBusy = !!state.busy;
        _bCompacting = !!state.compacting;
        _bControl = !!state.controlPending;
        _bSwitching = !!state.sessionSwitching;
        _bStopping = !!state.stopping;
        _bRefresh = !!state.sessionRefreshPending;
        sessionFile = state.sessionFile || "";
        sessionName = state.sessionName || "";
        freshSession = !!state.freshSession;
        model = state.model !== undefined ? state.model : null;
        models = state.models || [];
        commands = state.commands || [];
        stateOk = !!state.stateOk;
        extensionOk = !!state.extensionOk;
        sessionRefreshFailed = !!state.sessionRefreshFailed;
        sessionRefreshGeneration = state.sessionRefreshGeneration || 0;
        sessionChangeInFlight = !!state.sessionChangeInFlight;
        sessionChangeKind = state.sessionChangeKind || "";
        sessionChangeRequestId = state.sessionChangeRequestId || "";
        sessionChangeCancelRequested = !!state.sessionChangeCancelRequested;
        answer = state.answer || "";
        messagesSessionFile = state.messagesSessionFile || "";
        messagesGeneration = state.messagesGeneration || 0;
        messagesAwaitingSessionState = !!state.messagesAwaitingSessionState;
        messages = state.messages || [];
        status = state.status || "";
        pendingApproval = state.pendingApproval === null || state.pendingApproval === undefined
            ? null : state.pendingApproval;
        pendingRequests = state.pendingRequests || ({});
        serial = state.serial || 0;
        generation = state.generation || 0;
        lastRequestId = state.lastRequestId || "";
        processStarted = !!state.processStarted;
        processStartFailed = !!state.processStartFailed;
        startRequested = !!state.startRequested;
        launchAttempted = !!state.launchAttempted;
        idleStopped = !!state.idleStopped;
        pendingResume = !!state.pendingResume;
        desiredRunning = !!state.desiredRunning;
        _retryableBase = !!state.retryable;
        idleStopping = !!state.idleStopping;
        diagnostic = state.diagnostic || "";
        statsText = state.statsText || "";
    }

    function handleBridgeDead() {
        // Explicit bridge-dead: clear ALL live gates/pending/approval and
        // optimistic overlays, emit uiRequest(null), require explicit Retry,
        // preserve the latest authoritative cached session or clear identity
        // when a refresh was unresolved. Never replays queued commands.
        let refreshUnresolved = messagesAwaitingSessionState || _bRefresh || _bSwitching ||
            _optRefresh || _optSwitching;
        let pendingCopy = {};
        for (let key of Object.keys(_pendingAcks)) pendingCopy[key] = _pendingAcks[key];
        _pendingAcks = ({});
        _pendingCount = 0;
        _optBusy = false;
        _optControl = false;
        _optSwitching = false;
        _optCompacting = false;
        _optStopping = false;
        _optRefresh = false;
        _bReady = false;
        _bBusy = false;
        _bCompacting = false;
        _bControl = false;
        _bSwitching = false;
        _bStopping = false;
        _bRefresh = false;
        pendingApproval = null;
        pendingRequests = ({});
        dropPendingNoReplay();
        processStarted = false;
        desiredRunning = false;
        idleStopped = false;
        idleStopping = false;
        pendingResume = false;
        startRequested = false;
        _retryableBase = true;
        processStartFailed = true;
        if (refreshUnresolved) {
            sessionFile = "";
            sessionName = "";
            freshSession = false;
            messages = [];
            messagesSessionFile = "";
            messagesAwaitingSessionState = false;
        }
        // Authoritative history is per-identity: a dead bridge with an
        // unresolved refresh must not retain a stale loaded signal.
        historyLoadedSessionFile = "";
        historyLoadedGeneration = -1;
        historyLoadedValid = false;
        status = "Agent bridge stopped — Retry to restart";
        for (let pid of Object.keys(pendingCopy)) {
            opFinished(pid, pendingCopy[pid], false, "Agent bridge stopped — no prompt was replayed.");
        }
        uiRequest(null);
        failed("Agent bridge stopped — Retry to restart. " + buildErrorMessage());
        _bridgeExitHandled = true;
        bridgeDead();
    }

    function handleFailedToStart() {
        // Missing binary or immediate spawn failure, even with no pending
        // ops (lazy Component launch). Actionable build command required.
        // Only for a bridge Process that never reached onStarted; never
        // classified from the Pi projection (processStarted).
        let pendingCopy = {};
        for (let key of Object.keys(_pendingAcks)) pendingCopy[key] = _pendingAcks[key];
        _pendingAcks = ({});
        _pendingCount = 0;
        _optBusy = false;
        _optControl = false;
        _optSwitching = false;
        _optCompacting = false;
        _optStopping = false;
        _optRefresh = false;
        _bReady = false;
        _bBusy = false;
        _bCompacting = false;
        _bControl = false;
        _bSwitching = false;
        _bStopping = false;
        _bRefresh = false;
        pendingApproval = null;
        pendingRequests = ({});
        dropPendingNoReplay();
        processStarted = false;
        desiredRunning = false;
        _retryableBase = true;
        processStartFailed = true;
        historyLoadedSessionFile = "";
        historyLoadedGeneration = -1;
        historyLoadedValid = false;
        status = buildErrorMessage();
        for (let pid of Object.keys(pendingCopy)) {
            opFinished(pid, pendingCopy[pid], false, buildErrorMessage());
        }
        uiRequest(null);
        failed(buildErrorMessage());
        _bridgeExitHandled = true;
        bridgeDead();
    }

    function handleUpdate(value) {
        if (!value || value.version !== 1 || value.type !== "update" || !value.state) return;
        applyState(value.state);
        if (value.ack && _pendingAcks[value.ack]) {
            let op = clearPending(value.ack);
            let accepted = value.accepted !== false;
            let message = "";
            if (!accepted) {
                for (let e of (value.events || [])) {
                    if (e && e.name === "failed") { message = String((e.args || [])[0] || ""); break; }
                }
                if (!message) message = status || "The request was rejected.";
            }
            opFinished(value.ack, op, accepted, message);
            if (value.accepted === false && op !== "respond") {
                // Rejected commands surface without clearing the caller's
                // draft synchronously; the bridge already sent a failed event
                // in most cases, but ensure one exists. requestMessages
                // rejections also surface as historyFailed with identity so
                // the retry UI clears its pending flag.
                let hasFailed = (value.events || []).some(e => e && e.name === "failed");
                if (op === "requestMessages") {
                    noteHistoryFailed();
                    historyFailed(message, sessionFile, messagesGeneration);
                    if (!hasFailed) failed(message);
                } else if (!hasFailed) {
                    failed(status || "The request was rejected.");
                }
            }
        }
        for (let event of (value.events || [])) {
            if (!event || !event.name) continue;
            let args = event.args || [];
            if (event.name === "textDelta") textDelta(String(args[0] || ""));
            else if (event.name === "finished") finished();
            else if (event.name === "failed") failed(String(args[0] || "Agent failed"));
            else if (event.name === "uiRequest") uiRequest(args[0] === undefined ? null : args[0]);
            else if (event.name === "stateUpdated") stateUpdated();
            else if (event.name === "statsChanged") statsChanged(args[0] === undefined ? ({}) : args[0]);
            else if (event.name === "historyFailed") {
                noteHistoryFailed();
                historyFailed(String(args[0] || ""), String(args[1] || ""), Number(args[2] || 0));
            }
            else if (event.name === "historyLoaded") {
                noteHistoryLoaded(String(args[0] || ""), Number(args[1] || 0));
                historyLoaded(String(args[0] || ""), Number(args[1] || 0));
            }
        }
    }

    Component.onCompleted: {
        // LAZY launch: never spawn the bridge here. The first explicit
        // start() refreshes the spawn identity from the latest session and
        // freezes command args for that process. Callers (agentFor/activate)
        // already start explicitly; failures stay retryable until Retry.
    }

    Component.onDestruction: {
        if (bridgeProc.running) {
            try { bridgeProc.write(json({ version: 1, id: "ui-shutdown", op: "shutdown", args: {} })); } catch (error) {}
        }
    }

    Process {
        id: bridgeProc
        workingDirectory: Quickshell.shellPath(".")
        stdinEnabled: true
        stdout: SplitParser {
            onRead: data => {
                try { root.handleUpdate(JSON.parse(data)); }
                catch (error) { root.failed("Invalid agent bridge output"); }
            }
        }
        stderr: SplitParser {
            onRead: data => { root.diagnostic = data.slice(-2048); }
        }
        onStarted: {
            // Actual bridge Process start tracking per launch. Flush queued
            // start/control ops in order; the bridge acks each.
            root._bridgeProcessStarted = true;
            root._bridgeExitHandled = false;
            let queue = root._queuedOps;
            root._queuedOps = [];
            for (let item of queue) {
                try { bridgeProc.write(root.json(item.payload)); } catch (error) {}
            }
        }
        onRunningChanged: {
            // Classify ONLY from actual bridge Process tracking, never the
            // Pi projection (processStarted). A Rust terminal transport
            // failure emits processStarted:false while refresh is pending;
            // that once-started death must reach handleBridgeDead, not here.
            if (!bridgeProc.running && root._bridgeLaunched && !root._bridgeProcessStarted) {
                // A bridge that never started (missing release binary) must
                // report an actionable build error even with no pending ops
                // from a lazy launch, not stick in Starting.
                if (!root._bridgeExitHandled) root.handleFailedToStart();
            }
        }
        onExited: (code, status) => {
            // The bridge owns the Pi child and reaps it on EOF/shutdown. An
            // unexpected bridge exit never replays prompts; surface Retry.
            // Queued prompts are dropped (no command replay on death).
            // Any once-started death routes to the common bridge-dead path
            // (preserving unresolved evidence, then invalidating identity).
            // A never-started exit is already handled as failed-to-start
            // with no double error.
            if (!root._bridgeLaunched) return;
            if (root._bridgeExitHandled) return;
            root._bridgeExitHandled = true;
            if (!root._bridgeProcessStarted) {
                root.handleFailedToStart();
                return;
            }
            root.handleBridgeDead();
        }
    }
}
