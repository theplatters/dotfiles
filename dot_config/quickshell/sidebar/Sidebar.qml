import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import Quickshell
import "../theme"

PanelWindow {
    id: sidebar
    
    focusable: true
    
    property bool expanded: false
    property int sidebarWidth: 400
    
    anchors {
        top: true
        bottom: true
        left: true // Positioned on the left
    }
    
    implicitWidth: expanded ? sidebarWidth : 0
    visible: expanded || widthAnimation.running
    color: "transparent"
    
    Behavior on implicitWidth {
        NumberAnimation {
            id: widthAnimation
            duration: 300
            easing.type: Easing.InOutQuad
        }
    }

    Rectangle {
        anchors.fill: parent
        color: Theme.base
        opacity: expanded ? 0.95 : 0
        radius: Theme.radius // Round all corners initially
        
        // Covering the left corners to make them square
        Rectangle {
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            width: parent.radius
            color: parent.color
            visible: parent.radius > 0
        }

        Behavior on opacity {
            NumberAnimation { duration: 300 }
        }

        RowLayout {
            anchors.fill: parent
            spacing: 0

            // Tab Bar (Stays on the leftmost edge)
            Rectangle {
                Layout.fillHeight: true
                Layout.preferredWidth: 60
                color: Theme.mantle
                
                ColumnLayout {
                    anchors.fill: parent
                    spacing: 10
                    
                    Repeater {
                        model: [
                            { icon: "󰭹", name: "Gemini" },
                            { icon: "󱗆", name: "TODOs" },
                            { icon: "󰇮", name: "News" },
                            { icon: "󰅌", name: "Clipboard" }
                        ]
                        
                        delegate: ItemDelegate {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 60
                            
                            contentItem: Text {
                                text: modelData.icon
                                font.pixelSize: 24
                                color: stack.currentIndex === index ? Theme.mauve : Theme.text
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }
                            
                            onClicked: stack.currentIndex = index
                            
                            background: Rectangle {
                                color: (stack.currentIndex === index || hovered) ? Theme.surface0 : "transparent"
                            }
                        }
                    }
                    
                    Item { Layout.fillHeight: true }
                }
            }

            // Main Content (Fills the rest to the right)
            StackLayout {
                id: stack
                Layout.fillWidth: true
                Layout.fillHeight: true
                currentIndex: 0

                GeminiView {}
                TodoView {}
                NewsView {}
                ClipboardView {}
            }
        }
    }
    
    function toggle() {
        expanded = !expanded;
    }

    function showTab(index) {
        stack.currentIndex = index;
        expanded = true;
    }

    onExpandedChanged: {
        if (expanded) {
            sidebar.requestActivate();
        }
    }

    Shortcut {
        sequence: "Escape"
        enabled: sidebar.expanded
        onActivated: sidebar.expanded = false
    }
}
