import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Widgets
import Quickshell.Services.SystemTray
import "../theme"

Item {
    id: root
    implicitWidth: layout.implicitWidth
    implicitHeight: layout.implicitHeight
    width: implicitWidth
    height: implicitHeight

    Row {
        id: layout
        spacing: 8
        anchors.verticalCenter: parent.verticalCenter

        Repeater {
            model: SystemTray.items

            Item {
                id: delegate
                // StatusNotifier Status enum values are Passive 0, Active 1,
                // NeedsAttention 2. Compared numerically because the enum
                // type is not exposed as a QML value under this import.
                readonly property bool isPassive: !modelData || modelData.status === 0
                readonly property string accessibleLabel: (modelData && (modelData.title || modelData.id)) || "Tray icon"
                readonly property string fallbackLetter: {
                    var s = (modelData && (modelData.title || modelData.id)) || "?";
                    return s.charAt(0).toUpperCase();
                }

                width: isPassive ? 0 : 20
                implicitWidth: isPassive ? 0 : 20
                height: 32
                implicitHeight: 32
                visible: !isPassive

                function openMenu(mouse) {
                    if (!modelData || (!modelData.onlyMenu && !modelData.hasMenu)) {
                        return;
                    }
                    var win = QsWindow.window;
                    if (!win) {
                        return;
                    }
                    var mx = (mouse && mouse.x !== undefined) ? mouse.x : clickArea.width / 2;
                    var my = (mouse && mouse.y !== undefined) ? mouse.y : clickArea.height / 2;
                    if (win.contentItem) {
                        var p = clickArea.mapToItem(win.contentItem, mx, my);
                        modelData.display(win, p.x, p.y);
                    } else {
                        var q = clickArea.mapToItem(null, mx, my);
                        modelData.display(win, q.x, q.y);
                    }
                }

                // Hover highlight behind the icon. Plain rect: no input
                // handling, so sibling hit areas are never affected.
                Rectangle {
                    id: hoverBg
                    width: 28
                    height: 32
                    anchors.centerIn: parent
                    radius: Theme.chipRadius
                    color: Theme.surface0
                    opacity: clickArea.containsMouse ? 1 : 0
                    Behavior on opacity {
                        NumberAnimation {
                            duration: Theme.motionFast
                            easing.type: Easing.OutCubic
                        }
                    }
                }

                // Small attention dot for NeedsAttention (status === 2).
                // Plain paint: no input, never resizes the row.
                Rectangle {
                    width: 6
                    height: 2
                    radius: height / 2
                    anchors.bottom: parent.bottom
                    anchors.bottomMargin: 4
                    anchors.horizontalCenter: parent.horizontalCenter
                    color: Theme.text
                    visible: modelData && modelData.status === 2
                }

                // Letter fallback so a broken or empty theme icon name
                // never renders as blank space. Hidden once the real
                // icon loads. Also shown while loading so async load
                // never flashes blank.
                Text {
                    id: fallback
                    anchors.centerIn: parent
                    text: delegate.fallbackLetter
                    color: Theme.subtext1
                    font.pixelSize: 13
                    font.weight: Font.Medium
                    font.family: Theme.fontFamily
                    visible: !modelData || !modelData.icon || trayIcon.status === Image.Error || trayIcon.status === Image.Null || trayIcon.status === Image.Loading
                }

                IconImage {
                    id: trayIcon
                    width: 20
                    height: 20
                    anchors.centerIn: parent
                    source: modelData ? modelData.icon : ""
                    asynchronous: true
                    mipmap: true
                }

                MouseArea {
                    id: clickArea
                    // 28x32 touch target centered on the 20px visual. It
                    // reaches 4px into the 8px row gaps on each side, so
                    // neighbours exactly meet without overlapping.
                    width: 28
                    height: 32
                    anchors.centerIn: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    acceptedButtons: Qt.LeftButton | Qt.RightButton | Qt.MiddleButton
                    Accessible.name: delegate.accessibleLabel
                    Accessible.role: Accessible.Button
                    Accessible.onPressAction: {
                        if (!modelData) {
                            return;
                        }
                        if (modelData.onlyMenu && modelData.hasMenu) {
                            delegate.openMenu(null);
                        } else {
                            modelData.activate();
                        }
                    }
                    onClicked: (mouse) => {
                        if (!modelData) {
                            return;
                        }
                        if (mouse.button === Qt.LeftButton) {
                            if (modelData.onlyMenu && modelData.hasMenu) {
                                delegate.openMenu(mouse);
                            } else {
                                modelData.activate();
                            }
                        } else if (mouse.button === Qt.MiddleButton) {
                            modelData.secondaryActivate();
                        } else if (mouse.button === Qt.RightButton) {
                            if (modelData.onlyMenu || modelData.hasMenu) {
                                delegate.openMenu(mouse);
                            } else {
                                modelData.activate();
                            }
                        }
                    }
                }
            }
        }
    }
}
