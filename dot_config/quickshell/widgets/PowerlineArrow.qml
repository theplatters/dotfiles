import QtQuick
import Quickshell
import "../theme"

Item {
    id: root
    property bool isLeft: true
    property color arrowColor: Theme.mantle
    property color bgColor: "transparent"
    
    width: arrowText.implicitWidth
    height: 24

    Rectangle {
        anchors.fill: parent
        color: root.bgColor
    }

    Text {
        id: arrowText
        anchors.fill: parent
        text: root.isLeft ? "" : ""
        color: root.arrowColor
        font.pixelSize: 24 // Adjusted for better fit
        verticalAlignment: Text.AlignVCenter
        horizontalAlignment: Text.AlignHCenter
    }
}
