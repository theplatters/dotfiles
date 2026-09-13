import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).parents[1]
GEOMETRY = ROOT / "widgets" / "PaletteGeometry.js"


@unittest.skipUnless(shutil.which("node"), "node is required for palette geometry helper coverage")
class PaletteGeometryTests(unittest.TestCase):
    def evaluate(self, expression):
        script = r'''
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const context = {};
vm.createContext(context);
vm.runInContext(source, context);
console.log(JSON.stringify(vm.runInContext(process.argv[2], context)));
'''
        completed = subprocess.run(
            ["node", "-e", script, str(GEOMETRY), expression],
            check=True, text=True, capture_output=True,
        )
        return json.loads(completed.stdout)

    def test_reversed_drag_normalizes(self):
        self.assertEqual(
            self.evaluate("normalizedSelection(200, 100, 20, 40)"),
            {"x": 20, "y": 40, "width": 180, "height": 60},
        )

    def test_tiny_drag_gets_4px_minimum(self):
        self.assertEqual(
            self.evaluate("normalizedSelection(10.2, 8.1, 11.1, 8.9)"),
            {"x": 10, "y": 8, "width": 4, "height": 4},
        )

    def test_non_finite_coordinates_return_null(self):
        self.assertEqual(
            self.evaluate(
                "[normalizedSelection(NaN, 0, 1, 1),"
                " normalizedSelection(0, 0, Infinity, 1),"
                ' normalizedSelection("a", 0, 1, 1)]'
            ),
            [None, None, None],
        )

    def test_geometry_string_adds_screen_offset(self):
        self.assertEqual(
            self.evaluate(
                'geometryForSelection({x: 20, y: 40, width: 180, height: 60}, {x: 1920, y: 0})'
            ),
            "1940,40 180x60",
        )

    def test_geometry_string_rejects_bad_selection(self):
        self.assertEqual(
            self.evaluate(
                '[geometryForSelection(null, {x: 0, y: 0}),'
                ' geometryForSelection({x: NaN, y: 0, width: 4, height: 4}, {x: 0, y: 0})]'
            ),
            ["", ""],
        )

    def test_clamp_stays_inside_bounds(self):
        self.assertEqual(
            self.evaluate("[clampSelectionCoordinate(-5, 800),"
                          " clampSelectionCoordinate(900, 800),"
                          " clampSelectionCoordinate(400, 800)]"),
            [0, 800, 400],
        )

    def test_safe_number_falls_back(self):
        self.assertEqual(
            self.evaluate('[safeNumber({x: 12}, "x", 0), safeNumber({x: NaN}, "x", 7),'
                          ' safeNumber(null, "x", 3), safeNumber({x: "12"}, "x", 5)]'),
            [12, 7, 3, 5],
        )


if __name__ == "__main__":
    unittest.main()
