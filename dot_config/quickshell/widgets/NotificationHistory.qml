import QtQuick
import Quickshell
import Quickshell.Services.Notifications

// Single shell-level notification-history owner (S-002). One instance lives
// in shell.qml; every per-bar NotificationModule is a view over it, so bells
// on different monitors share count and dismiss state. Banners
// (NotificationPopout) stay independent: they hold bare references and never
// set tracked.
Item {
    id: root

    property var notifServer: null

    ListModel {
        id: historyModel
    }

    // View surface: the shared model and its count.
    readonly property var model: historyModel
    readonly property int count: historyModel.count

    Connections {
        target: root.notifServer
        enabled: !!root.notifServer

        function onNotification(n) {
            // Check if it's an update to an existing notification
            for (let i = 0; i < historyModel.count; i++) {
                if (historyModel.get(i).notifId === n.id) {
                    historyModel.setProperty(i, "summary", n.summary);
                    historyModel.setProperty(i, "body", n.body);
                    historyModel.setProperty(i, "appName", n.appName);
                    return;
                }
            }

            // New notification: history is the sole tracking owner. Banners
            // hold bare references and never set tracked (tracked=false is
            // equivalent to dismiss()).
            n.tracked = true;
            n.closed.connect((reason) => {
                // Sole-closure path: remote close or our own dismiss() lands
                // here. Remove model-only; never touch tracked (already
                // closed). No-op if an explicit action already removed it.
                for (let i = 0; i < historyModel.count; i++) {
                    if (historyModel.get(i).notifId === n.id) {
                        historyModel.remove(i);
                        break;
                    }
                }
            });

            console.log("New notification: " + n.summary);

            historyModel.insert(0, {
                "notifId": n.id,
                "summary": n.summary,
                "body": n.body,
                "appName": n.appName,
                "notifObj": n
            });

            if (historyModel.count > 10) {
                // Evict oldest: capture the tracked object BEFORE removal
                // (the row dict is invalid after remove), remove from the
                // model first, then release tracking. The closed handler
                // then finds nothing (no double remove of a shifted index).
                var evictObj = historyModel.get(10).notifObj;
                historyModel.remove(10);
                if (evictObj) evictObj.dismiss();
            }
        }
    }

    // Clear All: collect first, clear the model, then dismiss. Each
    // dismiss() re-enters the closed handler, which finds nothing (no double
    // remove, no skipped untracked rows).
    function clearAll() {
        let pending = [];
        for (let i = 0; i < historyModel.count; ++i) {
            let item = historyModel.get(i);
            if (item && item.notifObj) pending.push(item.notifObj);
        }
        historyModel.clear();
        for (let j = 0; j < pending.length; ++j) {
            pending[j].dismiss();
        }
    }

    // Explicit row dismissal (right-click): capture first, remove, then
    // dismiss so closed finds nothing.
    function dismissAt(index) {
        if (index < 0 || index >= historyModel.count) return false;
        var target = historyModel.get(index).notifObj;
        historyModel.remove(index);
        if (target) target.dismiss();
        return true;
    }

    // Row activation (left-click). Returns "invoked" when a default action
    // was invoked (nonresident rows remove via closed; resident rows persist
    // by design), "dismissed" when there was no default action and the row
    // was explicitly dismissed, "none" when the index is stale.
    function activateAt(index) {
        if (index < 0 || index >= historyModel.count) return "none";
        // Capture everything BEFORE invoking: the action may synchronously
        // close (and destroy) the notification, which removes the row via
        // closed. Touching index or target afterwards would double-remove or
        // use a destroyed object.
        var row = historyModel.get(index);
        var activateTarget = row ? row.notifObj : null;
        var defaultAction = null;
        // Resident rows persist by design (never explicitly dismissed here);
        // the invoke path below lets closed remove nonresident rows only.
        if (activateTarget && activateTarget.actions) {
            for (let i = 0; i < activateTarget.actions.length; i++) {
                if (activateTarget.actions[i].identifier === "default") {
                    defaultAction = activateTarget.actions[i];
                    break;
                }
            }
        }
        if (defaultAction) {
            // Activation, not dismissal: invoke and let closed remove
            // nonresident rows; resident rows persist by design.
            defaultAction.invoke();
            return "invoked";
        }
        historyModel.remove(index);
        if (activateTarget) activateTarget.dismiss();
        return "dismissed";
    }
}
