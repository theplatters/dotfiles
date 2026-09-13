/*
 * PaletteApprovalDialog.qml — agent approval overlay for the command palette.
 *
 * Responsibility: own the approval FocusScope UI and its response logic.
 * The palette never parses approval payloads; this dialog does.
 *
 * Owned state/processes:
 * - request (var, pending RPC request object), choiceIndex (int, select cursor).
 * - No Process/Timer. responseInput/choiceList/acceptButton are internal ids.
 *
 * Contract with CommandPalette:
 * - In: `property var agent` (ScopedAgent, for respond), `property var request`,
 *   `property bool paletteVisible/paletteOpen/paletteClosing` (mirror the
 *   palette PanelWindow visible/requestedOpen/closing for the visible gate),
 *   `property var paletteInput` (palette TextField for focus restore).
 * - Palette routes agent onUiRequest/open() into openRequest(); routes
 *   null-request into close(). Dialog calls paletteInput.forceActiveFocus()
 *   on dismiss/restore; no other palette writes.
 * - dismiss() stays view-only: it clears the local view and never responds
 *   to RPC, so closing the palette leaves the request pending. reject()
 *   responds cancelled, accept() responds confirm/select/input branches.
 *
 * Generation/staleness: N/A (single pending request; openRequest replaces it).
 *
 * No new controls were added; Reject/Accept keep the existing Button style.
 */
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "../theme"

