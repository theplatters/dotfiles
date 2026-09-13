import QtQuick
import QtQuick.Controls
import "../theme"

// Shared text button for session controls (New/Rename/Restore) and other
// planner/journal actions. Keeps a consistent 40-44px touch height,
// generous horizontal padding, theme font/radius/motion, and visible
// hover/pressed/checked/disabled plus keyboard-focus styling.
Button {
    id: root

    focusPolicy: Qt.StrongFocus
    topPadding: 10
    bottomPadding: 10
    leftPadding: 16
    rightPadding: 16
    implicitHeight: Math.max(Theme.controlMinHeight, contentItem.implicitHeight + 20)

    font.family: Theme.fontFamily

    contentItem: Text {
        text: root.text
        color: root.enabled ? Theme.text : Theme.subtext0
        font.family: Theme.fontFamily
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
        opacity: root.enabled ? 1.0 : 0.55
    }

    background: Rectangle {
        color: {
            if (!root.enabled) return Theme.mantle
            if (root.pressed) return Theme.surface2
            if (root.checked) return Theme.surface1
            if (root.hovered) return Theme.surface1
            return Theme.base
        }
        radius: Theme.controlRadius
        border.width: 1
        border.color: {
            if (!root.enabled) return Theme.border
            if (root.activeFocus) return Theme.focusBorder
            if (root.checked) return Theme.focusBorder
            if (root.hovered) return Theme.accentMuted
            return Theme.border
        }
        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
    }
}
