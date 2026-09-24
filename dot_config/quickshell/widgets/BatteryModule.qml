import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Services.UPower
import "../theme"

Item {
    id: root
    implicitWidth: layout.implicitWidth
    implicitHeight: layout.implicitHeight
    width: implicitWidth
    height: implicitHeight

    property bool compact: false
    property var controlCenter: null
    property bool hovered: mouseArea.containsMouse
    
    readonly property real percentage: UPower.displayDevice.percentage * 100
    readonly property int state: UPower.displayDevice.state // 1: Charging, 2: Discharging
    
    readonly property string timeRemaining: {
        let seconds = 0;
        if (root.state === 1) {
            seconds = UPower.displayDevice.timeToFull;
        } else if (root.state === 2) {
            seconds = UPower.displayDevice.timeToEmpty;
        }
        
        if (seconds <= 0) return "";
        
        let hours = Math.floor(seconds / 3600);
        let minutes = Math.floor((seconds % 3600) / 60);
        
        if (hours > 0) {
            return hours + "h " + minutes + "m";
        } else {
            return minutes + "m";
        }
    }

    Row {
        id: layout
        spacing: 8
        anchors.verticalCenter: parent.verticalCenter

        Text {
            color: Theme.subtext1
            font.family: Theme.iconFontFamily
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
            visible: !root.compact
            color: Theme.text
            font.pixelSize: 12
            anchors.verticalCenter: parent.verticalCenter
            text: {
                if (!UPower.displayDevice.ready) return "..."
                return Math.round(root.percentage) + "%"
            }
        }
    }

    MouseArea {
        id: mouseArea
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: {
            if (root.controlCenter) root.controlCenter.toggleSection(3);
        }
    }
}
