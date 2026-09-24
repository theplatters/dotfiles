import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Services.Notifications
import "../theme"

// View over the shell-level NotificationHistory store (S-002): badge count,
// popup list, Clear All, and row actions. All tracking, eviction, and
// closed-handler logic lives in widgets/NotificationHistory.qml so bells on
// different monitors share one history.
Item {
    id: root
    implicitWidth: 32
    implicitHeight: 32

    // Shell-level owner (required). Kept for backcompat wiring; the history
    // store already holds the server.
    property var notifServer: null
    property var history: null
    property bool expanded: false

    readonly property int historyCount: root.history ? root.history.count : 0

    Rectangle {
        id: bellContainer
         height: 32; width: 32; radius: Theme.controlRadius; color: Theme.mantle
         border.color: Theme.border
         border.width: 1

        Text {
            anchors.centerIn: parent
            text: "󰂚"
            color: root.historyCount > 0 ? Theme.text : Theme.subtext1
            font.family: Theme.iconFontFamily
            font.pixelSize: Theme.iconSizeSmall
        }

        Rectangle {
            visible: root.historyCount > 0
             width: 14; height: 14; radius: Theme.chipRadius; color: Theme.surface2
            anchors.top: parent.top; anchors.right: parent.right
            anchors.topMargin: -2; anchors.rightMargin: -2

            Text {
                text: root.historyCount
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
        color: Theme.transparent

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
                                if (root.history) root.history.clearAll();
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
                    model: root.history ? root.history.model : null

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
                                if (!root.history) return;
                                if (mouse.button === Qt.RightButton) {
                                    console.log("Dismissing notification: " + summary);
                                    root.history.dismissAt(index);
                                } else {
                                    console.log("Activating notification: " + summary);
                                    var outcome = root.history.activateAt(index);
                                    // The view only collapses the popup;
                                    // removal/retention is the store's job
                                    // (closed removes nonresident rows;
                                    // resident rows persist by design).
                                    if (outcome === "invoked" || outcome === "dismissed") root.expanded = false;
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
