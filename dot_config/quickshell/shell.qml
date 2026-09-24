//@ pragma UseQApplication
import QtQuick
import Quickshell
import Quickshell.Wayland
import Quickshell.Hyprland
import Quickshell.Services.Notifications
import "./widgets"
import "./theme"

ShellRoot {
    NotificationServer {
        id: notifServer
        property bool inhibit: false
        actionsSupported: true
        bodyMarkupSupported: true
    }

    CommandPalette {
        id: commandPalette
        ambientSource: ambientContext
    }

    ProjectPlanner {
        id: projectPlanner
        agenda: dailyAgenda
        ambientSource: ambientContext
    }

    // Single ambient-context resolver (S-057): one ladder (three helper
    // forks per refresh, on demand only) shared by the palette
    // (unpinned scope) and the planner (pinned scope). Consumers bind
    // via ambientSource and scope-check; nothing refreshes at boot.
    AmbientContext {
        id: ambientContext
    }

    // Shared daily-agenda state: one date, one backend operation, and one
    // Pomodoro timer for the clock popout and the planner Daily tab.
    // It lives here (not in a popup) so closing a popup never stops it.
    DailyAgenda {
        id: dailyAgenda
        scheduler: memoryScheduler
    }

    // Resident Work → Memory / Memory → Work scheduler: invisible item,
    // one Process + repeating tick Timer, bootstraps via memory_tick.py
    // status then ticks while enabled. Emits dataChanged() for popup
    // refresh; never surfaces auto-tick errors.
    MemoryScheduler {
        id: memoryScheduler
    }

    // Single notification-history owner: every per-bar bell is a view over
    // this store, so counts and dismiss state agree across monitors.
    NotificationHistory {
        id: notifHistory
        notifServer: notifServer
    }

    // Single system-stats poller (mem/cpu/disk, 5 s) fanning out to every
    // per-bar StatModule view.
    SystemStats {
        id: systemStats
    }

    // Single resident current-project stream (one `watch` child) plus
    // its own slower 60 s ledger-badge refresh; every per-bar
    // CurrentProjectModule binds here.
    CurrentProjectSource {
        id: currentProjectSource
        agenda: dailyAgenda
    }

    // Single 1 s clock source; every per-bar ClockModule binds here.
    SharedClock {
        id: sharedClock
    }

    Connections {
        target: commandPalette
        function onProjectPlanningRequested(projectId, action, message) {
            let msg = String(message === undefined || message === null ? "" : message).substring(0, 300);
            if (projectId) projectPlanner.openProject(projectId, action, msg)
            else projectPlanner.open()
        }
    }

    // One top bar per screen: Quickshell.screens is a live model, so monitors
    // added later get their own bar window without a restart. Every window
    // owns its screen-local popouts (calendar/media/tray overview/control
    // center), which anchor to that bar and stay on that monitor.
    Variants {
        model: Quickshell.screens

        delegate: PanelWindow {
            id: barWindow
            required property var modelData
            screen: modelData
            color: Theme.transparent
            anchors {
                top: true
                left: true
                right: true
            }
            implicitHeight: 40

            // Exclude from layout so windows don't overlap it
            exclusionMode: ExclusionMode.Auto

            // Shared shell objects passed into this per-screen delegate.
            // Inside a delegate component an unqualified binding whose name
            // matches the target property resolves to the child's own (null)
            // property instead of the outer id, so every consumer must use
            // these qualified refs.
            readonly property var projectPlannerRef: projectPlanner
            readonly property var notifServerRef: notifServer
            readonly property var notifHistoryRef: notifHistory
            readonly property var systemStatsRef: systemStats
            readonly property var projectSourceRef: currentProjectSource
            readonly property var clockSourceRef: sharedClock
            // Bar window geometry for nested popouts/catcher bindings:
            // they must qualify through barWindow (see above), so expose
            // the screen and height as declared refs.
            readonly property var barScreen: screen
            readonly property var barHeight: implicitHeight
            // Qualified ref to the per-screen password dialog below: inside
            // this delegate an unqualified `passwordPopup` binding on CC
            // would resolve to CC's own (null) property, so consumers go
            // through barWindow.passwordPopupRef.
            readonly property var passwordPopupRef: passwordPopup

            // Exclusive-open per screen (D1/S-006): opening any anchored
            // bar popout closes the others on this screen. Each popout
            // keeps its own requestedOpen/setOpen lifecycle; this only
            // calls setOpen(false) on the siblings (a no-op when hidden).
            // Banners are excluded (not popouts). The bell popup is out
            // of scope (owned by NotificationModule, expanded-state).
            function closeOthers(except) {
                if (except !== mediaPopout) mediaPopout.setOpen(false);
                if (except !== calendarPopout) calendarPopout.setOpen(false);
                if (except !== projectOverview && projectOverview.requestedOpen) projectOverview.closePopup();
                if (except !== controlCenter && except !== passwordPopup) controlCenter.setOpen(false);
                if (except !== passwordPopup) passwordPopup.cancelAndClose();
            }

            MediaPopout {
                id: mediaPopout
                anchorScope: bar
            }

            CalendarPopout {
                id: calendarPopout
                anchorItem: bar.clockAnchor
                anchorScope: bar
                agenda: dailyAgenda
            }

            ProjectOverviewPopup {
                id: projectOverview
                anchorScope: bar
                projectPlanner: barWindow.projectPlannerRef
            }

            // Per-screen Wi-Fi password dialog (S-010): follows the screen
            // whose ControlCenter asked for it, like the other popouts.
            PasswordPopup {
                id: passwordPopup
                screen: barWindow.barScreen
            }

            // One outside-click rule (S-008): a per-screen transparent
            // input-only layer above standard windows (topMargin = bar
            // height), visible only while an anchored popout is open. A
            // click outside closes the open popout and is consumed by the
            // dismissal (standard popover behavior). Mechanics: Top layer
            // (above app windows, below the Overlay popouts) so popout
            // content receives its clicks first; the bar area is excluded
            // via margins so bar controls keep working and switching
            // popouts via the bar never eats a first click;
            // keyboardFocus None so it never steals focus while open.
            // The modal PasswordPopup keeps its own backdrop (excluded
            // from this catcher's visibility gate).
            PanelWindow {
                id: popoutCatcher
                screen: barWindow.barScreen
                anchors {
                    top: true
                    bottom: true
                    left: true
                    right: true
                }
                margins {
                    top: barWindow.barHeight
                }
                color: Theme.transparent
                visible: mediaPopout.requestedOpen || mediaPopout.visible
                    || calendarPopout.requestedOpen || calendarPopout.visible
                    || projectOverview.requestedOpen || projectOverview.visible
                    || controlCenter.requestedOpen || controlCenter.visible
                exclusionMode: ExclusionMode.Ignore
                WlrLayershell.layer: WlrLayer.Top
                WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
                MouseArea {
                    anchors.fill: parent
                    onClicked: closeOthers(null)
                }
            }

            Connections {
                target: mediaPopout
                function onRequestedOpenChanged() { if (mediaPopout.requestedOpen) closeOthers(mediaPopout); }
            }

            Connections {
                target: calendarPopout
                function onRequestedOpenChanged() { if (calendarPopout.requestedOpen) closeOthers(calendarPopout); }
            }

            Connections {
                target: projectOverview
                function onRequestedOpenChanged() { if (projectOverview.requestedOpen) closeOthers(projectOverview); }
            }

            Connections {
                target: controlCenter
                function onRequestedOpenChanged() { if (controlCenter.requestedOpen) closeOthers(controlCenter); }
            }

            Connections {
                target: passwordPopup
                function onRequestedOpenChanged() { if (passwordPopup.requestedOpen) closeOthers(passwordPopup); }
            }

            Connections {
                target: projectOverview
                function onProjectPlanningRequested(projectId, action, message) {
                    let msg = String(message === undefined || message === null ? "" : message).substring(0, 300);
                    if (projectId) projectPlanner.openProject(projectId, action, msg)
                    else projectPlanner.open()
                }
            }

            Connections {
                target: calendarPopout
                function onProjectPlanningRequested(projectId, action, message) {
                    let msg = String(message === undefined || message === null ? "" : message).substring(0, 300);
                    if (projectId) projectPlanner.openProject(projectId, action, msg)
                    else projectPlanner.open()
                }
            }

            ControlCenter {
                id: controlCenter
                anchorItem: bar.trayAnchor
                trayScope: bar
                barWindow: barWindow
                notifServer: barWindow.notifServerRef
                passwordPopup: barWindow.passwordPopupRef
            }

            Bar {
                id: bar
                notifServer: barWindow.notifServerRef
                notifHistory: barWindow.notifHistoryRef
                systemStats: barWindow.systemStatsRef
                projectSource: barWindow.projectSourceRef
                clockSource: barWindow.clockSourceRef
                mediaPopout: mediaPopout
                controlCenter: controlCenter
                calendarPopout: calendarPopout
                projectPlanner: barWindow.projectPlannerRef
                projectOverviewPopup: projectOverview
                agenda: dailyAgenda
            }
        }
    }

    // Notification banners stay a single surface on the default screen:
    // one banner per notification, never one per monitor.
    NotificationPopout {
        notifServer: notifServer
    }
}
