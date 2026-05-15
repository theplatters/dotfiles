import QtQuick
import Quickshell
import Quickshell.Io
import "../theme"

Item {
    id: root
    property string label: ""
    property var command: []
    property int interval: 5000
    property color textColor: Theme.text
    
    width: layout.implicitWidth
    height: layout.implicitHeight

    Row {
        id: layout
        spacing: 5
        anchors.verticalCenter: parent.verticalCenter
        Text {
            text: root.label
            color: root.textColor
            font.pixelSize: 12
        }
        Text {
            id: valueText
            text: "..."
            color: root.textColor
            font.pixelSize: 12
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
