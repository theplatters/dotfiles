/*
 * ScreenshotAction.qml — single owner for delayed hyprshot captures.
 *
 * Responsibility: own the deferred screenshot delay, the setsid hyprshot
 * process, and the pending-mode/generation state. The palette keeps thin
 * scheduleScreenshot/cancelPendingScreenshot delegates (pinned by tests)
 * and owns immediateUnmap; both the palette (widgets/CommandPalette.qml)
 * and the bar's ScreenshotModule (widgets/ScreenshotModule.qml) consume
 * this component.
 *
 * Owned state/processes:
 * - pendingMode (string, hyprshot -m mode), generation (int, invalidates
 *   stale triggers), blocked (bool, mirrors palette requestedOpen or an
 *   in-flight action process).
 * - shotDelay Timer (180ms debounce so the palette unmaps first),
 *   shotProcess Process (setsid -f hyprshot -m <mode>).
 *
 * Contract with CommandPalette:
 * - In: `property bool blocked` (palette binds requestedOpen ||
 *   actionProcess.running so a trigger while mapped or busy drops safely).
 * - Out: `signal unmapRequested()` (palette runs immediateUnmap before
 *   the delay restarts, matching the pre-extraction schedule order).
 * - Palette delegates scheduleScreenshot/cancelPendingScreenshot here and
 *   calls cancelPendingScreenshot() on open/close like before.
 *
 * Generation/staleness: schedule bumps generation and restarts the delay;
 * cancel bumps generation and stops it. The trigger clears the pending
 * mode before the guard, so a blocked trigger drops the request exactly
 * like the pre-extraction screenshotDelay did.
 */
import QtQuick
import Quickshell
import Quickshell.Io

Item {
    id: root

    property string pendingMode: ""
    property int generation: 0
    property bool blocked: false

    signal unmapRequested()

    Timer {
        id: shotDelay
        interval: 180
        onTriggered: {
            let generation = root.generation;
            let mode = root.pendingMode;
            root.pendingMode = "";
            if (generation === 0 || !mode || root.blocked || shotProcess.running) return;
            shotProcess.command = root.screenshotCommand(mode);
            shotProcess.running = true;
        }
    }

    Process {
        id: shotProcess
    }

    function screenshotCommand(mode) {
        return ["setsid", "-f", "hyprshot", "-m", String(mode || "")];
    }

    function scheduleScreenshot(mode) {
        root.generation++;
        root.pendingMode = String(mode || "");
        root.unmapRequested();
        shotDelay.restart();
        return true;
    }

    function cancelPendingScreenshot() {
        root.generation++;
        root.pendingMode = "";
        shotDelay.stop();
    }
}
