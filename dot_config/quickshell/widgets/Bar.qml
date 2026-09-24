import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Services.UPower
import "../theme"

Rectangle {
    id: bar
    width: parent.width
    height: 40
    color: Theme.transparent

    property var notifServer: null
    property var notifHistory: null
    property var mediaPopout: null
    property var controlCenter: null
    property var calendarPopout: null
    property var projectPlanner: null
    property var projectOverviewPopup: null
    // Shared DailyAgenda for the P5 session-ledger badge.
    property var agenda: null
    // Shared shell-level pollers (one instance per desktop, not per bar).
    property var systemStats: null
    property var projectSource: null
    property var clockSource: null
    property alias trayAnchor: unifiedTray
    property alias clockAnchor: clockContainer

    readonly property bool compactStats: width < 1400
    readonly property bool compactNetwork: width < 1200
    readonly property bool compactBattery: width < 1000
    readonly property bool compactClock: width < 1150

    // Measured side widths (implicit only: no layout feedback). The central
    // slot takes the remainder and scrolls internally, so side capsules
    // never overlap it and it never hides controls by clipping.
    readonly property real sideReserve: leftRow.implicitWidth + rightRow.implicitWidth + 44
    readonly property real availableCenterSpace: Math.max(0, bar.width - sideReserve)
    // Worst-case side cost, exactly invariant to network compactness: the
    // measured reserve minus the network module's actual width plus its
    // compact-independent fullWidth (capped label + capped BT count + all
    // gaps, as if shown). Toggling compact therefore cannot move this
    // value, so tightSides can drive compactness without feedback.
    readonly property real sideReserveWorst: sideReserve - networkModule.implicitWidth + networkModule.fullWidth
    // Content-aware compact: when even the default workspace set (~200px)
    // would not fit beside worst-case sides, drop the SSID text early.
    readonly property bool tightSides: sideReserveWorst > bar.width - 200

    // Center budget: workspace capsule first (natural width + 20px capsule
    // padding + 8px row spacing), media gets the remainder.
    readonly property real workspaceNeed: workspaceModule.implicitWidth + 28
    readonly property real mediaRemainder: Math.max(0, availableCenterSpace - workspaceNeed)
    // Media needs are bounded constants: the title is hard-capped at 200px
    // in MediaModule (full ~= 200 title + icon/controls/spacing + 28px
    // capsule padding), compact is transport controls only.
    readonly property real mediaFullNeed: 320
    readonly property real mediaCompactNeed: 140
    readonly property bool mediaFitsFull: mediaRemainder >= mediaFullNeed
    readonly property bool mediaFitsCompact: mediaRemainder >= mediaCompactNeed
    // Stable by design: never reads mediaModule.implicitWidth (which itself
    // depends on compact and would jitter). Driven purely by the measured
    // center remainder against the bounded needs; compact cannot move the
    // remainder (needs are constants), so no oscillation and no hysteresis
    // band is needed.
    readonly property bool compactMedia: mediaModule.status !== "" && !mediaFitsFull
    // When even the compact capsule cannot fit, the capsule hides and a
    // 32px mini entry keeps media reachable via the center scroller.
    readonly property bool showMediaCapsule: mediaModule.status !== "" && (compactMedia ? mediaFitsCompact : mediaFitsFull)
    readonly property bool showMediaMini: mediaModule.status !== "" && !showMediaCapsule

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 10
        anchors.rightMargin: 10
        anchors.topMargin: 4
        anchors.bottomMargin: 4
        spacing: 12

        // LEFT: fixed to content width (unified tray + notifications + clock).
        Item {
            id: leftSlot
            objectName: "leftSlot"
            Layout.fillHeight: true
            Layout.fillWidth: false
            Layout.preferredWidth: leftRow.implicitWidth
            Layout.minimumWidth: leftRow.implicitWidth
            Layout.maximumWidth: leftRow.implicitWidth
            Row {
                id: leftRow
                objectName: "leftRow"
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                spacing: 8

                // Unified status tray: ONE shared capsule, first on the left.
                // Toggle chevron + audio + network/BT + battery + current
                // project. SNI tray icons live in their own capsule in the
                // right slot (sysTrayCapsule) so the project module can
                // never overlap them.
                // Modules keep their own MouseAreas (no overlay stealing):
                // audio left=mute right=section0 wheel=volume, network=section1,
                // battery=section3, tray=own activate/middle/right.
                // Wheel-only catchers (NoButton) scroll the tray viewport;
                // they never accept clicks, so module events are unaffected.
                Rectangle {
                    id: unifiedTray
                    objectName: "unifiedTray"
                    height: 32
                    width: trayRow.implicitWidth + 20
                    radius: Theme.controlRadius
                    color: (bar.controlCenter && (bar.controlCenter.requestedOpen || bar.controlCenter.visible)) ? Theme.surface0 : Theme.mantle
                    border.color: (bar.controlCenter && (bar.controlCenter.requestedOpen || bar.controlCenter.visible)) ? Theme.accentMuted : Theme.border
                    border.width: 1
                    Behavior on color { ColorAnimation { duration: Theme.motionFast } }

                    Row {
                        id: trayRow
                        objectName: "trayRow"
                        height: 32
                        anchors.left: parent.left
                        anchors.leftMargin: 10
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: 6

                        WidgetIconButton {
                            id: trayToggle
                            objectName: "trayToggle"
                            width: 32
                            height: 32
                            topPadding: 5
                            bottomPadding: 5
                            leftPadding: 5
                            rightPadding: 5
                            // Zero style insets so the actual Button background
                            // rect equals the 32px geometry (contained at any
                            // glyph angle). Root rotation stays 0; only the
                            // inner glyph rotates via iconRotation.
                            topInset: 0
                            bottomInset: 0
                            leftInset: 0
                            rightInset: 0
                            anchors.verticalCenter: parent.verticalCenter
                            text: "Control center"
                            iconSource: "icons/chevron-down.svg"
                            borderVisible: false
                            Accessible.name: "Open control center"
                            iconRotation: (bar.controlCenter && bar.controlCenter.requestedOpen) ? 180 : 0
                            Behavior on iconRotation { NumberAnimation { duration: Theme.motionFast; easing.type: Easing.OutCubic } }
                            onClicked: {
                                if (bar.controlCenter) bar.controlCenter.toggle();
                            }
                        }

                        Rectangle { width: 1; height: 16; color: Theme.border; anchors.verticalCenter: parent.verticalCenter }

                        AudioModule {
                            id: audioModule
                            objectName: "audioModule"
                            height: 32
                            anchors.verticalCenter: parent.verticalCenter
                            controlCenter: bar.controlCenter
                        }

                        Rectangle { width: 1; height: 16; color: Theme.border; anchors.verticalCenter: parent.verticalCenter }

                        NetworkModule {
                            id: networkModule
                            objectName: "networkModule"
                            height: 32
                            anchors.verticalCenter: parent.verticalCenter
                            controlCenter: bar.controlCenter
                            compact: bar.compactNetwork || bar.tightSides
                        }

                        Rectangle {
                            width: batteryModule.visible ? 1 : 0
                            height: 16
                            color: Theme.border
                            anchors.verticalCenter: parent.verticalCenter
                            visible: batteryModule.visible
                        }

                        BatteryModule {
                            id: batteryModule
                            objectName: "batteryModule"
                            height: 32
                            anchors.verticalCenter: parent.verticalCenter
                            visible: UPower.displayDevice.type === 2
                            compact: bar.compactBattery
                            controlCenter: bar.controlCenter
                        }

                        Rectangle { width: 1; height: 16; color: Theme.border; anchors.verticalCenter: parent.verticalCenter }

                        // Current working project (authoritative desktop_projects
                        // helper, polled). Compactness follows a plain bar.width
                        // threshold (compactNetwork), never tightSides: the
                        // measured sideReserve already includes this module, so
                        // reading tightSides here would feed back into layout.
                        CurrentProjectModule {
                            id: currentProjectModule
                            objectName: "currentProjectModule"
                            height: 32
                            anchors.verticalCenter: parent.verticalCenter
                            projectPlanner: bar.projectPlanner
                            overviewPopup: bar.projectOverviewPopup
                            // Shared DailyAgenda for the P5 badge.
                            agenda: bar.agenda
                            projectSource: bar.projectSource
                            compact: bar.compactNetwork
                        }
                    }
                }

                // Notifications history stays reachable next to the tray.
                // View over the shell-level NotificationHistory store: every
                // bar's bell shares one model.
                NotificationModule {
                    notifServer: bar.notifServer
                    history: bar.notifHistory
                    anchors.verticalCenter: parent.verticalCenter
                }

                Rectangle {
                    id: clockContainer
                    objectName: "clockContainer"
                    height: 32
                    width: clockLayout.implicitWidth + 28
                    radius: Theme.controlRadius
                    border.color: Theme.border
                    border.width: 1
                    color: Theme.mantle
                    Row {
                        id: clockLayout
                        anchors.centerIn: parent
                        spacing: 12
                        Text {
                            text: "󰃭"
                            color: Theme.text
                            font.family: Theme.iconFontFamily
                            font.pixelSize: 14
                            anchors.verticalCenter: parent.verticalCenter
                        }
                        ClockModule { format: "ddd"; visible: !bar.compactClock; anchors.verticalCenter: parent.verticalCenter; clockSource: bar.clockSource }
                        ClockModule { format: "HH:mm"; anchors.verticalCenter: parent.verticalCenter; clockSource: bar.clockSource }
                        ClockModule { format: "MM-dd"; visible: !bar.compactClock; anchors.verticalCenter: parent.verticalCenter; clockSource: bar.clockSource }
                    }
                    MouseArea {
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onEntered: clockContainer.color = Theme.surface0
                        onExited: clockContainer.color = Theme.mantle
                        onClicked: {
                            if (bar.calendarPopout) {
                                bar.calendarPopout.toggle(clockContainer);
                            }
                        }
                    }
                }
            }
        }

        // CENTER: bounded remainder with explicit overflow. The row centers
        // when it fits; when it overflows it left-aligns inside a horizontal
        // scroller (wheel/drag) so every control stays reachable. The slot
        // clip is a paint backstop only, never the access path.
        Item {
            id: centerSlot
            objectName: "centerSlot"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumWidth: 0
            Layout.preferredWidth: 200
            clip: true
            Flickable {
                id: centerFlick
                objectName: "centerFlick"
                anchors.fill: parent
                contentWidth: centerRow.implicitWidth
                contentHeight: 32
                flickableDirection: Flickable.HorizontalFlick
                boundsBehavior: Flickable.StopAtBounds
                clip: true
                MouseArea {
                    anchors.fill: parent
                    acceptedButtons: Qt.NoButton
                    hoverEnabled: false
                    onWheel: (wheel) => {
                        centerFlick.contentX = Math.max(0, Math.min(centerFlick.contentWidth - centerFlick.width, centerFlick.contentX - wheel.angleDelta.y - wheel.angleDelta.x));
                    }
                }
                Row {
                    id: centerRow
                    objectName: "centerRow"
                    spacing: 8
                    x: Math.max(0, (centerFlick.width - implicitWidth) / 2)
                    anchors.verticalCenter: parent.verticalCenter
                    Rectangle {
                        id: workspaceCapsule
                        objectName: "workspaceCapsule"
                        height: 32
                        width: workspaceModule.implicitWidth + 20
                        radius: Theme.controlRadius
                        border.color: Theme.border
                        border.width: 1
                        color: Theme.mantle
                        anchors.verticalCenter: parent.verticalCenter
                        WorkspaceModule { id: workspaceModule; objectName: "workspaceModule"; anchors.centerIn: parent }
                    }

                    Rectangle {
                        id: mediaContainer
                        objectName: "mediaContainer"
                        height: 32
                        width: mediaModule.implicitWidth + 28
                        radius: Theme.controlRadius
                        border.color: Theme.border
                        border.width: 1
                        // Highlight stays while hovering the capsule body or
                        // any transport/title control (no flicker when moving
                        // between them).
                        color: (mediaBgMouse.containsMouse || mediaModule.hovered) ? Theme.surface0 : Theme.mantle
                        anchors.verticalCenter: parent.verticalCenter
                        visible: bar.showMediaCapsule
                        // One click owner per region: this background toggle
                        // sits BELOW the module, so transport/title clicks
                        // reach the control under the cursor first and only
                        // the capsule body toggles the popout.
                        MouseArea {
                            id: mediaBgMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: {
                                if (bar.mediaPopout) {
                                    bar.mediaPopout.toggle(mediaContainer);
                                }
                            }
                        }
                        MediaModule {
                            id: mediaModule
                            objectName: "mediaModule"
                            anchors.centerIn: parent
                            mediaPopout: bar.mediaPopout
                            compact: bar.compactMedia
                        }
                    }

                    // Mini media entry: always reachable through the scroller
                    // when the full/compact capsule cannot fit.
                    Rectangle {
                        id: mediaMini
                        objectName: "mediaMini"
                        height: 32
                        width: 32
                        radius: Theme.controlRadius
                        border.color: Theme.border
                        border.width: 1
                        color: Theme.mantle
                        anchors.verticalCenter: parent.verticalCenter
                        visible: bar.showMediaMini
                        Text {
                            anchors.centerIn: parent
                            text: "󰝚"
                            color: Theme.subtext1
                            font.family: Theme.iconFontFamily
                            font.pixelSize: 14
                        }
                        MouseArea {
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onEntered: mediaMini.color = Theme.surface0
                            onExited: mediaMini.color = Theme.mantle
                            onClicked: {
                                if (bar.mediaPopout) {
                                    bar.mediaPopout.toggle(mediaMini);
                                }
                            }
                        }
                    }
                }
            }
        }

        // RIGHT: fixed to content width (screenshot + stats stay right).
        Item {
            id: rightSlot
            objectName: "rightSlot"
            Layout.fillHeight: true
            Layout.fillWidth: false
            Layout.preferredWidth: rightRow.implicitWidth
            Layout.minimumWidth: rightRow.implicitWidth
            Layout.maximumWidth: rightRow.implicitWidth
            Row {
                id: rightRow
                objectName: "rightRow"
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                spacing: 8

                // SNI tray capsule: separate from the unified tray so the
                // CurrentProjectModule can never overlap tray icons. Collapses
                // when the tray is empty.
                Rectangle {
                    id: sysTrayCapsule
                    objectName: "sysTrayCapsule"
                    height: 32
                    width: sysTrayFlick.width + 20
                    radius: Theme.controlRadius
                    border.color: Theme.border
                    border.width: 1
                    color: Theme.mantle
                    visible: sysTrayModule.implicitWidth > 0
                    // Tray viewport: capped at ~5 icons (5x20 + 4x8 + slack);
                    // extra icons scroll with wheel/drag. Icon MouseAreas
                    // keep native left/middle/right actions; tray icons
                    // have no wheel action, so the wheel catcher is safe.
                    Flickable {
                        id: sysTrayFlick
                        objectName: "sysTrayFlick"
                        height: 32
                        width: Math.min(sysTrayModule.implicitWidth + 4, 148)
                        visible: sysTrayModule.implicitWidth > 0
                        anchors.centerIn: parent
                        contentWidth: sysTrayModule.implicitWidth
                        contentHeight: 32
                        flickableDirection: Flickable.HorizontalFlick
                        boundsBehavior: Flickable.StopAtBounds
                        clip: true
                        MouseArea {
                            anchors.fill: parent
                            acceptedButtons: Qt.NoButton
                            hoverEnabled: false
                            onWheel: (wheel) => {
                                sysTrayFlick.contentX = Math.max(0, Math.min(sysTrayFlick.contentWidth - sysTrayFlick.width, sysTrayFlick.contentX - wheel.angleDelta.y - wheel.angleDelta.x));
                            }
                        }
                        TrayModule {
                            id: sysTrayModule
                            objectName: "sysTrayModule"
                            x: 0
                            height: 32
                            anchors.verticalCenter: parent.verticalCenter
                        }
                    }
                }

                // Screenshot
                ScreenshotModule {}

                // System Stats (Mem, CPU, Disk): static capsule, no click
                // action (stats popout is roadmap) and therefore no hover
                // affordance.
                Rectangle {
                    id: statsContainer
                    objectName: "statsContainer"
                    height: 32
                    width: statsLayout.implicitWidth + 32
                    radius: Theme.controlRadius
                    border.color: Theme.border
                    border.width: 1
                    color: Theme.mantle
                    Row {
                        id: statsLayout
                        anchors.centerIn: parent
                        spacing: 15
                        StatModule {
                            label: "Mem"; textColor: Theme.subtext1;
                            statKey: "mem"
                            statsSource: bar.systemStats
                            compact: bar.compactStats
                        }
                        StatModule {
                            label: "CPU"; textColor: Theme.subtext1;
                            statKey: "cpu"
                            statsSource: bar.systemStats
                            compact: bar.compactStats
                        }
                        StatModule {
                            label: "Disk"; textColor: Theme.subtext1;
                            statKey: "disk"
                            statsSource: bar.systemStats
                            compact: bar.compactStats
                        }
                    }
                }
            }
        }
    }
}
