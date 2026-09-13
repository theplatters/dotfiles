import QtQuick
import Quickshell
import Quickshell.Services.Pipewire
import "../theme"

Item {
    id: root
    implicitWidth: layout.implicitWidth
    implicitHeight: layout.implicitHeight
    width: implicitWidth
    height: implicitHeight
    
    readonly property var sink: Pipewire.defaultAudioSink

    PwObjectTracker {
        objects: [root.sink]
    }

    readonly property bool hasSink: !!root.sink && !!root.sink.audio
    readonly property bool isMuted: root.hasSink && root.sink.audio.muted
    readonly property real volume: root.hasSink ? root.sink.audio.volume : 0

    property var audioPopup: null
    property var controlCenter: null
    property bool hovered: false

    Row {
        id: layout
        spacing: 8
        anchors.verticalCenter: parent.verticalCenter

        Text {
            id: iconText
            color: root.isMuted ? Theme.red : Theme.accentMuted
            font.family: Theme.iconFontFamily
            font.pixelSize: 14
            anchors.verticalCenter: parent.verticalCenter
            text: {
                if (root.isMuted) return "󰝟";
                let vol = Math.round(root.volume * 100);
                if (vol > 50) return "󰕾";
                if (vol > 0) return "󰖀";
                return "󰕿";
            }
            
            scale: root.isMuted ? 0.9 : 1.0
            
            Behavior on scale {
                NumberAnimation {
                    duration: Theme.motionPanel
                    easing.type: Easing.OutCubic
                }
            }

            Behavior on color {
                ColorAnimation { duration: Theme.motionPanel }
            }
        }

        Text {
            color: root.isMuted ? Theme.subtext0 : Theme.text
            font.pixelSize: 12
            anchors.verticalCenter: parent.verticalCenter
            text: root.hasSink ? (root.isMuted ? "Muted" : Math.round(root.volume * 100) + "%") : "No audio"
            opacity: root.isMuted ? 0.7 : 1.0

            Behavior on opacity {
                NumberAnimation { duration: Theme.motionPanel }
            }

            Behavior on color {
                ColorAnimation { duration: Theme.motionPanel }
            }
        }
    }

    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        hoverEnabled: true
        onEntered: root.hovered = true
        onExited: root.hovered = false
        onClicked: (mouse) => {
            if (mouse.button === Qt.RightButton) {
                if (root.controlCenter) root.controlCenter.toggleSection(0);
                else if (root.audioPopup) root.audioPopup.toggle(parent);
            } else {
                if (root.hasSink) {
                    root.sink.audio.muted = !root.sink.audio.muted;
                }
            }
        }
        onWheel: (wheel) => {
            if (root.hasSink) {
                let delta = wheel.angleDelta.y > 0 ? 0.02 : -0.02;
                root.sink.audio.volume = Math.max(0, Math.min(1, root.sink.audio.volume + delta));
            }
        }
    }
}
