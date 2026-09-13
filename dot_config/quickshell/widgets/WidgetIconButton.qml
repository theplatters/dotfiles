import QtQuick
import QtQuick.Controls
import "../theme"

// Shared icon-only button for tab, panel, close, and history controls.
// Keeps a 44px hit target with a centered 22px monochrome SVG glyph, a
// readable tooltip on hover/focus, and theme hover/pressed/checked/
// disabled plus keyboard-focus styling. The text property is retained for
// accessibility and state bindings even though only the icon is shown.
Button {
    id: root

    // Local SVG asset rendered through Image (monochrome strokes, no
    // custom colorization or external icon dependencies).
    property url iconSource
    property string tooltipText: text
    // Glyph-only rotation for indicators (e.g. tray chevron): rotating the
    // inner Image keeps the 32px Button background contained at 45/90/135deg,
    // where rotating the whole Button would let the square corners protrude.
    // Default 0 preserves every existing non-rotating control.
    property real iconRotation: 0
    // Border toggle for icon buttons embedded in a shared capsule (e.g.
    // tray chevron). Default true preserves the border on all icons.
    property bool borderVisible: true

    focusPolicy: Qt.StrongFocus
    implicitWidth: Math.max(Theme.iconButtonSize, contentItem.implicitWidth + 16)
    implicitHeight: Math.max(Theme.iconButtonSize, contentItem.implicitHeight + 16)
    topPadding: 8
    bottomPadding: 8
    leftPadding: 8
    rightPadding: 8

    font.family: Theme.fontFamily

    Accessible.name: tooltipText
    ToolTip.text: tooltipText
    ToolTip.visible: tooltipText !== "" && (hovered || activeFocus)
    ToolTip.delay: 400

    contentItem: Item {
        implicitWidth: Theme.iconSize
        implicitHeight: Theme.iconSize
        Image {
            objectName: "iconImage"
            anchors.centerIn: parent
            width: Theme.iconSize
            height: Theme.iconSize
            source: root.iconSource
            fillMode: Image.PreserveAspectFit
            smooth: true
            sourceSize.width: 44
            sourceSize.height: 44
            rotation: root.iconRotation
            transformOrigin: Item.Center
            opacity: !root.enabled ? 0.35
                : (root.checked || root.hovered || root.pressed ? 1.0 : 0.8)
        }
    }

    background: Rectangle {
        color: {
            if (!root.enabled) return Theme.mantle
            if (root.pressed) return Theme.surface2
            if (root.checked) return Theme.surface1
            if (root.hovered) return Theme.surface1
            return Theme.mantle
        }
        radius: Theme.controlRadius
        border.width: root.borderVisible ? 1 : 0
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
