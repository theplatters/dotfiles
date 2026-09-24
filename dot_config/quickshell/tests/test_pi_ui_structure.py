import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
PALETTE = (ROOT / "widgets" / "CommandPalette.qml").read_text(encoding="utf-8")
APPROVAL = (ROOT / "widgets" / "PaletteApprovalDialog.qml").read_text(encoding="utf-8")
CAPTURE = (ROOT / "widgets" / "PaletteCapture.qml").read_text(encoding="utf-8")
DATASOURCES = (ROOT / "widgets" / "PaletteDataSources.qml").read_text(encoding="utf-8")
PALETTE_TEXT = (ROOT / "widgets" / "PaletteText.js").read_text(encoding="utf-8")
PALETTE_GEOMETRY = (ROOT / "widgets" / "PaletteGeometry.js").read_text(encoding="utf-8")
PALETTE_MODEL = (ROOT / "widgets" / "PaletteModel.js").read_text(encoding="utf-8")


class PaletteUiStructureTests(unittest.TestCase):
    """Regression coverage for the state/overlay contracts without a QML harness."""

    def test_approval_is_an_inline_modal_not_a_controls_popup(self):
        # The dialog component owns the inline modal; the palette only hosts it.
        self.assertIn("FocusScope", APPROVAL)
        self.assertIn("paletteVisible && root.paletteOpen && !root.paletteClosing && root.request !== null", APPROVAL)
        self.assertIn("anchors.fill: parent", APPROVAL)
        self.assertIn("acceptedButtons: Qt.AllButtons", APPROVAL)
        self.assertIn("id: choiceList", APPROVAL)
        self.assertIn("ScrollBar.vertical", APPROVAL)
        self.assertIn("Keys.onPressed", APPROVAL)
        self.assertIn("function openRequest(value)", APPROVAL)
        self.assertIn("function dismiss()", APPROVAL)
        self.assertIn("function close() { dismiss(); }", APPROVAL)
        self.assertIn("Qt.callLater(restoreFocus)", APPROVAL)
        self.assertIn("paletteInput.forceActiveFocus()", APPROVAL)
        self.assertNotIn(" Dialog {", APPROVAL)
        self.assertNotIn(" ComboBox {", APPROVAL)
        self.assertNotIn(" Dialog {", PALETTE)
        self.assertNotIn(" ComboBox {", PALETTE)
        # Palette wiring: owns the agent, routes requests into the dialog.
        self.assertIn("PaletteApprovalDialog", PALETTE)
        self.assertIn("property ScopedAgent agent", PALETTE)
        self.assertIn("approvalDialog.openRequest(request)", PALETTE)
        self.assertIn("approvalDialog.close()", PALETTE)
        self.assertIn("approvalDialog.openRequest(agent.pendingApproval)", PALETTE)

    def test_pure_helpers_come_from_shared_modules(self):
        self.assertIn('import "PaletteQuery.js" as Query', PALETTE)
        self.assertIn('import "PaletteText.js" as PaletteText', PALETTE)
        self.assertIn('import "PaletteGeometry.js" as PaletteGeometry', PALETTE)
        self.assertIn('import "PaletteModel.js" as PaletteModel', PALETTE)
        self.assertIn("Query.score(row, root.searchText())", PALETTE)
        self.assertIn("Query.parseQuery(query)", PALETTE)
        # The helpers must not be duplicated back into the component.
        self.assertNotIn("function parseQuery(", PALETTE)
        self.assertNotIn("function score(", PALETTE)
        self.assertNotIn("function plainSnippet(", PALETTE)
        self.assertNotIn("function boundedCaptureDetail(", PALETTE)
        self.assertNotIn("function captureFailure(", PALETTE)
        self.assertNotIn("function safeNumber(", PALETTE)
        self.assertNotIn("function normalizedSelection(", PALETTE)
        self.assertNotIn("function geometryForSelection(", PALETTE)
        self.assertNotIn("function clampSelectionCoordinate(", PALETTE)
        self.assertNotIn("function compareRows(", PALETTE)
        # …but they live exactly once in their modules.
        self.assertIn("function plainSnippet(value, limit, needle)", PALETTE_TEXT)
        self.assertIn("function boundedCaptureDetail(value)", PALETTE_TEXT)
        self.assertIn("function captureFailure(code, errorText)", PALETTE_TEXT)
        self.assertIn("function normalizedSelection(startX, startY, endX, endY)", PALETTE_GEOMETRY)
        self.assertIn("function geometryForSelection(selection, offset)", PALETTE_GEOMETRY)
        self.assertIn("function compareRows(a, b)", PALETTE_MODEL)
        # State-coupled helpers stay in QML.
        self.assertIn("function noMatchText()", PALETTE)
        self.assertIn("function screenOffset()", PALETTE)

    def test_extracted_widgets_exist_with_documented_contracts(self):
        for name, source in (("PaletteApprovalDialog.qml", APPROVAL),
                             ("PaletteCapture.qml", CAPTURE),
                             ("PaletteDataSources.qml", DATASOURCES)):
            self.assertTrue((ROOT / "widgets" / name).is_file(), "missing " + name)
            self.assertIn("Responsibility:", source)
            self.assertIn("Contract with CommandPalette:", source)
            self.assertIn("Owned state", source)
        # Approval contract: agent in, deferring dismiss (S-048 parks the
        # request queued round-robin instead of a view-only close).
        self.assertIn("property var agent", APPROVAL)
        self.assertIn("property var request", APPROVAL)
        self.assertIn("parks the request queued", APPROVAL)
        self.assertIn("agent.deferRequest(deferredId)", APPROVAL)
        # Capture contract: captured/failed/reopen signals, prompt preserved,
        # stale drops inside, screenshot stays in the palette.
        self.assertIn("signal reopenRequest(string message)", CAPTURE)
        self.assertIn("signal captured(string prompt, var images)", CAPTURE)
        self.assertIn("signal failed(string message)", CAPTURE)
        self.assertIn("preserved for retry", CAPTURE)
        self.assertIn("generation !== root.captureGeneration", CAPTURE)
        self.assertIn("function scheduleScreenshot(mode)", PALETTE)
        self.assertNotIn("function scheduleScreenshot", CAPTURE)
        self.assertNotIn("id: screenshotDelay", CAPTURE)
        # Data-source contract: caches/flags/generations, loaded() rebuild.
        self.assertIn("signal loaded(string source)", DATASOURCES)
        self.assertIn("LOGSEQ_GRAPH", DATASOURCES)
        self.assertIn('"--query"', DATASOURCES)
        self.assertIn('"--limit", "40"', DATASOURCES)
        self.assertIn("interval: 180", DATASOURCES)
        self.assertIn("function ensureTodoData()", DATASOURCES)
        self.assertIn("function finishFileSearch(", DATASOURCES)
        self.assertIn("function rebuildModel()", PALETTE)
        self.assertIn("function rebuild()", PALETTE)
        # No duplicated logic left in the orchestrator.
        # NOTE (Phase 2b §4.3): `id: todoProcess` is intentionally
        # palette-owned — the todo: quick-add ladder drives the composer
        # row, the exact-preview Confirm overlay, and the stays-open
        # serial entry, none of which belong in the data-source caches.
        for needle in ("function openRequest(", "function moveChoice(",
                       "function beginRegionSelection(", "function finishRegionSelection(",
                       "function cancelRegionSelection(", "function reopenCaptureAi(",
                       "function cancelPendingCapture()",
                       "function ensureTodoData(", "function ensureClipboardData(",
                       "function finishTodoLoad(", "function finishClipboardLoad(",
                       "function finishFileSearch(", "function startFileSearch(",
                       "property bool selectingRegion", "property string captureGeometry",
                       "property var todos", "property string clipboardText",
                       "id: captureProcess", "id: fileProcess",
                       "id: approval\n", "id: selectionOverlay", "id: choiceList"):
            self.assertNotIn(needle, PALETTE, "duplicated in palette: " + needle)
        # The palette-owned todo ladder keeps its contract markers.
        self.assertIn("id: todoProcess", PALETTE)
        self.assertIn("function todoConfirmApply()", PALETTE)
        self.assertIn('objectName: "todoConfirmButton"', PALETTE)

    def test_switch_command_reports_rejected_agent_action(self):
        self.assertIn('["Switch Pi session", "resume"]', PALETTE)
        self.assertIn("if (!agent.switchSession()) notice", PALETTE)

    def test_capture_region_label_matches_its_search_term(self):
        self.assertIn('["Capture region for Pi", "capture-region"]', PALETTE)

    def test_capture_process_drains_output_and_reopens_ai_detail(self):
        capture = CAPTURE[CAPTURE.index("id: captureProcess"):CAPTURE.index("function beginRegionSelection")]
        self.assertEqual(capture.count("waitForEnd: true"), 2)
        self.assertIn("stderr: StdioCollector", capture)
        self.assertIn("root.failed(PaletteText.captureFailure(code, captureError.text))", capture)
        self.assertIn("root.captured(prompt, data.images || [])", capture)
        self.assertIn("generation !== root.captureGeneration", capture)
        # The palette owns the draft/agent side of those signals.
        # Capture-first sends ride through the ambient wrapper
        # (images-capable variant), never raw.
        self.assertIn("root.agent.prompt(captured.prompt, captured.images)", PALETTE)
        self.assertNotIn("root.agent.prompt(prompt, images", PALETTE)
        self.assertIn('root.query = "ai:"', PALETTE)
        self.assertIn("input.text = root.query", PALETTE)
        self.assertIn("function boundedCaptureDetail(value)", PALETTE_TEXT)
        self.assertIn("captureError.text", CAPTURE)

    def test_capture_cancellation_is_separate_from_immediate_unmap(self):
        self.assertIn("function cancelPendingCapture()", CAPTURE)
        self.assertEqual(PALETTE.count("capture.cancelPendingCapture();"), 2)
        self.assertIn("capture.cancelPendingCapture();", PALETTE)
        cancel = CAPTURE[CAPTURE.index("function cancelPendingCapture()"):CAPTURE.index("function cancelPendingScreenshot") if "function cancelPendingScreenshot" in CAPTURE else CAPTURE.index("    // Native selection")]
        self.assertIn("captureGeneration++;", cancel)
        self.assertIn("captureDelay.stop();", cancel)
        self.assertIn("if (captureProcess.running) captureProcess.running = false;", cancel)
        immediate = PALETTE[PALETTE.index("function immediateUnmap()"):PALETTE.index("function setOpen")]
        self.assertNotIn("captureGeneration", immediate)
        capture = CAPTURE[CAPTURE.index("id: captureProcess"):CAPTURE.index("function beginRegionSelection")]
        self.assertIn("generation !== root.captureGeneration", capture)

    def test_capture_guard_covers_every_prompt_rejection_state(self):
        actions = PALETTE[PALETTE.index('if (name === "capture-region"'):PALETTE.index("let prompts = {")]
        self.assertIn("agent.stopping", actions)
        self.assertIn("agent.controlPending", actions)
        self.assertIn('notice = "Pi is handling another operation"', actions)

    def test_capture_actions_are_gated_before_unmapping(self):
        actions = PALETTE[PALETTE.index('if (name === "capture-region"'):PALETTE.index("let prompts = {")]
        self.assertIn("if (!agent.ready)", actions)
        self.assertIn(
            "agent.busy || agent.compacting || agent.stopping || agent.controlPending || agent.sessionSwitching",
            actions,
        )
        self.assertLess(actions.index("capture.captureGeneration++"), actions.index("capture.beginRegionSelection"))
        self.assertIn('notice = "Capturing screen region…"', actions)
        self.assertNotIn("close()", actions)
        self.assertNotIn("immediateUnmap()", actions)

    def test_native_selector_normalizes_geometry_and_unmaps_only_on_release(self):
        self.assertIn("property bool selectingRegion: false", CAPTURE)
        self.assertIn("property real selectionStartX: 0", CAPTURE)
        self.assertIn("property real selectionCurrentX: 0", CAPTURE)
        self.assertIn("property string captureGeometry: \"\"", CAPTURE)
        normalize = PALETTE_GEOMETRY[PALETTE_GEOMETRY.index("function normalizedSelection("):PALETTE_GEOMETRY.index("function geometryForSelection(")]
        self.assertIn("Math.min(startX, endX)", normalize)
        self.assertIn("Math.max(4, right - left)", normalize)
        finish = CAPTURE[CAPTURE.index("function finishRegionSelection()"):CAPTURE.index("function cancelRegionSelection()")]
        self.assertIn("root.selectingRegion = false", finish)
        self.assertLess(finish.index("root.unmapRequested()"), finish.index("captureDelay.restart()"))
        self.assertIn("PaletteGeometry.geometryForSelection(selection, offset)", finish)
        cancel = CAPTURE[CAPTURE.index("function cancelRegionSelection()"):CAPTURE.index("function reopenCaptureAi(")]
        self.assertIn("root.cancelPendingCapture()", cancel)
        self.assertIn("root.selectionCancelled()", cancel)
        # Palette owns the visible side-effects of cancellation.
        self.assertIn('root.notice = "Capture cancelled"', PALETTE)
        self.assertIn("input.forceActiveFocus()", PALETTE)
        self.assertIn("onSelectionCancelled", PALETTE)

    def test_capture_timer_snapshots_geometry_command(self):
        timer = CAPTURE[CAPTURE.index("id: captureDelay"):CAPTURE.index("id: captureProcess")]
        self.assertIn("captureProcessGeometry = root.captureGeometry", timer)
        self.assertIn('"--geometry"', timer)
        self.assertIn("root.captureProcessCommand", timer)

    def test_screen_action_prompts_are_defaults_not_command_search_text(self):
        self.assertIn(
            '"Translate this screen region. Translate German to English by default '
            'and every other source language to German.',
            PALETTE,
        )
        self.assertIn('"Describe this screen region."', PALETTE)
        self.assertIn(
            '"Summarize this screen region and append the exact preview to the Logseq journal',
            PALETTE,
        )
        self.assertNotIn('" to " + text', PALETTE)
        self.assertIn('["Translate region with Pi", "translate"]', PALETTE)
        self.assertIn('["Summarize region to Logseq", "summarize"]', PALETTE)

    def test_capture_outer_item_is_fullscreen_above_card_below_approval(self):
        # P1: the outer capture Item carries fullscreen geometry + stacking;
        # the inner overlay z alone cannot lift a 0x0 parent above the card.
        outer = CAPTURE[:CAPTURE.index("property string capturePrompt")]
        self.assertIn("anchors.fill: parent", outer)
        self.assertIn("z: 50", outer)
        # Stacking order: card z:1 < capture z:50 < approval z:100.
        self.assertIn("z: 50", CAPTURE)
        self.assertIn("z: 100", APPROVAL)
        self.assertIn("id: card", PALETTE)
        # Inner selection overlay still fills the outer item and gates on
        # the palette mirrors + region state.
        overlay = CAPTURE[CAPTURE.index("id: selectionOverlay"):CAPTURE.index("id: selectionMouse")]
        self.assertIn("anchors.fill: parent", overlay)
        self.assertIn("root.paletteVisible && root.paletteOpen && root.selectingRegion", CAPTURE)
        # Mouse coords clamp against the real outer dimensions.
        mouse = CAPTURE[CAPTURE.index("id: selectionMouse"):CAPTURE.index("Keys.onPressed")]
        self.assertIn("PaletteGeometry.clampSelectionCoordinate(mouse.x, root.width)", mouse)
        self.assertIn("PaletteGeometry.clampSelectionCoordinate(mouse.y, root.height)", mouse)
        # Palette hosts the capture component (not an inlined overlay).
        self.assertIn("PaletteCapture", PALETTE)
        self.assertIn("id: capture", PALETTE)
        self.assertNotIn("id: selectionOverlay", PALETTE)

    def test_file_cache_is_authoritative_and_sorted_by_shared_comparator(self):
        # P2: file-mode reads the authoritative dataSources.fileRows cache
        # with no stale-fallback resurrection.
        rebuild = PALETTE[PALETTE.index("function rebuildModel()"):PALETTE.index("function rebuild()")]
        file_branch = rebuild[rebuild.index('if (root.mode === "file")'):rebuild.index('if (root.mode === "ai"')]
        self.assertIn("dataSources.fileRows", file_branch)
        self.assertNotIn("previousRows", rebuild)
        self.assertNotIn("cachedFiles = previousRows", rebuild)
        # Final sort reuses the shared comparator instead of duplicating it.
        self.assertIn("root.rows.sort(PaletteModel.compareRows)", rebuild)
        self.assertNotIn("scoreDifference", rebuild)

    def test_data_source_contract_and_guards_match_palette(self):
        # P3 + contract nits: calculatorOnly is bound in, stale docs are gone.
        self.assertNotIn("paletteSearchingFiles", DATASOURCES)
        self.assertNotIn("paletteUnified", DATASOURCES)
        self.assertIn("property bool calculatorOnly", DATASOURCES)
        self.assertIn("calculatorOnly: root.calculatorOnly()", PALETTE)
        should = DATASOURCES[DATASOURCES.index("function shouldSearchFiles()"):DATASOURCES.index("function finishFileSearch(")]
        # S-025: arithmetic-looking queries rank the calculator first but
        # never suppress file rows; calculatorOnly no longer gates.
        self.assertNotIn("!root.calculatorOnly", should)
        self.assertIn('root.mode === "file"', should)
        self.assertIn("root.isUnifiedSearch()", should)
        # resetForOpen intentionally leaves in-flight processes to the
        # generation bump (matches pre-extraction open()).
        reset = DATASOURCES[DATASOURCES.index("function resetForOpen()"):DATASOURCES.index("function resetForClose()")]
        self.assertNotIn("stopFileSearch()", reset)
        self.assertNotIn("fileDelay.stop()", reset)
        self.assertIn("Do not stop in-flight processes here", reset)
        # Loading gates document the pending-flag equivalence with the
        # pre-refactor process.running checks.
        self.assertIn("dataSources.clipboardPending", PALETTE)
        self.assertIn("dataSources.todoPending", PALETTE)
        self.assertIn("pending when the process starts", PALETTE)
        self.assertIn("clipboardProcess.running/todoProcess.running", PALETTE)
        self.assertIn("pending flags are the extracted equivalents", DATASOURCES)

    def test_shared_js_modules_have_headers_and_capture_callers(self):
        from pathlib import Path as _Path
        query_src = (_Path(__file__).parents[1] / "widgets" / "PaletteQuery.js").read_text(encoding="utf-8")
        self.assertTrue(query_src.startswith("// Query parsing"),
                        "PaletteQuery.js must carry a header mechanism block")
        self.assertIn("parseQuery(value)", query_src)
        self.assertIn("Callers:", query_src)
        self.assertIn("widgets/CommandPalette.qml", query_src)
        self.assertIn("PaletteCapture", PALETTE_TEXT)
        self.assertIn("PaletteCapture", PALETTE_GEOMETRY)


if __name__ == "__main__":
    unittest.main()
