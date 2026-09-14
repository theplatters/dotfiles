import QtQuick
import QtQuick.Layouts
import Quickshell
import "../theme"

// Calendar + daily agenda popout anchored to the bar clock. The shared
// DailyAgenda state lives in shell.qml, so closing this popup never
// stops the Pomodoro timer or drops a completion draft.
PopupWindow {
    id: root

    property var anchorItem: null
    property var agenda: null
    property bool requestedOpen: false
    property bool closing: false

    visible: false
    implicitWidth: 430
    implicitHeight: 640
    color: "transparent"

    anchor {
        window: QsWindow.window
        rect: anchorItem ? Qt.rect(anchorItem.mapToGlobal(0, 0).x, anchorItem.mapToGlobal(0, 0).y + anchorItem.height + 8, anchorItem.width, 1) : Qt.rect(0, 48, 1, 1)
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
            if (planner) planner.syncViewToDate();
            if (agenda && !agenda.agendaBusy && !agenda.completionSaving) agenda.reload();
            enterMotion.restart();
        } else if (visible && !closing) {
            closing = true;
            enterMotion.stop();
            exitMotion.restart();
        }
    }

    function toggle(item) {
        if (item) anchorItem = item;
        setOpen(!requestedOpen);
    }

    Rectangle {
        id: panel
        anchors.fill: parent
        color: Theme.base
        radius: Theme.cardRadius
        border.color: Theme.border
        border.width: 1
        opacity: 0
        scale: 0.98

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 14
            spacing: 10

            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Text {
                    text: "Calendar"
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: 16
                    font.bold: true
                    Layout.fillWidth: true
                }
                WidgetIconButton {
                    text: "Close"
                    iconSource: "icons/x.svg"
                    tooltipText: "Close"
                    Accessible.name: "Close calendar"
                    onClicked: root.setOpen(false)
                }
            }

            Flickable {
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                contentWidth: width
                contentHeight: planner.implicitHeight
                boundsBehavior: Flickable.StopAtBounds
                DailyPlanner {
                    id: planner
                    width: parent.width
                    agenda: root.agenda
                }
            }
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
