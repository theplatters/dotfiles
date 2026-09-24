import QtQuick
import Quickshell
import Quickshell.Io
import "AmbientContext.js" as Ambient

// Shared deterministic ambient-context resolver (Phase 1, §2.1).
//
// One resolver, one shape: composes the existing read-only commands
// `scripts/desktop_projects.py current-context` (project identity),
// `current-session` (session identity), and `project-activity --limit 1`
// (per-session resources ranked by matched_at_ms) into the canonical
// AmbientContext.js block. Bounded (≤ 8 resources, ≤ 160 chars each),
// fail-soft (failures keep the last-known cache, never an error the
// Send path must handle), and never blocking Send: refresh() is
// fire-and-forget with 12 s watchdogs + 3 s kill escalation, while
// Send reads ambientBlock synchronously and attaches it visibly only
// on the first message (callers gate on prior user history).
//
// Day is the local date (Ambient.ambientDayKey). Zotero resources
// travel as window/resource title (label) + zotero://select URI
// (identity); the backend never emits a zotero title. Identity-family
// kinds ("portable"/"local") normalize to the display enum. No Rust,
// no new backend, no schema.
Item {
    id: root

    property var ambientBlock: null
    property string ambientDay: Ambient.ambientDayKey(new Date())
    property string ambientError: ""

    property string _ambientProjectId: ""
    property string _ambientProjectName: ""
    property string _ambientSessionId: ""
    property double _ambientSessionStartMs: 0
    property var _ambientResources: []
    property int ambientGeneration: 0
    property int ambientProcessGeneration: 0
    property bool ambientBusy: false
    property bool ambientRetiring: false
    property bool _ambientStarted: false
    // Pinned-project override (planner workers pinned to arbitrary
    // projects, §2.1): when set, the block's project is the pinned
    // registry entry and resources are fetched via
    // `project-activity --project <pinned> --limit 1`. Session stays the
    // desktop-current session id and day is unchanged. Empty means
    // unpinned (compositor-current identity). A pinned id whose registry
    // entry cannot be resolved omits project/resources (day + session
    // only) rather than misattributing.
    property string _ambientPinnedId: ""
    property string _ambientPinnedName: ""
    // Which project the cached resources were fetched for ("" =
    // compositor-current). Stale resources from another project are
    // never shown under a pin: reassemble drops them until the pinned
    // fetch returns.
    property string _ambientResourcesProjectId: ""
    // Scope the current ambientBlock was resolved for ("" =
    // compositor-current/unpinned, else the pinned project id). One
    // shell-level resolver serves two scopes (S-057): the palette
    // mirrors only unpinned blocks, the planner scope-checks in
    // plannerAmbientForSend. Set by reassembleAmbient.
    property string ambientScopeId: ""
    // Serialized refresh, latest-wins (S-057): while a ladder runs, a
    // new request is parked here (overwriting any earlier one) and
    // runs when the ladder drains. refreshAmbient keeps its signature.
    property string _ambientQueuedPin: ""
    property string _ambientQueuedPinName: ""
    property bool _ambientHasQueued: false

    function ambientRefreshDay() {
        try { root.ambientDay = Ambient.ambientDayKey(new Date()); } catch (error) {}
    }

    function reassembleAmbient() {
        try {
            root.ambientScopeId = String(root._ambientPinnedId || "");
            let resources = root._ambientResources;
            if (root._ambientPinnedId && (root._ambientResourcesProjectId !== root._ambientPinnedId
                    || !root._ambientPinnedName)) {
                // Never misattribute: resources fetched for another
                // project (or the compositor-current one) stay hidden
                // under a pin until the pinned fetch returns, and an
                // unresolvable pin omits resources entirely (day +
                // session only).
                resources = [];
            }
            root.ambientBlock = Ambient.buildAmbientBlock({
                projectId: root._ambientProjectId,
                projectName: root._ambientProjectName,
                sessionId: root._ambientSessionId,
                sessionStartMs: root._ambientSessionStartMs,
                day: root.ambientDay,
                resources: resources
            });
            root.ambientError = "";
        } catch (error) {}
    }

    function parseAmbientContext(output) {
        // current-context envelope: {project: {id, name} | null, ...}.
        // Unassociated stays unassociated: null project clears the id.
        // A pinned project (planner selection) is never overwritten by
        // the compositor-current identity.
        try {
            if (root._ambientPinnedId) return true;
            let data = JSON.parse(output || "{}");
            let project = data && typeof data === "object" ? data.project : null;
            if (project && typeof project === "object" && typeof project.id === "string" && project.id) {
                root._ambientProjectId = String(project.id);
                root._ambientProjectName = (typeof project.name === "string") ? project.name : "";
            } else {
                root._ambientProjectId = "";
                root._ambientProjectName = "";
            }
            return true;
        } catch (error) {
            return false;
        }
    }

    function parseAmbientSession(output) {
        // current-session envelope: {session: {session_id/id, start_ms} | null}.
        try {
            let data = JSON.parse(output || "{}");
            let session = data && typeof data === "object" ? (data.session || null) : null;
            if (session && typeof session === "object") {
                let sid = session.session_id !== undefined && session.session_id !== null
                    ? String(session.session_id) : (session.id !== undefined && session.id !== null ? String(session.id) : "");
                if (sid) {
                    root._ambientSessionId = sid;
                    let start = Number(session.start_ms);
                    root._ambientSessionStartMs = (isFinite(start) && start > 0) ? start : 0;
                    return true;
                }
            }
            root._ambientSessionId = "";
            root._ambientSessionStartMs = 0;
            return true;
        } catch (error) {
            return false;
        }
    }

    function parseAmbientActivity(output) {
        // project-activity envelope: {sessions: [{resources: [...]}]}.
        // Attributed to the project the ladder fetched for, so a pin
        // change can drop stale resources instead of misattributing.
        try {
            let data = JSON.parse(output || "{}");
            root._ambientResources = Ambient.ambientResourcesFromActivity(data, 8);
            root._ambientResourcesProjectId = String(root._ambientPinnedId || "");
            return true;
        } catch (error) {
            return false;
        }
    }

    function finishAmbientPart(kind, code, output, generation) {
        if (Number(generation) !== Number(root.ambientProcessGeneration)) return false;
        if (code !== 0) return false;
        let ok = false;
        if (kind === "context") ok = root.parseAmbientContext(output);
        else if (kind === "session") ok = root.parseAmbientSession(output);
        else if (kind === "activity") ok = root.parseAmbientActivity(output);
        if (ok) root.reassembleAmbient();
        return ok;
    }

    function refreshAmbient(pinnedId, pinnedName) {
        // Fire-and-forget: never overlaps a running ladder (a request
        // while busy parks latest-wins in the queue), never throws,
        // never blocks Send. A retiring ladder stays retired until its
        // exits arrive (fail-soft: the last-known block stays cached).
        // Optional pin (planner selection): the block's project becomes
        // the pinned registry entry and resources are fetched via
        // `project-activity --project <pinned> --limit 1`; session and
        // day stay desktop-current. An unresolvable pin (id without a
        // name) omits project/resources rather than misattributing.
        // Changing the pin drops stale resources immediately.
        try {
            root.ambientRefreshDay();
            let pin = String(pinnedId === undefined || pinnedId === null ? "" : pinnedId);
            let pinName = String(pinnedName === undefined || pinnedName === null ? "" : pinnedName);
            if (ambientContextProcess.running || ambientSessionProcess.running
                    || ambientActivityProcess.running || root.ambientRetiring || root.ambientBusy) {
                // One ladder at a time, latest-wins: park the newest
                // request; drainAmbientQueue runs it when this ladder
                // finishes and drops anything it superseded.
                root._ambientQueuedPin = pin;
                root._ambientQueuedPinName = pinName;
                root._ambientHasQueued = true;
                return false;
            }
            if (pin !== String(root._ambientPinnedId || "")
                    || (pin && root._ambientResourcesProjectId !== pin)) {
                root._ambientResources = [];
                root._ambientResourcesProjectId = pin;
            }
            root._ambientPinnedId = pin;
            root._ambientPinnedName = pinName;
            if (pin) {
                // Never misattribute: the pinned identity applies
                // synchronously, before the fetch returns. Unresolvable
                // (no name) omits the project; session/day are untouched.
                root._ambientProjectId = pin;
                root._ambientProjectName = pinName;
                if (!pinName) root._ambientProjectId = "";
                root.reassembleAmbient();
            }
            root.ambientGeneration = Number(root.ambientGeneration) + 1;
            root.ambientProcessGeneration = Number(root.ambientGeneration);
            root.ambientBusy = true;
            root._ambientStarted = false;
            root.ambientRetiring = false;
            ambientContextProcess.command = ["python3", Quickshell.shellPath("scripts/desktop_projects.py"), "current-context"];
            ambientSessionProcess.command = ["python3", Quickshell.shellPath("scripts/desktop_projects.py"), "current-session"];
            ambientActivityProcess.command = ["python3", Quickshell.shellPath("scripts/desktop_projects.py"), "project-activity", "--limit", "1"];
            if (pin) {
                // Pinned fetch: the same helper, scoped to the pinned
                // project (explicit --project UUID, as the
                // desktop_project_activity tool allows).
                let scoped = ambientActivityProcess.command.slice();
                scoped.splice(3, 0, "--project", pin);
                ambientActivityProcess.command = scoped;
            }
            ambientWatchdog.restart();
            ambientContextProcess.running = true;
            ambientSessionProcess.running = true;
            ambientActivityProcess.running = true;
            return true;
        } catch (error) {
            return false;
        }
    }

    function drainAmbientQueue() {
        // Run the parked latest-wins request, if any. Called only when
        // the ladder has fully exited (never overlaps a running one);
        // refreshAmbient relaunches from here when idle.
        try {
            if (!root._ambientHasQueued) return false;
            if (root.ambientBusy || root.ambientRetiring) return false;
            if (ambientContextProcess.running || ambientSessionProcess.running
                    || ambientActivityProcess.running) return false;
            let pin = String(root._ambientQueuedPin || "");
            let pinName = String(root._ambientQueuedPinName || "");
            root._ambientHasQueued = false;
            root._ambientQueuedPin = "";
            root._ambientQueuedPinName = "";
            if (pin) return root.refreshAmbient(pin, pinName);
            return root.refreshAmbient();
        } catch (error) {
            return false;
        }
    }

    function cancelAmbientRefresh() {
        if (!root.ambientBusy) return false;
        root.ambientBusy = false;
        root.ambientRetiring = true;
        root._ambientHasQueued = false;
        root._ambientQueuedPin = "";
        root._ambientQueuedPinName = "";
        root.ambientGeneration = Number(root.ambientGeneration) + 1;
        try { if (ambientContextProcess.running) ambientContextProcess.running = false; } catch (error) {}
        try { if (ambientSessionProcess.running) ambientSessionProcess.running = false; } catch (error) {}
        try { if (ambientActivityProcess.running) ambientActivityProcess.running = false; } catch (error) {}
        return true;
    }

    function handleAmbientExited(kind, code, output, generation) {
        // Timers stop only when the whole ladder has exited: stopping
        // them on the first exit would disarm the 12 s + 3 s SIGKILL
        // contract while a hung sibling still holds ambientBusy (and
        // refreshAmbient would refuse new work forever). While siblings
        // are still in flight, the watchdog is (re)started so the bound
        // holds from the most recent exit.
        let applied = root.finishAmbientPart(kind, code, output, generation);
        if (!ambientContextProcess.running && !ambientSessionProcess.running && !ambientActivityProcess.running) {
            ambientWatchdog.stop();
            ambientKillTimer.stop();
            root.ambientBusy = false;
            root.ambientRetiring = false;
            root.drainAmbientQueue();
        } else {
            try { ambientWatchdog.restart(); } catch (error) {}
        }
        return applied;
    }

    function handleAmbientRunningChanged() {
        // Never-started recovery (ProjectOverviewPopup
        // handleOverviewRunningChanged convention): a spawn failure emits
        // runningChanged(false) with NO onExited and never sets
        // _ambientStarted, so without this the busy latch would refuse
        // refreshes forever. Consume _ambientStarted and degrade to the
        // day-only/fail-soft cache instead of permanent refusal.
        try {
            if (ambientContextProcess.running || ambientSessionProcess.running || ambientActivityProcess.running) return false;
        } catch (error) {
            return false;
        }
        if (!root.ambientBusy || root._ambientStarted) return false;
        try { ambientWatchdog.stop(); } catch (error) {}
        try { ambientKillTimer.stop(); } catch (error) {}
        root.ambientBusy = false;
        root.ambientRetiring = false;
        root.ambientGeneration = Number(root.ambientGeneration) + 1;
        root.drainAmbientQueue();
        return true;
    }

    function handleAmbientTimeout() {
        // Bounded watchdog: retire the ladder, keep the last-known block.
        if (!root.ambientBusy) { ambientWatchdog.stop(); return false; }
        ambientWatchdog.stop();
        root.ambientRetiring = true;
        root.ambientBusy = false;
        root.ambientGeneration = Number(root.ambientGeneration) + 1;
        try { if (ambientContextProcess.running) ambientContextProcess.running = false; } catch (error) {}
        try { if (ambientSessionProcess.running) ambientSessionProcess.running = false; } catch (error) {}
        try { if (ambientActivityProcess.running) ambientActivityProcess.running = false; } catch (error) {}
        ambientKillTimer.restart();
        return true;
    }

    function fireAmbientKillTimeout() {
        if (root.ambientRetiring
                && (ambientContextProcess.running || ambientSessionProcess.running || ambientActivityProcess.running)) {
            try { ambientContextProcess.signal(9); } catch (error) {}
            try { ambientSessionProcess.signal(9); } catch (error) {}
            try { ambientActivityProcess.signal(9); } catch (error) {}
            return true;
        }
        return false;
    }

    Process {
        id: ambientContextProcess
        workingDirectory: Quickshell.shellPath(".")
        stdinEnabled: false
        stdout: StdioCollector { id: ambientContextOutput; waitForEnd: true }
        stderr: StdioCollector { id: ambientContextError; waitForEnd: true }
        onStarted: { root._ambientStarted = true; }
        onRunningChanged: { root.handleAmbientRunningChanged(); }
        onExited: (code) => {
            root.handleAmbientExited("context", code, ambientContextOutput.text, root.ambientProcessGeneration);
        }
    }

    Process {
        id: ambientSessionProcess
        workingDirectory: Quickshell.shellPath(".")
        stdinEnabled: false
        stdout: StdioCollector { id: ambientSessionOutput; waitForEnd: true }
        stderr: StdioCollector { id: ambientSessionError; waitForEnd: true }
        onStarted: { root._ambientStarted = true; }
        onRunningChanged: { root.handleAmbientRunningChanged(); }
        onExited: (code) => {
            root.handleAmbientExited("session", code, ambientSessionOutput.text, root.ambientProcessGeneration);
        }
    }

    Process {
        id: ambientActivityProcess
        workingDirectory: Quickshell.shellPath(".")
        stdinEnabled: false
        stdout: StdioCollector { id: ambientActivityOutput; waitForEnd: true }
        stderr: StdioCollector { id: ambientActivityError; waitForEnd: true }
        onStarted: { root._ambientStarted = true; }
        onRunningChanged: { root.handleAmbientRunningChanged(); }
        onExited: (code) => {
            root.handleAmbientExited("activity", code, ambientActivityOutput.text, root.ambientProcessGeneration);
        }
    }

    Timer {
        id: ambientWatchdog
        interval: 12000
        repeat: false
        onTriggered: root.handleAmbientTimeout()
    }

    Timer {
        id: ambientKillTimer
        interval: 3000
        repeat: false
        onTriggered: root.fireAmbientKillTimeout()
    }

    Timer {
        id: ambientDayPoll
        interval: 60000
        repeat: true
        running: true
        onTriggered: root.ambientRefreshDay()
    }

    // No self-start (S-057, Manifesto §5): the first refresh happens on
    // first demand (palette/planner open). ambientDay is already today's
    // date from the property initializer and stays fresh via
    // ambientDayPoll (no fork); ambientBlock stays null until then and
    // send paths use the day fallback.
}
