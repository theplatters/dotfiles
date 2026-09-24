import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Wayland
import "../theme"

// Calendar + daily agenda popout anchored to the bar clock. The shared
// DailyAgenda state lives in shell.qml, so closing this popup never
// stops the Pomodoro timer or drops a completion draft.
//
// PanelWindow (not PopupWindow) so the embedded DailyPlanner TextFields
// (focusField/breakField/pickerSearch) and TextArea (completionNote) can
// receive keyboard input. Layer-shell OnDemand focus preserves
// click-to-focus: opening never steals focus or auto-focuses the search;
// closing disables focus. Small floating surface: top+left anchored with
// screen-local margins derived from the clock item inside the bar window.
PanelWindow {
    id: root

    property var anchorItem: null
    // Stable bar ancestor for the TransformWatcher (bound to the Bar in
    // shell.qml). refreshPosition already tracks the clock item's own
    // geometry; the watcher additionally fires on ancestor motion that
    // leaves the clock's own x/y/width/height untouched.
    property var anchorScope: null
    property var agenda: null
    property bool requestedOpen: false
    property bool closing: false
    // Single reveal progress (ControlCenter pattern): panel chrome
    // derives from it so an interrupted close reopens mid-fade.
    property real reveal: 0
    // Screen-local placement, refreshed on open and on clock/screen
    // changes (defaults: maximum 430x640 card just under the clock).
    property int popupX: 8
    property int popupY: 48
    property int popupWidth: 430
    property int popupHeight: 640

    signal projectPlanningRequested(string projectId, string action, string message)

    // The bar window hosting the clock item; its screen keeps the popout
    // on the same monitor as the clock in multi-screen setups.
    readonly property var anchorWindow: anchorItem ? anchorItem.QsWindow.window : null
    screen: anchorWindow && anchorWindow.screen ? anchorWindow.screen : (Quickshell.screens.length > 0 ? Quickshell.screens[0] : null)

    anchors {
        top: true
        left: true
    }
    margins {
        left: root.popupX
        top: root.popupY
    }

    implicitWidth: popupWidth
    implicitHeight: popupHeight
    color: Theme.transparent
    visible: false

    // Non-reserving overlay: never shifts tiled windows, like the working
    // PasswordPopup/ProjectPlanner/CommandPalette surfaces.
    exclusionMode: ExclusionMode.Ignore
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: root.requestedOpen && !root.closing ? WlrKeyboardFocus.OnDemand : WlrKeyboardFocus.None

    // Placement math (pure functions over numbers, unit-tested in
    // tests/test_daily_planner_ui.py). The bar is top/left/right anchored
    // with zero margins, so the clock position mapped into the bar window
    // content is already screen-local: no global-origin subtraction, which
    // would break monitors with a nonzero origin.
    function clampPopupX(anchorX, screenWidth, width) {
        var margin = 8;
        var maxX = Math.max(margin, screenWidth - width - margin);
        return Math.max(margin, Math.min(Math.round(anchorX), maxX));
    }

    function popupWidthFor(screenWidth) {
        var margin = 8;
        var room = screenWidth - margin * 2;
        if (room <= 0) return Math.max(0, Math.round(screenWidth));
        return Math.round(Math.min(430, room));
    }

    function popupHeightFor(anchorBottom, screenHeight) {
        var gap = 8, margin = 8, minH = 200;
        var room = screenHeight - margin * 2;
        if (room < minH) return Math.max(0, Math.round(room));
        var below = screenHeight - anchorBottom - gap - margin;
        if (below >= 640) return 640;
        if (below >= minH) return Math.round(below);
        return Math.round(Math.min(640, room));
    }

    function popupTopFor(anchorBottom, screenHeight, height) {
        var gap = 8, margin = 8;
        var top = anchorBottom + gap;
        var maxTop = Math.max(margin, screenHeight - height - margin);
        return Math.max(margin, Math.min(Math.round(top), maxTop));
    }

    // Recompute screen-local placement from the clock item. Called on
    // open and whenever the clock or screen geometry changes.
    function refreshPosition() {
        var bar = root.anchorWindow;
        var s = root.screen;
        if (!root.anchorItem || !bar || !bar.contentItem || !s) return;
        var sw = Number(s.width), sh = Number(s.height);
        if (!isFinite(sw) || !isFinite(sh) || sw <= 0 || sh <= 0) return;
        var local = root.anchorItem.mapToItem(bar.contentItem, 0, 0);
        var anchorBottom = local.y + root.anchorItem.height;
        var width = root.popupWidthFor(sw);
        var height = root.popupHeightFor(anchorBottom, sh);
        root.popupWidth = width;
        root.popupHeight = height;
        root.popupX = root.clampPopupX(local.x, sw, width);
        root.popupY = root.popupTopFor(anchorBottom, sh, height);
    }

    onAnchorItemChanged: root.refreshPosition()
    onScreenChanged: root.refreshPosition()

    Connections {
        target: root.anchorItem
        enabled: !!root.anchorItem
        ignoreUnknownSignals: true
        function onXChanged() { root.refreshPosition(); }
        function onYChanged() { root.refreshPosition(); }
        function onWidthChanged() { root.refreshPosition(); }
        function onHeightChanged() { root.refreshPosition(); }
    }

    Connections {
        target: root.screen
        ignoreUnknownSignals: true
        function onWidthChanged() { root.refreshPosition(); }
        function onHeightChanged() { root.refreshPosition(); }
    }

    // PanelWindow variant of the anchor grammar (docs/control-center.md):
    // screen-local margins derived item-relative from the clock; the
    // watcher covers ancestor motion like ControlCenter's TransformWatcher.
    TransformWatcher {
        id: anchorWatcher
        a: root.anchorScope ? root.anchorScope : root.anchorItem
        b: root.anchorItem
        onTransformChanged: root.refreshPosition()
    }

    function setOpen(open) {
        requestedOpen = open;
        if (open) {
            closing = false;
            exitMotion.stop();
            root.refreshPosition();
            if (!visible) visible = true;
            // Already open and steady: keep the frame (no reset/flash).
            if (root.reveal >= 0.99 && enterMotion.running === false) {
                if (planner) planner.syncViewToDate();
                if (agenda && !agenda.agendaBusy && !agenda.completionSaving) agenda.reload();
                return;
            }
            // Otherwise reverse from the current reveal value. Never
            // assign a fresh start while partially visible.
            if (planner) planner.syncViewToDate();
            if (agenda && !agenda.agendaBusy && !agenda.completionSaving) agenda.reload();
            enterMotion.restart();
        } else if (visible && !closing) {
            closing = true;
            enterMotion.stop();
            exitMotion.restart();
        }
    }

    function toggle(item) {
        if (item) anchorItem = item;
        setOpen(!requestedOpen);
    }

    Shortcut {
        sequence: "Escape"
        onActivated: root.setOpen(false)
    }

    Rectangle {
        id: panel
        anchors.fill: parent
        color: Theme.base
        radius: Theme.cardRadius
        border.color: Theme.border
        border.width: 1
        // Reveal-driven chrome (ControlCenter pattern): opacity + scale
        // derive from the single progress; content slides via transform.
        opacity: root.reveal
        scale: 0.96 + 0.04 * root.reveal
        transformOrigin: Item.Top
        clip: true
        visible: root.reveal > 0.01 || root.visible

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 14
            spacing: 10
            transform: Translate { y: (1 - root.reveal) * -8 }

            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Text {
                    text: "Calendar"
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: 16
                    font.bold: true
                    Layout.fillWidth: true
                }
                WidgetIconButton {
                    text: "Close"
                    iconSource: "icons/x.svg"
                    tooltipText: "Close"
                    Accessible.name: "Close calendar"
                    onClicked: root.setOpen(false)
                }
            }

            // The card height is clamped to the space below the clock on
            // short screens; overflow scrolls here instead of leaving
            // the screen.
            Flickable {
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                contentWidth: width
                contentHeight: planner.implicitHeight
                boundsBehavior: Flickable.StopAtBounds
                DailyPlannerPane {
                    id: planner
                    width: parent.width
                    agenda: root.agenda
                    // Slim popout set (§6.3): month grid, Scheduled,
                    // picker, Pomodoro, quick-add. Captured/Review/
                    // SessionCard live in the planner Daily tab only.
                    compact: true
                    onProjectPlanningRequested: (projectId, action, message) => root.projectPlanningRequested(projectId, action, message)
                }
            }
        }
    }

    // Single-progress motion (ControlCenter pattern): enter/exit animate
    // reveal to 1/0 from the current value (interrupted close reopens
    // mid-fade, never resets). No spring/overshoot: OutCubic in, InCubic
    // out.
    NumberAnimation {
        id: enterMotion
        target: root
        property: "reveal"
        to: 1
        duration: Theme.motionPanel
        easing.type: Easing.OutCubic
    }

    NumberAnimation {
        id: exitMotion
        target: root
        property: "reveal"
        to: 0
        duration: Theme.motionExit
        easing.type: Easing.InCubic
        onFinished: {
            if (!root.requestedOpen) root.visible = false;
            root.closing = false;
        }
    }
}
