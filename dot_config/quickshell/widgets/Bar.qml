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
    property var mediaPopout: null
    property var audioPopup: null
    property var networkPopup: null
    property var batteryPopup: null
    property var controlCenter: null
    property alias trayAnchor: unifiedTray

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
    // depends on compact and would jitter).
    readonly property bool compactMedia: mediaModule.status !== "" && (bar.width < 1400 || !mediaFitsFull)
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
                // Toggle chevron + audio + network/BT + battery + tray icons.
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
                            audioPopup: bar.audioPopup
                            controlCenter: bar.controlCenter
                        }

                        Rectangle { width: 1; height: 16; color: Theme.border; anchors.verticalCenter: parent.verticalCenter }

                        NetworkModule {
                            id: networkModule
                            objectName: "networkModule"
                            height: 32
                            anchors.verticalCenter: parent.verticalCenter
                            networkPopup: bar.networkPopup
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
                            batteryPopup: bar.batteryPopup
                            controlCenter: bar.controlCenter
                        }

                        Rectangle {
                            width: trayFlick.visible ? 1 : 0
                            height: 16
                            color: Theme.border
                            anchors.verticalCenter: parent.verticalCenter
                            visible: trayFlick.visible
                        }

                        // Tray viewport: capped at ~5 icons (5x20 + 4x8 + slack);
                        // extra icons scroll with wheel/drag. Icon MouseAreas
                        // keep native left/middle/right actions; tray icons
                        // have no wheel action, so the wheel catcher is safe.
                        Flickable {
                            id: trayFlick
                            objectName: "trayFlick"
                            height: 32
                            width: Math.min(trayModule.implicitWidth + 4, 148)
                            visible: trayModule.visible
                            anchors.verticalCenter: parent.verticalCenter
                            contentWidth: trayModule.implicitWidth
                            contentHeight: 32
                            flickableDirection: Flickable.HorizontalFlick
                            boundsBehavior: Flickable.StopAtBounds
                            clip: true
                            MouseArea {
                                anchors.fill: parent
                                acceptedButtons: Qt.NoButton
                                hoverEnabled: false
                                onWheel: (wheel) => {
                                    trayFlick.contentX = Math.max(0, Math.min(trayFlick.contentWidth - trayFlick.width, trayFlick.contentX - wheel.angleDelta.y - wheel.angleDelta.x));
                                }
                            }
                            TrayModule {
                                id: trayModule
                                objectName: "trayModule"
                                x: 0
                                height: 32
                                anchors.verticalCenter: parent.verticalCenter
                                visible: implicitWidth > 0
                            }
                        }
                    }
                }

                // Notifications history stays reachable next to the tray.
                NotificationModule {
                    notifServer: bar.notifServer
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
                        ClockModule { format: "ddd"; visible: !bar.compactClock; anchors.verticalCenter: parent.verticalCenter }
                        ClockModule { format: "HH:mm"; anchors.verticalCenter: parent.verticalCenter }
                        ClockModule { format: "MM-dd"; visible: !bar.compactClock; anchors.verticalCenter: parent.verticalCenter }
                    }
                    MouseArea {
                        anchors.fill: parent
                        hoverEnabled: true
                        onEntered: clockContainer.color = Theme.surface0
                        onExited: clockContainer.color = Theme.mantle
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
                        color: Theme.mantle
                        anchors.verticalCenter: parent.verticalCenter
                        visible: bar.showMediaCapsule
                        MediaModule {
                            id: mediaModule
                            objectName: "mediaModule"
                            anchors.centerIn: parent
                            mediaPopout: bar.mediaPopout
                            compact: bar.compactMedia
                        }
                        MouseArea {
                            anchors.fill: parent
                            hoverEnabled: true
                            onEntered: mediaContainer.color = Theme.surface0
                            onExited: mediaContainer.color = Theme.mantle
                            onClicked: {
                                if (bar.mediaPopout) {
                                    bar.mediaPopout.toggle(mediaContainer);
                                }
                            }
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

                // Screenshot
                ScreenshotModule {}

                // System Stats (Mem, CPU, Disk)
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
                            command: ["sh", "-c", "LC_ALL=C free | awk '/Mem:/ {print int($3/$2 * 100)\"%\"}'"]
                            compact: bar.compactStats
                        }
                        StatModule {
                            label: "CPU"; textColor: Theme.subtext1;
                            command: ["sh", "-c", "LC_ALL=C top -bn1 | grep '^%*Cpu(s)' | awk '{print int(100 - $8)\"%\"}'"]
                            compact: bar.compactStats
                        }
                        StatModule {
                            label: "Disk"; textColor: Theme.subtext1;
                            command: ["sh", "-c", "python3 -c \"import shutil; t, u, f = shutil.disk_usage('/'); print(f'{round((t-f)/t*100)}%')\""]
                            compact: bar.compactStats
                        }
                    }
                    MouseArea {
                        anchors.fill: parent
                        hoverEnabled: true
                        onEntered: statsContainer.color = Theme.surface0
                        onExited: statsContainer.color = Theme.mantle
                    }
                }
            }
        }
    }
}
