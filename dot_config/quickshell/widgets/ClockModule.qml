import QtQuick
import Quickshell
import "../theme"

// View over the shell-level SharedClock source (S-005c): no Timer. The bar
// binds `clockSource`; text follows the shared `now` (HH:mm ticks every
// second) or the shared `dayDate` (date segments advance on day change).
Text {
    id: root
    property string format: "hh:mm"
    property var clockSource: null
    color: Theme.text
    font.pixelSize: 12

    text: {
        if (root.clockSource) return root.clockSource.textFor(root.format);
        return Qt.formatDateTime(new Date(), root.format);
    }
}
