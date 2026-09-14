import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "../theme"

// Reusable daily-planner UI bound to a shared DailyAgenda state object.
// Used both by the clock CalendarPopout and the ProjectPlanner Daily tab,
// so the date, drafts, and Pomodoro stay identical in both surfaces.
ColumnLayout {
    id: root

    property var agenda: null
    property string pickerFilter: ""
    property int viewYear: 2026
    property int viewMonth: 8

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

    // Header: title + selected day + reload.
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
            text: root.agenda && root.agenda.agendaBusy ? "Loading…" : "Reload"
            enabled: !!root.agenda && !root.agenda.agendaBusy && !root.agenda.agendaRetiring && !root.agenda.completionSaving
            Accessible.name: "Reload daily agenda"
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
                    width: 22; height: 22; radius: 6
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
                text: "The page changed elsewhere. Reload, then press Reselect on the current task below — your note is preserved."
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
}
