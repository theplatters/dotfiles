import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import Quickshell.Services.Pipewire
import "../theme"

PopupWindow {
    id: root
    
    property var anchorItem: null
    visible: false
    
    width: 300
    height: 300
    
    color: Theme.base
    
    Process {
        id: wpctlProc
    }

    anchor {
        window: QsWindow.window
        rect: anchorItem ? Qt.rect(anchorItem.mapToGlobal(0, 0).x, anchorItem.mapToGlobal(0, 0).y + anchorItem.height + 8, anchorItem.width, 1) : Qt.rect(0, 48, 1, 1)
        gravity: Edges.Bottom
    }

    Rectangle {
        anchors.fill: parent
        color: Theme.base
        radius: Theme.radius
        border.color: Theme.pink
        border.width: 2

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 10
            spacing: 10

            Text {
                text: "Audio Outputs"
                color: Theme.pink
                font.pixelSize: 16
                font.bold: true
                Layout.alignment: Qt.AlignHCenter
            }

            ListView {
                id: sinkList
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 5
                clip: true
                model: Pipewire.nodes

                delegate: Rectangle {
                    // Only show nodes that are sinks or the current default sink
                    readonly property bool isDefault: Pipewire.defaultAudioSink && Pipewire.defaultAudioSink.id === modelData.id
                    readonly property bool isActuallySink: modelData.isSink || (modelData.mediaClass && modelData.mediaClass.includes("Sink")) || isDefault
                    visible: isActuallySink
                    height: visible ? 45 : 0
                    width: sinkList.width
                    color: isDefault ? Theme.mauve : "transparent"
                    radius: 8
                    
                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: 10
                        visible: parent.visible
                        
                        Text {
                            text: (isDefault ? "󰓃 " : "󰓄 ") + (modelData.description || modelData.name)
                            color: isDefault ? Theme.base : Theme.text
                            font.pixelSize: 13
                            Layout.fillWidth: true
                            elide: Text.ElideRight
                        }

                        Text {
                            text: Math.round((modelData.audio?.volume ?? 0) * 100) + "%"
                            color: isDefault ? Theme.base : Theme.subtext0
                            font.pixelSize: 11
                            visible: modelData.audio !== null
                        }
                    }

                    MouseArea {
                        anchors.fill: parent
                        enabled: parent.visible
                        hoverEnabled: true
                        onEntered: if (!isDefault) parent.color = Theme.surface0
                        onExited: if (!isDefault) parent.color = "transparent"
                        onClicked: {
                            // Use wpctl to set the default device in WirePlumber/Pipewire
                            wpctlProc.command = ["wpctl", "set-default", modelData.id.toString()];
                            wpctlProc.running = true;
                            
                            // Close the popup - the UI will update once Pipewire reports the change
                            root.visible = false;
                        }
                    }
                }
            }
        }
    }

    function toggle(item) {
        if (visible) {
            visible = false;
        } else {
            anchorItem = item;
            visible = true;
        }
    }
}
