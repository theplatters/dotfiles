import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
PLANNER = (ROOT / "widgets" / "ProjectPlanner.qml").read_text(encoding="utf-8")
JOURNAL = (ROOT / "widgets" / "JournalAssistant.qml").read_text(encoding="utf-8")
PALETTE = (ROOT / "widgets" / "CommandPalette.qml").read_text(encoding="utf-8")
SESSION_CARD = (ROOT / "widgets" / "SessionCard.qml").read_text(encoding="utf-8")
CAPTURE_INBOX = (ROOT / "widgets" / "CaptureInbox.qml").read_text(encoding="utf-8")
REVIEW_CARD = (ROOT / "widgets" / "ReviewCard.qml").read_text(encoding="utf-8")
PREVIEW_PANEL = (ROOT / "widgets" / "PreviewPanel.qml").read_text(encoding="utf-8")
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
    "play.svg": "resume",
    "message-circle.svg": "ask pi",
    "send.svg": "composer send",
    "stop.svg": "composer stop",
}


def _compact(source):
    """Source with all whitespace removed for formatting-robust matching."""
    return re.sub(r"\s+", "", source)


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
            "play.svg", "message-circle.svg", "send.svg", "stop.svg",
        })


class WidgetSharedControlsTests(unittest.TestCase):
    def test_theme_exposes_shared_sizing_tokens(self):
        self.assertIn("controlMinHeight", THEME)
        self.assertIn("iconButtonSize", THEME)
        self.assertIn("iconSize", THEME)
        self.assertIn("iconSizeSmall", THEME)
        self.assertIn("chipRadius", THEME)
        self.assertIn("motionFast", THEME)
        self.assertIn("motionPanel", THEME)
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
        # Generous horizontal padding around the 40-44px minimum height
        # (values may change; the declarations must stay explicit).
        self.assertIn("leftPadding", BUTTON)
        self.assertIn("rightPadding", BUTTON)
        self.assertRegex(BUTTON, r"leftPadding\s*:\s*\d+")
        self.assertRegex(BUTTON, r"rightPadding\s*:\s*\d+")

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
        start = bar.index("id: trayToggle")
        toggle = bar[start:start + 2000]
        # The window must cover the whole toggle control (icon + click
        # handler), so the border opt-out below is scoped to it.
        self.assertIn('iconSource: "icons/chevron-down.svg"', toggle)
        self.assertIn("onClicked", toggle)
        self.assertIn("borderVisible:false", _compact(toggle))


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

    def test_planner_and_journal_session_controls_share_menu_grammar(self):
        # S-031: both surfaces use one Session… menu with identical
        # labels/order and the same three session actions.
        self.assertIn("id: journalSessionMenuButton", JOURNAL)
        self.assertIn("id: sessionMenuButton", PLANNER)
        for source in (JOURNAL, PLANNER):
            self.assertIn("WidgetButton", source)
            self.assertIn("id: sessionMenu", source)
            self.assertIn("MenuItem {", source)
            for label in ('text: "New session"', 'text: "Rename"',
                          'text: "Restore session"'):
                self.assertIn(label, source)
            self.assertIn("onClicked: sessionMenu.open()", source)
            block = source[source.index("id: sessionMenu"):]
            order = [block.index(label) for label in
                     ('text: "New session"', 'text: "Rename"',
                      'text: "Restore session"')]
            self.assertEqual(order, sorted(order))
        journal_block = JOURNAL[JOURNAL.index("id: sessionMenu"):JOURNAL.index('Text { text: "Conversation"')]
        for call in ("root.newSession()", "root.openRename()", "root.restoreSession()"):
            self.assertIn(call, journal_block)
        self.assertIn("root.sessionControlBlockedReason()", journal_block)
        planner_block = PLANNER[PLANNER.index("id: sessionMenu"):PLANNER.index("id: modelMenu")]
        for call in ("root.newSession()", "root.openRename()", "root.restoreSession()"):
            self.assertIn(call, planner_block)
        self.assertIn("root.sessionControlsEnabled()", planner_block)
        self.assertIn("root.sessionMenuValidFor(owner, path, session)", planner_block)

    def test_palette_resume_actions_use_shared_icon_controls(self):
        for marker in ("id: resumeActionButton", "id: askResumeActionButton",
                       "id: resumeHistoryActionButton"):
            self.assertIn(marker, PALETTE)
        resume_start = PALETTE.index("id: resumeActionButton")
        ask_start = PALETTE.index("id: askResumeActionButton")
        history_start = PALETTE.index("id: resumeHistoryActionButton")
        for start in (resume_start, ask_start, history_start):
            self.assertIn("WidgetIconButton", PALETTE[max(0, start - 120):start])
        resume = PALETTE[resume_start:ask_start]
        ask = PALETTE[ask_start:history_start]
        resume_history = PALETTE[history_start:history_start + 1200]
        # Resume label reflects busy state (wording may change; the binding
        # and both states must stay).
        self.assertIn("root.resumeExecuteBusy", resume)
        self.assertIn('"Resume"', resume)
        self.assertIn('"Resuming', resume)
        self.assertIn('iconSource: "icons/play.svg"', resume)
        self.assertIn("tooltipText:", resume)
        self.assertIn("Resuming", resume)
        self.assertIn("enabled: !root.resumeExecuteBusy", resume)
        self.assertIn("onClicked: root.resumeSelectedProject()", resume)
        self.assertIn("Accessible.name", resume)
        self.assertIn('text: "Ask Pi"', ask)
        self.assertIn('iconSource: "icons/message-circle.svg"', ask)
        self.assertIn("tooltipText:", ask)
        self.assertIn("Accessible.name", ask)
        self.assertIn("onClicked: root.askResumeProject()", ask)
        self.assertIn('text: "Open history"', resume_history)
        self.assertIn('iconSource: "icons/history.svg"', resume_history)
        self.assertIn("tooltipText:", resume_history)
        self.assertIn("Accessible.name", resume_history)
        self.assertIn("onClicked: root.historyResumeProject()", resume_history)
        row_start = PALETTE.rindex("RowLayout {", 0, resume_start)
        row = PALETTE[row_start:history_start + 2000]
        # Action row keeps explicit spacing and stretches (values and
        # formatting may change; the declarations must stay).
        self.assertRegex(row, r"spacing\s*:\s*\d+")
        self.assertRegex(row, r"Layout\.topMargin\s*:\s*\d+")
        self.assertIn("Layout.fillWidth:true", _compact(row))
        self.assertIn("Item {", row)

    def test_planner_composer_actions_use_shared_icon_controls(self):
        for marker in ("id: projectSendButton", "id: projectStopButton"):
            self.assertIn(marker, PLANNER)
        send_start = PLANNER.index("id: projectSendButton")
        stop_start = PLANNER.index("id: projectStopButton")
        composer_start = PLANNER.index("id: composerBar")
        self.assertLess(composer_start, send_start)
        self.assertLess(send_start, stop_start)
        for start in (send_start, stop_start):
            self.assertIn("WidgetIconButton", PLANNER[max(0, start - 120):start])
        send = PLANNER[send_start:stop_start]
        stop = PLANNER[stop_start:stop_start + 1200]
        composer = PLANNER[composer_start:stop_start + 1200]
        # Shared icon controls with local assets, tooltips, and a11y.
        self.assertIn('iconSource: "icons/send.svg"', send)
        self.assertIn('iconSource: "icons/stop.svg"', stop)
        self.assertIn("tooltipText:", send)
        # Send control discloses its keyboard shortcut (wording may change;
        # the shortcut token must stay).
        self.assertIn("Ctrl+Enter", send)
        self.assertIn("Loading", send)
        self.assertIn('tooltipText: "Stop project agent"', stop)
        self.assertIn("Accessible.name", send)
        self.assertIn("Accessible.description", send)
        self.assertIn("loading fresh context before sending", send.lower())
        self.assertIn("Accessible.name", stop)
        self.assertIn("Accessible.description", stop)
        self.assertIn("abort", stop.lower())
        self.assertIn("Layout.alignment:Qt.AlignVCenter", _compact(send))
        self.assertIn("Layout.alignment:Qt.AlignVCenter", _compact(stop))
        # Unchanged text gates and handlers (match whitespace-insensitively so
        # formatter rewraps don't break the test; every gate stays pinned).
        send_c = _compact(send)
        stop_c = _compact(stop)
        self.assertIn("root.sendBusy", send_c)
        self.assertIn('"Send"', send_c)
        self.assertIn('"Loading', send_c)
        self.assertIn('text:"Stop"', stop_c)
        self.assertIn(_compact("enabled: !!root.selectedAgent && root.selectedAgent.ready && !root.selectedAgent.busy && !root.pageBusy && !root.toggleBusy && !root.toggleRetiring && !root.sendBusy && !root.approvalRequest && !root.hasBusyAgent()"), send_c)
        self.assertIn(_compact("enabled: !!root.selectedAgent && (root.selectedAgent.busy || root.selectedAgent.pendingApproval)"), stop_c)
        self.assertIn("onClicked: root.send()", send)
        self.assertIn("onClicked: root.stopAgent()", stop)
        # Composer layout, input, and keyboard behavior are unchanged
        # (values match whitespace-insensitively).
        self.assertIn("id: composer", composer)
        self.assertRegex(composer, r"spacing\s*:\s*\d+")
        self.assertIn("Layout.fillWidth:true", _compact(composer))
        self.assertIn("onTextChanged: root.setDraft(root.selectedPath, text)", composer)
        self.assertIn("Qt.ControlModifier", composer)
        self.assertIn("root.send()", composer)
        self.assertIn("Qt.Key_Escape", composer)
        # Shared WidgetIconButton control is untouched.
        self.assertIn("property url iconSource", ICON_BUTTON)
        self.assertIn("ToolTip.text", ICON_BUTTON)
        self.assertIn("Accessible.name", ICON_BUTTON)

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


    def test_palette_grammar_names_confirms_and_close_stay(self):
        # S-019: one name per session concept (Pi session rows say so).
        self.assertIn('["New Pi session", "new"]', PALETTE)
        self.assertIn('["Switch Pi session", "resume"]', PALETTE)
        self.assertIn('["Rename Pi session", "rename"]', PALETTE)
        # S-020: Resume stays on the preview-before-execute button while
        # planner handoffs are labelled as handoffs. Phase 2b renames the
        # session-row handoff verbs (Resume project / Open history /
        # Open session) and adds the seen:/todo: actions.
        self.assertIn('text: "Resume project"', PALETTE)
        self.assertIn('text: "Open session"', PALETTE)
        self.assertIn('text: "Open history"', PALETTE)
        self.assertIn('objectName: "seenOpenButton"', PALETTE)
        self.assertIn('objectName: "todoConfirmButton"', PALETTE)
        self.assertIn('"Resume"', PALETTE)
        # The footer states the actual close-vs-stay rule.
        self.assertIn("keep open", PALETTE)
        self.assertIn("Ctrl-N/P", PALETTE)
        # S-021: power actions use an explicit labelled confirm; a bare
        # second Enter never executes.
        self.assertIn("id: powerConfirmButton", PALETTE)
        self.assertIn("onClicked: root.confirmPowerAction()", PALETTE)
        self.assertIn("onClicked: root.cancelPowerConfirm()", PALETTE)
        key = PALETTE[PALETTE.index("function paletteKeyPressed"):]
        self.assertIn("if (root.confirming) event.accepted = true;", key)
        self.assertNotIn("root.runAction(root.confirmationAction)", key)
        # S-023: the armed capture action travels into the overlay header.
        self.assertIn("capture.captureActionLabel", PALETTE)
        # Screenshot scheduling delegates to the single owner.
        self.assertIn("ScreenshotAction {", PALETTE)
        self.assertIn("screenshotAction.scheduleScreenshot(mode)", PALETTE)
        self.assertIn("screenshotAction.cancelPendingScreenshot()", PALETTE)


