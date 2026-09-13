import QtQuick
import QtQuick.Layouts
import Quickshell
import "../theme"

// Thin legacy wrapper around AudioPanel for backcompat.
// New code should use ControlCenter; no instances remain in shell.qml.
PopupWindow {
    id: root

    property var anchorItem: null
    property alias activeTab: panel.activeTab
    property bool requestedOpen: false
    property bool closing: false

    visible: false
    implicitWidth: 380
    implicitHeight: 520
    color: "transparent"

    anchor {
        window: QsWindow.window
        rect: anchorItem ? Qt.rect(anchorItem.mapToGlobal(0, 0).x, anchorItem.mapToGlobal(0, 0).y + anchorItem.height + 8, anchorItem.width, 1) : Qt.rect(0, 48, 1, 1)
        gravity: Edges.Bottom
    }

    function toggle(item) {
        anchorItem = item;
        root.setOpen(!requestedOpen);
    }

    function setOpen(open) {
        requestedOpen = open;
        if (open) {
            closing = false;
            exitMotion.stop();
            outer.opacity = 0;
            outer.scale = 0.98;
            if (!visible) visible = true;
            enterMotion.restart();
        } else if (visible && !closing) {
            closing = true;
            enterMotion.stop();
            exitMotion.restart();
        }
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

        AudioPanel {
            id: panel
            anchors.fill: parent
            anchors.margins: 12
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
