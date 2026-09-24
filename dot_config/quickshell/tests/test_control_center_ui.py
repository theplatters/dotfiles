import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


def read(name):
    return (ROOT / "widgets" / name).read_text(encoding="utf-8")


class ControlCenterIntegrationTests(unittest.TestCase):
    def test_shell_hosts_single_control_center_and_no_legacy_popups(self):
        shell = (ROOT / "shell.qml").read_text(encoding="utf-8")
        self.assertIn("ControlCenter {", shell)
        self.assertIn("id: controlCenter", shell)
        self.assertNotIn("AudioPopup {", shell)
        self.assertNotIn("NetworkPopup {", shell)
        self.assertNotIn("BatteryPopup {", shell)
        # Backend wiring: tray anchor + persistent bar window + notif + passwords.
        self.assertIn("anchorItem:", shell)
        self.assertIn("trayAnchor", shell)
        self.assertIn("barWindow:", shell)
        self.assertIn("notifServer:", shell)
        self.assertIn("passwordPopup:", shell)
        self.assertIn("controlCenter: controlCenter", shell)

    def test_bar_exposes_unified_tray_anchor_and_toggle(self):
        bar = read("Bar.qml")
        self.assertIn("property var controlCenter", bar)
        self.assertIn("trayAnchor", bar)
        self.assertIn("property alias trayAnchor: unifiedTray", bar)
        self.assertIn("WidgetIconButton", bar)
        self.assertIn("chevron-down.svg", bar)
        self.assertNotIn("panel-right-open.svg", bar)
        self.assertIn("bar.controlCenter", bar)
        # Routing lives in the modules themselves (no overlay stealing):
        # Bar passes controlCenter down and the chevron toggles the popup.
        self.assertNotIn("toggleSection", bar)
        self.assertIn("controlCenter: bar.controlCenter", bar)
        # Single shared capsule first on the left with an always-present
        # toggle; no per-module pills or sidecar button beside the tray.
        self.assertIn("id: unifiedTray", bar)
        self.assertIn("id: trayToggle", bar)
        self.assertIn("id: trayRow", bar)
        self.assertNotIn("id: ccContainer", bar)
        self.assertNotIn("id: ccButton", bar)
        self.assertNotIn("id: audioContainer", bar)
        self.assertNotIn("id: networkContainer", bar)
        self.assertNotIn("id: batteryContainer", bar)
        self.assertNotIn("id: trayContainer", bar)
        self.assertIn("if (bar.controlCenter) bar.controlCenter.toggle()", bar)
        # Legacy fallback popup props are deleted (D1): one wired path per
        # control, straight into ControlCenter.
        self.assertNotIn("property var audioPopup", bar)
        self.assertNotIn("property var networkPopup", bar)
        self.assertNotIn("property var batteryPopup", bar)
        self.assertNotIn("audioPopup", bar)
        self.assertNotIn("networkPopup", bar)
        self.assertNotIn("batteryPopup", bar)

    def test_modules_route_into_cc_tabs_and_preserve_tray_actions(self):
        audio = read("AudioModule.qml")
        self.assertIn("property var controlCenter", audio)
        self.assertIn("toggleSection(0)", audio)
        # Left-mute / wheel behaviour preserved.
        self.assertIn("audio.muted = !", audio)
        self.assertIn("onWheel", audio)

        network = read("NetworkModule.qml")
        self.assertIn("property var controlCenter", network)
        self.assertIn("toggleSection(1)", network)

        battery = read("BatteryModule.qml")
        self.assertIn("property var controlCenter", battery)
        self.assertIn("toggleSection(3)", battery)
        # Old hover tooltip trigger is gone; click opens Power.
        self.assertNotIn("showMessage", battery)
        self.assertNotIn("onHoveredChanged", battery)
        self.assertIn("onClicked", battery)

        tray = read("TrayModule.qml")
        self.assertIn("modelData.activate()", tray)
        self.assertIn("modelData.secondaryActivate()", tray)
        self.assertIn("modelData.display(", tray)

    def test_anchor_left_aligns_under_unified_tray(self):
        cc = read("ControlCenter.qml")
        bar = read("Bar.qml")
        # Item-relative anchor, no global-coordinate rect.
        self.assertIn("anchor {", cc)
        self.assertIn("item: root.anchorItem", cc)
        self.assertIn("Edges.Bottom | Edges.Left", cc)
        self.assertIn("Edges.Bottom | Edges.Right", cc)
        self.assertIn("margins.top: 4", cc)
        self.assertNotIn("margins.top: 8", cc)
        self.assertNotIn("mapToGlobal", cc)
        self.assertNotIn("mapToItem", cc)
        # Toggle/open never reassign the tray anchor.
        toggle = cc[cc.index("function toggle("):cc.index("function openSection")]
        self.assertNotIn("anchorItem =", toggle)
        self.assertIn("property alias trayAnchor", bar)
        self.assertIn("property alias trayAnchor: unifiedTray", bar)
        # Tray-following hook stays item-relative and actually recalculates:
        # the verified anchor.updateAnchor() API (Quickshell only computes
        # the item-relative rect at show time; re-assigning the same item
        # would be a no-op for motion).
        self.assertIn("function updateAnchor()", cc)
        self.assertIn("root.anchor.updateAnchor()", cc)
        self.assertIn("onAnchorItemChanged", cc)
        self.assertIn("onScreenChanged", cc)
        self.assertIn("onWidthChanged", cc)
        update = cc[cc.index("function updateAnchor()"):cc.index("onAnchorItemChanged")]
        self.assertNotIn("mapToGlobal", update)
        self.assertNotIn("mapToItem", update)
        # Ancestor motion (rows/slots/bar shifting the tray without touching
        # its own x/y) is tracked via the verified TransformWatcher on the
        # Bar to tray path, plus a fresh recalc on every open.
        self.assertIn("TransformWatcher", cc)
        self.assertIn("property var trayScope", cc)
        self.assertIn("onTransformChanged", cc)
        self.assertIn("trayScope: bar", (ROOT / "shell.qml").read_text(encoding="utf-8"))


class UnifiedTrayStructureTests(unittest.TestCase):
    def _capsule(self, bar):
        start = bar.index("id: unifiedTray")
        end = bar.index("NotificationModule", start)
        return bar[start:end]

    def test_status_modules_live_inside_one_shared_capsule(self):
        bar = read("Bar.qml")
        capsule = self._capsule(bar)
        for marker in ("id: trayRow", "id: trayToggle", "id: audioModule",
                       "id: networkModule", "id: batteryModule"):
            self.assertIn(marker, capsule, marker)
        # SNI tray lives in its own right-slot capsule (never overlapped by
        # CurrentProjectModule in the unified tray).
        self.assertIn("id: sysTrayModule", bar)
        self.assertNotIn("id: trayModule", capsule)
        # One outer border/background: subtle separators, not per-status pills.
        self.assertIn("Theme.mantle", capsule)
        self.assertIn("Theme.surface0", capsule)
        self.assertIn("Theme.accentMuted", capsule)
        self.assertIn("width: 1; height: 16; color: Theme.border", capsule)
        # Active capsule follows the popup state on the shared fast token.
        self.assertIn("requestedOpen", capsule)
        self.assertIn("Theme.motionFast", capsule)

    def test_no_overlay_steals_module_events(self):
        bar = read("Bar.qml")
        capsule = self._capsule(bar)
        # Modules handle their own clicks. The only MouseAreas inside the
        # capsule are wheel-only scrollers: they accept no buttons, so clicks
        # (left/middle/right) always reach the module handlers underneath.
        self.assertNotIn("Qt.LeftButton", capsule)
        self.assertNotIn("Qt.RightButton", capsule)
        self.assertNotIn("Qt.MiddleButton", capsule)
        # Wheel-only tray scroller lives in the separate right-slot sysTray
        # capsule (same NoButton idiom, never steals clicks).
        self.assertIn("acceptedButtons: Qt.NoButton", bar)
        self.assertIn("onWheel", bar)
        tray = read("TrayModule.qml")
        self.assertIn("modelData.activate()", tray)
        self.assertIn("modelData.secondaryActivate()", tray)
        # Battery only occupies the row when a battery exists; sysTray slot
        # hides when the tray is empty.
        self.assertIn("UPower.displayDevice.type === 2", capsule)
        self.assertIn("visible: sysTrayModule.implicitWidth > 0", bar)
        self.assertIn("id: sysTrayModule", bar)

    def test_modules_keep_full_height_hit_areas_and_fallbacks(self):
        bar = read("Bar.qml")
        capsule = self._capsule(bar)
        for module in ("audioModule", "networkModule", "batteryModule"):
            section = capsule[capsule.index("id: " + module):capsule.index("id: " + module) + 400]
            self.assertIn("height: 32", section, module)
            self.assertIn("anchors.verticalCenter", section, module)
        # SNI tray lives in its own right-slot capsule at full height.
        sys_section = bar[bar.index("id: sysTrayModule"):bar.index("id: sysTrayModule") + 400]
        self.assertIn("height: 32", sys_section, "sysTrayModule")
        self.assertIn("anchors.verticalCenter", sys_section, "sysTrayModule")
        self.assertNotIn("audioPopup: bar.audioPopup", capsule)
        self.assertNotIn("networkPopup: bar.networkPopup", capsule)
        self.assertNotIn("batteryPopup: bar.batteryPopup", capsule)
        self.assertNotIn("audioPopup", capsule)
        self.assertNotIn("networkPopup", capsule)
        self.assertNotIn("batteryPopup", capsule)

    def test_notifications_clock_screenshot_stats_keep_places(self):
        bar = read("Bar.qml")
        self.assertIn("NotificationModule", bar)
        self.assertIn("id: clockContainer", bar)
        self.assertIn("ScreenshotModule", bar)
        self.assertIn("id: statsContainer", bar)
        self.assertIn("WorkspaceModule", bar)
        self.assertIn("id: mediaContainer", bar)
        left = bar.index("id: leftRow")
        tray = bar.index("id: unifiedTray")
        center = bar.index("id: centerRow")
        right = bar.index("id: rightRow")
        shot = bar.index("ScreenshotModule")
        # Unified tray lives inside the left row, before center/right.
        self.assertLess(left, tray)
        self.assertLess(tray, center)
        self.assertLess(center, right)
        self.assertLess(right, shot)


class BarSizingTests(unittest.TestCase):
    def test_sides_measured_center_bounded(self):
        bar = read("Bar.qml")
        # No equal-thirds fillWidth regions.
        self.assertNotIn("Layout.fillWidth: true\n            Layout.fillHeight: true\n            Row {\n                id: leftRow", bar)
        self.assertIn("Layout.preferredWidth: leftRow.implicitWidth", bar)
        self.assertIn("Layout.preferredWidth: rightRow.implicitWidth", bar)
        self.assertIn("Layout.minimumWidth: leftRow.implicitWidth", bar)
        self.assertIn("Layout.minimumWidth: rightRow.implicitWidth", bar)
        self.assertIn("Layout.fillWidth: true", bar)
        self.assertIn("Layout.minimumWidth: 0", bar)
        self.assertIn("clip: true", bar)
        self.assertIn("availableCenterSpace", bar)
        self.assertIn("compactClock", bar)

    def test_compact_media_avoids_implicit_width_cycle(self):
        bar = read("Bar.qml")
        compact_line = [line for line in bar.splitlines() if "compactMedia" in line and "readonly property" in line]
        self.assertTrue(compact_line, "compactMedia property missing")
        self.assertNotIn("implicitWidth", compact_line[0])
        # Decided from the budgeted remainder (workspace-first), which itself
        # derives from measured side widths, never the module's own width.
        # No hard width gate: pure measured-budget logic.
        self.assertIn("mediaFitsFull", compact_line[0])
        self.assertNotIn("1400", compact_line[0])
        self.assertNotIn("bar.width", compact_line[0])
        remainder = [line for line in bar.splitlines() if "readonly property real mediaRemainder" in line]
        self.assertTrue(remainder)
        self.assertIn("availableCenterSpace", remainder[0])
        self.assertIn("workspaceNeed", remainder[0])
        # Side reserve derives from implicit widths only (no layout feedback).
        reserve = [line for line in bar.splitlines() if "sideReserve" in line and "readonly property" in line]
        self.assertTrue(reserve)
        self.assertIn("implicitWidth", reserve[0])
        self.assertNotIn(".width -", reserve[0].replace("implicitWidth", ""))


