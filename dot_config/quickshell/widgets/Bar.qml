import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Services.UPower
import "../theme"

Rectangle {
    id: bar
    width: parent.width
    height: 40
    color: Theme.bg

    property var notifServer: null
    property var mediaPopout: null
    property var audioPopup: null
    signal toggleSidebar()

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 10
        anchors.rightMargin: 10
        anchors.topMargin: 4
        anchors.bottomMargin: 4
        spacing: 12

        // LEFT
        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Row {
                anchors.left: parent.left
                spacing: 8

                // Sidebar Toggle
                Rectangle {
                    id: sidebarToggle
                    height: 32; width: 32; radius: Theme.radius; color: Theme.mantle
                    Text {
                        anchors.centerIn: parent
                        text: "󰍜"
                        color: Theme.mauve
                        font.pixelSize: 18
                    }
                    MouseArea {
                        anchors.fill: parent
                        onClicked: bar.toggleSidebar()
                        cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        onEntered: sidebarToggle.color = Theme.surface0
                        onExited: sidebarToggle.color = Theme.mantle
                    }
                }

                Rectangle {
                    id: clockContainer
                    height: 32
                    width: childrenRect.width + 16
                    radius: Theme.radius
                    color: Theme.mantle
                    Row {
                        anchors.verticalCenter: parent.verticalCenter
                        x: 8
                        spacing: 12
                        Text {
                            text: "󰃭"
                            color: Theme.text
                            font.pixelSize: 14
                            anchors.verticalCenter: parent.verticalCenter
                        }
                        ClockModule { format: "ddd"; anchors.verticalCenter: parent.verticalCenter }
                        ClockModule { format: "HH:mm"; anchors.verticalCenter: parent.verticalCenter }
                        ClockModule { format: "MM-dd"; anchors.verticalCenter: parent.verticalCenter }
                    }
                    MouseArea {
                        anchors.fill: parent
                        hoverEnabled: true
                        onEntered: clockContainer.color = Theme.surface0
                        onExited: clockContainer.color = Theme.mantle
                    }
                }
            }
        }

        // CENTER
        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Row {
                anchors.centerIn: parent
                spacing: 8
                Rectangle {
                    height: 32
                    width: childrenRect.width + 10
                    radius: Theme.radius
                    color: Theme.mantle
                    anchors.verticalCenter: parent.verticalCenter
                    WorkspaceModule { anchors.verticalCenter: parent.verticalCenter; x: 5 }
                }

                Rectangle {
                    id: mediaContainer
                    height: 32
                    width: childrenRect.width + 16
                    radius: Theme.radius
                    color: Theme.mantle
                    anchors.verticalCenter: parent.verticalCenter
                    visible: mediaModule.status !== ""
                    MediaModule {
                        id: mediaModule
                        anchors.verticalCenter: parent.verticalCenter
                        mediaPopout: bar.mediaPopout
                        x: 8
                    }
                    MouseArea {
                        anchors.fill: parent
                        hoverEnabled: true
                        onEntered: mediaContainer.color = Theme.surface0
                        onExited: mediaContainer.color = Theme.mantle
                        onClicked: {
                            if (bar.mediaPopout) {
                                bar.mediaPopout.toggle(mediaContainer);
                            }
                        }
                    }
                }
            }
        }

        // RIGHT
        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Row {
                anchors.right: parent.right
                spacing: 8
                
                // Audio
                Rectangle {
                    id: audioContainer
                    height: 32; width: childrenRect.width + 16; radius: Theme.radius; color: Theme.mantle
                    AudioModule { 
                        id: audioModule
                        anchors.verticalCenter: parent.verticalCenter; x: 8 
                        audioPopup: bar.audioPopup
                        onHoveredChanged: (hovered) => audioContainer.color = hovered ? Theme.surface0 : Theme.mantle
                    }
                    MouseArea {
                        anchors.fill: parent
                        acceptedButtons: Qt.RightButton
                        onClicked: {
                            if (bar.audioPopup) {
                                bar.audioPopup.toggle(audioContainer);
                            }
                        }
                    }
                }

                // Screenshot
                ScreenshotModule {}
                
                // System Stats (Mem, CPU, Disk)
                Rectangle {
                    id: statsContainer
                    height: 32
                    width: childrenRect.width + 16
                    radius: Theme.radius
                    color: Theme.mantle
                    Row {
                        anchors.verticalCenter: parent.verticalCenter
                        x: 8
                        spacing: 15
                        StatModule { 
                            label: "Mem"; textColor: Theme.mauve; 
                            command: ["sh", "-c", "LC_ALL=C free | awk '/Mem:/ {print int($3/$2 * 100)\"%\"}'"]
                        }
                        StatModule { 
                            label: "CPU"; textColor: Theme.red; 
                            command: ["sh", "-c", "LC_ALL=C top -bn1 | grep '^%*Cpu(s)' | awk '{print int(100 - $8)\"%\"}'"]
                        }
                        StatModule { 
                            label: "Disk"; textColor: Theme.teal; 
                            command: ["sh", "-c", "python3 -c \"import shutil; t, u, f = shutil.disk_usage('/'); print(f'{round((t-f)/t*100)}%')\""]
                        }
                    }
                    MouseArea {
                        anchors.fill: parent
                        hoverEnabled: true
                        onEntered: statsContainer.color = Theme.surface0
                        onExited: statsContainer.color = Theme.mantle
                    }
                }

                // Notifications
                NotificationModule {
                    notifServer: bar.notifServer
                    anchors.verticalCenter: parent.verticalCenter
                }

                // Battery
                Rectangle {
                    id: batteryContainer
                    visible: UPower.displayDevice.type === 2
                    height: 32; width: childrenRect.width + 16; radius: Theme.radius; color: Theme.mantle
                    BatteryModule { anchors.verticalCenter: parent.verticalCenter; x: 8 }
                    MouseArea {
                        anchors.fill: parent
                        hoverEnabled: true
                        onEntered: batteryContainer.color = Theme.surface0
                        onExited: batteryContainer.color = Theme.mantle
                    }
                }

                // Tray
                Rectangle {
                    id: trayContainer
                    height: 32; width: childrenRect.width + 16; radius: Theme.radius; color: Theme.mantle
                    TrayModule { anchors.verticalCenter: parent.verticalCenter; x: 8 }
                    MouseArea {
                        anchors.fill: parent
                        hoverEnabled: true
                        onEntered: trayContainer.color = Theme.surface0
                        onExited: trayContainer.color = Theme.mantle
                    }
                }
            }
        }
    }
}
