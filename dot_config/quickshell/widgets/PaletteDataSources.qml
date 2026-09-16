/*
 * PaletteDataSources.qml — async todo/clipboard/file sourcing for the palette.
 *
 * Responsibility: own the Logseq TODO, clipboard-history, and file-search
 * processes plus their cached rows/flags. The palette orchestrates
 * rebuildModel/loadModeData; this component only fetches and reports.
 *
 * Owned state/processes:
 * - todoProcess (logseq_todos.py with LOGSEQ_GRAPH/settings.json default),
 *   clipboardProcess (cliphist list), fileProcess (palette_files.py
 *   --query/--limit 40) + fileDelay Timer (180ms debounce).
 * - resumeListProcess (desktop_resume.py list [--query]/--limit 20) +
 *   resumeDelay Timer (180ms debounce), resumePlanProcess
 *   (desktop_resume.py plan --project UUID) for the currently selected
 *   project only.
 * - todos (var), clipboardText (string), fileRows (var), fileBusy (bool),
 *   fileTruncated (bool), fileError (string, "File search failed" or
 *   "File search returned invalid data"), todo/clipboard loaded/pending/
 *   failed flags. Resume mirrors the file pattern: resumeRows (var),
 *   resumeBusy (bool), resumeError (string), resumePlan (var, validated
 *   ResumePlan v1 or null), resumePlanBusy (bool), resumePlanError (string),
 *   resumePlanProjectId (string). The pending flags are the extracted equivalents of the
 *   pre-refactor clipboardProcess.running/todoProcess.running display
 *   gates: true exactly while the load is in flight (see noMatchText).
 * - Generation props: openingGeneration (mirrored from palette),
 *   todoProcessGeneration, clipboardProcessGeneration, fileGeneration,
 *   fileProcessGeneration, pendingFileQuery, fileProcessQuery,
 *   resumeGeneration, resumeProcessGeneration, pendingResumeQuery,
 *   resumeProcessQuery, resumePlanGeneration, resumePlanProcessGeneration,
 *   pendingResumePlanId, resumePlanProcessId, resumePlanProjectId.
 *
 * Contract with CommandPalette:
 * - In: `property int openingGeneration`, `property bool paletteOpen`
 *   (mirror requestedOpen), `property string mode/modeQuery`,
 *   `property bool calculatorOnly` (mirror palette calculatorOnly() so
 *   file eligibility excludes calculator-only unified queries, matching
 *   the palette shouldSearchFiles).
 * - Out: `signal loaded(string source)` ("todo"/"clipboard"/"file"/
 *   "resume"/"resumePlan") which the palette uses to rebuildModel()
 *   (file/resume failures emit no loaded, only fileError/resumeError for
 *   the palette notice; invalid file/resume data emits loaded with the
 *   error set so the palette rebuilds and notices);
 *   `signal loadFailed(string source, string message)` for file/resume
 *   search failures where no rebuild happens; `signal reloadRequested()`
 *   for stale completions that arrived while open and need loadModeData().
 * - Palette binds openingGeneration/paletteOpen/mode/modeQuery/calculatorOnly, calls
 *   ensureTodoData/ensureClipboardData/scheduleFileSearch/startFileSearch/
 *   scheduleResumeSearch/startResumeSearch/ensureResumePlan/reset helpers
 *   from loadModeData/rebuild/open/close, and reads
 *   todos/clipboardText/fileRows/fileBusy/fileTruncated/resumeRows/
 *   resumeBusy/resumePlan/resumePlanBusy for rebuildModel.
 *
 * Generation/staleness: ensure* pins the openingGeneration; finish* drops
 * stale generations without installing payloads. A stale file/resume
 * completion while searching reschedules fileDelay/resumeDelay (never
 * installs); stale todo/clipboard completions while open emit
 * reloadRequested. A failed load is terminal for this opening (failed
 * flag set, loaded emitted once). Resume plans are loaded only for the
 * currently selected project: finishResumePlan drops payloads whose
 * generation or project id no longer matches pendingResumePlanId.
 *
 * No new controls were added.
 */
import QtQuick
import Quickshell
import Quickshell.Io