class OverflowSafetyTests(unittest.TestCase):
    def test_network_label_bounded_with_full_name_preserved(self):
        net = read("NetworkModule.qml")
        # Hard cap + elide in the bar; full name kept for ControlCenter.
        self.assertIn("property int maxLabelWidth", net)
        self.assertIn("id: ssidText", net)
        self.assertIn("Text.ElideRight", net)
        self.assertIn("Math.min(implicitWidth, root.maxLabelWidth)", net)
        self.assertIn("property string fullLabel", net)
        self.assertIn("activeWifi", net.split("property string fullLabel")[1].split("labelNaturalWidth")[0])
        # QQuickRow reports laid-out (actual) widths, so the caps already
        # bound the module: plain Row extent, no manual correction (which
        # would subtract the capped excess twice and go negative).
        self.assertIn("implicitWidth: layout.implicitWidth", net)
        self.assertNotIn("ssidText.implicitWidth - ssidText.width", net)
        self.assertIn("labelNaturalWidth", net)

    def test_media_title_bound_is_real(self):
        media = read("MediaModule.qml")
        # QQuickRow reports laid-out (actual) widths: the 200px paint cap
        # already bounds the module, so it reports the plain Row extent.
        self.assertIn("implicitWidth: layout.implicitWidth", media)
        self.assertNotIn("titleText.implicitWidth - titleText.width", media)
        self.assertIn("Math.min(implicitWidth, 200)", media)

    def test_tray_viewport_capped_and_scrollable(self):
        bar = read("Bar.qml")
        self.assertIn("id: sysTrayFlick", bar)
        decl = bar[bar.index("id: sysTrayFlick") - 250:bar.index("id: sysTrayFlick") + 30]
        self.assertIn("Flickable", decl)
        flick = bar[bar.index("id: sysTrayFlick"):bar.index("id: sysTrayFlick") + 1400]
        self.assertIn("Math.min(sysTrayModule.implicitWidth + 4, 148)", flick)
        self.assertIn("flickableDirection: Flickable.HorizontalFlick", flick)
        self.assertIn("boundsBehavior: Flickable.StopAtBounds", flick)
        self.assertIn("acceptedButtons: Qt.NoButton", flick)
        self.assertIn("onWheel", flick)
        self.assertIn("contentX", flick)
        # Tray icons keep native actions and no wheel handler of their own,
        # so the viewport wheel catcher cannot shadow anything.
        tray = read("TrayModule.qml")
        self.assertNotIn("onWheel", tray)

    def test_center_scroller_keeps_every_control_reachable(self):
        bar = read("Bar.qml")
        self.assertIn("id: centerFlick", bar)
        self.assertIn("id: mediaMini", bar)
        self.assertIn("showMediaCapsule", bar)
        self.assertIn("showMediaMini", bar)
        self.assertIn("mediaFitsFull", bar)
        self.assertIn("mediaFitsCompact", bar)
        self.assertIn("workspaceNeed", bar)
        # Workspace is budgeted first (natural width + capsule + spacing).
        need = [line for line in bar.splitlines() if "workspaceNeed" in line and "readonly property real workspaceNeed" in line]
        self.assertTrue(need)
        self.assertIn("workspaceModule.implicitWidth", need[0])
        # Row centers when it fits, left-aligns into the scroller on overflow.
        self.assertIn("x: Math.max(0, (centerFlick.width - implicitWidth) / 2)", bar)
        # Mini entry opens the popout, so media is never clipped away.
        mini = bar[bar.index("id: mediaMini"):bar.index("id: rightSlot")]
        self.assertIn("visible: bar.showMediaMini", mini)
        self.assertIn("mediaPopout.toggle(mediaMini)", mini)
        # Capsule shows only when its (full/compact) need fits the remainder.
        self.assertIn("visible: bar.showMediaCapsule", bar)

    def test_tight_sides_compact_is_oscillation_free(self):
        bar = read("Bar.qml")
        net = read("NetworkModule.qml")
        self.assertIn("sideReserveWorst", bar)
        self.assertIn("tightSides", bar)
        self.assertIn("compact: bar.compactNetwork || bar.tightSides", bar)
        # Worst case = measured reserve minus the module's actual width plus
        # its compact-independent fullWidth: toggling compact moves both
        # terms equally, so the result (and therefore tightSides) cannot
        # flap the condition that sets compact.
        worst = [line for line in bar.splitlines() if "readonly property real sideReserveWorst" in line]
        self.assertTrue(worst)
        self.assertIn("networkModule.fullWidth", worst[0])
        self.assertIn("- networkModule.implicitWidth", worst[0])
        # fullWidth covers the BT count too (hidden by compact as well), and
        # is built only from compact-independent terms.
        self.assertIn("btCountNaturalWidth", net)
        self.assertIn("id: btCountText", net)
        self.assertIn("Math.min(implicitWidth, 36)", net)
        full_start = net.index("readonly property real fullWidth")
        full_block = net[full_start:net.index("\n", net.index("+ 24", full_start))]
        self.assertIn("labelNaturalWidth", full_block)
        self.assertIn("btCountNaturalWidth", full_block)
        self.assertNotIn("compact", full_block)


class ControlCenterMotionTests(unittest.TestCase):
    def test_reveal_progress_drives_top_origin_chrome(self):
        cc = read("ControlCenter.qml")
        self.assertIn("property real reveal", cc)
        self.assertIn("transformOrigin: Item.TopLeft", cc)
        self.assertIn("opacity: root.reveal", cc)
        self.assertIn("0.96 + 0.04 * root.reveal", cc)
        self.assertIn("Translate", cc)
        self.assertIn("(1 - root.reveal)", cc)
        self.assertIn("clip: true", cc)
        # Anchored panel itself never animates y (invalid on anchors.fill).
        panel = cc[cc.index("id: panel"):cc.index("id: enterMotion")]
        self.assertNotIn('property: "y"', panel)
        self.assertNotIn("anchors.topMargin", cc[cc.index("id: enterMotion"):])

    def test_single_progress_motion_without_spring(self):
        cc = read("ControlCenter.qml")
        self.assertIn("id: enterMotion", cc)
        self.assertIn("id: exitMotion", cc)
        self.assertIn('property: "reveal"', cc)
        self.assertIn("to: 1", cc)
        self.assertIn("to: 0", cc)
        self.assertIn("Theme.motionPanel", cc)
        self.assertIn("Theme.motionExit", cc)
        self.assertIn("Easing.OutCubic", cc)
        self.assertIn("Easing.InCubic", cc)
        self.assertNotIn("OutBack", cc)
        self.assertNotIn("OutElastic", cc)
        self.assertNotIn("Spring", cc)

    def test_interrupted_open_reverses_without_reset_flash(self):
        cc = read("ControlCenter.qml")
        setter = cc[cc.index("function setOpen(open)"):cc.index("function toggle()")]
        code = "\n".join(line.split("//", 1)[0] for line in setter.splitlines())
        # Closing branch reverses from current; opening never zeroes progress.
        self.assertIn("exitMotion.stop()", code)
        self.assertIn("enterMotion.stop()", code)
        self.assertIn("enterMotion.restart()", code)
        self.assertIn("exitMotion.restart()", code)
        self.assertNotIn("reveal = 0", code)
        self.assertNotIn("reveal=0", code)
        self.assertNotIn("panel.opacity = 0", code)
        self.assertNotIn("panel.scale", code)
        # Already-open steady state returns early (section switches tabs via
        # selectedTab without replaying the reveal).
        self.assertIn("reveal >= 0.99", setter)
        self.assertIn("return;", setter)
        # Exit completion hides only when still requested-closed.
        self.assertIn("if (!root.requestedOpen) root.visible = false", cc)
        # Explicit dismiss affordances unchanged.
        self.assertIn('sequence: "Escape"', cc)
        self.assertIn('iconSource: "icons/x.svg"', cc)


class ControlCenterPanelExtractionTests(unittest.TestCase):
    def test_audio_panel_is_embeddable_and_legacy_wrapper_is_deleted(self):
        panel = read("AudioPanel.qml")
        self.assertFalse((ROOT / "widgets" / "AudioPopup.qml").exists())
        self.assertTrue(panel.lstrip().startswith("import QtQuick"))
        self.assertIn("Item {", panel.split("import", 1)[0] if False else panel)
        # Functional audio content lives in the panel.
        for marker in ("Pipewire.nodes", "outputDevices", "inputDevices",
                       "function setVolume(", "function toggleMute(",
                       "function setDefault(", "No output devices",
                       "No input devices", "No active streams"):
            self.assertIn(marker, panel, marker)
        # No popup chrome in the panel.
        self.assertNotIn("PopupWindow {", panel)
        self.assertNotIn("requestedOpen", panel)

    def test_network_panel_is_embeddable_and_legacy_wrapper_is_deleted(self):
        panel = read("NetworkPanel.qml")
        self.assertFalse((ROOT / "widgets" / "NetworkPopup.qml").exists())
        self.assertIn("Item {", panel)
        for marker in ("wifiDevice", "wiredDevice", "bluetoothAdapter",
                       "function connectNetwork(", "function disconnectNetwork(",
                       "function forgetNetwork(", "function toggleBluetoothDevice(",
                       "passwordPopup", "pendingNetwork", "pendingSettings",
                       "Saved - ", "No Wi-Fi adapter", "No Bluetooth adapter",
                       "WidgetButton {"):
            self.assertIn(marker, panel, marker)
        self.assertNotIn("PopupWindow {", panel)
        self.assertNotIn("requestedOpen", panel)

    def test_panels_persist_inside_control_center(self):
        cc = read("ControlCenter.qml")
        self.assertIn("AudioPanel {", cc)
        self.assertIn("NetworkPanel {", cc)
        self.assertIn("id: audioPanel", cc)
        self.assertIn("id: netPanel", cc)
        self.assertIn("passwordPopup: root.passwordPopup", cc)
        # No conditional Loader that would drop pending password state.
        self.assertNotIn("Loader {", cc)


class ControlCenterDiscoveryTests(unittest.TestCase):
    def test_active_gates_scanning_and_never_scans_hidden_tabs(self):
        panel = read("NetworkPanel.qml")
        self.assertIn("property bool active", panel)
        sync = panel[panel.index("function syncDiscovery()"):panel.index("Connections {", panel.index("function syncDiscovery()"))]
        self.assertIn("root.active && root.visible", sync)
        self.assertIn("root.activeTab === 0", sync)
        self.assertIn("root.activeTab === 1", sync)
        self.assertIn("scannerEnabled = wantWifi", sync)
        self.assertIn("discovering = wantBt", sync)

    def test_discovery_resyncs_on_visibility_tab_device_power(self):
        panel = read("NetworkPanel.qml")
        for marker in ("onVisibleChanged", "onActiveChanged", "onActiveTabChanged",
                       "onHasWifiChanged", "onHasBluetoothChanged",
                       "onWifiEnabledChanged", "onWifiHardwareEnabledChanged",
                       "onEnabledChanged", "onDefaultAdapterChanged",
                       "onWifiDeviceChanged", "onBluetoothAdapterChanged"):
            self.assertIn(marker, panel, marker)

    def test_discovery_stops_replaced_adapters_before_enabling(self):
        panel = read("NetworkPanel.qml")
        self.assertIn("stopStaleAdapters()", panel)
        self.assertIn("_lastWifiDevice", panel)
        self.assertIn("_lastBtAdapter", panel)
        self.assertIn("scannerEnabled = false", panel)
        self.assertIn("discovering = false", panel)

    def test_wifi_helper_failure_clears_pending_and_secrets(self):
        panel = read("NetworkPanel.qml")
        self.assertIn("function failWifiStart()", panel)
        self.assertIn("could not start helper", panel)
        self.assertIn("_launched", panel)
        self.assertIn("_started", panel)
        self.assertIn("onRunningChanged", panel)
        fail = panel[panel.index("function failWifiStart()"):panel.index("function runWifiCommand")]
        self.assertIn('pendingPassword = ""', fail)
        self.assertIn("pendingNetwork = null", fail)
        self.assertIn("pendingSettings = null", fail)

    def test_cc_routes_tabs_and_gates_panel_active(self):
        cc = read("ControlCenter.qml")
        # Network=0, Bluetooth=1 mapping from CC tabs.
        self.assertIn("netPanel.activeTab = 0", cc)
        self.assertIn("netPanel.activeTab = 1", cc)
        self.assertIn("active: root.requestedOpen && root.visible", cc)
        self.assertIn("root.selectedTab === 1 || root.selectedTab === 2", cc)
        self.assertIn("netPanel.syncDiscovery()", cc)


