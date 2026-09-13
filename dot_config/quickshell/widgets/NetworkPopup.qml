import QtQuick
import Quickshell
import "../theme"

// Thin legacy wrapper around NetworkPanel for backcompat.
// New code should use ControlCenter; no instances remain in shell.qml.
PopupWindow {
    id: root

    property var anchorItem: null
    property var passwordPopup: null
    property alias pendingNetwork: panel.pendingNetwork
    property alias pendingSettings: panel.pendingSettings
    property alias pendingPassword: panel.pendingPassword
    property alias activeTab: panel.activeTab
    property alias statusMessage: panel.statusMessage
    property bool requestedOpen: false
    property bool closing: false

    readonly property alias wifiDevice: panel.wifiDevice
    readonly property alias hasWifi: panel.hasWifi
    readonly property alias hasWired: panel.hasWired
    readonly property alias isScanning: panel.isScanning
    readonly property alias bluetoothAdapter: panel.bluetoothAdapter
    readonly property alias hasBluetooth: panel.hasBluetooth

    visible: false
    implicitWidth: 400
    implicitHeight: 580
    color: "transparent"

    anchor {
        window: QsWindow.window
        rect: anchorItem ? Qt.rect(anchorItem.mapToGlobal(0, 0).x, anchorItem.mapToGlobal(0, 0).y + anchorItem.height + 8, anchorItem.width, 1) : Qt.rect(0, 48, 1, 1)
        gravity: Edges.Bottom
    }

    onVisibleChanged: {
        panel.syncDiscovery();
    }

    onActiveTabChanged: {
        panel.syncDiscovery();
    }

    function syncDiscovery() {
        panel.syncDiscovery();
    }

    function setOpen(open) {
        requestedOpen = open;
        if (open) {
            closing = false;
            exitMotion.stop();
            outer.opacity = 0;
            outer.scale = 0.98;
            if (!visible) visible = true;
            panel.syncDiscovery();
            enterMotion.restart();
        } else if (visible && !closing) {
            closing = true;
            panel.syncDiscovery();
            enterMotion.stop();
            exitMotion.restart();
        }
    }

    function toggle(item) {
        anchorItem = item;
        setOpen(!requestedOpen);
    }

    Rectangle {
        id: outer
        anchors.fill: parent
        color: Theme.base
        radius: Theme.cardRadius
        border.color: Theme.border
        border.width: 1
        opacity: 0
        scale: 0.98

        NetworkPanel {
            id: panel
            anchors.fill: parent
            anchors.margins: 12
            passwordPopup: root.passwordPopup
            active: root.requestedOpen && root.visible
        }
    }

    ParallelAnimation {
        id: enterMotion
        NumberAnimation { target: outer; property: "opacity"; to: 1; duration: Theme.motionPanel; easing.type: Easing.OutCubic }
        NumberAnimation { target: outer; property: "scale"; to: 1; duration: Theme.motionPanel; easing.type: Easing.OutCubic }
    }

    SequentialAnimation {
        id: exitMotion
        ParallelAnimation {
            NumberAnimation { target: outer; property: "opacity"; to: 0; duration: Theme.motionExit; easing.type: Easing.InCubic }
            NumberAnimation { target: outer; property: "scale"; to: 0.98; duration: Theme.motionExit; easing.type: Easing.InCubic }
        }
        ScriptAction { script: { if (!root.requestedOpen) root.visible = false; root.closing = false; } }
    }
}
