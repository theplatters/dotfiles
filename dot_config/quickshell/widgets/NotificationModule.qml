import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Services.Notifications
import "../theme"

Item {
    id: root
    implicitWidth: 32
    implicitHeight: 32
    
    property var notifServer: null
    property bool expanded: false

    ListModel {
        id: notifModel
    }

    Connections {
        target: root.notifServer
        enabled: !!root.notifServer
        
        function onNotification(n) {
            // Check if it's an update to an existing notification
            for (let i = 0; i < notifModel.count; i++) {
                if (notifModel.get(i).notifId === n.id) {
                    notifModel.setProperty(i, "summary", n.summary);
                    notifModel.setProperty(i, "body", n.body);
                    notifModel.setProperty(i, "appName", n.appName);
                    return;
                }
            }
            
            // New notification: history is the sole tracking owner. Banners
            // hold bare references and never set tracked (tracked=false is
            // equivalent to dismiss()).
            n.tracked = true;
            n.closed.connect((reason) => {
                // Sole-closure path: remote close or our own dismiss() lands
                // here. Remove model-only; never touch tracked (already
                // closed). No-op if an explicit action already removed it.
                for (let i = 0; i < notifModel.count; i++) {
                    if (notifModel.get(i).notifId === n.id) {
                        notifModel.remove(i);
                        break;
                    }
                }
            });

            console.log("New notification: " + n.summary);
            
            notifModel.insert(0, {
                "notifId": n.id,
                "summary": n.summary,
                "body": n.body,
                "appName": n.appName,
                "notifObj": n
            });
            
            if (notifModel.count > 10) {
                // Evict oldest: capture the tracked object BEFORE removal
                // (the row dict is invalid after remove), remove from the
                // model first, then release tracking. The closed handler
                // then finds nothing (no double remove of a shifted index).
                var evictObj = notifModel.get(10).notifObj;
                notifModel.remove(10);
                if (evictObj) evictObj.dismiss();
            }
        }
    }

    Rectangle {
        id: bellContainer
         height: 32; width: 32; radius: Theme.controlRadius; color: Theme.mantle
         border.color: Theme.border
         border.width: 1
        
        Text {
            anchors.centerIn: parent
            text: "󰂚"
            color: notifModel.count > 0 ? Theme.text : Theme.subtext1
            font.pixelSize: 18
        }
        
        Rectangle {
            visible: notifModel.count > 0
             width: 14; height: 14; radius: 7; color: Theme.surface2
            anchors.top: parent.top; anchors.right: parent.right
            anchors.topMargin: -2; anchors.rightMargin: -2
            
            Text {
                text: notifModel.count
                 color: Theme.text
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

    PopupWindow {
        id: popup
        visible: root.expanded
        
        anchor {
            window: QsWindow.window
            rect: Qt.rect(bellContainer.mapToGlobal(0, 0).x, bellContainer.mapToGlobal(0, 0).y + bellContainer.height + 8, bellContainer.width, 1)
            gravity: Edges.Bottom
        }
        
        implicitWidth: 350
        implicitHeight: Math.min(500, mainLayout.implicitHeight + 20)
        color: "transparent"
        
        Rectangle {
            anchors.fill: parent
            color: Theme.base
             radius: Theme.cardRadius
             border.color: Theme.border
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
                        color: Theme.text
                        font.pixelSize: 12
                        
                        MouseArea {
                            anchors.fill: parent
                            onClicked: {
                                // Collect first, clear the model, then dismiss:
                                // each dismiss() re-enters the closed handler,
                                // which finds nothing (no double remove, no
                                // skipped untracked rows).
                                let pending = [];
                                for (let i = 0; i < notifModel.count; ++i) {
                                    let item = notifModel.get(i);
                                    if (item && item.notifObj) pending.push(item.notifObj);
                                }
                                notifModel.clear();
                                for (let j = 0; j < pending.length; ++j) {
                                    pending[j].dismiss();
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
                         radius: Theme.controlRadius
                         border.color: Theme.border
                         border.width: 1
                        
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
                                    color: Theme.subtext1
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
                                    // Explicit dismissal: capture first, remove,
                                    // then dismiss so closed finds nothing.
                                    var dismissTarget = notifObj;
                                    notifModel.remove(index);
                                    if (dismissTarget) dismissTarget.dismiss();
                                } else {
                                    console.log("Activating notification: " + summary);
                                    // Capture everything BEFORE invoking: the
                                    // action may synchronously close (and
                                    // destroy) the notification, which removes
                                    // the row via closed. Touching index or
                                    // target afterwards would double-remove or
                                    // use a destroyed object.
                                    var activateTarget = notifObj;
                                    var defaultAction = null;
                                    var isResident = activateTarget && activateTarget.resident === true;
                                    if (activateTarget && activateTarget.actions) {
                                        for (let i = 0; i < activateTarget.actions.length; i++) {
                                            if (activateTarget.actions[i].identifier === "default") {
                                                defaultAction = activateTarget.actions[i];
                                                break;
                                            }
                                        }
                                    }
                                    if (defaultAction) {
                                        // Activation, not dismissal: invoke and
                                        // let closed remove nonresident rows;
                                        // resident rows persist by design.
                                        defaultAction.invoke();
                                        root.expanded = false;
                                    } else {
                                        // No default action: explicit dismiss,
                                        // remove-first so closed is a no-op.
                                        notifModel.remove(index);
                                        root.expanded = false;
                                        if (activateTarget) activateTarget.dismiss();
                                    }
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
