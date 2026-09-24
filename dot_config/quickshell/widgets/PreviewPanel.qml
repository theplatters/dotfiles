import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "../theme"

// Shared exact-preview panel (P4 card convention): destination + revision
// line, bounded readonly block, applying/error lines, Cancel/Confirm.
// Used by CaptureInbox, ReviewCard, and SessionCard so every Logseq write
// follows the same prepare -> exact preview -> Confirm shape. Confirm
// stays gated by the caller (preview + single-use token + idle ladder);
// the caller also owns the destination/revision composition.
Rectangle {
    id: root

    property string destinationText: ""
    property string bodyText: ""
    property string bodyAccessibleName: "Preview"
    property string bodyObjectName: ""
    property bool applying: false
    property string applyError: ""
    property bool confirmEnabled: false
    property string confirmObjectName: ""
    property string cancelObjectName: ""
    // Confirm verb per §6.4: "Confirm" by default; file-to-project
    // previews pass "Save to project". Journal/organise previews keep
    // the generic verb since they do not file to a project page.
    property string confirmText: "Confirm"
    property string confirmAccessibleName: "Confirm preview save"
    property string cancelAccessibleName: "Cancel preview"

    signal confirmRequested()
    signal cancelRequested()

    Layout.fillWidth: true
    color: Theme.mantle
    radius: Theme.controlRadius
    border.color: Theme.focusBorder
    border.width: 1
    implicitHeight: previewCol.implicitHeight + 24

    ColumnLayout {
        id: previewCol
        anchors.fill: parent
        anchors.margins: 12
        spacing: 8
        Text {
            text: root.destinationText
            color: Theme.text
            font.family: Theme.fontFamily
            font.bold: true
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
        TextArea {
            objectName: root.bodyObjectName
            Layout.fillWidth: true
            Layout.preferredHeight: 96
            readOnly: true
            text: root.bodyText
            color: Theme.text
            font.family: "monospace"
            textFormat: TextEdit.PlainText
            wrapMode: TextArea.Wrap
            selectByMouse: true
            Accessible.name: root.bodyAccessibleName
            background: Rectangle { color: Theme.base; radius: Theme.controlRadius; border.color: Theme.border }
        }
        Text {
            visible: root.applying
            text: "Applying…"
            color: Theme.subtext1
            font.family: Theme.fontFamily
            Layout.fillWidth: true
        }
        Text {
            visible: root.applyError !== ""
            text: root.applyError
            color: Theme.red
            font.family: Theme.fontFamily
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }
        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            Item { Layout.fillWidth: true }
            WidgetButton {
                objectName: root.cancelObjectName
                text: "Cancel"
                enabled: !root.applying
                Accessible.name: root.cancelAccessibleName
                Accessible.description: "Discard the preview without writing"
                onClicked: root.cancelRequested()
            }
            WidgetButton {
                objectName: root.confirmObjectName
                text: root.applying ? "Applying…" : root.confirmText
                enabled: root.confirmEnabled && !root.applying
                Accessible.name: root.confirmAccessibleName
                onClicked: root.confirmRequested()
            }
        }
    }
}
