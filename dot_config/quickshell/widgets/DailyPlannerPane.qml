import QtQuick
import QtQuick.Layouts

// DailyPlannerPane: parity wrapper around DailyPlanner.
//
// Both daily surfaces (the clock CalendarPopout and the ProjectPlanner
// Daily tab) are deliberate mirrors: views of the same shared DailyAgenda
// state, neither secondary. To keep their wiring identical by
// construction, both instantiate this wrapper instead of DailyPlanner
// directly. The wrapper owns the one DailyPlanner binding
// (agenda + compact + projectPlanningRequested relay + syncViewToDate
// passthrough) so the two sites cannot drift.
//
// Card set per host (§6.3): the popout renders compact (month grid,
// Scheduled, picker, Pomodoro, quick-add only); the planner Daily tab
// renders the full set (plus Captured, Review, SessionCard).
ColumnLayout {
    id: root

    property var agenda: null
    property bool compact: false

    signal projectPlanningRequested(string projectId, string action, string message)

    function syncViewToDate() {
        inner.syncViewToDate()
    }

    DailyPlanner {
        id: inner
        Layout.fillWidth: true
        agenda: root.agenda
        compact: root.compact
        onProjectPlanningRequested: (projectId, action, message) => root.projectPlanningRequested(projectId, action, message)
    }
}
