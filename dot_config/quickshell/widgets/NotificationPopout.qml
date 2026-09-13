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
        left: true
    }
    
    margins {
        top: 50
        left: 15
    }
    
    implicitWidth: 350
    implicitHeight: mainLayout.implicitHeight
    color: "transparent"
    
    // DND: ControlCenter toggles notifServer.inhibit; banners hide while set.
    // History in NotificationModule still collects independently.
    readonly property bool inhibited: !!root.notifServer && root.notifServer.inhibit === true

    // Make sure we don't block clicks when empty or inhibited
    visible: activeNotifs.count > 0 && !root.inhibited
    
    ListModel {
        id: activeNotifs
    }

    Connections {
        target: root.notifServer
        enabled: !!root.notifServer
        
        function onNotification(n) {
            // DND suppresses incoming banners; history (NotificationModule)
            // is the sole tracking owner and still collects independently.
            if (root.inhibited) return;
            // Check for updates to existing notification
            for (let i = 0; i < activeNotifs.count; i++) {
                if (activeNotifs.get(i).notifId === n.id) {
                    activeNotifs.setProperty(i, "summary", n.summary);
                    activeNotifs.setProperty(i, "body", n.body);
                    activeNotifs.setProperty(i, "appName", n.appName);
                    return;
                }
            }

            // Banner holds a bare reference only: never set tracked, never
            // call dismiss/expire here. History owns tracking and closure;
            // banner expiry/hiding only removes the banner row.
            n.closed.connect((reason) => {
                root.removeBanner(n.id);
            });

            // Add to active notifications
            activeNotifs.append({
                "notifId": n.id,
                "summary": n.summary,
                "body": n.body,
                "appName": n.appName,
                "notifObj": n
            });
        }
    }
    
    // Model-only removal: banner expiry, DND hiding, and remote closure must
    // not untrack or dismiss history. Clearing tracked is equivalent to
    // dismiss(), so never touch tracked here.
    function removeBanner(id) {
        for (let i = 0; i < activeNotifs.count; i++) {
            if (activeNotifs.get(i).notifId === id) {
                activeNotifs.remove(i);
                break;
            }
        }
    }

    // Explicit user action: dismiss the notification itself so history (sole
    // tracking owner) also closes via its closed handler. Remove the banner
    // row optimistically; the closed handler then finds nothing (no double
    // remove).
    function userDismiss(id) {
        for (let i = 0; i < activeNotifs.count; i++) {
            if (activeNotifs.get(i).notifId === id) {
                let n = activeNotifs.get(i).notifObj;
                activeNotifs.remove(i);
                if (n) n.dismiss();
                break;
            }
        }
    }

    function clearBanners() {
        // Model only: hiding banners (e.g. DND) never dismisses history.
        for (let i = activeNotifs.count - 1; i >= 0; --i) {
            activeNotifs.remove(i);
        }
    }

    onInhibitedChanged: {
        if (root.inhibited) root.clearBanners();
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
                 border.color: Theme.border
                border.width: 1
                
                // Entry animation
                opacity: 0
                Component.onCompleted: opacity = 1
                 Behavior on opacity { NumberAnimation { duration: Theme.motionPanel; easing.type: Easing.OutCubic } }
                
                Timer {
                    id: dismissTimer
                    interval: 6000
                    running: !mouseArea.containsMouse
                    // Expiry hides the banner only; history retains it.
                    onTriggered: root.removeBanner(notifId)
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
                            color: Theme.subtext1
                            font.pixelSize: 10
                            font.italic: true
                        }
                        
                        Text {
                            text: "󰅖"
                            color: Theme.red
                            font.family: Theme.iconFontFamily
                            font.pixelSize: 14
                            MouseArea {
                                anchors.fill: parent
                                onClicked: root.userDismiss(notifId)
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
                        let target = notifObj;
                        if (target && target.actions) {
                            for (let i = 0; i < target.actions.length; i++) {
                                if (target.actions[i].identifier === "default") {
                                    target.actions[i].invoke();
                                    break;
                                }
                            }
                        }
                        // Explicit activation dismisses (history closes too).
                        root.userDismiss(notifId);
                    }
                }
            }
        }
    }
}
