/*
 * PaletteCapture.qml — region screen-capture flow for the command palette.
 *
 * Responsibility: own the region-selection state, the deferred screenshot
 * process, and the selection overlay. The palette orchestrates AI prompting;
 * this component only produces prompts/images or bounded failure notices.
 *
 * Owned state/processes:
 * - capturePrompt (string, preserved for retry), captureGeneration (int,
 *   invalidates stale runs), captureProcessGeneration/Prompt/Geometry/Command.
 * - selectingRegion (bool), selectionStart/Current X/Y (real),
 *   captureGeometry (string "x,y WxH").
 * - captureDelay Timer (180ms debounce so the overlay unmaps first),
 *   captureProcess Process (screen_capture.py --geometry, both streams
 *   drained with waitForEnd), selectionOverlay FocusScope + MouseArea UI.
 *
 * Contract with CommandPalette:
 * - In: `property var getScreenOffset` (function returning {x,y} from the
 *   palette screenOffset()), `property bool paletteVisible/paletteOpen`
 *   (mirror visible/requestedOpen for the overlay gate).
 * - Out signals: `reopenRequest(message)` (generic reopen; palette opens,
 *   resets query to "ai:", applies notice, restores "ai: "+capturePrompt
 *   when non-empty), `captured(prompt, images)` (palette reopens, restores
 *   the draft, and calls agent.prompt), `failed(message)` (palette reopens,
 *   restores the draft for retry, and applies the bounded notice).
 * - Palette also handles: `unmapRequested()` (immediateUnmap before the
 *   snapshot), `selectionStarted()` (stop enter/exit/show, restart hide),
 *   `selectionCancelled()` (restore requestedOpen/visible, restart show,
 *   notice "Capture cancelled", refocus input).
 * - capturePrompt is never cleared except by beginRegionSelection, so an
 *   async rejection or bridge death preserves it for retry.
 *
 * Generation/staleness: every snapshot pins captureProcessGeneration to the
 * live captureGeneration; onExited drops stale generations without touching
 * the draft. cancelPendingCapture() bumps captureGeneration, stops the
 * delay/process, and clears geometry/selection (prompt preserved).
 * Screenshot flow (scheduleScreenshot/screenshotDelay/actionProcess) stays
 * in CommandPalette.
 *
 * No new controls were added; the overlay keeps its Theme label/hint.
 */
import QtQuick
import Quickshell
import Quickshell.Io
import "../theme"
import "PaletteText.js" as PaletteText
import "PaletteGeometry.js" as PaletteGeometry

