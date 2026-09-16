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
        agenda: dailyAgenda
    }

    // Shared daily-agenda state: one date, one backend operation, and one
    // Pomodoro timer for the clock popout and the planner Daily tab.
    // It lives here (not in a popup) so closing a popup never stops it.
    DailyAgenda {
        id: dailyAgenda
    }

    Connections {
        target: commandPalette
        function onProjectPlanningRequested(projectId, action, message) {
            let msg = String(message === undefined || message === null ? "" : message).substring(0, 300);
            if (projectId) projectPlanner.openProject(projectId, action, msg)
            else projectPlanner.open()
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

        CalendarPopout {
            id: calendarPopout
            anchorItem: bar.clockAnchor
            agenda: dailyAgenda
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
            calendarPopout: calendarPopout
            projectPlanner: projectPlanner
        }
    }
}