Item {
    id: root

    property var todos: []
    property string clipboardText: ""
    property var fileRows: []
    property bool fileBusy: false
    property bool fileTruncated: false
    property string fileError: ""
    property bool todoLoaded: false
    property bool todoPending: false
    property bool todoFailed: false
    property bool clipboardLoaded: false
    property bool clipboardPending: false
    property bool clipboardFailed: false
    property int openingGeneration: 0
    property bool paletteOpen: false
    property string mode: ""
    property string modeQuery: ""
    // Mirrors CommandPalette.calculatorOnly(): when a unified query is a
    // successful calculator expression, file search is ineligible. Bound by
    // the palette so start/completion guards match palette shouldSearchFiles.
    property bool calculatorOnly: false
    property int todoProcessGeneration: 0
    property int clipboardProcessGeneration: 0
    property int fileGeneration: 0
    property int fileProcessGeneration: 0
    property string pendingFileQuery: ""
    property string fileProcessQuery: ""
    property var resumeRows: []
    property bool resumeBusy: false
    property string resumeError: ""
    property var resumePlan: null
    property bool resumePlanBusy: false
    property string resumePlanError: ""
    property string resumePlanProjectId: ""
    property int resumeGeneration: 0
    property int resumeProcessGeneration: 0
    property string pendingResumeQuery: ""
    property string resumeProcessQuery: ""
    property int resumePlanGeneration: 0
    property int resumePlanProcessGeneration: 0
    property string pendingResumePlanId: ""
    property string resumePlanProcessId: ""
    property bool resumePlanRetiring: false

    signal loaded(string source)
    signal loadFailed(string source, string message)
    signal reloadRequested()

    function isUnifiedSearch() { return root.mode === "" && !!root.modeQuery; }

    Process {
        id: todoProcess
        // Empty graph arg means "no override": logseq_todos.py resolves
        // LOGSEQ_GRAPH, then logseqGraph in settings.json.
        command: ["python3", Quickshell.shellPath("scripts/logseq_todos.py"),
            Quickshell.env("LOGSEQ_GRAPH") || ""]
        stdout: StdioCollector { id: todoOutput }
        onExited: (code) => root.finishTodoLoad(code, todoOutput.text,
                                                root.todoProcessGeneration)
    }

    Process {
        id: clipboardProcess
        command: ["cliphist", "list"]
        stdout: StdioCollector { id: clipboardOutput }
        onExited: (code) => root.finishClipboardLoad(code, clipboardOutput.text,
                                                       root.clipboardProcessGeneration)
    }

    Timer {
        id: fileDelay
        interval: 180
        onTriggered: root.startFileSearch()
    }

    Process {
        id: fileProcess
        stdout: StdioCollector { id: fileOutput }
        onExited: (code) => root.finishFileSearch(code, fileOutput.text,
                                                  root.fileProcessGeneration, root.fileProcessQuery)
    }

    Timer {
        id: resumeDelay
        interval: 180
        onTriggered: root.startResumeSearch()
    }

    Timer {
        id: resumeTimeout
        interval: 12000
        repeat: false
        onTriggered: root.cancelResumeSearch()
    }

    Timer {
        id: resumePlanTimeout
        interval: 12000
        repeat: false
        onTriggered: root.cancelResumePlan()
    }

    Process {
        id: resumeListProcess
        stdout: StdioCollector { id: resumeListOutput }
        onExited: (code) => root.finishResumeSearch(code, resumeListOutput.text,
                                                    root.resumeProcessGeneration, root.resumeProcessQuery)
    }

    Process {
        id: resumePlanProcess
        stdout: StdioCollector { id: resumePlanOutput }
        onExited: (code) => root.finishResumePlan(code, resumePlanOutput.text,
                                                  root.resumePlanProcessGeneration, root.resumePlanProcessId)
    }

    function ensureTodoData() {
        if (root.todoPending || root.todoLoaded || root.todoFailed) return;
        if (todoProcess.running) return;
        root.todoPending = true;
        root.todoProcessGeneration = root.openingGeneration;
        todoProcess.running = true;
    }

    function ensureClipboardData() {
        if (root.clipboardPending || root.clipboardLoaded || root.clipboardFailed) return;
        if (clipboardProcess.running) return;
        root.clipboardPending = true;
        root.clipboardProcessGeneration = root.openingGeneration;
        clipboardProcess.running = true;
    }

    function finishTodoLoad(code, output, generation) {
        root.todoPending = false;
        if (generation !== root.openingGeneration || !root.paletteOpen) {
            if (root.paletteOpen && (root.mode === "!" || root.isUnifiedSearch())) root.reloadRequested();
            return;
        }
        if (code !== 0) {
            root.todoFailed = true;
            root.loaded("todo");
            return;
        }
        try {
            root.todos = JSON.parse(output || "[]");
            root.todoLoaded = true;
        } catch (error) {
            root.todos = [];
            root.todoFailed = true;
        }
        root.loaded("todo");
    }

    function finishClipboardLoad(code, output, generation) {
        root.clipboardPending = false;
        if (generation !== root.openingGeneration || !root.paletteOpen) {
            if (root.paletteOpen && ((root.mode === "clip" || root.mode === "#") || root.isUnifiedSearch()))
                root.reloadRequested();
            return;
        }
        if (code === 0) {
            root.clipboardText = output || "";
            root.clipboardLoaded = true;
        } else {
            root.clipboardFailed = true;
        }
        root.loaded("clipboard");
    }

    function shouldSearchFiles() {
        // Matches CommandPalette.shouldSearchFiles: file mode always
        // searches; unified search is ineligible for calculator-only
        // queries so stale file completions cannot launch searches for
        // calculator expressions.
        return root.mode === "file" || (root.isUnifiedSearch() && !root.calculatorOnly);
    }

    function finishFileSearch(code, output, generation, requestQuery) {
        root.fileBusy = false;
        if (root.paletteOpen && root.shouldSearchFiles() && root.modeQuery &&
                (root.fileGeneration !== generation || root.modeQuery !== requestQuery)) {
            fileDelay.restart();
            return;
        }
        if (code !== 0 || !root.paletteOpen || !root.shouldSearchFiles() ||
                root.pendingFileQuery !== root.modeQuery || root.fileGeneration !== generation ||
                requestQuery !== root.modeQuery) {
            if (code !== 0 && root.paletteOpen && root.shouldSearchFiles()) {
                root.fileError = "File search failed";
                root.loadFailed("file", "File search failed");
            }
            return;
        }
        try {
            let payload = JSON.parse(output || "{}");
            root.fileError = "";
            root.fileTruncated = !!payload.truncated;
            root.fileRows = (payload.results || []).map((item) => ({
                prefix: "file:", title: item.name, subtitle: item.path,
                kind: "file", payload: item, score: 1
            }));
            root.loaded("file");
        } catch (error) {
            root.fileRows = [];
            root.fileError = "File search returned invalid data";
            root.loaded("file");
        }
    }

    function scheduleFileSearch() {
        fileDelay.restart();
    }

    function stopFileSearch() {
        fileDelay.stop();
        if (fileProcess.running) fileProcess.running = false;
    }

    function startFileSearch() {
        if (!root.paletteOpen || !root.shouldSearchFiles() || !root.modeQuery) return;
        if (fileProcess.running) { fileDelay.restart(); return; }
        root.fileBusy = true;
        root.fileProcessGeneration = root.fileGeneration;
        root.fileProcessQuery = root.modeQuery;
        fileProcess.command = ["python3", Quickshell.shellPath("scripts/palette_files.py"),
                               "--query", root.modeQuery, "--limit", "40"];
        fileProcess.running = true;
    }

    function shouldSearchResume() {
        return root.mode === "resume";
    }

    function validResumeList(payload) {
        return payload && typeof payload === "object" && Array.isArray(payload.entries);
    }

    function validResumePlan(payload) {
        return payload && typeof payload === "object" && payload.version === 1 &&
            payload.project && typeof payload.project === "object" &&
            typeof payload.project.id === "string" && payload.project.id !== "" &&
            Array.isArray(payload.operations);
    }

    function resumeListCommand(query) {
        let argv = ["python3", Quickshell.shellPath("scripts/desktop_resume.py"),
            "list", "--limit", "20"];
        if (query) argv.push("--query", query);
        return argv;
    }

    function finishResumeSearch(code, output, generation, requestQuery) {
        // Stale identity before touching busy/timer/error. A timeout is
        // terminal: its killed exit must not reschedule. Reschedule only
        // when a newer query exists (pending differs from the request).
        let isProcess = (generation === root.resumeProcessGeneration &&
            requestQuery === root.resumeProcessQuery);
        if (!isProcess) return;
        if (root.pendingResumeQuery !== requestQuery) {
            if (root.paletteOpen && root.shouldSearchResume()) {
                root.resumeBusy = false;
                resumeTimeout.stop();
                resumeDelay.restart();
            }
            return;
        }
        if (generation !== root.resumeGeneration) {
            // Same query but generation moved (timeout/cancel): terminal,
            // never reschedule.
            return;
        }
        root.resumeBusy = false;
        resumeTimeout.stop();
        if (code !== 0 || !root.paletteOpen || !root.shouldSearchResume() ||
                root.pendingResumeQuery !== root.modeQuery || root.resumeGeneration !== generation ||
                requestQuery !== root.modeQuery) {
            if (code !== 0 && root.paletteOpen && root.shouldSearchResume()) {
                root.resumeError = "Project search failed";
                root.loadFailed("resume", "Project search failed");
            }
            return;
        }
        try {
            let payload = JSON.parse(output || "{}");
            if (!root.validResumeList(payload)) throw new Error("invalid");
            root.resumeError = "";
            let entries = payload.entries.slice(0, 20);
            root.resumeRows = entries.filter((item) => item && typeof item === "object" &&
                    typeof item.id === "string" && item.id !== "").map((item) => ({
                prefix: "resume:", title: String(item.name || item.id),
                subtitle: String(item.logseq_path || item.local_folder || item.github_url || item.id),
                kind: "resume", payload: item, score: 1
            }));
            root.loaded("resume");
        } catch (error) {
            root.resumeRows = [];
            root.resumeError = "Project search returned invalid data";
            root.loaded("resume");
        }
    }

    function scheduleResumeSearch() {
        resumeDelay.restart();
    }

    function stopResumeSearch() {
        resumeDelay.stop();
        resumeTimeout.stop();
        if (resumeListProcess.running) resumeListProcess.running = false;
    }

    function cancelResumeSearch() {
        if (!root.resumeBusy) return;
        root.resumeBusy = false;
        root.resumeGeneration++;
        if (resumeListProcess.running) resumeListProcess.running = false;
        if (root.paletteOpen && root.shouldSearchResume()) {
            root.resumeError = "Project search timed out; retry";
            root.loadFailed("resume", "Project search timed out; retry");
        }
    }

    function startResumeSearch() {
        if (!root.paletteOpen || !root.shouldSearchResume()) return;
        if (resumeListProcess.running) { resumeDelay.restart(); return; }
        root.resumeBusy = true;
        root.resumeError = "";
        root.resumeProcessGeneration = root.resumeGeneration;
        root.resumeProcessQuery = root.modeQuery;
        resumeListProcess.command = root.resumeListCommand(root.modeQuery);
        resumeTimeout.restart();
        resumeListProcess.running = true;
    }

    function ensureResumePlan(projectId) {
        let wanted = String(projectId === undefined || projectId === null ? "" : projectId);
        if (!wanted || !root.paletteOpen || !root.shouldSearchResume()) return false;
        // Cached plan is authoritative for its project: reselecting it
        // while another id retires must keep the cache and retire the
        // in-flight request, never install the stale id.
        if (root.resumePlan && root.resumePlanProjectId === wanted) {
            if (root.pendingResumePlanId !== wanted) {
                root.resumePlanGeneration++;
                root.pendingResumePlanId = wanted;
            }
            if (resumePlanProcess.running || root.resumePlanRetiring ||
                    root.resumePlanProcessId !== wanted) {
                if (resumePlanProcess.running) {
                    root.resumePlanRetiring = true;
                    root.resumePlanBusy = false;
                    resumePlanTimeout.stop();
                    resumePlanProcess.running = false;
                    // No delayed exit will arrive when the process never
                    // started; clear the retiring marker in that case only.
                    // When a kill was issued, the old exit stays stale via
                    // the pending/process mismatch and clears retiring.
                    return true;
                }
                if (root.resumePlanRetiring && !resumePlanProcess.running) {
                    // Retiring without a running process means the old exit
                    // already arrived or never started: adopt the cache.
                    root.resumePlanRetiring = false;
                    root.resumePlanProcessGeneration = root.resumePlanGeneration;
                    root.resumePlanProcessId = wanted;
                }
                root.resumePlanBusy = false;
                resumePlanTimeout.stop();
                return true;
            }
            root.resumePlanBusy = false;
            resumePlanTimeout.stop();
            root.resumePlanRetiring = false;
            return true;
        }
        if (root.resumePlanRetiring) {
            if (root.pendingResumePlanId !== wanted) {
                root.resumePlanGeneration++;
                root.pendingResumePlanId = wanted;
            }
            root.resumePlanBusy = true;
            return true;
        }
        if (resumePlanProcess.running || root.resumePlanBusy) {
            if (root.pendingResumePlanId === wanted && root.resumePlanProcessId === wanted &&
                    root.resumePlanProcessGeneration === root.resumePlanGeneration) return true;
            // Superseding request: queue the desired id, retire the old
            // launch without overwriting its immutable metadata. The old
            // exit is classified stale/superseded and launches the desired
            // id afterwards (serialization for a single Process).
            root.resumePlanGeneration++;
            root.pendingResumePlanId = wanted;
            root.resumePlanBusy = true;
            root.resumePlanRetiring = true;
            resumePlanTimeout.stop();
            if (resumePlanProcess.running) resumePlanProcess.running = false;
            if (!resumePlanProcess.running) {
                root.resumePlanRetiring = false;
                root.resumePlanProcessGeneration = root.resumePlanGeneration;
                root.resumePlanProcessId = wanted;
                root.resumePlanError = "";
                resumePlanTimeout.restart();
                resumePlanProcess.command = ["python3", Quickshell.shellPath("scripts/desktop_resume.py"),
                    "plan", "--project", wanted];
                resumePlanProcess.running = true;
            }
            return true;
        }
        root.resumePlanGeneration++;
        root.resumePlanProcessGeneration = root.resumePlanGeneration;
        root.pendingResumePlanId = wanted;
        root.resumePlanProcessId = wanted;
        root.resumePlanBusy = true;
        root.resumePlanError = "";
        root.resumePlanRetiring = false;
        resumePlanProcess.command = ["python3", Quickshell.shellPath("scripts/desktop_resume.py"),
            "plan", "--project", wanted];
        resumePlanTimeout.restart();
        resumePlanProcess.running = true;
        return true;
    }

    function launchDesiredResumePlan(desired) {
        root.resumePlanProcessGeneration = root.resumePlanGeneration;
        root.resumePlanProcessId = desired;
        root.resumePlanBusy = true;
        root.resumePlanError = "";
        root.resumePlanRetiring = false;
        resumePlanTimeout.restart();
        resumePlanProcess.command = ["python3", Quickshell.shellPath("scripts/desktop_resume.py"),
            "plan", "--project", desired];
        resumePlanProcess.running = true;
    }

    function finishResumePlan(code, output, generation, requestId) {
        // Never touch busy/timer/error before stale identity checks.
        // Tier 1: not the current in-flight launch -> truly stale.
        if (generation !== root.resumePlanProcessGeneration ||
                requestId !== root.resumePlanProcessId) return;
        // Tier 2: in-flight but superseded by a newer desired id.
        if (root.pendingResumePlanId !== requestId) {
            let desired = String(root.pendingResumePlanId || "");
            // Cached desired becomes authoritative; never install stale id.
            if (desired && root.resumePlan && root.resumePlanProjectId === desired) {
                root.resumePlanRetiring = false;
                root.resumePlanBusy = false;
                resumePlanTimeout.stop();
                root.resumePlanProcessGeneration = root.resumePlanGeneration;
                root.resumePlanProcessId = desired;
                return;
            }
            // Timeout with the same pending query is terminal above only
            // when generations differ but pending matches; here pending
            // differs, so a newer project exists: launch it.
            if (desired && root.paletteOpen && root.shouldSearchResume()) {
                root.launchDesiredResumePlan(desired);
            } else {
                root.resumePlanRetiring = false;
                root.resumePlanBusy = false;
                resumePlanTimeout.stop();
            }
            return;
        }
        if (generation !== root.resumePlanGeneration) {
            // Same pending id but generation moved (timeout/cancel):
            // terminal, never reschedule or install.
            root.resumePlanRetiring = false;
            return;
        }
        root.resumePlanBusy = false;
        resumePlanTimeout.stop();
        root.resumePlanRetiring = false;
        // Plans belong only to the currently selected project: drop any
        // payload whose generation or id no longer matches the pending id.
        if (!root.paletteOpen || !root.shouldSearchResume() ||
                root.resumePlanGeneration !== generation ||
                root.pendingResumePlanId !== requestId ||
                requestId !== root.pendingResumePlanId) {
            return;
        }
        if (code !== 0) {
            root.resumePlan = null;
            root.resumePlanError = "Project plan failed";
            root.loaded("resumePlan");
            return;
        }
        try {
            let payload = JSON.parse(output || "{}");
            if (!root.validResumePlan(payload)) throw new Error("invalid");
            // Accept only the plan for the currently selected project id.
            let planId = String(payload.project.id || "");
            if (planId !== String(root.pendingResumePlanId || "")) return;
            root.resumePlan = payload;
            root.resumePlanProjectId = planId;
            root.resumePlanError = "";
            root.loaded("resumePlan");
        } catch (error) {
            root.resumePlan = null;
            root.resumePlanError = "Project plan returned invalid data";
            root.loaded("resumePlan");
        }
    }

    function cancelResumePlan() {
        if (!root.resumePlanBusy) return;
        root.resumePlanBusy = false;
        root.resumePlanGeneration++;
        if (resumePlanProcess.running) resumePlanProcess.running = false;
        if (root.paletteOpen && root.shouldSearchResume()) {
            root.resumePlanError = "Project plan timed out; retry";
            root.loaded("resumePlan");
        }
    }

    function stopResumePlan() {
        resumePlanTimeout.stop();
        if (resumePlanProcess.running) resumePlanProcess.running = false;
    }

    function resetForQueryChange() {
        root.fileError = "";
        root.fileTruncated = false;
        root.fileRows = [];
        root.fileGeneration++;
        fileDelay.stop();
        if (fileProcess.running) fileProcess.running = false;
        root.resumeError = "";
        root.resumeRows = [];
        root.resumeGeneration++;
        resumeDelay.stop();
        resumeTimeout.stop();
        if (resumeListProcess.running) resumeListProcess.running = false;
        // A new query invalidates the selected plan preview; the palette
        // requests a fresh plan for the new selection after rebuild.
        root.resumePlan = null;
        root.resumePlanError = "";
        root.resumePlanProjectId = "";
        root.resumePlanGeneration++;
        root.resumePlanProcessGeneration = root.resumePlanGeneration;
        root.resumePlanProcessId = "";
        root.pendingResumePlanId = "";
        root.resumePlanBusy = false;
        root.resumePlanRetiring = false;
        resumePlanTimeout.stop();
        if (resumePlanProcess.running) resumePlanProcess.running = false;
    }

    function resetForOpen() {
        root.todos = [];
        root.clipboardText = "";
        root.fileRows = [];
        root.fileError = "";
        root.todoLoaded = false;
        root.todoPending = false;
        root.todoFailed = false;
        root.clipboardLoaded = false;
        root.clipboardPending = false;
        root.clipboardFailed = false;
        root.fileBusy = false;
        root.fileTruncated = false;
        root.pendingFileQuery = "";
        root.resumeRows = [];
        root.resumeError = "";
        root.resumeBusy = false;
        root.pendingResumeQuery = "";
        root.resumePlan = null;
        root.resumePlanError = "";
        root.resumePlanBusy = false;
        root.resumePlanProjectId = "";
        root.pendingResumePlanId = "";
        root.resumePlanRetiring = false;
        // Do not stop in-flight processes here (matches the pre-extraction
        // open()): the openingGeneration bump already invalidates them, and
        // their stale completions reschedule via reloadRequested().
    }

    function resetForClose() {
        root.todoPending = false;
        root.clipboardPending = false;
        root.todos = [];
        root.clipboardText = "";
        root.fileRows = [];
        root.fileBusy = false;
        root.fileGeneration++;
        if (fileProcess.running) fileProcess.running = false;
        if (todoProcess.running) todoProcess.running = false;
        if (clipboardProcess.running) clipboardProcess.running = false;
        fileDelay.stop();
        root.resumeRows = [];
        root.resumeBusy = false;
        root.resumeError = "";
        root.resumeGeneration++;
        root.resumePlan = null;
        root.resumePlanError = "";
        root.resumePlanBusy = false;
        root.resumePlanProjectId = "";
        root.pendingResumePlanId = "";
        root.pendingResumeQuery = "";
        root.resumePlanGeneration++;
        root.resumePlanProcessGeneration = root.resumePlanGeneration;
        root.resumePlanProcessId = "";
        root.resumePlanRetiring = false;
        if (resumeListProcess.running) resumeListProcess.running = false;
        if (resumePlanProcess.running) resumePlanProcess.running = false;
        resumeDelay.stop();
        resumeTimeout.stop();
        resumePlanTimeout.stop();
    }
}
