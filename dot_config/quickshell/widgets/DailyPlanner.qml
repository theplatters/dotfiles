import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import "../theme"

// Reusable daily-planner UI bound to a shared DailyAgenda state object.
// Used both by the clock CalendarPopout and the ProjectPlanner Daily tab,
// so the date, drafts, and Pomodoro stay identical in both surfaces.
//
// Card set per host (§6.3): `compact` renders the slim popout set (month
// grid, Scheduled, picker, Pomodoro, quick-add); the full planner Daily
// tab additionally shows Captured, Review, and SessionCard.
ColumnLayout {
    id: root

    property var agenda: null
    property bool compact: false
    property string pickerFilter: ""
    property int viewYear: 2026
    property int viewMonth: 8
    // Quick-add TODO state (compact popout only, §6.3): bare `todo:`
    // input routing into the current project page, else today's journal,
    // via prepare → exact preview → Confirm. No silent writes; the write
    // lands only on Confirm. Provenance is degraded to a bare block
    // (no quickshell-session::/quickshell-ref::): this surface has no
    // ambient block, and the palette owns the session-scoped flow.
    property string quickText: ""
    property string quickRequestText: ""
    property string quickTarget: ""
    property string quickTargetName: ""
    property string quickPreview: ""
    property string quickRevision: ""
    property string quickContent: ""
    property string quickPath: ""
    property string quickProjectId: ""
    property string quickDate: ""
    property string quickAddition: ""
    property bool quickBusy: false
    property bool quickConfirming: false
    property bool quickApplying: false
    property bool quickStarted: false
    property bool quickStartFailed: false
    property string quickError: ""
    property string quickNotice: ""
    property string quickApplyError: ""
    property int quickGeneration: 0
    property int quickProcessGeneration: 0
    property string quickStage: ""
    property var quickPayload: null

    signal projectPlanningRequested(string projectId, string action, string message)

    spacing: 10

    function weekdayNames() {
        return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    }

    function syncViewToDate() {
        if (!root.agenda || !root.agenda.parseIsoDate) return
        let parsed = root.agenda.parseIsoDate(root.agenda.selectedDate)
        if (parsed) {
            root.viewYear = parsed.year
            root.viewMonth = parsed.month - 1
        }
    }

    function shiftMonth(delta) {
        let total = root.viewYear * 12 + root.viewMonth + delta
        root.viewYear = Math.floor(total / 12)
        root.viewMonth = total % 12
        if (root.viewMonth < 0) {
            root.viewMonth += 12
            root.viewYear -= 1
        }
    }

    function goToday() {
        if (!root.agenda) return
        // Set the date first so the month grid follows the new selection.
        // Sync only on success; a blocked set (busy/saving) keeps the
        // current view instead of jumping to the wrong month.
        if (root.agenda.setSelectedDate(root.agenda.todayIso())) root.syncViewToDate()
    }

    Component.onCompleted: root.syncViewToDate()

    // ---- Quick-add TODO (§6.3, §8.2) ----
    //
    // One serialized Process across the read stages (project/page/context/
    // prepare) plus the apply stage. List-form argv, shell=False (QML
    // Process), 12 s watchdog on reads; the apply write is never killed
    // on timeout (it may already have committed), only warned — the
    // planner toggle precedent.
    function quickSingleLineError(stderrText, fallback) {
        try {
            let raw = String(stderrText || "")
            let first = raw.split("\n")[0] || ""
            first = first.replace(/^error:\s*/, "").trim()
            if (first) return first.substring(0, 160)
        } catch (error) {}
        return fallback
    }

    function quickValidRevision(value) {
        try {
            if (typeof value === "string" && /^[0-9a-f]{64}$/i.test(value)) return value
            if (value === "missing") return value
        } catch (error) {}
        return ""
    }

    function quickComposePageBlock(text) {
        return "- TODO " + String(text || "").trim()
    }

    function quickBegin() {
        if (root.quickBusy) { root.quickError = "Preparing TODO…"; return false }
        // An armed preview needs its explicit Confirm button: Enter
        // never confirms.
        if (root.quickConfirming) return false
        let text = ""
        try {
            text = String(quickAddField.text || "").trim()
        } catch (error) {
            text = ""
        }
        if (!text) { root.quickError = "Type a TODO first"; return false }
        if (text.length > 2000) { root.quickError = "TODO is too long"; return false }
        root.quickText = text
        root.quickRequestText = text
        root.quickTarget = ""
        root.quickTargetName = ""
        root.quickPreview = ""
        root.quickRevision = ""
        root.quickContent = ""
        root.quickPath = ""
        root.quickProjectId = ""
        root.quickDate = ""
        root.quickAddition = ""
        root.quickError = ""
        root.quickApplyError = ""
        return root.quickLaunch("project",
            ["python3", Quickshell.shellPath("scripts/desktop_projects.py"), "current-project"],
            null)
    }

    function quickLaunch(stage, argv, payload) {
        if (quickProcess.running) { root.quickError = "Preparing TODO…"; return false }
        root.quickGeneration++
        root.quickProcessGeneration = root.quickGeneration
        root.quickStage = String(stage || "")
        root.quickPayload = (payload === undefined) ? null : payload
        root.quickBusy = true
        if (String(stage || "") !== "apply") root.quickApplying = false
        else root.quickApplying = true
        root.quickError = ""
        quickProcess.command = argv
        quickProcess.stdinEnabled = (payload !== null && payload !== undefined)
        quickWatchdog.restart()
        quickProcess.running = true
        return true
    }

    function finishQuickStage(code, output, stderrText, generation, stage) {
        if (generation !== root.quickProcessGeneration || stage !== root.quickStage) return false
        root.quickBusy = false
        root.quickApplying = false
        quickWatchdog.stop()
        // A changed input invalidates the in-flight prepare: the preview
        // belongs to the old text. Apply is exempt — it writes the
        // confirmed snapshot.
        if (stage !== "apply") {
            let current = ""
            try {
                current = String(quickAddField.text || "").trim()
            } catch (error) {
                current = ""
            }
            if (current !== root.quickRequestText) return false
        }
        if (stage === "project") {
            if (code !== 0) {
                root.quickError = root.quickSingleLineError(stderrText, "Project lookup failed")
                return false
            }
            let payload = null
            try {
                payload = JSON.parse(output || "{}")
            } catch (error) {
                payload = null
            }
            if (!payload || typeof payload !== "object") {
                root.quickError = "Project lookup returned invalid data"
                return false
            }
            let project = (payload.project && typeof payload.project === "object") ? payload.project : null
            if (project && project.id && payload.has_logseq_linkage === true) {
                root.quickProjectId = String(project.id)
                try {
                    if (typeof project.name === "string" && project.name.trim())
                        root.quickTargetName = project.name.trim().substring(0, 80)
                } catch (error) {}
                return root.quickLaunch("page",
                    ["python3", Quickshell.shellPath("scripts/project_planner.py"), "page"],
                    ({ project_id: String(project.id) }))
            }
            return root.quickLaunch("context",
                ["python3", Quickshell.shellPath("scripts/journal_assistant.py"), "context"],
                ({ query: "" }))
        }
        if (stage === "page") {
            if (code !== 0) {
                root.quickError = root.quickSingleLineError(stderrText, "Project page is unavailable")
                return false
            }
            let page = null
            try {
                page = JSON.parse(output || "{}")
            } catch (error) {
                page = null
            }
            let revision = page ? root.quickValidRevision(page.revision) : ""
            let content = (page && typeof page.content === "string") ? page.content : null
            let path = (page && typeof page.path === "string") ? page.path : ""
            if (!page || !revision || content === null || !path) {
                root.quickError = "Project page returned invalid data"
                return false
            }
            let block = root.quickComposePageBlock(root.quickText)
            let separator = (!content || content[content.length - 1] === "\n") ? "" : "\n"
            let composed = content + separator + block + "\n"
            if (composed.length > 128 * 1024) {
                root.quickError = "Project page would be too large"
                return false
            }
            root.quickTarget = "page"
            if (!root.quickTargetName) root.quickTargetName = "project page"
            root.quickPath = path
            root.quickRevision = revision
            root.quickContent = composed
            root.quickPreview = block
            root.quickConfirming = true
            root.quickError = ""
            return true
        }
        if (stage === "context") {
            if (code !== 0) {
                root.quickError = root.quickSingleLineError(stderrText, "Journal is unavailable")
                return false
            }
            let context = null
            try {
                context = JSON.parse(output || "{}")
            } catch (error) {
                context = null
            }
            let date = (context && typeof context.date === "string") ? context.date : ""
            let revision = context ? root.quickValidRevision(context.revision) : ""
            if (!context || !/^\d{4}_\d{2}_\d{2}$/.test(date) || !revision) {
                root.quickError = "Journal returned invalid data"
                return false
            }
            root.quickDate = date
            root.quickRevision = revision
            return root.quickLaunch("prepare",
                ["python3", Quickshell.shellPath("scripts/journal_assistant.py"), "prepare"],
                ({ date: date, revision: revision, text: "- TODO " + root.quickText }))
        }
        if (stage === "prepare") {
            if (code !== 0) {
                root.quickError = root.quickSingleLineError(stderrText, "Journal prepare failed")
                return false
            }
            let prepared = null
            try {
                prepared = JSON.parse(output || "{}")
            } catch (error) {
                prepared = null
            }
            let addition = (prepared && typeof prepared.addition === "string") ? prepared.addition : ""
            let revision = prepared ? root.quickValidRevision(prepared.revision) : ""
            if (!prepared || !addition || !revision) {
                root.quickError = "Journal prepare returned invalid data"
                return false
            }
            root.quickTarget = "journal"
            root.quickTargetName = "journal " + root.quickDate
            root.quickRevision = revision
            root.quickAddition = addition
            root.quickPreview = addition
            root.quickConfirming = true
            root.quickError = ""
            return true
        }
        if (stage === "apply") {
            if (code !== 0) {
                let message = root.quickSingleLineError(stderrText, "TODO write failed")
                if (/stale|revision|changed/i.test(message)) {
                    // Revision recheck fired: drop the preview so Enter
                    // re-prepares against the fresh revision.
                    root.quickConfirming = false
                    root.quickApplyError = ""
                    root.quickError = "Page changed; press Enter to retry"
                } else {
                    root.quickApplyError = message
                }
                return false
            }
            let targetName = root.quickTargetName || (root.quickTarget === "journal" ? "today's journal" : "project page")
            root.quickConfirming = false
            root.quickApplyError = ""
            root.quickNotice = "TODO added to " + targetName
            try {
                quickAddField.text = ""
            } catch (error) {}
            root.quickText = ""
            root.quickRequestText = ""
            // Serial entry: stay open with a cleared input. Refresh the
            // picker so the new open task appears.
            if (root.agenda && !root.agenda.agendaBusy && !root.agenda.completionSaving) {
                try {
                    root.agenda.reload()
                } catch (error) {}
            }
            return true
        }
        return false
    }

    function quickConfirmApply() {
        if (!root.quickConfirming || root.quickBusy) return false
        let current = ""
        try {
            current = String(quickAddField.text || "").trim()
        } catch (error) {
            current = ""
        }
        // Exact-preview discipline: the confirmed text must still match
        // the input; a changed input needs a fresh prepare.
        if (current !== root.quickRequestText) {
            root.quickConfirming = false
            root.quickError = "Text changed; press Enter to prepare again"
            return false
        }
        if (root.quickTarget === "page") {
            if (!root.quickProjectId || !root.quickRevision || !root.quickContent) {
                root.quickConfirming = false
                root.quickError = "Preview expired; press Enter to prepare again"
                return false
            }
            return root.quickLaunch("apply",
                ["python3", Quickshell.shellPath("scripts/project_planner.py"), "update"],
                ({ project_id: root.quickProjectId, revision: root.quickRevision,
                   content: root.quickContent }))
        }
        if (root.quickTarget === "journal") {
            if (!root.quickDate || !root.quickRevision || !root.quickAddition) {
                root.quickConfirming = false
                root.quickError = "Preview expired; press Enter to prepare again"
                return false
            }
            return root.quickLaunch("apply",
                ["python3", Quickshell.shellPath("scripts/journal_assistant.py"), "append"],
                ({ date: root.quickDate, revision: root.quickRevision,
                   addition: root.quickAddition }))
        }
        root.quickConfirming = false
        return false
    }

    function quickCancelConfirm() {
        root.quickConfirming = false
        root.quickApplyError = ""
        try {
            quickAddField.forceActiveFocus()
        } catch (error) {}
    }

    function cancelQuickStage() {
        if (!root.quickBusy) return false
        if (root.quickStage === "apply") {
            // A write is never killed on a wall-clock timeout: it may
            // already have committed. Warn and keep waiting for the
            // authoritative helper response.
            root.quickApplyError = "TODO write is taking longer than expected; it will not be cancelled."
            return false
        }
        root.quickBusy = false
        root.quickGeneration++
        if (quickProcess.running) {
            try {
                quickProcess.running = false
            } catch (error) {}
        }
        root.quickError = "TODO prepare timed out; retry"
        return true
    }

    function handleQuickRunningChanged() {
        if (quickProcess.running) return false
        if (!root.quickBusy || root.quickStarted) return false
        quickWatchdog.stop()
        root.quickBusy = false
        root.quickApplying = false
        root.quickStarted = false
        root.quickError = "TODO helper failed to start"
        return true
    }

    // Header: title + selected day + refresh. Card header rule (P4):
    // Theme.text bold title, optional date context, actions as
    // WidgetButtons showing Loading… while busy.
    RowLayout {
        Layout.fillWidth: true
        spacing: 8
        Text {
            text: "Daily planner"
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: 17
            font.bold: true
            Layout.fillWidth: true
            elide: Text.ElideRight
        }
        Text {
            visible: !!root.agenda
            text: root.agenda ? String(root.agenda.selectedDate) : ""
            color: Theme.subtext0
            font.family: Theme.fontFamily
            font.pixelSize: 12
        }
        WidgetButton {
            text: root.agenda && root.agenda.agendaBusy ? "Loading…" : "Refresh"
            enabled: !!root.agenda && !root.agenda.agendaBusy && !root.agenda.agendaRetiring && !root.agenda.completionSaving
            Accessible.name: "Refresh daily agenda"
            onClicked: root.agenda.reload()
        }
    }

    // Calendar navigation.
    RowLayout {
        Layout.fillWidth: true
        spacing: 8
        WidgetButton {
            text: "‹"
            Accessible.name: "Previous month"
            onClicked: root.shiftMonth(-1)
        }
        Text {
            text: root.agenda ? root.agenda.monthLabel(root.viewYear, root.viewMonth) : ""
            color: Theme.text
            font.family: Theme.fontFamily
            font.bold: true
            Layout.fillWidth: true
            horizontalAlignment: Text.AlignHCenter
            elide: Text.ElideRight
        }
        WidgetButton {
            text: "Today"
            Accessible.name: "Go to today"
            onClicked: root.goToday()
        }
        WidgetIconButton {
            text: "Sync view"
            iconSource: "icons/history.svg"
            tooltipText: "Sync calendar to selected day"
            Accessible.name: "Sync calendar to selected day"
            onClicked: root.syncViewToDate()
        }
        WidgetButton {
            text: "›"
            Accessible.name: "Next month"
            onClicked: root.shiftMonth(1)
        }
    }

    // Weekday header (Monday start, matching monthGrid).
    RowLayout {
        Layout.fillWidth: true
        spacing: 4
        Repeater {
            model: root.weekdayNames()
            Text {
                text: modelData
                color: Theme.subtext0
                font.family: Theme.fontFamily
                font.pixelSize: 11
                Layout.fillWidth: true
                horizontalAlignment: Text.AlignHCenter
            }
        }
    }

    // Month grid: 42 clickable days.
    GridLayout {
        Layout.fillWidth: true
        columns: 7
        rowSpacing: 4
        columnSpacing: 4
        Repeater {
            model: root.agenda ? root.agenda.monthGrid(root.viewYear, root.viewMonth) : []
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 32
                radius: Theme.controlRadius
                border.width: 1
                border.color: modelData.iso === (root.agenda ? root.agenda.selectedDate : "") ? Theme.focusBorder : Theme.border
                color: modelData.iso === (root.agenda ? root.agenda.todayIso() : "") ? Theme.surface1
                    : (modelData.inMonth ? Theme.mantle : Theme.base)
                opacity: modelData.inMonth ? 1.0 : 0.6
                Accessible.name: modelData.iso
                Text {
                    anchors.centerIn: parent
                    text: String(modelData.d)
                    color: modelData.iso === (root.agenda ? root.agenda.selectedDate : "") ? Theme.text
                        : (modelData.inMonth ? Theme.text : Theme.subtext0)
                    font.family: Theme.fontFamily
                    font.bold: modelData.iso === (root.agenda ? root.agenda.selectedDate : "")
                }
                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        if (root.agenda) {
                            if (root.agenda.setSelectedDate(modelData.iso)) root.syncViewToDate()
                        }
                    }
                }
            }
        }
    }

    // Pomodoro timer (state lives in the shared agenda so closing the
    // popup never stops the wall-clock deadline).
    Rectangle {
        Layout.fillWidth: true
        color: Theme.mantle
        radius: Theme.controlRadius
        border.color: Theme.border
        border.width: 1
        implicitHeight: pomoRow.implicitHeight + 18
        RowLayout {
            id: pomoRow
            anchors.fill: parent
            anchors.margins: 9
            spacing: 8
            Text {
                text: "Pomodoro"
                color: Theme.accentMuted
                font.family: Theme.fontFamily
                font.bold: true
            }
            Text {
                visible: !!root.agenda
                text: root.agenda
                    ? ((root.agenda.pomoPhase === "idle" ? "idle" : root.agenda.pomoPhase) + " " + root.agenda.pomoFormatted())
                    : ""
                color: Theme.text
                font.family: Theme.fontFamily
                Accessible.name: "Pomodoro status"
                Layout.fillWidth: true
                elide: Text.ElideRight
            }
            TextField {
                id: focusField
                enabled: !!root.agenda && !root.agenda.pomoRunning
                text: root.agenda ? String(root.agenda.focusMinutes) : "25"
                maximumLength: 3
                font.family: Theme.fontFamily
                color: Theme.text
                placeholderTextColor: Theme.subtext0
                Accessible.name: "Focus minutes"
                Layout.preferredWidth: 52
                inputMethodHints: Qt.ImhDigitsOnly
                onEditingFinished: {
                    if (!root.agenda) return
                    let v = parseInt(text, 10)
                    if (v >= 1 && v <= 180) root.agenda.focusMinutes = v
                    else text = String(root.agenda.focusMinutes)
                }
                background: Rectangle { color: Theme.base; radius: Theme.controlRadius; border.color: focusField.activeFocus ? Theme.focusBorder : Theme.border }
            }
            Text {
                text: "/"
                color: Theme.subtext0
                font.family: Theme.fontFamily
            }
            TextField {
                id: breakField
                enabled: !!root.agenda && !root.agenda.pomoRunning
                text: root.agenda ? String(root.agenda.breakMinutes) : "5"
                maximumLength: 3
                font.family: Theme.fontFamily
                color: Theme.text
                placeholderTextColor: Theme.subtext0
                Accessible.name: "Break minutes"
                Layout.preferredWidth: 52
                inputMethodHints: Qt.ImhDigitsOnly
                onEditingFinished: {
                    if (!root.agenda) return
                    let v = parseInt(text, 10)
                    if (v >= 1 && v <= 180) root.agenda.breakMinutes = v
                    else text = String(root.agenda.breakMinutes)
                }
                background: Rectangle { color: Theme.base; radius: Theme.controlRadius; border.color: breakField.activeFocus ? Theme.focusBorder : Theme.border }
            }
            WidgetIconButton {
                text: root.agenda && root.agenda.pomoRunning ? "Pause" : "Start"
                iconSource: root.agenda && root.agenda.pomoRunning ? "icons/pause.svg" : "icons/play.svg"
                tooltipText: root.agenda && root.agenda.pomoRunning ? "Pause pomodoro" : "Start pomodoro"
                enabled: !!root.agenda && !root.agenda.completionSaving
                Accessible.name: "Start or pause pomodoro"
                onClicked: {
                    if (!root.agenda) return
                    if (root.agenda.pomoRunning) root.agenda.pomoPause()
                    else root.agenda.pomoStart()
                }
            }
            WidgetIconButton {
                text: "Reset"
                iconSource: "icons/reset.svg"
                tooltipText: "Reset pomodoro"
                enabled: !!root.agenda && (root.agenda.pomoPhase !== "idle" || root.agenda.pomoRunning || root.agenda.pomoMessage !== "")
                Accessible.name: "Reset pomodoro"
                onClicked: { if (root.agenda) root.agenda.pomoReset() }
            }
        }
    }

    Text {
        visible: !!root.agenda && root.agenda.pomoMessage !== ""
        text: root.agenda ? String(root.agenda.pomoMessage) : ""
        color: Theme.subtext1
        font.family: Theme.fontFamily
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    Text {
        visible: !!root.agenda && root.agenda.pomoCompleted > 0
        text: root.agenda ? (root.agenda.pomoCompleted + (root.agenda.pomoCompleted === 1 ? " focus session done" : " focus sessions done")) : ""
        color: Theme.subtext0
        font.family: Theme.fontFamily
        Layout.fillWidth: true
    }

    // Scheduled for the selected day (open and done).
    Text {
        text: "Scheduled"
        color: Theme.accentMuted
        font.family: Theme.fontFamily
        font.bold: true
        Layout.fillWidth: true
    }

    ListView {
        id: scheduledList
        Layout.fillWidth: true
        Layout.preferredHeight: 168
        clip: true
        visible: !!root.agenda
        model: root.agenda ? root.agenda.agendaItems() : []
        spacing: 4
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
        delegate: Rectangle {
            width: scheduledList.width
            height: 64
            radius: Theme.controlRadius
            color: modelData.done ? Theme.surface0 : Theme.mantle
            border.color: Theme.border
            border.width: 1
            RowLayout {
                anchors.fill: parent
                anchors.margins: 8
                spacing: 8
                Rectangle {
                    width: 22; height: 22; radius: Theme.chipRadius
                    color: modelData.done ? Theme.accent : Theme.mantle
                    border.color: modelData.done ? Theme.accent : Theme.subtext0
                    Text { anchors.centerIn: parent; text: modelData.done ? "✓" : ""; color: Theme.bg; font.bold: true }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: { if (root.agenda) root.agenda.toggleEntry(modelData) }
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 1
                    Text {
                        text: String(modelData.task || "(empty task)")
                        color: modelData.done ? Theme.subtext1 : Theme.text
                        font.family: Theme.fontFamily
                        textFormat: Text.PlainText
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                    Text {
                        text: String(modelData.page || "") + " · line " + String(modelData.line || "")
                        color: Theme.subtext0
                        font.family: Theme.fontFamily
                        font.pixelSize: 11
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                }
                WidgetButton {
                    // While a draft is open the same explicit row becomes an
                    // explicit Reselect that keeps the note and retargets to
                    // the refreshed entry. No silent note loss, no automatic
                    // text/revision matching.
                    text: root.agenda && root.agenda.completionTask !== null ? "Reselect" : "Finish…"
                    enabled: !!root.agenda && !root.agenda.agendaBusy && !root.agenda.completionSaving && !modelData.done
                    Accessible.name: (root.agenda && root.agenda.completionTask !== null
                        ? "Reselect for completion keeping note: " : "Finish with note: ") + String(modelData.task || "task")
                    onClicked: {
                        if (!root.agenda) return
                        if (root.agenda.completionTask !== null) root.agenda.reselectCompletion(modelData)
                        else root.agenda.beginCompletion(modelData)
                    }
                }
                WidgetButton {
                    text: "Remove"
                    enabled: !!root.agenda && !root.agenda.agendaBusy && !root.agenda.completionSaving
                    Accessible.name: "Remove from day: " + String(modelData.task || "task")
                    onClicked: { if (root.agenda) root.agenda.selectEntry(modelData, false) }
                }
            }
        }
    }

    Text {
        visible: !!root.agenda && root.agenda.agendaItems().length === 0
        text: "Nothing scheduled for this day yet. Pick open tasks below."
        color: Theme.subtext0
        font.family: Theme.fontFamily
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    // Completion editor with a frozen snapshot: the note can only be
    // saved to the task it was opened for.
    Rectangle {
        visible: !!root.agenda && root.agenda.completionTask !== null
        Layout.fillWidth: true
        color: Theme.mantle
        radius: Theme.controlRadius
        border.color: Theme.focusBorder
        border.width: 1
        implicitHeight: completionCol.implicitHeight + 24
        ColumnLayout {
            id: completionCol
            anchors.fill: parent
            anchors.margins: 12
            spacing: 8
            Text {
                text: root.agenda && root.agenda.completionTask
                    ? ("Finish: " + String(root.agenda.completionTask.task || "(empty task)"))
                    : "Finish"
                color: Theme.text
                font.family: Theme.fontFamily
                font.bold: true
                textFormat: Text.PlainText
                elide: Text.ElideRight
                Layout.fillWidth: true
            }
            Text {
                visible: root.agenda && root.agenda.completionTask !== null
                text: root.agenda && root.agenda.completionTask
                    ? (String(root.agenda.completionTask.page || "") + " · line " + String(root.agenda.completionTask.line || ""))
                    : ""
                color: Theme.subtext0
                font.family: Theme.fontFamily
                font.pixelSize: 11
                Layout.fillWidth: true
            }
            TextArea {
                id: completionNote
                Layout.fillWidth: true
                Layout.preferredHeight: 72
                enabled: !!root.agenda && !root.agenda.completionSaving
                text: root.agenda ? String(root.agenda.completionNote || "") : ""
                placeholderText: "Progress note (required)…"
                color: Theme.text
                font.family: Theme.fontFamily
                textFormat: TextEdit.PlainText
                wrapMode: TextArea.Wrap
                selectByMouse: true
                Accessible.name: "Progress note"
                onTextChanged: {
                    if (root.agenda && text !== String(root.agenda.completionNote || "")) root.agenda.completionNote = text
                }
                background: Rectangle { color: Theme.base; radius: Theme.controlRadius; border.color: completionNote.activeFocus ? Theme.focusBorder : Theme.border }
            }
            Text {
                visible: !!root.agenda && root.agenda.completionError !== ""
                text: root.agenda ? String(root.agenda.completionError) : ""
                color: Theme.red
                font.family: Theme.fontFamily
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            Text {
                visible: !!root.agenda && root.agenda.completionTask !== null && !root.agenda.isCompletionStale()
                text: "Your note stays with this task until Cancel or save. To switch tasks, Cancel first, then Finish the other task."
                color: Theme.subtext0
                font.family: Theme.fontFamily
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            Text {
                visible: !!root.agenda && root.agenda.completionTask !== null && root.agenda.isCompletionStale()
                text: "The page changed elsewhere. Refresh, then press Reselect on the current task below — your note is preserved."
                color: Theme.subtext1
                font.family: Theme.fontFamily
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Item { Layout.fillWidth: true }
                WidgetButton {
                    text: "Cancel"
                    enabled: !!root.agenda && !root.agenda.completionSaving
                    Accessible.name: "Cancel completion"
                    onClicked: { if (root.agenda) root.agenda.cancelCompletion() }
                }
                WidgetButton {
                    text: root.agenda && root.agenda.completionSaving ? "Saving…" : "Complete & save"
                    enabled: !!root.agenda && !root.agenda.completionSaving && !root.agenda.agendaBusy
                        && String(root.agenda.completionNote || "").trim() !== ""
                    Accessible.name: "Complete and save with note"
                    onClicked: { if (root.agenda) root.agenda.saveCompletion() }
                }
            }
        }
    }

    // Captured inbox (planner Daily tab only, §6.3: hidden in the
    // compact CalendarPopout; the full card set lives in one component
    // and differs by the compact flag).
    CaptureInbox {
        Layout.fillWidth: true
        visible: !root.compact && root.agenda !== null
        agenda: root.agenda
    }

    // Evening review / morning plan (planner Daily tab only, §6.3).
    ReviewCard {
        Layout.fillWidth: true
        visible: !root.compact && root.agenda !== null
        agenda: root.agenda
    }

    // Session card (planner Daily tab only, §6.3).
    SessionCard {
        Layout.fillWidth: true
        visible: !root.compact && root.agenda !== null
        agenda: root.agenda
        onOpenProjectRequested: (projectId) => root.projectPlanningRequested(projectId, "", "")
    }

    // Picker: all open project tasks + search.
    Text {
        text: "Add open tasks"
        color: Theme.accentMuted
        font.family: Theme.fontFamily
        font.bold: true
        Layout.fillWidth: true
    }

    TextField {
        id: pickerSearch
        Layout.fillWidth: true
        placeholderText: "Search open tasks"
        text: root.pickerFilter
        color: Theme.text
        font.family: Theme.fontFamily
        placeholderTextColor: Theme.subtext0
        Accessible.name: "Search open tasks"
        onTextChanged: root.pickerFilter = text
        background: Rectangle { color: Theme.base; radius: Theme.controlRadius; border.color: pickerSearch.activeFocus ? Theme.focusBorder : Theme.border }
    }

    ListView {
        id: pickerList
        Layout.fillWidth: true
        Layout.preferredHeight: 168
        clip: true
        visible: !!root.agenda
        model: root.agenda ? root.agenda.pickerItems(root.pickerFilter) : []
        spacing: 4
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
        delegate: Rectangle {
            width: pickerList.width
            height: 56
            radius: Theme.controlRadius
            color: Theme.mantle
            border.color: Theme.border
            border.width: 1
            RowLayout {
                anchors.fill: parent
                anchors.margins: 8
                spacing: 8
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 1
                    Text {
                        text: String(modelData.task || "(empty task)")
                        color: Theme.text
                        font.family: Theme.fontFamily
                        textFormat: Text.PlainText
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                    Text {
                        text: String(modelData.page || "") + (modelData.scheduledDate ? (" · " + String(modelData.scheduledDate)) : " · unscheduled")
                        color: Theme.subtext0
                        font.family: Theme.fontFamily
                        font.pixelSize: 11
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                }
                WidgetButton {
                    // Scheduled elsewhere shows its date; the button moves it
                    // to the selected day.
                    text: modelData.scheduledDate === (root.agenda ? root.agenda.selectedDate : "") ? "Scheduled ✓" : "Add to day"
                    enabled: !!root.agenda && !root.agenda.agendaBusy && !root.agenda.completionSaving
                        && modelData.scheduledDate !== (root.agenda ? root.agenda.selectedDate : "")
                    Accessible.name: "Schedule for the selected day: " + String(modelData.task || "task")
                    onClicked: { if (root.agenda) root.agenda.selectEntry(modelData, true) }
                }
            }
        }
    }

    Text {
        visible: !!root.agenda && root.agenda.agendaTruncated
        text: "The task list was capped; scheduled tasks are shown first."
        color: Theme.subtext0
        font.family: Theme.fontFamily
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    Text {
        visible: !!root.agenda && root.agenda.agendaError !== ""
        text: root.agenda ? String(root.agenda.agendaError) : ""
        color: Theme.red
        font.family: Theme.fontFamily
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    Text {
        visible: !!root.agenda && root.agenda.agendaNotice !== ""
        text: root.agenda ? String(root.agenda.agendaNotice) : ""
        color: Theme.subtext1
        font.family: Theme.fontFamily
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    // Quick-add TODO (compact CalendarPopout only, §6.3): bare `todo:`
    // input routing into the current project page, else today's journal.
    // Prepare → exact preview → Confirm; the write lands only on
    // Confirm. Enter starts prepare, never confirms.
    Text {
        visible: root.compact
        text: "Quick-add TODO"
        color: Theme.accentMuted
        font.family: Theme.fontFamily
        font.bold: true
        Layout.fillWidth: true
    }

    RowLayout {
        visible: root.compact
        Layout.fillWidth: true
        spacing: 8
        TextField {
            id: quickAddField
            Layout.fillWidth: true
            placeholderText: "todo: Type a TODO · Enter previews"
            color: Theme.text
            font.family: Theme.fontFamily
            placeholderTextColor: Theme.subtext0
            Accessible.name: "Quick-add TODO"
            Accessible.description: "Type a TODO, Enter previews the exact write, Confirm writes it"
            enabled: !root.quickBusy
            onAccepted: root.quickBegin()
            background: Rectangle { color: Theme.base; radius: Theme.controlRadius; border.color: quickAddField.activeFocus ? Theme.focusBorder : Theme.border }
        }
        WidgetButton {
            text: root.quickBusy ? "Preparing…" : "Add"
            enabled: !root.quickBusy && !root.quickConfirming && String(quickAddField.text || "").trim() !== ""
            Accessible.name: "Preview quick-add TODO"
            onClicked: root.quickBegin()
        }
    }

    Text {
        visible: root.compact && root.quickError !== ""
        text: String(root.quickError)
        color: Theme.red
        font.family: Theme.fontFamily
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    Text {
        visible: root.compact && root.quickNotice !== "" && root.quickError === ""
        text: String(root.quickNotice)
        color: Theme.subtext1
        font.family: Theme.fontFamily
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    PreviewPanel {
        visible: root.compact && root.quickConfirming
        destinationText: String(root.quickTargetName || "")
        bodyText: String(root.quickPreview || "")
        bodyAccessibleName: "Quick-add TODO preview"
        bodyObjectName: "quickAddPreview"
        applying: root.quickApplying
        applyError: String(root.quickApplyError || "")
        confirmEnabled: root.quickConfirming && !root.quickBusy
        confirmObjectName: "quickAddConfirm"
        cancelObjectName: "quickAddCancel"
        confirmAccessibleName: "Confirm TODO quick-add"
        cancelAccessibleName: "Cancel TODO quick-add"
        onConfirmRequested: root.quickConfirmApply()
        onCancelRequested: root.quickCancelConfirm()
    }

    // Quick-add backend: one serialized Process across the read stages
    // plus the apply stage. List-form argv,
    // workingDirectory shellPath("."), 12 s watchdog; the apply write is
    // never killed on timeout (warn-only). Stdin JSON is written
    // onStarted, then closed.
    Process {
        id: quickProcess
        workingDirectory: Quickshell.shellPath(".")
        stdinEnabled: false
        stdout: StdioCollector { id: quickOutput; waitForEnd: true }
        stderr: StdioCollector { id: quickErrorOut; waitForEnd: true }
        onStarted: {
            root.quickStarted = true
            if (root.quickPayload !== null && root.quickPayload !== undefined) {
                try {
                    quickProcess.write(JSON.stringify(root.quickPayload) + "\n")
                } catch (error) {}
            }
            try {
                if (quickProcess.closeWriteChannel) quickProcess.closeWriteChannel()
            } catch (error) {}
            quickProcess.stdinEnabled = false
        }
        onRunningChanged: root.handleQuickRunningChanged()
        onExited: (code) => {
            let failed = root.quickStartFailed
            let generation = root.quickProcessGeneration
            let stage = root.quickStage
            root.quickStarted = false
            root.quickStartFailed = false
            if (failed) {
                quickWatchdog.stop()
                root.quickBusy = false
                root.quickApplying = false
                root.quickError = "TODO helper failed to start"
            } else {
                root.finishQuickStage(code, quickOutput.text, quickErrorOut.text, generation, stage)
            }
        }
    }

    Timer {
        id: quickWatchdog
        interval: 12000
        repeat: false
        onTriggered: root.cancelQuickStage()
    }
}
