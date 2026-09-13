import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import "../theme"

ColumnLayout {
    id: root
    spacing: 10
    
    property string apiKey: ""
    
    Process {
        id: envReader
        command: ["sh", "-c", "grep GEMINI_API_KEY " + Quickshell.configDirectory + "/.env | cut -d '=' -f2"]
        running: true
        stdout: StdioCollector {
            onTextChanged: (text) => {
                if (text) {
                    root.apiKey = text.trim();
                }
            }
        }
    }
    
    ListModel {
        id: messageModel
        ListElement { role: "bot"; text: "Hello! How can I help you today?" }
    }

    Text {
        text: "Gemini"
        font.pixelSize: 20
        font.bold: true
        color: Theme.text
        Layout.margins: 10
    }

    ListView {
        id: chatList
        Layout.fillWidth: true
        Layout.fillHeight: true
        Layout.leftMargin: 10
        Layout.rightMargin: 10
        model: messageModel
        clip: true
        spacing: 10
        
        delegate: Rectangle {
            width: chatList.width - 20
            height: messageText.implicitHeight + 30
            x: role === "user" ? 20 : 0
            color: role === "user" ? Theme.surface0 : Theme.mantle
            radius: 10
            
            Text {
                id: messageText
                anchors.fill: parent
                anchors.topMargin: 15
                anchors.leftMargin: 15
                anchors.rightMargin: 15
                anchors.bottomMargin: 15
                text: model.text
                color: Theme.text
                wrapMode: Text.WordWrap
                font.pixelSize: 14
            }
        }
        
        onCountChanged: chatList.positionViewAtEnd()
    }

    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 10
        
        TextField {
            id: inputField
            Layout.fillWidth: true
            placeholderText: "Type a message..."
            focus: true
            color: Theme.text
            background: Rectangle {
                color: Theme.mantle
                radius: 5
                border.color: inputField.activeFocus ? Theme.mauve : "transparent"
            }
            onAccepted: sendMessage()
            
            Keys.onEscapePressed: {
                sidebar.expanded = false;
            }
        }
        
        StyledButton {
            text: "Send"
            onClicked: sendMessage()
        }
    }

    function sendMessage() {
        if (inputField.text.trim() === "") return;
        
        let userText = inputField.text;
        messageModel.append({ role: "user", text: userText });
        inputField.text = "";
        
        fetchGeminiResponse(userText);
    }

    function fetchGeminiResponse(prompt) {
        let url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key=" + apiKey;
        let xhr = new XMLHttpRequest();
        xhr.open("POST", url);
        xhr.setRequestHeader("Content-Type", "application/json");

        xhr.onreadystatechange = function() {
            if (xhr.readyState === XMLHttpRequest.DONE) {
                if (xhr.status === 200) {
                    let response = JSON.parse(xhr.responseText);
                    let botText = response.candidates[0].content.parts[0].text;
                    messageModel.append({ role: "bot", text: botText });
                } else {
                    messageModel.append({ role: "bot", text: "Error: " + xhr.status + " " + xhr.responseText });
                }
            }
        };

        // Build conversation history
        let contents = [];
        for (let i = 0; i < messageModel.count; i++) {
            let msg = messageModel.get(i);
            // Gemini API roles are "user" and "model"
            let apiRole = msg.role === "user" ? "user" : "model";
            contents.push({
                "role": apiRole,
                "parts": [{ "text": msg.text }]
            });
        }

        let data = {
            "contents": contents
        };

        xhr.send(JSON.stringify(data));
    }
}
