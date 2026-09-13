//@ pragma UseQApplication
import QtQuick
import Quickshell
import Quickshell.Wayland
import Quickshell.Hyprland
import Quickshell.Services.Notifications
import "./widgets"

ShellRoot {
    NotificationServer {
        id: notifServer
        property bool inhibit: false
        actionsSupported: true
        bodyMarkupSupported: true
    }

    PasswordPopup {
        id: passwordPopup
    }

    CommandPalette {
        id: commandPalette
    }

    ProjectPlanner {
        id: projectPlanner
    }

    Connections {
        target: commandPalette
        function onProjectPlanningRequested() {
            projectPlanner.open()
        }
    }

    PanelWindow {
        id: barWindow
        color: "transparent"
        anchors {
            top: true
            left: true
            right: true
        }
        implicitHeight: 40
        
        // Exclude from layout so windows don't overlap it
        exclusionMode: ExclusionMode.Auto
        
        MediaPopout {
            id: mediaPopout
        }

        ControlCenter {
            id: controlCenter
            anchorItem: bar.trayAnchor
            trayScope: bar
            barWindow: barWindow
            notifServer: notifServer
            passwordPopup: passwordPopup
        }

        NotificationPopout {
            notifServer: notifServer
        }

        Bar {
            id: bar
            notifServer: notifServer
            mediaPopout: mediaPopout
            controlCenter: controlCenter
        }
    }
}
