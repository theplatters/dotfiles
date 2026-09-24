import QtQuick
import Quickshell
import Quickshell.Services.Mpris
import "../theme"

Item {
    id: root
    // Plain Row extent: QQuickRow reports laid-out (actual) widths, so the
    // 200px title cap below already bounds this. No manual correction.
    implicitWidth: layout.implicitWidth
    implicitHeight: layout.implicitHeight
    width: implicitWidth
    height: implicitHeight

    property var mediaPopout: null
    property bool compact: false
    // True while any transport/title control is hovered. The bar capsule
    // uses this (plus its own background hover) for a flicker-free
    // highlight: inner MouseAreas sit above the capsule toggle, so the
    // background alone would lose hover over the controls.
    readonly property bool hovered: titleMouse.containsMouse || prevMouse.containsMouse
        || playMouse.containsMouse || nextMouse.containsMouse

    readonly property var players: Mpris.players.values
    readonly property var activePlayer: players.find(player => player.isPlaying)
        || players.find(player => player.playbackState === MprisPlaybackState.Paused)
        || players[0]
        || null
    readonly property string status: activePlayer ? MprisPlaybackState.toString(activePlayer.playbackState) : ""
    readonly property string metadata: activePlayer ? ((activePlayer.trackArtist ? activePlayer.trackArtist + " - " : "") + (activePlayer.trackTitle || activePlayer.identity)) : ""

    function pauseOtherPlayers(active) {
        for (const player of root.players) {
            if (player !== active && player.canPause && player.isPlaying) {
                player.pause();
            }
        }
    }

    function control(cmd) {
        if (!root.activePlayer) return;

        if (cmd === "previous" && root.activePlayer.canGoPrevious) {
            root.activePlayer.previous();
        } else if (cmd === "next" && root.activePlayer.canGoNext) {
            root.activePlayer.next();
        } else if (cmd === "play-pause" && root.activePlayer.canTogglePlaying) {
            if (!root.activePlayer.isPlaying) root.pauseOtherPlayers(root.activePlayer);
            root.activePlayer.togglePlaying();
        }
    }

    Row {
        id: layout
        spacing: 10
        anchors.verticalCenter: parent.verticalCenter
        visible: !!root.activePlayer

        Text {
            text: "󰝚"
            color: Theme.subtext1
            font.family: Theme.iconFontFamily
            font.pixelSize: 14
            anchors.verticalCenter: parent.verticalCenter
        }

        Text {
            id: titleText
            visible: !root.compact
            text: root.metadata
            color: Theme.text
            font.pixelSize: 12
            elide: Text.ElideRight
            width: Math.min(implicitWidth, 200)
            anchors.verticalCenter: parent.verticalCenter

            MouseArea {
                id: titleMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: {
                    if (root.mediaPopout) {
                        root.mediaPopout.toggle(parent.parent);
                    }
                }
            }
        }

        Row {
            spacing: 8
            anchors.verticalCenter: parent.verticalCenter

            Text {
                text: "󰒮"
                color: root.activePlayer && root.activePlayer.canGoPrevious ? Theme.text : Theme.surface2
                font.family: Theme.iconFontFamily
                font.pixelSize: 16
                anchors.verticalCenter: parent.verticalCenter

                MouseArea {
                    id: prevMouse
                    anchors.fill: parent
                    enabled: root.activePlayer && root.activePlayer.canGoPrevious
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.control("previous")
                }
            }

            Text {
                text: root.activePlayer && root.activePlayer.isPlaying ? "󰏤" : "󰐊"
                color: root.activePlayer && root.activePlayer.canTogglePlaying ? Theme.text : Theme.surface2
                font.family: Theme.iconFontFamily
                font.pixelSize: 16
                anchors.verticalCenter: parent.verticalCenter

                MouseArea {
                    id: playMouse
                    anchors.fill: parent
                    enabled: root.activePlayer && root.activePlayer.canTogglePlaying
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.control("play-pause")
                }
            }

            Text {
                text: "󰒭"
                color: root.activePlayer && root.activePlayer.canGoNext ? Theme.text : Theme.surface2
                font.family: Theme.iconFontFamily
                font.pixelSize: 16
                anchors.verticalCenter: parent.verticalCenter

                MouseArea {
                    id: nextMouse
                    anchors.fill: parent
                    enabled: root.activePlayer && root.activePlayer.canGoNext
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.control("next")
                }
            }
        }
    }
}
