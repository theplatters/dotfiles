import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "../theme"

// Attribution hygiene card (§4.4) for the planner Projects tab.
// Presentation only: the folder list and the ephemeral ignore live in
// DailyAgenda (desktop_projects.py unmapped-folders ladder); adding a
// folder to a project goes through the existing projects.py
// create/update form (prefilled local_folder, revision-checked
// registry update) via addToProjectRequested. No inference, no
// auto-create, no writes from this card. Hidden when there is nothing
// to fix (idle, no error, no visible rows).
ColumnLayout {
    id: root

    property var agenda: null

    signal addToProjectRequested(string folder)

    spacing: 8
    visible: !!root.agenda && (root.isBusy() || root.statusText() !== "" || root.visibleRows().length > 0)

    onAgendaChanged: {
        if (root.agenda && root.agenda.reloadUnmapped && visible) root.agenda.reloadUnmapped()
    }
    onVisibleChanged: {
        if (visible && root.agenda && root.agenda.reloadUnmapped) root.agenda.reloadUnmapped()
    }
    Component.onCompleted: {
        if (root.agenda && root.agenda.reloadUnmapped && visible) root.agenda.reloadUnmapped()
    }

    function isBusy() {
        return !!root.agenda && !!root.agenda.unmappedBusy
    }

    function visibleRows() {
        if (!root.agenda || !root.agenda.unmappedVisibleRows) return []
        try {
            let rows = root.agenda.unmappedVisibleRows()
            return Array.isArray(rows) ? rows : []
        } catch (error) { return [] }
    }

    function boundLine(value, limit) {
        let max = Number(limit) > 0 ? Math.floor(Number(limit)) : 120
        let clean = String(value === undefined || value === null ? "" : value)
            .replace(/\s+/g, " ").trim()
        if (clean.length > max) clean = clean.substring(0, max - 1) + "…"
        return clean
    }

    function statusText() {
        if (!root.agenda) return ""
        return String(root.agenda.unmappedError || "")
    }

    function countLabel(count) {
        let n = Number(count)
        if (!(n >= 0)) n = 0
        return n === 1 ? "1 session" : Math.floor(n) + " sessions"
    }

    // Header: title + count + Refresh. Card header rule (P4):
    // Theme.text bold title with the count in parens, actions as
    // WidgetButtons showing Loading… while busy.
    RowLayout {
        Layout.fillWidth: true
        spacing: 8
        Text {
            text: "Unclaimed folders" + (root.visibleRows().length > 0 ? " (" + root.visibleRows().length + ")" : "")
            color: Theme.text
            font.family: Theme.fontFamily
            font.bold: true
            Layout.fillWidth: true
            elide: Text.ElideRight
        }
        WidgetButton {
            objectName: "unmappedRefreshButton"
            text: root.isBusy() ? "Loading…" : "Refresh"
            enabled: !!root.agenda && !root.isBusy() && !root.agenda.unmappedRetiring
            Accessible.name: "Refresh unclaimed folders"
            onClicked: {
                if (root.agenda && root.agenda.reloadUnmapped) root.agenda.reloadUnmapped()
            }
        }
    }

    Text {
        text: "You worked in these folders but no project claims them."
        color: Theme.subtext0
        font.family: Theme.fontFamily
        font.pixelSize: 11
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    Text {
        objectName: "unmappedStatus"
        visible: root.statusText() !== ""
        text: root.statusText()
        color: Theme.red
        font.family: Theme.fontFamily
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    ListView {
        id: unmappedList
        objectName: "unmappedList"
        visible: root.visibleRows().length > 0
        Layout.fillWidth: true
        Layout.preferredHeight: Math.min(180, Math.max(52, root.visibleRows().length * 60))
        clip: true
        model: root.visibleRows()
        spacing: 4
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
        delegate: Rectangle {
            width: unmappedList.width
            implicitHeight: folderCol.implicitHeight + 16
            radius: Theme.controlRadius
            color: Theme.mantle
            border.color: Theme.border
            border.width: 1
            ColumnLayout {
                id: folderCol
                anchors.fill: parent
                anchors.margins: 8
                spacing: 4
                Text {
                    text: root.boundLine(modelData.folder, 120)
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.bold: true
                    textFormat: Text.PlainText
                    elide: Text.ElideMiddle
                    Layout.fillWidth: true
                }
                Text {
                    text: root.countLabel(modelData.observation_count)
                    color: Theme.subtext0
                    font.family: Theme.fontFamily
                    font.pixelSize: 11
                    Layout.fillWidth: true
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    Item { Layout.fillWidth: true }
                    WidgetButton {
                        objectName: "unmappedIgnoreButton"
                        text: "Ignore"
                        enabled: !!root.agenda && !root.isBusy()
                        Accessible.name: "Ignore folder " + root.boundLine(modelData.folder, 60)
                        Accessible.description: "Hide this folder for this session only"
                        onClicked: {
                            if (root.agenda && root.agenda.ignoreUnmapped)
                                root.agenda.ignoreUnmapped(String(modelData.folder || ""))
                        }
                    }
                    WidgetButton {
                        objectName: "unmappedAddButton"
                        text: "Add to project…"
                        enabled: !!root.agenda && !root.isBusy()
                        Accessible.name: "Add folder to project: " + root.boundLine(modelData.folder, 60)
                        Accessible.description: "Open the project form prefilled with this folder"
                        onClicked: root.addToProjectRequested(String(modelData.folder || ""))
                    }
                }
            }
        }
    }
}
