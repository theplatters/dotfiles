import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
PLANNER = (ROOT / "widgets" / "ProjectPlanner.qml").read_text(encoding="utf-8")
JOURNAL = (ROOT / "widgets" / "JournalAssistant.qml").read_text(encoding="utf-8")
PALETTE = (ROOT / "widgets" / "CommandPalette.qml").read_text(encoding="utf-8")
THEME = (ROOT / "theme" / "Theme.qml").read_text(encoding="utf-8")
BUTTON = (ROOT / "widgets" / "WidgetButton.qml").read_text(encoding="utf-8")
ICON_BUTTON = (ROOT / "widgets" / "WidgetIconButton.qml").read_text(encoding="utf-8")

ICONS = {
    "folder.svg": "projects",
    "journal.svg": "journal",
    "panel-right-close.svg": "panel close",
    "panel-right-open.svg": "panel open",
    "chevron-down.svg": "tray toggle",
    "x.svg": "close",
    "history.svg": "history",
}


class WidgetIconAssetsTests(unittest.TestCase):
    def test_shared_svg_assets_are_valid_local_vectors(self):
        for name in ICONS:
            path = ROOT / "widgets" / "icons" / name
            self.assertTrue(path.is_file(), "missing icon asset: " + name)
            text = path.read_text(encoding="utf-8")
            self.assertIn("<svg", text)
            self.assertIn('viewBox="0 0 24 24"', text)
            self.assertIn("stroke-linecap", text)
            self.assertIn("stroke-linejoin", text)
            # Chevron toggle is a single chevron path (rotated 180 when open).
            if name == "chevron-down.svg":
                self.assertIn("<path", text)
            # Local original assets: no remote fetches or font glyphs.
            # (The xmlns namespace declaration itself contains http; strip it.)
            no_ns = text.replace('xmlns="http://www.w3.org/2000/svg"', "")
            self.assertNotIn("http://", no_ns)
            self.assertNotIn("https://", no_ns)
            self.assertNotIn("<text", text.lower())
            lowered = text.lower()
            self.assertNotIn("nerd", lowered)
            self.assertNotIn("font", lowered)

    def test_icon_set_covers_unified_controls(self):
        self.assertEqual(set(ICONS), {
            "folder.svg", "journal.svg", "panel-right-close.svg",
            "panel-right-open.svg", "chevron-down.svg", "x.svg", "history.svg",
        })


class WidgetSharedControlsTests(unittest.TestCase):
    def test_theme_exposes_shared_sizing_tokens(self):
        self.assertIn("controlMinHeight", THEME)
        self.assertIn("iconButtonSize", THEME)
        self.assertIn("iconSize", THEME)
        self.assertIn("44", THEME)
        self.assertIn("22", THEME)

    def test_widget_button_keeps_consistent_touch_size_and_states(self):
        self.assertIn("import QtQuick.Controls", BUTTON)
        self.assertIn("Theme.controlMinHeight", BUTTON)
        self.assertIn("Theme.fontFamily", BUTTON)
        self.assertIn("Theme.controlRadius", BUTTON)
        self.assertIn("Theme.motionFast", BUTTON)
        for state in ("hovered", "pressed", "checked", "enabled", "activeFocus"):
            self.assertIn(state, BUTTON)
        # Generous horizontal padding around the 40-44px minimum height.
        self.assertIn("leftPadding", BUTTON)
        self.assertIn("rightPadding", BUTTON)
        self.assertIn("16", BUTTON)

    def test_widget_icon_button_keeps_hit_target_focus_and_tooltip(self):
        self.assertIn("import QtQuick.Controls", ICON_BUTTON)
        self.assertIn("Theme.iconButtonSize", ICON_BUTTON)
        self.assertIn("Theme.iconSize", ICON_BUTTON)
        self.assertIn("property url iconSource", ICON_BUTTON)
        self.assertIn("Image {", ICON_BUTTON)
        self.assertIn("source: root.iconSource", ICON_BUTTON)
        self.assertIn("anchors.centerIn: parent", ICON_BUTTON)
        self.assertIn("ToolTip.text", ICON_BUTTON)
        self.assertIn("ToolTip.visible", ICON_BUTTON)
        self.assertIn("hovered || activeFocus", ICON_BUTTON)
        self.assertIn("Accessible.name", ICON_BUTTON)
        for state in ("hovered", "pressed", "checked", "enabled", "activeFocus"):
            self.assertIn(state, ICON_BUTTON)
        self.assertIn("Theme.fontFamily", ICON_BUTTON)
        self.assertIn("Theme.controlRadius", ICON_BUTTON)
        self.assertIn("Theme.motionFast", ICON_BUTTON)
        # Glyph-only rotation: inner Image rotates, Button background stays
        # contained (default 0 preserves existing controls).
        self.assertIn("property real iconRotation", ICON_BUTTON)
        self.assertIn("rotation: root.iconRotation", ICON_BUTTON)
        self.assertIn("transformOrigin: Item.Center", ICON_BUTTON)
        # Border toggle: default true preserves every existing icon; the
        # tray chevron opts out with borderVisible: false in Bar.qml.
        self.assertIn("property bool borderVisible", ICON_BUTTON)
        self.assertIn("root.borderVisible ? 1 : 0", ICON_BUTTON)

    def test_tray_toggle_hides_chevron_border(self):
        bar = (ROOT / "widgets" / "Bar.qml").read_text(encoding="utf-8")
        toggle = bar[bar.index('id: trayToggle'):bar.index("Rectangle { width: 1; height: 16")]
        self.assertIn("borderVisible: false", toggle)


