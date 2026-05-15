import QtQuick
import Quickshell
import Quickshell.Services.Pipewire
import "../theme"

Item {
    id: root
    width: layout.implicitWidth
    height: layout.implicitHeight

    property var sink: Pipewire.defaultAudioSink

    // Track the default sink to get volume updates
    PwObjectTracker {
        objects: [root.sink]
    }

    readonly property bool isMuted: root.sink?.audio?.muted ?? false
    readonly property real volume: root.sink?.audio?.volume ?? 0

    property var audioPopup: null

    Row {
        id: layout
        spacing: 8
        anchors.verticalCenter: parent.verticalCenter

        Text {
            id: iconText
            color: root.isMuted ? Theme.red : Theme.pink
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
                    duration: 200
                    easing.type: Easing.OutBack
                }
            }

            Behavior on color {
                ColorAnimation { duration: 200 }
            }
        }

        Text {
            color: root.isMuted ? Theme.subtext0 : Theme.pink
            font.pixelSize: 12
            anchors.verticalCenter: parent.verticalCenter
            text: root.isMuted ? "Muted" : Math.round(root.volume * 100) + "%"
            opacity: root.isMuted ? 0.7 : 1.0

            Behavior on opacity {
                NumberAnimation { duration: 200 }
            }

            Behavior on color {
                ColorAnimation { duration: 200 }
            }
        }
    }

    property bool hovered: false

    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        hoverEnabled: true
        onEntered: root.hovered = true
        onExited: root.hovered = false
        onClicked: (mouse) => {
            if (mouse.button === Qt.RightButton) {
                if (root.audioPopup) root.audioPopup.toggle(audioContainer);
            } else {
                if (root.sink?.audio) {
                    root.sink.audio.muted = !root.sink.audio.muted;
                }
            }
        }
        onWheel: (wheel) => {
            if (root.sink?.audio) {
                let delta = wheel.angleDelta.y > 0 ? 0.02 : -0.02;
                root.sink.audio.volume = Math.max(0, Math.min(1, root.sink.audio.volume + delta));
            }
        }
    }
}
