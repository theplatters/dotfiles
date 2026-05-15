import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import "../theme"

PopupWindow {
    id: root
    
    property var anchorItem: null
    visible: false
    
    width: 400
    height: 350
    
    color: Theme.base
    
    anchor {
        window: QsWindow.window
        rect: anchorItem ? Qt.rect(anchorItem.mapToGlobal(0, 0).x, anchorItem.mapToGlobal(0, 0).y + anchorItem.height + 8, anchorItem.width, 1) : Qt.rect(0, 48, 1, 1)
        gravity: Edges.Bottom
    }

    Rectangle {
        anchors.fill: parent
        color: Theme.base
        radius: Theme.radius
        border.color: Theme.mauve
        border.width: 2

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 15
            spacing: 15

            Text {
                text: "Active Players"
                color: Theme.mauve
                font.pixelSize: 16
                font.bold: true
            }

            // Player Carousel
            ListView {
                id: playerList
                Layout.fillWidth: true
                Layout.preferredHeight: 120
                orientation: ListView.Horizontal
                spacing: 15
                clip: true
                model: playerModel

                delegate: Rectangle {
                    width: 280
                    height: 110
                    color: Theme.mantle
                    radius: 10
                    border.color: Theme.surface0

                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: 10
                        spacing: 10

                        Rectangle {
                            width: 80
                            height: 80
                            color: Theme.surface0
                            radius: 5
                            clip: true
                            
                            Image {
                                anchors.fill: parent
                                source: model.artUrl || ""
                                fillMode: Image.PreserveAspectCrop
                                visible: source != ""
                            }
                            
                            Text {
                                anchors.centerIn: parent
                                text: "󰝚"
                                color: Theme.surface0
                                font.pixelSize: 30
                                visible: !model.artUrl
                            }
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2

                            Text {
                                text: model.title
                                color: Theme.text
                                font.pixelSize: 14
                                font.bold: true
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }

                            Text {
                                text: model.artist
                                color: Theme.text
                                font.pixelSize: 12
                                opacity: 0.8
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }

                            Row {
                                spacing: 15
                                Layout.topMargin: 5
                                
                                Text {
                                    text: "󰒮"
                                    color: Theme.teal
                                    font.pixelSize: 18
                                    MouseArea {
                                        anchors.fill: parent
                                        onClicked: root.playerControl(model.playerName, "previous")
                                    }
                                }
                                Text {
                                    text: model.status === "Playing" ? "󰏤" : "󰐊"
                                    color: Theme.teal
                                    font.pixelSize: 18
                                    MouseArea {
                                        anchors.fill: parent
                                        onClicked: root.playerControl(model.playerName, "play-pause")
                                    }
                                }
                                Text {
                                    text: "󰒭"
                                    color: Theme.teal
                                    font.pixelSize: 18
                                    MouseArea {
                                        anchors.fill: parent
                                        onClicked: root.playerControl(model.playerName, "next")
                                    }
                                }
                            }
                        }
                    }
                }
            }

            Text {
                text: "KDE Connect Devices"
                color: Theme.mauve
                font.pixelSize: 16
                font.bold: true
            }

            ListView {
                id: deviceList
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 8
                clip: true
                model: deviceModel

                delegate: Rectangle {
                    width: deviceList.width
                    height: 45
                    color: Theme.mantle
                    radius: 8
                    
                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: 10
                        
                        Text {
                            text: "󰄜 " + model.name
                            color: Theme.text
                            font.pixelSize: 13
                            Layout.fillWidth: true
                        }

                        Row {
                            spacing: 10
                            
                            Rectangle {
                                width: 30; height: 30; radius: 5; color: Theme.surface0
                                Text { anchors.centerIn: parent; text: "󰂚"; color: Theme.green }
                                MouseArea {
                                    anchors.fill: parent
                                    onClicked: root.runCmd(["kdeconnect-cli", "-d", model.id, "--ping"])
                                }
                            }
                            
                            Rectangle {
                                width: 30; height: 30; radius: 5; color: Theme.surface0
                                Text { anchors.centerIn: parent; text: "󰂞"; color: Theme.red }
                                MouseArea {
                                    anchors.fill: parent
                                    onClicked: root.runCmd(["kdeconnect-cli", "-d", model.id, "--ring"])
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    ListModel { id: playerModel }
    ListModel { id: deviceModel }

    Component {
        id: playerMetadataComponent
        Process {
            property string playerName
            stdout: StdioCollector { id: collector }
            onExited: (exitCode) => {
                if (exitCode === 0) {
                    let rawText = collector.text.trim();
                    let parts = rawText.split('|');
                    if (parts.length >= 4) {
                        let status = parts[0];
                        let artist = parts[1];
                        let title = parts[2];
                        let artUrl = parts[3] || "";
                        let xesamUrl = parts[4] || "";
                        
                        // YouTube Fallback
                        if (artUrl === "" && (xesamUrl.includes("youtube.com/watch?v=") || xesamUrl.includes("youtu.be/"))) {
                            let videoId = "";
                            if (xesamUrl.includes("v=")) {
                                videoId = xesamUrl.split('v=')[1].split('&')[0];
                            } else {
                                videoId = xesamUrl.split('be/')[1].split('?')[0];
                            }
                            artUrl = "https://img.youtube.com/vi/" + videoId + "/0.jpg";
                        }

                        playerModel.append({
                            playerName: playerName,
                            status: status,
                            artist: artist,
                            title: title,
                            artUrl: artUrl
                        });
                    }
                }
                destroy();
            }
        }
    }

    Component {
        id: genericCmdComponent
        Process {
            onExited: destroy()
        }
    }

    Process {
        id: playerListProc
        command: ["playerctl", "-l"]
        running: false
        stdout: StdioCollector { id: playerListCollector }
        onExited: (exitCode) => {
            if (exitCode === 0) {
                playerModel.clear();
                let rawText = playerListCollector.text.trim();
                let players = rawText.split('\n');
                for (let p of players) {
                    let name = p.trim();
                    if (name !== "") {
                        root.fetchPlayerData(name);
                    }
                }
            }
        }
    }

    function fetchPlayerData(playerName) {
        let proc = playerMetadataComponent.createObject(root, {
            playerName: playerName,
            command: ["playerctl", "-p", playerName, "metadata", "--format", "{{status}}|{{artist}}|{{title}}|{{mpris:artUrl}}|{{xesam:url}}"]
        });
        proc.running = true;
    }

    Process {
        id: deviceListProc
        command: ["kdeconnect-cli", "-l", "--id-name-only"]
        running: false
        stdout: StdioCollector { id: deviceListCollector }
        onExited: (exitCode) => {
            if (exitCode === 0) {
                deviceModel.clear();
                let rawText = deviceListCollector.text.trim();
                let lines = rawText.split('\n');
                for (let line of lines) {
                    let cleaned = line.trim();
                    if (cleaned === "") continue;
                    
                    // Handle format: "ID NAME" (space separated)
                    let firstSpace = cleaned.indexOf(' ');
                    if (firstSpace !== -1) {
                        let id = cleaned.substring(0, firstSpace);
                        let name = cleaned.substring(firstSpace + 1);
                        deviceModel.append({ name: name, id: id });
                    }
                }
            }
        }
    }

    function playerControl(name, cmd) {
        if (cmd === "play" || cmd === "play-pause") {
            // If we are starting playback, pause everyone else
            for (let i = 0; i < playerModel.count; i++) {
                let other = playerModel.get(i);
                if (other.playerName !== name) {
                    runCmd(["playerctl", "-p", other.playerName, "pause"]);
                }
            }
        }
        
        runCmd(["playerctl", "-p", name, cmd]);
        // Refresh after control
        timer.restart();
    }

    function runCmd(cmd) {
        let p = genericCmdComponent.createObject(root, { command: cmd });
        p.running = true;
    }

    Timer {
        id: timer
        interval: 3000
        running: root.visible
        repeat: true
        onTriggered: {
            if (!playerListProc.running) playerListProc.running = true;
            if (!deviceListProc.running) deviceListProc.running = true;
        }
    }
    
    onVisibleChanged: {
        if (visible && anchorItem) {
            // Re-trigger anchor evaluation
            let item = anchorItem;
            anchorItem = null;
            anchorItem = item;
        }
    }
    
    function toggle(item) {
        if (visible) {
            visible = false;
        } else {
            anchorItem = item;
            visible = true;
            playerListProc.running = true;
            deviceListProc.running = true;
        }
    }
}
