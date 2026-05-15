import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import "../theme"

ColumnLayout {
    id: root
    spacing: 10
    
    ListModel {
        id: clipboardModel
    }

    Process {
        id: listProcess
        command: ["/usr/bin/cliphist", "list"]
        stdout: StdioCollector { id: collector }
        stderr: StdioCollector { id: errorCollector }
        onExited: (exitCode) => {
            if (exitCode !== 0) {
                debugText.text = "Error " + exitCode + ": " + errorCollector.text.substring(0, 50);
            } else {
                debugText.text = "Len: " + collector.text.length + " | Count: " + clipboardModel.count;
            }

            if (exitCode === 0) {
                clipboardModel.clear();
                let lines = collector.text.split("\n");
                for (let line of lines) {
                    if (line.trim() === "") continue;
                    let parts = line.split("\t");
                    if (parts.length >= 2) {
                        let id = parts[0];
                        let content = parts.slice(1).join("\t");
                        clipboardModel.append({ 
                            "clipId": id, 
                            "clipContent": content,
                            "isBinary": content.includes("[[ binary data"),
                            "previewBase64": "" 
                        });
                    }
                }
            }
        }
    }

    Process {
        id: copyProcess
        running: false
    }

    function copyItem(id) {
        // Detect type and copy correctly
        let cmd = "tmp=$(mktemp); cliphist decode " + id + " > \"$tmp\"; " +
                  "type=$(file --mime-type \"$tmp\" -b); " +
                  "wl-copy --type \"$type\" < \"$tmp\"; rm \"$tmp\"";
        copyProcess.command = ["sh", "-c", cmd];
        copyProcess.running = true;
    }

    Process {
        id: wipeProcess
        command: ["cliphist", "wipe"]
        running: false
        onExited: refresh()
    }

    function clearHistory() {
        wipeProcess.running = true;
    }

    Text {
        id: debugText
        Layout.fillWidth: true
        Layout.margins: 5
        font.pixelSize: 12
        color: Theme.red
        wrapMode: Text.WrapAnywhere
        text: "Waiting for cliphist..."
        visible: false 
        
        onTextChanged: {
            if (text.startsWith("Error")) {
                visible = true;
            }
        }
    }

    function refresh() {
        if (listProcess.running) {
            listProcess.running = false;
        }
        listProcess.running = true;
    }

    function search(query) {
        if (listProcess.running) {
            listProcess.running = false;
        }
        clipboardModel.clear();
        if (query.length > 0) {
            listProcess.command = ["sh", "-c", "cliphist list | grep -i '" + query.replace(/'/g, "'\\''") + "'"];
        } else {
            listProcess.command = ["/usr/bin/cliphist", "list"];
        }
        listProcess.running = true;
    }

    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 10
        
        Text {
            text: "Clipboard"
            font.pixelSize: 20
            font.bold: true
            color: Theme.text
            Layout.fillWidth: true
        }
        
        IconButton {
            text: "󰁪" 
            onClicked: refresh()
        }
        
        IconButton {
            text: "󰆴" 
            onClicked: clearHistory()
        }
    }

    TextField {
        id: searchField
        Layout.fillWidth: true
        Layout.leftMargin: 10
        Layout.rightMargin: 10
        placeholderText: "Search..."
        color: Theme.text
        background: Rectangle {
            color: Theme.mantle
            radius: 5
            border.color: searchField.activeFocus ? Theme.mauve : "transparent"
        }
        onTextChanged: search(text)
        
        Keys.onEscapePressed: {
            sidebar.expanded = false;
        }
    }

    ListView {
        id: clipList
        Layout.fillWidth: true
        Layout.fillHeight: true
        Layout.leftMargin: 10
        Layout.rightMargin: 10
        Layout.bottomMargin: 10
        model: clipboardModel
        clip: true
        spacing: 5
        
        delegate: ItemDelegate {
            width: clipList.width
            height: isBinary ? 80 : 45
            
            contentItem: RowLayout {
                spacing: 15
                
                // Image Preview
                Rectangle {
                    visible: isBinary
                    width: 60; height: 60
                    color: Theme.mantle
                    radius: 4
                    Layout.alignment: Qt.AlignVCenter
                    
                    Image {
                        id: previewImage
                        anchors.fill: parent
                        anchors.margins: 2
                        fillMode: Image.PreserveAspectFit
                        source: model.previewBase64 !== "" ? "data:image/png;base64," + model.previewBase64 : ""
                        visible: model.previewBase64 !== ""
                    }
                    
                    Text {
                        anchors.centerIn: parent
                        text: "󰋩"
                        color: Theme.mauve
                        font.pixelSize: 24
                        visible: model.previewBase64 === ""
                    }

                    Process {
                        id: previewProc
                        command: ["sh", "-c", "cliphist decode " + model.clipId + " | base64 -w0"]
                        running: isBinary && model.previewBase64 === ""
                        stdout: StdioCollector {
                            onTextChanged: (text) => {
                                if (text && text.length > 0) {
                                    model.previewBase64 = text;
                                }
                            }
                        }
                    }
                }
                
                Text {
                    text: isBinary ? "Image Data" : model.clipContent
                    color: isBinary ? Theme.mauve : Theme.text
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                    font.pixelSize: 13
                    font.italic: isBinary
                    textFormat: Text.PlainText
                    verticalAlignment: Text.AlignVCenter
                }
            }
            
            background: Rectangle {
                color: hovered ? Theme.surface0 : "transparent"
                radius: Theme.radius / 2
            }
            
            onClicked: {
                copyItem(model.clipId);
                sidebar.expanded = false;
            }
        }
        
        ScrollBar.vertical: ScrollBar {
            policy: ScrollBar.AsNeeded
        }
        
        Text {
            anchors.centerIn: parent
            text: "No history found"
            color: Theme.text
            opacity: 0.5
            visible: clipboardModel.count === 0 && !listProcess.running
        }
    }

    Component.onCompleted: {
        refresh();
    }
    
    onVisibleChanged: {
        if (visible) {
            refresh();
            searchField.text = "";
            searchField.forceActiveFocus();
        }
    }
}
