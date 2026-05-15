import QtQuick
import Quickshell
import Quickshell.Services.UPower
import "../theme"

Item {
    id: root
    width: layout.implicitWidth
    height: layout.implicitHeight

    readonly property real percentage: UPower.displayDevice.percentage
    readonly property int state: UPower.displayDevice.state // 1: Charging, 2: Discharging

    Row {
        id: layout
        spacing: 8
        anchors.verticalCenter: parent.verticalCenter

        Text {
            color: Theme.green
            font.pixelSize: 14
            anchors.verticalCenter: parent.verticalCenter
            text: {
                if (!UPower.displayDevice.ready) return "󰂑"
                
                let percent = Math.round(root.percentage)
                if (root.state === 1) return "󰂄" // Charging
                
                if (percent < 10) return "󰂎"
                if (percent < 20) return "󰁺"
                if (percent < 30) return "󰁻"
                if (percent < 40) return "󰁼"
                if (percent < 50) return "󰁽"
                if (percent < 60) return "󰁾"
                if (percent < 70) return "󰁿"
                if (percent < 80) return "󰂀"
                if (percent < 90) return "󰂁"
                if (percent < 100) return "󰂂"
                return "󰁹"
            }
        }

        Text {
            color: Theme.green
            font.pixelSize: 12
            anchors.verticalCenter: parent.verticalCenter
            text: {
                if (!UPower.displayDevice.ready) return "..."
                return Math.round(root.percentage) + "%"
            }
        }
    }
}
