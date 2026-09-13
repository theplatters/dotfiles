import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import "../theme"

ColumnLayout {
    id: root
    spacing: 10
    
    readonly property string todoPath: Quickshell.configDirectory + "/sidebar/todos.json"

    ListModel {
        id: todoModel
    }

    // Process for reading
    Process {
        id: reader
        command: ["cat", todoPath]
        running: false
        stdout: StdioCollector {
            onTextChanged: (text) => {
                let data = (text || "").trim();
                if (data !== "") {
                    try {
                        let json = JSON.parse(data);
                        todoModel.clear();
                        for (let todo of json) {
                            todoModel.append(todo);
                        }
                    } catch(e) {
                        console.log("Error parsing todos:", e);
                    }
                }
            }
        }
    }

    // Process for writing
    Process {
        id: writer
        command: ["sh", "-c", "cat > " + todoPath]
        running: false
    }

    Component.onCompleted: {
        reader.running = true;
    }

    Text {
        text: "TODOs"
        font.pixelSize: 20
        font.bold: true
        color: Theme.text
        Layout.margins: 10
    }

    ListView {
        id: todoList
        Layout.fillWidth: true
        Layout.fillHeight: true
        model: todoModel
        clip: true
        spacing: 5
        
        delegate: Rectangle {
            width: todoList.width - 20
            height: 50
            x: 10
            color: Theme.mantle
            radius: 8
            
            RowLayout {
                anchors.fill: parent
                anchors.margins: 10
                
                CheckBox {
                    checked: model.done
                    onCheckedChanged: {
                        model.done = checked;
                        saveTodos();
                    }
                }
                
                Text {
                    Layout.fillWidth: true
                    text: model.task
                    color: Theme.text
                    font.pixelSize: 14
                    font.strikeout: model.done
                }
                
                IconButton {
                    text: "󰆴"
                    onClicked: {
                        todoModel.remove(index);
                        saveTodos();
                    }
                }
            }
        }
    }

    RowLayout {
        Layout.fillWidth: true
        Layout.margins: 10
        
        TextField {
            id: todoInput
            Layout.fillWidth: true
            placeholderText: "New task..."
            focus: true
            color: Theme.text
            background: Rectangle {
                color: Theme.mantle
                radius: 5
                border.color: todoInput.activeFocus ? Theme.mauve : "transparent"
            }
            onAccepted: addTodo()

            Keys.onEscapePressed: {
                sidebar.expanded = false;
            }
        }
        
        StyledButton {
            text: "Add"
            onClicked: addTodo()
        }
    }

    function addTodo() {
        if (todoInput.text.trim() === "") return;
        todoModel.append({ task: todoInput.text, done: false });
        todoInput.text = "";
        saveTodos();
    }

    function saveTodos() {
        let todos = [];
        for (let i = 0; i < todoModel.count; i++) {
            todos.push(todoModel.get(i));
        }
        let data = JSON.stringify(todos);
        
        // We use a simple echo/printf for small files if writer doesn't support stdin easily
        let writeCmd = "printf %s '" + data.replace(/'/g, "'\\''") + "' > " + todoPath;
        let proc = Quickshell.createChildProcess(["sh", "-c", writeCmd]);
        proc.running = true;
    }
}