class ControlCenterDndTests(unittest.TestCase):
    def test_shell_notif_server_has_inhibit_flag(self):
        shell = (ROOT / "shell.qml").read_text(encoding="utf-8")
        self.assertIn("property bool inhibit", shell)
        # Unqualified same-name bindings resolve to the child's own null
        # property inside the per-screen delegate, so consumers go through
        # the delegate-root qualified refs.
        self.assertIn(
            "readonly property var notifServerRef: notifServer", shell)
        self.assertIn("notifServer: barWindow.notifServerRef", shell)

    def test_cc_toggles_inhibit_and_popout_suppresses_banners(self):
        cc = read("ControlCenter.qml")
        self.assertIn("property var notifServer", cc)
        self.assertIn("notifServer.inhibit", cc)
        self.assertIn("DND On", cc)
        self.assertIn("DND Off", cc)

        popout = read("NotificationPopout.qml")
        self.assertIn("notifServer.inhibit", popout)
        self.assertIn("if (root.inhibited) return;", popout)
        self.assertIn("clearBanners()", popout)
        self.assertIn("onInhibitedChanged", popout)
        self.assertIn("activeNotifs.count > 0 && !root.inhibited", popout)

    def test_banner_never_tracks_and_history_is_sole_owner(self):
        popout = read("NotificationPopout.qml")
        history = read("NotificationHistory.qml")
        module = read("NotificationModule.qml")
        # Banner holds bare references: no tracked writes and no expire.
        # Strip // comments so explanatory notes don't trip the check.
        code = "\n".join(line.split("//", 1)[0] for line in popout.splitlines())
        self.assertNotIn("n.tracked", code)
        self.assertNotIn("tracked = false", code)
        self.assertNotIn("tracked=false", code)
        self.assertNotIn(".expire()", code)
        # History owns tracking and closure in the shell-level store.
        self.assertIn("n.tracked = true", history)
        self.assertIn("n.closed.connect", history)
        # The per-bar bell is a view: no tracking of its own.
        view_code = "\n".join(line.split("//", 1)[0] for line in module.splitlines())
        self.assertNotIn("n.tracked", view_code)
        self.assertNotIn("tracked = true", view_code)
        self.assertNotIn("ListModel {", module)
        self.assertNotIn("onNotification", module)
        self.assertIn("history.clearAll()", module)
        self.assertIn("history.dismissAt(", module)
        self.assertIn("history.activateAt(", module)
        # Explicit banner actions dismiss (history closes too); expiry and
        # DND hiding are model-only.
        self.assertIn("function userDismiss(", popout)
        self.assertIn("function removeBanner(", popout)
        self.assertIn("root.removeBanner(notifId)", popout)
        self.assertIn("root.userDismiss(notifId)", popout)
        self.assertNotIn("root.dismiss(notifId)", popout)
        # Clear path touches the model only.
        clear = popout[popout.index("function clearBanners()"):popout.index("onInhibitedChanged")]
        self.assertIn("activeNotifs.remove(", clear)
        self.assertNotIn("dismiss()", clear)

    def test_history_still_collects_while_dnd(self):
        history = read("NotificationHistory.qml")
        module = read("NotificationModule.qml")
        self.assertIn("historyModel", history)
        # The shell-level history store must not consult the DND flag, and
        # neither must the bell view.
        self.assertNotIn("inhibit", history)
        self.assertNotIn("inhibit", module)


class ControlCenterPowerToggleTests(unittest.TestCase):
    def test_backend_is_persistent_and_wired_to_bar_window(self):
        cc = read("ControlCenter.qml")
        self.assertIn("ControlCenterSystem {", cc)
        self.assertIn("id: ccSystem", cc)
        self.assertIn("active: root.requestedOpen", cc)
        self.assertIn("inhibitWindow: root.barWindow", cc)
        self.assertIn("property var barWindow", cc)
        # Persistent: outside any Loader (none exists).
        self.assertNotIn("Loader {", cc)

    def test_quick_controls_visible_alongside_tabs(self):
        cc = read("ControlCenter.qml")
        for marker in ("Brightness", "brightnessSlider", "ccSystem.setBrightness",
                       "ccSystem.brightness", "DND On", "Night On", "Awake On",
                       "toggleNightLight()", "idleInhibited"):
            self.assertIn(marker, cc, marker)
        # Brightness stays enabled while busy: backend coalesces, so the
        # control must gate on availability only (never mid-drag).
        slider = cc[cc.index("id: brightnessSlider"):cc.index("onMoved")]
        self.assertIn("ccSystem.brightnessAvailable", slider)
        self.assertNotIn("brightnessBusy", slider)
        # New controls use shared buttons.
        self.assertIn("WidgetButton {", cc)
        # Tabs use WidgetButton, header close uses WidgetIconButton x.svg.
        self.assertIn('text: "Audio"', cc)
        self.assertIn('text: "Network"', cc)
        self.assertIn('text: "Bluetooth"', cc)
        self.assertIn('text: "Power"', cc)
        self.assertIn("WidgetIconButton {", cc)
        self.assertIn('iconSource: "icons/x.svg"', cc)
        self.assertIn('sequence: "Escape"', cc)

    def test_power_view_reports_battery_gracefully(self):
        cc = read("ControlCenter.qml")
        for marker in ("UPower.displayDevice.percentage", "timeToFull",
                       "timeToEmpty", "No battery", "Charging", "Discharging"):
            self.assertIn(marker, cc, marker)

    def test_cc_uses_mantle_panel_surface_cards_and_scroller(self):
        cc = read("ControlCenter.qml")
        self.assertIn("color: Theme.mantle", cc)
        self.assertIn("color: Theme.surface0", cc)
        self.assertIn("Flickable {", cc)
        self.assertIn("id: bodyFlick", cc)
        self.assertIn("requestedOpen", cc)
        self.assertIn("function setOpen(open)", cc)

    def test_cc_popup_bound_to_anchor_screen_and_resets_scroll(self):
        cc = read("ControlCenter.qml")
        self.assertIn("anchorScreen", cc)
        self.assertIn("Quickshell.screens", cc)
        self.assertIn("maxPopupWidth", cc)
        self.assertIn("maxPopupHeight", cc)
        self.assertIn("availableBodyHeight", cc)
        # Bar strip + anchor gap accounted for in the height bound.
        self.assertIn("anchorScreen.height - 40 - 4", cc)
        # Scroll resets so header/quick controls stay reachable on tabs.
        self.assertIn("bodyFlick.contentY = 0", cc)
        self.assertIn("maxListHeight", cc)

    def test_panels_are_content_sized_not_fixed(self):
        for name in ("AudioPanel.qml", "NetworkPanel.qml"):
            panel = read(name)
            self.assertIn("maxListHeight", panel)
            self.assertIn("Layout.maximumHeight: root.maxListHeight", panel)
            # Natural height: inner content layout id + root binding.
            self.assertIn("id: contentLayout", panel)
            self.assertIn("implicitHeight: contentLayout.implicitHeight", panel)
        self.assertNotIn("implicitHeight: 460", read("AudioPanel.qml"))
        self.assertNotIn("implicitHeight: 440", read("NetworkPanel.qml"))

    def test_cc_tray_toggle_fits_bar_row(self):
        bar = read("Bar.qml")
        icon = read("WidgetIconButton.qml")
        toggle = bar[bar.index("id: trayToggle"):bar.index("id: trayToggle") + 1600]
        self.assertIn("width: 32", toggle)
        self.assertIn("height: 32", toggle)
        self.assertNotIn("width: 28", toggle)
        self.assertIn("chevron-down.svg", toggle)
        # Glyph-only rotation: the inner Image rotates, the Button itself
        # stays at rotation 0 with zero style insets so the square background
        # never protrudes mid-rotation (45/90/135deg).
        self.assertIn("iconRotation", toggle)
        self.assertIn("180", toggle)
        self.assertIn("Behavior on iconRotation", toggle)
        self.assertNotIn("Behavior on rotation", toggle.replace("Behavior on iconRotation", ""))
        self.assertNotIn("\n                            rotation:", toggle)
        for inset in ("topInset: 0", "bottomInset: 0", "leftInset: 0", "rightInset: 0"):
            self.assertIn(inset, toggle, inset)
        self.assertIn("Theme.motionFast", toggle)
        # Shared control exposes the glyph rotation (default 0 = no change).
        self.assertIn("property real iconRotation", icon)
        self.assertIn("rotation: root.iconRotation", icon)
        self.assertIn("transformOrigin: Item.Center", icon)
        self.assertIn('objectName: "iconImage"', icon)

    def test_cc_orders_tabs_after_quick_controls(self):
        cc = read("ControlCenter.qml")
        header = cc.index("id: headerRow")
        quick = cc.index("id: quickCard")
        divider = cc.index("id: dividerRect")
        tabs = cc.index("id: tabsRow")
        body = cc.index("id: bodyFlick")
        self.assertGreater(quick, header)
        self.assertGreater(divider, quick)
        self.assertGreater(tabs, divider)
        self.assertGreater(body, tabs)
        # Tabs sit directly above the body: no extra fixed spacer between
        # the tabs block and the body Flickable.
        between = cc[cc.index('objectName: "tabsRow"'):cc.index('objectName: "bodyFlick"')]
        self.assertNotIn("Layout.preferredHeight: 12", between)
        self.assertNotIn("height: 12", between)
        self.assertNotIn("Rectangle {\n                id: spacer", between)
        # Global quick controls stay available with the backend unchanged.
        for marker in ("Brightness", "brightnessSlider", "ccSystem.setBrightness",
                       "DND On", "Night On", "Awake On", "ControlCenterSystem {"):
            self.assertIn(marker, cc, marker)

    def test_cc_hides_duplicate_network_selector(self):
        panel = read("NetworkPanel.qml")
        cc = read("ControlCenter.qml")
        audio = read("AudioPanel.qml")
        self.assertIn("property bool showTabSelector", panel)
        self.assertIn("property bool showTabSelector: true", panel)
        # Selector row + divider collapse when hidden (no reserved height).
        self.assertIn('objectName: "tabSelectorRow"', panel)
        self.assertIn('objectName: "tabSelectorDivider"', panel)
        self.assertIn("visible: root.showTabSelector", panel)
        self.assertIn("? 30 : 0", panel)
        self.assertIn("? 1 : 0", panel)
        # Embedded use hides the duplicate; standalone default keeps it.
        self.assertIn("showTabSelector: false", cc)
        self.assertIn('objectName: "netPanel"', cc)
        # Routing/discovery/password behavior preserved.
        for marker in ("netPanel.activeTab = 0", "netPanel.activeTab = 1",
                       "active: root.requestedOpen && root.visible",
                       "passwordPopup: root.passwordPopup"):
            self.assertIn(marker, cc, marker)
        for marker in ("property int activeTab", "function syncDiscovery()",
                       "passwordPopup", "pendingNetwork"):
            self.assertIn(marker, panel, marker)
        # Audio keeps its own Output/Input/Streams tabs (not a duplicate).
        self.assertIn('["Output", "Input", "Streams"]', audio)


