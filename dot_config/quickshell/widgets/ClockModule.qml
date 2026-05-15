import QtQuick
import Quickshell
import "../theme"

Text {
    id: root
    property string format: "hh:mm"
    color: Theme.text
    font.pixelSize: 12
    
    function updateTime() {
        text = Qt.formatDateTime(new Date(), root.format);
    }

    Timer {
        interval: 1000
        running: true
        repeat: true
        onTriggered: root.updateTime()
    }

    Component.onCompleted: updateTime()
}
