import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Services.Notifications
import "../theme"

Item {
    id: root
    width: 32
    height: 32
    
    property var notifServer: null
    property bool expanded: false

    ListModel {
        id: notifModel
    }

    Connections {
        target: root.notifServer
        enabled: !!root.notifServer
        
        onNotification: (n) => {
            console.log("New notification: " + n.summary);
            // Check if it's an update to an existing notification
            for (let i = 0; i < notifModel.count; i++) {
                if (notifModel.get(i).notifId === n.id) {
                    notifModel.setProperty(i, "summary", n.summary);
                    notifModel.setProperty(i, "body", n.body);
                    notifModel.setProperty(i, "appName", n.appName);
                    return;
                }
            }
            
            // New notification
            notifModel.insert(0, {
                "notifId": n.id,
                "summary": n.summary,
                "body": n.body,
                "appName": n.appName,
                "notifObj": n
            });
            
            if (notifModel.count > 10) {
                notifModel.remove(10);
            }
        }
        
        onNotificationClosed: (n) => {
            for (let i = 0; i < notifModel.count; i++) {
                if (notifModel.get(i).notifId === n.id) {
                    notifModel.remove(i);
                    break;
                }
            }
        }
    }

    Rectangle {
        id: bellContainer
        height: 32; width: 32; radius: Theme.radius; color: Theme.mantle
        
        Text {
            anchors.centerIn: parent
            text: "󰂚"
            color: notifModel.count > 0 ? Theme.mauve : Theme.text
            font.pixelSize: 18
        }
        
        Rectangle {
            visible: notifModel.count > 0
            width: 14; height: 14; radius: 7; color: Theme.red
            anchors.top: parent.top; anchors.right: parent.right
            anchors.topMargin: -2; anchors.rightMargin: -2
            
            Text {
                text: notifModel.count
                color: "white"
                font.pixelSize: 9
                font.bold: true
                anchors.centerIn: parent
            }
        }

        MouseArea {
            anchors.fill: parent
            onClicked: root.expanded = !root.expanded
            cursorShape: Qt.PointingHandCursor
        }
    }

    PanelWindow {
        id: popup
        visible: root.expanded
        
        anchors {
            top: true
            right: true
        }
        
        margins {
            top: 45
            right: 10
        }
        
        width: 350
        height: Math.min(500, mainLayout.implicitHeight + 20)
        color: "transparent"
        
        focusable: true
        
        Rectangle {
            anchors.fill: parent
            color: Theme.base
            radius: Theme.radius
            border.color: Theme.surface0
            border.width: 1
            
            ColumnLayout {
                id: mainLayout
                anchors.fill: parent
                anchors.margins: 10
                spacing: 10
                
                RowLayout {
                    Text {
                        text: "Notifications"
                        color: Theme.text
                        font.bold: true
                        font.pixelSize: 16
                        Layout.fillWidth: true
                    }
                    
                    Text {
                        text: "Clear All"
                        color: Theme.mauve
                        font.pixelSize: 12
                        
                        MouseArea {
                            anchors.fill: parent
                            onClicked: {
                                while (notifModel.count > 0) {
                                    let item = notifModel.get(0);
                                    if (item && item.notifObj) {
                                        item.notifObj.dismiss();
                                    }
                                    notifModel.remove(0);
                                }
                            }
                        }
                    }
                }
                
                ListView {
                    id: listView
                    Layout.fillWidth: true
                    Layout.preferredHeight: contentHeight
                    Layout.maximumHeight: 400
                    spacing: 8
                    clip: true
                    model: notifModel
                    
                    delegate: Rectangle {
                        width: listView.width
                        height: innerLayout.implicitHeight + 20
                        color: Theme.mantle
                        radius: 8
                        
                        ColumnLayout {
                            id: innerLayout
                            anchors.fill: parent
                            anchors.margins: 10
                            spacing: 4
                            
                            RowLayout {
                                Text {
                                    text: summary
                                    color: Theme.text
                                    font.bold: true
                                    Layout.fillWidth: true
                                    elide: Text.ElideRight
                                }
                                Text {
                                    text: appName
                                    color: Theme.teal
                                    font.pixelSize: 10
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
                            }
                        }
                        
                        MouseArea {
                            anchors.fill: parent
                            acceptedButtons: Qt.LeftButton | Qt.RightButton
                            onClicked: (mouse) => {
                                if (mouse.button === Qt.RightButton) {
                                    console.log("Dismissing notification: " + summary);
                                    if (notifObj) notifObj.dismiss();
                                    notifModel.remove(index);
                                } else {
                                    console.log("Activating notification: " + summary);
                                    if (notifObj && notifObj.actions) {
                                        for (let i = 0; i < notifObj.actions.length; i++) {
                                            if (notifObj.actions[i].identifier === "default") {
                                                notifObj.actions[i].invoke();
                                                break;
                                            }
                                        }
                                    }
                                    notifModel.remove(index);
                                    root.expanded = false;
                                }
                            }
                        }
                    }
                }
                
                Text {
                    visible: listView.count === 0
                    text: "No new notifications"
                    color: Theme.text
                    Layout.alignment: Qt.AlignCenter
                    opacity: 0.5
                }
            }
        }
        
        Shortcut {
            sequence: "Escape"
            onActivated: root.expanded = false
        }
    }
}