Item {
    id: root

    // Fullscreen layer-surface item: carries the former overlay stacking
    // (above the card z:1, below approval z:100) so the inner selection
    // overlay fills real dimensions and mouse coords clamp correctly.
    anchors.fill: parent
    z: 50

    property string capturePrompt: ""
    property int captureGeneration: 0
    property int captureProcessGeneration: 0
    property string captureProcessPrompt: ""
    property string captureProcessGeometry: ""
    property var captureProcessCommand: []
    property bool selectingRegion: false
    property real selectionStartX: 0
    property real selectionStartY: 0
    property real selectionCurrentX: 0
    property real selectionCurrentY: 0
    property string captureGeometry: ""
    property bool paletteVisible: false
    property bool paletteOpen: false
    property var getScreenOffset

    signal reopenRequest(string message)
    signal captured(string prompt, var images)
    signal failed(string message)
    signal unmapRequested()
    signal selectionStarted()
    signal selectionCancelled()

    Timer {
        id: captureDelay
        interval: 180
        onTriggered: {
            if (root.captureGeneration === 0 || !root.captureGeometry || captureProcess.running) {
                if (root.captureGeneration !== 0 && !root.captureGeometry) {
                    root.reopenRequest("No capture geometry was selected");
                }
                return;
            }
            root.captureProcessGeneration = root.captureGeneration;
            root.captureProcessPrompt = root.capturePrompt;
            root.captureProcessGeometry = root.captureGeometry;
            root.captureProcessCommand = ["python3", Quickshell.shellPath("scripts/screen_capture.py"),
                                          "--geometry", root.captureProcessGeometry];
            captureProcess.command = root.captureProcessCommand;
            captureProcess.running = true;
        }
    }

    Process {
        id: captureProcess
        stdout: StdioCollector {
            id: captureOutput
            waitForEnd: true
        }
        stderr: StdioCollector {
            id: captureError
            waitForEnd: true
        }
        onExited: (code) => {
            let generation = root.captureProcessGeneration;
            let prompt = root.captureProcessPrompt;
            if (code !== 0 || generation === 0 || generation !== root.captureGeneration) {
                if (generation !== 0 && generation === root.captureGeneration) {
                    root.failed(PaletteText.captureFailure(code, captureError.text));
                }
                return;
            }
            try {
                let data = JSON.parse(captureOutput.text);
                if (!prompt.trim()) {
                    root.reopenRequest("Screen request was empty");
                    return;
                }
                root.captured(prompt, data.images || []);
            } catch (error) {
                root.failed("Invalid screen capture output");
            }
        }
    }

    function beginRegionSelection(prompt) {
        root.capturePrompt = prompt;
        root.captureGeometry = "";
        root.selectionStartX = 0;
        root.selectionStartY = 0;
        root.selectionCurrentX = 0;
        root.selectionCurrentY = 0;
        root.selectingRegion = true;
        root.selectionStarted();
        Qt.callLater(function() { selectionOverlay.forceActiveFocus(); });
    }

    function finishRegionSelection() {
        if (!root.selectingRegion) return;
        let selection = PaletteGeometry.normalizedSelection(root.selectionStartX, root.selectionStartY,
                                            root.selectionCurrentX, root.selectionCurrentY);
        let offset = root.getScreenOffset ? root.getScreenOffset() : { x: 0, y: 0 };
        root.captureGeometry = PaletteGeometry.geometryForSelection(selection, offset);
        root.selectingRegion = false;
        if (!root.captureGeometry) {
            root.captureGeneration++;
            captureDelay.stop();
            root.reopenRequest("No capture geometry was selected");
            return;
        }
        // Snapshotting happens in captureDelay, after the in-window overlay is
        // gone and before any later open/close action can alter the geometry.
        root.unmapRequested();
        captureDelay.restart();
    }

    function cancelRegionSelection() {
        if (!root.selectingRegion) return;
        root.cancelPendingCapture();
        root.selectingRegion = false;
        root.selectionCancelled();
    }

    // Capture runs while the palette is unmapped. The palette reopens only
    // after the process has drained both streams so the user can see the
    // resulting prompt, answer, or bounded failure detail.
    function reopenCaptureAi(message) {
        root.reopenRequest(message || "");
    }

    function cancelPendingCapture() {
        root.captureGeneration++;
        captureDelay.stop();
        if (captureProcess.running) captureProcess.running = false;
        root.selectingRegion = false;
        root.selectionStartX = 0;
        root.selectionStartY = 0;
        root.selectionCurrentX = 0;
        root.selectionCurrentY = 0;
        root.captureGeometry = "";
        root.captureProcessGeometry = "";
        root.captureProcessCommand = [];
    }

    // Native selection stays on this fullscreen layer surface. Keeping it in
    // the same window makes the dimmer and pointer input reliable on Wayland.
    FocusScope {
        id: selectionOverlay
        z: 50
        anchors.fill: parent
        visible: root.paletteVisible && root.paletteOpen && root.selectingRegion
        enabled: visible
        focus: visible

        Rectangle {
            anchors.fill: parent
            color: "#B0070707"
        }

        Rectangle {
            id: selectionFill
            x: Math.min(root.selectionStartX, root.selectionCurrentX)
            y: Math.min(root.selectionStartY, root.selectionCurrentY)
            width: Math.max(4, Math.abs(root.selectionCurrentX - root.selectionStartX))
            height: Math.max(4, Math.abs(root.selectionCurrentY - root.selectionStartY))
            color: Theme.surface1
            opacity: 0.55
        }
        Rectangle {
            x: selectionFill.x
            y: selectionFill.y
            width: selectionFill.width
            height: selectionFill.height
            color: "transparent"
            border.color: Theme.focusBorder
            border.width: 2
        }

        Rectangle {
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.top: parent.top
            anchors.topMargin: 28
            width: selectionInstruction.implicitWidth + 28
            height: selectionInstruction.implicitHeight + 16
            radius: Theme.controlRadius
            color: Theme.base
            border.color: Theme.border
            Text {
                id: selectionInstruction
                anchors.centerIn: parent
                text: "Drag to select · Esc cancel"
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: 14
            }
        }

        MouseArea {
            id: selectionMouse
            anchors.fill: parent
            acceptedButtons: Qt.AllButtons
            preventStealing: true
            cursorShape: Qt.CrossCursor
            onPressed: (mouse) => {
                if (mouse.button !== Qt.LeftButton) {
                    mouse.accepted = true;
                    return;
                }
                root.selectionStartX = PaletteGeometry.clampSelectionCoordinate(mouse.x, root.width);
                root.selectionStartY = PaletteGeometry.clampSelectionCoordinate(mouse.y, root.height);
                root.selectionCurrentX = root.selectionStartX;
                root.selectionCurrentY = root.selectionStartY;
                mouse.accepted = true;
            }
            onPositionChanged: (mouse) => {
                if (!pressed || !(mouse.buttons & Qt.LeftButton)) return;
                root.selectionCurrentX = PaletteGeometry.clampSelectionCoordinate(mouse.x, root.width);
                root.selectionCurrentY = PaletteGeometry.clampSelectionCoordinate(mouse.y, root.height);
            }
            onReleased: (mouse) => {
                if (mouse.button !== Qt.LeftButton) {
                    mouse.accepted = true;
                    return;
                }
                root.selectionCurrentX = PaletteGeometry.clampSelectionCoordinate(mouse.x, root.width);
                root.selectionCurrentY = PaletteGeometry.clampSelectionCoordinate(mouse.y, root.height);
                root.finishRegionSelection();
                mouse.accepted = true;
            }
        }

        Keys.onPressed: (event) => {
            if (event.key === Qt.Key_Escape) {
                root.cancelRegionSelection();
                event.accepted = true;
            }
        }
    }
}