FocusScope {
    id: root

    property var agent
    property var request: null
    property int choiceIndex: 0
    property bool paletteVisible: false
    property bool paletteOpen: false
    property bool paletteClosing: false
    property var paletteInput

    z: 100
    anchors.fill: parent
    visible: root.paletteVisible && root.paletteOpen && !root.paletteClosing && root.request !== null

    function openRequest(value) {
        if (!value) return;
        request = value;
        choiceIndex = 0;
        responseInput.text = value.prefill ? String(value.prefill) : "";
        Qt.callLater(focusRequest);
    }

    // Dismiss only removes the local view.  It deliberately does not send
    // a response: closing the palette must leave the RPC request pending.
    function dismiss() {
        request = null;
        choiceIndex = 0;
        responseInput.text = "";
        Qt.callLater(restoreFocus);
    }

    function close() { dismiss(); }

    function focusRequest() {
        if (!request) return;
        if (request.method === "select") choiceList.forceActiveFocus();
        else if (request.method === "input" || request.method === "editor") responseInput.forceActiveFocus();
        else acceptButton.forceActiveFocus();
    }

    function restoreFocus() {
        if (request) focusRequest();
        else if (root.paletteOpen && !root.paletteClosing && root.paletteInput) root.paletteInput.forceActiveFocus();
    }

    function titleFor(value) {
        if (!value) return "Approval";
        if (value.method === "select") return "Choose a session";
        if (value.method === "confirm") return "Confirm request";
        if (value.method === "editor") return "Edit response";
        return "Enter response";
    }

    function messageFor(value) {
        if (!value) return "";
        return value.message || value.title || value.prompt ||
            "Pi is requesting an explicit response.";
    }

    function argumentPreview(value) {
        if (!value) return "";
        let args = value.arguments !== undefined ? value.arguments :
            (value.input !== undefined ? value.input : value);
        try { return JSON.stringify(args, null, 2); }
        catch (error) { return String(args); }
    }

    function moveChoice(delta) {
        if (!request || request.method !== "select") return;
        let options = request.options || [];
        choiceIndex = Math.max(0, Math.min(options.length - 1, choiceIndex + delta));
        choiceList.positionViewAtIndex(choiceIndex, ListView.Contain);
    }

    function reject() {
        if (!request) return;
        let current = request;
        let requestId = current.id;
        dismiss();
        agent.respond(requestId, { cancelled: true });
    }

    function accept() {
        if (!request) return;
        let current = request;
        let value = {};
        if (current.method === "confirm") value.confirmed = true;
        else if (current.method === "select") {
            let options = current.options || [];
            if (!options.length) return;
            value.value = options[choiceIndex] || options[0];
        } else value.value = responseInput.text;
        let requestId = current.id;
        dismiss();
        agent.respond(requestId, value);
    }

    Keys.onPressed: (event) => {
        if (!request) return;
        if (event.key === Qt.Key_Escape) {
            reject();
            event.accepted = true;
        } else if (request.method === "select" && event.key === Qt.Key_Up) {
            moveChoice(-1);
            event.accepted = true;
        } else if (request.method === "select" && event.key === Qt.Key_Down) {
            moveChoice(1);
            event.accepted = true;
        } else if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter) &&
                   (request.method === "select" || request.method === "confirm")) {
            accept();
            event.accepted = true;
        }
    }

    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.AllButtons
    }

    Rectangle {
        id: approvalCard
        anchors.centerIn: parent
        width: Math.min(Math.max(280, parent.width - 32), 720)
        height: Math.min(Math.max(250, parent.height - 32),
            250 + (root.request && root.request.method === "select" ?
                Math.min(280, Math.max(48, (root.request.options || []).length * 42)) :
                (root.request && (root.request.method === "input" || root.request.method === "editor") ? 150 : 0)))
        color: Theme.base
        radius: Theme.cardRadius
        border.color: Theme.focusBorder
        border.width: 1

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 18
            spacing: 10

            Text {
                text: root.titleFor(root.request)
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: 20
                font.bold: true
                Layout.fillWidth: true
            }
            Text {
                text: root.messageFor(root.request)
                color: Theme.subtext1
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            ScrollView {
                Layout.fillWidth: true
                Layout.preferredHeight: 100
                clip: true
                TextArea {
                    readOnly: true
                    text: root.argumentPreview(root.request)
                    textFormat: TextEdit.PlainText
                    wrapMode: TextArea.Wrap
                    selectByMouse: true
                    color: Theme.subtext0
                    background: Rectangle { color: Theme.mantle; radius: Theme.controlRadius; border.color: Theme.border; border.width: 1 }
                }
            }
            ListView {
                id: choiceList
                visible: root.request && root.request.method === "select"
                Layout.fillWidth: true
                Layout.preferredHeight: visible ? Math.min(280, Math.max(48, (root.request.options || []).length * 42)) : 0
                model: root.request && root.request.options ? root.request.options : []
                clip: true
                spacing: 3
                currentIndex: root.choiceIndex
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                delegate: Rectangle {
                    width: choiceList.width
                    height: 39
                    radius: Theme.controlRadius
                    color: index === root.choiceIndex ? Theme.surface1 : Theme.mantle
                    border.color: index === root.choiceIndex ? Theme.focusBorder : Theme.border
                    Text {
                        anchors.fill: parent
                        anchors.margins: 10
                        text: modelData
                        color: Theme.text
                        elide: Text.ElideRight
                        verticalAlignment: Text.AlignVCenter
                    }
                    MouseArea {
                        anchors.fill: parent
                        onClicked: { root.choiceIndex = index; choiceList.forceActiveFocus(); }
                    }
                }
                Keys.onPressed: (event) => {
                    if (event.key === Qt.Key_Up) { root.moveChoice(-1); event.accepted = true; }
                    else if (event.key === Qt.Key_Down) { root.moveChoice(1); event.accepted = true; }
                    else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) { root.accept(); event.accepted = true; }
                    else if (event.key === Qt.Key_Escape) { root.reject(); event.accepted = true; }
                }
            }
            TextArea {
                id: responseInput
                visible: root.request && (root.request.method === "input" || root.request.method === "editor")
                Layout.fillWidth: true
                Layout.preferredHeight: root.request && root.request.method === "editor" ? 150 : 100
                placeholderText: root.request && root.request.placeholder || "Enter a response"
                textFormat: TextEdit.PlainText
                wrapMode: TextArea.Wrap
                selectByMouse: true
                color: Theme.text
                background: Rectangle { color: Theme.mantle; radius: Theme.controlRadius; border.color: responseInput.activeFocus ? Theme.focusBorder : Theme.border; border.width: 1 }
                // Enter intentionally remains a newline for input/editor.
                // The explicit Accept button is the only submit path.
                Keys.onPressed: (event) => {
                    if (event.key === Qt.Key_Escape) { root.reject(); event.accepted = true; }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                Button {
                    text: "Reject"
                    onClicked: root.reject()
                    contentItem: Text { text: parent.text; color: Theme.text; font.family: Theme.fontFamily; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                    background: Rectangle { color: parent.hovered ? Theme.surface1 : Theme.mantle; radius: Theme.controlRadius; border.color: Theme.border }
                }
                Button {
                    id: acceptButton
                    text: "Accept"
                    onClicked: root.accept()
                    contentItem: Text { text: parent.text; color: Theme.text; font.family: Theme.fontFamily; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                    background: Rectangle { color: parent.hovered ? Theme.surface1 : Theme.mantle; radius: Theme.controlRadius; border.color: Theme.border }
                }
            }
        }
    }
}
