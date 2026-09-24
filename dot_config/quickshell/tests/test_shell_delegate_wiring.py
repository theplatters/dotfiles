import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SHELL = (ROOT / "shell.qml").read_text(encoding="utf-8")


def delegate_block(source):
    # Extract the Variants delegate block by brace-matching from the
    # delegate token to its matching closing brace.
    # Note: brace counting does not account for braces inside strings or
    # comments (acceptable for this file today).
    token = "delegate: PanelWindow {"
    start = source.index(token)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening:index + 1]
    raise AssertionError("unterminated delegate block")


class ShellDelegateWiringTests(unittest.TestCase):
    def test_no_unqualified_same_name_binding_to_outer_id(self):
        block = delegate_block(SHELL)
        # Same-name property bindings (name: name) on their own line.
        same_name = set()
        for match in re.finditer(r"^\s*(\w+)\s*:\s*(\w+)\s*$", block, re.M):
            name, value = match.group(1), match.group(2)
            if name == value:
                same_name.add(name)
        # Ids declared inside the delegate resolve locally and stay valid.
        inner_ids = set(re.findall(r"\bid:\s*(\w+)", block))
        dangling = same_name - inner_ids
        self.assertEqual(
            dangling, set(),
            "unqualified same-name binding(s) %r inside the per-screen "
            "delegate resolve to the child's own (null) property instead "
            "of the outer id; route them through a qualified "
            "barWindow.*Ref" % (dangling,),
        )

    def test_delegate_root_refs_declared(self):
        block = delegate_block(SHELL)
        self.assertIn(
            "readonly property var projectPlannerRef: projectPlanner", block)
        self.assertIn(
            "readonly property var notifServerRef: notifServer", block)
        self.assertIn(
            "readonly property var notifHistoryRef: notifHistory", block)
        self.assertIn(
            "readonly property var systemStatsRef: systemStats", block)
        self.assertIn(
            "readonly property var projectSourceRef: currentProjectSource", block)
        self.assertIn(
            "readonly property var clockSourceRef: sharedClock", block)
        self.assertIn(
            "readonly property var passwordPopupRef: passwordPopup", block)

    def test_qualified_refs_are_declared(self):
        block = delegate_block(SHELL)
        referenced = set(re.findall(r"barWindow\.(\w+)", block))
        declared = set(re.findall(r"readonly property var (\w+)\s*:", block))
        self.assertEqual(
            referenced - declared, set(),
            "dangling barWindow ref(s) %r have no matching readonly "
            "property var declaration inside the delegate and would "
            "resolve to null at runtime" % (referenced - declared,),
        )

    def test_popup_bar_and_cc_use_qualified_refs(self):
        block = delegate_block(SHELL)
        # Popup and Bar share the planner; Bar and CC share the notif
        # server; CC also takes the password popup through the ref.
        self.assertIn("projectPlanner: barWindow.projectPlannerRef", block)
        self.assertIn("notifServer: barWindow.notifServerRef", block)
        self.assertIn("notifHistory: barWindow.notifHistoryRef", block)
        self.assertIn("systemStats: barWindow.systemStatsRef", block)
        self.assertIn("projectSource: barWindow.projectSourceRef", block)
        self.assertIn("clockSource: barWindow.clockSourceRef", block)
        self.assertIn("passwordPopup: barWindow.passwordPopupRef", block)
        # The top-level banner binding is outside the delegate and stays
        # direct; the delegate block must not carry the unqualified forms.
        self.assertNotIn("projectPlanner: projectPlanner", block)
        top_level = SHELL[SHELL.index("NotificationPopout {"):]
        self.assertIn("notifServer: notifServer", top_level)


class ShellAmbientWiringTests(unittest.TestCase):
    def test_single_shared_ambient_resolver(self):
        # S-057: one shell-level AmbientContext; the palette and the
        # planner bind it via ambientSource (same pattern as
        # agenda: dailyAgenda), one hop each.
        self.assertEqual(SHELL.count("AmbientContext {"), 1)
        self.assertIn("id: ambientContext", SHELL)
        self.assertEqual(SHELL.count("ambientSource: ambientContext"), 2)


if __name__ == "__main__":
    unittest.main()