def _control_block(source, object_name, before=600, after=600):
    marker = 'objectName: "%s"' % object_name
    index = source.index(marker)
    return source[max(0, index - before):index + after]


class CardVerbAndDescriptionTests(unittest.TestCase):
    """Phase 3 §6.4: one verb set on Session/Capture/Review cards, and
    every control carries Accessible.name + Accessible.description."""

    def test_session_card_verbs(self):
        # Some labels are busy-conditional (? "Loading…" : "Refresh"),
        # so verbs pin as substrings, not text: bindings.
        for verb in ('"Accept"', '"Dismiss"', '"Save thought"',
                     '"Organise"', '"Refresh"',
                     '"Dismiss session"', '"Open project"',
                     '"Resume"', '"Loading…"'):
            self.assertIn(verb, SESSION_CARD)
        # File Confirm uses the §6.4 file verb (single target, L1).
        self.assertIn('confirmText: "Save to project"', SESSION_CARD)
        # Header Resume routes the existing project path, never a row.
        resume = _control_block(SESSION_CARD, "sessionResumeButton")
        self.assertIn('"Resume"', resume)
        self.assertIn("headerResumeProject()", resume)
        self.assertIn("openProjectRequested", resume)
        self.assertNotIn("dismissSession", resume)
        # Header count is unconditional: Sessions · day (N).
        self.assertIn('"Sessions · " + root.dayLabel()', SESSION_CARD)
        self.assertIn('" (" + root.sortedEntries().length + ")"',
                      SESSION_CARD)
        # Truncation line follows the one S-041 pattern.
        self.assertIn('objectName: "sessionTruncation"', SESSION_CARD)
        self.assertIn("truncationText()", SESSION_CARD)
        self.assertIn('"showing " + shown + " of " + ', SESSION_CARD)

    def test_capture_inbox_verbs(self):
        for verb in ('"Accept"', '"Dismiss"', '"Refresh"',
                     '"Loading…"', '"Scan now"'):
            self.assertIn(verb, CAPTURE_INBOX)
        self.assertIn('objectName: "captureTruncation"', CAPTURE_INBOX)
        self.assertIn("truncationText()", CAPTURE_INBOX)
        self.assertIn('"showing " + shown + " of " + ', CAPTURE_INBOX)

    def test_review_card_verbs(self):
        # L1 keeps the Review journal save; its wording is pinned here
        # so any drift from the §6.4 set is a deliberate, visible edit.
        for verb in ('"Evening"', '"Morning"', '"Refresh"',
                     '"Loading…"', '"Save to journal"',
                     '"Add to tomorrow"', '"Add to today"'):
            self.assertIn(verb, REVIEW_CARD)
        self.assertIn('objectName: "reviewTruncation"', REVIEW_CARD)
        self.assertIn("truncationText()", REVIEW_CARD)
        self.assertIn('"showing " + shown + " of " + ', REVIEW_CARD)

    def test_every_card_control_has_name_and_description(self):
        cases = (
            (SESSION_CARD, ("sessionRefreshButton", "sessionResumeButton",
                            "sessionAcceptTodo", "sessionDismissTodo",
                            "sessionOrganise", "sessionSaveThought",
                            "sessionDismiss", "sessionOpenProject")),
            (CAPTURE_INBOX, ("captureRefreshButton", "captureScanButton",
                             "captureAddPage", "captureDismiss")),
            (REVIEW_CARD, ("reviewTabEvening", "reviewTabMorning",
                           "reviewRefreshButton", "reviewAddButton",
                           "reviewSaveButton")),
        )
        for source, names in cases:
            for name in names:
                with self.subTest(control=name):
                    block = _control_block(source, name)
                    self.assertIn("Accessible.name", block)
                    self.assertIn("Accessible.description", block)

    def test_preview_panel_confirm_verb_defaults_to_confirm(self):
        self.assertIn('property string confirmText: "Confirm"',
                      PREVIEW_PANEL)
        self.assertIn("root.confirmText", PREVIEW_PANEL)
        self.assertIn('"Applying…"', PREVIEW_PANEL)
        self.assertIn("Discard the preview without writing", PREVIEW_PANEL)


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
