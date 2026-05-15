import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Wayland
import Quickshell.Services.Notifications
import "../theme"

PanelWindow {
    id: root
    
    property var notifServer: null
    
    anchors {
        top: true
        right: true
    }
    
    margins {
        top: 50
        right: 15
    }
    
    width: 350
    height: mainLayout.implicitHeight
    color: "transparent"
    
    // Make sure we don't block clicks when empty
    visible: activeNotifs.count > 0
    
    ListModel {
        id: activeNotifs
    }

    Connections {
        target: root.notifServer
        enabled: !!root.notifServer
        
        onNotification: (n) => {
            // Check for updates to existing notification
            for (let i = 0; i < activeNotifs.count; i++) {
                if (activeNotifs.get(i).notifId === n.id) {
                    activeNotifs.setProperty(i, "summary", n.summary);
                    activeNotifs.setProperty(i, "body", n.body);
                    return;
                }
            }

            // Add to active notifications
            activeNotifs.append({
                "notifId": n.id,
                "summary": n.summary,
                "body": n.body,
                "appName": n.appName,
                "notifObj": n
            });
        }
        
        onNotificationClosed: (n) => {
            root.dismiss(n.id);
        }
    }
    
    function dismiss(id) {
        for (let i = 0; i < activeNotifs.count; i++) {
            if (activeNotifs.get(i).notifId === id) {
                activeNotifs.remove(i);
                break;
            }
        }
    }

    ColumnLayout {
        id: mainLayout
        width: parent.width
        spacing: 10
        
        Repeater {
            model: activeNotifs
            delegate: Rectangle {
                id: toast
                Layout.fillWidth: true
                height: innerLayout.implicitHeight + 24
                color: Theme.base
                radius: 12
                border.color: Theme.mauve
                border.width: 1
                
                // Entry animation
                opacity: 0
                Component.onCompleted: opacity = 1
                Behavior on opacity { NumberAnimation { duration: 250 } }
                
                Timer {
                    id: dismissTimer
                    interval: 6000
                    running: !mouseArea.containsMouse
                    onTriggered: root.dismiss(notifId)
                }

                ColumnLayout {
                    id: innerLayout
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 4
                    
                    RowLayout {
                        Text {
                            text: summary
                            color: Theme.text
                            font.bold: true
                            font.pixelSize: 14
                            Layout.fillWidth: true
                            elide: Text.ElideRight
                        }
                        Text {
                            text: appName
                            color: Theme.teal
                            font.pixelSize: 10
                            font.italic: true
                        }
                        
                        Text {
                            text: "󰅖"
                            color: Theme.red
                            font.pixelSize: 14
                            MouseArea {
                                anchors.fill: parent
                                onClicked: root.dismiss(notifId)
                            }
                        }
                    }
                    
                    Text {
                        text: body
                        color: Theme.text
                        font.pixelSize: 12
                        wrapMode: Text.WordWrap
                        Layout.fillWidth: true
                        maximumLineCount: 3
                        elide: Text.ElideRight
                        opacity: 0.9
                    }
                }
                
                MouseArea {
                    id: mouseArea
                    anchors.fill: parent
                    hoverEnabled: true
                    onClicked: {
                        if (notifObj && notifObj.actions) {
                            for (let i = 0; i < notifObj.actions.length; i++) {
                                if (notifObj.actions[i].identifier === "default") {
                                    notifObj.actions[i].invoke();
                                    break;
                                }
                            }
                        }
                        root.dismiss(notifId);
                    }
                }
            }
        }
    }
}
