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
 * - dismiss() defers (S-048): it sends deferRequest for the visible
 *   request so the bridge parks it round-robin (skipped while other
 *   requests remain, re-surfaced bridge-side when the round reaches it,
 *   never expiring while parked) and clears only the local view. The
 *   parked id is recorded (deferredIds) so its same-id echo never
 *   force-opens this dialog or the palette again in this surface
 *   session; new ids still surface immediately, and reopening re-shows
 *   whatever is pending via openRequest(agent.pendingApproval). It never
 *   sends an RPC response. reject() responds cancelled, accept()
 *   responds confirm/select/input branches (both via clearView, never
 *   via dismiss, so a failed respond cannot park the request).
 *   Gestures are labelled by effect: "Reject (Esc)" cancels,
 *   "Defer" parks the request queued, and the footer states that
 *   Defer/closing parks it without expiry or auto-reopen while Reject
 *   cancels it and Stop cancels everything.
 * - Preview follows the bounded exact-preview pattern (S-024): specific
 *   titles, a consequence line, a destination/what-will-run line, and a
 *   bounded monospace excerpt. Untrusted text stays bounded; args are
 *   never raw-dumped.
 *
 * Generation/staleness: N/A (single pending request; openRequest replaces it).
 *
 * No new controls were added; Reject/Accept keep the existing Button style.
 */
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "../theme"
import "ApprovalGrammar.js" as ApprovalGrammar

