import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Services.Mpris
import "../theme"

PopupWindow {
    id: root

    property var anchorItem: null
    property var selectedPlayer: Mpris.players.values.find(player => player.isPlaying) || Mpris.players.values[0] || null
    property bool requestedOpen: false
    property bool closing: false

    visible: false
    implicitWidth: 430
    implicitHeight: 410
    color: "transparent"

    anchor {
        window: QsWindow.window
        rect: anchorItem ? Qt.rect(anchorItem.mapToGlobal(0, 0).x, anchorItem.mapToGlobal(0, 0).y + anchorItem.height + 8, anchorItem.width, 1) : Qt.rect(0, 48, 1, 1)
        gravity: Edges.Bottom
    }

    function setOpen(open) {
        requestedOpen = open;
        if (open) {
            closing = false;
            exitMotion.stop();
            selectDefaultPlayer();
            panel.opacity = 0;
            panel.scale = 0.98;
            if (!visible) visible = true;
            enterMotion.restart();
        } else if (visible && !closing) {
            closing = true;
            enterMotion.stop();
            exitMotion.restart();
        }
    }

    function toggle(item) {
        anchorItem = item;
        setOpen(!requestedOpen);
    }

    function selectDefaultPlayer() {
        const players = Mpris.players.values;
        root.selectedPlayer = players.find(player => player.isPlaying)
            || (root.selectedPlayer && players.indexOf(root.selectedPlayer) !== -1 ? root.selectedPlayer : null)
            || players[0]
            || null;
    }

    function pauseOtherPlayers(active) {
        for (const player of Mpris.players.values) {
            if (player !== active && player.canPause && player.isPlaying) {
                player.pause();
            }
        }
    }

    function togglePlayer(player) {
        if (!player || !player.canTogglePlaying) return;
        if (!player.isPlaying) root.pauseOtherPlayers(player);
        player.togglePlaying();
    }

    function title(player) {
        return player ? (player.trackTitle || player.identity || "Unknown track") : "No media";
    }

    function artist(player) {
        return player ? (player.trackArtist || player.trackAlbumArtist || player.identity || "") : "";
    }

    function artUrl(player) {
        if (!player) return "";
        if (player.trackArtUrl) return player.trackArtUrl;

        const url = player.metadata["xesam:url"] || "";
        if (url.indexOf("youtube.com/watch?v=") !== -1) {
            const videoId = url.split("v=")[1].split("&")[0];
            return "https://img.youtube.com/vi/" + videoId + "/0.jpg";
        }
        if (url.indexOf("youtu.be/") !== -1) {
            const videoId = url.split("youtu.be/")[1].split("?")[0];
            return "https://img.youtube.com/vi/" + videoId + "/0.jpg";
        }

        return "";
    }

    function formatTime(seconds) {
        if (!seconds || seconds < 0) return "0:00";
        const total = Math.floor(seconds);
        const mins = Math.floor(total / 60);
        const secs = total % 60;
        return mins + ":" + (secs < 10 ? "0" : "") + secs;
    }

    function seekTo(player, ratio) {
        if (player && player.positionSupported && player.lengthSupported && player.length > 0) {
            player.position = Math.max(0, Math.min(player.length, ratio * player.length));
        }
    }

    function nextLoopState(player) {
        if (!player || !player.loopSupported) return;
        if (player.loopState === MprisLoopState.None) {
            player.loopState = MprisLoopState.Track;
        } else if (player.loopState === MprisLoopState.Track) {
            player.loopState = MprisLoopState.Playlist;
        } else {
            player.loopState = MprisLoopState.None;
        }
    }

    Rectangle {
        id: panel
        anchors.fill: parent
        color: Theme.base
        radius: Theme.cardRadius
        border.color: Theme.border
        border.width: 1
        opacity: 0
        scale: 0.98

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 14
            spacing: 12

            RowLayout {
                Layout.fillWidth: true
                spacing: 10

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 2

                    Text {
                        text: "Media"
                        color: Theme.text
                        font.pixelSize: 16
                        font.bold: true
                    }

                    Text {
                        text: Mpris.players.values.length + (Mpris.players.values.length === 1 ? " player" : " players")
                        color: Theme.subtext0
                        font.pixelSize: 11
                    }
                }

                Text {
                    visible: root.selectedPlayer && root.selectedPlayer.canRaise
                    text: "󰍉"
                    color: Theme.subtext1
                    font.pixelSize: 15

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.selectedPlayer.raise()
                    }
                }

                Text {
                    visible: root.selectedPlayer && root.selectedPlayer.canQuit
                    text: "󰅖"
                    color: Theme.red
                    font.pixelSize: 15

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.selectedPlayer.quit()
                    }
                }
            }

            ListView {
                id: playerStrip
                Layout.fillWidth: true
                Layout.preferredHeight: Mpris.players.values.length > 1 ? 42 : 0
                visible: Mpris.players.values.length > 1
                orientation: ListView.Horizontal
                spacing: 8
                clip: true
                model: Mpris.players

                delegate: Rectangle {
                    width: Math.max(92, playerName.implicitWidth + 26)
                    height: 34
                    radius: Theme.controlRadius
                    color: root.selectedPlayer === modelData ? Theme.surface2 : Theme.mantle
                    border.color: Theme.border
                    border.width: 1

                    Text {
                        id: playerName
                        anchors.centerIn: parent
                        text: modelData.identity || modelData.desktopEntry || "Player"
                        color: Theme.text
                        font.pixelSize: 11
                        font.bold: root.selectedPlayer === modelData
                        elide: Text.ElideRight
                        width: parent.width - 16
                        horizontalAlignment: Text.AlignHCenter
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.selectedPlayer = modelData
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.preferredHeight: 122
                spacing: 12
                visible: !!root.selectedPlayer

                Rectangle {
                    width: 112
                    height: 112
                    radius: Theme.controlRadius
                    color: Theme.surface0
                    clip: true

                    Image {
                        anchors.fill: parent
                        source: root.artUrl(root.selectedPlayer)
                        fillMode: Image.PreserveAspectCrop
                        visible: source !== ""
                    }

                    Text {
                        anchors.centerIn: parent
                        text: "󰝚"
                        color: Theme.subtext1
                        font.pixelSize: 36
                        visible: root.artUrl(root.selectedPlayer) === ""
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    spacing: 6

                    Text {
                        Layout.fillWidth: true
                        text: root.title(root.selectedPlayer)
                        color: Theme.text
                        font.pixelSize: 15
                        font.bold: true
                        elide: Text.ElideRight
                    }

                    Text {
                        Layout.fillWidth: true
                        text: root.artist(root.selectedPlayer)
                        color: Theme.subtext0
                        font.pixelSize: 12
                        elide: Text.ElideRight
                    }

                    Text {
                        Layout.fillWidth: true
                        text: root.selectedPlayer ? MprisPlaybackState.toString(root.selectedPlayer.playbackState) : ""
                        color: Theme.subtext1
                        font.pixelSize: 11
                    }

                    Item { Layout.fillHeight: true }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8

                        Text {
                            text: root.formatTime(root.selectedPlayer ? root.selectedPlayer.position : 0)
                            color: Theme.subtext0
                            font.pixelSize: 10
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            height: 7
                            radius: 4
                            color: Theme.surface1

                            Rectangle {
                                width: parent.width * (root.selectedPlayer && root.selectedPlayer.lengthSupported && root.selectedPlayer.length > 0 ? Math.min(1, root.selectedPlayer.position / root.selectedPlayer.length) : 0)
                                height: parent.height
                                radius: parent.radius
                                color: Theme.text
                            }

                            MouseArea {
                                anchors.fill: parent
                                enabled: root.selectedPlayer && root.selectedPlayer.positionSupported && root.selectedPlayer.lengthSupported
                                cursorShape: Qt.PointingHandCursor
                                onClicked: mouse => root.seekTo(root.selectedPlayer, mouse.x / width)
                                onPositionChanged: mouse => {
                                    if (pressed) root.seekTo(root.selectedPlayer, mouse.x / width);
                                }
                            }
                        }

                        Text {
                            text: root.formatTime(root.selectedPlayer && root.selectedPlayer.lengthSupported ? root.selectedPlayer.length : 0)
                            color: Theme.subtext0
                            font.pixelSize: 10
                        }
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 18
                visible: !!root.selectedPlayer

                Item { Layout.fillWidth: true }

                Text {
                    text: "󰒮"
                    color: root.selectedPlayer && root.selectedPlayer.canGoPrevious ? Theme.text : Theme.surface2
                    font.pixelSize: 22

                    MouseArea {
                        anchors.fill: parent
                        enabled: root.selectedPlayer && root.selectedPlayer.canGoPrevious
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.selectedPlayer.previous()
                    }
                }

                Text {
                    text: root.selectedPlayer && root.selectedPlayer.isPlaying ? "󰏤" : "󰐊"
                    color: root.selectedPlayer && root.selectedPlayer.canTogglePlaying ? Theme.text : Theme.surface2
                    font.pixelSize: 26

                    MouseArea {
                        anchors.fill: parent
                        enabled: root.selectedPlayer && root.selectedPlayer.canTogglePlaying
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.togglePlayer(root.selectedPlayer)
                    }
                }

                Text {
                    text: "󰒭"
                    color: root.selectedPlayer && root.selectedPlayer.canGoNext ? Theme.text : Theme.surface2
                    font.pixelSize: 22

                    MouseArea {
                        anchors.fill: parent
                        enabled: root.selectedPlayer && root.selectedPlayer.canGoNext
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.selectedPlayer.next()
                    }
                }

                Item { Layout.fillWidth: true }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 10
                visible: !!root.selectedPlayer

                Text {
                    text: "󰕾"
                    color: Theme.text
                    font.pixelSize: 13
                    visible: root.selectedPlayer && root.selectedPlayer.volumeSupported
                }

                Rectangle {
                    Layout.fillWidth: true
                    height: 7
                    radius: 4
                    color: Theme.surface1
                    visible: root.selectedPlayer && root.selectedPlayer.volumeSupported

                    Rectangle {
                        width: parent.width * Math.max(0, Math.min(1, root.selectedPlayer ? root.selectedPlayer.volume : 0))
                        height: parent.height
                        radius: parent.radius
                        color: Theme.text
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: mouse => root.selectedPlayer.volume = Math.max(0, Math.min(1, mouse.x / width))
                        onPositionChanged: mouse => {
                            if (pressed) root.selectedPlayer.volume = Math.max(0, Math.min(1, mouse.x / width));
                        }
                    }
                }

                Text {
                    text: root.selectedPlayer && root.selectedPlayer.volumeSupported ? Math.round(root.selectedPlayer.volume * 100) + "%" : ""
                    color: Theme.subtext0
                    font.pixelSize: 10
                    visible: root.selectedPlayer && root.selectedPlayer.volumeSupported
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 10
                visible: !!root.selectedPlayer

                Rectangle {
                    height: 30
                    width: 92
                    radius: Theme.controlRadius
                    color: root.selectedPlayer && root.selectedPlayer.shuffle ? Theme.surface2 : Theme.mantle
                    border.color: Theme.border
                    border.width: 1
                    opacity: root.selectedPlayer && root.selectedPlayer.shuffleSupported ? 1 : 0.45

                    Text {
                        anchors.centerIn: parent
                        text: "Shuffle"
                        color: Theme.text
                        font.pixelSize: 11
                        font.bold: root.selectedPlayer && root.selectedPlayer.shuffle
                    }

                    MouseArea {
                        anchors.fill: parent
                        enabled: root.selectedPlayer && root.selectedPlayer.shuffleSupported
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.selectedPlayer.shuffle = !root.selectedPlayer.shuffle
                    }
                }

                Rectangle {
                    height: 30
                    width: 110
                    radius: Theme.controlRadius
                    color: root.selectedPlayer && root.selectedPlayer.loopState !== MprisLoopState.None ? Theme.surface2 : Theme.mantle
                    border.color: Theme.border
                    border.width: 1
                    opacity: root.selectedPlayer && root.selectedPlayer.loopSupported ? 1 : 0.45

                    Text {
                        anchors.centerIn: parent
                        text: root.selectedPlayer ? "Loop " + MprisLoopState.toString(root.selectedPlayer.loopState) : "Loop"
                        color: Theme.text
                        font.pixelSize: 11
                        font.bold: root.selectedPlayer && root.selectedPlayer.loopState !== MprisLoopState.None
                    }

                    MouseArea {
                        anchors.fill: parent
                        enabled: root.selectedPlayer && root.selectedPlayer.loopSupported
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.nextLoopState(root.selectedPlayer)
                    }
                }

                Item { Layout.fillWidth: true }
            }

            Text {
                Layout.fillWidth: true
                Layout.fillHeight: true
                visible: Mpris.players.values.length === 0
                text: "No active media players"
                color: Theme.subtext0
                font.pixelSize: 12
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
        }
    }

    ParallelAnimation {
        id: enterMotion
        NumberAnimation { target: panel; property: "opacity"; to: 1; duration: Theme.motionPanel; easing.type: Easing.OutCubic }
        NumberAnimation { target: panel; property: "scale"; to: 1; duration: Theme.motionPanel; easing.type: Easing.OutCubic }
    }

    SequentialAnimation {
        id: exitMotion
        ParallelAnimation {
            NumberAnimation { target: panel; property: "opacity"; to: 0; duration: Theme.motionExit; easing.type: Easing.InCubic }
            NumberAnimation { target: panel; property: "scale"; to: 0.98; duration: Theme.motionExit; easing.type: Easing.InCubic }
        }
        ScriptAction { script: { if (!root.requestedOpen) root.visible = false; root.closing = false; } }
    }
}
