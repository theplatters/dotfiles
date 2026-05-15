import QtQuick
import Quickshell
import Quickshell.Io
import "../theme"

Item {
    id: root
    width: layout.implicitWidth
    height: layout.implicitHeight

    property string metadata: ""
    property string status: ""
    property var mediaPopout: null

    function update() {
        if (!metadataProc.running) {
            metadataProc.running = true;
        }
    }

    Process {
        id: metadataProc
        command: ["playerctl", "metadata", "--format", "{{status}}|{{artist}}|{{title}}"]
        running: false
        stdout: StdioCollector {
            id: collector
        }
        
        onExited: (exitCode, exitStatus) => {
            if (exitCode === 0) {
                let text = collector.text.trim();
                if (text === "") {
                    root.status = "";
                    root.metadata = "";
                    return;
                }
                let parts = text.split('|');
                if (parts.length >= 3) {
                    root.status = parts[0];
                    let artist = parts[1];
                    let title = parts[2];
                    root.metadata = (artist ? artist + " - " : "") + title;
                }
            } else {
                root.status = "";
                root.metadata = "";
            }
        }
    }

    Timer {
        id: pollTimer
        interval: 3000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.update()
    }

    Process {
        id: controlProc
        onExited: root.update()
    }

    function control(cmd) {
        // Optimistic UI update for play/pause
        if (cmd === "play-pause") {
            let becomingActive = root.status !== "Playing";
            root.status = becomingActive ? "Playing" : "Paused";
            
            if (becomingActive) {
                // Pause all other players
                controlProc.command = ["sh", "-c", "playerctl -l | grep -v $(playerctl metadata --format '{{playerName}}') | xargs -I {} playerctl -p {} pause"];
                controlProc.running = true;
            }
        }
        
        // Wait for potential previous process if needed, or just use another process
        // For simplicity, we'll just run the primary command
        let mainCmdProc = Qt.createQmlObject('import Quickshell.Io 1.0; Process {}', root);
        mainCmdProc.command = ["playerctl", cmd];
        mainCmdProc.exited.connect(() => {
            mainCmdProc.destroy();
            root.update();
        });
        mainCmdProc.running = true;
    }

    Row {
        id: layout
        spacing: 10
        anchors.verticalCenter: parent.verticalCenter
        visible: root.status !== ""

        Text {
            text: "󰝚"
            color: Theme.mauve
            font.pixelSize: 14
            anchors.verticalCenter: parent.verticalCenter
        }

        Text {
            id: titleText
            text: root.metadata
            color: Theme.text
            font.pixelSize: 12
            elide: Text.ElideRight
            width: Math.min(implicitWidth, 200)
            anchors.verticalCenter: parent.verticalCenter
            
            MouseArea {
                anchors.fill: parent
                onClicked: {
                    if (root.mediaPopout) {
                        root.mediaPopout.toggle(mediaContainer);
                    }
                }
                cursorShape: Qt.PointingHandCursor
            }
        }

        Row {
            spacing: 8
            anchors.verticalCenter: parent.verticalCenter
            
            Text {
                text: "󰒮"
                color: Theme.teal
                font.pixelSize: 16
                anchors.verticalCenter: parent.verticalCenter
                MouseArea {
                    anchors.fill: parent
                    onClicked: root.control("previous")
                    cursorShape: Qt.PointingHandCursor
                }
            }

            Text {
                text: root.status === "Playing" ? "󰏤" : "󰐊"
                color: Theme.teal
                font.pixelSize: 16
                anchors.verticalCenter: parent.verticalCenter
                MouseArea {
                    anchors.fill: parent
                    onClicked: root.control("play-pause")
                    cursorShape: Qt.PointingHandCursor
                }
            }

            Text {
                text: "󰒭"
                color: Theme.teal
                font.pixelSize: 16
                anchors.verticalCenter: parent.verticalCenter
                MouseArea {
                    anchors.fill: parent
                    onClicked: root.control("next")
                    cursorShape: Qt.PointingHandCursor
                }
            }
        }
    }
}
