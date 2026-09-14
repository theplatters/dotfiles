import QtQuick
import Quickshell
import Quickshell.Io

// Shared daily-agenda state. Lives once in shell.qml so the calendar
// popout, the planner Daily tab, and the Pomodoro timer all share one
// date, one task snapshot, and one backend operation at a time. Closing
// a popup never stops the timer or drops a completion draft.
Item {
    id: root

    // Emitted with the full read_page response after select/complete/
    // toggle so ProjectPlanner can refresh its page cache without
    // interrupting agent work.
    signal pageWritten(var page)

    property string selectedDate: todayIso()
    property var agendaTasks: []
    property string graphName: "Notes"
    property bool agendaTruncated: false
    property bool agendaBusy: false
    property bool agendaStarted: false
    property bool agendaStartFailed: false
    property bool agendaRetiring: false
    property int agendaGeneration: 0
    property int agendaLaunchGeneration: 0
    property string agendaOp: ""
    property string agendaLaunchOp: ""
    property var agendaPayload: null
    property string agendaError: ""
    property string agendaNotice: ""
    property bool agendaSlow: false
    // Sticky mutation error preserved through the automatic recovery list
    // so a failed select/toggle is not cleared by the following list.
    property string agendaStickyError: ""

    // Frozen completion snapshot (path/revision/line/task). The note draft
    // stays attached to this snapshot until explicit cancel/save, so a
    // refresh or date change can never attach it to the wrong TODO.
    property var completionTask: null
    property string completionNote: ""
    property string completionError: ""
    property bool completionSaving: false

    // Pomodoro (UI-only timer; the backend only stores completion notes).
    property int focusMinutes: 25
    property int breakMinutes: 5
    property string pomoPhase: "idle"
    property bool pomoRunning: false
    property double pomoDeadline: 0
    property int pomoRemainingSec: 0
    property int pomoCompleted: 0
    property string pomoMessage: ""

    function pad2(value) {
        let n = Number(value)
        return (n < 10 ? "0" : "") + n
    }

    function isoDate(year, month1, day) {
        return year + "-" + root.pad2(month1) + "-" + root.pad2(day)
    }

    function todayIso() {
        let now = new Date()
        return root.isoDate(now.getFullYear(), now.getMonth() + 1, now.getDate())
    }

    function parseIsoDate(value) {
        if (typeof value !== "string") return null
        let match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
        if (!match) return null
        let y = Number(match[1]), m = Number(match[2]), d = Number(match[3])
        if (m < 1 || m > 12 || d < 1 || d > 31) return null
        let probe = new Date(y, m - 1, d)
        if (probe.getFullYear() !== y || probe.getMonth() !== m - 1 || probe.getDate() !== d) return null
        return { year: y, month: m, day: d }
    }

    function isValidIso(value) {
        return root.parseIsoDate(value) !== null
    }

    function monthLabel(year, month0) {
        let names = ["January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December"]
        return names[month0] + " " + year
    }

    // Monday-start 6x7 grid for a month view. Returns 42 cells
    // {iso, y, m, d, inMonth} with m 1-based.
    function monthGrid(year, month0) {
        let first = new Date(year, month0, 1)
        let offset = (first.getDay() + 6) % 7
        let cells = []
        for (let i = 0; i < 42; i++) {
            let day = new Date(year, month0, 1 - offset + i)
            let y = day.getFullYear(), m = day.getMonth() + 1, d = day.getDate()
            cells.push({ iso: root.isoDate(y, m, d), y: y, m: m, d: d,
                inMonth: day.getMonth() === month0 })
        }
        return cells
    }

    // Agenda view: tasks scheduled exactly on the selected date (open and
    // done), in backend order.
    function agendaItemsFor(tasks, date) {
        let rows = Array.isArray(tasks) ? tasks : []
        return rows.filter(entry => entry && entry.scheduledDate === date)
    }

    // Picker view: all open tasks, optionally filtered by free text over
    // task/page/path.
    function pickerItemsFor(tasks, filter) {
        let rows = Array.isArray(tasks) ? tasks : []
        let open = rows.filter(entry => entry && !entry.done)
        let needle = String(filter || "").trim().toLowerCase()
        if (!needle) return open
        return open.filter(entry =>
            String(entry.task || "").toLowerCase().indexOf(needle) >= 0 ||
            String(entry.page || "").toLowerCase().indexOf(needle) >= 0 ||
            String(entry.path || "").toLowerCase().indexOf(needle) >= 0)
    }

    function agendaItems() {
        return root.agendaItemsFor(root.agendaTasks, root.selectedDate)
    }

    function pickerItems(filter) {
        return root.pickerItemsFor(root.agendaTasks, filter)
    }

    function graphArg() {
        // Empty means "no override": the Python helper resolves
        // LOGSEQ_GRAPH, then logseqGraph in settings.json.
        return Quickshell.env("LOGSEQ_GRAPH") || ""
    }

    function closeInput(process) {
        if (process.closeWriteChannel) process.closeWriteChannel()
        process.stdinEnabled = false
    }

    function writeJson(process, value) {
        process.write(JSON.stringify(value) + "\n")
        root.closeInput(process)
    }

    function failure(label, code, detail) {
        let text = String(detail === undefined || detail === null ? "" : detail)
            .replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim()
        if (text.length > 300) text = text.substring(0, 299) + "…"
        return label + " failed" + (code ? " (exit " + code + ")" : "") +
            (text ? ": " + text : "") + ". Check LOGSEQ_GRAPH or settings.json and retry."
    }

    function busyReason() {
        if (root.completionSaving) return "Wait for the completion write to finish."
        if (root.agendaBusy) return "Wait for the agenda request to finish."
        return ""
    }

    function setSelectedDate(date) {
        if (!root.isValidIso(date)) {
            root.agendaError = "Pick a valid calendar day (YYYY-MM-DD)."
            return false
        }
        if (root.completionSaving) {
            root.agendaError = "Wait for the completion write to finish before changing the date."
            return false
        }
        if (root.agendaBusy || root.agendaRetiring) {
            root.agendaError = "The agenda request is still running; retry shortly."
            return false
        }
        root.agendaError = ""
        root.selectedDate = date
        root.startList()
        return true
    }

    function reload() {
        if (root.agendaBusy || root.agendaRetiring) {
            root.agendaError = "The agenda request is still running; retry shortly."
            return false
        }
        if (root.completionSaving) {
            root.agendaError = "Wait for the completion write to finish."
            return false
        }
        return root.startList()
    }

    function launch(op, payload, scriptCommand) {
        if (root.agendaBusy || agendaProcess.running || root.agendaRetiring) {
            root.agendaError = "The agenda request is still running; retry shortly."
            return false
        }
        root.agendaGeneration++
        root.agendaOp = op
        root.agendaPayload = payload
        root.agendaLaunchGeneration = root.agendaGeneration
        root.agendaLaunchOp = op
        root.agendaBusy = true
        root.agendaStarted = false
        root.agendaStartFailed = false
        root.agendaSlow = false
        agendaProcess.command = ["python3", Quickshell.shellPath("scripts/daily_agenda.py"),
            "--graph", root.graphArg(), scriptCommand]
        if (op === "list") agendaTimeout.restart()
        else agendaWriteWarning.restart()
        agendaProcess.stdinEnabled = true
        agendaProcess.running = true
        return true
    }

    function startList() {
        return root.launch("list", { date: root.selectedDate }, "list")
    }

    function selectEntry(entry, selected) {
        if (!entry || root.agendaBusy || root.agendaRetiring) {
            root.agendaError = "The agenda request is still running; retry shortly."
            return false
        }
        if (root.completionSaving) {
            root.agendaError = "Wait for the completion write to finish."
            return false
        }
        let flag = selected === undefined ? !(entry.scheduledDate === root.selectedDate) : !!selected
        root.agendaError = ""
        root.agendaNotice = flag ? "Scheduling…" : "Removing from day…"
        return root.launch("select", { path: String(entry.path || ""),
            revision: String(entry.revision || ""), line: Number(entry.line || 0),
            date: root.selectedDate, selected: flag }, "select")
    }

    function toggleEntry(entry) {
        if (!entry || root.agendaBusy || root.agendaRetiring) {
            root.agendaError = "The agenda request is still running; retry shortly."
            return false
        }
        if (root.completionSaving) {
            root.agendaError = "Wait for the completion write to finish."
            return false
        }
        root.agendaError = ""
        root.agendaNotice = "Toggling checkbox…"
        return root.launch("toggle", { path: String(entry.path || ""),
            revision: String(entry.revision || ""), line: Number(entry.line || 0),
            done: !Boolean(entry.done) }, "toggle")
    }

    function sameCompletionTarget(a, b) {
        if (!a || !b) return false
        return String(a.path || "") === String(b.path || "") &&
            String(a.revision || "") === String(b.revision || "") &&
            Number(a.line || 0) === Number(b.line || 0)
    }

    function isCompletionStale() {
        let message = String(root.completionError || "").toLowerCase()
        return message.indexOf("stale") >= 0 ||
            message.indexOf("changed elsewhere") >= 0 ||
            message.indexOf("reload") >= 0
    }

    function beginCompletion(entry) {
        if (!entry) return false
        if (root.completionSaving) {
            root.completionError = "Wait for the completion write to finish."
            return false
        }
        if (root.agendaBusy) {
            root.completionError = "Wait for the agenda request to finish."
            return false
        }
        if (root.completionTask !== null) {
            // A draft is already open: never silently drop its note.
            // Same frozen target is a no-op (keeps note/error for focus).
            if (root.sameCompletionTarget(root.completionTask, entry)) return true
            // Different task requires explicit Cancel/discard or an
            // explicit Reselect that preserves the note.
            root.completionError = "A completion draft is already open. Cancel to discard it, or press Reselect on the current task to keep your note and retarget."
            return false
        }
        // Freeze the snapshot (including revision) so later refreshes or
        // date changes cannot re-target the note.
        root.completionTask = { path: String(entry.path || ""),
            revision: String(entry.revision || ""), line: Number(entry.line || 0),
            task: String(entry.task || ""), page: String(entry.page || "") }
        root.completionNote = ""
        root.completionError = ""
        return true
    }

    // Explicit stale recovery: the user picks the current task from the
    // refreshed list. The note draft is always preserved; nothing is
    // matched automatically by text and the revision is never patched
    // while keeping an old line.
    function reselectCompletion(entry) {
        if (!entry) return false
        if (root.completionSaving) {
            root.completionError = "Wait for the completion write to finish."
            return false
        }
        if (root.agendaBusy || root.agendaRetiring) {
            root.completionError = "Wait for the agenda request to finish before reselecting."
            return false
        }
        if (root.completionTask === null) return root.beginCompletion(entry)
        root.completionTask = { path: String(entry.path || ""),
            revision: String(entry.revision || ""), line: Number(entry.line || 0),
            task: String(entry.task || ""), page: String(entry.page || "") }
        root.completionError = ""
        return true
    }

    function cancelCompletion() {
        if (root.completionSaving) return false
        root.completionTask = null
        root.completionNote = ""
        root.completionError = ""
        return true
    }

    function saveCompletion() {
        let frozen = root.completionTask
        if (!frozen || root.completionSaving || root.agendaBusy || root.agendaRetiring) return false
        let note = String(root.completionNote || "")
        if (!note.trim()) {
            root.completionError = "Write a progress note before completing."
            return false
        }
        root.completionError = ""
        root.completionSaving = true
        root.agendaGeneration++
        root.agendaOp = "complete"
        root.agendaPayload = { path: frozen.path, revision: frozen.revision,
            line: frozen.line, note: note, date: root.selectedDate }
        root.agendaLaunchGeneration = root.agendaGeneration
        root.agendaLaunchOp = "complete"
        root.agendaBusy = true
        root.agendaStarted = false
        root.agendaStartFailed = false
        root.agendaSlow = false
        agendaProcess.command = ["python3", Quickshell.shellPath("scripts/daily_agenda.py"),
            "--graph", root.graphArg(), "complete"]
        agendaWriteWarning.restart()
        agendaProcess.stdinEnabled = true
        agendaProcess.running = true
        return true
    }

    function handleAgendaStartFailure(generation) {
        if (generation !== root.agendaGeneration) return
        agendaTimeout.stop()
        agendaWriteWarning.stop()
        if (!root.agendaStarted && !agendaProcess.running) root.agendaRetiring = false
        let wasComplete = root.agendaLaunchOp === "complete"
        root.agendaBusy = false
        root.completionSaving = false
        let message = root.failure("Agenda", 0, "process could not start")
        if (wasComplete) {
            // Preserve the frozen draft; only surface the error.
            root.completionError = message
        } else {
            root.agendaError = message
        }
    }

    function cancelListRead() {
        if (!root.agendaBusy || root.agendaOp !== "list") return
        root.agendaRetiring = true
        root.agendaGeneration++
        root.agendaBusy = false
        root.agendaStartFailed = false
        agendaProcess.running = false
        root.agendaError = root.failure("Agenda", 0, "read timed out; retry")
    }

    function handleProcessRunningChanged() {
        if (!agendaProcess.running && (root.agendaBusy || root.agendaStarted))
            root.agendaRetiring = true
        if (!agendaProcess.running && root.agendaBusy && !root.agendaStarted && !root.agendaStartFailed) {
            root.agendaStartFailed = true
            root.handleAgendaStartFailure(root.agendaLaunchGeneration)
        }
    }

    function finishAgenda(code, output, diagnostic, generation, op) {
        if (generation !== root.agendaGeneration) return
        agendaTimeout.stop()
        agendaWriteWarning.stop()
        root.agendaBusy = false
        root.agendaSlow = false
        // Backend failures print {"error": "..."} to stdout with a nonzero
        // exit, so parse stdout regardless of code. Success additionally
        // requires code 0 and a valid shape.
        let data = null
        try { data = JSON.parse(output || "{}") } catch (error) { data = null }
        if (op === "list") {
            let validList = code === 0 && data &&
                String(data.date) === String(root.selectedDate) && Array.isArray(data.tasks)
            if (!validList) {
                let backendError = data && data.error ? String(data.error) : ""
                let message = backendError ? backendError :
                    root.failure("Agenda", code, diagnostic || "invalid JSON output")
                // A recovery list that itself fails must not drop the
                // mutation error it was recovering from.
                if (root.agendaStickyError !== "") root.agendaError = root.agendaStickyError
                else root.agendaError = message
                return
            }
            root.agendaTasks = data.tasks
            root.graphName = String(data.graphName || "Notes")
            root.agendaTruncated = !!data.truncated
            if (root.agendaStickyError !== "") {
                // Preserve the failed mutation error through the recovery
                // list instead of clearing it on a fresh list response.
                root.agendaError = root.agendaStickyError
                root.agendaStickyError = ""
            } else {
                root.agendaError = ""
                root.agendaNotice = ""
            }
            return
        }
        // Mutations return {page: read_page response}.
        let page = data && data.page ? data.page : null
        let shapeValid = page && typeof page === "object" && typeof page.path === "string" &&
            typeof page.revision === "string" && typeof page.content === "string" && Array.isArray(page.todos)
        let valid = code === 0 && shapeValid
        if (!valid) {
            let backendError = data && data.error ? String(data.error) : ""
            let stale = String(diagnostic || "").toLowerCase().indexOf("stale") >= 0 ||
                backendError.toLowerCase().indexOf("stale") >= 0
            let message = backendError ? backendError :
                (stale ? "The page changed elsewhere; reload before retrying." :
                    root.failure("Agenda", code, diagnostic || "write could not be verified"))
            if (op === "complete") {
                // Preserve the frozen draft and note; the user retries
                // explicitly after reloading (completionError survives the
                // recovery list below because list responses never touch it).
                root.completionSaving = false
                root.completionError = message
            } else {
                root.agendaStickyError = message
                root.agendaError = message
                root.agendaNotice = ""
            }
            // A failed write may still have committed; offer a reload.
            root.startList()
            return
        }
        if (op === "complete") {
            root.completionSaving = false
            root.completionTask = null
            root.completionNote = ""
            root.completionError = ""
        }
        root.agendaStickyError = ""
        root.agendaError = ""
        root.agendaNotice = ""
        root.pageWritten(page)
        root.startList()
    }

    // ---- Pomodoro (wall-clock deadlines; survives popup close) ----

    function pomoPhaseSeconds(phase) {
        let name = phase || root.pomoPhase
        if (name === "break") return Math.max(60, Number(root.breakMinutes || 0) * 60)
        return Math.max(60, Number(root.focusMinutes || 0) * 60)
    }

    function pomoFormatted() {
        let total = Math.max(0, Number(root.pomoRemainingSec || 0))
        let mins = Math.floor(total / 60)
        let secs = total % 60
        return (mins < 10 ? "0" : "") + mins + ":" + (secs < 10 ? "0" : "") + secs
    }

    function pomoStart(phase) {
        if (root.pomoRunning) return false
        let focus = Math.max(1, Math.floor(Number(root.focusMinutes) || 25))
        let rest = Math.max(1, Math.floor(Number(root.breakMinutes) || 5))
        root.focusMinutes = focus
        root.breakMinutes = rest
        if (phase === "focus" || phase === "break") root.pomoPhase = phase
        else if (root.pomoPhase !== "focus" && root.pomoPhase !== "break") root.pomoPhase = "focus"
        if (!(root.pomoRemainingSec > 0)) root.pomoRemainingSec = root.pomoPhaseSeconds(root.pomoPhase)
        root.pomoDeadline = Date.now() + Number(root.pomoRemainingSec) * 1000
        root.pomoRunning = true
        root.pomoMessage = ""
        pomoTimer.restart()
        return true
    }

    function pomoPause() {
        if (!root.pomoRunning) return false
        root.pomoRemainingSec = Math.max(0, Math.round((Number(root.pomoDeadline) - Date.now()) / 1000))
        root.pomoRunning = false
        pomoTimer.stop()
        return true
    }

    function pomoReset() {
        root.pomoRunning = false
        pomoTimer.stop()
        root.pomoPhase = "idle"
        root.pomoRemainingSec = 0
        root.pomoDeadline = 0
        root.pomoMessage = ""
    }

    function pomoTick() {
        if (!root.pomoRunning) return
        root.pomoRemainingSec = Math.max(0, Math.round((Number(root.pomoDeadline) - Date.now()) / 1000))
        if (root.pomoRemainingSec > 0) return
        root.pomoRunning = false
        pomoTimer.stop()
        if (root.pomoPhase === "focus") {
            root.pomoCompleted++
            root.pomoPhase = "break"
            root.pomoRemainingSec = root.pomoPhaseSeconds("break")
            root.pomoMessage = "Focus complete — start your break."
        } else if (root.pomoPhase === "break") {
            root.pomoPhase = "idle"
            root.pomoRemainingSec = 0
            root.pomoMessage = "Break over — ready for the next focus."
        }
    }

    Process {
        id: agendaProcess
        workingDirectory: Quickshell.shellPath(".")
        stdinEnabled: false
        stdout: StdioCollector { id: agendaOutput; waitForEnd: true }
        stderr: StdioCollector { id: agendaErrorOut; waitForEnd: true }
        onStarted: {
            root.agendaStarted = true
            root.writeJson(agendaProcess, root.agendaPayload)
        }
        onRunningChanged: root.handleProcessRunningChanged()
        onExited: (code) => {
            let failed = root.agendaStartFailed
            let generation = root.agendaLaunchGeneration
            let op = root.agendaLaunchOp
            root.agendaStarted = false
            root.agendaStartFailed = false
            root.agendaRetiring = false
            if (failed) root.handleAgendaStartFailure(generation)
            else root.finishAgenda(code, agendaOutput.text, agendaErrorOut.text, generation, op)
        }
    }

    Timer {
        id: agendaTimeout
        interval: 12000
        repeat: false
        onTriggered: root.cancelListRead()
    }

    // Writes are never killed on timeout: the helper may already have
    // committed. This only surfaces a recovery hint while the
    // authoritative response is pending.
    Timer {
        id: agendaWriteWarning
        interval: 12000
        repeat: false
        onTriggered: {
            if (!root.agendaBusy) return
            root.agendaSlow = true
            if (root.agendaLaunchOp === "complete") root.completionError = "The completion write is taking longer than expected; it will not be cancelled."
            else root.agendaNotice = "The agenda write is taking longer than expected; it will not be cancelled."
        }
    }

    Timer {
        id: pomoTimer
        interval: 1000
        repeat: true
        running: root.pomoRunning
        onTriggered: root.pomoTick()
    }

    Component.onCompleted: root.startList()
}
