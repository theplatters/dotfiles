import QtQuick
import Quickshell

// Single shell-level clock source (S-005c). One 1 s Timer lives here; every
// per-bar ClockModule binds instead of running its own Timer. `now` ticks
// every second for the HH:mm segment; `dayDate` only advances on day change
// so date segments (ddd, MM-dd) re-evaluate at midnight, not every second.
Item {
    id: root

    property var now: new Date()
    property var dayDate: new Date()
    readonly property string dayKey: Qt.formatDateTime(dayDate, "yyyy-MM-dd")

    function sameDay(a, b) {
        return a.getFullYear() === b.getFullYear()
            && a.getMonth() === b.getMonth()
            && a.getDate() === b.getDate();
    }

    function textFor(format) {
        if (format === "HH:mm") return Qt.formatDateTime(root.now, format);
        return Qt.formatDateTime(root.dayDate, format);
    }

    Timer {
        id: clockTick
        objectName: "sharedClockTick"
        interval: 1000
        running: true
        repeat: true
        onTriggered: {
            let current = new Date();
            root.now = current;
            if (!root.sameDay(current, root.dayDate)) root.dayDate = current;
        }
    }
}
