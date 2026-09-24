import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import "../theme"

// Compact active-project tray popup anchored to the bar tray item.
//
// PanelWindow (not PopupWindow) so embedded controls can receive
// keyboard input. Layer-shell OnDemand focus preserves click-to-focus:
// opening never steals focus; closing disables focus. Small floating
// surface: top+left anchored with screen-local margins derived from the
// tray item inside the bar window (CalendarPopout conventions).
//
// Data comes from one bounded scripts/project_overview.py call (open
// TODOs, bounded work-session time with an explicit scope label, latest
// session plus change-only evidence from the session sidecar: baseline
// commit at session start -> latest/current commit plus working tree).
// The AI recap runs as an isolated ephemeral no-tools pi subprocess
// (scripts/project_recap.py: --print --no-session --no-tools plus full
// discovery/context lockdown), never through a shared project agent:
// no transcript contamination and no tool calls from evidence.
// This popup is a view only: it never resolves a project worker and
// never prompts one. Sending instructions happens in the full planner
// ("Full planner" / "Open planner" entries). Closing hides only: it
// never aborts any agent and never kills the read-only helper fetches
// (late responses are dropped unless they match the view).
PanelWindow {
    id: root

    property var anchorItem: null
    // Stable bar ancestor for the TransformWatcher (bound to the Bar in
    // shell.qml). refreshPosition already tracks the anchor item's own
    // geometry; the watcher additionally fires on ancestor motion that
    // leaves the item's own x/y/width/height untouched.
    property var anchorScope: null
    property var projectPlanner: null
    property bool requestedOpen: false
    property bool closing: false
    // Single reveal progress (ControlCenter pattern): panel chrome
    // derives from it so an interrupted close reopens mid-fade.
    property real reveal: 0
    property int popupX: 8
    property int popupY: 48
    property int popupWidth: 430
    property int popupHeight: 600

    // Current project identity (stable registry id; never a stale id).
    property string projectId: ""
    property string projectName: ""

    // Overview load state (generation-safe async + watchdog).
    property bool overviewBusy: false
    property bool overviewStarted: false
    property bool overviewStartFailed: false
    property bool overviewRetiring: false
    property int overviewGeneration: 0
    property int overviewLaunchGeneration: 0
    property string overviewProjectId: ""
    property bool overviewLoaded: false
    property string overviewError: ""
    property var overviewTodos: []
    property int overviewTodosOpen: 0
    property int overviewTodosTotal: 0
    property string overviewTodosReason: ""
    // Tracked work time in milliseconds. Double, not int: a 32-bit int
    // overflows past ~24.8 days of accumulated tracked time.
    property double overviewWorkMs: 0
    property string overviewWorkScope: ""
    property int overviewWorkCount: 0
    property string overviewWorkReason: ""
    property string overviewLastSessionId: ""
    property var overviewLastSession: null
    property string overviewLastReason: ""
    // Change-only recap evidence for the latest session, from the
    // session sidecar via project_overview.py (another worker owns the
    // sidecar and baseline capture; this popup never persists sessions).
    // Recap is keyed by summaryKey (an active session's evidence changes
    // while it runs, so the session id alone is not a stable key).
    property bool overviewChangesAvailable: false
    property bool overviewHasBaseline: false
    property string overviewBaselineCommit: ""
    property string overviewLatestCommit: ""
    property string overviewSummaryKey: ""
    property string overviewEvidence: ""
    property string overviewChangesReason: ""
    // Overview freshness: same-project reopens reuse displayed data only
    // within the TTL, otherwise they refresh in the background while the
    // old data stays visible (overviewKeepStale).
    property double overviewFetchedAt: 0
    property int overviewTtlMs: 60000
    property bool overviewKeepStale: false
    // Isolated recap subprocess (scripts/project_recap.py). Mirrors the
    // overview fetch guards: immutable launch identity, newest-wins
    // queue drained after settle, watchdog plus kill escalation.
    property bool recapBusy: false
    property bool recapStarted: false
    property bool recapStartFailed: false
    property bool recapRetiring: false
    property int recapGeneration: 0
    property int recapLaunchGeneration: 0
    property string recapLaunchProjectId: ""
    property string recapLaunchKey: ""
    property string recapLaunchSessionId: ""
    // Overview Resume button (Workstream F1): explicit plan preview
    // (desktop_resume.py plan) + Confirm execute with palette-equal
    // generation/staleness guards, then a planner handoff with action
    // "resume". Never automatic on open/refresh; operation filtering
    // is F2 out of scope. Never modal, never blocking.
    property bool resumePlanBusy: false
    property bool resumePlanStarted: false
    property bool resumePlanStartFailed: false
    property bool resumePlanRetiring: false
    property int resumePlanGeneration: 0
    property int resumePlanLaunchGeneration: 0
    property string resumePlanLaunchProjectId: ""
    property var resumePlan: null
    property bool resumeExecuteBusy: false
    property bool resumeExecuteStarted: false
    property bool resumeExecuteStartFailed: false
    property bool resumeExecuteRetiring: false
    property int resumeExecuteGeneration: 0
    property int resumeExecuteLaunchGeneration: 0
    property string resumeExecuteLaunchProjectId: ""
    property string resumeStatus: ""
    // Per-project recap cache so a switch never mixes summaries.
    // Visible summary fields mirror the current projectId entry.
    property var summaries: ({})
    property string summaryText: ""
    property string summaryState: "idle"
    property string statusText: ""
    // Newest overview fetch requested while another is in flight. Drained
    // after settle; dropped when the popup is closed or superseded.
    property string overviewQueuedProjectId: ""
    // Summary key already attempted for the current overview data. Blocks
    // repeated auto-summary prompts (accept+finish, reject, or failure);
    // cleared on new overview data, project switch, or manual retry.
    property string summaryAttemptedKey: ""

    // Palette-style planner handoff for the overview Resume flow.
    signal projectPlanningRequested(string projectId, string action, string message)

    readonly property var anchorWindow: anchorItem ? anchorItem.QsWindow.window : null
    screen: anchorWindow && anchorWindow.screen ? anchorWindow.screen : (Quickshell.screens.length > 0 ? Quickshell.screens[0] : null)

    anchors {
        top: true
        left: true
    }
    margins {
        left: root.popupX
        top: root.popupY
    }

    implicitWidth: popupWidth
    implicitHeight: popupHeight
    color: Theme.transparent
    visible: false

    exclusionMode: ExclusionMode.Ignore
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: root.requestedOpen && !root.closing ? WlrKeyboardFocus.OnDemand : WlrKeyboardFocus.None

    // -- Pure helpers (no root refs where noted; unit-tested via node) --

    // Human duration for tracked work time. Pure over numbers.
    function formatDurationMs(value) {
        let ms = Number(value || 0);
        if (!isFinite(ms) || ms < 0) ms = 0;
        let minutes = Math.floor(ms / 60000);
        if (minutes < 60) return minutes + "m";
        let hours = Math.floor(minutes / 60);
        let rest = minutes % 60;
        if (hours < 48) return hours + "h " + rest + "m";
        let days = Math.floor(hours / 24);
        return days + "d " + (hours % 24) + "h";
    }

    // Cache key for the auto recap: project + sidecar summary key. The
    // summary key (not the session id alone) versions the recap because
    // an active session's evidence changes while it runs.
    function summaryCacheKey(projectId, summaryKey) {
        return String(projectId || "") + "\u0000" + String(summaryKey || "");
    }

    // Short display form of a commit hash (first 7 chars when hex-like,
    // else a bounded raw string). Pure over strings.
    function shortCommit(commit) {
        let raw = String(commit || "").trim();
        if (!raw) return "";
        if (/^[0-9a-fA-F]{7,64}$/.test(raw)) return raw.substring(0, 7);
        return raw.substring(0, 12);
    }

    // Model override for the isolated recap. The popup no longer
    // resolves the shared project worker (view only, §2.3), so there is
    // no verified provider/id shape to borrow: always "" (the helper's
    // default model), which keeps recap quality attributable to the
    // helper default rather than a silently dropped override. Kept as a
    // function so the sendRecap call site and its --model plumbing stay
    // intact. Pure; never throws.
    function recapModelFor() {
        return "";
    }

    // Pure payload parser (no root refs): code !== 0, empty, malformed,
    // or unknown shapes => {ok:false}. Never throws.
    function parseOverview(output, code) {
        if (code !== 0) return { ok: false };
        let data = null;
        try {
            data = JSON.parse(output || "");
        } catch (error) {
            return { ok: false };
        }
        if (!data || typeof data !== "object" || Array.isArray(data)) return { ok: false };
        if (typeof data.requested_project_id !== "string" || !data.requested_project_id) return { ok: false };
        return { ok: true, data: data };
    }

    // Pure auto-summary gate (no root refs). The recap is an isolated
    // subprocess, so shared-worker busyness/approvals never block it;
    // missing change evidence means no call (never fall back to window
    // history); a cached recap, a consumed attempt, or an in-flight
    // recap suppresses regeneration.
    function shouldAutoSummarize(args) {
        if (!args || typeof args !== "object") return false;
        if (!args.popupOpen) return false;
        if (!args.overviewLoaded) return false;
        if (!args.hasChanges) return false;
        if (args.cached) return false;
        if (args.attempted) return false;
        if (args.recapBusy) return false;
        return true;
    }

    // Pure recap payload parser (no root refs): code !== 0 surfaces the
    // bounded helper diagnostic, malformed or shape-unknown payloads are
    // rejected. Never throws, never includes prompt/evidence text.
    function parseRecap(output, diagnostic, code) {
        if (code !== 0) {
            let detail = String(diagnostic || "").replace(/\s+/g, " ").trim().substring(0, 300);
            return { ok: false, error: detail || "Recap failed." };
        }
        let data = null;
        try {
            data = JSON.parse(output || "");
        } catch (error) {
            return { ok: false, error: "Recap response was invalid." };
        }
        if (!data || typeof data !== "object" || Array.isArray(data)) {
            return { ok: false, error: "Recap response was invalid." };
        }
        if (typeof data.summary !== "string" || !data.summary.trim()) {
            return { ok: false, error: "Recap returned no summary." };
        }
        if (typeof data.summary_key !== "string" || !data.summary_key) {
            return { ok: false, error: "Recap response was invalid." };
        }
        return { ok: true, data: data };
    }

    function parseResumePlan(output, diagnostic, code) {
        if (Number(code) !== 0) {
            let detail = String(diagnostic || "").replace(/\s+/g, " ").trim().substring(0, 300);
            return { ok: false, error: detail || "Resume failed." };
        }
        let data = null;
        try {
            data = JSON.parse(output || "");
        } catch (error) {
            return { ok: false, error: "Resume returned invalid data" };
        }
        if (!data || typeof data !== "object" || Array.isArray(data)) {
            return { ok: false, error: "Resume returned invalid data" };
        }
        if (Number(data.version) !== 1) {
            return { ok: false, error: "Resume returned invalid data" };
        }
        if (!data.project || typeof data.project !== "object" || Array.isArray(data.project)) {
            return { ok: false, error: "Resume returned invalid data" };
        }
        if (typeof data.project.id !== "string" || !data.project.id) {
            return { ok: false, error: "Resume returned invalid data" };
        }
        if (!Array.isArray(data.operations)) {
            return { ok: false, error: "Resume returned invalid data" };
        }
        return { ok: true, data: data };
    }

    // Pure resume plan command builder (no root refs): list-form argv
    // for the read-only plan fetch. Empty project yields []. Never
    // throws, never filters operations (F2 out of scope).
    function resumePlanCommand(projectId) {
        try {
            let id = String(projectId || "").trim();
            if (!id) return [];
            return ["python3", "scripts/desktop_resume.py", "plan", "--project", id];
        } catch (error) { return []; }
    }

    // Pure resume execute command builder (no root refs): list-form
    // argv for the Confirm execute. Empty project yields []. Never
    // throws, never filters operations.
    function resumeExecuteCommand(projectId) {
        try {
            let id = String(projectId || "").trim();
            if (!id) return [];
            return ["python3", "scripts/desktop_resume.py", "execute", "--project", id];
        } catch (error) { return []; }
    }

    // Pure available-operation names (no root refs): bounded names of
    // available operations (kind preferred, id fallback), at most 5.
    // Only names surface: params never leak. Unknown shapes yield [].
    // Never throws.
    function resumeAvailableNames(plan) {
        try {
            if (!plan || typeof plan !== "object" || Array.isArray(plan)) return [];
            let ops = plan.operations;
            if (!Array.isArray(ops)) return [];
            let out = [];
            for (let i = 0; i < ops.length && out.length < 5; i++) {
                let op = ops[i];
                if (!op || typeof op !== "object" || Array.isArray(op)) continue;
                if (op.available !== true) continue;
                let name = String(op.kind || op.id || "").substring(0, 80).trim();
                if (!name) continue;
                out.push(name);
            }
            return out;
        } catch (error) { return []; }
    }

    // Pure unavailable-operation lines (no root refs): at most 3
    // "<id>: <reason>" lines, reason bounded to 80 chars. Only id and
    // reason surface: params never leak. Unknown shapes yield [].
    // Never throws.
    function resumeUnavailableLines(plan) {
        try {
            if (!plan || typeof plan !== "object" || Array.isArray(plan)) return [];
            let ops = plan.operations;
            if (!Array.isArray(ops)) return [];
            let out = [];
            for (let i = 0; i < ops.length && out.length < 3; i++) {
                let op = ops[i];
                if (!op || typeof op !== "object" || Array.isArray(op)) continue;
                if (op.available !== false) continue;
                let label = String(op.id || op.kind || "op").substring(0, 80).trim() || "op";
                let reason = String(op.reason || "unavailable").replace(/\s+/g, " ").trim().substring(0, 80);
                out.push(label + ": " + reason);
            }
            return out;
        } catch (error) { return []; }
    }

    // Pure unavailable count (no root refs): total unavailable
    // operations, so the card can show "+N more". Never throws.
    function resumeUnavailableCount(plan) {
        try {
            if (!plan || typeof plan !== "object" || Array.isArray(plan)) return 0;
            let ops = plan.operations;
            if (!Array.isArray(ops)) return 0;
            let count = 0;
            for (let i = 0; i < ops.length; i++) {
                let op = ops[i];
                if (op && typeof op === "object" && !Array.isArray(op)
                        && op.available === false) count++;
            }
            return count;
        } catch (error) { return 0; }
    }

    // Pure first-warning text (no root refs): the first warning,
    // bounded to 100 chars, else "". Never throws.
    function resumeFirstWarning(plan) {
        try {
            if (!plan || typeof plan !== "object" || Array.isArray(plan)) return "";
            let warnings = plan.warnings;
            if (!Array.isArray(warnings)) return "";
            for (let i = 0; i < warnings.length; i++) {
                let text = String(warnings[i] || "").replace(/\s+/g, " ").trim();
                if (text) return text.substring(0, 100);
            }
            return "";
        } catch (error) { return ""; }
    }

    // Pure execute payload validator (no root refs, palette semantics):
    // {results: [], project: {id}}. Never throws.
    function validResumeExecutePayload(payload) {
        try {
            return !!(payload && typeof payload === "object" && !Array.isArray(payload)
                && Array.isArray(payload.results)
                && payload.project && typeof payload.project === "object"
                && !Array.isArray(payload.project)
                && typeof payload.project.id === "string" && payload.project.id !== "");
        } catch (error) { return false; }
    }

    // Pure execute summary (no root refs, palette semantics): counts
    // by status plus the first failed/partial/skipped kind+reason,
    // bounded to 300 chars. Partial failures stay visible but never
    // block the handoff. Never throws.
    function resumeExecuteSummary(payload) {
        try {
            let results = payload && Array.isArray(payload.results) ? payload.results : [];
            let counts = { ok: 0, launched: 0, partial: 0, failed: 0, skipped: 0, delegated: 0, other: 0 };
            let firstAttention = "";
            for (let i = 0; i < results.length; i++) {
                let item = results[i] || {};
                let status = String(item.status || "").toLowerCase();
                if (status === "ok") counts.ok++;
                else if (status === "launched") counts.launched++;
                else if (status === "partial") counts.partial++;
                else if (status === "failed") counts.failed++;
                else if (status === "skipped") counts.skipped++;
                else if (status === "delegated") counts.delegated++;
                else counts.other++;
                if (!firstAttention && (status === "failed" || status === "partial" || status === "skipped")) {
                    let label = String(item.kind || item.id || "op");
                    let reason = String(item.reason || status);
                    firstAttention = label + ": " + reason;
                }
            }
            let okTotal = counts.ok + counts.launched;
            let summary = "Restored " + okTotal + " ok";
            if (counts.delegated) summary += ", " + counts.delegated + " delegated";
            if (counts.partial) summary += ", " + counts.partial + " partial";
            if (counts.failed) summary += ", " + counts.failed + " failed";
            if (counts.skipped) summary += ", " + counts.skipped + " skipped";
            if (counts.other) summary += ", " + counts.other + " other";
            if (firstAttention) summary += " — " + firstAttention.substring(0, 120);
            return summary.substring(0, 300);
        } catch (error) {
            return "";
        }
    }

    // -- Placement math (pure over numbers; CalendarPopout conventions). --
    function clampPopupX(anchorX, screenWidth, width) {
        var margin = 8;
        var maxX = Math.max(margin, screenWidth - width - margin);
        return Math.max(margin, Math.min(Math.round(anchorX), maxX));
    }

    function popupWidthFor(screenWidth) {
        var margin = 8;
        var room = screenWidth - margin * 2;
        if (room <= 0) return Math.max(0, Math.round(screenWidth));
        return Math.round(Math.min(430, room));
    }

    function popupHeightFor(anchorBottom, screenHeight) {
        var gap = 8, margin = 8, minH = 240;
        var room = screenHeight - margin * 2;
        if (room < minH) return Math.max(0, Math.round(room));
        var below = screenHeight - anchorBottom - gap - margin;
        if (below >= 600) return 600;
        if (below >= minH) return Math.round(below);
        return Math.round(Math.min(600, room));
    }

    function popupTopFor(anchorBottom, screenHeight, height) {
        var gap = 8, margin = 8;
        var top = anchorBottom + gap;
        var maxTop = Math.max(margin, screenHeight - height - margin);
        return Math.max(margin, Math.min(Math.round(top), maxTop));
    }

    function refreshPosition() {
        var bar = root.anchorWindow;
        var s = root.screen;
        if (!root.anchorItem || !bar || !bar.contentItem || !s) return;
        var sw = Number(s.width), sh = Number(s.height);
        if (!isFinite(sw) || !isFinite(sh) || sw <= 0 || sh <= 0) return;
        var local = root.anchorItem.mapToItem(bar.contentItem, 0, 0);
        var anchorBottom = local.y + root.anchorItem.height;
        var width = root.popupWidthFor(sw);
        var height = root.popupHeightFor(anchorBottom, sh);
        root.popupWidth = width;
        root.popupHeight = height;
        root.popupX = root.clampPopupX(local.x, sw, width);
        root.popupY = root.popupTopFor(anchorBottom, sh, height);
    }

    onAnchorItemChanged: root.refreshPosition()
    onScreenChanged: root.refreshPosition()

    Connections {
        target: root.anchorItem
        enabled: !!root.anchorItem
        ignoreUnknownSignals: true
        function onXChanged() { root.refreshPosition(); }
        function onYChanged() { root.refreshPosition(); }
        function onWidthChanged() { root.refreshPosition(); }
        function onHeightChanged() { root.refreshPosition(); }
    }

    Connections {
        target: root.screen
        ignoreUnknownSignals: true
        function onWidthChanged() { root.refreshPosition(); }
        function onHeightChanged() { root.refreshPosition(); }
    }

    // PanelWindow variant of the anchor grammar (docs/control-center.md):
    // screen-local margins derived item-relative from the tray button; the
    // watcher covers ancestor motion like ControlCenter's TransformWatcher.
    TransformWatcher {
        id: anchorWatcher
        a: root.anchorScope ? root.anchorScope : root.anchorItem
        b: root.anchorItem
        onTransformChanged: root.refreshPosition()
    }

    function setOpen(open) {
        requestedOpen = open;
        if (open) {
            closing = false;
            exitMotion.stop();
            root.refreshPosition();
            if (!visible) visible = true;
            // Already open and steady: keep the frame (no reset/flash).
            if (!(root.reveal >= 0.99 && enterMotion.running === false)) {
                // Reverse from the current reveal value. Never assign a
                // fresh start while partially visible.
                enterMotion.restart();
            }
            Qt.callLater(function() { if (root.requestedOpen && !root.closing) root.maybeAutoSummary(); });
        } else if (visible && !closing) {
            closing = true;
            enterMotion.stop();
            exitMotion.restart();
        }
    }

    Shortcut {
        sequence: "Escape"
        onActivated: root.closePopup()
    }

    function toggle(item) {
        if (item) anchorItem = item;
        setOpen(!requestedOpen);
    }

    // Open for a stable project id. Empty ids never open here: the
    // caller falls back to the plain project list instead.
    function openFor(projectId, projectName, anchor) {
        let id = String(projectId || "").trim();
        if (!id) return false;
        if (anchor) anchorItem = anchor;
        let changed = id !== String(root.projectId || "");
        root.projectId = id;
        root.projectName = String(projectName || "");
        if (changed) {
            // Project switch: visible state follows the new project's
            // cache. The in-flight launch identity (overviewProjectId)
            // is NEVER cleared here: a late overview completion
            // validates against its launch and queued fetches drain
            // after settle.
            overviewLoaded = false;
            overviewError = "";
            overviewTodos = [];
            overviewTodosOpen = 0;
            overviewTodosTotal = 0;
            overviewTodosReason = "";
            overviewWorkMs = 0;
            overviewWorkScope = "";
            overviewWorkCount = 0;
            overviewWorkReason = "";
            overviewLastSessionId = "";
            overviewLastSession = null;
            overviewLastReason = "";
            overviewChangesAvailable = false;
            overviewHasBaseline = false;
            overviewFetchedAt = 0;
            overviewKeepStale = false;
            overviewBaselineCommit = "";
            overviewLatestCommit = "";
            overviewSummaryKey = "";
            overviewEvidence = "";
            overviewChangesReason = "";
            summaryText = "";
            summaryState = "idle";
            statusText = "";
            summaryAttemptedKey = "";
            root.resyncSummaryVisible();
            // Project switch: the resume view clears. In-flight resume
            // launches keep their immutable identity; stale completions
            // are discarded by generation guard, never painted onto or
            // handed off for the new project. Never auto-fetches.
            root.resumePlanGeneration++;
            root.resumeExecuteGeneration++;
            root.resumePlan = null;
            root.resumeStatus = "";
            root.resumePlanBusy = false;
            root.resumePlanStarted = false;
            root.resumePlanRetiring = false;
            root.resumeExecuteBusy = false;
            root.resumeExecuteStarted = false;
            root.resumeExecuteRetiring = false;
            try { resumePlanTimeout.stop(); } catch (error) {}
            try { resumePlanKillTimer.stop(); } catch (error) {}
            try { resumeExecuteTimeout.stop(); } catch (error) {}
            try { resumeExecuteKillTimer.stop(); } catch (error) {}
            try { resumePlanProcess.running = false; } catch (error) {}
            try { resumeExecuteProcess.running = false; } catch (error) {}
        }
        root.setOpen(true);
        // Fetch when the visible data is missing or foreign. A
        // same-project reopen reuses displayed data only within the TTL;
        // older data refreshes in the background while it stays visible.
        // A refused launch queues the newest request instead of dropping
        // it. Queued recap state belongs to the previous view: drop it.
        if (String(root.overviewProjectId || "") !== id || !root.overviewLoaded) {
            root.refreshOverview(id);
        } else {
            root.resyncSummaryVisible();
            let age = Date.now() - Number(root.overviewFetchedAt || 0);
            if (!(age >= 0 && age <= Number(root.overviewTtlMs || 0)))
                root.refreshOverview(id);
            else
                root.maybeAutoSummary();
        }
        return true;
    }

    function resyncSummaryVisible() {
        let id = String(root.projectId || "");
        let summaryKey = String(root.overviewSummaryKey || "");
        if (!id || !summaryKey) {
            if (root.summaryState !== "waiting" && root.summaryState !== "sending") {
                root.summaryText = root.summaryState === "idle" ? "" : root.summaryText;
            }
            return;
        }
        let key = root.summaryCacheKey(id, summaryKey);
        if (root.summaries && root.summaries[key] !== undefined) {
            root.summaryText = String(root.summaries[key]);
            if (root.summaryState !== "waiting" && root.summaryState !== "sending")
                root.summaryState = "ready";
        }
    }

    function refreshOverview(forId) {
        let id = String(forId === undefined || forId === null ? root.projectId : forId).trim();
        if (!id) return false;
        if (overviewBusy || overviewProcess.running || overviewRetiring) {
            // Never overlap a running helper: the in-flight launch keeps
            // its immutable identity and the newest request waits.
            overviewQueuedProjectId = id;
            return false;
        }
        overviewGeneration++;
        overviewLaunchGeneration = overviewGeneration;
        overviewBusy = true;
        overviewStarted = false;
        overviewStartFailed = false;
        overviewRetiring = false;
        overviewProjectId = id;
        // A same-project refresh keeps the displayed data until the new
        // payload paints; a switch or first load starts blank (the openFor
        // reset already cleared the view there).
        overviewKeepStale = root.overviewLoaded && String(root.overviewProjectId || "") === id;
        if (!root.overviewKeepStale) {
            overviewLoaded = false;
            overviewError = "";
        }
        overviewProcess.command = ["python3", Quickshell.shellPath("scripts/project_overview.py"),
            "overview", "--project", id, "--sessions-limit", "20"];
        overviewTimeout.restart();
        overviewProcess.running = true;
        return true;
    }

    // Drain one queued fetch after the in-flight launch settles. Runs
    // the newest request only when this popup still shows its project;
    // a close or a superseding switch drops the queue. When loaded data
    // already covers the queued project, resync instead of refetching.
    function drainOverviewQueue() {
        let queued = String(root.overviewQueuedProjectId || "");
        if (!queued) return false;
        root.overviewQueuedProjectId = "";
        if (!root.requestedOpen || root.closing) return false;
        if (queued !== String(root.projectId || "")) return false;
        if (root.overviewLoaded && String(root.overviewProjectId || "") === queued) {
            root.resyncSummaryVisible();
            root.maybeAutoSummary();
            return true;
        }
        return root.refreshOverview(queued);
    }

    function finishOverview(code, output, generation) {
        if (Number(generation) !== Number(root.overviewLaunchGeneration)) return false;
        if (Number(generation) !== Number(root.overviewGeneration)) return false;
        overviewTimeout.stop();
        overviewBusy = false;
        let parsed = null;
        try { parsed = root.parseOverview(output, code); } catch (error) { parsed = { ok: false }; }
        // The completion validates against the immutable launch identity,
        // never the current view: a switch mid-flight must not steal it.
        let launchId = String(root.overviewProjectId || "");
        if (!parsed || !parsed.ok) {
            // Robust partial failure: only surface when still viewing the
            // launch. A background refresh keeps the stale data visible
            // with a notice instead of blanking it.
            if (launchId !== "" && launchId === String(root.projectId || "")) {
                if (root.overviewKeepStale) {
                    root.overviewKeepStale = false;
                    statusText = "Refresh failed; showing last loaded data.";
                } else {
                    overviewLoaded = false;
                    overviewError = "Project overview is unavailable (backend error).";
                    statusText = overviewError;
                }
            }
            root.drainOverviewQueue();
            return false;
        }
        let data = parsed.data;
        if (String(data.requested_project_id || "") !== launchId) {
            root.drainOverviewQueue();
            return false;
        }
        let forId = String(data.requested_project_id || "");
        let todos = data.todos && typeof data.todos === "object" ? data.todos : {};
        let work = data.work_time && typeof data.work_time === "object" ? data.work_time : {};
        let last = data.last_session && typeof data.last_session === "object" ? data.last_session : {};
        let openTodos = Array.isArray(todos.open) ? todos.open.slice(0, 10) : [];
        let lastSession = (last.session && typeof last.session === "object") ? last.session : null;
        let changes = (last.changes && typeof last.changes === "object") ? last.changes : {};
        let lastSessionId = "";
        try { lastSessionId = lastSession ? String(lastSession.session_id || "") : ""; } catch (error) {}
        let changesAvailable = !!changes.available;
        let hasBaseline = !!changes.has_baseline;
        let baselineCommit = String(changes.baseline_commit || "");
        let latestCommit = String(changes.latest_commit || "");
        let summaryKey = String(changes.summary_key || "");
        // Never clipped here: the sidecar bounds evidence at 8000 with
        // balanced section budgets, and prefix-clipping would risk
        // stripping the baseline/latest attribution sections or the
        // trailing limitation disclaimer. The recap helper bounds argv.
        let evidence = String(changes.evidence || "");
        let changesReason = String(changes.reason || "");
        // Capture the previous key BEFORE assigning: the comparison
        // after assignment would always be false and the attempt key
        // would never reset on new evidence.
        let prevSummaryKey = String(root.overviewSummaryKey || "");
        // Only paint when still viewing the launched project.
        if (launchId !== "" && launchId === String(root.projectId || "")) {
            overviewTodos = openTodos;
            overviewTodosOpen = Number(todos.open_count || 0);
            overviewTodosTotal = Number(todos.total_count || 0);
            overviewTodosReason = String(todos.reason || "");
            overviewWorkMs = Number(work.total_ms || 0);
            overviewWorkScope = String(work.scope_label || "last 20 work sessions");
            overviewWorkCount = Number(work.count || 0);
            overviewWorkReason = String(work.reason || "");
            overviewLastSessionId = lastSessionId;
            overviewLastSession = lastSession;
            overviewLastReason = String(last.reason || "");
            overviewChangesAvailable = changesAvailable;
            overviewHasBaseline = hasBaseline;
            overviewBaselineCommit = baselineCommit;
            overviewLatestCommit = latestCommit;
            overviewSummaryKey = summaryKey;
            overviewEvidence = evidence;
            overviewChangesReason = changesReason;
            overviewLoaded = true;
            overviewError = "";
            overviewKeepStale = false;
            overviewFetchedAt = Date.now();
            if (!overviewTodosOpen && !overviewTodosReason)
                overviewTodosReason = "No open TODOs.";
            // New evidence versions the recap: a changed summary key
            // re-arms the single auto attempt and drops the stale recap
            // text; the same key never loops and keeps its cache.
            if (summaryKey !== prevSummaryKey) {
                root.summaryAttemptedKey = "";
                root.summaryText = "";
                if (root.summaryState !== "waiting" && root.summaryState !== "sending")
                    root.summaryState = "idle";
            }
            statusText = "";
            root.resyncSummaryVisible();
            root.maybeAutoSummary();
        }
        root.drainOverviewQueue();
        return true;
    }

    function handleOverviewExited(code, output) {
        overviewTimeout.stop();
        overviewKillTimer.stop();
        if (!overviewBusy && !overviewRetiring) return false;
        if (overviewRetiring) {
            overviewBusy = false;
            overviewStarted = false;
            overviewRetiring = false;
            root.drainOverviewQueue();
            return false;
        }
        let launchBefore = root.overviewLaunchGeneration;
        let generation = root.overviewLaunchGeneration;
        let applied = false;
        try { applied = root.finishOverview(code, output, generation); } catch (error) { applied = false; }
        if (Number(root.overviewLaunchGeneration) === Number(launchBefore)) {
            // No queued fetch launched: settle the flags. A drained
            // launch owns them now and its completion must not be
            // dropped as "not busy".
            overviewBusy = false;
            overviewStarted = false;
        }
        return applied;
    }

    function handleOverviewRunningChanged() {
        if (overviewProcess.running) return false;
        if (!overviewBusy || overviewStarted) return false;
        overviewTimeout.stop();
        overviewKillTimer.stop();
        if (overviewRetiring) {
            overviewBusy = false;
            overviewRetiring = false;
            root.drainOverviewQueue();
            return false;
        }
        let launchBefore = root.overviewLaunchGeneration;
        let generation = root.overviewLaunchGeneration;
        let applied = false;
        try { applied = root.finishOverview(1, "", generation); } catch (error) { applied = false; }
        if (Number(root.overviewLaunchGeneration) === Number(launchBefore)) {
            overviewBusy = false;
        }
        return applied;
    }

    function handleOverviewTimeout() {
        if (!overviewBusy) { overviewTimeout.stop(); return false; }
        overviewTimeout.stop();
        overviewRetiring = true;
        overviewProcess.running = false;
        overviewKillTimer.restart();
        // Clear only the requesting view; cached per-project data stays.
        // A background refresh keeps the stale data with a notice.
        if (String(root.overviewProjectId || "") === String(root.projectId || "")) {
            if (root.overviewKeepStale) {
                root.overviewKeepStale = false;
                statusText = "Refresh timed out; showing last loaded data.";
            } else {
                overviewLoaded = false;
                overviewError = "Project overview timed out; retry.";
                statusText = overviewError;
            }
        }
        return true;
    }

    function fireOverviewKillTimeout() {
        if (root.overviewRetiring && overviewProcess.running) {
            try { overviewProcess.signal(9); } catch (error) {}
            return true;
        }
        return false;
    }

    // -- Agent: auto recap + instruction send (shared worker) --

    // Automatic recap via the isolated no-tools subprocess. Independent
    // of the shared worker: busyness, approvals, and history readiness
    // never block or delay it, and it never queues a prompt onto a busy
    // worker. Single attempt per summary key; manual retry re-arms.
    function maybeAutoSummary() {
        if (!root.requestedOpen || root.closing) return false;
        if (!root.overviewLoaded) return false;
        if (String(root.overviewProjectId || "") !== String(root.projectId || "")) return false;
        let id = String(root.projectId || "");
        if (!id) return false;
        let sessionId = String(root.overviewLastSessionId || "");
        if (!sessionId) {
            if (root.summaryState === "idle" && !root.summaryText) {
                root.summaryState = "empty";
                root.summaryText = "No recorded work session for this project.";
            }
            return false;
        }
        // Change-only recap: without baselined sidecar evidence there is
        // no LLM call and no window-history fallback -- the reason shows.
        if (!root.overviewChangesAvailable || !root.overviewHasBaseline ||
                String(root.overviewSummaryKey || "") === "" ||
                String(root.overviewEvidence || "") === "") {
            if (root.summaryState === "idle" && !root.summaryText) {
                root.summaryState = "empty";
                root.summaryText = root.overviewChangesReason !== ""
                    ? root.overviewChangesReason
                    : (root.overviewLastReason !== "" ? root.overviewLastReason
                        : "No change baseline for this session; recap unavailable.");
            }
            return false;
        }
        let skey = String(root.overviewSummaryKey || "");
        let key = root.summaryCacheKey(id, skey);
        if (root.summaries && root.summaries[key] !== undefined) {
            root.summaryText = String(root.summaries[key]);
            root.summaryState = "ready";
            return false;
        }
        let gate = {
            popupOpen: !!(root.requestedOpen && !root.closing),
            overviewLoaded: !!root.overviewLoaded,
            hasChanges: true,
            cached: false,
            attempted: String(root.summaryAttemptedKey || "") === key,
            recapBusy: !!(root.recapBusy || recapProcess.running || root.recapRetiring)
        };
        let ok = false;
        try { ok = root.shouldAutoSummarize(gate); } catch (error) { ok = false; }
        if (!ok) return false;
        let model = "";
        try { model = root.recapModelFor(); } catch (error) { model = ""; }
        return root.sendRecap(id, sessionId, skey, model);
    }

    // Launch one isolated recap for a summary key. The attempt key is
    // recorded only on actual launch. Newer evidence painted while a
    // recap runs is picked up by the finish-time chain below, so no
    // queue is needed.
    function sendRecap(projectId, sessionId, key, model) {
        let id = String(projectId || "").trim();
        let sid = String(sessionId || "").trim();
        let skey = String(key || "");
        if (!id || !sid || !skey) return false;
        if (root.recapBusy || recapProcess.running || root.recapRetiring) return false;
        root.recapGeneration++;
        root.recapLaunchGeneration = root.recapGeneration;
        root.recapBusy = true;
        root.recapStarted = false;
        root.recapStartFailed = false;
        root.recapRetiring = false;
        root.recapLaunchProjectId = id;
        root.recapLaunchKey = skey;
        root.recapLaunchSessionId = sid;
        root.summaryAttemptedKey = root.summaryCacheKey(id, skey);
        root.summaryState = "sending";
        let cmd = ["python3", Quickshell.shellPath("scripts/project_recap.py"),
            "recap", "--project", id, "--session", sid];
        if (String(model || "") !== "") cmd.push("--model", String(model));
        recapProcess.command = cmd;
        recapTimeout.restart();
        recapProcess.running = true;
        root.statusText = "";
        return true;
    }

    function finishRecap(code, output, generation) {
        if (Number(generation) !== Number(root.recapLaunchGeneration)) return false;
        if (Number(generation) !== Number(root.recapGeneration)) return false;
        recapTimeout.stop();
        root.recapBusy = false;
        let launchId = String(root.recapLaunchProjectId || "");
        let launchKey = String(root.recapLaunchKey || "");
        let parsed = null;
        try { parsed = root.parseRecap(output, recapError.text, code); }
        catch (error) { parsed = { ok: false, error: "Recap failed." }; }
        if (!parsed || !parsed.ok) {
            if (launchId !== "" && launchId === String(root.projectId || "")) {
                if (root.summaryState === "waiting" || root.summaryState === "sending")
                    root.summaryState = "idle";
                root.statusText = (parsed && parsed.error) || "Recap failed.";
            }
            // A settled recap may unblock newer evidence painted
            // meanwhile; the attempt/cache gates keep this loop-free.
            try { root.maybeAutoSummary(); } catch (error) {}
            return false;
        }
        let data = parsed.data;
        let dataKey = String(data.summary_key || "");
        let dataProject = String(data.project_id || "");
        if (dataKey !== launchKey || dataProject.toLowerCase() !== launchId.toLowerCase()) {
            // The sidecar advanced while this run was in flight: the
            // result belongs to superseded evidence. Settle the visible
            // state and refresh the overview (identity-guarded) so the
            // newer evidence paints and triggers a fresh recap. The
            // attempt key is cleared: it was consumed by stale evidence.
            if (launchId !== "" && launchId === String(root.projectId || "")) {
                if (root.summaryState === "waiting" || root.summaryState === "sending")
                    root.summaryState = "idle";
                root.statusText = "";
                root.summaryAttemptedKey = "";
                if (root.requestedOpen && !root.closing)
                    root.refreshOverview(launchId);
            }
            return false;
        }
        let text = String(data.summary || "");
        let cacheKey = root.summaryCacheKey(launchId, launchKey);
        let scopy = Object.assign({}, root.summaries);
        scopy[cacheKey] = text;
        root.summaries = scopy;
        // Paint only when the view still shows the launched evidence.
        if (launchId.toLowerCase() === String(root.projectId || "").toLowerCase() &&
                launchKey === String(root.overviewSummaryKey || "")) {
            root.summaryText = text;
            root.summaryState = "ready";
            root.statusText = "";
        }
        // Newer evidence painted during this run starts now; the gates
        // (attempted/cached/busy) make a same-key repeat impossible.
        try { root.maybeAutoSummary(); } catch (error) {}
        return true;
    }

    // Retirement completion: settle the VISIBLE recap to idle with a
    // timeout diagnostic so manual retry is possible. Only touches the
    // view still showing the retired launch; a switched project is never
    // contaminated. The attempt key stays consumed (no auto loop); a
    // newer painted key still chains via maybeAutoSummary.
    function settleRetiredRecap() {
        root.recapBusy = false;
        root.recapStarted = false;
        root.recapRetiring = false;
        if (String(root.recapLaunchProjectId || "") === String(root.projectId || "") &&
                String(root.recapLaunchKey || "") === String(root.overviewSummaryKey || "")) {
            if (root.summaryState === "waiting" || root.summaryState === "sending")
                root.summaryState = "idle";
            root.statusText = "Recap timed out; retry from the recap header.";
        }
        try { root.maybeAutoSummary(); } catch (error) {}
    }

    function handleRecapExited(code, output) {
        recapTimeout.stop();
        recapKillTimer.stop();
        if (!root.recapBusy && !root.recapRetiring) return false;
        if (root.recapRetiring) {
            root.settleRetiredRecap();
            return false;
        }
        let launchBefore = root.recapGeneration;
        let generation = root.recapLaunchGeneration;
        let applied = false;
        try { applied = root.finishRecap(code, output, generation); } catch (error) { applied = false; }
        if (Number(root.recapGeneration) === Number(launchBefore)) {
            // No queued recap launched: settle the flags. A drained
            // launch owns them now and its completion must not be
            // dropped as "not busy".
            root.recapBusy = false;
            root.recapStarted = false;
        }
        return applied;
    }

    function handleRecapRunningChanged() {
        if (recapProcess.running) return false;
        if (!root.recapBusy || root.recapStarted) return false;
        recapTimeout.stop();
        recapKillTimer.stop();
        if (root.recapRetiring) {
            root.settleRetiredRecap();
            return false;
        }
        let launchBefore = root.recapGeneration;
        let generation = root.recapLaunchGeneration;
        let applied = false;
        try { applied = root.finishRecap(1, "", generation); } catch (error) { applied = false; }
        if (Number(root.recapGeneration) === Number(launchBefore)) {
            root.recapBusy = false;
        }
        return applied;
    }

    function handleRecapTimeout() {
        if (!root.recapBusy) { recapTimeout.stop(); return false; }
        recapTimeout.stop();
        root.recapRetiring = true;
        recapProcess.running = false;
        recapKillTimer.restart();
        // The 60s helper budget plus escalation fits inside this 75s
        // watchdog; a late completion still validates by launch key.
        if (String(root.recapLaunchProjectId || "") === String(root.projectId || "") &&
                (root.summaryState === "waiting" || root.summaryState === "sending")) {
            root.summaryState = "waiting";
            root.statusText = "Recap is taking longer than expected…";
        }
        return true;
    }

    function fireRecapKillTimeout() {
        if (root.recapRetiring && recapProcess.running) {
            try { recapProcess.signal(9); } catch (error) {}
            return true;
        }
        return false;
    }

    function requestResumePlan() {
        if (!root.requestedOpen || root.closing) return false;
        let id = String(root.projectId || "").trim();
        if (!id) return false;
        if (root.resumePlanBusy || resumePlanProcess.running || root.resumePlanRetiring) return false;
        if (root.resumeExecuteBusy || resumeExecuteProcess.running || root.resumeExecuteRetiring) return false;
        let cmd = root.resumePlanCommand(id);
        if (!cmd.length) return false;
        root.resumePlanGeneration++;
        root.resumePlanLaunchGeneration = root.resumePlanGeneration;
        root.resumePlanBusy = true;
        root.resumePlanStarted = false;
        root.resumePlanStartFailed = false;
        root.resumePlanRetiring = false;
        root.resumePlanLaunchProjectId = id;
        let full = ["python3", Quickshell.shellPath("scripts/desktop_resume.py"),
            "plan", "--project", id];
        resumePlanProcess.command = full;
        resumePlanTimeout.restart();
        resumePlanProcess.running = true;
        root.resumeStatus = "Loading resume plan…";
        return true;
    }

    function finishResumePlan(code, output, generation) {
        if (Number(generation) !== Number(root.resumePlanLaunchGeneration)) return false;
        if (Number(generation) !== Number(root.resumePlanGeneration)) return false;
        resumePlanTimeout.stop();
        root.resumePlanBusy = false;
        let launchId = String(root.resumePlanLaunchProjectId || "");
        let parsed = null;
        try { parsed = root.parseResumePlan(output, resumePlanError.text, code); }
        catch (error) { parsed = { ok: false, error: "Resume returned invalid data" }; }
        if (!parsed || !parsed.ok) {
            if (launchId !== "" && launchId === String(root.projectId || "")
                    && root.requestedOpen && !root.closing) {
                root.resumePlan = null;
                root.resumeStatus = String((parsed && parsed.error) || "Resume failed.").substring(0, 300);
            }
            return false;
        }
        let data = parsed.data;
        // Identity rejection: the plan must belong to the launch.
        if (String((data.project && data.project.id) || "") !== launchId) return false;
        // Stale guard: close/switch never paints.
        if (launchId === "" || launchId !== String(root.projectId || "")) return false;
        if (!root.requestedOpen || root.closing) return false;
        root.resumePlan = data;
        root.resumeStatus = "Review the resume preview, then Confirm.";
        return true;
    }

    function settleRetiredResumePlan() {
        root.resumePlanBusy = false;
        root.resumePlanStarted = false;
        root.resumePlanRetiring = false;
        if (String(root.resumePlanLaunchProjectId || "") === String(root.projectId || "")
                && root.requestedOpen && !root.closing) {
            root.resumePlan = null;
            root.resumeStatus = "Resume timed out; retry";
        }
    }

    function handleResumePlanExited(code, output) {
        resumePlanTimeout.stop();
        resumePlanKillTimer.stop();
        if (!root.resumePlanBusy && !root.resumePlanRetiring) return false;
        if (root.resumePlanRetiring) {
            root.settleRetiredResumePlan();
            return false;
        }
        let launchBefore = root.resumePlanGeneration;
        let generation = root.resumePlanLaunchGeneration;
        let applied = false;
        try { applied = root.finishResumePlan(code, output, generation); } catch (error) { applied = false; }
        if (Number(root.resumePlanGeneration) === Number(launchBefore)) {
            root.resumePlanBusy = false;
            root.resumePlanStarted = false;
        }
        return applied;
    }

    function handleResumePlanRunningChanged() {
        if (resumePlanProcess.running) return false;
        if (!root.resumePlanBusy || root.resumePlanStarted) return false;
        resumePlanTimeout.stop();
        resumePlanKillTimer.stop();
        if (root.resumePlanRetiring) {
            root.settleRetiredResumePlan();
            return false;
        }
        let launchBefore = root.resumePlanGeneration;
        let generation = root.resumePlanLaunchGeneration;
        let applied = false;
        try { applied = root.finishResumePlan(1, "", generation); } catch (error) { applied = false; }
        if (Number(root.resumePlanGeneration) === Number(launchBefore)) {
            root.resumePlanBusy = false;
        }
        return applied;
    }

    function handleResumePlanTimeout() {
        if (!root.resumePlanBusy) { resumePlanTimeout.stop(); return false; }
        resumePlanTimeout.stop();
        root.resumePlanRetiring = true;
        resumePlanProcess.running = false;
        resumePlanKillTimer.restart();
        if (String(root.resumePlanLaunchProjectId || "") === String(root.projectId || "")
                && root.requestedOpen && !root.closing) {
            root.resumeStatus = "Resume is taking longer than expected…";
        }
        return true;
    }

    function fireResumePlanKillTimeout() {
        if (root.resumePlanRetiring && resumePlanProcess.running) {
            try { resumePlanProcess.signal(9); } catch (error) {}
            return true;
        }
        return false;
    }

    // Explicit Cancel only: discards the plan preview.
    function cancelResumePreview() {
        root.resumePlan = null;
        root.resumeStatus = "";
        return true;
    }

    // Explicit Confirm only: executes the previewed plan for the
    // visible project (palette argv semantics, all operations).
    function confirmResumeExecute() {
        if (!root.requestedOpen || root.closing) return false;
        if (!root.resumePlan) return false;
        if (root.resumeExecuteBusy || resumeExecuteProcess.running || root.resumeExecuteRetiring) return false;
        if (root.resumePlanBusy || resumePlanProcess.running || root.resumePlanRetiring) return false;
        let id = String(root.projectId || "").trim();
        if (!id) return false;
        let planProject = "";
        try { planProject = String(root.resumePlan.project.id || ""); } catch (error) { planProject = ""; }
        if (!planProject || planProject !== id) return false;
        let cmd = root.resumeExecuteCommand(id);
        if (!cmd.length) return false;
        root.resumeExecuteGeneration++;
        root.resumeExecuteLaunchGeneration = root.resumeExecuteGeneration;
        root.resumeExecuteBusy = true;
        root.resumeExecuteStarted = false;
        root.resumeExecuteStartFailed = false;
        root.resumeExecuteRetiring = false;
        root.resumeExecuteLaunchProjectId = id;
        let full = ["python3", Quickshell.shellPath("scripts/desktop_resume.py"),
            "execute", "--project", id];
        resumeExecuteProcess.command = full;
        resumeExecuteTimeout.restart();
        resumeExecuteProcess.running = true;
        root.resumeStatus = "Resuming…";
        return true;
    }

    function finishResumeExecute(code, output, generation, requestProjectId) {
        if (Number(generation) !== Number(root.resumeExecuteLaunchGeneration)) return false;
        if (Number(generation) !== Number(root.resumeExecuteGeneration)) return false;
        resumeExecuteTimeout.stop();
        root.resumeExecuteBusy = false;
        let launchId = String(root.resumeExecuteLaunchProjectId || "");
        let rawRequest = (requestProjectId === undefined || requestProjectId === null)
            ? launchId : String(requestProjectId);
        let executedId = rawRequest || launchId;
        if (executedId && launchId && executedId !== launchId) return false;
        if (Number(code) !== 0) {
            if (launchId !== "" && launchId === String(root.projectId || "")
                    && root.requestedOpen && !root.closing) {
                root.resumeStatus = "Resume failed";
            }
            return false;
        }
        let payload = null;
        try { payload = JSON.parse(output || "{}"); }
        catch (error) { payload = null; }
        if (!root.validResumeExecutePayload(payload)) {
            if (launchId !== "" && launchId === String(root.projectId || "")
                    && root.requestedOpen && !root.closing) {
                root.resumeStatus = "Resume returned invalid data";
            }
            return false;
        }
        // Require the result project id to equal the executed id;
        // never hand off a mismatched project.
        let pid = String(payload.project.id || "");
        if (!pid || pid !== executedId) {
            if (launchId !== "" && launchId === String(root.projectId || "")
                    && root.requestedOpen && !root.closing) {
                root.resumeStatus = "Resume returned invalid data";
            }
            return false;
        }
        // Stale guard: close/switch never hands off.
        if (launchId === "" || launchId !== String(root.projectId || "")) return false;
        if (!root.requestedOpen || root.closing) return false;
        // Partial failures stay visible but never block the handoff.
        let message = "";
        try { message = root.resumeExecuteSummary(payload); } catch (error) { message = ""; }
        root.resumePlan = null;
        root.resumeStatus = "";
        root.handoffToProjectPlanner(pid, "resume", message);
        return true;
    }

    function settleRetiredResumeExecute() {
        root.resumeExecuteBusy = false;
        root.resumeExecuteStarted = false;
        root.resumeExecuteRetiring = false;
        if (String(root.resumeExecuteLaunchProjectId || "") === String(root.projectId || "")
                && root.requestedOpen && !root.closing) {
            root.resumeStatus = "Resume timed out; retry";
        }
    }

    function handleResumeExecuteExited(code, output) {
        resumeExecuteTimeout.stop();
        resumeExecuteKillTimer.stop();
        if (!root.resumeExecuteBusy && !root.resumeExecuteRetiring) return false;
        if (root.resumeExecuteRetiring) {
            root.settleRetiredResumeExecute();
            return false;
        }
        let launchBefore = root.resumeExecuteGeneration;
        let generation = root.resumeExecuteLaunchGeneration;
        let launchId = String(root.resumeExecuteLaunchProjectId || "");
        let applied = false;
        try { applied = root.finishResumeExecute(code, output, generation, launchId); }
        catch (error) { applied = false; }
        if (Number(root.resumeExecuteGeneration) === Number(launchBefore)) {
            root.resumeExecuteBusy = false;
            root.resumeExecuteStarted = false;
        }
        return applied;
    }

    function handleResumeExecuteRunningChanged() {
        if (resumeExecuteProcess.running) return false;
        if (!root.resumeExecuteBusy || root.resumeExecuteStarted) return false;
        resumeExecuteTimeout.stop();
        resumeExecuteKillTimer.stop();
        if (root.resumeExecuteRetiring) {
            root.settleRetiredResumeExecute();
            return false;
        }
        let launchBefore = root.resumeExecuteGeneration;
        let generation = root.resumeExecuteLaunchGeneration;
        let launchId = String(root.resumeExecuteLaunchProjectId || "");
        let applied = false;
        try { applied = root.finishResumeExecute(1, "", generation, launchId); }
        catch (error) { applied = false; }
        if (Number(root.resumeExecuteGeneration) === Number(launchBefore)) {
            root.resumeExecuteBusy = false;
        }
        return applied;
    }

    function handleResumeExecuteTimeout() {
        if (!root.resumeExecuteBusy) { resumeExecuteTimeout.stop(); return false; }
        resumeExecuteTimeout.stop();
        root.resumeExecuteRetiring = true;
        resumeExecuteProcess.running = false;
        resumeExecuteKillTimer.restart();
        if (String(root.resumeExecuteLaunchProjectId || "") === String(root.projectId || "")
                && root.requestedOpen && !root.closing) {
            root.resumeStatus = "Resume is taking longer than expected…";
        }
        return true;
    }

    function fireResumeExecuteKillTimeout() {
        if (root.resumeExecuteRetiring && resumeExecuteProcess.running) {
            try { resumeExecuteProcess.signal(9); } catch (error) {}
            return true;
        }
        return false;
    }

    function handoffToProjectPlanner(projectId, action, message) {
        let pid = String(projectId === undefined || projectId === null ? "" : projectId);
        let act = String(action === undefined || action === null ? "" : action);
        let msg = String(message === undefined || message === null ? "" : message).substring(0, 300);
        try { projectPlanningRequested(pid, act, msg); } catch (error) {}
        return pid;
    }

    // Manual recap retry from the recap header. Re-arms the single auto
    // attempt for the current overview data.
    function canRetrySummary() {
        if (!root.overviewLoaded || !root.overviewChangesAvailable || !root.overviewHasBaseline) return false;
        if (root.summaryState === "waiting" || root.summaryState === "sending") return false;
        if (root.recapBusy || recapProcess.running || root.recapRetiring) return false;
        let key = root.summaryCacheKey(String(root.projectId || ""), String(root.overviewSummaryKey || ""));
        if (root.summaries && root.summaries[key] !== undefined) return false;
        return true;
    }

    function retrySummary() {
        if (!root.canRetrySummary()) return false;
        root.summaryAttemptedKey = "";
        if (root.summaryState !== "waiting" && root.summaryState !== "sending" && root.summaryText === "")
            root.summaryState = "idle";
        return root.maybeAutoSummary();
    }

    function openFullPlanner() {
        let id = String(root.projectId || "");
        try {
            if (root.projectPlanner && typeof root.projectPlanner.openProject === "function")
                root.projectPlanner.openProject(id, "", "");
            else if (root.projectPlanner && typeof root.projectPlanner.open === "function")
                root.projectPlanner.open();
        } catch (error) {}
        try { root.closePopup(); } catch (error) {}
        return id;
    }

    // NOTE: approval review lives in the planner now (it owns
    // approvals via openApprovalForProject); the popup keeps no
    // approval UI and no review handoff.

    // Closing hides only: no agent is ever aborted or stopped here,
    // in-flight overview fetches keep their launch identity (a late
    // completion paints only when it still matches the view).
    // Reopening while a fetch runs queues newest via refresh.
    // Resume launches are invalidated: both generations bump before
    // stopping so a delayed plan/execute completion is stale.
    function closePopup() {
        requestedOpen = false;
        closing = true;
        root.resumePlanGeneration++;
        root.resumeExecuteGeneration++;
        root.resumePlanBusy = false;
        root.resumeExecuteBusy = false;
        try { resumePlanTimeout.stop(); } catch (error) {}
        try { resumePlanKillTimer.stop(); } catch (error) {}
        try { resumeExecuteTimeout.stop(); } catch (error) {}
        try { resumeExecuteKillTimer.stop(); } catch (error) {}
        try { resumePlanProcess.running = false; } catch (error) {}
        try { resumeExecuteProcess.running = false; } catch (error) {}
        enterMotion.stop();
        exitMotion.restart();
        return true;
    }

    Process {
        id: overviewProcess
        workingDirectory: Quickshell.shellPath(".")
        stdinEnabled: false
        stdout: StdioCollector { id: overviewOutput; waitForEnd: true }
        stderr: StdioCollector { id: overviewErrorOut; waitForEnd: true }
        onStarted: { root.overviewStarted = true; }
        onRunningChanged: { root.handleOverviewRunningChanged(); }
        onExited: (code) => { root.handleOverviewExited(code, overviewOutput.text); }
    }

    Timer {
        id: overviewTimeout
        interval: 12000
        repeat: false
        onTriggered: root.handleOverviewTimeout()
    }

    Timer {
        id: overviewKillTimer
        interval: 3000
        repeat: false
        onTriggered: root.fireOverviewKillTimeout()
    }

    Process {
        id: recapProcess
        workingDirectory: Quickshell.shellPath(".")
        stdinEnabled: false
        stdout: StdioCollector { id: recapOutput; waitForEnd: true }
        stderr: StdioCollector { id: recapError; waitForEnd: true }
        onStarted: { root.recapStarted = true; }
        onRunningChanged: { root.handleRecapRunningChanged(); }
        onExited: (code) => { root.handleRecapExited(code, recapOutput.text); }
    }

    // The 60s helper budget plus escalation fits inside this 75s
    // watchdog; a late completion still validates by launch key.
    Timer {
        id: recapTimeout
        interval: 75000
        repeat: false
        onTriggered: root.handleRecapTimeout()
    }

    Timer {
        id: recapKillTimer
        interval: 3000
        repeat: false
        onTriggered: root.fireRecapKillTimeout()
    }

    // Isolated Resume subprocesses (scripts/desktop_resume.py plan +
    // execute). Each has its own running guard plus generation-guarded
    // completion (immutable launch project id), watchdog plus 3s
    // SIGKILL escalation. Never modal, never blocking, never
    // automatic: plan launches only from the Resume header button,
    // execute only from the preview Confirm.
    Process {
        id: resumePlanProcess
        workingDirectory: Quickshell.shellPath(".")
        stdinEnabled: false
        stdout: StdioCollector { id: resumePlanOutput; waitForEnd: true }
        stderr: StdioCollector { id: resumePlanError; waitForEnd: true }
        onStarted: { root.resumePlanStarted = true; }
        onRunningChanged: { root.handleResumePlanRunningChanged(); }
        onExited: (code) => { root.handleResumePlanExited(code, resumePlanOutput.text); }
    }

    Timer {
        id: resumePlanTimeout
        interval: 12000
        repeat: false
        onTriggered: root.handleResumePlanTimeout()
    }

    Timer {
        id: resumePlanKillTimer
        interval: 3000
        repeat: false
        onTriggered: root.fireResumePlanKillTimeout()
    }

    Process {
        id: resumeExecuteProcess
        workingDirectory: Quickshell.shellPath(".")
        stdinEnabled: false
        stdout: StdioCollector { id: resumeExecuteOutput; waitForEnd: true }
        stderr: StdioCollector { id: resumeExecuteError; waitForEnd: true }
        onStarted: { root.resumeExecuteStarted = true; }
        onRunningChanged: { root.handleResumeExecuteRunningChanged(); }
        onExited: (code) => { root.handleResumeExecuteExited(code, resumeExecuteOutput.text); }
    }

    Timer {
        id: resumeExecuteTimeout
        interval: 12000
        repeat: false
        onTriggered: root.handleResumeExecuteTimeout()
    }

    Timer {
        id: resumeExecuteKillTimer
        interval: 3000
        repeat: false
        onTriggered: root.fireResumeExecuteKillTimeout()
    }

    Rectangle {
        id: panel
        anchors.fill: parent
        color: Theme.base
        radius: Theme.cardRadius
        border.color: Theme.border
        border.width: 1
        // Reveal-driven chrome (ControlCenter pattern): opacity + scale
        // derive from the single progress; content slides via transform.
        opacity: root.reveal
        scale: 0.96 + 0.04 * root.reveal
        transformOrigin: Item.Top
        clip: true
        visible: root.reveal > 0.01 || root.visible

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 14
            spacing: 10
            transform: Translate { y: (1 - root.reveal) * -8 }

            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Text {
                    objectName: "overviewTitle"
                    text: root.projectName !== "" ? root.projectName : root.projectId
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: 16
                    font.bold: true
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
                WidgetIconButton {
                    text: "Full planner"
                    iconSource: "icons/panel-right-open.svg"
                    tooltipText: "Open full planner"
                    Accessible.name: "Open full planner"
                    onClicked: root.openFullPlanner()
                }
                WidgetIconButton {
                    objectName: "resumeButton"
                    text: (root.resumePlanBusy || root.resumeExecuteBusy) ? "Resuming…" : "Resume"
                    iconSource: "icons/play.svg"
                    tooltipText: "Preview the resume plan for this project"
                    Accessible.name: "Resume project"
                    enabled: root.projectId !== "" && !root.resumePlanBusy && !root.resumeExecuteBusy
                    onClicked: root.requestResumePlan()
                }
                WidgetIconButton {
                    text: "Close"
                    iconSource: "icons/x.svg"
                    tooltipText: "Close"
                    Accessible.name: "Close project overview"
                    onClicked: root.closePopup()
                }
            }

            Text {
                objectName: "overviewWorkTime"
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                color: Theme.subtext1
                font.pixelSize: 12
                text: {
                    if (!root.overviewLoaded) return root.overviewBusy ? "Loading project overview…" : (root.overviewError || "Project overview is unavailable.");
                    let total = root.formatDurationMs(root.overviewWorkMs);
                    let scope = root.overviewWorkScope || "last 20 work sessions";
                    if (root.overviewWorkCount > 0) return "Tracked " + total + " · " + scope;
                    return (root.overviewWorkReason || "No tracked work sessions.") + " · " + scope;
                }
            }

            // Variable-length content scrolls in a clipped region so long
            // TODO lists or recaps can never push the header outside the
            // 600px card at small heights.
            Flickable {
                id: overviewScroll
                objectName: "overviewScroll"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: 60
                clip: true
                contentWidth: width
                contentHeight: scrollContent.implicitHeight
                flickableDirection: Flickable.VerticalFlick
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                ColumnLayout {
                    id: scrollContent
                    width: overviewScroll.width
                    spacing: 10
                    Text {
                        text: "Open TODOs"
                        color: Theme.text
                        font.pixelSize: 13
                        font.bold: true
                        Layout.fillWidth: true
                    }
                    Text {
                        objectName: "overviewTodos"
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        color: Theme.subtext1
                        font.pixelSize: 12
                        text: {
                            if (!root.overviewLoaded) return root.overviewBusy ? "…" : (root.overviewError || "—");
                            if (!root.overviewTodos || root.overviewTodos.length === 0)
                                return root.overviewTodosReason || "No open TODOs.";
                            let lines = [];
                            for (let i = 0; i < root.overviewTodos.length; i++)
                                lines.push("• " + String(root.overviewTodos[i].task || ""));
                            let suffix = root.overviewTodosOpen > root.overviewTodos.length
                                ? "\n+" + (root.overviewTodosOpen - root.overviewTodos.length) + " more" : "";
                            return lines.join("\n") + suffix;
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        Text {
                            text: "Last session recap"
                            color: Theme.text
                            font.pixelSize: 13
                            font.bold: true
                            Layout.fillWidth: true
                        }
                        WidgetIconButton {
                            visible: root.canRetrySummary()
                            text: "Retry recap"
                            iconSource: "icons/history.svg"
                            tooltipText: "Retry the session recap"
                            Accessible.name: "Retry session recap"
                            onClicked: root.retrySummary()
                        }
                    }
                    Text {
                        objectName: "overviewBaseline"
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        visible: root.overviewLoaded && root.overviewBaselineCommit !== ""
                        color: Theme.subtext0
                        font.pixelSize: 11
                        text: "Baseline " + root.shortCommit(root.overviewBaselineCommit) +
                            (root.overviewLatestCommit !== "" ? " → " + root.shortCommit(root.overviewLatestCommit) : "")
                    }
                    Text {
                        objectName: "overviewSummary"
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        color: Theme.text
                        font.pixelSize: 12
                        text: {
                            if (!root.overviewLoaded) return root.overviewBusy ? "…" : (root.overviewError || "—");
                            if (root.summaryText !== "") return root.summaryText;
                            if (root.summaryState === "waiting" || root.summaryState === "sending") return "Summarising session changes…";
                            if (root.overviewLastSessionId === "") return "No recorded work session for this project.";
                            if (!root.overviewChangesAvailable)
                                return root.overviewChangesReason !== "" ? root.overviewChangesReason : "No change baseline for this session; recap unavailable.";
                            return "Waiting for the recap…";
                        }
                    }

                    // Resume card: explicit plan preview (desktop_resume.py
                    // plan) with Confirm execute. Available operations are
                    // bounded names (at most 5); unavailable operations
                    // show at most 3 "<id>: <reason>" lines plus "+N
                    // more"; the first warning is bounded. Operation
                    // params and page content never render here.
                    ColumnLayout {
                        objectName: "resumeCard"
                        Layout.fillWidth: true
                        spacing: 6
                        visible: root.resumePlan !== null
                        Text {
                            text: "Resume preview"
                            color: Theme.text
                            font.pixelSize: 13
                            font.bold: true
                            Layout.fillWidth: true
                        }
                        Text {
                            objectName: "resumeAvailable"
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            textFormat: Text.PlainText
                            color: Theme.text
                            font.pixelSize: 12
                            text: {
                                let names = [];
                                try { names = root.resumeAvailableNames(root.resumePlan); }
                                catch (error) { names = []; }
                                if (!names.length) return "Available: none";
                                return "Available: " + names.join(", ");
                            }
                        }
                        Text {
                            objectName: "resumeUnavailable"
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            textFormat: Text.PlainText
                            color: Theme.subtext1
                            font.pixelSize: 12
                            visible: {
                                let count = 0;
                                try { count = root.resumeUnavailableCount(root.resumePlan); }
                                catch (error) { count = 0; }
                                return count > 0;
                            }
                            text: {
                                let lines = [];
                                let total = 0;
                                try { lines = root.resumeUnavailableLines(root.resumePlan); }
                                catch (error) { lines = []; }
                                try { total = root.resumeUnavailableCount(root.resumePlan); }
                                catch (error) { total = lines.length; }
                                let body = lines.join("\n");
                                if (total > lines.length)
                                    body += (body !== "" ? "\n" : "") + "+" + (total - lines.length) + " more";
                                return body !== "" ? "Unavailable:\n" + body : "";
                            }
                        }
                        Text {
                            objectName: "resumeWarning"
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            textFormat: Text.PlainText
                            color: Theme.subtext0
                            font.pixelSize: 12
                            visible: {
                                let warning = "";
                                try { warning = root.resumeFirstWarning(root.resumePlan); }
                                catch (error) { warning = ""; }
                                return warning !== "";
                            }
                            text: {
                                let warning = "";
                                try { warning = root.resumeFirstWarning(root.resumePlan); }
                                catch (error) { warning = ""; }
                                return warning !== "" ? "Note: " + warning : "";
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8
                            WidgetIconButton {
                                objectName: "resumeConfirm"
                                text: "Resume"
                                iconSource: "icons/send.svg"
                                tooltipText: "Execute the resume plan"
                                Accessible.name: "Confirm resume execute"
                                enabled: !root.resumeExecuteBusy && !root.resumePlanBusy
                                onClicked: root.confirmResumeExecute()
                            }
                            WidgetIconButton {
                                objectName: "resumeCancel"
                                text: "Cancel"
                                iconSource: "icons/x.svg"
                                tooltipText: "Discard the resume preview"
                                Accessible.name: "Cancel resume preview"
                                onClicked: root.cancelResumePreview()
                            }
                        }
                        Text {
                            objectName: "resumeStatus"
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            textFormat: Text.PlainText
                            visible: root.resumeStatus !== ""
                            color: Theme.subtext0
                            font.pixelSize: 12
                            text: root.resumeStatus
                        }
                    }

                    Text {
                        objectName: "overviewStatus"
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        visible: root.statusText !== ""
                        color: Theme.subtext0
                        font.pixelSize: 12
                        text: root.statusText
                    }
                }
            }

            // No composer here: sending instructions happens in the full
            // planner ("Full planner" above / "Open planner" below).
        }
    }

    // Single-progress motion (ControlCenter pattern): enter/exit animate
    // reveal to 1/0 from the current value (interrupted close reopens
    // mid-fade, never resets). No spring/overshoot: OutCubic in, InCubic
    // out.
    NumberAnimation {
        id: enterMotion
        target: root
        property: "reveal"
        to: 1
        duration: Theme.motionPanel
        easing.type: Easing.OutCubic
    }

    NumberAnimation {
        id: exitMotion
        target: root
        property: "reveal"
        to: 0
        duration: Theme.motionExit
        easing.type: Easing.InCubic
        onFinished: {
            if (!root.requestedOpen) root.visible = false;
            root.closing = false;
        }
    }
}
