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
    
    signal passwordEntered(string password)
    signal canceled()

    // Full screen transparent window to center the content
    anchors {
        top: true
        bottom: true
        left: true
        right: true
    }
    
    color: "transparent"
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
        color: "#B0070707"
        opacity: 0
        
        MouseArea {
            anchors.fill: parent
            onClicked: {
                root.dismiss();
                root.canceled();
            }
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
        opacity: 0
        scale: 0.98
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
                    onClicked: {
                        root.canceled();
                        root.dismiss();
                    }
                    
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

    function setOpen(open) {
        requestedOpen = open;
        if (open) {
            closing = false;
            exitMotion.stop();
            backdrop.opacity = 0;
            card.opacity = 0;
            card.scale = 0.98;
            if (!visible) visible = true;
            enterMotion.restart();
        } else if (visible && !closing) {
            closing = true;
            passwordField.focus = false;
            enterMotion.stop();
            exitMotion.restart();
        }
    }

    ParallelAnimation {
        id: enterMotion
        NumberAnimation { target: backdrop; property: "opacity"; to: 1; duration: Theme.motionPanel; easing.type: Easing.OutCubic }
        NumberAnimation { target: card; property: "opacity"; to: 1; duration: Theme.motionPanel; easing.type: Easing.OutCubic }
        NumberAnimation { target: card; property: "scale"; to: 1; duration: Theme.motionPanel; easing.type: Easing.OutCubic }
    }

    SequentialAnimation {
        id: exitMotion
        ParallelAnimation {
            NumberAnimation { target: backdrop; property: "opacity"; to: 0; duration: Theme.motionExit; easing.type: Easing.InCubic }
            NumberAnimation { target: card; property: "opacity"; to: 0; duration: Theme.motionExit; easing.type: Easing.InCubic }
            NumberAnimation { target: card; property: "scale"; to: 0.98; duration: Theme.motionExit; easing.type: Easing.InCubic }
        }
        ScriptAction { script: { if (!root.requestedOpen) root.visible = false; root.closing = false; } }
    }
}
