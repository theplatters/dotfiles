import QtQuick
import Quickshell
import Quickshell.Io
import "../theme"

Rectangle {
    id: root
    height: 32
    width: 32
    radius: Theme.radius
    color: Theme.mantle

    Text {
        anchors.centerIn: parent
        text: "󰹑"
        color: Theme.blue
        font.pixelSize: 18
    }

    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        onClicked: (mouse) => {
            if (mouse.button === Qt.RightButton) {
                screenshotProcess.command = ["hyprshot", "-m", "output"]
            } else {
                screenshotProcess.command = ["hyprshot", "-m", "region"]
            }
            screenshotProcess.running = true
        }
        cursorShape: Qt.PointingHandCursor
        hoverEnabled: true
        onEntered: root.color = Theme.surface0
        onExited: root.color = Theme.mantle
    }

    Process {
        id: screenshotProcess
        command: ["hyprshot", "-m", "region"]
    }
}
