import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "../theme"

// Captured inbox card bound to the shared DailyAgenda state object.
// Presentation only: all backend work lives in DailyAgenda (list,
// prepare, apply, dismiss, scanNow). Hidden/no-op when agenda is null.
// No direct file access, no pop-up errors; errors/notices are bounded
// single-line text.
ColumnLayout {
    id: root

    property var agenda: null

    spacing: 8
    visible: !!root.agenda

    onAgendaChanged: {
        if (root.agenda && root.agenda.reloadCaptures && visible) root.agenda.reloadCaptures()
    }
    onVisibleChanged: {
        if (visible && root.agenda && root.agenda.reloadCaptures) root.agenda.reloadCaptures()
    }
    Component.onCompleted: {
        if (root.agenda && root.agenda.reloadCaptures && visible) root.agenda.reloadCaptures()
    }

    // Header: title + count + Refresh + Scan now. Card header rule
    // (P4): Theme.text bold title with the count in parens, actions as
    // WidgetButtons showing Loading… while busy; Scan now stays an
    // icon-button supplemental action with its own tooltip.
    RowLayout {
        Layout.fillWidth: true
        spacing: 8
        Text {
            text: "Captured" + (root.agenda && root.agenda.captureItems ? " (" + root.agenda.captureItems.length + ")" : "")
            color: Theme.text
            font.family: Theme.fontFamily
            font.bold: true
            Layout.fillWidth: true
            elide: Text.ElideRight
        }
        WidgetButton {
            objectName: "captureRefreshButton"
            text: root.agenda && root.agenda.captureBusy ? "Loading…" : "Refresh"
            enabled: !!root.agenda && !root.agenda.captureBusy && !root.agenda.captureApplying
                && !(root.agenda.scheduler && root.agenda.scheduler.scanBusy)
            Accessible.name: "Refresh captures"
            Accessible.description: "Reload the captured TODOs for the selected day"
            onClicked: {
                if (root.agenda && root.agenda.reloadCaptures) root.agenda.reloadCaptures()
            }
        }
        WidgetIconButton {
            objectName: "captureScanButton"
            text: "Scan now"
            iconSource: "icons/history.svg"
            tooltipText: "Scan for new captures"
            Accessible.name: "Scan now"
            Accessible.description: "Scan for new captures now"
            enabled: !!root.agenda && !root.agenda.captureBusy
                && !(root.agenda.scheduler && root.agenda.scheduler.scanBusy)
            onClicked: {
                if (root.agenda && root.agenda.scanNow) root.agenda.scanNow()
            }
        }
    }

    Text {
        visible: !!root.agenda && String(root.agenda.captureError || "") !== ""
        text: root.agenda ? String(root.agenda.captureError || "") : ""
        color: Theme.red
        font.family: Theme.fontFamily
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    Text {
        visible: !!root.agenda && String(root.agenda.captureNotice || "") !== ""
        text: root.agenda ? String(root.agenda.captureNotice || "") : ""
        color: Theme.subtext1
        font.family: Theme.fontFamily
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    Text {
        visible: !!root.agenda && !!root.agenda.scheduler && String(root.agenda.scheduler.scanNotice || "") !== ""
        text: root.agenda && root.agenda.scheduler ? String(root.agenda.scheduler.scanNotice || "") : ""
        color: Theme.subtext0
        font.family: Theme.fontFamily
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    // One truncation pattern (S-041): "showing X of Y", silent
    // unless the day list was actually capped (backend total > shown).
    function truncationText() {
        let items = root.agenda && Array.isArray(root.agenda.captureItems)
            ? root.agenda.captureItems : []
        let shown = Math.min(items.length, 20)
        let total = 0
        try {
            total = Number(root.agenda ? root.agenda.captureTotal : 0)
        } catch (error) {
            total = 0
        }
        if (!(typeof total === "number" && isFinite(total) && total > 0))
            total = shown
        if (total > shown) return "showing " + shown + " of " + Math.floor(total)
        return ""
    }

    ListView {
        id: captureList
        Layout.fillWidth: true
        Layout.preferredHeight: 148
        clip: true
        visible: !!root.agenda
        model: root.agenda && root.agenda.captureItems ? root.agenda.captureItems.slice(0, 20) : []
        spacing: 4
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
        delegate: Rectangle {
            width: captureList.width
            height: 64
            radius: Theme.controlRadius
            color: Theme.mantle
            border.color: Theme.border
            border.width: 1
            // Per-row index/id available to handlers via modelData + index.
            property int rowIndex: index
            property var rowId: modelData.id
            RowLayout {
                anchors.fill: parent
                anchors.margins: 8
                spacing: 8
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 1
                    Text {
                        text: String(modelData.kind || "todo")
                        color: Theme.subtext0
                        font.family: Theme.fontFamily
                        font.pixelSize: 11
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                    Text {
                        text: String(modelData.text || "")
                        color: Theme.text
                        font.family: Theme.fontFamily
                        textFormat: Text.PlainText
                        elide: Text.ElideRight
                        maximumLineCount: 2
                        wrapMode: Text.NoWrap
                        Layout.fillWidth: true
                    }
                }
                WidgetButton {
                    objectName: "captureAddPage"
                    text: "Accept"
                    enabled: !!root.agenda && !root.agenda.captureBusy && !root.agenda.captureApplying
                    Accessible.name: "Accept capture: " + String(modelData.text || "capture") + " [" + index + "]"
                    Accessible.description: "File this capture to the project page with an exact preview"
                    onClicked: {
                        // No session context at this surface yet (the
                        // session-scoped SessionCard arrives in Phase 2b):
                        // the backend files a bare block, never blocked.
                        if (root.agenda && root.agenda.prepareCapture) root.agenda.prepareCapture(modelData.id)
                    }
                }
                WidgetButton {
                    objectName: "captureDismiss"
                    text: "Dismiss"
                    enabled: !!root.agenda && !root.agenda.captureBusy && !root.agenda.captureApplying
                    Accessible.name: "Dismiss capture: " + String(modelData.text || "capture") + " [" + index + "]"
                    Accessible.description: "Remove this capture without writing to Logseq"
                    onClicked: {
                        if (root.agenda && root.agenda.dismissCapture) root.agenda.dismissCapture(modelData.id)
                    }
                }
            }
        }
    }

    Text {
        objectName: "captureTruncation"
        visible: !!root.agenda && root.truncationText() !== ""
        text: root.truncationText()
        color: Theme.subtext0
        font.family: Theme.fontFamily
        font.pixelSize: 11
        elide: Text.ElideRight
        Layout.fillWidth: true
    }

    Text {
        visible: !!root.agenda && (!root.agenda.captureItems || root.agenda.captureItems.length === 0) && !root.agenda.captureBusy
        text: "Nothing captured for this day yet. Press Scan now to look for new activity."
        color: Theme.subtext0
        font.family: Theme.fontFamily
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    // Exact-preview panel: destination + exact block + Confirm/Cancel
    // (shared PreviewPanel: destination + revision + bounded readonly
    // block + Confirm/Cancel).
    PreviewPanel {
        visible: !!root.agenda && !!root.agenda.capturePreview
        destinationText: {
            if (!root.agenda || !root.agenda.capturePreview) return ""
            let preview = root.agenda.capturePreview
            return String(preview.page || "") + " · " + String(preview.path || "")
        }
        bodyText: root.agenda && root.agenda.capturePreview ? String(root.agenda.capturePreview.block || "") : ""
        bodyAccessibleName: "Capture preview"
        applying: !!root.agenda && !!root.agenda.captureApplying
        applyError: root.agenda ? String(root.agenda.captureApplyError || "") : ""
        confirmEnabled: !!root.agenda && !!root.agenda.capturePreview && !!root.agenda.capturePreviewToken && !root.agenda.captureApplying && !root.agenda.captureBusy
        confirmObjectName: "captureConfirm"
        cancelObjectName: "captureCancel"
        confirmAccessibleName: "Confirm capture save"
        cancelAccessibleName: "Cancel capture preview"
        onConfirmRequested: { if (root.agenda && root.agenda.applyCapture) root.agenda.applyCapture() }
        onCancelRequested: { if (root.agenda && root.agenda.cancelCapturePreview) root.agenda.cancelCapturePreview() }
    }
}
