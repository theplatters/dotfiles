import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).parents[1]
PALETTE = ROOT / "widgets" / "CommandPalette.qml"
QUERY = ROOT / "widgets" / "PaletteQuery.js"
TEXT = ROOT / "widgets" / "PaletteText.js"
GEOMETRY = ROOT / "widgets" / "PaletteGeometry.js"
MODEL = ROOT / "widgets" / "PaletteModel.js"


@unittest.skipUnless(shutil.which("node"), "node is required for parser coverage")
class PaletteRoutingTests(unittest.TestCase):
    def parse(self, *values):
        script = r'''
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const context = {};
vm.createContext(context);
vm.runInContext(source, context);
console.log(JSON.stringify(process.argv.slice(2).map(value => context.parseQuery(value))));
'''
        completed = subprocess.run(
            ["node", "-e", script, str(QUERY), *values],
            check=True, text=True, capture_output=True,
        )
        return json.loads(completed.stdout)

    def test_clipboard_aliases_require_a_boundary(self):
        self.assertEqual(
            self.parse("clip: cats", "CLIP cats", "clipboard cats", "clipboardfoo"),
            [
                {"mode": "clip", "text": "cats"},
                {"mode": "clip", "text": "cats"},
                {"mode": "clip", "text": "cats"},
                {"mode": "", "text": "clipboardfoo"},
            ],
        )

    def test_ai_file_and_symbol_routes(self):
        self.assertEqual(
            self.parse("AI:  hello, world!", "file notes", "file:notes", "> lock", "# old"),
            [
                {"mode": "ai", "text": "hello, world!"},
                {"mode": "file", "text": "notes"},
                {"mode": "file", "text": "notes"},
                {"mode": ">", "text": "lock"},
                {"mode": "#", "text": "old"},
            ],
        )

    def test_empty_plain_and_symbol_queries(self):
        self.assertEqual(
            self.parse("", "  ", "window", "/", "/ build"),
            [
                {"mode": "", "text": ""},
                {"mode": "", "text": ""},
                {"mode": "", "text": "window"},
                {"mode": "/", "text": ""},
                {"mode": "/", "text": "build"},
            ],
        )

    def test_native_selector_normalizes_reversed_and_small_drags(self):
        script = r'''
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const context = {};
vm.createContext(context);
vm.runInContext(source, context);
console.log(JSON.stringify([
  context.normalizedSelection(200, 100, 20, 40),
  context.normalizedSelection(10.2, 8.1, 11.1, 8.9)
]));
'''
        completed = subprocess.run(
            ["node", "-e", script, str(GEOMETRY)],
            check=True, text=True, capture_output=True,
        )
        self.assertEqual(json.loads(completed.stdout), [
            {"x": 20, "y": 40, "width": 180, "height": 60},
            {"x": 10, "y": 8, "width": 4, "height": 4},
        ])

    def test_model_and_rebuild_regressions_execute_javascript(self):
        script = r'''
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const querySource = fs.readFileSync(process.argv[2], "utf8");
const textSource = fs.readFileSync(process.argv[3], "utf8");
const modelSource = fs.readFileSync(process.argv[4], "utf8");

function extract(name) {
  const start = source.indexOf("function " + name + "(");
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === "{") depth++;
    if (source[i] === "}" && --depth === 0)
      return source.slice(start, i + 1);
  }
  throw new Error("missing " + name);
}

const resultModel = {
  items: [],
  clear() { this.items = []; },
  append(item) { this.items.push(item); },
  get count() { return this.items.length; }
};
const root = {
  mode: "", modeQuery: "", query: "", rows: [], todos: [],
  clipboardText: "1\tcat\n2\tdog", selectedIndex: 0,
  requestedOpen: true, confirming: false, confirmationAction: "",
  notice: "", showStats: false, fileTruncated: false,
  searchText() { return this.modeQuery; }
};
root.fileRows = [];
// Async caches live in PaletteDataSources.qml; rebuildModel/rebuild read
// them through the same dataSources object the QML uses.
const dataSources = {
  get todos() { return root.todos; },
  get clipboardText() { return root.clipboardText; },
  get fileRows() { return root.fileRows || []; },
  set fileRows(v) { root.fileRows = v; },
  pendingFileQuery: "",
  resetForQueryChange() { root.fileRows = []; },
  scheduleFileSearch() {},
};
const context = {
  root, resultModel, dataSources, clipboardText: root.clipboardText, todos: root.todos,
  agent: { commands: [], answer: "kept" },
  Hyprland: { toplevels: { values: [] }, workspaces: { values: [] } },
  DesktopEntries: { applications: { values: [] } },
  selector() { return root.mode.length === 1 && ">@%+#!/".includes(root.mode) ? root.mode : ""; },
  searchText() { return root.modeQuery; },
  addRow(prefix, title, subtitle, kind, payload, keywords) {
    root.rows.push({prefix, title: title || "", subtitle: subtitle || "", kind,
                    payload, keywords: keywords || "", score: 1});
  },
  loadCount: 0,
  loadModeData() { context.loadCount++; },
  fileDelay: { stop() {}, restart() {} },
  fileProcess: { running: false },
  fileGeneration: 0,
  pendingFileQuery: "",
  fileBusy: false,
  selectedIndex: 0,
  confirming: false,
  confirmationAction: "",
  notice: "",
  showStats: false,
  fileTruncated: false
};
vm.createContext(context);
// Query helpers live in PaletteQuery.js; rebuild() calls Query.parseQuery.
// Snippet/ordering helpers live in PaletteText.js/PaletteModel.js and are
// reached through the same qualified names the QML uses.
vm.runInContext(querySource, context);
context.Query = {parseQuery: context.parseQuery, score: context.score};
vm.runInContext(textSource, context);
context.PaletteText = {plainSnippet: context.plainSnippet,
  boundedCaptureDetail: context.boundedCaptureDetail, captureFailure: context.captureFailure};
vm.runInContext(modelSource, context);
context.PaletteModel = {compareRows: context.compareRows};
vm.runInContext(extract("rebuildModel"), context);
vm.runInContext(extract("rebuild"), context);

function model(mode, query) {
  root.mode = mode; root.modeQuery = query; root.rows = [];
  context.clipboardText = root.clipboardText;
  context.todos = root.todos;
  context.rebuildModel();
  return resultModel.count;
}

root.todos = [{task: "one", page: "Notes", marker: "TODO"}];
if (model("!", "") !== 1 || model("!", "") !== 1)
  throw new Error("TODO rows duplicated");
if (model(">", "") !== 26 || model(">", "") !== 26)
  throw new Error("command rows duplicated");
for (const query of ["capture", "translate", "summarize"]) {
  model(">", query);
  const payload = {capture: "capture-region", translate: "translate", summarize: "summarize"}[query];
  if (!root.rows.some(row => row.payload === payload))
    throw new Error("command search did not find > " + query);
}

root.mode = "ai"; root.modeQuery = "old"; root.query = "ai: old";
context.query = root.query; context.agent.answer = "kept";
context.rebuild();
if (context.agent.answer !== "kept") throw new Error("AI answer was cleared");

context.loadCount = 0; root.mode = ""; root.modeQuery = "";
context.query = "clip: cat"; context.rebuild();
context.query = "clip: dog"; context.rebuild();
if (context.loadCount !== 1) throw new Error("clipboard refetched while filtering: " + context.loadCount);

root.mode = "file"; root.modeQuery = "notes";
root.fileRows = [{prefix: "file:", title: "notes", subtitle: "/tmp/notes", kind: "file"}];
root.rows = [];
context.rebuildModel();
if (root.rows.length !== 1 || resultModel.count !== 1)
  throw new Error("file rows were not preserved from authoritative cache");
// An empty authoritative cache clears previously displayed file rows
// instead of resurrecting stale ones.
root.fileRows = [];
root.rows = [{prefix: "file:", title: "stale", subtitle: "/tmp/stale", kind: "file"}];
context.rebuildModel();
if (root.rows.length !== 0 || resultModel.count !== 0)
  throw new Error("empty file cache did not clear stale file rows");
'''
        completed = subprocess.run(
            ["node", "-e", script, str(PALETTE), str(QUERY), str(TEXT), str(MODEL)],
            check=False, text=True, capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")


if __name__ == "__main__":
    unittest.main()
