import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import Quickshell
import Quickshell.Wayland
import "../theme"

PanelWindow {
    id: root
    
    property string ssid: ""
    property bool requestedOpen: false
    property bool closing: false
    // Single reveal progress (ControlCenter pattern): backdrop + card
    // derive from it so an interrupted close reopens mid-fade.
    property real reveal: 0
    
    signal passwordEntered(string password)
    signal canceled()

    // Full screen transparent window to center the content
    anchors {
        top: true
        bottom: true
        left: true
        right: true
    }
    
    color: Theme.transparent
    visible: false
    
    // Make it stay on top
    WlrLayershell.layer: WlrLayer.Overlay
    
    // Capture keyboard input when visible
    WlrLayershell.keyboardFocus: root.requestedOpen && !root.closing
        ? WlrKeyboardFocus.OnDemand : WlrKeyboardFocus.None

    // Dim background
    Rectangle {
        id: backdrop
        anchors.fill: parent
        color: Theme.scrim
        opacity: root.reveal
        
        MouseArea {
            anchors.fill: parent
            enabled: root.requestedOpen && !root.closing
            onClicked: root.cancelAndClose()
        }
    }

    Rectangle {
        id: card
        anchors.centerIn: parent
        width: 320
        height: 200
        color: Theme.base
        radius: Theme.cardRadius
        border.color: Theme.border
        border.width: 1
        opacity: root.reveal
        scale: 0.96 + 0.04 * root.reveal
        transformOrigin: Item.Center
        enabled: root.requestedOpen && !root.closing
        
        // Prevent clicks from closing the popup
        MouseArea { anchors.fill: parent }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 25
            spacing: 20
            
            Text {
                text: "Wi-Fi Password"
                color: Theme.text
                font.bold: true
                font.pixelSize: 18
                Layout.alignment: Qt.AlignHCenter
            }
            
            ColumnLayout {
                spacing: 5
                Layout.fillWidth: true
                
                Text {
                    text: "Connecting to: " + root.ssid
                    color: Theme.text
                    font.pixelSize: 12
                    opacity: 0.8
                    Layout.fillWidth: true
                    elide: Text.ElideMiddle
                }
                
                TextField {
                    id: passwordField
                    Layout.fillWidth: true
                    placeholderText: "Password"
                    echoMode: TextInput.Password
                    focus: true
                    enabled: root.requestedOpen && !root.closing
                    
                    background: Rectangle {
                        color: Theme.mantle
                        radius: Theme.controlRadius
                border.color: parent.activeFocus ? Theme.focusBorder : Theme.border
                border.width: 1
                    }
                    
                    color: Theme.text
                    font.pixelSize: 14
                    
                    onAccepted: {
                        if (text.length === 0) return;
                        root.passwordEntered(text);
                        root.dismiss();
                    }
                }
            }
            
            RowLayout {
                Layout.fillWidth: true
                spacing: 12
                
                Button {
                    text: "Cancel"
                    Layout.fillWidth: true
                    onClicked: root.cancelAndClose()
                    
                    contentItem: Text {
                        text: parent.text
                        color: Theme.text
                        font.bold: true
                        horizontalAlignment: Text.AlignHCenter
                    }
                    background: Rectangle {
                        color: Theme.surface1
                        radius: Theme.controlRadius
                        border.color: Theme.border
                    }
                }
                
                Button {
                    text: "Connect"
                    Layout.fillWidth: true
                    enabled: passwordField.text.length > 0
                    onClicked: {
                        root.passwordEntered(passwordField.text);
                        root.dismiss();
                    }
                    
                    contentItem: Text {
                        text: parent.text
                        color: Theme.text
                        font.bold: true
                        horizontalAlignment: Text.AlignHCenter
                    }
                    background: Rectangle {
                        color: Theme.surface2
                        radius: Theme.controlRadius
                        border.color: Theme.border
                    }
                }
            }
        }
    }
    
    function show(ssid) {
        root.ssid = ssid;
        setOpen(true);
        if (!closing) passwordField.forceActiveFocus();
    }

    function dismiss() {
        setOpen(false);
        passwordField.text = "";
    }

    // One external dismissal path: notify the requester and clear the
    // field exactly once (no double-cancel during the exit fade).
    function cancelAndClose() {
        if (!root.requestedOpen || root.closing) return false;
        root.canceled();
        root.dismiss();
        return true;
    }

    function setOpen(open) {
        requestedOpen = open;
        if (open) {
            closing = false;
            exitMotion.stop();
            if (!visible) visible = true;
            // Already open and steady: keep the frame (no reset/flash).
            if (root.reveal >= 0.99 && enterMotion.running === false) return;
            // Otherwise reverse from the current reveal value. Never
            // assign a fresh start while partially visible.
            enterMotion.restart();
        } else if (visible && !closing) {
            closing = true;
            passwordField.focus = false;
            enterMotion.stop();
            exitMotion.restart();
        }
    }

    Shortcut {
        sequence: "Escape"
        onActivated: root.cancelAndClose()
    }

    // Single-progress motion (ControlCenter pattern): enter/exit animate
    // reveal to 1/0 from the current value (interrupted close reopens
    // mid-fade, never resets). No spring/overshoot: OutCubic in, InCubic
    // out.
    NumberAnimation {
        id: enterMotion
        target: root
        property: "reveal"
        to: 1
        duration: Theme.motionPanel
        easing.type: Easing.OutCubic
    }

    NumberAnimation {
        id: exitMotion
        target: root
        property: "reveal"
        to: 0
        duration: Theme.motionExit
        easing.type: Easing.InCubic
        onFinished: {
            if (!root.requestedOpen) root.visible = false;
            root.closing = false;
        }
    }
}
