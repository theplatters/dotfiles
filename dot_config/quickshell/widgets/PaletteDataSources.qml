/*
 * PaletteDataSources.qml — async todo/clipboard/file sourcing for the palette.
 *
 * Responsibility: own the Logseq TODO, clipboard-history, and file-search
 * processes plus their cached rows/flags. The palette orchestrates
 * rebuildModel/loadModeData; this component only fetches and reports.
 *
 * Owned state/processes:
 * - todoProcess (logseq_todos.py with LOGSEQ_GRAPH default),
 *   clipboardProcess (cliphist list), fileProcess (palette_files.py
 *   --query/--limit 40) + fileDelay Timer (180ms debounce).
 * - todos (var), clipboardText (string), fileRows (var), fileBusy (bool),
 *   fileTruncated (bool), fileError (string, "File search failed" or
 *   "File search returned invalid data"), todo/clipboard loaded/pending/
 *   failed flags. The pending flags are the extracted equivalents of the
 *   pre-refactor clipboardProcess.running/todoProcess.running display
 *   gates: true exactly while the load is in flight (see noMatchText).
 * - Generation props: openingGeneration (mirrored from palette),
 *   todoProcessGeneration, clipboardProcessGeneration, fileGeneration,
 *   fileProcessGeneration, pendingFileQuery, fileProcessQuery.
 *
 * Contract with CommandPalette:
 * - In: `property int openingGeneration`, `property bool paletteOpen`
 *   (mirror requestedOpen), `property string mode/modeQuery`,
 *   `property bool calculatorOnly` (mirror palette calculatorOnly() so
 *   file eligibility excludes calculator-only unified queries, matching
 *   the palette shouldSearchFiles).
 * - Out: `signal loaded(string source)` ("todo"/"clipboard"/"file") which
 *   the palette uses to rebuildModel() (file failures emit no loaded, only
 *   fileError for the palette notice; invalid file data emits loaded with
 *   fileError set so the palette rebuilds and notices);
 *   `signal loadFailed(string source, string message)` for file-search
 *   failures where no rebuild happens; `signal reloadRequested()` for
 *   stale completions that arrived while open and need loadModeData().
 * - Palette binds openingGeneration/paletteOpen/mode/modeQuery/calculatorOnly, calls
 *   ensureTodoData/ensureClipboardData/scheduleFileSearch/startFileSearch/
 *   reset helpers from loadModeData/rebuild/open/close, and reads
 *   todos/clipboardText/fileRows/fileBusy/fileTruncated for rebuildModel.
 *
 * Generation/staleness: ensure* pins the openingGeneration; finish* drops
 * stale generations without installing payloads. A stale file completion
 * while searching reschedules fileDelay (never installs); stale todo/
 * clipboard completions while open emit reloadRequested. A failed load is
 * terminal for this opening (failed flag set, loaded emitted once).
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

    signal loaded(string source)
    signal loadFailed(string source, string message)
    signal reloadRequested()

    function isUnifiedSearch() { return root.mode === "" && !!root.modeQuery; }

    Process {
        id: todoProcess
        command: ["python3", Quickshell.shellPath("scripts/logseq_todos.py"),
            Quickshell.env("LOGSEQ_GRAPH") || "/home/franzs/Nextcloud/Documents/Notes/"]
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

    function resetForQueryChange() {
        root.fileError = "";
        root.fileTruncated = false;
        root.fileRows = [];
        root.fileGeneration++;
        fileDelay.stop();
        if (fileProcess.running) fileProcess.running = false;
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
    }
}
