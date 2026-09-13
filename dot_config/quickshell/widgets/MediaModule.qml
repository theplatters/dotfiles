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
                anchors.fill: parent
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
                    anchors.fill: parent
                    enabled: root.activePlayer && root.activePlayer.canGoPrevious
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
                    anchors.fill: parent
                    enabled: root.activePlayer && root.activePlayer.canTogglePlaying
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
                    anchors.fill: parent
                    enabled: root.activePlayer && root.activePlayer.canGoNext
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.control("next")
                }
            }
        }
    }
}
