import QtQuick
import QtQuick.Controls
import "../theme"

Button {
    id: control
    text: ""
    property alias iconText: icon.text
    
    background: Rectangle {
        implicitWidth: 40
        implicitHeight: 40
        color: control.hovered ? Theme.surface0 : "transparent"
        radius: 5
    }
    
    contentItem: Text {
        id: icon
        text: control.text
        font.pixelSize: 20
        color: Theme.text
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
    }
}
