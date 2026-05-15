import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Widgets
import Quickshell.Services.SystemTray

Item {
    id: root
    width: layout.implicitWidth
    height: layout.implicitHeight

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
                
                MouseArea {
                    anchors.fill: parent
                    acceptedButtons: Qt.LeftButton | Qt.RightButton | Qt.MiddleButton
                    onClicked: (mouse) => {
                        if (mouse.button === Qt.LeftButton) {
                            const pos = trayIcon.mapToItem(null, mouse.x, mouse.y);
                            modelData.display(QsWindow.window, pos.x, pos.y);
                        } else if (mouse.button === Qt.MiddleButton) {
                            modelData.secondaryActivate();
                        } else if (mouse.button === Qt.RightButton) {
                            modelData.activate();
                        }
                    }
                }
            }
        }
    }
}
