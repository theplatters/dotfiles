from pathlib import Path


ROOT = Path(__file__).parents[1]
QML = tuple(ROOT.rglob("*.qml"))


def test_rich_black_theme_tokens_are_centralised():
    theme = (ROOT / "theme" / "Theme.qml").read_text()

    for token in ("#070707", "#0C0C0C", "#111111", "#161616", "#1D1D1D", "#F5F5F5", "#A1A1A1", "#6F6F6F"):
        assert token in theme
    assert '"Inter"' in theme
    assert 'fallbackFontFamily: "Noto Sans"' in theme


def test_qml_does_not_reintroduce_legacy_motion_or_effects():
    source = "\n".join(path.read_text() for path in QML)

    assert "OutBack" not in source
    assert "duration: 300" not in source
    assert "gradient" not in source.lower()
    assert "glow" not in source.lower()


def test_command_palette_keeps_safe_visibility_and_keyboard_hints():
    palette = (ROOT / "widgets" / "CommandPalette.qml").read_text()
    approval = (ROOT / "widgets" / "PaletteApprovalDialog.qml").read_text()

    assert "property bool closing" in palette
    assert "exitMotion" in palette
    assert "root.visible = false" in palette
    assert "Ctrl-N/P navigate" in palette
    assert "font.pixelSize: 20" in palette
    assert "function immediateUnmap()" in palette
    assert "function scheduleScreenshot(mode)" in palette
    assert "screenshotDelay.restart()" in palette
    assert "function cancelPendingScreenshot()" in palette
    assert "input.focus = false" in palette
    assert "Layout.preferredHeight: visible ? Math.min(280" in approval
    assert "id: choiceList" in approval
    immediate = palette.split("function immediateUnmap()", 1)[1].split("function setOpen", 1)[0]
    assert "captureGeneration" not in immediate

    screenshot_branch = palette.split('if (name.indexOf("screenshot-") === 0)', 1)[1].split("if (name === \"wifi\")", 1)[0]
    assert "scheduleScreenshot(name.substring(11))" in screenshot_branch
    assert "close()" not in screenshot_branch


def test_network_discovery_resyncs_when_requested_state_reopens_in_place():
    # Discovery logic now lives in the embeddable NetworkPanel; the legacy
    # NetworkPopup wrapper delegates to it.
    panel = (ROOT / "widgets" / "NetworkPanel.qml").read_text()
    wrapper = (ROOT / "widgets" / "NetworkPopup.qml").read_text()
    cc = (ROOT / "widgets" / "ControlCenter.qml").read_text()

    assert "function syncDiscovery()" in panel
    # Explicit active gate: scans only when open AND on the matching tab.
    assert "property bool active" in panel
    assert "root.active && root.visible" in panel
    assert "root.activeTab === 0" in panel
    assert "root.activeTab === 1" in panel
    # Sync on visibility/tab/device changes.
    assert "onVisibleChanged" in panel
    assert "onActiveChanged" in panel
    assert "onActiveTabChanged" in panel
    # Wrapper still resyncs on open/close via the panel.
    assert "panel.syncDiscovery()" in wrapper
    # ControlCenter drives panel active + tab routing without hidden scans.
    assert "netPanel.syncDiscovery()" in cc
    assert "netPanel.activeTab = 0" in cc
    assert "netPanel.activeTab = 1" in cc


def test_popup_lifecycle_has_one_requested_state_and_no_duplicate_handlers():
    lifecycle_files = (
        "AudioPopup.qml",
        "BatteryPopup.qml",
        "ControlCenter.qml",
        "MediaPopout.qml",
        "NetworkPopup.qml",
        "PasswordPopup.qml",
    )
    for name in lifecycle_files:
        source = (ROOT / "widgets" / name).read_text()
        assert "property bool requestedOpen" in source, name
        assert "function setOpen(open)" in source, name
        assert source.count("onVisibleChanged") <= 1, name

    media = (ROOT / "widgets" / "MediaPopout.qml").read_text()
    assert "onVisibleChanged" not in media


def test_primary_button_text_remains_contrasting():
    styled = (ROOT / "widgets" / "WidgetButton.qml").read_text()
    password = (ROOT / "widgets" / "PasswordPopup.qml").read_text()
    assert "Theme.text" in styled
    assert "color: Theme.text" in password
