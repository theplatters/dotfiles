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

    // Capture inbox state (workstream D, UI half). Shared daily-agenda
    // state object owns the inbox so the calendar popout and the planner
    // Daily tab show one list, one preview, one backend operation.
    // scheduler is set from shell.qml (MemoryScheduler); dataChanged
    // refreshes the list. One dedicated Process + op/generation ladder,
    // following the launch/finishAgenda patterns: immutable launch
    // generation/date, stale completions dropped by generation AND
    // selected-date mismatch, 12s watchdog + 3s kill escalation,
    // list-form argv, bounded single-line errors/notices, never modal.
    property var scheduler: null
    property var captureItems: []
    // Capture-list total from the backend (S-041): items are capped
    // at 20 client-side, so the card can report "showing X of Y".
    // Updated only on valid completions.
    property int captureTotal: 0
    property bool captureBusy: false
    property bool captureStarted: false
    property bool captureStartFailed: false
    property bool captureRetiring: false
    property int captureGeneration: 0
    property int captureLaunchGeneration: -1
    property string captureOp: ""
    property string captureLaunchOp: ""
    property string captureLaunchDate: ""
    property string captureError: ""
    property string captureNotice: ""
    property var capturePreview: null
    property string capturePreviewToken: ""
    property bool captureApplying: false
    property string captureApplyError: ""

    // Review state (workstream E, UI half). Shared daily-agenda state
    // object owns the review so the calendar popout and the planner
    // Daily tab show one payload, one preview, one backend operation.
    // One dedicated Process + op/generation ladder, following the
    // capture launch/finish patterns: immutable launch
    // generation/date, stale completions dropped by generation AND
    // selected-date mismatch, 12s watchdog + 3s kill escalation,
    // list-form argv, bounded single-line errors/notices, never modal.
    // reviewKind selects the Evening/Morning tab; reviewPayload holds
    // the last validated get response ({found, review?}).
    property string reviewKind: "evening"
    property var reviewPayload: null
    property bool reviewBusy: false
    property bool reviewStarted: false
    property bool reviewStartFailed: false
    property bool reviewRetiring: false
    property int reviewGeneration: 0
    property int reviewLaunchGeneration: -1
    property string reviewOp: ""
    property string reviewLaunchOp: ""
    property string reviewLaunchDate: ""
    property string reviewError: ""
    property string reviewNotice: ""
    property var reviewPreview: null
    property string reviewPreviewToken: ""
    property bool reviewApplying: false
    property string reviewApplyError: ""

    // Session ledger state (plan section 6.1). The shared daily-agenda
    // state object owns the ledger so the planner Daily tab and the
    // calendar popout show one entry list, one preview, one backend
    // operation. One dedicated Process for list|inbox|link (stdin JSON
    // for writes, closed via closeInput) plus prepare/apply Processes for work_log.py, following
    // the capture/review launch/finish patterns: immutable launch
    // generation/date/session, stale completions dropped by generation
    // AND operation identity, 12s watchdog + 3s kill escalation,
    // list-form argv, bounded single-line errors/notices, never modal.
    // ledgerPayload holds the stdin payload for the active ledgerProcess
    // launch.
    property var ledgerEntries: []
    // Day-list total from the backend (S-041 truncation reporting):
    // entries are sanitized/capped at 50 client-side, so the card can
    // report "showing X of Y". Updated only on valid completions.
    property int ledgerTotal: 0
    property bool ledgerBusy: false
    property bool ledgerStarted: false
    property bool ledgerStartFailed: false
    property bool ledgerRetiring: false
    property int ledgerGeneration: 0
    property int ledgerLaunchGeneration: -1
    property string ledgerOp: ""
    property string ledgerLaunchOp: ""
    property string ledgerLaunchDate: ""
    property string ledgerError: ""
    property string ledgerNotice: ""
    property string ledgerStickyError: ""
    property string ledgerSelectedId: ""
    property string ledgerSelectedRevision: ""
    property bool ledgerConflict: false
    property bool ledgerRefreshPending: false
    property var ledgerPreview: null
    property string ledgerPreviewToken: ""
    property bool ledgerApplying: false
    property string ledgerApplyError: ""
    property bool ledgerPrepareBusy: false
    property bool ledgerPrepareStarted: false
    property bool ledgerPrepareStartFailed: false
    property bool ledgerPrepareRetiring: false
    property int ledgerPrepareGeneration: 0
    property int ledgerPrepareLaunchGeneration: -1
    property string ledgerPrepareLaunchSession: ""
    property string ledgerPrepareLaunchDraftId: ""
    property string ledgerPrepareLaunchTarget: ""
    property bool ledgerApplyStarted: false
    property bool ledgerApplyStartFailed: false
    property bool ledgerApplyRetiring: false
    property int ledgerApplyGeneration: 0
    property int ledgerApplyLaunchGeneration: -1
    property string ledgerApplyLaunchSession: ""
    property string ledgerApplyLaunchDraftId: ""
    property string ledgerApplyLaunchTarget: ""
    property string ledgerApplyLaunchPath: ""
    property var ledgerPayload: null
    // Session badge (advisory, read-only): today's sessions needing
    // attention, counted for the bar's CurrentProjectModule. Refreshed
    // by the CurrentProjectModule poll cadence calling
    // refreshLedgerInboxBadge(); never touches the card ladder above.
    property int ledgerInboxBadge: 0
    property bool ledgerInboxBadgeBusy: false
    property bool ledgerInboxBadgeStarted: false
    property bool ledgerInboxBadgeRetiring: false
    property int ledgerInboxBadgeGeneration: 0
    property int ledgerInboxBadgeLaunchGeneration: -1

    // Session card state (§4.5/§4.4). Thought text itself lives in
    // SessionCard (ephemeral drafts); the agenda owns only frozen
    // in-flight payloads, previews, and backend ladders. Signals tell
    // the card when a save landed (clear its draft) or an Organise
    // result is ready (replace the editor text).
    signal thoughtSaved(string sessionId)
    signal organiseDone(string sessionId, string text)
    // Which session an in-flight capture preview belongs to (the
    // capture ladder is shared with CaptureInbox).
    property string capturePreviewSession: ""
    // Thought ladder (journal_assistant context/prepare/append over
    // stdin JSON, following the agenda launch/finish patterns).
    property bool thoughtBusy: false
    property bool thoughtStarted: false
    property bool thoughtStartFailed: false
    property bool thoughtRetiring: false
    property int thoughtGeneration: 0
    property int thoughtLaunchGeneration: -1
    property string thoughtOp: ""
    property string thoughtLaunchOp: ""
    property string thoughtLaunchDate: ""
    property string thoughtLaunchSession: ""
    property var thoughtPayload: null
    property string thoughtError: ""
    property string thoughtNotice: ""
    property string thoughtJournalRevision: ""
    property string thoughtSessionId: ""
    property string thoughtText: ""
    property var thoughtPreview: null
    property string thoughtApplyRevision: ""
    property string thoughtApplyAddition: ""
    property bool thoughtApplying: false
    property string thoughtApplyError: ""
    // Organise ladder (thought_organise prepare/apply, argv-only,
    // following the capture prepare/apply patterns). Prepare runs the
    // one Pi call (60s timeout); apply only burns the single-use
    // token and returns the text — no write happens here.
    property bool organiseBusy: false
    property bool organiseStarted: false
    property bool organiseStartFailed: false
    property bool organiseRetiring: false
    property int organiseGeneration: 0
    property int organiseLaunchGeneration: -1
    property string organiseOp: ""
    property string organiseLaunchOp: ""
    property string organiseLaunchDate: ""
    property string organiseLaunchSession: ""
    property string organiseText: ""
    property var organisePreview: null
    property string organiseToken: ""
    property bool organiseApplying: false
    property string organiseApplyError: ""
    property string organiseError: ""
    // Dismiss-session ladder (sessions.py annotate over stdin JSON).
    property bool dismissBusy: false
    property bool dismissStarted: false
    property bool dismissStartFailed: false
    property bool dismissRetiring: false
    property int dismissGeneration: 0
    property int dismissLaunchGeneration: -1
    property string dismissLaunchDate: ""
    property string dismissSessionId: ""
    property var dismissPayload: null
    property string dismissError: ""
    // Attribution hygiene (§4.4): recent folders no project claims.
    // Ignore is ephemeral card-local state (never durable content).
    property var unmappedRows: []
    property bool unmappedBusy: false
    property bool unmappedStarted: false
    property bool unmappedStartFailed: false
    property bool unmappedRetiring: false
    property int unmappedGeneration: 0
    property int unmappedLaunchGeneration: -1
    property string unmappedLaunchDate: ""
    property string unmappedError: ""
    property string unmappedNotice: ""
    property var unmappedIgnored: []

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
        root.reloadCaptures()
        root.reloadReview()
        root.ledgerSelectedId = ""
        root.ledgerSelectedRevision = ""
        root.ledgerConflict = false
        // A day change invalidates staged session previews (thought,
        // organise, capture): cancel them so a Confirm can never file
        // to the wrong session. Ledger filing keeps its own ladder.
        root.cancelThoughtPreview()
        root.cancelOrganisePreview()
        root.cancelCapturePreview()
        root.reloadLedger()
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

    // ---- Capture inbox (workstream D, UI half) ----

    // Bounded single-line text for capture errors/notices (never modal).
    function captureBound(text, limit) {
        let max = Number(limit) > 0 ? Math.floor(Number(limit)) : 200
        let clean = String(text === undefined || text === null ? "" : text)
            .replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim()
        if (clean.length > max) clean = clean.substring(0, max - 1) + "…"
        return clean
    }

    function captureFailure(label, code, detail) {
        return root.captureBound(String(label || "Capture") + " failed"
            + (code ? " (exit " + code + ")" : "")
            + (detail ? ": " + String(detail) : ""), 200)
    }

    // List argv builder (pure over the date): list-form, no shell.
    function captureListCommand(date) {
        let day = String(date || "")
        if (!root.isValidIso(day)) return []
        return ["python3", Quickshell.shellPath("scripts/session_capture.py"),
            "list", "--day", day, "--status", "new", "--limit", "20"]
    }

    // Prepare argv builder (pure over the id + optional session
    // provenance): list-form, no shell. L1 single save target — the
    // project page only, so there is no target argument. Session/ref
    // ride along only when the caller supplies them; otherwise the
    // backend files a bare - TODO block (degraded, never blocked).
    function capturePrepareCommand(id, sessionId, refIdentity) {
        let token = String(id === undefined || id === null ? "" : id).trim()
        if (!token) return []
        let argv = ["python3", Quickshell.shellPath("scripts/session_capture.py"),
            "prepare", "--capture", token]
        let sid = String(sessionId === undefined || sessionId === null ? "" : sessionId).trim().toLowerCase()
        if (sid !== "") {
            if (!/^[0-9a-f]{32}$/.test(sid)) return []
            argv.push("--session", sid)
        }
        let refText = String(refIdentity === undefined || refIdentity === null ? "" : refIdentity)
        if (refText.trim() !== "") {
            if (refText.length > 4096) return []
            argv.push("--ref", refText)
        }
        return argv
    }

    function captureApplyCommand(token) {
        let value = String(token || "").trim()
        if (!value) return []
        return ["python3", Quickshell.shellPath("scripts/session_capture.py"),
            "apply", "--prepared", value]
    }

    function captureDismissCommand(id) {
        let token = String(id === undefined || id === null ? "" : id).trim()
        if (!token) return []
        return ["python3", Quickshell.shellPath("scripts/session_capture.py"),
            "set-status", "--id", token, "--status", "dismissed"]
    }

    // Serialized launch for the dedicated capture Process. Never overlap
    // a running capture, a retiring one, or the runningChanged window.
    function launchCapture(op, date, argv) {
        if (root.captureBusy || captureProcess.running || root.captureRetiring) return false
        if (!Array.isArray(argv) || argv.length === 0) return false
        if (op !== "list" && op !== "prepare" && op !== "apply" && op !== "dismiss") return false
        root.captureGeneration = Number(root.captureGeneration) + 1
        root.captureOp = op
        root.captureLaunchGeneration = Number(root.captureGeneration)
        root.captureLaunchOp = op
        root.captureLaunchDate = String(date || "")
        root.captureBusy = true
        root.captureStarted = false
        root.captureStartFailed = false
        if (op === "apply") root.captureApplying = true
        captureProcess.command = argv
        captureWatchdog.restart()
        captureProcess.running = true
        return true
    }

    // List new captures for the selected day. No-op while busy.
    function reloadCaptures() {
        if (root.captureBusy || captureProcess.running || root.captureRetiring) return false
        if (root.captureApplying) return false
        let argv = root.captureListCommand(root.selectedDate)
        if (argv.length === 0) return false
        return root.launchCapture("list", root.selectedDate, argv)
    }

    // Guarded list completion: generation AND launch/selected-date must
    // match (stale completions dropped). Validates {captures:[...]},
    // bounds items to 20, then applies.
    function finishCaptures(code, output, generation, date, op) {
        if (Number(generation) !== Number(root.captureLaunchGeneration)) return false
        if (String(date) !== String(root.captureLaunchDate)) return false
        if (String(date) !== String(root.selectedDate)) return false
        if (op !== "list" || root.captureLaunchOp !== "list") return false
        captureWatchdog.stop()
        root.captureBusy = false
        let data = null
        try { data = JSON.parse(output || "{}") } catch (error) { data = null }
        let valid = code === 0 && data && Array.isArray(data.captures)
        if (!valid) {
            let backendError = data && data.error ? String(data.error) : ""
            root.captureError = backendError
                ? root.captureBound(backendError, 200)
                : root.captureFailure("Capture", code, "")
            return false
        }
        let rows = data.captures.slice(0, 20).filter(entry => entry && typeof entry === "object")
        // Keep only entries with an id and text; bound text later in the card.
        let items = []
        for (let i = 0; i < rows.length; i++) {
            let entry = rows[i]
            if (entry.id === undefined || entry.id === null) continue
            if (typeof entry.text !== "string" || !entry.text.trim()) continue
            items.push(entry)
            if (items.length >= 20) break
        }
        root.captureItems = items
        // Backend day total for the "showing X of Y" truncation line
        // (S-041). Defensive: missing/non-numeric totals degrade to the
        // shown count so the line stays silent instead of wrong.
        let captureDayTotal = Number(data.total)
        if (typeof captureDayTotal === "number" && isFinite(captureDayTotal) && captureDayTotal >= 0)
            root.captureTotal = Math.floor(captureDayTotal)
        else root.captureTotal = items.length
        root.captureError = ""
        return true
    }

    // Prepare an exact preview for Accept (project page, single target).
    // Optional sessionId (32-hex) + refIdentity flow into --session/--ref;
    // omit both when the caller has no session context — the backend
    // then files a bare block (degraded, never blocked).
    function prepareCapture(id, sessionId, refIdentity) {
        if (root.captureBusy || captureProcess.running || root.captureRetiring) return false
        if (root.captureApplying) return false
        if (id === undefined || id === null || String(id).trim() === "") return false
        let argv = root.capturePrepareCommand(id, sessionId, refIdentity)
        if (argv.length === 0) return false
        root.captureApplyError = ""
        let sid = String(sessionId === undefined || sessionId === null ? "" : sessionId).trim().toLowerCase()
        root.capturePreviewSession = root.isLedgerSessionId(sid) ? sid : ""
        return root.launchCapture("prepare", root.selectedDate, argv)
    }

    // Guarded prepare completion: validates {prepared, preview}, then
    // stores the token + preview for the Confirm panel.
    function finishPrepare(code, output, generation, date, op) {
        if (Number(generation) !== Number(root.captureLaunchGeneration)) return false
        if (String(date) !== String(root.captureLaunchDate)) return false
        if (String(date) !== String(root.selectedDate)) return false
        if (op !== "prepare" || root.captureLaunchOp !== "prepare") return false
        captureWatchdog.stop()
        root.captureBusy = false
        let data = null
        try { data = JSON.parse(output || "{}") } catch (error) { data = null }
        let preview = data && data.preview ? data.preview : null
        let valid = code === 0 && data
            && typeof data.prepared === "string" && data.prepared !== ""
            && preview && typeof preview === "object" && !Array.isArray(preview)
            && typeof preview.block === "string" && preview.block !== ""
            && preview.target === "page"
        if (!valid) {
            let backendError = data && data.error ? String(data.error) : ""
            root.captureApplyError = backendError
                ? root.captureBound(backendError, 200)
                : root.captureFailure("Capture preview", code, "")
            return false
        }
        root.capturePreview = preview
        root.capturePreviewToken = String(data.prepared)
        root.captureApplyError = ""
        return true
    }

    // Confirm the stored preview token. On success the preview clears
    // and the list reloads; on failure a bounded error stays.
    function applyCapture() {
        if (root.captureBusy || captureProcess.running || root.captureRetiring) return false
        if (root.captureApplying) return false
        if (!root.capturePreview || !root.capturePreviewToken) return false
        let argv = root.captureApplyCommand(root.capturePreviewToken)
        if (argv.length === 0) return false
        root.captureApplyError = ""
        return root.launchCapture("apply", root.selectedDate, argv)
    }

    function finishApply(code, output, generation, date, op) {
        if (Number(generation) !== Number(root.captureLaunchGeneration)) return false
        if (String(date) !== String(root.captureLaunchDate)) return false
        if (String(date) !== String(root.selectedDate)) return false
        if (op !== "apply" || root.captureLaunchOp !== "apply") return false
        captureWatchdog.stop()
        root.captureBusy = false
        root.captureApplying = false
        let data = null
        try { data = JSON.parse(output || "{}") } catch (error) { data = null }
        let valid = code === 0 && data && data.applied === true
        if (!valid) {
            let backendError = data && data.error ? String(data.error) : ""
            root.captureApplyError = backendError
                ? root.captureBound(backendError, 200)
                : root.captureFailure("Capture save", code, "")
            return false
        }
        root.capturePreview = null
        root.capturePreviewToken = ""
        root.capturePreviewSession = ""
        root.captureApplyError = ""
        root.captureNotice = root.captureBound("Saved to project page.", 200)
        root.reloadCaptures()
        return true
    }

    function cancelCapturePreview() {
        if (root.captureApplying) return false
        root.capturePreview = null
        root.capturePreviewToken = ""
        root.capturePreviewSession = ""
        root.captureApplyError = ""
        return true
    }

    // Dismiss a capture (set-status dismissed), then reload the list.
    function dismissCapture(id) {
        if (root.captureBusy || captureProcess.running || root.captureRetiring) return false
        if (root.captureApplying) return false
        if (id === undefined || id === null || String(id).trim() === "") return false
        let argv = root.captureDismissCommand(id)
        if (argv.length === 0) return false
        return root.launchCapture("dismiss", root.selectedDate, argv)
    }

    function finishDismiss(code, output, generation, date, op) {
        if (Number(generation) !== Number(root.captureLaunchGeneration)) return false
        if (String(date) !== String(root.captureLaunchDate)) return false
        if (String(date) !== String(root.selectedDate)) return false
        if (op !== "dismiss" || root.captureLaunchOp !== "dismiss") return false
        captureWatchdog.stop()
        root.captureBusy = false
        let data = null
        try { data = JSON.parse(output || "{}") } catch (error) { data = null }
        let valid = code === 0 && data && (data.updated === true || data.dismissed === true || data.status === "dismissed")
        // The frozen CLI returns {updated:true,id,status}; accept it.
        if (code === 0 && data && data.updated === true) valid = true
        if (!valid) {
            let backendError = data && data.error ? String(data.error) : ""
            root.captureError = backendError
                ? root.captureBound(backendError, 200)
                : root.captureFailure("Capture dismiss", code, "")
            return false
        }
        root.captureError = ""
        root.reloadCaptures()
        return true
    }

    // Manual scan trigger via the resident scheduler. Returns the
    // scheduler result; when false a bounded notice explains why.
    function scanNow() {
        if (!root.scheduler || !root.scheduler.requestScan) {
            root.captureNotice = "Scan unavailable"
            return false
        }
        let launched = false
        try { launched = root.scheduler.requestScan() } catch (error) { launched = false }
        if (!launched) {
            root.captureNotice = "Scan running"
            return false
        }
        root.captureNotice = ""
        return true
    }

    // ---- Evening review / morning plan (workstream E, UI half) ----

    // Bounded single-line text for review errors/notices (never modal).
    function reviewBound(text, limit) {
        let max = Number(limit) > 0 ? Math.floor(Number(limit)) : 200
        let clean = String(text === undefined || text === null ? "" : text)
            .replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim()
        if (clean.length > max) clean = clean.substring(0, max - 1) + "…"
        return clean
    }

    function reviewFailure(label, code, detail) {
        return root.reviewBound(String(label || "Review") + " failed"
            + (code ? " (exit " + code + ")" : "")
            + (detail ? ": " + String(detail) : ""), 200)
    }

    function isReviewKind(kind) {
        return kind === "evening" || kind === "morning"
    }

    // Argv builders (pure over kind/date): list-form, no shell. The
    // frozen backend speaks argv-only flags with JSON on stdout.
    function reviewGetCommand(kind, date, refresh) {
        if (!root.isReviewKind(kind)) return []
        let day = String(date || "")
        if (!root.isValidIso(day)) return []
        let argv = ["python3", Quickshell.shellPath("scripts/daily_review.py"),
            "get", "--kind", String(kind), "--date", day]
        if (refresh) argv.push("--refresh")
        return argv
    }

    function reviewPrepareCommand(kind, date) {
        if (!root.isReviewKind(kind)) return []
        let day = String(date || "")
        if (!root.isValidIso(day)) return []
        return ["python3", Quickshell.shellPath("scripts/daily_review.py"),
            "prepare-save", "--kind", String(kind), "--date", day]
    }

    function reviewApplyCommand(token) {
        let value = String(token || "").trim()
        if (!value) return []
        return ["python3", Quickshell.shellPath("scripts/daily_review.py"),
            "apply", "--prepared", value]
    }

    // Serialized launch for the dedicated review Process. Never overlap
    // a running review, a retiring one, or the runningChanged window.
    function launchReview(op, date, argv) {
        if (root.reviewBusy || reviewProcess.running || root.reviewRetiring) return false
        if (!Array.isArray(argv) || argv.length === 0) return false
        if (op !== "get" && op !== "refresh" && op !== "prepare" && op !== "apply") return false
        root.reviewGeneration = Number(root.reviewGeneration) + 1
        root.reviewOp = op
        root.reviewLaunchGeneration = Number(root.reviewGeneration)
        root.reviewLaunchOp = op
        root.reviewLaunchDate = String(date || "")
        root.reviewBusy = true
        root.reviewStarted = false
        root.reviewStartFailed = false
        if (op === "apply") root.reviewApplying = true
        reviewProcess.command = argv
        reviewWatchdog.restart()
        reviewProcess.running = true
        return true
    }

    // Load the review for the selected day + active tab. No-op while busy.
    function reloadReview() {
        if (root.reviewBusy || reviewProcess.running || root.reviewRetiring) return false
        if (root.reviewApplying) return false
        let argv = root.reviewGetCommand(root.reviewKind, root.selectedDate, false)
        if (argv.length === 0) return false
        return root.launchReview("get", root.selectedDate, argv)
    }

    // Regenerate the review (same shape, fresh generation).
    function refreshReview() {
        if (root.reviewBusy || reviewProcess.running || root.reviewRetiring) return false
        if (root.reviewApplying) return false
        let argv = root.reviewGetCommand(root.reviewKind, root.selectedDate, true)
        if (argv.length === 0) return false
        return root.launchReview("refresh", root.selectedDate, argv)
    }

    // Switch the Evening/Morning tab, then reload for the new kind.
    function setReviewKind(kind) {
        if (!root.isReviewKind(kind)) {
            root.reviewError = "Pick evening or morning."
            return false
        }
        root.reviewKind = String(kind)
        root.reviewError = ""
        return root.reloadReview()
    }

    // Bound one markdown body for display (never raw unbounded text).
    function reviewBoundMarkdown(text) {
        let clean = String(text === undefined || text === null ? "" : text)
        if (clean.length > 8000) clean = clean.substring(0, 7999) + "…"
        return clean
    }

    // Guarded get/refresh completion: generation AND launch/selected-date
    // must match (stale completions dropped). Validates {found, review}
    // and stores a bounded reviewPayload (markdown ≤8000 chars, top ≤3).
    function finishReview(code, output, generation, date, op) {
        if (Number(generation) !== Number(root.reviewLaunchGeneration)) return false
        if (String(date) !== String(root.reviewLaunchDate)) return false
        if (String(date) !== String(root.selectedDate)) return false
        if ((op !== "get" && op !== "refresh") || root.reviewLaunchOp !== op) return false
        reviewWatchdog.stop()
        root.reviewBusy = false
        let data = null
        try { data = JSON.parse(output || "{}") } catch (error) { data = null }
        let isFound = data && data.found === true
        let isMissing = data && data.found === false
        if (!(code === 0 && data && (isFound || isMissing))) {
            let backendError = data && data.error ? String(data.error) : ""
            root.reviewError = backendError
                ? root.reviewBound(backendError, 200)
                : root.reviewFailure("Review", code, "")
            return false
        }
        if (isMissing) {
            root.reviewPayload = { found: false,
                reason: root.reviewBound(data.reason || "", 200),
                kind: String(data.kind || root.reviewKind),
                day: String(data.day || root.selectedDate) }
            root.reviewError = ""
            return true
        }
        let review = data.review
        let valid = review && typeof review === "object" && !Array.isArray(review)
            && review.sections && typeof review.sections === "object" && !Array.isArray(review.sections)
            && typeof review.markdown === "string" && review.markdown !== ""
            && Array.isArray(review.top)
        if (!valid) {
            root.reviewError = root.reviewFailure("Review", code, "invalid review output")
            return false
        }
        let top = review.top.filter(entry => entry && typeof entry === "object").slice(0, 3)
        // Pre-slice top count for the "showing X of Y" truncation line
        // (S-041); the payload keeps only the bounded three rows.
        let topTotal = review.top.filter(entry => entry && typeof entry === "object").length
        root.reviewPayload = { found: true,
            kind: String(data.kind || root.reviewKind),
            day: String(data.day || root.selectedDate),
            review: { generated_ms: review.generated_ms,
                polished: !!review.polished,
                saved_ms: review.saved_ms === undefined || review.saved_ms === null ? null : review.saved_ms,
                prioritized: !!review.prioritized,
                polish_enabled: review.polish_enabled === true,
                sections: review.sections,
                top: top,
                top_total: topTotal,
                markdown: root.reviewBoundMarkdown(review.markdown) } }
        root.reviewError = ""
        return true
    }

    // Prepare an exact journal preview for Save to journal.
    function prepareReviewSave() {
        if (root.reviewBusy || reviewProcess.running || root.reviewRetiring) return false
        if (root.reviewApplying) return false
        if (!root.reviewPayload || !root.reviewPayload.found || !root.reviewPayload.review) return false
        let argv = root.reviewPrepareCommand(root.reviewKind, root.selectedDate)
        if (argv.length === 0) return false
        root.reviewApplyError = ""
        return root.launchReview("prepare", root.selectedDate, argv)
    }

    // Guarded prepare completion: validates {prepared, preview}, then
    // stores the token + preview for the Confirm panel.
    function finishReviewPrepare(code, output, generation, date, op) {
        if (Number(generation) !== Number(root.reviewLaunchGeneration)) return false
        if (String(date) !== String(root.reviewLaunchDate)) return false
        if (String(date) !== String(root.selectedDate)) return false
        if (op !== "prepare" || root.reviewLaunchOp !== "prepare") return false
        reviewWatchdog.stop()
        root.reviewBusy = false
        let data = null
        try { data = JSON.parse(output || "{}") } catch (error) { data = null }
        let preview = data && data.preview ? data.preview : null
        let valid = code === 0 && data
            && typeof data.prepared === "string" && data.prepared !== ""
            && preview && typeof preview === "object" && !Array.isArray(preview)
            && preview.target === "journal"
            && typeof preview.addition === "string" && preview.addition !== ""
            && typeof preview.path === "string" && typeof preview.revision === "string"
        if (!valid) {
            let backendError = data && data.error ? String(data.error) : ""
            root.reviewApplyError = backendError
                ? root.reviewBound(backendError, 200)
                : root.reviewFailure("Review preview", code, "")
            return false
        }
        root.reviewPreview = preview
        root.reviewPreviewToken = String(data.prepared)
        root.reviewApplyError = ""
        return true
    }

    // Confirm the stored preview token. On success the preview clears
    // and the payload reloads; on failure a bounded error stays and the
    // preview is kept for retry.
    function applyReviewSave() {
        if (root.reviewBusy || reviewProcess.running || root.reviewRetiring) return false
        if (root.reviewApplying) return false
        if (!root.reviewPreview || !root.reviewPreviewToken) return false
        let argv = root.reviewApplyCommand(root.reviewPreviewToken)
        if (argv.length === 0) return false
        root.reviewApplyError = ""
        return root.launchReview("apply", root.selectedDate, argv)
    }

    function finishReviewApply(code, output, generation, date, op) {
        if (Number(generation) !== Number(root.reviewLaunchGeneration)) return false
        if (String(date) !== String(root.reviewLaunchDate)) return false
        if (String(date) !== String(root.selectedDate)) return false
        if (op !== "apply" || root.reviewLaunchOp !== "apply") return false
        reviewWatchdog.stop()
        root.reviewBusy = false
        root.reviewApplying = false
        let data = null
        try { data = JSON.parse(output || "{}") } catch (error) { data = null }
        let valid = code === 0 && data && data.applied === true
        if (!valid) {
            let backendError = data && data.error ? String(data.error) : ""
            root.reviewApplyError = backendError
                ? root.reviewBound(backendError, 200)
                : root.reviewFailure("Review save", code, "")
            return false
        }
        root.reviewPreview = null
        root.reviewPreviewToken = ""
        root.reviewApplyError = ""
        root.reviewNotice = root.reviewBound("Saved to journal.", 200)
        root.reloadReview()
        return true
    }

    function cancelReviewPreview() {
        if (root.reviewApplying) return false
        root.reviewPreview = null
        root.reviewPreviewToken = ""
        root.reviewApplyError = ""
        return true
    }

    // Schedule one review top item through the EXISTING shared agenda
    // select op (no second process). The select payload reuses the
    // daily_agenda.py contract: {path, revision, line, date, selected}.
    // The item's target_date is the destination day ("Add to tomorrow"
    // for evening reviews, "Add to today" for morning plans). A stale
    // page revision fails in finishAgenda with the bounded backend text
    // ("page revision is stale; reload the page"), which suggests
    // Refresh; nothing here invents a second write path.
    function scheduleReviewItem(item) {
        if (!item || typeof item !== "object" || Array.isArray(item)) {
            root.agendaError = "Review item cannot be scheduled."
            return false
        }
        if (root.agendaBusy || agendaProcess.running || root.agendaRetiring) {
            root.agendaError = "The agenda request is still running; retry shortly."
            return false
        }
        if (root.completionSaving) {
            root.agendaError = "Wait for the completion write to finish."
            return false
        }
        let path = String(item.path || "")
        let day = String(item.target_date || "")
        if (!path || !root.isValidIso(day)) {
            root.agendaError = "Review item cannot be scheduled (missing target)."
            return false
        }
        root.agendaError = ""
        root.agendaNotice = "Scheduling…"
        return root.launch("select", { path: path,
            revision: String(item.revision || ""), line: Number(item.line || 0),
            date: day, selected: true }, "select")
    }

    function handleReviewStartFailure(generation) {
        if (Number(generation) !== Number(root.reviewGeneration)) return
        reviewWatchdog.stop()
        reviewKillTimer.stop()
        if (!root.reviewStarted && !reviewProcess.running) root.reviewRetiring = false
        let wasApply = root.reviewLaunchOp === "apply" || root.reviewLaunchOp === "prepare"
        root.reviewBusy = false
        root.reviewApplying = false
        let message = root.reviewFailure("Review", 0, "process could not start")
        if (wasApply) root.reviewApplyError = message
        else root.reviewError = message
    }

    function handleReviewRunningChanged() {
        if (!reviewProcess.running && (root.reviewBusy || root.reviewStarted))
            root.reviewRetiring = true
        if (!reviewProcess.running && root.reviewBusy && !root.reviewStarted && !root.reviewStartFailed) {
            root.reviewStartFailed = true
            root.handleReviewStartFailure(root.reviewLaunchGeneration)
        }
    }

    // Actual review exit handler (thin onExited delegates here so tests
    // exercise the real logic). Uses the frozen launch generation/date,
    // never re-read current state. Either path consumes the launch.
    function handleReviewExited(code, output) {
        reviewWatchdog.stop()
        reviewKillTimer.stop()
        if (!root.reviewBusy && !root.reviewRetiring) {
            // No launch in flight (already reconciled): drop late exits.
            if (!root.reviewStarted && root.reviewLaunchGeneration < 0) return false
        }
        if (root.reviewRetiring) {
            root.reviewBusy = false
            root.reviewApplying = false
            root.reviewLaunchGeneration = -1
            root.reviewRetiring = false
            root.reviewStarted = false
            root.reviewStartFailed = false
            return false
        }
        let generation = root.reviewLaunchGeneration
        let date = root.reviewLaunchDate
        let op = root.reviewLaunchOp
        let applied = false
        if (op === "get" || op === "refresh") applied = root.finishReview(code, output, generation, date, op)
        else if (op === "prepare") applied = root.finishReviewPrepare(code, output, generation, date, op)
        else if (op === "apply") applied = root.finishReviewApply(code, output, generation, date, op)
        root.reviewLaunchGeneration = -1
        root.reviewStarted = false
        root.reviewStartFailed = false
        return applied
    }

    function handleReviewTimeout() {
        if (!root.reviewBusy) {
            reviewWatchdog.stop()
            return false
        }
        reviewWatchdog.stop()
        root.reviewRetiring = true
        root.closeInput(reviewProcess)
        reviewProcess.running = false
        reviewKillTimer.restart()
        return true
    }

    function fireReviewKillTimeout() {
        if (root.reviewRetiring && reviewProcess.running) {
            try {
                reviewProcess.signal(9)
            } catch (error) {}
            return true
        }
        return false
    }

    function handleCaptureStartFailure(generation) {
        if (Number(generation) !== Number(root.captureGeneration)) return
        captureWatchdog.stop()
        captureKillTimer.stop()
        if (!root.captureStarted && !captureProcess.running) root.captureRetiring = false
        let wasApply = root.captureLaunchOp === "apply" || root.captureLaunchOp === "prepare"
        root.captureBusy = false
        root.captureApplying = false
        let message = root.captureFailure("Capture", 0, "process could not start")
        if (wasApply) root.captureApplyError = message
        else root.captureError = message
    }

    function handleCaptureRunningChanged() {
        if (!captureProcess.running && (root.captureBusy || root.captureStarted))
            root.captureRetiring = true
        if (!captureProcess.running && root.captureBusy && !root.captureStarted && !root.captureStartFailed) {
            root.captureStartFailed = true
            root.handleCaptureStartFailure(root.captureLaunchGeneration)
        }
    }

    // Actual capture exit handler (thin onExited delegates here so tests
    // exercise the real logic). Uses the frozen launch generation/date,
    // never re-read current state. Either path consumes the launch.
    function handleCaptureExited(code, output) {
        captureWatchdog.stop()
        captureKillTimer.stop()
        if (!root.captureBusy && !root.captureRetiring) {
            // No launch in flight (already reconciled): drop late exits.
            if (!root.captureStarted && root.captureLaunchGeneration < 0) return false
        }
        if (root.captureRetiring) {
            root.captureBusy = false
            root.captureApplying = false
            root.captureLaunchGeneration = -1
            root.captureRetiring = false
            root.captureStarted = false
            root.captureStartFailed = false
            return false
        }
        let generation = root.captureLaunchGeneration
        let date = root.captureLaunchDate
        let op = root.captureLaunchOp
        let applied = false
        if (op === "list") applied = root.finishCaptures(code, output, generation, date, op)
        else if (op === "prepare") applied = root.finishPrepare(code, output, generation, date, op)
        else if (op === "apply") applied = root.finishApply(code, output, generation, date, op)
        else if (op === "dismiss") applied = root.finishDismiss(code, output, generation, date, op)
        root.captureLaunchGeneration = -1
        root.captureStarted = false
        root.captureStartFailed = false
        return applied
    }

    function handleCaptureTimeout() {
        if (!root.captureBusy) {
            captureWatchdog.stop()
            return false
        }
        captureWatchdog.stop()
        root.captureRetiring = true
        root.closeInput(captureProcess)
        captureProcess.running = false
        captureKillTimer.restart()
        return true
    }

    function fireCaptureKillTimeout() {
        if (root.captureRetiring && captureProcess.running) {
            try {
                captureProcess.signal(9)
            } catch (error) {}
            return true
        }
        return false
    }

    // ---- Session ledger (plan section 6.1; P1a ladder + P1c filing) ----

    // Bounded single-line text for ledger errors/notices (never modal).
    function ledgerBound(text, limit) {
        let max = Number(limit) > 0 ? Math.floor(Number(limit)) : 200
        let clean = String(text === undefined || text === null ? "" : text)
            .replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim()
        if (clean.length > max) clean = clean.substring(0, max - 1) + "…"
        return clean
    }

    // Bounded length cap for exact previews: exact-preview callers must
    // pre-check the bound and fail closed; this helper only caps length
    // and never transforms content.
    function ledgerBoundText(text, limit) {
        let max = Number(limit) > 0 ? Math.floor(Number(limit)) : 8000
        let clean = String(text === undefined || text === null ? "" : text)
        if (clean.length > max) clean = clean.substring(0, max - 1) + "…"
        return clean
    }

    function ledgerFailure(label, code, detail) {
        return root.ledgerBound(String(label || "Ledger") + " failed"
            + (code ? " (exit " + code + ")" : "")
            + (detail ? ": " + String(detail) : "") + ".", 200)
    }

    // Backend error text: data.error wins, else the stderr diagnostic with
    // a leading "error:" stripped. Bounded later by the callers.
    function ledgerBackendError(data, diagnostic) {
        if (data && typeof data === "object" && !Array.isArray(data)
                && data.error !== undefined && data.error !== null
                && String(data.error) !== "")
            return String(data.error)
        let text = String(diagnostic === undefined || diagnostic === null ? "" : diagnostic)
        return text.replace(/^\s*error:\s*/i, "").trim()
    }

    function isLedgerSessionId(value) {
        if (typeof value !== "string") return false
        return /^[0-9a-fA-F]{32}$/.test(value.trim())
    }

    // Split a tags draft on commas: trim, drop empties, case-insensitive
    // dedupe.
    function ledgerTagsFromText(text) {
        let parts = String(text === undefined || text === null ? "" : text).split(",")
        let seen = {}
        let out = []
        for (let i = 0; i < parts.length; i++) {
            let tag = parts[i].trim()
            if (!tag) continue
            let key = tag.toLowerCase()
            if (seen[key]) continue
            seen[key] = true
            out.push(tag)
        }
        return out
    }

    // Local day window {from_ms, to_ms} for an ISO date: local midnight
    // through the next local midnight. Null when invalid.
    function ledgerDayWindow(date) {
        let parsed = root.parseIsoDate(date)
        if (!parsed) return null
        let from = new Date(parsed.year, parsed.month - 1, parsed.day).getTime()
        let to = new Date(parsed.year, parsed.month - 1, parsed.day + 1).getTime()
        if (!(from >= 0) || !(to > from)) return null
        return { from_ms: from, to_ms: to }
    }

    function ledgerMsValid(value) {
        if (typeof value === "boolean") return false
        if (value === null || value === undefined || value === "") return false
        let n = Number(value)
        return typeof n === "number" && isFinite(n) && Math.floor(n) === n && n >= 0
    }

    function ledgerLimitValid(limit) {
        if (limit === undefined || limit === null) return 50
        if (typeof limit === "boolean" || limit === "") return -1
        let n = Number(limit)
        if (typeof n !== "number" || !isFinite(n) || Math.floor(n) !== n) return -1
        if (n < 1 || n > 1000) return -1
        return n
    }

    // Argv builders (pure over ms): list-form, no shell. The frozen
    // backend speaks argv-only flags with JSON on stdout.
    function ledgerListCommand(fromMs, toMs, limit) {
        if (!root.ledgerMsValid(fromMs) || !root.ledgerMsValid(toMs)) return []
        let from = Number(fromMs), to = Number(toMs)
        if (!(to > from)) return []
        let n = root.ledgerLimitValid(limit)
        if (n < 0) return []
        return ["python3", Quickshell.shellPath("scripts/sessions.py"),
            "list", "--from", String(from), "--to", String(to),
            "--limit", String(n)]
    }

    function ledgerInboxCommand(fromMs, toMs, limit) {
        if (!root.ledgerMsValid(fromMs) || !root.ledgerMsValid(toMs)) return []
        let from = Number(fromMs), to = Number(toMs)
        if (!(to > from)) return []
        let n = root.ledgerLimitValid(limit)
        if (n < 0) return []
        return ["python3", Quickshell.shellPath("scripts/sessions.py"),
            "inbox", "--from", String(from), "--to", String(to),
            "--limit", String(n)]
    }

    function ledgerFindEntry(sessionId) {
        if (!root.isLedgerSessionId(sessionId)) return null
        let want = String(sessionId).trim().toLowerCase()
        let rows = Array.isArray(root.ledgerEntries) ? root.ledgerEntries : []
        for (let i = 0; i < rows.length; i++) {
            let entry = rows[i]
            if (!entry || typeof entry !== "object" || Array.isArray(entry)) continue
            let session = entry.session
            if (!session || typeof session !== "object" || Array.isArray(session)) continue
            if (String(session.session_id || "").toLowerCase() === want) return entry
        }
        return null
    }

    // Keep only entries with a valid session id; cap at 50 rows.
    function ledgerSanitizeEntries(rows) {
        let out = []
        let list = Array.isArray(rows) ? rows : []
        for (let i = 0; i < list.length && out.length < 50; i++) {
            let entry = list[i]
            if (!entry || typeof entry !== "object" || Array.isArray(entry)) continue
            let session = entry.session
            if (!session || typeof session !== "object" || Array.isArray(session)) continue
            if (!root.isLedgerSessionId(session.session_id)) continue
            out.push(entry)
        }
        return out
    }

    function ledgerResumeProject(sessionId) {
        let entry = root.ledgerFindEntry(sessionId)
        if (!entry || typeof entry !== "object" || Array.isArray(entry)) return ""
        let session = entry.session
        if (!session || typeof session !== "object" || Array.isArray(session)) return ""
        let project = session.project
        if (!project || typeof project !== "object" || Array.isArray(project)) return ""
        let id = String(project.id || "")
        if (typeof id !== "string"
                || !/^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(id.trim())) return ""
        return id.trim().toLowerCase()
    }

    function ledgerResumeMessage(sessionId) {
        let entry = root.ledgerFindEntry(sessionId)
        if (!entry || typeof entry !== "object" || Array.isArray(entry)) return ""
        let meta = entry.meta
        if (!meta || typeof meta !== "object" || Array.isArray(meta)) return ""
        if (typeof meta.next_step !== "string") return ""
        if (meta.next_step.trim() === "") return ""
        return root.ledgerBound("Next: " + meta.next_step, 300)
    }

    // Select a row (populating the drafts) or deselect by tapping again.
    function selectLedgerEntry(sessionId) {
        if (!root.isLedgerSessionId(sessionId)) {
            root.ledgerError = root.ledgerBound("Pick a valid session.", 200)
            return false
        }
        let want = String(sessionId).trim().toLowerCase()
        if (want !== "" && String(root.ledgerSelectedId || "").toLowerCase() === want) {
            root.ledgerSelectedId = ""
            root.ledgerSelectedRevision = ""
            root.ledgerConflict = false
            return true
        }
        let entry = root.ledgerFindEntry(sessionId)
        if (!entry) return false
        root.ledgerSelectedId = String(entry.session.session_id)
        root.ledgerConflict = false
        root.setLedgerDrafts(entry)
        return true
    }

    // Carry the selected entry's revision for the annotate round-trip.
    // (Deleted Phase-2a metadata drafts are never populated: the
    // surviving sidecar holds no title/outcome/next-step/tags fields.)
    function setLedgerDrafts(entry) {
        if (!entry || typeof entry !== "object" || Array.isArray(entry)) return false
        let meta = entry.meta
        if (!meta || typeof meta !== "object" || Array.isArray(meta)) meta = {}
        root.ledgerSelectedRevision = typeof meta.revision === "string" ? meta.revision : ""
        return true
    }

    // Serialized launch for the dedicated ledger Process. Never overlap
    // a running ledger, a retiring one, or the runningChanged window.
    // The stdin payload is frozen in ledgerPayload and written onStarted.
    function launchLedger(op, argv, payload, date) {
        if (root.ledgerBusy || ledgerProcess.running || root.ledgerRetiring) return false
        if (!Array.isArray(argv) || argv.length === 0) return false
        if (op !== "list" && op !== "inbox" && op !== "link") return false
        root.ledgerGeneration = Number(root.ledgerGeneration) + 1
        root.ledgerOp = op
        root.ledgerLaunchGeneration = Number(root.ledgerGeneration)
        root.ledgerLaunchOp = op
        root.ledgerLaunchDate = String(date || "")
        if (payload && typeof payload === "object" && !Array.isArray(payload))
            root.ledgerPayload = payload
        else root.ledgerPayload = {}
        root.ledgerBusy = true
        root.ledgerStarted = false
        root.ledgerStartFailed = false
        ledgerProcess.command = argv
        ledgerWatchdog.restart()
        ledgerProcess.stdinEnabled = true
        ledgerProcess.running = true
        return true
    }

    function launchLedgerPrepare(argv, sessionId, draftId, target) {
        if (root.ledgerPrepareBusy || ledgerPrepareProcess.running || root.ledgerPrepareRetiring) return false
        if (!Array.isArray(argv) || argv.length === 0) return false
        root.ledgerPrepareGeneration = Number(root.ledgerPrepareGeneration) + 1
        root.ledgerPrepareLaunchGeneration = Number(root.ledgerPrepareGeneration)
        root.ledgerPrepareLaunchSession = String(sessionId || "")
        root.ledgerPrepareLaunchDraftId = String(draftId || "")
        root.ledgerPrepareLaunchTarget = String(target || "")
        root.ledgerPrepareBusy = true
        root.ledgerPrepareStarted = false
        root.ledgerPrepareStartFailed = false
        ledgerPrepareProcess.command = argv
        ledgerPrepareWatchdog.restart()
        ledgerPrepareProcess.running = true
        return true
    }

    function launchLedgerApply(argv, sessionId, draftId, target, path) {
        if (root.ledgerApplying || ledgerApplyProcess.running || root.ledgerApplyRetiring) return false
        if (!Array.isArray(argv) || argv.length === 0) return false
        root.ledgerApplyGeneration = Number(root.ledgerApplyGeneration) + 1
        root.ledgerApplyLaunchGeneration = Number(root.ledgerApplyGeneration)
        root.ledgerApplyLaunchSession = String(sessionId || "")
        root.ledgerApplyLaunchDraftId = String(draftId || "")
        root.ledgerApplyLaunchTarget = String(target || "")
        root.ledgerApplyLaunchPath = String(path || "")
        root.ledgerApplying = true
        root.ledgerApplyStarted = false
        root.ledgerApplyStartFailed = false
        ledgerApplyProcess.command = argv
        ledgerApplyWatchdog.restart()
        ledgerApplyProcess.running = true
        return true
    }

    // Load the ledger entries for the selected day. No-op while busy.
    function reloadLedger() {
        if (root.ledgerBusy || ledgerProcess.running || root.ledgerRetiring || root.ledgerApplying) return false
        let window = root.ledgerDayWindow(root.selectedDate)
        if (!window) return false
        let argv = root.ledgerListCommand(window.from_ms, window.to_ms, 50)
        if (argv.length === 0) return false
        return root.launchLedger("list", argv, {}, root.selectedDate)
    }

    // Clear the transient status, then reload.
    function refreshLedger() {
        if (root.ledgerBusy || ledgerProcess.running || root.ledgerRetiring || root.ledgerApplying) return false
        root.ledgerError = ""
        root.ledgerNotice = ""
        root.ledgerStickyError = ""
        return root.reloadLedger()
    }

    // Attach/detach a link target (capture, draft, agent_session,
    // continued_from).
    function linkLedger(sessionId, kind, target, remove) {
        if (!root.isLedgerSessionId(sessionId)) {
            root.ledgerError = root.ledgerBound("Pick a valid session.", 200)
            return false
        }
        let name = String(kind === undefined || kind === null ? "" : kind)
        if (name !== "capture" && name !== "draft" && name !== "agent_session"
                && name !== "continued_from") {
            root.ledgerError = root.ledgerBound("Pick a valid link kind.", 200)
            return false
        }
        let destination = String(target === undefined || target === null ? "" : target).trim()
        if (!destination) {
            root.ledgerError = root.ledgerBound("Pick a valid link target.", 200)
            return false
        }
        let argv = ["python3", Quickshell.shellPath("scripts/sessions.py"),
            "link", "--session", String(sessionId).trim().toLowerCase(),
            "--kind", name, "--target", destination]
        if (remove) argv.push("--remove")
        return root.launchLedger("link", argv, {}, root.selectedDate)
    }

    // Stage the exact project-page preview for a session draft. Single
    // save target: the project page.
    function prepareLedgerFile(sessionId, draftId, target) {
        if (root.ledgerPrepareBusy || ledgerPrepareProcess.running || root.ledgerPrepareRetiring) return false
        if (root.ledgerApplying) return false
        if (!root.isLedgerSessionId(sessionId)) {
            root.ledgerApplyError = root.ledgerBound("Pick a valid session.", 200)
            return false
        }
        let draft = String(draftId === undefined || draftId === null ? "" : draftId).trim()
        if (!draft) {
            root.ledgerApplyError = root.ledgerBound("Pick a session draft to file.", 200)
            return false
        }
        let kind = String(target === undefined || target === null ? "" : target)
        if (kind !== "page") {
            root.ledgerApplyError = root.ledgerBound("Pick the project page.", 200)
            return false
        }
        let entry = root.ledgerFindEntry(sessionId)
        if (entry) {
            let active = entry.session && typeof entry.session === "object"
                && !Array.isArray(entry.session) && entry.session.active === true
            if (active) {
                root.ledgerApplyError = root.ledgerBound("This session is still open.", 200)
                return false
            }
        }
        let canonical = String(sessionId).trim().toLowerCase()
        let argv = ["python3", Quickshell.shellPath("scripts/work_log.py"),
            "prepare", "--draft-id", draft, "--target", kind]
        root.ledgerApplyError = ""
        return root.launchLedgerPrepare(argv, canonical, draft, kind)
    }

    // Guarded prepare completion: validates {prepared, target, preview},
    // then stores the normalized preview + token for the Confirm panel.
    // Page text lives in preview.block.
    function finishLedgerPrepare(code, output, generation, sessionId, diagnostic) {
        if (Number(generation) !== Number(root.ledgerPrepareLaunchGeneration)) return false
        if (String(sessionId) !== String(root.ledgerPrepareLaunchSession)) return false
        ledgerPrepareWatchdog.stop()
        root.ledgerPrepareBusy = false
        let data = null
        try { data = JSON.parse(output || "{}") } catch (error) { data = null }
        let target = root.ledgerPrepareLaunchTarget
        let preview = data && data.preview ? data.preview : null
        let valid = code === 0 && data
            && typeof data.prepared === "string" && data.prepared !== ""
            && data.target === target && target === "page"
            && preview && typeof preview === "object" && !Array.isArray(preview)
            && typeof preview.path === "string" && preview.path !== ""
            && typeof preview.block === "string" && preview.block !== ""
        if (!valid) {
            let backendError = root.ledgerBackendError(data, diagnostic)
            root.ledgerApplyError = backendError
                ? root.ledgerBound(backendError, 200)
                : root.ledgerFailure("Ledger preview", code, "")
            return false
        }
        let text = preview.block
        // 65536 covers every valid draft (work_log markdown cap 16384 +
        // indentation); over-limit previews fail closed so Confirm can
        // never apply text the user did not see.
        if (typeof text !== "string" || text === "") {
            let backendError = root.ledgerBackendError(data, diagnostic)
            root.ledgerApplyError = backendError
                ? root.ledgerBound(backendError, 200)
                : root.ledgerFailure("Ledger preview", code, "")
            return false
        }
        if (text.length > 65536) {
            root.ledgerApplyError = root.ledgerBound("The file preview is too large to display safely.", 200)
            return false
        }
        root.ledgerPreview = { session: root.ledgerPrepareLaunchSession,
            draft_id: root.ledgerPrepareLaunchDraftId, target: target,
            text: root.ledgerBoundText(text, 65536),
            path: String(preview.path), token: String(data.prepared),
            graph: typeof preview.graphName === "string" ? root.ledgerBoundText(preview.graphName, 200) : "",
            page: typeof preview.page === "string" ? root.ledgerBoundText(preview.page, 200) : "" }
        root.ledgerPreviewToken = String(data.prepared)
        root.ledgerApplyError = ""
        return true
    }

    // Confirm the stored preview token. On success the preview clears
    // and the list reloads; on failure a bounded error stays and
    // the preview is kept for retry.
    function applyLedgerFile() {
        if (root.ledgerApplying || ledgerApplyProcess.running || root.ledgerApplyRetiring) return false
        if (root.ledgerPrepareBusy || ledgerPrepareProcess.running || root.ledgerPrepareRetiring) return false
        if (!root.ledgerPreview || typeof root.ledgerPreview !== "object"
                || Array.isArray(root.ledgerPreview)) return false
        if (!root.ledgerPreviewToken
                || String(root.ledgerPreviewToken).trim() === "") return false
        let argv = ["python3", Quickshell.shellPath("scripts/work_log.py"),
            "apply", "--prepared", String(root.ledgerPreviewToken)]
        if (argv.length === 0) return false
        root.ledgerApplyError = ""
        return root.launchLedgerApply(argv, String(root.ledgerPreview.session || ""),
            String(root.ledgerPreview.draft_id || ""),
            String(root.ledgerPreview.target || ""),
            String(root.ledgerPreview.path || ""))
    }

    function finishLedgerApply(code, output, generation, sessionId, diagnostic) {
        if (Number(generation) !== Number(root.ledgerApplyLaunchGeneration)) return false
        if (String(sessionId) !== String(root.ledgerApplyLaunchSession)) return false
        ledgerApplyWatchdog.stop()
        root.ledgerApplying = false
        let data = null
        try { data = JSON.parse(output || "{}") } catch (error) { data = null }
        let valid = code === 0 && data && data.applied === true
        if (!valid) {
            let backendError = root.ledgerBackendError(data, diagnostic)
            root.ledgerApplyError = backendError
                ? root.ledgerBound(backendError, 200)
                : root.ledgerFailure("Ledger save", code, "")
            return false
        }
        root.ledgerPreview = null
        root.ledgerPreviewToken = ""
        root.ledgerApplyError = ""
        root.ledgerNotice = root.ledgerBound("Saved to project page.", 200)
        root.reloadLedger()
        return true
    }

    function cancelLedgerPreview() {
        if (root.ledgerApplying) return false
        root.ledgerPreview = null
        root.ledgerPreviewToken = ""
        root.ledgerApplyError = ""
        if (root.ledgerPrepareBusy || ledgerPrepareProcess.running || root.ledgerPrepareRetiring) {
            // An explicit cancel retires an in-flight replacement
            // prepare so its late completion never repopulates the
            // preview; a hung process is still escalated via the kill
            // timer.
            root.ledgerPrepareGeneration = Number(root.ledgerPrepareGeneration) + 1
            root.ledgerPrepareLaunchGeneration = -1
            root.ledgerPrepareRetiring = true
            root.ledgerPrepareBusy = false
            root.ledgerPrepareStarted = false
            root.closeInput(ledgerPrepareProcess)
            ledgerPrepareProcess.running = false
            ledgerPrepareKillTimer.restart()
        }
        return true
    }

    // Reconcile after a timed-out write: the write may have committed,
    // so reload once the ladder is idle. Returns whether a reload launched.
    function drainLedgerRefresh() {
        if (root.ledgerBusy || ledgerProcess.running || root.ledgerRetiring || root.ledgerApplying) return false
        if (root.ledgerRefreshPending) {
            root.ledgerRefreshPending = false
            return root.reloadLedger()
        }
        return false
    }

    // Guarded list/inbox completion: generation AND launch/selected-date
    // must match (stale completions dropped). Validates {entries:[...]},
    // sanitizes rows, reconciles the selection, then applies the
    // sticky-error rule and the bounded backend reason notice.
    function finishLedgerList(code, output, generation, date, op, diagnostic) {
        if (Number(generation) !== Number(root.ledgerLaunchGeneration)) return false
        if ((op !== "list" && op !== "inbox") || root.ledgerLaunchOp !== op) return false
        if (String(date) !== String(root.ledgerLaunchDate)) return false
        ledgerWatchdog.stop()
        root.ledgerBusy = false
        if (String(date) !== String(root.selectedDate)) {
            // Stale rows are never applied, but the launch must not stay
            // busy with no process: the launch is already settled above,
            // so reload for the current date (a no-op while a newer launch
            // owns the ladder, e.g. an in-flight apply).
            root.reloadLedger()
            return false
        }
        let data = null
        try { data = JSON.parse(output || "{}") } catch (error) { data = null }
        let valid = code === 0 && data && Array.isArray(data.entries)
        if (!valid) {
            let backendError = root.ledgerBackendError(data, diagnostic)
            root.ledgerError = backendError
                ? root.ledgerBound(backendError, 200)
                : root.ledgerFailure("Sessions", code, "")
            return false
        }
        root.ledgerEntries = root.ledgerSanitizeEntries(data.entries)
        // Backend day total for the "showing X of Y" truncation line
        // (S-041). Defensive: non-numeric totals degrade to the shown
        // count so the line stays silent instead of wrong.
        let dayTotal = Number(data.total)
        if (typeof dayTotal === "number" && isFinite(dayTotal) && dayTotal >= 0)
            root.ledgerTotal = Math.floor(dayTotal)
        else root.ledgerTotal = root.ledgerEntries.length
        let selected = String(root.ledgerSelectedId || "")
        if (selected !== "") {
            let foundEntry = null
            for (let i = 0; i < root.ledgerEntries.length; i++) {
                let entry = root.ledgerEntries[i]
                if (entry && entry.session
                        && String(entry.session.session_id || "").toLowerCase() === selected.toLowerCase()) {
                    foundEntry = entry
                    break
                }
            }
            if (!foundEntry) {
                root.ledgerSelectedId = ""
                root.ledgerSelectedRevision = ""
                root.ledgerConflict = false
            }
        }
        if (root.ledgerStickyError !== "") {
            root.ledgerError = root.ledgerStickyError
            root.ledgerStickyError = ""
        } else {
            root.ledgerError = ""
        }
        let reason = data && data.reason !== undefined && data.reason !== null
            ? String(data.reason) : ""
        if (reason.trim() !== "") root.ledgerNotice = root.ledgerBound(reason, 200)
        return true
    }

    function finishLedgerLink(code, output, generation, op, diagnostic) {
        if (Number(generation) !== Number(root.ledgerLaunchGeneration)) return false
        if (op !== "link" || root.ledgerLaunchOp !== "link") return false
        ledgerWatchdog.stop()
        root.ledgerBusy = false
        let data = null
        try { data = JSON.parse(output || "{}") } catch (error) { data = null }
        let valid = code === 0 && data && data.ok === true
        if (!valid) {
            let backendError = root.ledgerBackendError(data, diagnostic)
            root.ledgerError = backendError
                ? root.ledgerBound(backendError, 200)
                : root.ledgerFailure("Ledger", code, "")
            return false
        }
        root.ledgerError = ""
        root.ledgerNotice = root.ledgerBound("Link updated.", 200)
        root.reloadLedger()
        return true
    }

    // Actual ledger exit handler (thin onExited delegates here so tests
    // exercise the real logic). Uses the frozen launch generation/date/op,
    // never re-read current state. The launchBefore guard keeps a reload
    // started inside a finish from being clobbered by the identity reset,
    // and a staged refresh drains once the ladder is idle.
    function handleLedgerExited(code, output, diagnostic) {
        ledgerWatchdog.stop()
        ledgerKillTimer.stop()
        if (!root.ledgerBusy && !root.ledgerRetiring) {
            // No launch in flight (already reconciled): drop late exits.
            if (!root.ledgerStarted && root.ledgerLaunchGeneration < 0) return false
        }
        if (root.ledgerRetiring) {
            root.ledgerBusy = false
            root.ledgerLaunchGeneration = -1
            root.ledgerRetiring = false
            root.ledgerStarted = false
            root.ledgerStartFailed = false
            root.drainLedgerRefresh()
            return false
        }
        let launchBefore = root.ledgerGeneration
        let generation = root.ledgerLaunchGeneration
        let date = root.ledgerLaunchDate
        let op = root.ledgerLaunchOp
        let applied = false
        if (op === "list" || op === "inbox")
            applied = root.finishLedgerList(code, output, generation, date, op, diagnostic)
        else if (op === "link")
            applied = root.finishLedgerLink(code, output, generation, op, diagnostic)
        if (Number(root.ledgerGeneration) === Number(launchBefore)) {
            // No reload drained a new launch: settle the identity. A
            // drained launch owns the flags now and its completion must
            // not be dropped as "not busy".
            root.ledgerLaunchGeneration = -1
            root.ledgerStarted = false
            root.ledgerStartFailed = false
        }
        root.drainLedgerRefresh()
        return applied
    }

    function handleLedgerPrepareExited(code, output, diagnostic) {
        ledgerPrepareWatchdog.stop()
        ledgerPrepareKillTimer.stop()
        if (!root.ledgerPrepareBusy && !root.ledgerPrepareRetiring) {
            // No launch in flight (already reconciled): drop late exits.
            if (!root.ledgerPrepareStarted && root.ledgerPrepareLaunchGeneration < 0) return false
        }
        if (root.ledgerPrepareRetiring) {
            root.ledgerPrepareBusy = false
            root.ledgerPrepareLaunchGeneration = -1
            root.ledgerPrepareRetiring = false
            root.ledgerPrepareStarted = false
            root.ledgerPrepareStartFailed = false
            return false
        }
        let launchBefore = root.ledgerPrepareGeneration
        let generation = root.ledgerPrepareLaunchGeneration
        let sessionId = root.ledgerPrepareLaunchSession
        let applied = root.finishLedgerPrepare(code, output, generation, sessionId, diagnostic)
        if (Number(root.ledgerPrepareGeneration) === Number(launchBefore)) {
            root.ledgerPrepareLaunchGeneration = -1
            root.ledgerPrepareStarted = false
            root.ledgerPrepareStartFailed = false
        }
        return applied
    }

    function handleLedgerApplyExited(code, output, diagnostic) {
        ledgerApplyWatchdog.stop()
        ledgerApplyKillTimer.stop()
        if (!root.ledgerApplying && !root.ledgerApplyRetiring) {
            // No launch in flight (already reconciled): drop late exits.
            if (!root.ledgerApplyStarted && root.ledgerApplyLaunchGeneration < 0) return false
        }
        if (root.ledgerApplyRetiring) {
            root.ledgerApplying = false
            root.ledgerApplyLaunchGeneration = -1
            root.ledgerApplyRetiring = false
            root.ledgerApplyStarted = false
            root.ledgerApplyStartFailed = false
            root.drainLedgerRefresh()
            return false
        }
        let launchBefore = root.ledgerApplyGeneration
        let generation = root.ledgerApplyLaunchGeneration
        let sessionId = root.ledgerApplyLaunchSession
        let applied = root.finishLedgerApply(code, output, generation, sessionId, diagnostic)
        if (Number(root.ledgerApplyGeneration) === Number(launchBefore)) {
            root.ledgerApplyLaunchGeneration = -1
            root.ledgerApplyStarted = false
            root.ledgerApplyStartFailed = false
        }
        root.drainLedgerRefresh()
        return applied
    }

    function handleLedgerRunningChanged() {
        if (!ledgerProcess.running && (root.ledgerBusy || root.ledgerStarted))
            root.ledgerRetiring = true
        if (!ledgerProcess.running && root.ledgerBusy && !root.ledgerStarted && !root.ledgerStartFailed) {
            root.ledgerStartFailed = true
            root.handleLedgerStartFailure(root.ledgerLaunchGeneration)
        }
    }

    function handleLedgerPrepareRunningChanged() {
        if (!ledgerPrepareProcess.running && (root.ledgerPrepareBusy || root.ledgerPrepareStarted))
            root.ledgerPrepareRetiring = true
        if (!ledgerPrepareProcess.running && root.ledgerPrepareBusy && !root.ledgerPrepareStarted && !root.ledgerPrepareStartFailed) {
            root.ledgerPrepareStartFailed = true
            root.handleLedgerPrepareStartFailure(root.ledgerPrepareLaunchGeneration)
        }
    }

    function handleLedgerApplyRunningChanged() {
        if (!ledgerApplyProcess.running && (root.ledgerApplying || root.ledgerApplyStarted))
            root.ledgerApplyRetiring = true
        if (!ledgerApplyProcess.running && root.ledgerApplying && !root.ledgerApplyStarted && !root.ledgerApplyStartFailed) {
            root.ledgerApplyStartFailed = true
            root.handleLedgerApplyStartFailure(root.ledgerApplyLaunchGeneration)
        }
    }

    function handleLedgerStartFailure(generation) {
        if (Number(generation) !== Number(root.ledgerGeneration)) return
        ledgerWatchdog.stop()
        ledgerKillTimer.stop()
        if (!root.ledgerStarted && !ledgerProcess.running) root.ledgerRetiring = false
        root.ledgerBusy = false
        let message = root.ledgerFailure("Ledger", 0, "process could not start")
        root.ledgerError = message
        // A start failure emits no exit, so staged requests can never
        // drain: drop them and surface the bounded failure on the
        // surface that requested them instead of leaving them pending.
        if (root.ledgerRefreshPending) {
            root.ledgerRefreshPending = false
            root.ledgerError = message
        }
    }

    function handleLedgerPrepareStartFailure(generation) {
        if (Number(generation) !== Number(root.ledgerPrepareGeneration)) return
        ledgerPrepareWatchdog.stop()
        ledgerPrepareKillTimer.stop()
        if (!root.ledgerPrepareStarted && !ledgerPrepareProcess.running) root.ledgerPrepareRetiring = false
        root.ledgerPrepareBusy = false
        root.ledgerApplyError = root.ledgerFailure("Ledger", 0, "process could not start")
    }

    function handleLedgerApplyStartFailure(generation) {
        if (Number(generation) !== Number(root.ledgerApplyGeneration)) return
        ledgerApplyWatchdog.stop()
        ledgerApplyKillTimer.stop()
        if (!root.ledgerApplyStarted && !ledgerApplyProcess.running) root.ledgerApplyRetiring = false
        root.ledgerApplying = false
        root.ledgerApplyError = root.ledgerFailure("Ledger", 0, "process could not start")
    }

    function handleLedgerTimeout() {
        if (!root.ledgerBusy) {
            ledgerWatchdog.stop()
            return false
        }
        ledgerWatchdog.stop()
        root.ledgerRetiring = true
        if (root.ledgerLaunchOp === "list" || root.ledgerLaunchOp === "inbox")
            root.ledgerError = root.ledgerBound("The session list is taking longer than expected; retry.", 200)
        else root.ledgerError = root.ledgerBound("The ledger write is taking longer than expected; reload to check.", 200)
        if (root.ledgerLaunchOp === "link")
            root.ledgerRefreshPending = true
        root.closeInput(ledgerProcess)
        ledgerProcess.running = false
        ledgerKillTimer.restart()
        return true
    }

    function fireLedgerKillTimeout() {
        if (root.ledgerRetiring && ledgerProcess.running) {
            try {
                ledgerProcess.signal(9)
            } catch (error) {}
            return true
        }
        return false
    }

    function handleLedgerPrepareTimeout() {
        if (!root.ledgerPrepareBusy) {
            ledgerPrepareWatchdog.stop()
            return false
        }
        ledgerPrepareWatchdog.stop()
        root.ledgerPrepareRetiring = true
        root.ledgerApplyError = root.ledgerBound("The file preview is taking longer than expected; retry.", 200)
        root.closeInput(ledgerPrepareProcess)
        ledgerPrepareProcess.running = false
        ledgerPrepareKillTimer.restart()
        return true
    }

    function fireLedgerPrepareKillTimeout() {
        if (root.ledgerPrepareRetiring && ledgerPrepareProcess.running) {
            try {
                ledgerPrepareProcess.signal(9)
            } catch (error) {}
            return true
        }
        return false
    }

    function handleLedgerApplyTimeout() {
        if (!root.ledgerApplying) {
            ledgerApplyWatchdog.stop()
            return false
        }
        ledgerApplyWatchdog.stop()
        root.ledgerApplyRetiring = true
        root.ledgerApplyError = root.ledgerBound("The file save is taking longer than expected; reload to check the journal/page.", 200)
        root.ledgerRefreshPending = true
        root.closeInput(ledgerApplyProcess)
        ledgerApplyProcess.running = false
        ledgerApplyKillTimer.restart()
        return true
    }

    function fireLedgerApplyKillTimeout() {
        if (root.ledgerApplyRetiring && ledgerApplyProcess.running) {
            try {
                ledgerApplyProcess.signal(9)
            } catch (error) {}
            return true
        }
        return false
    }

    // ---- Session bar badge (advisory, read-only) ----
    // Today's sessions-needing-attention count for the bar's
    // CurrentProjectModule. No writes, no new timers: the 3s cadence
    // comes from CurrentProjectModule calling refreshLedgerInboxBadge()
    // once per existing project-poller launch. Never touches the card
    // ladder above (dedicated Process + generation ladder only).

    // List-form argv for the badge. No --from/--to: the helper's
    // default window is the current local day.
    function ledgerInboxBadgeCommand() {
        return ["python3", Quickshell.shellPath("scripts/sessions.py"),
            "inbox", "--limit", "1"]
    }

    // Pure parser for the badge response. Returns -1 when invalid,
    // otherwise 0..999 (capped). Prefers data.total, falls back to
    // data.entries.length only when total is absent. Note: the
    // CurrentProjectModule clamp floors floats because it reads an int
    // property; this parser rejects them instead (different contracts).
    function parseLedgerInboxBadge(output, code) {
        if (code !== 0) return -1
        let data = null
        try { data = JSON.parse(output || "{}") } catch (error) { return -1 }
        if (!data || typeof data !== "object" || Array.isArray(data)) return -1
        if (Object.prototype.hasOwnProperty.call(data, "total")) {
            let total = data.total
            if (typeof total === "number" && isFinite(total) && Math.floor(total) === total && total >= 0)
                return Math.min(total, 999)
            return -1
        }
        if (Array.isArray(data.entries)) return Math.min(data.entries.length, 999)
        return -1
    }

    // Serialized launch for the dedicated badge Process. Never overlap
    // a running badge, a retiring one, or the runningChanged window.
    function refreshLedgerInboxBadge() {
        if (root.ledgerInboxBadgeBusy || ledgerInboxBadgeProcess.running || root.ledgerInboxBadgeRetiring) return false
        let argv = root.ledgerInboxBadgeCommand()
        if (!Array.isArray(argv) || argv.length === 0) return false
        root.ledgerInboxBadgeGeneration = Number(root.ledgerInboxBadgeGeneration) + 1
        root.ledgerInboxBadgeLaunchGeneration = Number(root.ledgerInboxBadgeGeneration)
        root.ledgerInboxBadgeBusy = true
        root.ledgerInboxBadgeStarted = false
        ledgerInboxBadgeProcess.command = argv
        ledgerInboxBadgeWatchdog.restart()
        ledgerInboxBadgeProcess.running = true
        return true
    }

    // Guarded badge completion: stale generations dropped, failures keep
    // the previous count (badge is silent, never surfaces an error).
    function finishLedgerInboxBadge(code, output, generation) {
        if (Number(generation) !== Number(root.ledgerInboxBadgeLaunchGeneration)) return false
        ledgerInboxBadgeWatchdog.stop()
        root.ledgerInboxBadgeBusy = false
        let count = root.parseLedgerInboxBadge(output, code)
        if (count < 0) return false
        root.ledgerInboxBadge = count
        return true
    }

    // Actual badge exit handler (thin onExited delegates here so tests
    // exercise the real logic). Uses the frozen launch generation, never
    // re-reads current state. Either path consumes the launch.
    function handleLedgerInboxBadgeExited(code, output) {
        ledgerInboxBadgeWatchdog.stop()
        ledgerInboxBadgeKillTimer.stop()
        if (!root.ledgerInboxBadgeBusy && !root.ledgerInboxBadgeRetiring) {
            // No launch in flight (already reconciled): drop late exits.
            if (!root.ledgerInboxBadgeStarted && root.ledgerInboxBadgeLaunchGeneration < 0) return false
        }
        if (root.ledgerInboxBadgeRetiring) {
            root.ledgerInboxBadgeBusy = false
            root.ledgerInboxBadgeLaunchGeneration = -1
            root.ledgerInboxBadgeRetiring = false
            root.ledgerInboxBadgeStarted = false
            return false
        }
        let launchBefore = root.ledgerInboxBadgeGeneration
        let generation = root.ledgerInboxBadgeLaunchGeneration
        let applied = root.finishLedgerInboxBadge(code, output, generation)
        if (Number(root.ledgerInboxBadgeGeneration) === Number(launchBefore)) {
            root.ledgerInboxBadgeLaunchGeneration = -1
            root.ledgerInboxBadgeStarted = false
        }
        return applied
    }

    // Badge is silent: a failed start reconciles without surfacing an
    // error, and the last count is kept.
    function handleLedgerInboxBadgeRunningChanged() {
        if (!ledgerInboxBadgeProcess.running && (root.ledgerInboxBadgeBusy || root.ledgerInboxBadgeStarted))
            root.ledgerInboxBadgeRetiring = true
        if (!ledgerInboxBadgeProcess.running && root.ledgerInboxBadgeBusy && !root.ledgerInboxBadgeStarted) {
            ledgerInboxBadgeWatchdog.stop()
            ledgerInboxBadgeKillTimer.stop()
            root.ledgerInboxBadgeBusy = false
            root.ledgerInboxBadgeLaunchGeneration = -1
            root.ledgerInboxBadgeRetiring = false
            return true
        }
        return false
    }

    function handleLedgerInboxBadgeTimeout() {
        if (!root.ledgerInboxBadgeBusy) {
            ledgerInboxBadgeWatchdog.stop()
            return false
        }
        ledgerInboxBadgeWatchdog.stop()
        root.ledgerInboxBadgeRetiring = true
        ledgerInboxBadgeProcess.running = false
        ledgerInboxBadgeKillTimer.restart()
        return true
    }

    function fireLedgerInboxBadgeKillTimeout() {
        if (root.ledgerInboxBadgeRetiring && ledgerInboxBadgeProcess.running) {
            try {
                ledgerInboxBadgeProcess.signal(9)
            } catch (error) {}
            return true
        }
        return false
    }

    // ---- Session card ladders (§4.5 thought/organise/dismiss, §4.4 unmapped) ----

    function thoughtBound(text, limit) {
        let max = Number(limit) > 0 ? Math.floor(Number(limit)) : 200
        let clean = String(text === undefined || text === null ? "" : text)
            .replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim()
        if (clean.length > max) clean = clean.substring(0, max - 1) + "…"
        return clean
    }

    function thoughtFailure(label, code, detail) {
        return root.thoughtBound(String(label || "Thought") + " failed"
            + (code ? " (exit " + code + ")" : "")
            + (detail ? ": " + String(detail) : ""), 200)
    }

    function organiseBound(text, limit) {
        let max = Number(limit) > 0 ? Math.floor(Number(limit)) : 200
        let clean = String(text === undefined || text === null ? "" : text)
            .replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim()
        if (clean.length > max) clean = clean.substring(0, max - 1) + "…"
        return clean
    }

    function organiseFailure(label, code, detail) {
        return root.organiseBound(String(label || "Organise") + " failed"
            + (code ? " (exit " + code + ")" : "")
            + (detail ? ": " + String(detail) : ""), 200)
    }

    // Captures for one session: the day's new captures filed under the
    // session's resolved project. Pure client-side filter (captures are
    // project-scoped; the collector session id is provenance only).
    function sessionCapturesFor(sessionId) {
        if (!root.isLedgerSessionId(sessionId)) return []
        let entry = root.ledgerFindEntry(sessionId)
        if (!entry) return []
        let session = entry.session
        let project = session && typeof session === "object" ? session.project : null
        let pid = project && typeof project === "object" && typeof project.id === "string"
            ? project.id.trim().toLowerCase() : ""
        if (pid === "") return []
        let items = Array.isArray(root.captureItems) ? root.captureItems : []
        let out = []
        for (let i = 0; i < items.length; i++) {
            let item = items[i]
            if (!item || typeof item !== "object" || Array.isArray(item)) continue
            if (String(item.project_id || "").trim().toLowerCase() !== pid) continue
            if (item.id === undefined || item.id === null) continue
            if (typeof item.text !== "string" || !item.text.trim()) continue
            out.push(item)
            if (out.length >= 20) break
        }
        return out
    }

    function sessionTodoCount(sessionId) {
        return root.sessionCapturesFor(sessionId).length
    }

    // Distinct projects across the day's entries for the explicit
    // attribution picker. Bounded at 50 rows like the ledger list.
    function sessionProjects() {
        let rows = Array.isArray(root.ledgerEntries) ? root.ledgerEntries : []
        let seen = {}
        let out = []
        for (let i = 0; i < rows.length && out.length < 50; i++) {
            let entry = rows[i]
            if (!entry || typeof entry !== "object" || Array.isArray(entry)) continue
            let session = entry.session
            if (!session || typeof session !== "object" || Array.isArray(session)) continue
            let project = session.project
            if (!project || typeof project !== "object" || Array.isArray(project)) continue
            let id = typeof project.id === "string" ? project.id.trim().toLowerCase() : ""
            if (!/^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(id)) continue
            if (seen[id]) continue
            seen[id] = true
            let name = typeof project.name === "string" && project.name.trim() !== ""
                ? project.name.trim().substring(0, 80) : id.substring(0, 8)
            out.push({ id: id, name: name })
        }
        return out
    }

    // Open-project target for a session: the resolved project id or ""
    // (navigation only — no backend call, no write).
    function openSessionProject(sessionId) {
        return root.ledgerResumeProject(sessionId)
    }

    // ---- Thought save (journal prepare -> exact preview -> Confirm) ----

    // Argv builders (pure): journal_assistant speaks stdin JSON, so the
    // commands carry only the subcommand; payloads are built at launch.
    function thoughtContextCommand() {
        return ["python3", Quickshell.shellPath("scripts/journal_assistant.py"),
            "--graph", root.graphArg(), "context"]
    }

    function thoughtPrepareCommand() {
        return ["python3", Quickshell.shellPath("scripts/journal_assistant.py"),
            "--graph", root.graphArg(), "prepare"]
    }

    function thoughtApplyCommand() {
        return ["python3", Quickshell.shellPath("scripts/journal_assistant.py"),
            "--graph", root.graphArg(), "append"]
    }

    // Serialized launch for the dedicated thought Process. Never overlap
    // a running thought, a retiring one, or the runningChanged window.
    // The stdin payload is frozen in thoughtPayload and written onStarted.
    function launchThought(op, date, argv, payload) {
        if (root.thoughtBusy || thoughtProcess.running || root.thoughtRetiring) return false
        if (!Array.isArray(argv) || argv.length === 0) return false
        if (op !== "context" && op !== "prepare" && op !== "apply") return false
        root.thoughtGeneration = Number(root.thoughtGeneration) + 1
        root.thoughtOp = op
        root.thoughtLaunchGeneration = Number(root.thoughtGeneration)
        root.thoughtLaunchOp = op
        root.thoughtLaunchDate = String(date || "")
        if (payload && typeof payload === "object" && !Array.isArray(payload))
            root.thoughtPayload = payload
        else root.thoughtPayload = {}
        root.thoughtBusy = true
        root.thoughtStarted = false
        root.thoughtStartFailed = false
        if (op === "apply") root.thoughtApplying = true
        thoughtProcess.command = argv
        thoughtWatchdog.restart()
        thoughtProcess.stdinEnabled = true
        thoughtProcess.running = true
        return true
    }

    // Stage a thought for today's journal (the journal destination is
    // always today, even when the card shows another day). Explicit
    // click only; chains context (fresh revision) -> prepare (exact
    // preview). No write and no model call happens here.
    function saveThought(sessionId, text) {
        if (root.thoughtBusy || thoughtProcess.running || root.thoughtRetiring) return false
        if (root.thoughtApplying) return false
        if (!root.isLedgerSessionId(sessionId)) {
            root.thoughtError = root.thoughtBound("Pick a valid session.", 200)
            return false
        }
        let body = String(text === undefined || text === null ? "" : text)
        if (body.trim() === "") {
            root.thoughtError = root.thoughtBound("Write a thought before saving.", 200)
            return false
        }
        if (body.length > 4096) {
            root.thoughtError = root.thoughtBound("The thought is too long to save safely.", 200)
            return false
        }
        let argv = root.thoughtContextCommand()
        if (argv.length === 0) return false
        root.thoughtError = ""
        root.thoughtApplyError = ""
        root.thoughtPreview = null
        root.thoughtSessionId = String(sessionId).trim().toLowerCase()
        root.thoughtLaunchSession = root.thoughtSessionId
        root.thoughtText = body
        return root.launchThought("context", root.selectedDate, argv, {})
    }

    // Guarded context completion: validates {revision}, then chains the
    // prepare with the frozen draft text. The chain starts only from an
    // explicit Save thought click above.
    function finishThoughtContext(code, output, generation, date, op, diagnostic) {
        if (Number(generation) !== Number(root.thoughtLaunchGeneration)) return false
        if (String(date) !== String(root.thoughtLaunchDate)) return false
        if (String(date) !== String(root.selectedDate)) return false
        if (op !== "context" || root.thoughtLaunchOp !== "context") return false
        thoughtWatchdog.stop()
        root.thoughtBusy = false
        let data = null
        try { data = JSON.parse(output || "{}") } catch (error) { data = null }
        let revision = data && typeof data.revision === "string" ? data.revision : ""
        if (!(code === 0 && data && revision !== "")) {
            let backendError = data && data.error ? String(data.error) : ""
            if (!backendError && diagnostic) backendError = String(diagnostic).replace(/^\s*error:\s*/i, "").trim()
            root.thoughtError = backendError
                ? root.thoughtBound(backendError, 200)
                : root.thoughtFailure("Thought", code, "")
            return false
        }
        root.thoughtJournalRevision = revision
        root.thoughtError = ""
        let argv = root.thoughtPrepareCommand()
        if (argv.length === 0) {
            root.thoughtError = root.thoughtFailure("Thought preview", 0, "unavailable")
            return false
        }
        return root.launchThought("prepare", root.thoughtLaunchDate, argv,
            { date: root.todayIso(), revision: revision,
                text: root.thoughtText, session_id: root.thoughtSessionId })
    }

    // Guarded prepare completion: validates {addition, path, revision},
    // then stores the exact preview for Confirm. The preview is shown
    // verbatim; nothing is written until Confirm.
    function finishThoughtPrepare(code, output, generation, date, op, diagnostic) {
        if (Number(generation) !== Number(root.thoughtLaunchGeneration)) return false
        if (String(date) !== String(root.thoughtLaunchDate)) return false
        if (String(date) !== String(root.selectedDate)) return false
        if (op !== "prepare" || root.thoughtLaunchOp !== "prepare") return false
        thoughtWatchdog.stop()
        root.thoughtBusy = false
        let data = null
        try { data = JSON.parse(output || "{}") } catch (error) { data = null }
        let valid = code === 0 && data
            && typeof data.addition === "string" && data.addition !== ""
            && typeof data.path === "string" && data.path !== ""
            && typeof data.revision === "string" && data.revision !== ""
        if (!valid) {
            let backendError = data && data.error ? String(data.error) : ""
            if (!backendError && diagnostic) backendError = String(diagnostic).replace(/^\s*error:\s*/i, "").trim()
            root.thoughtApplyError = backendError
                ? root.thoughtBound(backendError, 200)
                : root.thoughtFailure("Thought preview", code, "")
            return false
        }
        if (data.addition.length > 65536) {
            root.thoughtApplyError = root.thoughtBound("The thought preview is too large to display safely.", 200)
            return false
        }
        root.thoughtPreview = { addition: data.addition, path: String(data.path) }
        root.thoughtApplyRevision = String(data.revision)
        root.thoughtApplyAddition = String(data.addition)
        root.thoughtApplyError = ""
        return true
    }

    // Confirm the staged preview: appends the exact addition shown.
    // Revision rechecked by the backend; single-use by construction
    // (the preview clears on success).
    function applyThought() {
        if (root.thoughtBusy || thoughtProcess.running || root.thoughtRetiring) return false
        if (root.thoughtApplying) return false
        if (!root.thoughtPreview || typeof root.thoughtPreview !== "object") return false
        if (String(root.thoughtApplyRevision || "") === "") return false
        if (String(root.thoughtApplyAddition || "") === "") return false
        let argv = root.thoughtApplyCommand()
        if (argv.length === 0) return false
        root.thoughtApplyError = ""
        return root.launchThought("apply", root.selectedDate, argv,
            { date: root.todayIso(), revision: root.thoughtApplyRevision,
                addition: root.thoughtApplyAddition, session_id: root.thoughtSessionId })
    }

    function finishThoughtApply(code, output, generation, date, op, diagnostic) {
        if (Number(generation) !== Number(root.thoughtLaunchGeneration)) return false
        if (String(date) !== String(root.thoughtLaunchDate)) return false
        if (String(date) !== String(root.selectedDate)) return false
        if (op !== "apply" || root.thoughtLaunchOp !== "apply") return false
        thoughtWatchdog.stop()
        root.thoughtBusy = false
        root.thoughtApplying = false
        let data = null
        try { data = JSON.parse(output || "{}") } catch (error) { data = null }
        let valid = code === 0 && data
            && typeof data.path === "string" && data.path !== ""
            && typeof data.revision === "string" && data.revision !== ""
        if (!valid) {
            let backendError = data && data.error ? String(data.error) : ""
            if (!backendError && diagnostic) backendError = String(diagnostic).replace(/^\s*error:\s*/i, "").trim()
            root.thoughtApplyError = backendError
                ? root.thoughtBound(backendError, 200)
                : root.thoughtFailure("Thought save", code, "")
            return false
        }
        let saved = String(root.thoughtSessionId || "")
        root.thoughtPreview = null
        root.thoughtApplyRevision = ""
        root.thoughtApplyAddition = ""
        root.thoughtSessionId = ""
        root.thoughtLaunchSession = ""
        root.thoughtText = ""
        root.thoughtApplyError = ""
        root.thoughtNotice = root.thoughtBound("Saved to today's journal.", 200)
        if (saved !== "") root.thoughtSaved(saved)
        root.reloadLedger()
        return true
    }

    function cancelThoughtPreview() {
        if (root.thoughtApplying) return false
        root.thoughtPreview = null
        root.thoughtApplyRevision = ""
        root.thoughtApplyAddition = ""
        root.thoughtApplyError = ""
        if (root.thoughtBusy || thoughtProcess.running || root.thoughtRetiring) {
            root.thoughtGeneration = Number(root.thoughtGeneration) + 1
            root.thoughtLaunchGeneration = -1
            root.thoughtRetiring = true
            root.thoughtBusy = false
            root.thoughtStarted = false
            root.closeInput(thoughtProcess)
            thoughtProcess.running = false
            thoughtKillTimer.restart()
        }
        return true
    }

    // ---- Organise (L8, the only model path: explicit + preview-first) ----

    // Argv builders (pure over text/session): list-form, no shell.
    // Thought text rides --text (bounded 4 KiB, fail closed); the
    // session id is optional 32-hex provenance.
    function organisePrepareCommand(text, sessionId) {
        let body = String(text === undefined || text === null ? "" : text)
        if (body.trim() === "" || body.length > 4096) return []
        let argv = ["python3", Quickshell.shellPath("scripts/thought_organise.py"),
            "prepare", "--text", body]
        let sid = String(sessionId === undefined || sessionId === null ? "" : sessionId).trim().toLowerCase()
        if (sid !== "") {
            if (!/^[0-9a-f]{32}$/.test(sid)) return []
            argv.push("--session", sid)
        }
        return argv
    }

    function organiseApplyCommand(token) {
        let value = String(token || "").trim()
        if (!value) return []
        return ["python3", Quickshell.shellPath("scripts/thought_organise.py"),
            "apply", "--prepared", value]
    }

    // Serialized launch for the dedicated organise Process. The prepare
    // watchdog is 65s (the one Pi call may take 60s); apply stays 12s.
    function launchOrganise(op, date, argv) {
        if (root.organiseBusy || organiseProcess.running || root.organiseRetiring) return false
        if (!Array.isArray(argv) || argv.length === 0) return false
        if (op !== "prepare" && op !== "apply") return false
        root.organiseGeneration = Number(root.organiseGeneration) + 1
        root.organiseOp = op
        root.organiseLaunchGeneration = Number(root.organiseGeneration)
        root.organiseLaunchOp = op
        root.organiseLaunchDate = String(date || "")
        root.organiseBusy = true
        root.organiseStarted = false
        root.organiseStartFailed = false
        if (op === "apply") root.organiseApplying = true
        organiseProcess.command = argv
        organiseWatchdog.interval = op === "prepare" ? 65000 : 12000
        organiseWatchdog.restart()
        organiseProcess.running = true
        return true
    }

    // Explicit Organise click only: stages the rewritten preview.
    // Nothing is written and the editor is untouched until Confirm.
    function prepareOrganise(sessionId, text) {
        if (root.organiseBusy || organiseProcess.running || root.organiseRetiring) return false
        if (root.organiseApplying) return false
        if (!root.isLedgerSessionId(sessionId)) {
            root.organiseError = root.organiseBound("Pick a valid session.", 200)
            return false
        }
        let argv = root.organisePrepareCommand(text, sessionId)
        if (argv.length === 0) {
            root.organiseError = root.organiseBound("The thought is empty or too long to organise.", 200)
            return false
        }
        root.organiseError = ""
        root.organiseApplyError = ""
        root.organisePreview = null
        root.organiseToken = ""
        root.organiseLaunchSession = String(sessionId).trim().toLowerCase()
        root.organiseText = String(text)
        return root.launchOrganise("prepare", root.selectedDate, argv)
    }

    // Guarded prepare completion: validates {prepared, preview}, then
    // stores the token + preview for the Confirm panel. The preview
    // replaces the editor text only after Confirm (apply burns the
    // single-use token and returns the text; no write happens).
    function finishOrganisePrepare(code, output, generation, date, op, diagnostic) {
        if (Number(generation) !== Number(root.organiseLaunchGeneration)) return false
        if (String(date) !== String(root.organiseLaunchDate)) return false
        if (String(date) !== String(root.selectedDate)) return false
        if (op !== "prepare" || root.organiseLaunchOp !== "prepare") return false
        organiseWatchdog.stop()
        root.organiseBusy = false
        let data = null
        try { data = JSON.parse(output || "{}") } catch (error) { data = null }
        let valid = code === 0 && data
            && typeof data.prepared === "string" && data.prepared !== ""
            && typeof data.preview === "string" && data.preview.trim() !== ""
        if (!valid) {
            let backendError = data && data.error ? String(data.error) : ""
            if (!backendError && diagnostic) backendError = String(diagnostic).replace(/^\s*error:\s*/i, "").trim()
            root.organiseApplyError = backendError
                ? root.organiseBound(backendError, 200)
                : root.organiseFailure("Organise preview", code, "")
            return false
        }
        if (data.preview.length > 4096) {
            root.organiseApplyError = root.organiseBound("The organised preview is too large to display safely.", 200)
            return false
        }
        root.organisePreview = { text: String(data.preview) }
        root.organiseToken = String(data.prepared)
        root.organiseApplyError = ""
        return true
    }

    // Confirm the staged preview: burns the single-use token and
    // returns the text into the editor (still unsaved until the
    // journal Confirm).
    function applyOrganise() {
        if (root.organiseBusy || organiseProcess.running || root.organiseRetiring) return false
        if (root.organiseApplying) return false
        if (!root.organisePreview || !root.organiseToken) return false
        let argv = root.organiseApplyCommand(root.organiseToken)
        if (argv.length === 0) return false
        root.organiseApplyError = ""
        return root.launchOrganise("apply", root.selectedDate, argv)
    }

    function finishOrganiseApply(code, output, generation, date, op, diagnostic) {
        if (Number(generation) !== Number(root.organiseLaunchGeneration)) return false
        if (String(date) !== String(root.organiseLaunchDate)) return false
        if (String(date) !== String(root.selectedDate)) return false
        if (op !== "apply" || root.organiseLaunchOp !== "apply") return false
        organiseWatchdog.stop()
        root.organiseBusy = false
        root.organiseApplying = false
        let data = null
        try { data = JSON.parse(output || "{}") } catch (error) { data = null }
        let valid = code === 0 && data && data.applied === true
            && typeof data.text === "string" && data.text.trim() !== ""
        if (!valid) {
            let backendError = data && data.error ? String(data.error) : ""
            if (!backendError && diagnostic) backendError = String(diagnostic).replace(/^\s*error:\s*/i, "").trim()
            root.organiseApplyError = backendError
                ? root.organiseBound(backendError, 200)
                : root.organiseFailure("Organise", code, "")
            return false
        }
        let sid = String(root.organiseLaunchSession || "")
        if (data.session_id && String(data.session_id).trim() !== "") sid = String(data.session_id).trim().toLowerCase()
        root.organisePreview = null
        root.organiseToken = ""
        root.organiseText = ""
        root.organiseApplyError = ""
        root.organiseError = ""
        if (sid !== "") root.organiseDone(sid, String(data.text))
        return true
    }

    function cancelOrganisePreview() {
        if (root.organiseApplying) return false
        root.organisePreview = null
        root.organiseToken = ""
        root.organiseApplyError = ""
        if (root.organiseBusy || organiseProcess.running || root.organiseRetiring) {
            root.organiseGeneration = Number(root.organiseGeneration) + 1
            root.organiseLaunchGeneration = -1
            root.organiseRetiring = true
            root.organiseBusy = false
            root.organiseStarted = false
            organiseProcess.running = false
            organiseKillTimer.restart()
        }
        return true
    }

    // ---- Dismiss session (attended = 1, revision-checked) ----

    function dismissSessionCommand() {
        return ["python3", Quickshell.shellPath("scripts/sessions.py"), "annotate"]
    }

    // Serialized launch for the dedicated dismiss Process. The stdin
    // payload is frozen in dismissPayload and written onStarted.
    function launchDismiss(date, argv, payload) {
        if (root.dismissBusy || dismissProcess.running || root.dismissRetiring) return false
        if (!Array.isArray(argv) || argv.length === 0) return false
        root.dismissGeneration = Number(root.dismissGeneration) + 1
        root.dismissLaunchGeneration = Number(root.dismissGeneration)
        root.dismissLaunchDate = String(date || "")
        if (payload && typeof payload === "object" && !Array.isArray(payload))
            root.dismissPayload = payload
        else root.dismissPayload = {}
        root.dismissBusy = true
        root.dismissStarted = false
        root.dismissStartFailed = false
        dismissProcess.command = argv
        dismissWatchdog.restart()
        dismissProcess.stdinEnabled = true
        dismissProcess.running = true
        return true
    }

    // Mark a session attended. It stays visible (attended drives the
    // row marker and the badge, never visibility). Revision rechecked
    // by the backend; omit revision only when no meta row exists yet.
    function dismissSession(sessionId) {
        if (root.dismissBusy || dismissProcess.running || root.dismissRetiring) return false
        if (!root.isLedgerSessionId(sessionId)) {
            root.dismissError = root.ledgerBound("Pick a valid session.", 200)
            return false
        }
        let entry = root.ledgerFindEntry(sessionId)
        if (!entry) return false
        let canonical = String(sessionId).trim().toLowerCase()
        let payload = { session: canonical, attended: 1 }
        let meta = entry.meta
        if (meta && typeof meta === "object" && !Array.isArray(meta)
                && typeof meta.revision === "string" && meta.revision !== "")
            payload.revision = meta.revision
        let argv = root.dismissSessionCommand()
        if (argv.length === 0) return false
        root.dismissError = ""
        root.dismissSessionId = canonical
        return root.launchDismiss(root.selectedDate, argv, payload)
    }

    function finishDismissSession(code, output, generation, date, diagnostic) {
        if (Number(generation) !== Number(root.dismissLaunchGeneration)) return false
        if (String(date) !== String(root.dismissLaunchDate)) return false
        if (String(date) !== String(root.selectedDate)) return false
        dismissWatchdog.stop()
        root.dismissBusy = false
        let data = null
        try { data = JSON.parse(output || "{}") } catch (error) { data = null }
        let valid = code === 0 && data && data.ok === true
        if (!valid) {
            let backendError = data && data.error ? String(data.error) : ""
            if (!backendError && diagnostic) backendError = String(diagnostic).replace(/^\s*error:\s*/i, "").trim()
            root.dismissError = backendError
                ? root.ledgerBound(backendError, 200)
                : root.ledgerFailure("Dismiss session", code, "")
            return false
        }
        root.dismissError = ""
        root.dismissSessionId = ""
        root.ledgerNotice = root.ledgerBound("Session dismissed.", 200)
        root.reloadLedger()
        return true
    }

    // ---- Attribution hygiene (§4.4 unmapped folders) ----

    function unmappedBound(text, limit) {
        let max = Number(limit) > 0 ? Math.floor(Number(limit)) : 200
        let clean = String(text === undefined || text === null ? "" : text)
            .replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim()
        if (clean.length > max) clean = clean.substring(0, max - 1) + "…"
        return clean
    }

    // Argv builder (pure over days/limit): list-form, no shell.
    // Bounded ≤ 10 rows per the plan contract.
    function unmappedListCommand(days, limit) {
        let d = days === undefined || days === null || days === "" ? 7 : Number(days)
        if (typeof d !== "number" || !isFinite(d) || Math.floor(d) !== d || d < 1 || d > 31) return []
        let n = limit === undefined || limit === null || limit === "" ? 10 : Number(limit)
        if (typeof n !== "number" || !isFinite(n) || Math.floor(n) !== n || n < 1 || n > 10) return []
        return ["python3", Quickshell.shellPath("scripts/desktop_projects.py"),
            "unmapped-folders", "--days", String(d), "--limit", String(n)]
    }

    // Serialized launch for the dedicated unmapped Process.
    function launchUnmapped(date, argv) {
        if (root.unmappedBusy || unmappedProcess.running || root.unmappedRetiring) return false
        if (!Array.isArray(argv) || argv.length === 0) return false
        root.unmappedGeneration = Number(root.unmappedGeneration) + 1
        root.unmappedLaunchGeneration = Number(root.unmappedGeneration)
        root.unmappedLaunchDate = String(date || "")
        root.unmappedBusy = true
        root.unmappedStarted = false
        root.unmappedStartFailed = false
        unmappedProcess.command = argv
        unmappedWatchdog.restart()
        unmappedProcess.running = true
        return true
    }

    function reloadUnmapped() {
        if (root.unmappedBusy || unmappedProcess.running || root.unmappedRetiring) return false
        let argv = root.unmappedListCommand(7, 10)
        if (argv.length === 0) return false
        return root.launchUnmapped(root.selectedDate, argv)
    }

    // Keep only rows with a folder path; cap at 10. Accepts the plan
    // shape {git_root_or_cwd, observation_count, last_seen_ms} plus
    // tolerant alternates (path/folder) for forward compatibility.
    function unmappedSanitizeRows(rows) {
        let out = []
        let list = Array.isArray(rows) ? rows : []
        for (let i = 0; i < list.length && out.length < 10; i++) {
            let row = list[i]
            if (!row || typeof row !== "object" || Array.isArray(row)) continue
            let folder = row.git_root_or_cwd
            if (folder === undefined || folder === null || String(folder).trim() === "") folder = row.path
            if (folder === undefined || folder === null || String(folder).trim() === "") folder = row.folder
            if (folder === undefined || folder === null || String(folder).trim() === "") continue
            let clean = String(folder).trim()
            if (clean.length > 512) clean = clean.substring(0, 511) + "…"
            let count = Number(row.observation_count)
            if (!(count >= 0)) count = Number(row.count)
            if (!(count >= 0)) count = 0
            let seen = Number(row.last_seen_ms)
            if (!(seen >= 0)) seen = 0
            out.push({ folder: clean, observation_count: Math.floor(count), last_seen_ms: Math.floor(seen) })
        }
        return out
    }

    // Guarded list completion: stale generations dropped, failures keep
    // the previous rows (inline bounded error, never modal).
    function finishUnmapped(code, output, generation, date, diagnostic) {
        if (Number(generation) !== Number(root.unmappedLaunchGeneration)) return false
        if (String(date) !== String(root.unmappedLaunchDate)) return false
        if (String(date) !== String(root.selectedDate)) return false
        unmappedWatchdog.stop()
        root.unmappedBusy = false
        let data = null
        try { data = JSON.parse(output || "{}") } catch (error) { data = null }
        let valid = code === 0 && data && Array.isArray(data.rows)
        if (!valid) {
            let backendError = data && data.error ? String(data.error) : ""
            if (!backendError && diagnostic) backendError = String(diagnostic).replace(/^\s*error:\s*/i, "").trim()
            root.unmappedError = backendError
                ? root.unmappedBound(backendError, 200)
                : root.unmappedBound("Unmapped folders failed" + (code ? " (exit " + code + ")" : "") + ".", 200)
            return false
        }
        root.unmappedRows = root.unmappedSanitizeRows(data.rows)
        root.unmappedError = ""
        return true
    }

    // Ephemeral ignore (never durable content): hides the folder for
    // this shell run. Bounded at 100 entries.
    function ignoreUnmapped(folder) {
        let clean = String(folder === undefined || folder === null ? "" : folder).trim()
        if (clean === "") return false
        let list = Array.isArray(root.unmappedIgnored) ? root.unmappedIgnored.slice() : []
        for (let i = 0; i < list.length; i++) {
            if (String(list[i]) === clean) return true
        }
        list.push(clean)
        while (list.length > 100) list.shift()
        root.unmappedIgnored = list
        return true
    }

    function unmappedVisibleRows() {
        let rows = Array.isArray(root.unmappedRows) ? root.unmappedRows : []
        let ignored = Array.isArray(root.unmappedIgnored) ? root.unmappedIgnored : []
        if (ignored.length === 0) return rows
        return rows.filter(row => row && ignored.indexOf(String(row.folder || "")) < 0)
    }

    // Actual thought exit handler (thin onExited delegates here so tests
    // exercise the real logic). Uses the frozen launch generation/date,
    // never re-read current state. Either path consumes the launch.
    function handleThoughtExited(code, output, diagnostic) {
        thoughtWatchdog.stop()
        thoughtKillTimer.stop()
        if (!root.thoughtBusy && !root.thoughtRetiring) {
            if (!root.thoughtStarted && root.thoughtLaunchGeneration < 0) return false
        }
        if (root.thoughtRetiring) {
            root.thoughtBusy = false
            root.thoughtApplying = false
            root.thoughtLaunchGeneration = -1
            root.thoughtRetiring = false
            root.thoughtStarted = false
            root.thoughtStartFailed = false
            return false
        }
        let launchBefore = root.thoughtGeneration
        let generation = root.thoughtLaunchGeneration
        let date = root.thoughtLaunchDate
        let op = root.thoughtLaunchOp
        let applied = false
        if (op === "context") applied = root.finishThoughtContext(code, output, generation, date, op, diagnostic)
        else if (op === "prepare") applied = root.finishThoughtPrepare(code, output, generation, date, op, diagnostic)
        else if (op === "apply") applied = root.finishThoughtApply(code, output, generation, date, op, diagnostic)
        if (Number(root.thoughtGeneration) === Number(launchBefore)) {
            root.thoughtLaunchGeneration = -1
            root.thoughtStarted = false
            root.thoughtStartFailed = false
        }
        return applied
    }

    function handleThoughtRunningChanged() {
        if (!thoughtProcess.running && (root.thoughtBusy || root.thoughtStarted))
            root.thoughtRetiring = true
        if (!thoughtProcess.running && root.thoughtBusy && !root.thoughtStarted && !root.thoughtStartFailed) {
            root.thoughtStartFailed = true
            root.handleThoughtStartFailure(root.thoughtLaunchGeneration)
        }
    }

    function handleThoughtStartFailure(generation) {
        if (Number(generation) !== Number(root.thoughtGeneration)) return
        thoughtWatchdog.stop()
        thoughtKillTimer.stop()
        if (!root.thoughtStarted && !thoughtProcess.running) root.thoughtRetiring = false
        root.thoughtBusy = false
        root.thoughtApplying = false
        root.thoughtError = root.thoughtFailure("Thought", 0, "process could not start")
    }

    function handleThoughtTimeout() {
        if (!root.thoughtBusy) {
            thoughtWatchdog.stop()
            return false
        }
        thoughtWatchdog.stop()
        root.thoughtRetiring = true
        if (root.thoughtLaunchOp === "apply")
            root.thoughtApplyError = root.thoughtBound("The thought save is taking longer than expected; reload to check the journal.", 200)
        else root.thoughtError = root.thoughtBound("The thought request is taking longer than expected; retry.", 200)
        root.closeInput(thoughtProcess)
        thoughtProcess.running = false
        thoughtKillTimer.restart()
        return true
    }

    function fireThoughtKillTimeout() {
        if (root.thoughtRetiring && thoughtProcess.running) {
            try {
                thoughtProcess.signal(9)
            } catch (error) {}
            return true
        }
        return false
    }

    // Actual organise exit handler (thin onExited delegates here).
    function handleOrganiseExited(code, output, diagnostic) {
        organiseWatchdog.stop()
        organiseKillTimer.stop()
        if (!root.organiseBusy && !root.organiseRetiring) {
            if (!root.organiseStarted && root.organiseLaunchGeneration < 0) return false
        }
        if (root.organiseRetiring) {
            root.organiseBusy = false
            root.organiseApplying = false
            root.organiseLaunchGeneration = -1
            root.organiseRetiring = false
            root.organiseStarted = false
            root.organiseStartFailed = false
            return false
        }
        let launchBefore = root.organiseGeneration
        let generation = root.organiseLaunchGeneration
        let date = root.organiseLaunchDate
        let op = root.organiseLaunchOp
        let applied = false
        if (op === "prepare") applied = root.finishOrganisePrepare(code, output, generation, date, op, diagnostic)
        else if (op === "apply") applied = root.finishOrganiseApply(code, output, generation, date, op, diagnostic)
        if (Number(root.organiseGeneration) === Number(launchBefore)) {
            root.organiseLaunchGeneration = -1
            root.organiseStarted = false
            root.organiseStartFailed = false
        }
        return applied
    }

    function handleOrganiseRunningChanged() {
        if (!organiseProcess.running && (root.organiseBusy || root.organiseStarted))
            root.organiseRetiring = true
        if (!organiseProcess.running && root.organiseBusy && !root.organiseStarted && !root.organiseStartFailed) {
            root.organiseStartFailed = true
            root.handleOrganiseStartFailure(root.organiseLaunchGeneration)
        }
    }

    function handleOrganiseStartFailure(generation) {
        if (Number(generation) !== Number(root.organiseGeneration)) return
        organiseWatchdog.stop()
        organiseKillTimer.stop()
        if (!root.organiseStarted && !organiseProcess.running) root.organiseRetiring = false
        root.organiseBusy = false
        root.organiseApplying = false
        root.organiseError = root.organiseFailure("Organise", 0, "process could not start")
    }

    function handleOrganiseTimeout() {
        if (!root.organiseBusy) {
            organiseWatchdog.stop()
            return false
        }
        organiseWatchdog.stop()
        root.organiseRetiring = true
        if (root.organiseLaunchOp === "apply")
            root.organiseApplyError = root.organiseBound("The organise confirm is taking longer than expected; retry.", 200)
        else root.organiseError = root.organiseBound("Organise is taking longer than expected (the model call may take up to a minute); retry.", 200)
        organiseProcess.running = false
        organiseKillTimer.restart()
        return true
    }

    function fireOrganiseKillTimeout() {
        if (root.organiseRetiring && organiseProcess.running) {
            try {
                organiseProcess.signal(9)
            } catch (error) {}
            return true
        }
        return false
    }

    // Actual dismiss exit handler (thin onExited delegates here).
    function handleDismissExited(code, output, diagnostic) {
        dismissWatchdog.stop()
        dismissKillTimer.stop()
        if (!root.dismissBusy && !root.dismissRetiring) {
            if (!root.dismissStarted && root.dismissLaunchGeneration < 0) return false
        }
        if (root.dismissRetiring) {
            root.dismissBusy = false
            root.dismissLaunchGeneration = -1
            root.dismissRetiring = false
            root.dismissStarted = false
            root.dismissStartFailed = false
            return false
        }
        let launchBefore = root.dismissGeneration
        let generation = root.dismissLaunchGeneration
        let date = root.dismissLaunchDate
        let applied = root.finishDismissSession(code, output, generation, date, diagnostic)
        if (Number(root.dismissGeneration) === Number(launchBefore)) {
            root.dismissLaunchGeneration = -1
            root.dismissStarted = false
            root.dismissStartFailed = false
        }
        return applied
    }

    function handleDismissRunningChanged() {
        if (!dismissProcess.running && (root.dismissBusy || root.dismissStarted))
            root.dismissRetiring = true
        if (!dismissProcess.running && root.dismissBusy && !root.dismissStarted && !root.dismissStartFailed) {
            root.dismissStartFailed = true
            root.handleDismissStartFailure(root.dismissLaunchGeneration)
        }
    }

    function handleDismissStartFailure(generation) {
        if (Number(generation) !== Number(root.dismissGeneration)) return
        dismissWatchdog.stop()
        dismissKillTimer.stop()
        if (!root.dismissStarted && !dismissProcess.running) root.dismissRetiring = false
        root.dismissBusy = false
        root.dismissError = root.ledgerFailure("Dismiss session", 0, "process could not start")
    }

    function handleDismissTimeout() {
        if (!root.dismissBusy) {
            dismissWatchdog.stop()
            return false
        }
        dismissWatchdog.stop()
        root.dismissRetiring = true
        root.dismissError = root.ledgerBound("Dismiss is taking longer than expected; reload to check.", 200)
        root.closeInput(dismissProcess)
        dismissProcess.running = false
        dismissKillTimer.restart()
        return true
    }

    function fireDismissKillTimeout() {
        if (root.dismissRetiring && dismissProcess.running) {
            try {
                dismissProcess.signal(9)
            } catch (error) {}
            return true
        }
        return false
    }

    // Actual unmapped exit handler (thin onExited delegates here).
    function handleUnmappedExited(code, output, diagnostic) {
        unmappedWatchdog.stop()
        unmappedKillTimer.stop()
        if (!root.unmappedBusy && !root.unmappedRetiring) {
            if (!root.unmappedStarted && root.unmappedLaunchGeneration < 0) return false
        }
        if (root.unmappedRetiring) {
            root.unmappedBusy = false
            root.unmappedLaunchGeneration = -1
            root.unmappedRetiring = false
            root.unmappedStarted = false
            root.unmappedStartFailed = false
            return false
        }
        let launchBefore = root.unmappedGeneration
        let generation = root.unmappedLaunchGeneration
        let date = root.unmappedLaunchDate
        let applied = root.finishUnmapped(code, output, generation, date, diagnostic)
        if (Number(root.unmappedGeneration) === Number(launchBefore)) {
            root.unmappedLaunchGeneration = -1
            root.unmappedStarted = false
            root.unmappedStartFailed = false
        }
        return applied
    }

    function handleUnmappedRunningChanged() {
        if (!unmappedProcess.running && (root.unmappedBusy || root.unmappedStarted))
            root.unmappedRetiring = true
        if (!unmappedProcess.running && root.unmappedBusy && !root.unmappedStarted && !root.unmappedStartFailed) {
            root.unmappedStartFailed = true
            root.handleUnmappedStartFailure(root.unmappedLaunchGeneration)
        }
    }

    function handleUnmappedStartFailure(generation) {
        if (Number(generation) !== Number(root.unmappedGeneration)) return
        unmappedWatchdog.stop()
        unmappedKillTimer.stop()
        if (!root.unmappedStarted && !unmappedProcess.running) root.unmappedRetiring = false
        root.unmappedBusy = false
        root.unmappedError = root.unmappedBound("Unmapped folders could not start.", 200)
    }

    function handleUnmappedTimeout() {
        if (!root.unmappedBusy) {
            unmappedWatchdog.stop()
            return false
        }
        unmappedWatchdog.stop()
        root.unmappedRetiring = true
        root.unmappedError = root.unmappedBound("Unmapped folders is taking longer than expected; retry.", 200)
        unmappedProcess.running = false
        unmappedKillTimer.restart()
        return true
    }

    function fireUnmappedKillTimeout() {
        if (root.unmappedRetiring && unmappedProcess.running) {
            try {
                unmappedProcess.signal(9)
            } catch (error) {}
            return true
        }
        return false
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

    // Dedicated capture inbox backend (scripts/session_capture.py).
    // List-form argv, workingDirectory shellPath("."), 12s watchdog +
    // 3s kill escalation. Immutable launch generation/date; stale
    // completions dropped by generation AND selected-date mismatch.
    Process {
        id: captureProcess
        workingDirectory: Quickshell.shellPath(".")
        stdinEnabled: false
        stdout: StdioCollector { id: captureOutput; waitForEnd: true }
        stderr: StdioCollector { id: captureErrorOut; waitForEnd: true }
        onStarted: {
            root.captureStarted = true
        }
        onRunningChanged: root.handleCaptureRunningChanged()
        onExited: (code) => {
            let failed = root.captureStartFailed
            let generation = root.captureLaunchGeneration
            root.captureStarted = false
            root.captureStartFailed = false
            if (failed) root.handleCaptureStartFailure(generation)
            else root.handleCaptureExited(code, captureOutput.text)
        }
    }

    Timer {
        id: captureWatchdog
        interval: 12000
        repeat: false
        onTriggered: root.handleCaptureTimeout()
    }

    Timer {
        id: captureKillTimer
        interval: 3000
        repeat: false
        onTriggered: root.fireCaptureKillTimeout()
    }

    // Dedicated review backend (scripts/daily_review.py). Argv-only
    // flags, workingDirectory shellPath("."), 12s watchdog + 3s kill
    // escalation. Immutable launch generation/date; stale completions
    // dropped by generation AND selected-date mismatch.
    Process {
        id: reviewProcess
        workingDirectory: Quickshell.shellPath(".")
        stdinEnabled: false
        stdout: StdioCollector { id: reviewOutput; waitForEnd: true }
        stderr: StdioCollector { id: reviewErrorOut; waitForEnd: true }
        onStarted: {
            root.reviewStarted = true
        }
        onRunningChanged: root.handleReviewRunningChanged()
        onExited: (code) => {
            let failed = root.reviewStartFailed
            let generation = root.reviewLaunchGeneration
            root.reviewStarted = false
            root.reviewStartFailed = false
            if (failed) root.handleReviewStartFailure(generation)
            else root.handleReviewExited(code, reviewOutput.text)
        }
    }

    Timer {
        id: reviewWatchdog
        interval: 12000
        repeat: false
        onTriggered: root.handleReviewTimeout()
    }

    Timer {
        id: reviewKillTimer
        interval: 3000
        repeat: false
        onTriggered: root.fireReviewKillTimeout()
    }

    // Dedicated session backend (scripts/sessions.py).
    // List-form argv, workingDirectory shellPath("."), 12s watchdog + 3s
    // kill escalation. Immutable launch generation/date/op plus a frozen
    // stdin payload (written onStarted, then closed); stale completions
    // dropped by generation AND operation identity.
    Process {
        id: ledgerProcess
        workingDirectory: Quickshell.shellPath(".")
        stdinEnabled: false
        stdout: StdioCollector { id: ledgerOutput; waitForEnd: true }
        stderr: StdioCollector { id: ledgerErrorOut; waitForEnd: true }
        onStarted: {
            root.ledgerStarted = true
            root.writeJson(ledgerProcess, root.ledgerPayload || {})
        }
        onRunningChanged: root.handleLedgerRunningChanged()
        onExited: (code) => {
            let failed = root.ledgerStartFailed
            let generation = root.ledgerLaunchGeneration
            root.ledgerStarted = false
            root.ledgerStartFailed = false
            if (failed) root.handleLedgerStartFailure(generation)
            else root.handleLedgerExited(code, ledgerOutput.text, ledgerErrorOut.text)
        }
    }

    Timer {
        id: ledgerWatchdog
        interval: 12000
        repeat: false
        onTriggered: root.handleLedgerTimeout()
    }

    Timer {
        id: ledgerKillTimer
        interval: 3000
        repeat: false
        onTriggered: root.fireLedgerKillTimeout()
    }

    // Exact-preview filing through scripts/work_log.py (argv-only, like
    // the overview session-log ladder). Prepare stages the preview +
    // token; apply consumes the token, then the list reloads
    // on the ledger ladder above.
    Process {
        id: ledgerPrepareProcess
        workingDirectory: Quickshell.shellPath(".")
        stdinEnabled: false
        stdout: StdioCollector { id: ledgerPrepareOutput; waitForEnd: true }
        stderr: StdioCollector { id: ledgerPrepareErrorOut; waitForEnd: true }
        onStarted: {
            root.ledgerPrepareStarted = true
        }
        onRunningChanged: root.handleLedgerPrepareRunningChanged()
        onExited: (code) => {
            let failed = root.ledgerPrepareStartFailed
            let generation = root.ledgerPrepareLaunchGeneration
            root.ledgerPrepareStarted = false
            root.ledgerPrepareStartFailed = false
            if (failed) root.handleLedgerPrepareStartFailure(generation)
            else root.handleLedgerPrepareExited(code, ledgerPrepareOutput.text, ledgerPrepareErrorOut.text)
        }
    }

    Timer {
        id: ledgerPrepareWatchdog
        interval: 12000
        repeat: false
        onTriggered: root.handleLedgerPrepareTimeout()
    }

    Timer {
        id: ledgerPrepareKillTimer
        interval: 3000
        repeat: false
        onTriggered: root.fireLedgerPrepareKillTimeout()
    }

    Process {
        id: ledgerApplyProcess
        workingDirectory: Quickshell.shellPath(".")
        stdinEnabled: false
        stdout: StdioCollector { id: ledgerApplyOutput; waitForEnd: true }
        stderr: StdioCollector { id: ledgerApplyErrorOut; waitForEnd: true }
        onStarted: {
            root.ledgerApplyStarted = true
        }
        onRunningChanged: root.handleLedgerApplyRunningChanged()
        onExited: (code) => {
            let failed = root.ledgerApplyStartFailed
            let generation = root.ledgerApplyLaunchGeneration
            root.ledgerApplyStarted = false
            root.ledgerApplyStartFailed = false
            if (failed) root.handleLedgerApplyStartFailure(generation)
            else root.handleLedgerApplyExited(code, ledgerApplyOutput.text, ledgerApplyErrorOut.text)
        }
    }

    Timer {
        id: ledgerApplyWatchdog
        interval: 12000
        repeat: false
        onTriggered: root.handleLedgerApplyTimeout()
    }

    Timer {
        id: ledgerApplyKillTimer
        interval: 3000
        repeat: false
        onTriggered: root.fireLedgerApplyKillTimeout()
    }

    // Session bar badge backend (scripts/sessions.py
    // inbox --limit 1). List-form argv, workingDirectory shellPath("."),
    // 12s watchdog + 3s kill escalation. Immutable launch generation;
    // stale completions dropped. No repeating timer: the 3s cadence
    // comes from CurrentProjectModule calling refreshLedgerInboxBadge().
    Process {
        id: ledgerInboxBadgeProcess
        workingDirectory: Quickshell.shellPath(".")
        stdinEnabled: false
        stdout: StdioCollector { id: ledgerInboxBadgeOutput; waitForEnd: true }
        stderr: StdioCollector { id: ledgerInboxBadgeErrorOut; waitForEnd: true }
        onStarted: {
            root.ledgerInboxBadgeStarted = true
        }
        onRunningChanged: root.handleLedgerInboxBadgeRunningChanged()
        onExited: (code) => root.handleLedgerInboxBadgeExited(code, ledgerInboxBadgeOutput.text)
    }

    Timer {
        id: ledgerInboxBadgeWatchdog
        interval: 12000
        repeat: false
        onTriggered: root.handleLedgerInboxBadgeTimeout()
    }

    Timer {
        id: ledgerInboxBadgeKillTimer
        interval: 3000
        repeat: false
        onTriggered: root.fireLedgerInboxBadgeKillTimeout()
    }

    // Thought save backend (scripts/journal_assistant.py context /
    // prepare / append over stdin JSON). List-form argv,
    // workingDirectory shellPath("."), 12s watchdog + 3s kill
    // escalation. Immutable launch generation/date/op plus a frozen
    // stdin payload (written onStarted, then closed); stale completions
    // dropped by generation AND operation identity.
    Process {
        id: thoughtProcess
        workingDirectory: Quickshell.shellPath(".")
        stdinEnabled: false
        stdout: StdioCollector { id: thoughtOutput; waitForEnd: true }
        stderr: StdioCollector { id: thoughtErrorOut; waitForEnd: true }
        onStarted: {
            root.thoughtStarted = true
            root.writeJson(thoughtProcess, root.thoughtPayload || {})
        }
        onRunningChanged: root.handleThoughtRunningChanged()
        onExited: (code) => {
            let failed = root.thoughtStartFailed
            root.thoughtStarted = false
            root.thoughtStartFailed = false
            if (failed) root.handleThoughtStartFailure(root.thoughtLaunchGeneration)
            else root.handleThoughtExited(code, thoughtOutput.text, thoughtErrorOut.text)
        }
    }

    Timer {
        id: thoughtWatchdog
        interval: 12000
        repeat: false
        onTriggered: root.handleThoughtTimeout()
    }

    Timer {
        id: thoughtKillTimer
        interval: 3000
        repeat: false
        onTriggered: root.fireThoughtKillTimeout()
    }

    // Organise backend (scripts/thought_organise.py prepare / apply,
    // argv-only). List-form argv, workingDirectory shellPath("."), 3s
    // kill escalation. The prepare watchdog is 65s (the single Pi call
    // may take 60s); apply stays 12s. Immutable launch
    // generation/date/op; stale completions dropped by generation AND
    // operation identity.
    Process {
        id: organiseProcess
        workingDirectory: Quickshell.shellPath(".")
        stdinEnabled: false
        stdout: StdioCollector { id: organiseOutput; waitForEnd: true }
        stderr: StdioCollector { id: organiseErrorOut; waitForEnd: true }
        onStarted: {
            root.organiseStarted = true
        }
        onRunningChanged: root.handleOrganiseRunningChanged()
        onExited: (code) => {
            let failed = root.organiseStartFailed
            root.organiseStarted = false
            root.organiseStartFailed = false
            if (failed) root.handleOrganiseStartFailure(root.organiseLaunchGeneration)
            else root.handleOrganiseExited(code, organiseOutput.text, organiseErrorOut.text)
        }
    }

    Timer {
        id: organiseWatchdog
        interval: 12000
        repeat: false
        onTriggered: root.handleOrganiseTimeout()
    }

    Timer {
        id: organiseKillTimer
        interval: 3000
        repeat: false
        onTriggered: root.fireOrganiseKillTimeout()
    }

    // Dismiss-session backend (scripts/sessions.py annotate over stdin
    // JSON). List-form argv, workingDirectory shellPath("."), 12s
    // watchdog + 3s kill escalation. Immutable launch generation/date
    // plus a frozen stdin payload (written onStarted, then closed).
    Process {
        id: dismissProcess
        workingDirectory: Quickshell.shellPath(".")
        stdinEnabled: false
        stdout: StdioCollector { id: dismissOutput; waitForEnd: true }
        stderr: StdioCollector { id: dismissErrorOut; waitForEnd: true }
        onStarted: {
            root.dismissStarted = true
            root.writeJson(dismissProcess, root.dismissPayload || {})
        }
        onRunningChanged: root.handleDismissRunningChanged()
        onExited: (code) => {
            let failed = root.dismissStartFailed
            root.dismissStarted = false
            root.dismissStartFailed = false
            if (failed) root.handleDismissStartFailure(root.dismissLaunchGeneration)
            else root.handleDismissExited(code, dismissOutput.text, dismissErrorOut.text)
        }
    }

    Timer {
        id: dismissWatchdog
        interval: 12000
        repeat: false
        onTriggered: root.handleDismissTimeout()
    }

    Timer {
        id: dismissKillTimer
        interval: 3000
        repeat: false
        onTriggered: root.fireDismissKillTimeout()
    }

    // Attribution-hygiene backend (scripts/desktop_projects.py
    // unmapped-folders, argv-only). List-form argv, workingDirectory
    // shellPath("."), 12s watchdog + 3s kill escalation. Immutable
    // launch generation/date; stale completions dropped.
    Process {
        id: unmappedProcess
        workingDirectory: Quickshell.shellPath(".")
        stdinEnabled: false
        stdout: StdioCollector { id: unmappedOutput; waitForEnd: true }
        stderr: StdioCollector { id: unmappedErrorOut; waitForEnd: true }
        onStarted: {
            root.unmappedStarted = true
        }
        onRunningChanged: root.handleUnmappedRunningChanged()
        onExited: (code) => {
            let failed = root.unmappedStartFailed
            root.unmappedStarted = false
            root.unmappedStartFailed = false
            if (failed) root.handleUnmappedStartFailure(root.unmappedLaunchGeneration)
            else root.handleUnmappedExited(code, unmappedOutput.text, unmappedErrorOut.text)
        }
    }

    Timer {
        id: unmappedWatchdog
        interval: 12000
        repeat: false
        onTriggered: root.handleUnmappedTimeout()
    }

    Timer {
        id: unmappedKillTimer
        interval: 3000
        repeat: false
        onTriggered: root.fireUnmappedKillTimeout()
    }

    // Scheduler refresh: a changed scan/tick reloads the inbox list,
    // the review payload, and the session ledger.
    Connections {
        target: root.scheduler
        enabled: !!root.scheduler
        ignoreUnknownSignals: true
        function onDataChanged() { root.reloadCaptures(); root.reloadReview(); root.reloadLedger() }
    }

    Component.onCompleted: root.startList()
}
