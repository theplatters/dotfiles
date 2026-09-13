import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Widgets
import Quickshell.Services.SystemTray

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
            
            IconImage {
                id: trayIcon
                width: 20
                height: 20
                source: modelData.icon
                anchors.verticalCenter: parent.verticalCenter
                
                MouseArea {
                    anchors.fill: parent
                    acceptedButtons: Qt.LeftButton | Qt.RightButton | Qt.MiddleButton
                    onClicked: (mouse) => {
                        if (mouse.button === Qt.LeftButton) {
                            modelData.activate();
                        } else if (mouse.button === Qt.MiddleButton) {
                            modelData.secondaryActivate();
                        } else if (mouse.button === Qt.RightButton) {
                            const pos = trayIcon.mapToItem(null, mouse.x, mouse.y);
                            modelData.display(QsWindow.window, pos.x, pos.y);
                        }
                    }
                }
            }
        }
    }
}
