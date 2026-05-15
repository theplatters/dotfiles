import QtQuick
import QtQuick.Controls
import "../theme"

Button {
    id: control
    
    contentItem: Text {
        text: control.text
        font.pixelSize: 14
        font.bold: true
        color: Theme.base
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        leftPadding: 10
        rightPadding: 10
    }
    
    scale: control.hovered ? 1.05 : 1.0
    
    Behavior on scale {
        NumberAnimation { duration: 100; easing.type: Easing.OutQuad }
    }
    
    background: Rectangle {
        implicitWidth: 80
        implicitHeight: 38
        color: control.pressed ? Theme.pink : Theme.mauve
        radius: 8
        
        // Depth effect / Shadow
        Rectangle {
            anchors.bottom: parent.bottom
            width: parent.width
            height: 4
            color: Qt.darker(parent.color, 1.2)
            radius: parent.radius
            z: -1
        }
        
        Behavior on color {
            ColorAnimation { duration: 150 }
        }
    }
}