class WidgetAdoptionTests(unittest.TestCase):
    def test_planner_tabs_close_and_tasks_use_shared_icon_controls(self):
        for marker in ('text: "Projects"', 'text: "Journal"', 'text: "Close"'):
            self.assertIn(marker, PLANNER)
        self.assertIn("WidgetIconButton", PLANNER)
        self.assertIn('iconSource: "icons/folder.svg"', PLANNER)
        self.assertIn('iconSource: "icons/journal.svg"', PLANNER)
        self.assertIn('iconSource: "icons/x.svg"', PLANNER)
        self.assertIn("checked: root.activeTab", PLANNER)
        self.assertIn('onClicked: root.selectTab("projects")', PLANNER)
        self.assertIn('onClicked: root.selectTab("journal")', PLANNER)
        self.assertIn("onClicked: root.close()", PLANNER)
        self.assertIn('Accessible.name: "Projects tab"', PLANNER)
        self.assertIn('Accessible.name: "Journal assistant tab"', PLANNER)
        # Tasks toggle keeps its identity, guard-adjacent bindings, and
        # switches the panel open/close glyph with its tooltip.
        self.assertIn("id: tasksToggle", PLANNER)
        tasks = PLANNER[PLANNER.index("id: tasksToggle"):PLANNER.index("id: chatArea")]
        self.assertIn("WidgetIconButton", PLANNER[PLANNER.index("id: tasksToggle") - 80:PLANNER.index("id: chatArea")])
        self.assertIn("checkable: true", tasks)
        self.assertIn("checked: root.tasksOpen", tasks)
        self.assertIn('Accessible.name: "Toggle tasks panel"', tasks)
        self.assertIn("onClicked: root.tasksOpen = !root.tasksOpen", tasks)
        self.assertIn("icons/panel-right-close.svg", tasks)
        self.assertIn("icons/panel-right-open.svg", tasks)

    def test_planner_and_journal_session_buttons_share_widget_button(self):
        # Journal keeps three WidgetButtons with direct session actions.
        self.assertIn("WidgetButton", JOURNAL)
        for label in ('text: "New session"', 'text: "Rename"',
                      'text: "Restore session"'):
            self.assertIn(label, JOURNAL)
        self.assertIn("onClicked: root.newSession()", JOURNAL)
        self.assertIn("onClicked: root.openRename()", JOURNAL)
        self.assertIn("onClicked: root.restoreSession()", JOURNAL)
        self.assertIn("enabled:", JOURNAL)
        self.assertIn("root.sessionControlBlockedReason()", JOURNAL)
        # Planner compacts the same three actions into a Session menu
        # opened from a WidgetButton, preserving gating and routing.
        self.assertIn("WidgetButton", PLANNER)
        self.assertIn("id: sessionMenuButton", PLANNER)
        self.assertIn("id: sessionMenu", PLANNER)
        self.assertIn("MenuItem {", PLANNER)
        for label in ('text: "New session"', 'text: "Rename"',
                      'text: "Restore session"'):
            self.assertIn(label, PLANNER)
        session_block = PLANNER[PLANNER.index("id: sessionMenu"):PLANNER.index("id: modelMenu")]
        self.assertIn("root.sessionControlsEnabled()", session_block)
        self.assertIn("root.newSession()", session_block)
        self.assertIn("root.openRename()", session_block)
        self.assertIn("root.restoreSession()", session_block)
        self.assertIn("root.sessionMenuValidFor(owner, path, session)", session_block)
        self.assertIn("onClicked: sessionMenu.open()", PLANNER)
        self.assertIn("enabled:", PLANNER)
        self.assertIn("root.sessionControlsEnabled()", PLANNER)

    def test_palette_history_toggle_uses_shared_icon_control(self):
        self.assertIn("WidgetIconButton", PALETTE)
        self.assertIn("id: historyToggle", PALETTE)
        history = PALETTE[PALETTE.index("id: historyToggle"):PALETTE.index("id: historyInput")]
        self.assertIn('text: root.showHistory ? "Hide history" : "History"', history)
        self.assertIn('iconSource: "icons/history.svg"', history)
        self.assertIn("checkable: true", history)
        self.assertIn("checked: root.showHistory", history)
        self.assertIn("onClicked: root.toggleHistory()", history)
        self.assertIn("Accessible.name", history)
        self.assertIn("function toggleHistory()", PALETTE)
        self.assertIn("root.showHistory = !root.showHistory", PALETTE)


class PrimaryButtonUnificationTests(unittest.TestCase):
    def test_styled_button_is_removed_and_widget_button_is_sole_primary(self):
        self.assertFalse((ROOT / "sidebar" / "StyledButton.qml").exists())
        for path in ROOT.rglob("*.qml"):
            if ".git" in path.parts or "services" in path.parts:
                continue
            self.assertNotIn("StyledButton", path.read_text(encoding="utf-8"), str(path))
        # Connection controls now live in the embeddable NetworkPanel (used
        # persistently by ControlCenter); the legacy popup is a thin wrapper.
        panel = (ROOT / "widgets" / "NetworkPanel.qml").read_text(encoding="utf-8")
        self.assertIn("WidgetButton {", panel)
        self.assertNotIn('import "../sidebar"', panel)
        cc = (ROOT / "widgets" / "ControlCenter.qml").read_text(encoding="utf-8")
        self.assertIn("WidgetButton {", cc)
        self.assertIn("WidgetIconButton {", cc)


if __name__ == "__main__":
    unittest.main()
