import QtQuick
import Quickshell
import Quickshell.Io
import "../theme"

Item {
    id: root
    implicitWidth: layout.implicitWidth
    implicitHeight: layout.implicitHeight
    width: implicitWidth
    height: implicitHeight
    
    property string label: ""
    property var command: []
    property int interval: 5000
    property color textColor: Theme.text
    property bool compact: false
    
    Row {
        id: layout
        spacing: 5
        anchors.verticalCenter: parent.verticalCenter

        Text {
            visible: !root.compact
            text: root.label
            color: root.textColor
            font.pixelSize: 12
            anchors.verticalCenter: parent.verticalCenter
        }
        Text {
            id: valueText
            text: "..."
            color: root.textColor
            font.pixelSize: 12
            anchors.verticalCenter: parent.verticalCenter
        }
    }

    Process {
        id: proc
        command: root.command
        running: true
        stdout: StdioCollector {
            id: collector
        }
        
        onExited: (exitCode, exitStatus) => {
            valueText.text = collector.text.trim();
        }
    }

    Timer {
        interval: root.interval
        running: true
        repeat: true
        onTriggered: proc.running = true
    }
}