HEIGHT_HARNESS = """//@ pragma UseQApplication
import QtQuick
import Quickshell
import Quickshell.Wayland

ShellRoot {
    PanelWindow {
        anchors { top: true; left: true; right: true }
        implicitHeight: 40
        color: "transparent"
        exclusionMode: ExclusionMode.Ignore
        WlrLayershell.layer: WlrLayer.Overlay
        Column {
            Loader {
                id: audioLoader
                source: "WIDGETS_URL/AudioPanel.qml"
                width: 380
                onLoaded: { if (item) item.width = 380; }
            }
            Loader {
                id: netLoader
                source: "WIDGETS_URL/NetworkPanel.qml"
                width: 380
                onLoaded: { if (item) item.width = 380; }
            }
        }
        Timer {
            interval: 3000
            running: true
            repeat: false
            onTriggered: {
                var aH = audioLoader.item ? audioLoader.item.implicitHeight : -1;
                var nH = netLoader.item ? netLoader.item.implicitHeight : -1;
                console.log("CC-HEIGHT-CHECK audio=" + aH + " network=" + nH);
                console.log("CC-HEIGHT-CHECK " + ((aH > 0 && nH > 0) ? "PASS" : "FAIL"));
                Qt.quit();
            }
        }
    }
}
"""


@unittest.skipUnless(shutil.which("quickshell"), "quickshell is required to instantiate panels")
class ControlCenterPanelHeightTests(unittest.TestCase):
    def test_panels_instantiate_with_nonzero_implicit_height(self):
        widgets_url = "file://" + str(ROOT / "widgets")
        source = HEIGHT_HARNESS.replace("WIDGETS_URL", widgets_url)
        with tempfile.TemporaryDirectory(prefix="cc-height-") as tmp:
            shell = Path(tmp) / "shell.qml"
            shell.write_text(source, encoding="utf-8")
            try:
                completed = subprocess.run(
                    ["quickshell", "-p", str(shell)],
                    text=True, capture_output=True, timeout=30,
                )
            except subprocess.TimeoutExpired as exc:
                output = str(exc.stdout or "") + str(exc.stderr or "")
                self.fail("quickshell height harness timed out: " + output[-2000:])
        output = str(completed.stdout or "") + str(completed.stderr or "")
        self.assertIn("CC-HEIGHT-CHECK PASS", output, output[-3000:])
        heights = {}
        for line in output.splitlines():
            if "CC-HEIGHT-CHECK audio=" in line:
                parts = line.split("audio=")[1].split()
                audio = int(parts[0])
                network = int(line.split("network=")[1].split()[0].rstrip("}"))
                heights = {"audio": audio, "network": network}
        self.assertGreater(heights.get("audio", 0), 0, output[-3000:])
        self.assertGreater(heights.get("network", 0), 0, output[-3000:])


