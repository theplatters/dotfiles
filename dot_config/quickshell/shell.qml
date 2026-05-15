//@ pragma UseQApplication
import QtQuick
import Quickshell
import Quickshell.Wayland
import Quickshell.Hyprland
import Quickshell.Services.Notifications
import "./widgets"
import "./sidebar"

ShellRoot {
    NotificationServer {
        id: notifServer
        actionsSupported: true
        bodyMarkupSupported: true
    }

    Sidebar {
        id: sideMenu
    }

    GlobalShortcut {
        name: "clipboard"
        onPressed: sideMenu.showTab(3)
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
        exclusionMode: ExclusionMode.Exclusive
        
        MediaPopout {
            id: mediaPopout
        }

        AudioPopup {
            id: audioPopup
        }

        NotificationPopout {
            notifServer: notifServer
        }

        Bar {
            notifServer: notifServer
            mediaPopout: mediaPopout
            audioPopup: audioPopup
            onToggleSidebar: sideMenu.toggle()
        }
    }
}
