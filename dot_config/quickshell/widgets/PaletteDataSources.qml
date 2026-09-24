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
 *   --query/--limit 40) + fileDelay Timer (180ms debounce) + fileTimeout
 *   Timer (12000ms).
 * - resumeListProcess (desktop_resume.py list [--query]/--limit 20, bare
 *   lists recent) + resumeDelay Timer (180ms debounce) + resumeTimeout
 *   Timer (12000ms), resumePlanProcess (desktop_resume.py plan --project
 *   UUID) for the currently selected project only.
 * - seenListProcess (desktop_projects.py seen [--query]/[--limit 20],
 *   bare lists recent; flat {kind,label,identity,...} rows per the Phase
 *   2b backend contract) + seenDelay Timer (180ms debounce) +
 *   seenTimeout Timer (12000ms).
 * - sessionListProcess (sessions.py search --query/--limit 20, bare
 *   session: lists recent via list --limit 20) +
 *   sessionDelay Timer (180ms debounce) + sessionTimeout Timer (12000ms).
 * - todos (var), clipboardText (string), fileRows (var), fileBusy (bool),
 *   fileTruncated (bool), fileError (string, "File search failed" or
 *   "File search returned invalid data"), todo/clipboard loaded/pending/
 *   failed flags. Resume mirrors the file pattern: resumeRows (var),
 *   resumeBusy (bool), resumeError (string), resumePlan (var, validated
 *   ResumePlan v1 or null), resumePlanBusy (bool), resumePlanError (string),
 *   resumePlanProjectId (string). The pending flags are the extracted equivalents of the
 *   pre-refactor clipboardProcess.running/todoProcess.running display
 *   gates: true exactly while the load is in flight (see noMatchText).
 *   Seen mirrors the resume pattern: seenRows (var), seenBusy (bool),
 *   seenError (string), seenGeneration, seenProcessGeneration,
 *   pendingSeenQuery, seenProcessQuery. Session mirrors the seen pattern:
 *   sessionRows (var), sessionBusy (bool), sessionError (string),
 *   sessionGeneration, sessionProcessGeneration, pendingSessionQuery,
 *   sessionProcessQuery. Search entries are flat union rows; bare-list
 *   entries are nested {session, meta, pending, draft} shapes.
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
 *   `property bool calculatorOnly` (mirrors palette calculatorOnly(),
 *   kept for binding compat; file eligibility no longer depends on it —
 *   unified calculator queries keep every source, S-025).
 * - Out: `signal loaded(string source)` ("todo"/"clipboard"/"file"/
 *   "resume"/"resumePlan"/"seen"/"session") which the palette uses to rebuildModel().
 *   Failures and timeouts clear stale rows and emit loaded with the error
 *   set (one error row + Enter-to-retry in the palette); invalid data
 *   emits loaded with the error set so the palette rebuilds and notices.
 *   `signal loadFailed(string source, string message)` is retained for
 *   binding compat but no longer emitted by the search helpers.
 * - Palette binds openingGeneration/paletteOpen/mode/modeQuery/calculatorOnly, calls
 *   ensureTodoData/ensureClipboardData/scheduleFileSearch/startFileSearch/
 *   scheduleResumeSearch/startResumeSearch/ensureResumePlan/reset helpers
 *   from loadModeData/rebuild/open/close, and reads
 *   todos/clipboardText/fileRows/fileBusy/fileTruncated/resumeRows/
 *   resumeBusy/resumePlan/resumePlanBusy/seenRows/seenBusy/sessionRows/
 *   sessionBusy for rebuildModel.
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
    property var seenRows: []
    property bool seenBusy: false
    property string seenError: ""
    property bool seenTruncated: false
    // Backend day/query total for the "showing X of Y" line (S-041).
    property int seenTotal: 0
    property int seenGeneration: 0
    property int seenProcessGeneration: 0
    property string pendingSeenQuery: ""
    property string seenProcessQuery: ""
    // Bounded per-keystroke scoring (S-056): at most this many
    // clipboard lines are kept at ingest; the palette caps its
    // per-keystroke scan the same way, so work scales with the cap,
    // not with history size.
    property int maxSourceRows: 200
    property var sessionRows: []
    property bool sessionBusy: false
    property string sessionError: ""
    // Truncation reporting (S-041), mirroring the seen pattern: the
    // bare `list` payload carries {count, total}; `search` carries
    // {count, total, truncated} (§5.5). Defensive fallbacks keep the
    // line silent instead of wrong when keys are absent.
    property bool sessionTruncated: false
    property int sessionTotal: 0
    property int sessionGeneration: 0
    property int sessionProcessGeneration: 0
    property string pendingSessionQuery: ""
    property string sessionProcessQuery: ""

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

    Timer {
        id: fileTimeout
        interval: 12000
        repeat: false
        onTriggered: root.cancelFileSearch()
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

    Timer {
        id: seenDelay
        interval: 180
        onTriggered: root.startSeenSearch()
    }

    Timer {
        id: seenTimeout
        interval: 12000
        repeat: false
        onTriggered: root.cancelSeenSearch()
    }

    Process {
        id: seenListProcess
        stdout: StdioCollector { id: seenListOutput }
        onExited: (code) => root.finishSeenSearch(code, seenListOutput.text,
                                                 root.seenProcessGeneration, root.seenProcessQuery)
    }

    Timer {
        id: sessionDelay
        interval: 180
        onTriggered: root.startSessionSearch()
    }

    Timer {
        id: sessionTimeout
        interval: 12000
        repeat: false
        onTriggered: root.cancelSessionSearch()
    }

    Process {
        id: sessionListProcess
        stdout: StdioCollector { id: sessionListOutput }
        onExited: (code) => root.finishSessionSearch(code, sessionListOutput.text,
                                                     root.sessionProcessGeneration, root.sessionProcessQuery)
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
            let text = output || "";
            try {
                let cap = (typeof root.maxSourceRows === "number" && root.maxSourceRows > 0)
                    ? root.maxSourceRows : 200;
                let lines = String(text).split("\n");
                if (lines.length > cap) text = lines.slice(0, cap).join("\n");
            } catch (error) {}
            root.clipboardText = text;
            root.clipboardLoaded = true;
        } else {
            root.clipboardFailed = true;
        }
        root.loaded("clipboard");
    }

    function shouldSearchFiles() {
        // Unified search is always file-eligible: an arithmetic-looking
        // query ranks the calculator first but never suppresses files
        // (S-025). The mirrored calculatorOnly prop stays bound for
        // compat but no longer gates eligibility.
        return root.mode === "file" || root.isUnifiedSearch();
    }

    function finishFileSearch(code, output, generation, requestQuery) {
        root.fileBusy = false;
        fileTimeout.stop();
        if (root.paletteOpen && root.shouldSearchFiles() && root.modeQuery &&
                (root.fileGeneration !== generation || root.modeQuery !== requestQuery)) {
            fileDelay.restart();
            return;
        }
        if (code !== 0 || !root.paletteOpen || !root.shouldSearchFiles() ||
                root.pendingFileQuery !== root.modeQuery || root.fileGeneration !== generation ||
                requestQuery !== root.modeQuery) {
            if (code !== 0 && root.paletteOpen && root.shouldSearchFiles()) {
                // One error row + Retry: replace stale rows and rebuild
                // through loaded so the palette shows the Retry affordance.
                root.fileRows = [];
                root.fileError = "File search failed";
                root.loaded("file");
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
        fileTimeout.stop();
        if (fileProcess.running) fileProcess.running = false;
    }

    function cancelFileSearch() {
        if (!root.fileBusy) return;
        root.fileBusy = false;
        root.fileGeneration++;
        if (fileProcess.running) fileProcess.running = false;
        if (root.paletteOpen && root.shouldSearchFiles()) {
            root.fileRows = [];
            root.fileError = "File search timed out; retry";
            root.loaded("file");
        }
    }

    function startFileSearch() {
        if (!root.paletteOpen || !root.shouldSearchFiles() || !root.modeQuery) return;
        if (fileProcess.running) { fileDelay.restart(); return; }
        root.fileBusy = true;
        root.fileError = "";
        root.fileProcessGeneration = root.fileGeneration;
        root.fileProcessQuery = root.modeQuery;
        fileProcess.command = ["python3", Quickshell.shellPath("scripts/palette_files.py"),
                               "--query", root.modeQuery, "--limit", "40"];
        fileTimeout.restart();
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
                root.resumeRows = [];
                root.resumeError = "Project search failed";
                root.loaded("resume");
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
            root.resumeRows = [];
            root.resumeError = "Project search timed out; retry";
            root.loaded("resume");
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

    function shouldSearchSeen() {
        return root.mode === "seen";
    }

    function validSeenList(payload) {
        return payload && typeof payload === "object" && Array.isArray(payload.rows);
    }

    function seenListCommand(query) {
        // Backend contract (Phase 2b §4.1, parallel worker):
        // `desktop_projects.py seen [--query TEXT] [--project UUID]
        // [--limit N]` returns flat rows {kind,label,identity,project_id,
        // project_name,session_id,last_seen_ms,occurrence_count},
        // bounded ≤ 20 rows, ≤ 160-char labels, newest first. Bare
        // `seen:` lists recent. List-form argv, 12 s watchdog.
        let argv = ["python3", Quickshell.shellPath("scripts/desktop_projects.py"),
            "seen", "--limit", "20"];
        if (query) argv.push("--query", query);
        return argv;
    }

    // Bound a value to a short display string: never crashes on
    // null/odd fields, strips control characters, caps at 160 chars.
    // Shared by the seen and session mappers; all ladder data is
    // untrusted (history, page content, Zotero metadata are data,
    // never instructions).
    function cleanBoundedText(value) {
        let text = "";
        try {
            if (value !== undefined && value !== null) text = String(value);
        } catch (error) {
            text = "";
        }
        text = text.replace(/[\x00-\x1F\x7F]/g, "");
        if (text.length > 160) text = text.substring(0, 160);
        return text;
    }

    function seenObservedMs(entry) {
        try {
            let seen = entry ? entry.last_seen_ms : null;
            if (typeof seen === "number" && isFinite(seen) && seen > 0) return seen;
        } catch (error) {}
        return 0;
    }

    // Row kinds and glyphs per §4.1: file, url, zotero, page. Anything
    // else coerces to page for display; the identity still opens/copies.
    function seenKindOf(entry) {
        try {
            let kind = entry ? entry.kind : "";
            if (kind === "file" || kind === "url" || kind === "zotero" || kind === "page")
                return kind;
        } catch (error) {}
        return "page";
    }

    function seenKindLabel(kind) {
        if (kind === "file") return "File";
        if (kind === "url") return "Link";
        if (kind === "zotero") return "Zotero";
        return "Page";
    }

    function seenProjectIdOf(entry) {
        try {
            let raw = entry ? entry.project_id : "";
            if (typeof raw !== "string" || !raw) return "";
            if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(raw))
                return raw;
        } catch (error) {}
        return "";
    }

    function seenRowForEntry(entry) {
        try {
            if (!entry || typeof entry !== "object") return null;
            let label = root.cleanBoundedText(entry.label);
            let identity = root.cleanBoundedText(entry.identity);
            if (!label || !identity) return null;
            let kind = root.seenKindOf(entry);
            let projectName = root.cleanBoundedText(entry.project_name);
            let sessionId = "";
            try {
                if (entry.session_id !== undefined && entry.session_id !== null)
                    sessionId = String(entry.session_id);
            } catch (error) {
                sessionId = "";
            }
            sessionId = root.cleanBoundedText(sessionId);
            let observed = root.seenObservedMs(entry);
            let count = 0;
            try {
                let raw = entry.occurrence_count;
                if (typeof raw === "number" && isFinite(raw) && raw > 0)
                    count = Math.floor(raw);
            } catch (error) {
                count = 0;
            }
            let when = "";
            try {
                if (observed > 0) when = new Date(observed).toLocaleString();
            } catch (error) {
                when = "";
            }
            if (!when || when === "Invalid Date") when = "";
            let subtitle = root.seenKindLabel(kind) + " · " + (projectName || "Unresolved project");
            if (count > 1) subtitle += " · ×" + count;
            if (when) subtitle += " · " + when;
            subtitle = root.cleanBoundedText(subtitle);
            return {
                prefix: "seen:", title: label, subtitle: subtitle,
                kind: "seen",
                payload: {
                    kind: kind,
                    label: label,
                    identity: identity,
                    project_id: root.seenProjectIdOf(entry),
                    project_name: projectName,
                    session_id: sessionId,
                    last_seen_ms: observed,
                    occurrence_count: count
                },
                score: 1
            };
        } catch (error) {
            return null;
        }
    }

    // Store the backend truncated/total pair (S-041) alongside the
    // mapped rows. Non-numeric totals degrade to the shown count so
    // the "showing X of Y" line stays silent instead of wrong.
    function storeSeenTruncation(payload, shown) {
        let total = shown
        try {
            let n = Number(payload ? payload.total : NaN)
            if (typeof n === "number" && isFinite(n) && n > 0)
                total = Math.floor(n)
        } catch (error) {}
        root.seenTotal = total
        let flagged = false
        try {
            flagged = !!(payload && payload.truncated)
        } catch (error) {}
        root.seenTruncated = flagged || total > shown
    }

    // Same pair for session payloads (S-041): {count, total} on bare
    // list, {count, total, truncated} on search. Absent totals degrade
    // to the shown count (line stays silent).
    function storeSessionTruncation(payload, shown) {
        let total = shown
        try {
            let n = Number(payload ? payload.total : NaN)
            if (typeof n === "number" && isFinite(n) && n > 0)
                total = Math.floor(n)
        } catch (error) {}
        root.sessionTotal = total
        let flagged = false
        try {
            flagged = !!(payload && payload.truncated)
        } catch (error) {
            flagged = false
        }
        root.sessionTruncated = flagged || total > shown
    }

    function finishSeenSearch(code, output, generation, requestQuery) {
        // Stale identity before touching busy/timer/error. A timeout is
        // terminal: its killed exit must not reschedule. Reschedule only
        // when a newer query exists (pending differs from the request).
        let isProcess = (generation === root.seenProcessGeneration &&
            requestQuery === root.seenProcessQuery);
        if (!isProcess) return;
        if (root.pendingSeenQuery !== requestQuery) {
            if (root.paletteOpen && root.shouldSearchSeen()) {
                root.seenBusy = false;
                seenTimeout.stop();
                seenDelay.restart();
            }
            return;
        }
        if (generation !== root.seenGeneration) {
            // Same query but generation moved (timeout/cancel): terminal,
            // never reschedule.
            return;
        }
        root.seenBusy = false;
        seenTimeout.stop();
        if (code !== 0 || !root.paletteOpen || !root.shouldSearchSeen() ||
                root.pendingSeenQuery !== root.modeQuery || root.seenGeneration !== generation ||
                requestQuery !== root.modeQuery) {
            if (code !== 0 && root.paletteOpen && root.shouldSearchSeen()) {
                root.seenRows = [];
                root.seenTruncated = false;
                root.seenTotal = 0;
                root.seenError = "Seen search failed";
                root.loaded("seen");
            }
            return;
        }
        try {
            let payload = JSON.parse(output || "{}");
            if (!root.validSeenList(payload)) throw new Error("invalid");
            root.seenError = "";
            root.seenTruncated = !!payload.truncated;
            let rows = payload.rows.slice(0, 20);
            let mapped = [];
            for (let i = 0; i < rows.length; ++i) {
                let row = root.seenRowForEntry(rows[i]);
                if (row) mapped.push(row);
            }
            root.seenRows = mapped;
            root.storeSeenTruncation(payload, mapped.length);
            root.loaded("seen");
        } catch (error) {
            root.seenRows = [];
            root.seenTruncated = false;
            root.seenTotal = 0;
            root.seenError = "Seen search returned invalid data";
            root.loaded("seen");
        }
    }

    function scheduleSeenSearch() {
        seenDelay.restart();
    }

    function stopSeenSearch() {
        seenDelay.stop();
        seenTimeout.stop();
        if (seenListProcess.running) seenListProcess.running = false;
    }

    function cancelSeenSearch() {
        if (!root.seenBusy) return;
        root.seenBusy = false;
        root.seenGeneration++;
        if (seenListProcess.running) seenListProcess.running = false;
        if (root.paletteOpen && root.shouldSearchSeen()) {
            root.seenRows = [];
            root.seenTruncated = false;
            root.seenTotal = 0;
            root.seenError = "Seen search timed out; retry";
            root.loaded("seen");
        }
    }

    function startSeenSearch() {
        if (!root.paletteOpen || !root.shouldSearchSeen()) return;
        if (seenListProcess.running) { seenDelay.restart(); return; }
        root.seenBusy = true;
        root.seenError = "";
        root.seenProcessGeneration = root.seenGeneration;
        root.seenProcessQuery = root.modeQuery;
        seenListProcess.command = root.seenListCommand(root.modeQuery);
        seenTimeout.restart();
        seenListProcess.running = true;
    }

    function shouldSearchSessions() {
        return root.mode === "session";
    }

    function sessionRequestQuery() {
        return root.modeQuery;
    }

    function sessionListCommand(query) {
        // Unified `session:` source over the §0.2 union (content index ∪
        // activity FTS, already union-backed by `sessions.py search`).
        // Bare `session:` lists recent work sessions via the no-query
        // `list` command (search rejects empty queries). List entries are
        // nested {session, meta, pending, ...} shapes; search entries are
        // flat union rows. List-form argv, 12 s watchdog.
        if (!query) {
            return ["python3", Quickshell.shellPath("scripts/sessions.py"),
                "list", "--limit", "20"];
        }
        return ["python3", Quickshell.shellPath("scripts/sessions.py"),
            "search", "--query", query, "--limit", "20"];
    }

    function validSessionList(payload) {
        return payload && typeof payload === "object" && Array.isArray(payload.entries);
    }

    function sessionClock(ms) {
        try {
            let date = new Date(ms);
            let hours = date.getHours();
            let minutes = date.getMinutes();
            let hourText = hours < 10 ? "0" + hours : String(hours);
            let minuteText = minutes < 10 ? "0" + minutes : String(minutes);
            return hourText + ":" + minuteText;
        } catch (error) {
            return "";
        }
    }

    function sessionRangeLabel(startMs, endMs) {
        let hasStart = typeof startMs === "number" && isFinite(startMs);
        let hasEnd = typeof endMs === "number" && isFinite(endMs);
        if (hasStart && hasEnd) return root.sessionClock(startMs) + "–" + root.sessionClock(endMs);
        if (hasStart) return root.sessionClock(startMs);
        return "";
    }

    function sessionProjectIdOf(value) {
        try {
            if (typeof value !== "string" || !value) return "";
            if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value))
                return value;
        } catch (error) {}
        return "";
    }

    // Flat union rows from `sessions.py search`: {session_id, device_id,
    // project_id, project_name, start_ms, end_ms, matched, resource_copy}.
    // NOTE (§4.2 gap): the union payload carries no TODO counts and no
    // thought text beyond the matched snippet, so rows show
    // `time range · project · match source · snippet` instead of the
    // aspirational `N TODOs` count. Counts arrive only if the backend
    // adds them; the mapper degrades without them.
    function sessionRowForSearchEntry(entry) {
        try {
            if (!entry || typeof entry !== "object") return null;
            let sid = "";
            try {
                if (entry.session_id !== undefined && entry.session_id !== null)
                    sid = String(entry.session_id);
            } catch (error) {
                sid = "";
            }
            if (!/^[0-9a-f]{32}$/i.test(sid)) return null;
            let projectId = root.sessionProjectIdOf(entry.project_id);
            let projectName = root.cleanBoundedText(entry.project_name);
            let displayProject = projectName || "Unresolved project";
            let matched = "";
            try {
                if (entry.matched === "content" || entry.matched === "collector")
                    matched = String(entry.matched);
            } catch (error) {
                matched = "";
            }
            let snippet = root.cleanBoundedText(entry.resource_copy);
            let title = snippet || ("Session " + sid.substring(0, 8));
            let startMs = (typeof entry.start_ms === "number" && isFinite(entry.start_ms))
                ? entry.start_ms : null;
            let endMs = (typeof entry.end_ms === "number" && isFinite(entry.end_ms))
                ? entry.end_ms : null;
            let range = root.sessionRangeLabel(
                (typeof entry.start_ms === "number" && isFinite(entry.start_ms)) ? entry.start_ms : NaN,
                (typeof entry.end_ms === "number" && isFinite(entry.end_ms)) ? entry.end_ms : NaN);
            let rawSubtitle = (range ? range + " · " : "") + displayProject;
            if (matched === "content") rawSubtitle += " · thought/TODO match";
            else if (matched === "collector") rawSubtitle += " · activity match";
            let subtitle = root.cleanBoundedText(rawSubtitle);
            return {
                prefix: "session:", title: title, subtitle: subtitle,
                kind: "session",
                payload: {
                    session_id: sid,
                    project_id: projectId,
                    project_name: projectName,
                    matched: matched,
                    resource_copy: snippet,
                    start_ms: startMs,
                    end_ms: endMs
                },
                score: 1
            };
        } catch (error) {
            return null;
        }
    }

    // Nested shapes from bare `sessions.py list`: {session, meta,
    // pending, pending_draft, pending_capture, attended, draft}. Draft
    // stubs carry no text, so nested rows show pending state instead of
    // a snippet; "needs attention" is the default sort and the bar
    // badge (§4.2), never a separate prefix.
    function sessionRowForListEntry(entry) {
        try {
            if (!entry || typeof entry !== "object") return null;
            let session = entry.session;
            if (!session || typeof session !== "object") return null;
            let sid = "";
            try {
                if (session.session_id !== undefined && session.session_id !== null)
                    sid = String(session.session_id);
            } catch (error) {
                sid = "";
            }
            if (!/^[0-9a-f]{32}$/i.test(sid)) return null;
            let project = session.project && typeof session.project === "object" ? session.project : null;
            let projectId = "";
            let projectName = "";
            try {
                if (project) {
                    projectId = root.sessionProjectIdOf(project.id);
                    if (project.name !== undefined && project.name !== null)
                        projectName = root.cleanBoundedText(project.name);
                }
            } catch (error) {}
            let pending = entry.pending === true;
            let attended = entry.attended === true;
            let title = "Session " + sid.substring(0, 8);
            if (pending) title += " · needs attention";
            let displayProject = projectName || "Unresolved project";
            let range = root.sessionRangeLabel(
                (typeof session.start_ms === "number" && isFinite(session.start_ms)) ? session.start_ms : NaN,
                (typeof session.end_ms === "number" && isFinite(session.end_ms)) ? session.end_ms : NaN);
            let rawSubtitle = (range ? range + " · " : "") + displayProject;
            rawSubtitle += pending ? " · needs attention" : (attended ? " · done" : "");
            let subtitle = root.cleanBoundedText(rawSubtitle);
            let startMs = (typeof session.start_ms === "number" && isFinite(session.start_ms))
                ? session.start_ms : null;
            let endMs = (typeof session.end_ms === "number" && isFinite(session.end_ms))
                ? session.end_ms : null;
            return {
                prefix: "session:", title: title, subtitle: subtitle,
                kind: "session",
                payload: {
                    session_id: sid,
                    project_id: projectId,
                    project_name: projectName,
                    pending: pending,
                    attended: attended,
                    resource_copy: "",
                    start_ms: startMs,
                    end_ms: endMs
                },
                score: 1
            };
        } catch (error) {
            return null;
        }
    }

    function finishSessionSearch(code, output, generation, requestQuery) {
        // Stale identity before touching busy/timer/error. A timeout is
        // terminal: its killed exit must not reschedule. Reschedule only
        // when a newer query exists (pending differs from the request).
        let isProcess = (generation === root.sessionProcessGeneration &&
            requestQuery === root.sessionProcessQuery);
        if (!isProcess) return;
        if (root.pendingSessionQuery !== requestQuery) {
            if (root.paletteOpen && root.shouldSearchSessions()) {
                root.sessionBusy = false;
                sessionTimeout.stop();
                sessionDelay.restart();
            }
            return;
        }
        if (generation !== root.sessionGeneration) {
            // Same query but generation moved (timeout/cancel): terminal,
            // never reschedule.
            return;
        }
        root.sessionBusy = false;
        sessionTimeout.stop();
        if (code !== 0 || !root.paletteOpen || !root.shouldSearchSessions() ||
                root.pendingSessionQuery !== requestQuery || root.sessionGeneration !== generation ||
                requestQuery !== root.sessionRequestQuery()) {
            if (code !== 0 && root.paletteOpen && root.shouldSearchSessions()) {
                root.sessionRows = [];
                root.sessionTruncated = false;
                root.sessionTotal = 0;
                root.sessionError = "Session search failed";
                root.loaded("session");
            }
            return;
        }
        try {
            let payload = JSON.parse(output || "{}");
            if (!root.validSessionList(payload)) throw new Error("invalid");
            root.sessionError = "";
            let entries = payload.entries.slice(0, 20);
            let rows = [];
            // Bare-session `list` entries are nested shapes; search
            // entries are flat. The request query tells them apart.
            let nested = !requestQuery;
            for (let i = 0; i < entries.length; ++i) {
                let row = nested ? root.sessionRowForListEntry(entries[i])
                    : root.sessionRowForSearchEntry(entries[i]);
                if (row) rows.push(row);
            }
            root.sessionRows = rows;
            root.storeSessionTruncation(payload, rows.length);
            root.loaded("session");
        } catch (error) {
            root.sessionRows = [];
            root.sessionTruncated = false;
            root.sessionTotal = 0;
            root.sessionError = "Session search returned invalid data";
            root.loaded("session");
        }
    }

    function scheduleSessionSearch() {
        sessionDelay.restart();
    }

    function stopSessionSearch() {
        sessionDelay.stop();
        sessionTimeout.stop();
        if (sessionListProcess.running) sessionListProcess.running = false;
    }

    function cancelSessionSearch() {
        if (!root.sessionBusy) return;
        root.sessionBusy = false;
        root.sessionGeneration++;
        if (sessionListProcess.running) sessionListProcess.running = false;
        if (root.paletteOpen && root.shouldSearchSessions()) {
            root.sessionRows = [];
            root.sessionTruncated = false;
            root.sessionTotal = 0;
            root.sessionError = "Session search timed out; retry";
            root.loaded("session");
        }
    }

    function startSessionSearch() {
        if (!root.paletteOpen || !root.shouldSearchSessions()) return;
        if (sessionListProcess.running) { sessionDelay.restart(); return; }
        root.sessionBusy = true;
        root.sessionError = "";
        root.sessionProcessGeneration = root.sessionGeneration;
        root.sessionProcessQuery = root.sessionRequestQuery();
        sessionListProcess.command = root.sessionListCommand(root.modeQuery);
        sessionTimeout.restart();
        sessionListProcess.running = true;
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
        fileTimeout.stop();
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
        root.seenError = "";
        root.seenTruncated = false;
        root.seenRows = [];
        root.seenGeneration++;
        seenDelay.stop();
        seenTimeout.stop();
        if (seenListProcess.running) seenListProcess.running = false;
        root.sessionError = "";
        root.sessionRows = [];
        root.sessionGeneration++;
        sessionDelay.stop();
        sessionTimeout.stop();
        if (sessionListProcess.running) sessionListProcess.running = false;
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
        root.seenRows = [];
        root.seenError = "";
        root.seenTruncated = false;
        root.seenBusy = false;
        root.pendingSeenQuery = "";
        root.sessionRows = [];
        root.sessionError = "";
        root.sessionBusy = false;
        root.pendingSessionQuery = "";
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
        root.fileError = "";
        root.fileGeneration++;
        if (fileProcess.running) fileProcess.running = false;
        if (todoProcess.running) todoProcess.running = false;
        if (clipboardProcess.running) clipboardProcess.running = false;
        fileDelay.stop();
        fileTimeout.stop();
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
        root.seenRows = [];
        root.seenBusy = false;
        root.seenError = "";
        root.seenTruncated = false;
        root.seenGeneration++;
        root.pendingSeenQuery = "";
        root.sessionRows = [];
        root.sessionBusy = false;
        root.sessionError = "";
        root.sessionGeneration++;
        root.pendingSessionQuery = "";
        if (seenListProcess.running) seenListProcess.running = false;
        if (sessionListProcess.running) sessionListProcess.running = false;
        if (resumeListProcess.running) resumeListProcess.running = false;
        if (resumePlanProcess.running) resumePlanProcess.running = false;
        resumeDelay.stop();
        resumeTimeout.stop();
        resumePlanTimeout.stop();
        seenDelay.stop();
        seenTimeout.stop();
        sessionDelay.stop();
        sessionTimeout.stop();
    }
}
