import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Wayland
import "../theme"

PopupWindow {
    id: root
    
    property var anchorItem: null
    property string text: ""
    property bool requestedOpen: false
    property bool closing: false
    visible: false

    // Slight offset to prevent overlap with the trigger area which causes flashing
    anchor {
        window: QsWindow.window
        rect: anchorItem ? Qt.rect(anchorItem.mapToGlobal(0, 0).x, anchorItem.mapToGlobal(0, 0).y + anchorItem.height + 4, anchorItem.width, 1) : Qt.rect(0, 0, 0, 0)
        gravity: Edges.Bottom
    }

    function setOpen(open) {
        requestedOpen = open;
        if (open) {
            closing = false;
            exitMotion.stop();
            panel.opacity = 0;
            panel.scale = 0.98;
            if (!visible) visible = true;
            enterMotion.restart();
        } else if (visible && !closing) {
            closing = true;
            enterMotion.stop();
            exitMotion.restart();
        }
    }

    function showMessage(message, item) {
        text = message;
        anchorItem = item;
        setOpen(true);
    }

    function hide() {
        setOpen(false);
    }

    Rectangle {
        id: panel
        implicitWidth: tooltipText.implicitWidth + 20
        implicitHeight: tooltipText.implicitHeight + 10
        color: Theme.base
        radius: Theme.controlRadius
        border.color: Theme.border
        border.width: 1
        opacity: 0
        scale: 0.98

        Text {
            id: tooltipText
            anchors.centerIn: parent
            text: root.text
            color: Theme.text
            font.pixelSize: 11
            font.bold: true
        }
    }

    ParallelAnimation {
        id: enterMotion
        NumberAnimation { target: panel; property: "opacity"; to: 1; duration: Theme.motionPanel; easing.type: Easing.OutCubic }
        NumberAnimation { target: panel; property: "scale"; to: 1; duration: Theme.motionPanel; easing.type: Easing.OutCubic }
    }

    SequentialAnimation {
        id: exitMotion
        ParallelAnimation {
            NumberAnimation { target: panel; property: "opacity"; to: 0; duration: Theme.motionExit; easing.type: Easing.InCubic }
            NumberAnimation { target: panel; property: "scale"; to: 0.98; duration: Theme.motionExit; easing.type: Easing.InCubic }
        }
        ScriptAction { script: { if (!root.requestedOpen) root.visible = false; root.closing = false; } }
    }
}
