import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Hyprland
import "../theme"

Row {
    id: root
    spacing: 4

    readonly property var workspaceIds: {
        let ids = new Set([1, 2, 3, 4, 5]);
        if (Hyprland.focusedMonitor) {
            for (let i = 0; i < Hyprland.workspaces.values.length; i++) {
                let ws = Hyprland.workspaces.values[i];
                let isFocused = Hyprland.focusedMonitor.activeWorkspace && Hyprland.focusedMonitor.activeWorkspace.id === ws.id;
                
                if (ws.windows > 0 || isFocused) {
                    ids.add(ws.id);
                }
            }
        }
        return Array.from(ids).sort((a, b) => a - b);
    }

    Repeater {
        model: root.workspaceIds
        
        Rectangle {
            id: wsButton
            
            property bool hovered: false
            
            readonly property var workspace: {
                for (let i = 0; i < Hyprland.workspaces.values.length; i++) {
                    if (Hyprland.workspaces.values[i].id === modelData) {
                        return Hyprland.workspaces.values[i];
                    }
                }
                return null;
            }
            
            readonly property bool isFocused: Hyprland.focusedMonitor && Hyprland.focusedMonitor.activeWorkspace && Hyprland.focusedMonitor.activeWorkspace.id === modelData
            readonly property bool isActive: workspace !== null

            width: 30
            height: 24
            color: {
                if (isFocused) return Theme.blue;
                if (hovered) return Theme.surface0;
                return "transparent";
            }
            radius: Theme.radius
            clip: true

            Text {
                id: wsText
                anchors.horizontalCenter: parent.horizontalCenter
                y: (isActive || isFocused || wsButton.hovered) ? (parent.height - height) / 2 : parent.height
                text: modelData
                color: isFocused ? Theme.base : (wsButton.hovered ? Theme.text : Theme.base3)
                font.pixelSize: 12
                opacity: (isActive || isFocused || wsButton.hovered) ? 1.0 : 0.0

                Behavior on y {
                    NumberAnimation {
                        duration: 200
                        easing.type: Easing.OutCubic
                    }
                }

                Behavior on opacity {
                    NumberAnimation { duration: 200 }
                }
            }

            MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                onEntered: wsButton.hovered = true
                onExited: wsButton.hovered = false
                onClicked: {
                    const proc = Quickshell.createProcess(["hyprctl", "dispatch", "workspace", modelData.toString()]);
                    proc.running = true;
                }
                cursorShape: Qt.PointingHandCursor
            }
        }
    }
}
