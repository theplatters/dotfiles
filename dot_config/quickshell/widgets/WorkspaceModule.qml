import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Hyprland
import "../theme"

Item {
    id: root
    
    readonly property bool isSpecialMode: {
        if (!Hyprland.workspaces) return false;
        for (let i = 0; i < Hyprland.workspaces.values.length; i++) {
            let ws = Hyprland.workspaces.values[i];
            if (ws.id < 0 && ws.active) return true;
        }
        return false;
    }

    implicitWidth: layoutContainer.width
    implicitHeight: 30
    width: implicitWidth
    height: implicitHeight

    Item {
        id: layoutContainer
        height: parent.height
        width: root.isSpecialMode ? specialTray.implicitWidth : normalTray.implicitWidth
        clip: true

        Behavior on width {
            NumberAnimation { duration: Theme.motionPanel; easing.type: Easing.OutCubic }
        }

        Row {
            id: normalTray
            spacing: 4
            anchors.verticalCenter: parent.verticalCenter
            opacity: root.isSpecialMode ? 0 : 1
            visible: opacity > 0
            x: root.isSpecialMode ? -20 : 0

            Behavior on opacity { NumberAnimation { duration: Theme.motionPanel } }
            Behavior on x { NumberAnimation { duration: Theme.motionPanel; easing.type: Easing.OutCubic } }

            readonly property var workspaceIds: {
                let ids = new Set([1, 2, 3, 4, 5]);
                if (Hyprland.focusedMonitor) {
                    for (let i = 0; i < Hyprland.workspaces.values.length; i++) {
                        let ws = Hyprland.workspaces.values[i];
                        let isFocused = Hyprland.focusedMonitor.activeWorkspace && Hyprland.focusedMonitor.activeWorkspace.id === ws.id;
                        
                        if (ws.id > 0 && (ws.windows > 0 || isFocused)) {
                            ids.add(ws.id);
                        }
                    }
                }
                return Array.from(ids).sort((a, b) => a - b);
            }

            Repeater {
                model: normalTray.workspaceIds
                
                Rectangle {
                    id: wsButton
                    property bool hovered: false
                    
                    readonly property var workspace: {
                        for (let i = 0; i < Hyprland.workspaces.values.length; i++) {
                            if (Hyprland.workspaces.values[i].id === modelData) return Hyprland.workspaces.values[i];
                        }
                        return null;
                    }
                    
                    readonly property bool isFocused: Hyprland.focusedMonitor && Hyprland.focusedMonitor.activeWorkspace && Hyprland.focusedMonitor.activeWorkspace.id === modelData
                    readonly property bool isActive: workspace !== null

                    width: 30
                    height: 24
                    color: isFocused ? Theme.surface2 : (hovered ? Theme.surface0 : "transparent")
                    radius: Theme.controlRadius
                    clip: true

                    Text {
                        anchors.horizontalCenter: parent.horizontalCenter
                        y: (isActive || isFocused || wsButton.hovered) ? (parent.height - height) / 2 : parent.height
                        text: modelData
                        color: isFocused ? Theme.text : (wsButton.hovered ? Theme.text : Theme.subtext0)
                        font.pixelSize: 12
                        opacity: (isActive || isFocused || wsButton.hovered) ? 1.0 : 0.0

                        Behavior on y {
                            NumberAnimation { duration: Theme.motionPanel; easing.type: Easing.OutCubic }
                        }

                        Behavior on opacity {
                            NumberAnimation { duration: Theme.motionPanel }
                        }
                    }

                    MouseArea {
                        anchors.fill: parent
                        hoverEnabled: true
                        onEntered: wsButton.hovered = true
                        onExited: wsButton.hovered = false
                        onClicked: {
                            if (workspace) {
                                workspace.activate();
                            } else {
                                Hyprland.dispatch("hl.dsp.focus({ workspace = " + modelData + " })");
                            }
                        }
                        cursorShape: Qt.PointingHandCursor
                    }
                }
            }
        }

        Row {
            id: specialTray
            spacing: 8
            anchors.verticalCenter: parent.verticalCenter
            opacity: root.isSpecialMode ? 1 : 0
            visible: opacity > 0
            x: root.isSpecialMode ? 0 : 20

            Behavior on opacity { NumberAnimation { duration: Theme.motionPanel } }
            Behavior on x { NumberAnimation { duration: Theme.motionPanel; easing.type: Easing.OutCubic } }

            Repeater {
                model: [
                    { name: "special:evolution", icon: "󰇰", color: Theme.accentMuted },
                    { name: "special:scratchpad", icon: "󰠮", color: Theme.accentMuted }
                ]
                
                Rectangle {
                    id: specBtn
                    property bool hovered: false
                    
                    readonly property var workspace: {
                        for (let i = 0; i < Hyprland.workspaces.values.length; i++) {
                            if (Hyprland.workspaces.values[i].name === modelData.name) return Hyprland.workspaces.values[i];
                        }
                        return null;
                    }
                    
                    readonly property bool isFocused: workspace && workspace.focused
                    readonly property bool isActive: workspace !== null && workspace.active

                    width: 32
                    height: 26
                    color: isFocused ? modelData.color : (hovered ? Theme.surface0 : "transparent")
                    radius: Theme.controlRadius
                    clip: true

                    Text {
                        anchors.centerIn: parent
                        text: modelData.icon
                        color: isFocused ? Theme.base : (specBtn.hovered ? Theme.text : (isActive ? Theme.text : Theme.surface2))
                        font.pixelSize: 14
                        font.bold: isFocused
                        
                        Behavior on color { ColorAnimation { duration: 200 } }
                    }

                    MouseArea {
                        anchors.fill: parent
                        hoverEnabled: true
                        onEntered: specBtn.hovered = true
                        onExited: specBtn.hovered = false
                        onClicked: {
                            if (workspace) {
                                workspace.activate();
                            } else {
                                Hyprland.dispatch("hl.dsp.focus({ workspace = '" + modelData.name + "' })");
                            }
                        }
                        cursorShape: Qt.PointingHandCursor
                    }
                }
            }
        }
    }
}
