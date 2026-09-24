import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
QML = tuple(ROOT.rglob("*.qml"))


def _compact(source):
    """Source with all whitespace removed for formatting-robust matching."""
    return re.sub(r"\s+", "", source)


def _strip_line_comments(source):
    return "\n".join(line.split("//", 1)[0] for line in source.splitlines())


def _handler_count(source, name):
    """Count QML `onX: ...` handler definitions, ignoring // comments."""
    code = _strip_line_comments(source)
    return len(re.findall(r"\b" + re.escape(name) + r"\s*[:{]", code))


def _function_body(source, name):
    """Extract the brace-delimited body of `function name(...) { ... }`."""
    start = source.index("function " + name + "(")
    brace = source.index("{", start)
    depth = 0
    for i in range(brace, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[brace:i + 1]
    raise AssertionError("unbalanced braces in function " + name)


def test_rich_black_theme_tokens_are_centralised():
    theme = (ROOT / "theme" / "Theme.qml").read_text()

    for token in ("#070707", "#0C0C0C", "#111111", "#161616", "#1D1D1D", "#F5F5F5", "#A1A1A1", "#6F6F6F"):
        assert token in theme
    assert '"Inter"' in theme
    assert 'fallbackFontFamily: "Noto Sans"' in theme
    # Modal dim layer is a token, not a literal scattered across backdrops.
    assert "scrim" in theme
    assert "#B0070707" in theme


def test_qml_does_not_reintroduce_legacy_motion_or_effects():
    source = "\n".join(path.read_text() for path in QML)

    assert "OutBack" not in source
    assert "duration: 300" not in source
    assert "gradient" not in source.lower()
    assert "glow" not in source.lower()


def test_command_palette_keeps_safe_visibility_and_keyboard_hints():
    palette = (ROOT / "widgets" / "CommandPalette.qml").read_text()
    approval = (ROOT / "widgets" / "PaletteApprovalDialog.qml").read_text()
    palette_c = _compact(palette)

    assert "propertyboolclosing" in palette_c
    assert "exitMotion" in palette
    assert "root.visible=false" in palette_c
    # Shortcut hints are pinned on stable tokens, not the full sentence, so
    # copy tweaks don't break the test. The hint row must disclose
    # navigation, run, and hide affordances together.
    hint_lines = [line for line in palette.splitlines() if "Ctrl-N" in line]
    assert hint_lines, "palette must disclose its Ctrl-N/P navigation hint"
    assert any("Esc" in line for line in hint_lines), "hint row must also disclose Esc"
    assert any("Enter" in line for line in hint_lines), "hint row must also disclose Enter"
    # Query input keeps an explicitly sized, themed text size (any value).
    assert "id: input" in palette
    input_block = palette[palette.index("id: input"):palette.index("id: input") + 1500]
    assert re.search(r"font\.pixelSize\s*:\s*\d+", input_block), "query input must set an explicit text size"
    assert "Theme.fontFamily" in input_block, "query input must use the themed font"
    assert "function immediateUnmap()" in palette
    assert "function scheduleScreenshot(mode)" in palette
    assert "function cancelPendingScreenshot()" in palette
    # Delayed hyprshot scheduling lives in ScreenshotAction (the single
    # owner the bar will reuse); the palette keeps thin delegates.
    assert "screenshotAction.scheduleScreenshot(mode)" in _compact(palette)
    assert "screenshotAction.cancelPendingScreenshot()" in _compact(palette)
    screenshot_helper = (ROOT / "widgets" / "ScreenshotAction.qml").read_text()
    assert "hyprshot" in screenshot_helper
    assert "shotDelay.restart()" in _compact(screenshot_helper)
    assert "function screenshotCommand(mode)" in screenshot_helper
    assert "signal unmapRequested()" in screenshot_helper
    # Instant unmap releases focus and hides the surface without touching
    # capture state (which the delayed screenshot owns).
    unmap = _function_body(palette, "immediateUnmap")
    unmap_c = _compact(unmap)
    assert "input.focus=false" in unmap_c, "instant unmap must release input focus"
    assert "visible=false" in unmap_c, "instant unmap must hide the surface"
    assert "captureGeneration" not in unmap, "instant unmap must not touch capture state"
    # Approval choice list exists with a bounded, option-driven height (bound
    # value and formatting may change; boundedness must not).
    assert "id: choiceList" in approval
    assert "titleFor" in approval, "approval dialog must title its request"
    choice_block = _compact(approval[approval.index("id: choiceList"):approval.index("id: choiceList") + 800])
    assert "Layout.preferredHeight:" in choice_block, "choice list must bind its height"
    assert re.search(r"Math\.min\(\d+,", choice_block), "choice list height must stay bounded"
    assert "root.request.options" in choice_block, "choice list height must follow option count"

    # Screenshot actions stay prefix-routed: the stripped mode is scheduled
    # and the palette is left open (the delayed capture unmaps it).
    run = _function_body(palette, "runAction")
    run_c = _compact(run)
    assert 'if(name.indexOf("screenshot-")===0)' in run_c, "screenshot actions stay prefix-routed"
    assert "scheduleScreenshot(" in run_c, "screenshot actions must schedule a delayed capture"
    assert "name.substring(11)" in run_c, "screenshot prefix must be stripped when scheduling"
    screenshot_branch = run_c.split('if(name.indexOf("screenshot-")===0)', 1)[1].split('if(name==="wifi")', 1)[0]
    assert "scheduleScreenshot(" in screenshot_branch
    assert "close()" not in screenshot_branch, "screenshot scheduling must not close the palette directly"


def test_network_discovery_resyncs_when_requested_state_reopens_in_place():
    # Discovery logic lives in the embeddable NetworkPanel; the legacy
    # NetworkPopup wrapper was deleted (D1), so there is no wrapper to
    # delegate through.
    panel = (ROOT / "widgets" / "NetworkPanel.qml").read_text()
    cc = (ROOT / "widgets" / "ControlCenter.qml").read_text()
    panel_c = _compact(panel)
    cc_c = _compact(cc)

    assert not (ROOT / "widgets" / "NetworkPopup.qml").exists()
    assert "functionsyncDiscovery()" in panel_c
    # Explicit active gate: scans only when open AND on the matching tab.
    assert "propertyboolactive" in panel_c
    assert "root.active&&root.visible" in panel_c
    assert "root.activeTab===0" in panel_c
    assert "root.activeTab===1" in panel_c
    # Sync on visibility/tab/device changes (identifier-based handler
    # presence, robust to body reformatting).
    for handler in ("onVisibleChanged", "onActiveChanged", "onActiveTabChanged"):
        assert _handler_count(panel, handler) >= 1, handler
    # ControlCenter drives panel active + tab routing without hidden scans.
    assert "netPanel.syncDiscovery()" in cc_c
    assert "netPanel.activeTab=0" in cc_c
    assert "netPanel.activeTab=1" in cc_c
    # One-way routing (S-011): CC owns selectedTab, the panel is a slave.
    # The mapping lives in onSelectedTabChanged only; the old reverse
    # handler (panel activeTab writing back to selectedTab) is deleted and
    # all four tab buttons share the single openSection() path. The
    # invariant preserved: syncDiscovery() gates on open+visible+tab.
    assert "onActiveTabChanged" not in cc, "CC must not track the panel tab back"
    assert "target:netPanel" not in cc_c, "reverse Connections to netPanel must stay deleted"
    for entry in ("openSection(0)", "openSection(1)", "openSection(2)", "openSection(3)"):
        assert entry in cc_c, entry
    assert "selectedTab=tab" in cc_c, "openSection must set the owned tab"


def test_popup_lifecycle_has_one_requested_state_and_no_duplicate_handlers():
    lifecycle_files = (
        "CalendarPopout.qml",
        "ControlCenter.qml",
        "MediaPopout.qml",
        "PasswordPopup.qml",
        "ProjectOverviewPopup.qml",
    )
    # Deleted legacy wrappers stay deleted (D1).
    for gone in ("AudioPopup.qml", "BatteryPopup.qml", "NetworkPopup.qml"):
        assert not (ROOT / "widgets" / gone).exists(), gone
    for name in lifecycle_files:
        source = (ROOT / "widgets" / name).read_text()
        assert "propertyboolrequestedOpen" in _compact(source), name
        assert "functionsetOpen(open)" in _compact(source), name
        assert _handler_count(source, "onVisibleChanged") <= 1, name

    media = (ROOT / "widgets" / "MediaPopout.qml").read_text()
    assert _handler_count(media, "onVisibleChanged") == 0


def test_primary_button_text_remains_contrasting():
    styled = (ROOT / "widgets" / "WidgetButton.qml").read_text()
    password = (ROOT / "widgets" / "PasswordPopup.qml").read_text()
    assert "Theme.text" in styled
    assert "color: Theme.text" in password


def test_generated_markdown_renders_through_shared_markdown_body():
    # §6.1: generated markdown has one convention. MarkdownBody.qml is
    # the shared body: MarkdownText, Wrap, bounded lines + elide,
    # token-driven (no hard colors/fonts).
    body = (ROOT / "widgets" / "MarkdownBody.qml").read_text()
    assert "textFormat: Text.MarkdownText" in body
    assert "wrapMode: Text.Wrap" in body
    assert "maximumLineCount" in body
    assert "Text.ElideRight" in body
    assert "Theme.text" in body
    assert "Theme.fontFamily" in body
    assert "#" not in body.replace("../theme", ""), \
        "MarkdownBody must use theme tokens, not literal colors"
    # The Review card's generated entry body renders through it (the
    # §6.1 bug was Text.PlainText on entry.markdown).
    review = (ROOT / "widgets" / "ReviewCard.qml").read_text()
    assert "MarkdownBody {" in review
    assert "root.markdownText()" in review
    # Exact write previews stay exact: PreviewPanel keeps PlainText +
    # monospace so Confirm shows the exact bytes to be written.
    panel = (ROOT / "widgets" / "PreviewPanel.qml").read_text()
    assert "textFormat: TextEdit.PlainText" in panel
    assert "monospace" in panel
    assert "MarkdownBody" not in panel
    # User-authored text stays PlainText (untrusted-input rule):
    # session capture rows, thought editor, capture inbox rows, and
    # the journal composer never render markdown.
    session = (ROOT / "widgets" / "SessionCard.qml").read_text()
    assert "MarkdownBody" not in session
    assert "textFormat: Text.PlainText" in session
    inbox = (ROOT / "widgets" / "CaptureInbox.qml").read_text()
    assert "MarkdownBody" not in inbox
    journal = (ROOT / "widgets" / "JournalAssistant.qml").read_text()
    composer = journal[journal.index("id: journalComposer"):]
    assert "textFormat: TextEdit.PlainText" in composer[:800]