def _run_harness(source, timeout=60):
    with tempfile.TemporaryDirectory(prefix="cc-live-") as tmp:
        shell = Path(tmp) / "shell.qml"
        shell.write_text(source, encoding="utf-8")
        try:
            completed = subprocess.run(
                ["quickshell", "-p", str(shell)],
                text=True, capture_output=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            return str(exc.stdout or "") + str(exc.stderr or "") + "\n[TIMEOUT]"
        return str(completed.stdout or "") + str(completed.stderr or "")


BAR_GEOMETRY_HARNESS = """//@ pragma UseQApplication
import QtQuick
import Quickshell
import Quickshell.Wayland

// Real-Bar geometry proof: instantiates the production Bar at supported
// widths and checks actual child bounds (via objectName traversal +
// item-relative mapToItem), not just reserve arithmetic. Caps must hold for
// arbitrary SSID/tray content; center overflow must be scroll-reachable.
ShellRoot {
    PanelWindow {
        id: win
        anchors { top: true; left: true; right: true }
        implicitHeight: 40
        color: "transparent"
        exclusionMode: ExclusionMode.Ignore
        WlrLayershell.layer: WlrLayer.Overlay
        property var testWidths: [800, 1024, 1440, 1920]
        property int step: 0
        property bool scrollProbed: false
        property bool failed: false
        Component.onCompleted: { barHolder.width = testWidths[0]; geoTimer.restart(); }
        Item {
            id: barHolder
            width: 800
            height: 40
            Loader {
                id: barLoader
                anchors.fill: parent
                source: "WIDGETS_URL/Bar.qml"
            }
        }
        function findByName(root, name) {
            if (!root) return null;
            if (root.objectName === name) return root;
            var kids = root.children;
            if (!kids) return null;
            for (var i = 0; i < kids.length; i++) {
                var f = win.findByName(kids[i], name);
                if (f) return f;
            }
            return null;
        }
        function insideBar(item, bar, label) {
            if (!item || !item.visible) return;
            var p = item.mapToItem(bar, 0, 0);
            if (!(p.x >= -1 && (p.x + item.width) <= bar.width + 1)) {
                win.failed = true;
                console.log("GEO-FAIL " + label + " x=" + Math.round(p.x) + " w=" + Math.round(item.width) + " barW=" + Math.round(bar.width));
            }
        }
        Timer {
            id: geoTimer
            interval: 900
            running: false
            repeat: true
            onTriggered: {
                var bar = barLoader.item;
                if (!bar) { console.log("GEO-FAIL no-bar"); win.failed = true; win.finish(); return; }
                var w = win.testWidths[win.step];
                var names = ["leftSlot", "rightSlot", "unifiedTray", "clockContainer", "statsContainer", "networkModule", "sysTrayFlick"];
                for (var i = 0; i < names.length; i++) win.insideBar(win.findByName(bar, names[i]), bar, names[i] + "@" + w);
                var tray = win.findByName(bar, "sysTrayFlick");
                if (tray && tray.visible && tray.width > 149) { win.failed = true; console.log("GEO-FAIL trayCap w=" + tray.width); }
                var net = win.findByName(bar, "networkModule");
                if (net && net.width > 280) { win.failed = true; console.log("GEO-FAIL netCap w=" + net.width); }
                var mc = win.findByName(bar, "mediaContainer");
                if (mc && mc.visible && mc.width > 349) { win.failed = true; console.log("GEO-FAIL mediaCap w=" + mc.width); }
                console.log("GEO-STATUS w=" + w + " reserve=" + Math.round(bar.sideReserve) + " avail=" + Math.round(bar.availableCenterSpace) + " compactMedia=" + bar.compactMedia + " capsule=" + bar.showMediaCapsule + " mini=" + bar.showMediaMini + " tight=" + bar.tightSides);
                var flick = win.findByName(bar, "centerFlick");
                var row = win.findByName(bar, "centerRow");
                var slot = win.findByName(bar, "centerSlot");
                if (flick && row && slot) {
                    if (row.implicitWidth <= flick.width + 1) {
                        var rp = row.mapToItem(bar, 0, 0);
                        var sp = slot.mapToItem(bar, 0, 0);
                        if (rp.x < sp.x - 1 || (rp.x + row.implicitWidth) > sp.x + slot.width + 1) {
                            win.failed = true;
                            console.log("GEO-FAIL centerFit x=" + Math.round(rp.x) + " w=" + Math.round(row.implicitWidth));
                        }
                    } else if (!win.scrollProbed) {
                        flick.contentX = flick.contentWidth - flick.width;
                        win.scrollProbed = true;
                        return;
                    } else {
                        var kids = row.children, first = null, last = null, k = 0;
                        for (k = 0; k < kids.length; k++) if (kids[k].visible) { if (!first) first = kids[k]; last = kids[k]; }
                        if (last) {
                            var lp = last.mapToItem(flick, last.width, last.height);
                            if (lp.x > flick.width + 1) { win.failed = true; console.log("GEO-FAIL centerTail x=" + Math.round(lp.x)); }
                        }
                        flick.contentX = 0;
                        if (first) {
                            var fp = first.mapToItem(flick, 0, 0);
                            if (fp.x < -1) { win.failed = true; console.log("GEO-FAIL centerHead x=" + Math.round(fp.x)); }
                        }
                    }
                }
                win.scrollProbed = false;
                win.step++;
                if (win.step >= win.testWidths.length) { win.finish(); return; }
                barHolder.width = win.testWidths[win.step];
            }
        }
        function finish() {
            console.log("GEO-RESULT " + (win.failed ? "FAIL" : "PASS"));
            Qt.quit();
        }
    }
}
"""


CC_MOVE_HARNESS = """//@ pragma UseQApplication
import QtQuick
import Quickshell
import Quickshell.Wayland

// Open-move proof: with the popup open, move ONLY an ancestor of the tray
// (the tray item's own x/y/width/height stay identical, so direct-geometry
// Connections cannot fire). The anchorUpdates counter must still advance,
// which proves the TransformWatcher path recalculates the real anchor rect.
ShellRoot {
    PanelWindow {
        id: win
        anchors { top: true; left: true; right: true }
        implicitHeight: 40
        color: "transparent"
        exclusionMode: ExclusionMode.Ignore
        WlrLayershell.layer: WlrLayer.Overlay
        property int step: 0
        property int baseUpdates: -1
        property real baseTrayX: 0
        property bool failed: false
        Item {
            id: trayParent
            x: 0
            width: 420
            height: 32
            Rectangle {
                id: fakeTray
                x: 10
                width: 300
                height: 32
                color: "transparent"
            }
        }
        Loader {
            id: ccLoader
            source: "WIDGETS_URL/ControlCenter.qml"
            onLoaded: {
                if (item) {
                    item.anchorItem = fakeTray;
                    item.trayScope = trayParent;
                    item.barWindow = win;
                    moveTimer.restart();
                }
            }
        }
        Timer {
            id: moveTimer
            interval: 800
            running: false
            repeat: true
            onTriggered: {
                var cc = ccLoader.item;
                if (!cc) { console.log("MOVE-FAIL no-cc"); win.failed = true; win.done(); return; }
                if (win.step === 0) {
                    console.log("MOVE-INFO edges=" + cc.anchor.edges + " gravity=" + cc.anchor.gravity + " top=" + cc.anchor.margins.top);
                    cc.setOpen(true);
                } else if (win.step === 1) {
                    win.baseUpdates = cc.anchorUpdates;
                    win.baseTrayX = fakeTray.mapToItem(win.contentItem, 0, 0).x;
                    console.log("MOVE-INFO opened reveal=" + cc.reveal.toFixed(2) + " updates=" + win.baseUpdates + " trayX=" + win.baseTrayX);
                    trayParent.x = 120;
                } else if (win.step === 2) {
                    var nowX = fakeTray.mapToItem(win.contentItem, 0, 0).x;
                    var moved = nowX - win.baseTrayX;
                    var tracked = cc.anchorUpdates - win.baseUpdates;
                    console.log("MOVE-INFO moved=" + moved + " tracked=" + tracked + " reveal=" + cc.reveal.toFixed(2) + " visible=" + cc.visible + " sameItem=" + (cc.anchor.item === fakeTray));
                    if (!(moved > 119 && moved < 121)) { win.failed = true; console.log("MOVE-FAIL tray did not move"); }
                    if (!(tracked >= 1)) { win.failed = true; console.log("MOVE-FAIL watcher did not recalc"); }
                    if (!(cc.visible && cc.reveal > 0.99)) { win.failed = true; console.log("MOVE-FAIL popup disturbed"); }
                    win.done();
                    return;
                }
                win.step++;
            }
        }
        function done() {
            console.log("MOVE-RESULT " + (win.failed ? "FAIL" : "PASS"));
            Qt.quit();
        }
    }
}
"""


OVERFLOW_MECHANISM_HARNESS = """//@ pragma UseQApplication
import QtQuick
import Quickshell
import Quickshell.Wayland

// Adversarial mechanism proof with injected mock content (services cannot
// produce a 150-char SSID, 12 tray icons, or 800px of center content on
// demand): the same idioms production uses (elide cap, capped Flickable
// viewport, center-x scroller) bound layout and keep every tail reachable.
ShellRoot {
    PanelWindow {
        id: win
        anchors { top: true; left: true; right: true }
        implicitHeight: 40
        color: "transparent"
        exclusionMode: ExclusionMode.Ignore
        WlrLayershell.layer: WlrLayer.Overlay
        property int step: 0
        property bool failed: false
        Text {
            id: longText
            visible: false
            text: "AVeryLongNetworkNameThatGoesOnAndOnAndOnAndOnAndOnAndOnAndOnAndOnAndOnAndOnAndOnAndOnAndOnAndOnAndOnAndOnAndOnAndOn"
            font.pixelSize: 12
            elide: Text.ElideRight
            width: Math.min(implicitWidth, 160)
        }
        Flickable {
            id: trayRep
            visible: false
            height: 32
            width: Math.min(trayRowRep.implicitWidth + 4, 148)
            contentWidth: trayRowRep.implicitWidth
            contentHeight: 32
            flickableDirection: Flickable.HorizontalFlick
            boundsBehavior: Flickable.StopAtBounds
            clip: true
            Row {
                id: trayRowRep
                height: 32
                spacing: 8
                Repeater {
                    model: 12
                    Rectangle { width: 20; height: 20; color: "transparent"; anchors.verticalCenter: parent.verticalCenter }
                }
            }
        }
        Flickable {
            id: centerRep
            visible: false
            width: 300
            height: 32
            contentWidth: centerRowRep.implicitWidth
            contentHeight: 32
            flickableDirection: Flickable.HorizontalFlick
            boundsBehavior: Flickable.StopAtBounds
            clip: true
            Row {
                id: centerRowRep
                spacing: 8
                x: Math.max(0, (centerRep.width - implicitWidth) / 2)
                Rectangle { id: mockWs; width: 500; height: 32; color: "transparent" }
                Rectangle { id: mockMedia; width: 300; height: 32; color: "transparent" }
            }
        }
        Timer {
            id: mechTimer
            interval: 700
            running: true
            repeat: true
            onTriggered: {
                if (win.step === 0) {
                    var textOk = (longText.width === 160) && (longText.implicitWidth > 300);
                    console.log("MECH-STATUS textCap w=" + longText.width + " natural=" + Math.round(longText.implicitWidth) + " " + (textOk ? "TEXT-CAP PASS" : "TEXT-CAP FAIL"));
                    if (!textOk) win.failed = true;
                    var trayOk = (trayRep.width === 148) && (trayRep.contentWidth === 328);
                    console.log("MECH-STATUS trayViewport w=" + trayRep.width + " content=" + trayRep.contentWidth + " " + (trayOk ? "TRAY-CAP PASS" : "TRAY-CAP FAIL"));
                    if (!trayOk) win.failed = true;
                    trayRep.contentX = trayRep.contentWidth - trayRep.width;
                } else if (win.step === 1) {
                    var lastBox = trayRowRep.children[11].mapToItem(trayRep, 20, 20);
                    var scrollOk = lastBox.x <= trayRep.width + 1;
                    console.log("MECH-STATUS trayTail x=" + Math.round(lastBox.x) + " " + (scrollOk ? "TRAY-SCROLL PASS" : "TRAY-SCROLL FAIL"));
                    if (!scrollOk) win.failed = true;
                    var leftOk = (centerRowRep.x === 0);
                    console.log("MECH-STATUS centerX=" + centerRowRep.x + " rowW=" + centerRowRep.implicitWidth + " " + (leftOk ? "CENTER-LEFT PASS" : "CENTER-LEFT FAIL"));
                    if (!leftOk) win.failed = true;
                    centerRep.contentX = centerRep.contentWidth - centerRep.width;
                } else if (win.step === 2) {
                    var tailC = mockMedia.mapToItem(centerRep, mockMedia.width, mockMedia.height);
                    var cOk = tailC.x <= centerRep.width + 1;
                    console.log("MECH-STATUS centerTail x=" + Math.round(tailC.x) + " " + (cOk ? "CENTER-SCROLL PASS" : "CENTER-SCROLL FAIL"));
                    if (!cOk) win.failed = true;
                    centerRep.width = 900;
                } else if (win.step === 3) {
                    var centered = (centerRowRep.x === 46);
                    console.log("MECH-STATUS centerX=" + centerRowRep.x + " " + (centered ? "CENTER-CENTER PASS" : "CENTER-CENTER FAIL"));
                    if (!centered) win.failed = true;
                    console.log("MECH-RESULT " + (win.failed ? "FAIL" : "PASS"));
                    Qt.quit();
                    return;
                }
                win.step++;
            }
        }
    }
}
"""


ROW_SEMANTICS_HARNESS = """//@ pragma UseQApplication
import QtQuick
import Quickshell
import Quickshell.Wayland

// Ground truth for bar budget math, using real Qt types (no production
// formulas involved): a Row of a width-capped elided Text plus a fixed box,
// and a Row with an invisible child.
ShellRoot {
    PanelWindow {
        id: win
        anchors { top: true; left: true; right: true }
        implicitHeight: 40
        color: "transparent"
        exclusionMode: ExclusionMode.Ignore
        WlrLayershell.layer: WlrLayer.Overlay
        property bool failed: false
        Row {
            id: cappedRow
            spacing: 8
            Text {
                id: cappedText
                text: "AVeryLongNetworkNameThatGoesOnAndOnAndOnAndOnAndOnAndOnAndOnAndOnAndOnAndOnAndOnAndOnAndOnAndOnAndOnAndOnAndOnAndOn"
                font.pixelSize: 12
                elide: Text.ElideRight
                width: Math.min(implicitWidth, 160)
            }
            Rectangle { width: 20; height: 10 }
        }
        Row {
            id: hiddenRow
            spacing: 8
            Text { visible: false; text: "hello-hidden-label"; font.pixelSize: 12 }
            Rectangle { width: 20; height: 10 }
        }
        Timer {
            interval: 800
            running: true
            repeat: false
            onTriggered: {
                var cappedOk = (cappedRow.implicitWidth === 188) && (cappedText.width === 160) && (cappedText.implicitWidth > 300);
                console.log("ROW-SEM cappedRow=" + cappedRow.implicitWidth + " textW=" + cappedText.width + " natural=" + Math.round(cappedText.implicitWidth) + " " + (cappedOk ? "ROW-CAPPED PASS" : "ROW-CAPPED FAIL"));
                if (!cappedOk) win.failed = true;
                var hiddenOk = (hiddenRow.implicitWidth === 20);
                console.log("ROW-SEM hiddenRow=" + hiddenRow.implicitWidth + " " + (hiddenOk ? "ROW-HIDDEN PASS" : "ROW-HIDDEN FAIL"));
                if (!hiddenOk) win.failed = true;
                console.log("ROW-SEM-RESULT " + (win.failed ? "FAIL" : "PASS"));
                Qt.quit();
            }
        }
    }
}
"""


NET_INVARIANCE_HARNESS = """//@ pragma UseQApplication
import QtQuick
import Quickshell
import Quickshell.Wayland

// Real NetworkModule instance (live services, whatever SSID/BT state the
// machine has): toggling compact must leave fullWidth bitwise identical
// (it is built only from compact-independent terms, including the BT
// count that compact also hides), and reported sizes must stay sane.
ShellRoot {
    PanelWindow {
        id: win
        anchors { top: true; left: true; right: true }
        implicitHeight: 40
        color: "transparent"
        exclusionMode: ExclusionMode.Ignore
        WlrLayershell.layer: WlrLayer.Overlay
        property int step: 0
        property bool failed: false
        property real fullShown: -1
        Loader {
            id: netLoader
            source: "WIDGETS_URL/NetworkModule.qml"
            onLoaded: { if (item) invTimer.restart(); }
        }
        Timer {
            id: invTimer
            interval: 800
            running: false
            repeat: true
            onTriggered: {
                var m = netLoader.item;
                if (!m) { console.log("NET-INV-FAIL no-module"); win.failed = true; win.done(); return; }
                if (win.step === 0) {
                    m.compact = false;
                } else if (win.step === 1) {
                    win.fullShown = m.fullWidth;
                    console.log("NET-INV shown full=" + m.fullWidth + " implicit=" + m.implicitWidth + " w=" + m.width + " bt=" + m.connectedBluetoothDevices);
                    m.compact = true;
                } else if (win.step === 2) {
                    var same = (m.fullWidth === win.fullShown);
                    var sane = (m.implicitWidth >= 0) && (m.width >= 0) && (m.width <= m.fullWidth + 1);
                    console.log("NET-INV compact full=" + m.fullWidth + " implicit=" + m.implicitWidth + " w=" + m.width + " " + ((same && sane) ? "NET-INV PASS" : "NET-INV FAIL"));
                    if (!(same && sane)) win.failed = true;
                    win.done();
                    return;
                }
                win.step++;
            }
        }
        function done() {
            console.log("NET-INV-RESULT " + (win.failed ? "FAIL" : "PASS"));
            Qt.quit();
        }
    }
}
"""


CHEVRON_BOUNDS_HARNESS = """//@ pragma UseQApplication
import QtQuick
import Quickshell
import Quickshell.Wayland

// Glyph-only rotation proof: a real 32px WidgetIconButton keeps root
// rotation 0 and its actual Button background rect contained while the inner
// glyph spins through 45/90/135deg (mid-rotation corners would protrude if
// the whole Button rotated).
ShellRoot {
    PanelWindow {
        id: win
        anchors { top: true; left: true; right: true }
        implicitHeight: 40
        color: "transparent"
        exclusionMode: ExclusionMode.Ignore
        WlrLayershell.layer: WlrLayer.Overlay
        property int step: 0
        property bool failed: false
        property var angles: [45, 90, 135]
        property real targetAngle: 0
        Loader {
            id: btnLoader
            source: "WIDGETS_URL/WidgetIconButton.qml"
            width: 32
            height: 32
            onLoaded: {
                if (item) {
                    item.width = 32;
                    item.height = 32;
                    item.topInset = 0; item.bottomInset = 0; item.leftInset = 0; item.rightInset = 0;
                    checkTimer.restart();
                }
            }
        }
        function findByName(root, name) {
            if (!root) return null;
            if (root.objectName === name) return root;
            var kids = root.children;
            if (!kids) return null;
            for (var i = 0; i < kids.length; i++) {
                var f = win.findByName(kids[i], name);
                if (f) return f;
            }
            if (root.contentItem) {
                var c = win.findByName(root.contentItem, name);
                if (c) return c;
            }
            if (root.background) {
                var b = (root.background.objectName === name) ? root.background : null;
                if (b) return b;
            }
            return null;
        }
        Timer {
            id: checkTimer
            interval: 700
            running: false
            repeat: true
            onTriggered: {
                var btn = btnLoader.item;
                if (!btn) { console.log("CHEVRON-FAIL no-button"); win.failed = true; win.finish(); return; }
                var phase = win.step % 2;
                var idx = Math.floor(win.step / 2);
                if (idx >= win.angles.length) { win.finish(); return; }
                if (phase === 0) {
                    win.targetAngle = win.angles[idx];
                    btn.iconRotation = win.targetAngle;
                } else {
                    var glyph = win.findByName(btn, "iconImage");
                    var rotOk = (btn.rotation === 0);
                    var targetOk = Math.abs(btn.iconRotation - win.targetAngle) < 2;
                    var glyphOk = glyph && (Math.abs(glyph.rotation - win.targetAngle) < 2);
                    var bg = btn.background;
                    var bgOk = false;
                    var info = "no-bg";
                    if (bg) {
                        var p = bg.mapToItem(btn, 0, 0);
                        bgOk = (p.x >= -1) && (p.y >= -1)
                            && ((p.x + bg.width) <= btn.width + 1)
                            && ((p.y + bg.height) <= btn.height + 1)
                            && (bg.width <= btn.width + 1) && (bg.height <= btn.height + 1);
                        info = "bg x=" + Math.round(p.x) + " y=" + Math.round(p.y) + " w=" + Math.round(bg.width) + " h=" + Math.round(bg.height) + " btn=" + Math.round(btn.width) + "x" + Math.round(btn.height);
                    }
                    var ok = rotOk && targetOk && glyphOk && bgOk;
                    console.log("CHEVRON-STATUS angle=" + win.targetAngle + " rootRot=" + btn.rotation + " glyph=" + (glyph ? Math.round(glyph.rotation) : -1) + " " + info + " " + (ok ? "CHEVRON-PASS" : "CHEVRON-FAIL"));
                    if (!ok) win.failed = true;
                }
                win.step++;
                if (win.step >= win.angles.length * 2) { win.finish(); return; }
            }
        }
        function finish() {
            console.log("CHEVRON-RESULT " + (win.failed ? "FAIL" : "PASS"));
            Qt.quit();
        }
    }
}
"""


CC_ORDER_HARNESS = """//@ pragma UseQApplication
import QtQuick
import Quickshell
import Quickshell.Wayland

// CC order proof: header, quick controls, divider, tabs directly above the
// body (no extra spacer). Selected tab stays on Audio while closed so the
// network panel never scans; geometry is checked after opening.
ShellRoot {
    PanelWindow {
        id: win
        anchors { top: true; left: true; right: true }
        implicitHeight: 40
        color: "transparent"
        exclusionMode: ExclusionMode.Ignore
        WlrLayershell.layer: WlrLayer.Overlay
        property int step: 0
        property bool failed: false
        Item {
            id: trayParent
            x: 0
            width: 420
            height: 32
            Rectangle {
                id: fakeTray
                x: 10
                width: 300
                height: 32
                color: "transparent"
            }
        }
        Loader {
            id: ccLoader
            source: "WIDGETS_URL/ControlCenter.qml"
            onLoaded: {
                if (item) {
                    item.anchorItem = fakeTray;
                    item.trayScope = trayParent;
                    item.barWindow = win;
                    item.selectedTab = 0;
                    orderTimer.restart();
                }
            }
        }
        function findByName(root, name) {
            if (!root) return null;
            try { if (root.objectName === name) return root; } catch (e) {}
            var lists = [];
            try { if (root.children) lists.push(root.children); } catch (e) {}
            try { if (root.data) lists.push(root.data); } catch (e2) {}
            try { if (root.contentData) lists.push(root.contentData); } catch (e3) {}
            for (var li = 0; li < lists.length; li++) {
                var kids = lists[li];
                for (var i = 0; i < kids.length; i++) {
                    var f = null;
                    try { f = win.findByName(kids[i], name); } catch (e4) { continue; }
                    if (f) return f;
                }
            }
            try {
                if (root.contentItem && root.contentItem !== root) {
                    var c = win.findByName(root.contentItem, name);
                    if (c) return c;
                }
            } catch (e5) {}
            return null;
        }
        Timer {
            id: orderTimer
            interval: 800
            running: false
            repeat: true
            onTriggered: {
                var cc = ccLoader.item;
                if (!cc) { console.log("CC-ORDER-FAIL no-cc"); win.failed = true; win.done(); return; }
                if (win.step === 0) {
                    cc.selectedTab = 0;
                    cc.setOpen(true);
                } else if (win.step === 1) {
                    var header = win.findByName(cc, "headerRow");
                    var quick = win.findByName(cc, "quickCard");
                    var divider = win.findByName(cc, "dividerRect");
                    var tabs = win.findByName(cc, "tabsRow");
                    var body = win.findByName(cc, "bodyFlick");
                    if (!header || !quick || !divider || !tabs || !body) {
                        win.failed = true;
                        console.log("CC-ORDER-FAIL missing " + (!header ? "header " : "") + (!quick ? "quick " : "") + (!divider ? "divider " : "") + (!tabs ? "tabs " : "") + (!body ? "body" : ""));
                        win.done();
                        return;
                    }
                    var orderOk = (header.y < quick.y) && (quick.y < divider.y) && (divider.y < tabs.y) && (tabs.y < body.y);
                    var sameParent = (tabs.parent === body.parent);
                    var kids = sameParent ? tabs.parent.children : [];
                    var tabsIdx = -1, bodyIdx = -1, k = 0;
                    for (k = 0; k < kids.length; k++) {
                        if (kids[k] === tabs) tabsIdx = k;
                        if (kids[k] === body) bodyIdx = k;
                    }
                    var adjacentOk = sameParent && (tabsIdx !== -1) && (bodyIdx === tabsIdx + 1);
                    var gap = body.y - (tabs.y + tabs.height);
                    var gapOk = (gap >= 8) && (gap <= 12);
                    console.log("CC-ORDER-STATUS y header=" + Math.round(header.y) + " quick=" + Math.round(quick.y) + " divider=" + Math.round(divider.y) + " tabs=" + Math.round(tabs.y) + "h" + Math.round(tabs.height) + " body=" + Math.round(body.y) + " gap=" + Math.round(gap) + " idx " + tabsIdx + "->" + bodyIdx);
                    var ok = orderOk && adjacentOk && gapOk;
                    console.log("CC-ORDER " + (ok ? "PASS" : "FAIL"));
                    if (!ok) win.failed = true;
                    win.done();
                    return;
                }
                win.step++;
            }
        }
        function done() {
            console.log("CC-ORDER-RESULT " + (win.failed ? "FAIL" : "PASS"));
            Qt.quit();
        }
    }
}
"""


NET_SELECTOR_HARNESS = """//@ pragma UseQApplication
import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Wayland

// Embedded vs standalone selector proof: standalone NetworkPanel keeps its
// internal Network/Bluetooth tabs with natural height; the ControlCenter
// embedded instance hides them with no reserved height. CC stays closed
// (selectedTab set while closed) so no hardware scan is triggered.
ShellRoot {
    PanelWindow {
        id: win
        anchors { top: true; left: true; right: true }
        implicitHeight: 40
        color: "transparent"
        exclusionMode: ExclusionMode.Ignore
        WlrLayershell.layer: WlrLayer.Overlay
        property int step: 0
        property bool failed: false
        Loader {
            id: standaloneLoader
            source: "WIDGETS_URL/NetworkPanel.qml"
            width: 380
            onLoaded: { if (item) { item.width = 380; selTimer.restart(); } }
        }
        Item {
            id: trayParent
            x: 0
            width: 420
            height: 32
            Rectangle {
                id: fakeTray
                x: 10
                width: 300
                height: 32
                color: "transparent"
            }
        }
        Loader {
            id: ccLoader
            source: "WIDGETS_URL/ControlCenter.qml"
            onLoaded: {
                if (item) {
                    item.anchorItem = fakeTray;
                    item.trayScope = trayParent;
                    item.barWindow = win;
                    item.selectedTab = 0;
                }
            }
        }
        function findByName(root, name) {
            if (!root) return null;
            try { if (root.objectName === name) return root; } catch (e) {}
            var lists = [];
            try { if (root.children) lists.push(root.children); } catch (e) {}
            try { if (root.data) lists.push(root.data); } catch (e2) {}
            try { if (root.contentData) lists.push(root.contentData); } catch (e3) {}
            for (var li = 0; li < lists.length; li++) {
                var kids = lists[li];
                for (var i = 0; i < kids.length; i++) {
                    var f = null;
                    try { f = win.findByName(kids[i], name); } catch (e4) { continue; }
                    if (f) return f;
                }
            }
            try {
                if (root.contentItem && root.contentItem !== root) {
                    var c = win.findByName(root.contentItem, name);
                    if (c) return c;
                }
            } catch (e5) {}
            return null;
        }
        Timer {
            id: selTimer
            interval: 800
            running: false
            repeat: true
            onTriggered: {
                var solo = standaloneLoader.item;
                var cc = ccLoader.item;
                if (!solo || !cc) { console.log("NET-SEL-FAIL not-loaded"); win.failed = true; win.done(); return; }
                if (win.step === 0) {
                    var row = win.findByName(solo, "tabSelectorRow");
                    var div = win.findByName(solo, "tabSelectorDivider");
                    var soloOk = solo.showTabSelector === true && row && row.visible && (row.height >= 29)
                        && div && div.visible && (div.height === 1) && (solo.implicitHeight > 0);
                    console.log("NET-SEL-STATUS standalone show=" + solo.showTabSelector + " rowVis=" + (row ? row.visible : "?") + " rowH=" + (row ? Math.round(row.height) : -1) + " divH=" + (div ? div.height : -1) + " implicit=" + Math.round(solo.implicitHeight) + " " + (soloOk ? "STANDALONE-PASS" : "STANDALONE-FAIL"));
                    if (!soloOk) win.failed = true;
                    cc.selectedTab = 1;
                } else if (win.step === 1) {
                    var net = win.findByName(cc, "netPanel");
                    if (!net) { console.log("NET-SEL-FAIL no-netPanel"); win.failed = true; win.done(); return; }
                    var erow = win.findByName(net, "tabSelectorRow");
                    var ediv = win.findByName(net, "tabSelectorDivider");
                    var embOk = (net.showTabSelector === false) && erow && (!erow.visible) && (erow.Layout.preferredHeight === 0)
                        && ediv && (!ediv.visible) && (ediv.height === 0) && (ediv.Layout.preferredHeight === 0);
                    var routeOk = (net.activeTab === 0);
                    console.log("NET-SEL-STATUS embedded show=" + net.showTabSelector + " rowVis=" + (erow ? erow.visible : "?") + " rowH=" + (erow ? erow.height : -1) + " activeTab=" + net.activeTab + " " + ((embOk && routeOk) ? "EMBEDDED-PASS" : "EMBEDDED-FAIL"));
                    if (!(embOk && routeOk)) win.failed = true;
                    cc.selectedTab = 2;
                } else if (win.step === 2) {
                    var net2 = win.findByName(cc, "netPanel");
                    var btOk = net2 && (net2.activeTab === 1) && (!win.findByName(net2, "tabSelectorRow").visible);
                    console.log("NET-SEL-STATUS btRoute activeTab=" + (net2 ? net2.activeTab : -1) + " " + (btOk ? "BT-ROUTE-PASS" : "BT-ROUTE-FAIL"));
                    if (!btOk) win.failed = true;
                    cc.selectedTab = 0;
                    win.done();
                    return;
                }
                win.step++;
            }
        }
        function done() {
            console.log("NET-SEL-RESULT " + (win.failed ? "FAIL" : "PASS"));
            Qt.quit();
        }
    }
}
"""


@unittest.skipUnless(shutil.which("quickshell"), "quickshell is required for live geometry harnesses")
class BarGeometryLiveTests(unittest.TestCase):
    def test_real_bar_child_bounds_at_supported_widths(self):
        widgets_url = "file://" + str(ROOT / "widgets")
        source = BAR_GEOMETRY_HARNESS.replace("WIDGETS_URL", widgets_url)
        output = _run_harness(source, timeout=60)
        self.assertIn("GEO-RESULT PASS", output, output[-4000:])
        self.assertNotIn("GEO-FAIL", output, output[-4000:])
        # The tightSides <-> compact budget must settle: any feedback would
        # print a binding-loop warning while widths re-evaluate.
        self.assertNotIn("binding loop", output, output[-4000:])

    def test_open_popup_tracks_ancestor_motion(self):
        widgets_url = "file://" + str(ROOT / "widgets")
        source = CC_MOVE_HARNESS.replace("WIDGETS_URL", widgets_url)
        output = _run_harness(source, timeout=45)
        self.assertIn("MOVE-RESULT PASS", output, output[-4000:])
        self.assertNotIn("MOVE-FAIL", output, output[-4000:])

    def test_adversarial_overflow_mechanisms(self):
        output = _run_harness(OVERFLOW_MECHANISM_HARNESS, timeout=45)
        for tag in ("TEXT-CAP PASS", "TRAY-CAP PASS", "TRAY-SCROLL PASS",
                    "CENTER-LEFT PASS", "CENTER-SCROLL PASS", "CENTER-CENTER PASS"):
            self.assertIn(tag, output, output[-4000:])
        self.assertIn("MECH-RESULT PASS", output, output[-4000:])

    def test_row_reports_laid_out_widths(self):
        output = _run_harness(ROW_SEMANTICS_HARNESS, timeout=45)
        self.assertIn("ROW-CAPPED PASS", output, output[-4000:])
        self.assertIn("ROW-HIDDEN PASS", output, output[-4000:])
        self.assertIn("ROW-SEM-RESULT PASS", output, output[-4000:])

    def test_real_network_module_compact_toggle_is_budget_stable(self):
        widgets_url = "file://" + str(ROOT / "widgets")
        source = NET_INVARIANCE_HARNESS.replace("WIDGETS_URL", widgets_url)
        output = _run_harness(source, timeout=45)
        self.assertIn("NET-INV PASS", output, output[-4000:])
        self.assertIn("NET-INV-RESULT PASS", output, output[-4000:])
        self.assertNotIn("binding loop", output, output[-4000:])

    def test_chevron_background_stays_contained_while_glyph_rotates(self):
        widgets_url = "file://" + str(ROOT / "widgets")
        source = CHEVRON_BOUNDS_HARNESS.replace("WIDGETS_URL", widgets_url)
        output = _run_harness(source, timeout=45)
        for angle in ("angle=45", "angle=90", "angle=135"):
            self.assertIn(angle, output, output[-4000:])
        self.assertIn("CHEVRON-PASS", output, output[-4000:])
        self.assertNotIn("CHEVRON-FAIL", output, output[-4000:])
        self.assertIn("CHEVRON-RESULT PASS", output, output[-4000:])

    def test_cc_tabs_follow_quick_controls_with_no_extra_spacer(self):
        widgets_url = "file://" + str(ROOT / "widgets")
        source = CC_ORDER_HARNESS.replace("WIDGETS_URL", widgets_url)
        output = _run_harness(source, timeout=45)
        self.assertIn("CC-ORDER PASS", output, output[-4000:])
        self.assertIn("CC-ORDER-RESULT PASS", output, output[-4000:])
        self.assertNotIn("CC-ORDER-FAIL", output, output[-4000:])

    def test_embedded_selector_hidden_standalone_visible(self):
        widgets_url = "file://" + str(ROOT / "widgets")
        source = NET_SELECTOR_HARNESS.replace("WIDGETS_URL", widgets_url)
        output = _run_harness(source, timeout=45)
        for tag in ("STANDALONE-PASS", "EMBEDDED-PASS", "BT-ROUTE-PASS"):
            self.assertIn(tag, output, output[-4000:])
        self.assertIn("NET-SEL-RESULT PASS", output, output[-4000:])
        self.assertNotIn("NET-SEL-FAIL", output, output[-4000:])

    def test_timed_shell_load_without_killing_existing(self):
        # Timed load of the production shell (no kill of the existing
        # session): staying up for the window proves imports/bindings hold;
        # any QML Type/Reference error fails fast.
        shell = str(ROOT / "shell.qml")
        completed = None
        try:
            completed = subprocess.run(
                ["quickshell", "-p", shell],
                text=True, capture_output=True, timeout=12,
            )
            output = str(completed.stdout or "") + str(completed.stderr or "")
        except subprocess.TimeoutExpired as exc:
            output = str(exc.stdout or "") + str(exc.stderr or "")
            output += "\n[TIMED-STILL-RUNNING]"
        tail = output[-4000:]
        for bad in ("TypeError", "ReferenceError", "Cannot assign to non-existent",
                    "Failed to load", "Module not found"):
            self.assertNotIn(bad, tail, tail)
        self.assertTrue(
            "[TIMED-STILL-RUNNING]" in output or (completed is not None and completed.returncode == 0),
            tail,
        )


class NotificationHistoryOwnershipTests(unittest.TestCase):
    def test_eviction_captures_object_before_removal(self):
        history = read("NotificationHistory.qml")
        self.assertIn("var evictObj = historyModel.get(10).notifObj;", history)
        self.assertIn("historyModel.remove(10);", history)
        self.assertIn("if (evictObj) evictObj.dismiss();", history)
        self.assertNotIn("removed.notifObj", history)

    def test_activation_separated_from_dismissal_with_resident_preserved(self):
        history = read("NotificationHistory.qml")
        self.assertIn("var defaultAction = null;", history)
        # Resident preservation is structural: activation only invokes, and
        # removal happens exclusively through the notification's closed
        # handler (resident notifications never close).
        self.assertIn("n.closed.connect", history)
        self.assertIn("defaultAction.invoke();", history)
        # Action branch only invokes; removal happens via closed for
        # nonresident, resident rows persist.
        action_branch = history[history.index("if (defaultAction) {"):history.index('return "invoked";')]
        self.assertNotIn("historyModel.remove(", action_branch)
        self.assertNotIn(".dismiss()", action_branch)
        # No-action branch is the safe explicit dismiss (remove then dismiss).
        no_action = history[history.index('return "invoked";'):history.index('return "dismissed";') + 30]
        self.assertIn("historyModel.remove(index);", no_action)
        self.assertIn("activateTarget.dismiss();", no_action)
        # Clear All collects first, clears, then dismisses (no double remove).
        clear = history[history.index("function clearAll()"):history.index("function dismissAt(")]
        self.assertIn("historyModel.clear();", clear)
        self.assertIn(".dismiss();", clear)

    def test_single_shell_owner_fans_out_to_every_bar(self):
        shell = (ROOT / "shell.qml").read_text(encoding="utf-8")
        bar = read("Bar.qml")
        # One store outside the per-screen delegate, fanned out through a
        # qualified delegate-root ref (unqualified same-name bindings would
        # resolve to the child's own null property).
        self.assertIn("NotificationHistory {", shell)
        self.assertIn("id: notifHistory", shell)
        self.assertIn("readonly property var notifHistoryRef: notifHistory", shell)
        self.assertIn("notifHistory: barWindow.notifHistoryRef", shell)
        self.assertIn("property var notifHistory", bar)
        self.assertIn("history: bar.notifHistory", bar)


class MediaClickOwnershipTests(unittest.TestCase):
    def test_capsule_toggle_sits_below_module_controls(self):
        bar = read("Bar.qml")
        media = read("MediaModule.qml")
        capsule = bar[bar.index("id: mediaContainer"):bar.index("id: mediaMini")]
        # Background toggle is declared BEFORE the module, so the module's
        # transport/title MouseAreas sit above it and win clicks.
        self.assertLess(capsule.index("id: mediaBgMouse"),
                        capsule.index("MediaModule {"))
        self.assertIn("mediaPopout.toggle(mediaContainer)", capsule)
        # Flicker-free highlight: capsule color follows both the background
        # hover and the module's control hover.
        self.assertIn("mediaBgMouse.containsMouse", capsule)
        self.assertIn("mediaModule.hovered", capsule)
        self.assertNotIn("onEntered: mediaContainer.color", capsule)
        # Module exposes one hovered bit over all four control targets, and
        # transports act (never toggle the popout).
        self.assertIn("readonly property bool hovered", media)
        for target in ("titleMouse", "prevMouse", "playMouse", "nextMouse"):
            self.assertIn("id: " + target, media)
        self.assertIn('root.control("previous")', media)
        self.assertIn('root.control("play-pause")', media)
        self.assertIn('root.control("next")', media)

    def test_stats_capsule_has_no_dead_hover_affordance(self):
        bar = read("Bar.qml")
        stats = bar[bar.index("id: statsContainer"):]
        self.assertNotIn("MouseArea", stats)
        self.assertNotIn("onEntered", stats)
        self.assertIn("color: Theme.mantle", stats)


class SharedPollingTests(unittest.TestCase):
    def test_stats_one_shell_source_fans_out_to_views(self):
        source = read("SystemStats.qml")
        module = read("StatModule.qml")
        shell = (ROOT / "shell.qml").read_text(encoding="utf-8")
        bar = read("Bar.qml")
        # S-053 native sampling: mem/cpu read /proc in-process (zero forks);
        # disk keeps exactly one bounded fork on its own slow timer.
        # Values fan out to per-bar views.
        self.assertEqual(source.count("Process {"), 1)
        self.assertEqual(source.count("FileView {"), 2)
        self.assertEqual(source.count("Timer {"), 2)
        self.assertIn("interval: 5000", source)
        self.assertIn("objectName: \"systemStatsPoll\"", source)
        for value in ("memValue", "cpuValue", "diskValue"):
            self.assertIn(value, source)
        self.assertIn("function valueFor(", source)
        # mem/cpu sample /proc via FileView, reloaded on the 5 s tick
        # (procfs watches are unreliable, so watchChanges stays off).
        self.assertIn("/proc/meminfo", source)
        self.assertIn("/proc/stat", source)
        self.assertIn("watchChanges: false", source)
        self.assertIn(".reload()", source)
        self.assertIn("parseMemUsedPercent", source)
        self.assertIn("MemAvailable", source)
        self.assertIn("parseCpuPercent", source)
        # No per-tick shell forks for mem/cpu remain.
        self.assertNotIn('"sh"', source)
        self.assertNotIn("free |", source)
        self.assertNotIn("top -bn1", source)
        self.assertEqual(source.count('"python3"'), 1)
        # The one remaining fork is the slow disk sampler: direct argv
        # (no shell wrapper) on a >= 60 s cadence.
        self.assertIn("shutil.disk_usage", source)
        m = re.search(r"diskInterval\s*:\s*(\d+)", source)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 60000)
        # Views run nothing.
        self.assertNotIn("Process {", module)
        self.assertNotIn("Timer {", module)
        self.assertNotIn("StdioCollector", module)
        self.assertIn("statsSource", module)
        self.assertIn("statKey", module)
        # Shell owns one instance; bars consume it.
        self.assertIn("SystemStats {", shell)
        self.assertIn("id: systemStats", shell)
        self.assertIn("statsSource: bar.systemStats", bar)

    def test_clock_one_shared_second_fans_out_to_views(self):
        source = read("SharedClock.qml")
        module = read("ClockModule.qml")
        shell = (ROOT / "shell.qml").read_text(encoding="utf-8")
        bar = read("Bar.qml")
        self.assertEqual(source.count("Timer {"), 1)
        self.assertIn("interval: 1000", source)
        self.assertIn("objectName: \"sharedClockTick\"", source)
        # Date segments advance on day change, not every tick.
        self.assertIn("dayDate", source)
        self.assertIn("sameDay", source)
        self.assertIn("function textFor(", source)
        self.assertNotIn("Timer {", module)
        self.assertIn("clockSource", module)
        self.assertIn("SharedClock {", shell)
        self.assertIn("clockSource: bar.clockSource", bar)
        # Three visual segments stay, fed by the shared source.
        for fmt in ('format: "ddd"', 'format: "HH:mm"', 'format: "MM-dd"'):
            self.assertIn(fmt, bar)

    def test_project_one_shell_stream_with_slow_badge(self):
        source = read("CurrentProjectSource.qml")
        module = read("CurrentProjectModule.qml")
        shell = (ROOT / "shell.qml").read_text(encoding="utf-8")
        bar = read("Bar.qml")
        # One resident watch stream + backoff restart + one 60 s badge timer.
        self.assertIn('"watch"', source)
        self.assertIn("SplitParser", source)
        self.assertIn('objectName: "watchRestartTimer"', source)
        self.assertNotIn("projectPoll", source)
        self.assertNotIn("signal(9)", source)
        self.assertIn("interval: 60000", source)
        self.assertIn("objectName: \"ledgerBadgePoll\"", source)
        self.assertIn("current-project", source)
        self.assertIn("currentGeneration", source)
        self.assertIn("watchLive", source)
        # The view runs nothing but keeps popup/badge behavior.
        self.assertNotIn("Process {", module)
        self.assertNotIn("Timer {", module)
        self.assertNotIn("launchGeneration", module)
        self.assertNotIn("currentProjectProcess", module)
        self.assertIn("property var projectSource", module)
        self.assertIn("activateCurrentProject", module)
        self.assertIn("overviewPopup.openFor", module)
        self.assertIn("currentProjectLedgerBadge", module)
        self.assertIn("refreshLedgerInboxBadge", module)
        # Shell owns one instance; bars consume it.
        self.assertIn("CurrentProjectSource {", shell)
        self.assertIn("projectSource: bar.projectSource", bar)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class SystemStatsFormulaTests(unittest.TestCase):
    """S-053: the mem/cpu formulas execute behaviorally, not just present.

    Follows the node/vm pattern of tests/test_palette_query.py: the pure
    QML functions are extracted from SystemStats.qml and run against
    /proc fixtures with exact expected percents.
    """

    SOURCE = (ROOT / "widgets" / "SystemStats.qml").read_text(encoding="utf-8")

    def extract(self, name):
        source = self.SOURCE
        start = source.index("function " + name + "(")
        opening = source.index("{", start)
        depth = 0
        for index in range(opening, len(source)):
            if source[index] == "{":
                depth += 1
            elif source[index] == "}":
                depth -= 1
                if depth == 0:
                    return source[start:index + 1]
        raise AssertionError("unterminated function: " + name)

    def run_case(self, expression):
        functions = [self.extract(name) for name in
                     ("parseMemUsedPercent", "parseCpuPercent", "noteStatsTick")]
        script = f"""
const vm = require("vm");
const warnings = [];
const context = {{
  root: {{
    _prevCpuTotal: -1, _prevCpuIdle: -1,
    memValue: "...", cpuValue: "...",
    _memStaleTicks: 0, _cpuStaleTicks: 0,
    _memStaleWarned: false, _cpuStaleWarned: false,
  }},
  console: {{ warn(m) {{ warnings.push(String(m)); }}, log() {{}} }},
}};
vm.createContext(context);
for (const value of {json.dumps(functions)}) vm.runInContext(value, context);
const out = vm.runInContext({json.dumps(expression)}, context);
console.log(JSON.stringify({{ out, warnings,
  stale: [context.root._memStaleTicks, context.root._cpuStaleTicks] }}));
"""
        completed = subprocess.run(
            ["node", "-e", script], text=True, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_mem_used_matches_free_semantics(self):
        # used = MemTotal - MemAvailable (what free(1) reports since
        # procps-ng 4.x); percent = int(used/total*100).
        # used = 16384000 - 4096000 = 12288000 -> exactly 75%.
        fixture = ("MemTotal:       16384000 kB\n"
                   "MemFree:         2000000 kB\n"
                   "MemAvailable:    4096000 kB\n"
                   "Buffers:          500000 kB\n"
                   "Cached:          1000000 kB\n"
                   "SReclaimable:     200000 kB\n")
        value = self.run_case("parseMemUsedPercent(%s)" % json.dumps(fixture))
        self.assertEqual(value["out"], "75%")

    def test_mem_falls_back_without_memavailable(self):
        # Pre-3.14 kernels lack MemAvailable: total - free - buffers -
        # cached - sreclaimable. used = 8000-1000-200-800-200 = 5800 ->
        # floor(72.5) = 72%.
        fixture = ("MemTotal:        8000 kB\n"
                   "MemFree:         1000 kB\n"
                   "Buffers:          200 kB\n"
                   "Cached:           800 kB\n"
                   "SReclaimable:     200 kB\n")
        value = self.run_case("parseMemUsedPercent(%s)" % json.dumps(fixture))
        self.assertEqual(value["out"], "72%")

    def test_mem_malformed_stays_unavailable(self):
        for bad in ("", "not procfs output\n",
                    "MemFree:         100 kB\n"):
            value = self.run_case(
                "parseMemUsedPercent(%s)" % json.dumps(bad))
            self.assertEqual(value["out"], "...", repr(bad))

    def test_cpu_delta_counts_iowait_busy(self):
        # Aggregate "cpu" line only (cpu0/cpu1 must not match); total =
        # ALL fields, idle = the idle field only (iowait counts busy,
        # like the legacy top pipeline).
        # s1 total = 950, idle = 700; s2 total = 1125, idle = 750 ->
        # busy = 175 - 50 = 125 -> floor(125/175*100) = 71%.
        first = ("cpu  100 0 50 700 100 0 0 0 0 0\n"
                 "cpu0 50 0 25 350 50 0 0 0 0 0\n"
                 "cpu1 50 0 25 350 50 0 0 0 0 0\n")
        second = ("cpu  150 0 75 750 150 0 0 0 0 0\n"
                  "cpu0 75 0 38 375 75 0 0 0 0 0\n"
                  "cpu1 75 0 37 375 75 0 0 0 0 0\n")
        expression = ("[parseCpuPercent(%s), parseCpuPercent(%s)]"
                      % (json.dumps(first), json.dumps(second)))
        value = self.run_case(expression)
        # First sample only stores state ("..." until two exist).
        self.assertEqual(value["out"], ["...", "71%"])

    def test_cpu_counter_reset_and_malformed_stay_unavailable(self):
        steady = "cpu  100 0 50 700 100 0 0 0 0 0\n"
        # Counter reset (total went backwards) recovers on the next tick.
        expression = ("[parseCpuPercent(%s), parseCpuPercent(%s), "
                      "parseCpuPercent(%s)]"
                      % (json.dumps(steady), json.dumps(steady),
                         json.dumps("cpu  200 0 100 800 200 0 0 0 0 0\n")))
        value = self.run_case(expression)
        self.assertEqual(value["out"], ["...", "...", "71%"])
        for bad in ("", "garbage\n", "cpu  1 2 3\n"):
            value = self.run_case(
                "parseCpuPercent(%s)" % json.dumps(bad))
            self.assertEqual(value["out"], "...", repr(bad))

    def test_stale_ticks_warn_once_and_recover(self):
        expression = """
root.memValue = "..."; root.cpuValue = "...";
noteStatsTick(); noteStatsTick(); noteStatsTick(); noteStatsTick();
const stuck = [root._memStaleTicks, root._cpuStaleTicks,
  root._memStaleWarned, root._cpuStaleWarned];
root.memValue = "12%"; root.cpuValue = "34%";
noteStatsTick();
const recovered = [root._memStaleTicks, root._cpuStaleTicks,
  root._memStaleWarned, root._cpuStaleWarned];
[stuck, recovered]
"""
        value = self.run_case(expression)
        self.assertEqual(value["out"][0], [4, 4, True, True])
        self.assertEqual(value["out"][1], [0, 0, False, False])
        # Exactly one warn per metric despite four stuck ticks.
        self.assertEqual(len(value["warnings"]), 2)
        self.assertTrue(any("meminfo" in warning for warning in value["warnings"]))
        self.assertTrue(any("/proc/stat" in warning for warning in value["warnings"]))


    def test_stale_accounting_runs_on_the_poll_tick(self):
        start = self.SOURCE.index('objectName: "systemStatsPoll"')
        poll = self.SOURCE[start:start + 600]
        self.assertIn("memFile.reload()", poll)
        self.assertIn("cpuFile.reload()", poll)
        self.assertIn("root.noteStatsTick()", poll)


class AudioDisplayTokenTests(unittest.TestCase):
    def test_dead_visualizer_stays_deleted_and_no_one_off_easing(self):
        self.assertFalse((ROOT / "widgets" / "AudioDisplay.qml").exists())
        qml_files = list(ROOT.glob("widgets/*.qml")) + [ROOT / "shell.qml"]
        hits = [p for p in qml_files if p.exists() and "Easing.InOutQuad" in p.read_text(encoding="utf-8")]
        self.assertEqual(hits, [])


class PopoutExclusivityTests(unittest.TestCase):
    def _shell(self):
        return (ROOT / "shell.qml").read_text(encoding="utf-8")

    def _close_others(self):
        shell = self._shell()
        start = shell.index("function closeOthers(except)")
        brace = shell.index("{", start)
        depth = 0
        for i in range(brace, len(shell)):
            if shell[i] == "{":
                depth += 1
            elif shell[i] == "}":
                depth -= 1
                if depth == 0:
                    return shell[start:i + 1]
        raise AssertionError("unterminated closeOthers")

    def test_close_others_uses_full_dismissals(self):
        body = self._close_others()
        # Stateless popouts dismiss directly.
        self.assertIn("if (except !== mediaPopout) mediaPopout.setOpen(false);", body)
        self.assertIn("if (except !== calendarPopout) calendarPopout.setOpen(false);", body)
        # The overview aborts in-flight resume plan/execute (generation
        # bumps + process kills); a bare setOpen would leak them.
        self.assertIn("if (except !== projectOverview && projectOverview.requestedOpen) projectOverview.closePopup();", body)
        self.assertNotIn("projectOverview.setOpen(false)", body)
        # The password dialog is modal over ControlCenter: opening it must
        # not close its originator (connection-status feedback survives).
        self.assertIn("if (except !== controlCenter && except !== passwordPopup) controlCenter.setOpen(false);", body)
        # External password dismissal notifies + clears via the single path.
        self.assertIn("if (except !== passwordPopup) passwordPopup.cancelAndClose();", body)
        self.assertNotIn("passwordPopup.setOpen(false)", body)

    def test_requested_open_handlers_gate_on_truthiness(self):
        shell = self._shell()
        # Recursion guard: closeOthers only runs for the popout that just
        # opened (closePopup/cancelAndClose set requestedOpen=false, so
        # the closing side never re-enters).
        for name in ("mediaPopout", "calendarPopout", "projectOverview",
                     "controlCenter", "passwordPopup"):
            self.assertIn("if (%s.requestedOpen) closeOthers(%s);" % (name, name), shell, name)

    def test_catcher_sits_above_windows_below_popouts(self):
        shell = self._shell()
        catcher = shell[shell.index("id: popoutCatcher"):shell.index("id: popoutCatcher") + 1600]
        self.assertIn("screen: barWindow.barScreen", catcher)
        self.assertIn("readonly property var barScreen:", shell)
        self.assertIn("WlrLayer.Top", catcher)
        self.assertNotIn("WlrLayer.Bottom", catcher)
        self.assertIn("margins {", catcher)
        self.assertIn("top: barWindow.barHeight", catcher)
        self.assertIn("readonly property var barHeight:", shell)
        self.assertNotIn("top: 40", catcher)
        self.assertIn("WlrKeyboardFocus.None", catcher)
        # Visible only while a non-password anchored popout is open...
        for marker in ("mediaPopout.requestedOpen || mediaPopout.visible",
                       "calendarPopout.requestedOpen || calendarPopout.visible",
                       "projectOverview.requestedOpen || projectOverview.visible",
                       "controlCenter.requestedOpen || controlCenter.visible"):
            self.assertIn(marker, catcher, marker)
        # ...and a click on it dismisses via the shared path.
        self.assertIn("MouseArea {", catcher)
        self.assertIn("onClicked: closeOthers(null)", catcher)

    def test_per_screen_password_follows_its_screen(self):
        shell = self._shell()
        popup = shell[shell.index("PasswordPopup {"):shell.index("PasswordPopup {") + 200]
        self.assertIn("id: passwordPopup", popup)
        self.assertIn("screen: barWindow.barScreen", popup)

    def test_escape_closes_every_dismissible_popout(self):
        for name in ("MediaPopout.qml", "CalendarPopout.qml",
                     "ProjectOverviewPopup.qml", "PasswordPopup.qml"):
            source = (ROOT / "widgets" / name).read_text(encoding="utf-8")
            self.assertIn('sequence: "Escape"', source, name)

    def test_password_single_external_dismissal_path(self):
        popup = (ROOT / "widgets" / "PasswordPopup.qml").read_text(encoding="utf-8")
        self.assertIn("function cancelAndClose()", popup)
        guard = popup[popup.index("function cancelAndClose()"):popup.index("function cancelAndClose()") + 400]
        self.assertIn("!root.requestedOpen || root.closing", guard)
        self.assertIn("root.canceled()", guard)
        self.assertIn("root.dismiss()", guard)
        self.assertIn("return false", guard)
        self.assertIn("return true", guard)
        # Every external path routes through it: scrim click, card cancel,
        # Escape shortcut.
        self.assertEqual(popup.count("root.cancelAndClose()"), 3)
        self.assertIn("onClicked: root.cancelAndClose()", popup)
        self.assertIn("onActivated: root.cancelAndClose()", popup)
        # canceled() fires exactly once (inside the guard); the field is
        # cleared exactly once (inside dismiss()).
        self.assertEqual(popup.count("root.canceled()"), 1)
        self.assertEqual(popup.count('passwordField.text = ""'), 1)
        # Defense in depth: backdrop clicks disabled during the exit fade.
        self.assertIn("enabled: root.requestedOpen && !root.closing", popup)


if __name__ == "__main__":
    unittest.main()
