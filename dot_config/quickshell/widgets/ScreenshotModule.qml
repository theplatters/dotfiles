import QtQuick
import Quickshell
import "../theme"

// Bar screenshot entry: left = region, right = output. Capture itself is
// owned by the shared ScreenshotAction helper (S-017), so the bar gets the
// same unmap/delay semantics as the palette (180 ms debounce, setsid hyprshot
// process, generation-guarded trigger). The palette keeps its own delegates;
// this module only schedules. No new menus: the two existing gestures stay.
Rectangle {
    id: root
    height: 32
    width: 32
    radius: Theme.controlRadius
    border.color: Theme.border
    border.width: 1
    color: Theme.mantle

    ScreenshotAction {
        id: screenshotAction
        // The bar has no palette to unmap; the request is a no-op here.
        // Blocked only while a capture is in flight (owned internally).
        blocked: false
    }

    Text {
        anchors.centerIn: parent
        text: "󰹑"
        color: Theme.subtext1
        font.family: Theme.iconFontFamily
        font.pixelSize: Theme.iconSizeSmall
    }

    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        onClicked: (mouse) => {
            if (mouse.button === Qt.RightButton) {
                screenshotAction.scheduleScreenshot("output")
            } else {
                screenshotAction.scheduleScreenshot("region")
            }
        }
        cursorShape: Qt.PointingHandCursor
        hoverEnabled: true
        onEntered: root.color = Theme.surface0
        onExited: root.color = Theme.mantle
    }
}
