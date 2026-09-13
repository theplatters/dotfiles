import Quickshell
import QtQuick
import Quickshell.Services.Mpris

Item {
    id: audioVisualizer
    width: 30
    height: 24


    property bool trayOpen: false
    property bool isPlaying: {
      return Mpris.players.values.some(player => player.isPlaying);
    }

    // Click to test the animation toggle
    MouseArea {
        anchors.fill: parent
        onClicked: audioVisualizer.isPlaying = !audioVisualizer.isPlaying
    }

    Row {
        anchors.centerIn: parent
        spacing: 4


        // A Repeater is a quick way to generate multiple identical items
        Repeater {
            model: 5 // Creates 3 audio bars
            
            Rectangle {
                id: audioBar
                width: 3
                height: 3 // Resting height
                color: "#a6e3a1" // Soft green color
                radius: 2
                
                // This keeps the bars anchored to the middle so they grow up AND down
                anchors.bottom: parent.bottom 

                // The secret sauce: smoothly animates any change to the 'height' property
                Behavior on height {
                    NumberAnimation { 
                        duration: 150 
                        easing.type: Easing.InOutQuad 
                    }
                }

                // Generates random heights while playing
                Timer {
                    interval: 100 // Keep it snappy (50ms)
                    running: audioVisualizer.isPlaying
                    repeat: true
                    onTriggered: {
                        // 1. Define bounds and maximum step size
                        let minHeight = 4;
                        let maxHeight = 17;
                        let maxStep = 20; // Controls how fast the bar can grow/shrink per tick

                        // 2. Calculate a random step change between -maxStep and +maxStep
                        let change = (Math.random() * 2 - 1) * maxStep;

                        // 3. Apply the change to the current height
                        let newHeight = audioBar.height + change;

                        // 4. Clamp the value so it doesn't break out of your min/max bounds
                        audioBar.height = Math.max(minHeight, Math.min(maxHeight, newHeight));
                    }
                }

                // Force the bars back to a resting state when audio pauses
                Connections {
                    target: audioVisualizer
                    function onIsPlayingChanged() {
                        if (!audioVisualizer.isPlaying) {
                            audioBar.height = 4;
                        }
                    }
                }
            }
        }
    }
}
