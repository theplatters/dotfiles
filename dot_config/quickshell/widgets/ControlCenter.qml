import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import Quickshell
import Quickshell.Services.UPower
import "../theme"

// Unified control center anchored to the tray area.
// Entry points (audio/network/battery/tray button) route into selectedTab;
// the anchor stays fixed on the tray item even when opened elsewhere.
PopupWindow {
    id: root

    property var anchorItem: null
    property var barWindow: null
    // Stable bar ancestor for the TransformWatcher (bound to the Bar in
    // shell.qml). Lets the popup follow tray motion caused by any ancestor
    // in the Bar -> tray path, not just the tray item itself.
    property var trayScope: null
    property var notifServer: null
    property var passwordPopup: null
    property int selectedTab: 0 // 0 Audio, 1 Network, 2 Bluetooth, 3 Power
    property bool requestedOpen: false
    property bool closing: false
    // Diagnostic: how many times the anchor rect was recalculated. Used by
    // the live open-move harness to prove tracking actually runs.
    property int anchorUpdates: 0
    // Single reveal progress (0 hidden .. 1 shown). Panel opacity, top-origin
    // scale and content slide all derive from it, so interrupted close/reopen
    // reverses from the current value instead of flashing from zero.
    property real reveal: 0

    readonly property bool dndEnabled: !!root.notifServer && root.notifServer.inhibit === true

    // Screen-bound sizing: anchor screen logical size minus the bar strip
    // (40px bar + 4px anchor gap) and a small safety margin. Keeps header +
    // quick controls reachable on short screens; the body Flickable scrolls.
    // Supported minimum is an ordinary 368x424 logical screen: below that the
    // 320x360 floors hold (popup may then exceed the screen rather than
    // collapse chrome), which is acceptable and documented here.
    readonly property var anchorScreen: (root.screen !== null && root.screen !== undefined) ? root.screen : (Quickshell.screens.length > 0 ? Quickshell.screens[0] : null)
    readonly property int maxPopupWidth: anchorScreen ? Math.min(440, Math.max(320, anchorScreen.width - 48)) : 420
    readonly property int maxPopupHeight: anchorScreen ? Math.max(360, Math.min(640, anchorScreen.height - 40 - 4 - 16)) : 600
    // Deliberate viewport for the scroll body (what remains after chrome).
    readonly property int availableBodyHeight: bodyFlick.height > 0 ? bodyFlick.height : Math.max(180, maxPopupHeight - 300)

    visible: false
    implicitWidth: Math.min(420, maxPopupWidth)
    implicitHeight: Math.min(600, maxPopupHeight)
    color: Theme.transparent

    anchor {
        item: root.anchorItem
        edges: Edges.Bottom | Edges.Left
        gravity: Edges.Bottom | Edges.Right
        margins.top: 4
    }

    // The popup tracks the unified tray capsule while open. Quickshell only
    // computes the item-relative anchor rect when the popup is shown, so
    // every geometry path calls the verified anchor.updateAnchor() API to
    // recalculate it (a re-assignment guarded on item identity would be a
    // no-op for motion). Item-relative only, no global coords.
    function updateAnchor() {
        if (!root.anchorItem) return;
        root.anchor.updateAnchor();
        root.anchorUpdates++;
    }

    onAnchorItemChanged: root.updateAnchor()
    onScreenChanged: root.updateAnchor()

    // Watches the whole Bar -> tray geometry path: fires when the tray item
    // or any ancestor (rows, slots, bar) moves or resizes, including motion
    // that leaves the tray item's own x/y/width/height untouched.
    TransformWatcher {
        id: trayWatcher
        a: root.trayScope ? root.trayScope : root.anchorItem
        b: root.anchorItem
        onTransformChanged: root.updateAnchor()
    }

    Connections {
        target: root.anchorItem
        enabled: !!root.anchorItem
        ignoreUnknownSignals: true
        function onXChanged() { root.updateAnchor(); }
        function onYChanged() { root.updateAnchor(); }
        function onWidthChanged() { root.updateAnchor(); }
        function onHeightChanged() { root.updateAnchor(); }
    }

    // Persistent backend: lives outside any conditional Loader so brightness
    // polling, night-light discovery and idle inhibit survive tab switches.
    ControlCenterSystem {
        id: ccSystem
        active: root.requestedOpen
        inhibitWindow: root.barWindow
    }

    // One-way tab routing (S-011): CC owns selectedTab; the embedded
    // NetworkPanel is a slave. The mapping lives here only (network=0,
    // BT=1); the panel never writes back to selectedTab and its hidden
    // inner selector stays inert (showTabSelector: false).
    onSelectedTabChanged: {
        if (root.selectedTab === 1) netPanel.activeTab = 0;
        else if (root.selectedTab === 2) netPanel.activeTab = 1;
        bodyFlick.contentY = 0;
        netPanel.syncDiscovery();
    }

    onVisibleChanged: {
        netPanel.syncDiscovery();
    }

    function setOpen(open) {
        requestedOpen = open;
        if (open) {
            closing = false;
            exitMotion.stop();
            // Fresh anchor rect for this open (tray may have moved while
            // the popup was hidden, when nothing was tracking it).
            root.updateAnchor();
            if (!visible) visible = true;
            // Already open and steady: keep the frame (no reset/flash).
            // Section switches arrive via openSection -> selectedTab change;
            // scroll/discovery are handled there and by onSelectedTabChanged.
            if (reveal >= 0.99 && enterMotion.running === false) {
                bodyFlick.contentY = 0;
                netPanel.syncDiscovery();
                return;
            }
            // Otherwise reverse from the current reveal value: fully hidden
            // starts at the bottom, an interrupted close continues mid-fade.
            // Never assign a fresh start while partially visible.
            bodyFlick.contentY = 0;
            netPanel.syncDiscovery();
            enterMotion.restart();
        } else if (visible && !closing) {
            closing = true;
            netPanel.syncDiscovery();
            enterMotion.stop();
            exitMotion.restart();
        }
    }

    function toggle() {
        // Anchor stays fixed on the tray item; caller identity is ignored.
        setOpen(!requestedOpen);
    }

    function openSection(tab) {
        // Single click path for all four tabs: set the owned tab and
        // open; the onSelectedTabChanged handler routes into the panel.
        selectedTab = tab;
        setOpen(true);
    }

    function toggleSection(tab) {
        if (requestedOpen && selectedTab === tab) setOpen(false);
        else openSection(tab);
    }

    Shortcut {
        sequence: "Escape"
        onActivated: root.setOpen(false)
    }

    Rectangle {
        id: panel
        anchors.fill: parent
        color: Theme.mantle
        radius: Theme.cardRadius
        border.color: Theme.border
        border.width: 1
        // Reveal-driven chrome: top-left origin so the popup grows down from
        // the tray capsule. Opacity + scale derive from the single progress;
        // the content slides a short distance via transform (never an invalid
        // y animation on the anchored panel). Panel clips so the negative
        // start offset never paints outside the popup surface.
        opacity: root.reveal
        scale: 0.96 + 0.04 * root.reveal
        transformOrigin: Item.TopLeft
        clip: true
        visible: root.reveal > 0.01 || root.visible

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 12
            spacing: 10
            transform: Translate { y: (1 - root.reveal) * -8 }

            RowLayout {
                id: headerRow
                objectName: "headerRow"
                Layout.fillWidth: true
                spacing: 10

                Text {
                    text: "Control Center"
                    color: Theme.text
                    font.pixelSize: 16
                    font.bold: true
                    Layout.fillWidth: true
                }

                WidgetIconButton {
                    text: "Close"
                    iconSource: "icons/x.svg"
                    Accessible.name: "Close control center"
                    onClicked: root.setOpen(false)
                }
            }

            Rectangle {
                id: quickCard
                objectName: "quickCard"
                Layout.fillWidth: true
                Layout.preferredHeight: quickControls.implicitHeight + 20
                radius: Theme.controlRadius
                color: Theme.surface0
                border.color: Theme.border
                border.width: 1

                ColumnLayout {
                    id: quickControls
                    anchors.fill: parent
                    anchors.margins: 10
                    spacing: 8

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 10

                        Text {
                            text: "Brightness"
                            color: Theme.text
                            font.pixelSize: 12
                            font.bold: true
                        }

                        Slider {
                            id: brightnessSlider
                            Layout.fillWidth: true
                            from: 1
                            to: 100
                            stepSize: 1
                            value: ccSystem.brightness
                            // Availability only: the backend debounces and
                            // coalesces writes, so never gate on busy here
                            // (that would disable the control mid-drag).
                            enabled: ccSystem.brightnessAvailable
                            onMoved: ccSystem.setBrightness(value)
                        }

                        Text {
                            text: ccSystem.brightnessAvailable ? ccSystem.brightness + "%" : "N/A"
                            color: Theme.subtext0
                            font.pixelSize: 11
                            Layout.minimumWidth: 36
                            horizontalAlignment: Text.AlignRight
                        }
                    }

                    Text {
                        Layout.fillWidth: true
                        visible: ccSystem.brightnessError !== ""
                        text: ccSystem.brightnessError
                        color: Theme.subtext0
                        font.pixelSize: 10
                        elide: Text.ElideRight
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 6

                        WidgetButton {
                            text: root.dndEnabled ? "DND On" : "DND Off"
                            Layout.fillWidth: true
                            checkable: true
                            checked: root.dndEnabled
                            enabled: !!root.notifServer
                            onClicked: {
                                if (root.notifServer) root.notifServer.inhibit = !root.notifServer.inhibit;
                            }
                        }

                        WidgetButton {
                            text: ccSystem.nightLightEnabled ? "Night On" : "Night Off"
                            Layout.fillWidth: true
                            checkable: true
                            checked: ccSystem.nightLightEnabled
                            enabled: ccSystem.nightLightAvailable && !ccSystem.nightLightBusy
                            onClicked: ccSystem.toggleNightLight()
                        }

                        WidgetButton {
                            text: ccSystem.idleInhibited ? "Awake On" : "Awake Off"
                            Layout.fillWidth: true
                            checkable: true
                            checked: ccSystem.idleInhibited
                            onClicked: ccSystem.idleInhibited = !ccSystem.idleInhibited
                        }
                    }

                    Text {
                        Layout.fillWidth: true
                        visible: ccSystem.nightLightError !== ""
                        text: ccSystem.nightLightError
                        color: Theme.subtext0
                        font.pixelSize: 10
                        elide: Text.ElideRight
                    }
                }
            }

            Rectangle {
                id: dividerRect
                objectName: "dividerRect"
                Layout.fillWidth: true
                height: 1
                color: Theme.surface0
                opacity: 0.6
            }

            // Section tabs sit directly above the body content (after the
            // global brightness/DND/night/awake quick controls), so the
            // selector is adjacent to the options it switches. No extra
            // fixed spacer between tabs and content.
            RowLayout {
                id: tabsRow
                objectName: "tabsRow"
                Layout.fillWidth: true
                spacing: 6

                WidgetButton {
                    text: "Audio"
                    Layout.fillWidth: true
                    checkable: true
                    checked: root.selectedTab === 0
                    onClicked: root.openSection(0)
                }
                WidgetButton {
                    text: "Network"
                    Layout.fillWidth: true
                    checkable: true
                    checked: root.selectedTab === 1
                    onClicked: root.openSection(1)
                }
                WidgetButton {
                    text: "Bluetooth"
                    Layout.fillWidth: true
                    checkable: true
                    checked: root.selectedTab === 2
                    onClicked: root.openSection(2)
                }
                WidgetButton {
                    text: "Power"
                    Layout.fillWidth: true
                    checkable: true
                    checked: root.selectedTab === 3
                    onClicked: root.openSection(3)
                }
            }

            Flickable {
                id: bodyFlick
                objectName: "bodyFlick"
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                contentWidth: width
                contentHeight: contentCol.implicitHeight
                boundsBehavior: Flickable.StopAtBounds

                ColumnLayout {
                    id: contentCol
                    width: parent.width
                    spacing: 10

                    AudioPanel {
                        id: audioPanel
                        Layout.fillWidth: true
                        Layout.preferredHeight: visible ? implicitHeight : 0
                        visible: root.selectedTab === 0
                        maxListHeight: Math.max(120, Math.min(320, root.availableBodyHeight))
                    }

                    NetworkPanel {
                        id: netPanel
                        objectName: "netPanel"
                        Layout.fillWidth: true
                        Layout.preferredHeight: visible ? implicitHeight : 0
                        visible: root.selectedTab === 1 || root.selectedTab === 2
                        passwordPopup: root.passwordPopup
                        maxListHeight: Math.max(120, Math.min(300, root.availableBodyHeight))
                        // Embedded panel reuses the CC-level Audio/Network/
                        // Bluetooth/Power tabs above; hide its duplicate
                        // internal Network/Bluetooth selector.
                        showTabSelector: false
                        active: root.requestedOpen && root.visible
                            && (root.selectedTab === 1 || root.selectedTab === 2)
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: visible ? powerInner.implicitHeight + 20 : 0
                        visible: root.selectedTab === 3
                        radius: Theme.controlRadius
                        color: Theme.surface0
                        border.color: Theme.border
                        border.width: 1

                        ColumnLayout {
                            id: powerInner
                            anchors.fill: parent
                            anchors.margins: 10
                            spacing: 6

                            Text {
                                text: "Power"
                                color: Theme.text
                                font.pixelSize: 16
                                font.bold: true
                            }

                            Text {
                                Layout.fillWidth: true
                                color: Theme.text
                                font.pixelSize: 13
                                font.bold: true
                                text: {
                                    if (UPower.displayDevice.type !== 2 || !UPower.displayDevice.ready) return "No battery";
                                    return Math.round(UPower.displayDevice.percentage * 100) + "%";
                                }
                            }

                            Text {
                                Layout.fillWidth: true
                                color: Theme.subtext0
                                font.pixelSize: 11
                                text: {
                                    if (UPower.displayDevice.type !== 2 || !UPower.displayDevice.ready) return "No battery detected on this device";
                                    var state = UPower.displayDevice.state;
                                    var label = state === 1 ? "Charging" : (state === 2 ? "Discharging" : "Battery");
                                    var seconds = state === 1 ? UPower.displayDevice.timeToFull : (state === 2 ? UPower.displayDevice.timeToEmpty : 0);
                                    if (seconds > 0) {
                                        var h = Math.floor(seconds / 3600);
                                        var m = Math.floor((seconds % 3600) / 60);
                                        var when = state === 1 ? "full" : "empty";
                                        if (h > 0) return label + " - " + h + "h " + m + "m until " + when;
                                        return label + " - " + m + "m until " + when;
                                    }
                                    return label;
                                }
                                wrapMode: Text.WordWrap
                            }
                        }
                    }
                }
            }
        }
    }

    // Single-progress motion: enter/exit animate reveal to 1/0 from the
    // current value (interrupted close reopens mid-fade, never resets).
    // No spring/overshoot easing: OutCubic in, InCubic out.
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