FocusScope {
    id: root

    property var agent
    property var request: null
    property int choiceIndex: 0
    property bool paletteVisible: false
    property bool paletteOpen: false
    property bool paletteClosing: false
    property var paletteInput
    // Same-id suppression for the current surface session: every id
    // parked via dismiss() is recorded here so a bridge round-robin
    // re-surface of that SAME id never force-opens this dialog (or the
    // palette) while the surface stays open. New ids still surface
    // immediately; the set resets on open()/mode change, and reopening
    // re-shows whatever is pending via openRequest(agent.pendingApproval).
    property var deferredIds: ({})

    z: 100
    anchors.fill: parent
    visible: root.paletteVisible && root.paletteOpen && !root.paletteClosing && root.request !== null

    function openRequest(value) {
        if (!value) return;
        request = value;
        choiceIndex = 0;
        responseInput.text = value.prefill ? String(value.prefill) : "";
        // S-048: a dialog opened for this request marks it surfaced, so
        // the bridge never expires it silently. Guarded: test harnesses
        // may install a partial agent without the op.
        try {
            if (agent && typeof agent.surfaceRequest === "function" && value.id)
                agent.surfaceRequest(value.id);
        } catch (error) {}
        Qt.callLater(focusRequest);
    }

    // View-only reset: clears the visible request without parking it.
    // Accept/Reject use this so the respond runs first and a failed
    // respond can never park the request (parking is the Defer/close
    // path's job, below). Defer/close use dismiss(), never this.
    function clearView() {
        request = null;
        choiceIndex = 0;
        responseInput.text = "";
        Qt.callLater(restoreFocus);
    }

    // Dismiss defers (S-048): park the visible request round-robin via
    // deferRequest, then clear only the local view. It deliberately
    // sends no RPC response: closing the palette parks the request
    // queued (never expiring, never reopening on its own this session),
    // and reopening re-shows whatever is pending.
    // Reached only from the Defer button and surface-close paths
    // (close(), compositor unmap); Accept/Reject go through clearView.
    function dismiss() {
        // Capture the id first: clearing the view must not lose the op.
        let deferredId = null;
        try {
            if (request && request.id) deferredId = request.id;
        } catch (error) {
            deferredId = null;
        }
        // Record BEFORE parking: even a failed park must suppress the
        // same-id echo (the request stays queued bridge-side regardless).
        noteDeferred(deferredId);
        clearView();
        // Park AFTER the view is cleared; a failed park never resurrects
        // the dialog (the request stays queued bridge-side regardless).
        try {
            if (deferredId && agent && typeof agent.deferRequest === "function")
                agent.deferRequest(deferredId);
        } catch (error) {}
    }

    // Same-id suppression bookkeeping (see deferredIds): plain var-map
    // copies so QML notifies on replace; reads are null-safe for
    // harness contexts.
    function noteDeferred(requestId) {
        if (!requestId) return;
        try {
            let copy = Object.assign({}, deferredIds);
            copy[requestId] = true;
            deferredIds = copy;
        } catch (error) {}
    }

    function isDeferred(requestId) {
        if (!requestId) return false;
        try {
            return !!deferredIds[requestId];
        } catch (error) {
            return false;
        }
    }

    function resetDeferred() {
        deferredIds = ({});
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

    // Thin wrapper over the shared approval grammar (S-059): the same
    // request derives the same title on every surface. Kept by name —
    // tests pin titleFor here.
    function titleFor(value) {
        return ApprovalGrammar.titleFor(value);
    }


    function messageFor(value) {
        return ApprovalGrammar.messageFor(value);
    }


    // Bound untrusted text: strip controls, collapse whitespace, cap.
    function boundedText(value, limit) {
        return ApprovalGrammar.boundedText(value, limit);
    }


    // Consequence line (S-024/S-059): shared grammar. Exactly true: a
    // deferred request never expires, never reopens on its own while
    // this view stays open, and is still pending when the user comes
    // back to it; only Reject cancels it and Stop cancels everything.
    function consequenceFor(value) {
        return ApprovalGrammar.consequenceFor(value);
    }


    // Destination / what-will-run line (S-024/S-059): shared grammar,
    // bounded, never a raw dump. Passes this dialog's choice cursor.
    function destinationFor(value) {
        return ApprovalGrammar.destinationFor(value, choiceIndex);
    }


    // Bounded exact-preview excerpt (S-024/S-059): shared field-walk,
    // capped for display. Never a raw JSON dump of args.
    function argumentPreview(value) {
        return ApprovalGrammar.argumentPreview(value);
    }


    function moveChoice(delta) {
        if (!request || request.method !== "select") return;
        choiceIndex = ApprovalGrammar.moveChoice(delta, choiceIndex, (request.options || []).length);
        choiceList.positionViewAtIndex(choiceIndex, ListView.Contain);
    }


    function reject() {
        if (!request) return;
        let current = request;
        let requestId = current.id;
        clearView();
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
        clearView();
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
            Text {
                text: root.consequenceFor(root.request)
                color: Theme.accentMuted
                wrapMode: Text.Wrap
                font.pixelSize: 12
                Layout.fillWidth: true
            }
            Text {
                text: root.destinationFor(root.request)
                color: Theme.subtext1
                wrapMode: Text.Wrap
                font.pixelSize: 12
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
                    font.family: "monospace"
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
                // Enter intentionally remains a newline for input/editor
                // (labelled by the hint above). The explicit Accept button
                // is the only submit path.
                Keys.onPressed: (event) => {
                    if (event.key === Qt.Key_Escape) { root.reject(); event.accepted = true; }
                }
            }
            Text {
                visible: root.request && (root.request.method === "input" || root.request.method === "editor")
                text: "Enter adds a newline · Accept submits"
                color: Theme.subtext0
                font.pixelSize: 12
                Layout.fillWidth: true
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                Button {
                    text: "Reject (Esc)"
                    onClicked: root.reject()
                    contentItem: Text { text: parent.text; color: Theme.text; font.family: Theme.fontFamily; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                    background: Rectangle { color: parent.hovered ? Theme.surface1 : Theme.mantle; radius: Theme.controlRadius; border.color: Theme.border }
                }
                Button {
                    text: "Defer"
                    onClicked: root.dismiss()
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
            Text {
                text: "Defer or closing the palette parks the request queued — it never expires and never reopens on its own while the palette stays open. Reopening the palette shows whatever is pending then. Reject cancels this request; Stop cancels everything."
                color: Theme.subtext0
                wrapMode: Text.Wrap
                font.pixelSize: 12
                Layout.fillWidth: true
            }
        }
    }
}
