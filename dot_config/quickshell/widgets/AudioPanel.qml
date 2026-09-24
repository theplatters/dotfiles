import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Services.Pipewire
import "../theme"

// Embeddable audio controls for the ControlCenter.
// Item root so it can live persistently inside ControlCenter.
// The standalone AudioPopup wrapper was deleted (D1).
Item {
    id: root

    property int activeTab: 0

    readonly property var audioNodes: Pipewire.nodes.values.filter(node => node.audio)
    readonly property var outputDevices: audioNodes.filter(node => !node.isStream && node.isSink)
    readonly property var inputDevices: audioNodes.filter(node => !node.isStream && !node.isSink)
    readonly property var streams: audioNodes.filter(node => node.isStream)
    readonly property var defaultSink: Pipewire.defaultAudioSink
    readonly property var defaultSource: Pipewire.defaultAudioSource

    implicitWidth: 380
    // Natural height from content so the outer CC Flickable (bound to the
    // anchor screen) is the single responsive scroller. The content layout
    // is top-anchored (not fill) so this binding is not circular:
    // layout width follows root, layout implicit height drives root.
    implicitHeight: contentLayout.implicitHeight
    property int maxListHeight: 300

    PwObjectTracker {
        objects: root.audioNodes
    }

    function nodeTitle(node) {
        if (!node) return "Unknown";
        return node.nickname || node.description || node.properties["application.name"] || node.properties["media.name"] || node.name;
    }

    function nodeSubtitle(node) {
        if (!node) return "";
        if (node.isStream) return node.properties["media.name"] || node.properties["application.process.binary"] || PwNodeType.toString(node.type);
        return node.name;
    }

    function isDefaultSink(node) {
        return root.defaultSink && node && root.defaultSink.id === node.id;
    }

    function isDefaultSource(node) {
        return root.defaultSource && node && root.defaultSource.id === node.id;
    }

    function setDefault(node) {
        if (!node || node.isStream) return;

        if (node.isSink) {
            Pipewire.preferredDefaultAudioSink = node;
        } else {
            Pipewire.preferredDefaultAudioSource = node;
        }
    }

    function clampVolume(value) {
        return Math.max(0, Math.min(1.5, value));
    }

    function setVolume(node, value) {
        if (node && node.audio) {
            node.audio.volume = root.clampVolume(value);
        }
    }

    function toggleMute(node) {
        if (node && node.audio) {
            node.audio.muted = !node.audio.muted;
        }
    }

    ColumnLayout {
        id: contentLayout
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: 10

        RowLayout {
            Layout.fillWidth: true
            spacing: 10

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2

                Text {
                    text: "Audio"
                    color: Theme.text
                    font.pixelSize: 16
                    font.bold: true
                }

                Text {
                    text: Pipewire.ready ? "PipeWire ready" : "Syncing PipeWire"
                    color: Theme.subtext0
                    font.pixelSize: 11
                }
            }

            Text {
                text: root.defaultSink && root.defaultSink.audio && root.defaultSink.audio.muted ? "󰝟" : "󰕾"
                color: root.defaultSink && root.defaultSink.audio && root.defaultSink.audio.muted ? Theme.red : Theme.subtext1
                font.family: Theme.iconFontFamily
                font.pixelSize: 18
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 6

            Repeater {
                model: ["Output", "Input", "Streams"]

                Rectangle {
                    Layout.fillWidth: true
                    height: 30
                    radius: Theme.controlRadius
                    color: root.activeTab === index ? Theme.surface2 : Theme.mantle
                    border.color: Theme.border
                    border.width: 1

                    Text {
                        anchors.centerIn: parent
                        text: modelData
                        color: Theme.text
                        font.pixelSize: 12
                        font.bold: root.activeTab === index
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.activeTab = index
                    }
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            height: 1
            color: Theme.surface0
            opacity: 0.6
        }

        ListView {
            id: audioList
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(root.maxListHeight, Math.max(140, audioList.contentHeight))
            Layout.minimumHeight: 120
            Layout.maximumHeight: root.maxListHeight
            spacing: 6
            clip: true
            model: Pipewire.nodes

            delegate: Rectangle {
                id: audioRow

                readonly property bool audioNode: !!modelData.audio
                readonly property bool outputDevice: audioNode && !modelData.isStream && modelData.isSink
                readonly property bool inputDevice: audioNode && !modelData.isStream && !modelData.isSink
                readonly property bool streamNode: audioNode && modelData.isStream
                readonly property bool rowVisible: root.activeTab === 0 ? outputDevice : (root.activeTab === 1 ? inputDevice : streamNode)
                readonly property bool defaultNode: modelData.isSink ? root.isDefaultSink(modelData) : root.isDefaultSource(modelData)
                readonly property bool muted: audioNode && modelData.audio.muted
                readonly property real volume: audioNode ? modelData.audio.volume : 0

                width: audioList.width
                height: rowVisible ? 92 : 0
                visible: rowVisible
                radius: Theme.controlRadius
                color: defaultNode ? Theme.surface2 : (rowHover.hovered ? Theme.surface0 : Theme.transparent)
                border.color: defaultNode ? Theme.border : Theme.transparent
                border.width: 1
                clip: true

                HoverHandler {
                    id: rowHover
                }

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 10
                    spacing: 8

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 10

                        Text {
                            text: audioRow.streamNode ? "󰝚" : (modelData.isSink ? "󰓃" : "󰍬")
                            color: Theme.text
                            font.family: Theme.iconFontFamily
                            font.pixelSize: 16
                            Layout.alignment: Qt.AlignVCenter
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 1

                            Text {
                                Layout.fillWidth: true
                                text: root.nodeTitle(modelData)
                                color: Theme.text
                                font.pixelSize: 13
                                font.bold: audioRow.defaultNode
                                elide: Text.ElideRight
                            }

                            Text {
                                Layout.fillWidth: true
                                text: root.nodeSubtitle(modelData)
                                color: Theme.subtext0
                                font.pixelSize: 10
                                opacity: 0.85
                                elide: Text.ElideRight
                            }
                        }

                        Text {
                            text: audioRow.muted ? "Muted" : Math.round(audioRow.volume * 100) + "%"
                            color: audioRow.muted ? Theme.red : Theme.subtext0
                            font.pixelSize: 11
                            font.bold: true
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8

                        Rectangle {
                            Layout.fillWidth: true
                            height: 8
                            radius: height / 2
                            color: Theme.surface1

                            Rectangle {
                                width: parent.width * Math.min(audioRow.volume, 1.5) / 1.5
                                height: parent.height
                                radius: parent.radius
                                color: audioRow.muted ? Theme.surface2 : Theme.accentMuted
                            }

                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: mouse => root.setVolume(modelData, mouse.x / width * 1.5)
                                onPositionChanged: mouse => {
                                    if (pressed) root.setVolume(modelData, mouse.x / width * 1.5);
                                }
                                onWheel: wheel => root.setVolume(modelData, audioRow.volume + (wheel.angleDelta.y > 0 ? 0.02 : -0.02))
                            }
                        }

                        Text {
                            text: audioRow.muted ? "󰝟" : "󰕾"
                            color: audioRow.muted ? Theme.red : Theme.text
                            font.family: Theme.iconFontFamily
                            font.pixelSize: 14

                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: root.toggleMute(modelData)
                            }
                        }

                        Text {
                            visible: !audioRow.streamNode
                            text: audioRow.defaultNode ? "Default" : "Set"
                            color: audioRow.defaultNode ? Theme.text : Theme.subtext1
                            font.pixelSize: 11
                            font.bold: true

                            MouseArea {
                                anchors.fill: parent
                                enabled: !audioRow.defaultNode
                                cursorShape: Qt.PointingHandCursor
                                onClicked: root.setDefault(modelData)
                            }
                        }
                    }
                }
            }
        }

        Text {
            Layout.fillWidth: true
            visible: (root.activeTab === 0 && root.outputDevices.length === 0)
                || (root.activeTab === 1 && root.inputDevices.length === 0)
                || (root.activeTab === 2 && root.streams.length === 0)
            text: root.activeTab === 0 ? "No output devices" : (root.activeTab === 1 ? "No input devices" : "No active streams")
            color: Theme.subtext0
            font.pixelSize: 12
            horizontalAlignment: Text.AlignHCenter
        }
    }
}
