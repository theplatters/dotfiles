import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).parents[1]
PALETTE = ROOT / "widgets" / "CommandPalette.qml"
DATASOURCES = ROOT / "widgets" / "PaletteDataSources.qml"
QUERY = ROOT / "widgets" / "PaletteQuery.js"
TEXT = ROOT / "widgets" / "PaletteText.js"
MODEL = ROOT / "widgets" / "PaletteModel.js"


@unittest.skipUnless(shutil.which("node"), "node is required for palette behavior coverage")
class UnifiedPaletteTests(unittest.TestCase):
    def run_javascript(self):
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
    if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error("missing " + name);
}

const resultModel = {
  items: [], clear() { this.items = []; },
  append(item) { this.items.push(item); },
  get count() { return this.items.length; },
  get(index) { return this.items[index]; }
};
const root = {
  mode: "", modeQuery: "needle", query: "needle", rows: [], fileRows: [],
  todos: [{task: "needle todo", page: "Notes", marker: "TODO"}],
  clipboardText: "1\tneedle clipboard\n2\tother", calculatorResult: {matched: false, value: "", error: ""},
  selectedIndex: 0, requestedOpen: true, confirming: false, confirmationAction: "",
  messages: [], showHistory: false, historyQuery: "",
  searchText() { return this.modeQuery; },
  copyRawText(value, purpose) { this.copied = {value, purpose}; },
  openHistory(value) { this.historyOpened = value; }
};
const windows = Array.from({length: 12}, (_, i) => ({title: "needle window " + i, address: "0x" + i}));
const apps = Array.from({length: 12}, (_, i) => ({name: "needle app " + i, id: "app" + i, comment: "desktop"}));
root.fileRows = Array.from({length: 12}, (_, i) => ({prefix: "file:", title: "needle file " + i,
  subtitle: "/tmp/needle-" + i, kind: "file", payload: {uri: "file:///tmp/" + i}}));
root.messages = Array.from({length: 12}, (_, i) => ({role: "user", text: "needle history " + i}));
// Query helpers live in PaletteQuery.js; the stub addRow below calls the
// real module score through the same Query object the QML uses. Snippet and
// ordering helpers live in PaletteText.js/PaletteModel.js; rebuildModel
// reaches them through the same qualified names the QML uses.
const Query = {};
const PaletteText = {};
const PaletteModel = {};
const dataSources = {
  get todos() { return root.todos; },
  set todos(v) { root.todos = v; },
  get clipboardText() { return root.clipboardText; },
  set clipboardText(v) { root.clipboardText = v; },
  get fileRows() { return root.fileRows || []; },
  set fileRows(v) { root.fileRows = v; },
};
const context = {
  root, resultModel, dataSources, Hyprland: {toplevels: {values: windows}, workspaces: {values: []}},
  DesktopEntries: {applications: {values: apps}},
  agent: {commands: [], messages: root.messages},
  todos: root.todos,
  selectedIndex: 0,
  Query,
  selector() { return root.mode.length === 1 && ">@%+#!/".includes(root.mode) ? root.mode : ""; },
  searchText() { return root.modeQuery; },
  PaletteText,
  PaletteModel,
  addRow(prefix, title, subtitle, kind, payload, keywords) {
    // Mirrors the QML addRow, using the real module score.
    const row = {prefix, title: title || "", subtitle: subtitle || "", kind, payload,
                 keywords: keywords || ""};
    row.order = root.rows.length;
    row.score = Query.score(row, root.modeQuery);
    if (row.score >= 0) root.rows.push(row);
  }
};
const Hyprland = context.Hyprland;
vm.createContext(context);
vm.runInContext(querySource, context);
Query.score = context.score;
Query.parseQuery = context.parseQuery;
vm.runInContext(textSource, context);
PaletteText.plainSnippet = context.plainSnippet;
vm.runInContext(modelSource, context);
PaletteModel.compareRows = context.compareRows;
for (const name of ["rebuildModel", "activate"])
  vm.runInContext(extract(name), context);

context.rebuildModel();
const kinds = root.rows.map(row => row.kind);
const counts = {};
for (const kind of kinds) counts[kind] = (counts[kind] || 0) + 1;
if (root.rows[0].prefix !== "!") throw new Error("deterministic tie ordering failed");
for (const kind of ["window", "app", "file", "clipboard", "todo", "history"])
  if (!counts[kind] || counts[kind] > 6) throw new Error("unified quota failed for " + kind);

// A direct match must win its source quota even when it follows six fuzzy
// matches.  This is also a regression for the old negative substring score.
root.fileRows = []; root.clipboardText = ""; root.todos = [];
root.messages = []; context.agent.messages = [];
Hyprland.toplevels.values = Array.from({length: 6}, () => ({title: "n e e d l e fuzzy", address: "f"}));
Hyprland.toplevels.values.push({title: "x".repeat(600) + "needle", address: "strong"});
context.rebuildModel();
if (!root.rows.some(row => row.kind === "window" && row.title.endsWith("needle")))
  throw new Error("strong seventh source match was hidden by quota");

// Search the complete raw value while rendering a useful bounded snippet.
Hyprland.toplevels.values = []; root.clipboardText = "1\t" + "x".repeat(600) + "needle clipboard";
root.messages = [{role: "assistant", text: "x".repeat(600) + "needle history"}];
context.agent.messages = root.messages; context.rebuildModel();
const lateRows = root.rows.filter(row => row.kind === "clipboard" || row.kind === "history");
if (lateRows.length !== 2 || lateRows.some(row => row.score < 0 || row.title.indexOf("needle") < 0))
  throw new Error("late full-text source match was not searchable/displayed");

// Filtering must happen before the explicit-mode display cap.
root.mode = "!"; root.modeQuery = "unique-todo";
root.todos = Array.from({length: 40}, (_, i) => ({task: "filler " + i, page: "Notes", marker: "TODO"}));
root.todos.push({task: "unique-todo", page: "Notes", marker: "TODO"}); context.todos = root.todos;
resultModel.items = []; context.rebuildModel();
if (!root.rows.some(row => row.title === "unique-todo")) throw new Error("41st TODO was not searchable");
root.mode = "%"; root.modeQuery = "unique-workspace";
Hyprland.workspaces.values = Array.from({length: 40}, (_, i) => ({name: "filler " + i, id: i, toplevels: {values: []}}));
Hyprland.workspaces.values.push({name: "unique-workspace", id: 41, toplevels: {values: []}});
resultModel.items = []; context.rebuildModel();
if (!root.rows.some(row => row.title === "unique-workspace")) throw new Error("41st workspace was not searchable");

const routes = [context.parseQuery("calc: 1 + 2"), context.parseQuery("= -2^2"),
                context.parseQuery("+ terminal"), context.parseQuery("/build")];
if (routes[0].mode !== "=" || routes[0].text !== "1 + 2" || routes[1].text !== "-2^2" ||
    routes[2].mode !== "+" || routes[3].mode !== "/") throw new Error("calculator route stole a prefix");

root.mode = "="; root.modeQuery = "1 + 2";
root.calculatorResult = {matched: true, value: "3", error: ""};
root.rows = []; resultModel.items = [];
context.rebuildModel();
context.activate(0);
if (!root.copied || root.copied.value !== "3" || root.copied.purpose !== "calculator")
  throw new Error("calculator activation did not use raw copy");

root.mode = ""; root.modeQuery = "needle"; root.rows = [{kind: "history", payload: {query: "needle"}}];
resultModel.items = [{rowIndex: 0}]; root.historyOpened = "";
context.activate(0);
if (root.historyOpened !== "needle") throw new Error("history activation did not navigate to its filter");
console.log(JSON.stringify({counts, routes, copied: root.copied, history: root.historyOpened}));
'''
        return subprocess.run(
            ["node", "-e", script, str(PALETTE), str(QUERY), str(TEXT), str(MODEL)],
            check=True,
            text=True,
            capture_output=True,
        )

    def test_unified_sources_are_balanced_and_actions_are_safe(self):
        result = json.loads(self.run_javascript().stdout)
        self.assertEqual(result["copied"], {"value": "3", "purpose": "calculator"})
        self.assertEqual(result["history"], "needle")
        self.assertEqual(result["routes"][2]["mode"], "+")

    def test_source_caps_bound_per_keystroke_work(self):
        # S-056: named bounds exist and ingest/scoring respects them —
        # only the most recent maxSourceRows clipboard lines and agent
        # messages are considered, with bounded match text per row.
        palette_text = PALETTE.read_text(encoding="utf-8")
        query_text = QUERY.read_text(encoding="utf-8")
        datasources_text = DATASOURCES.read_text(encoding="utf-8")
        self.assertIn("property int maxSourceRows: 200", palette_text)
        self.assertIn("property int maxMatchSnippet: 240", palette_text)
        self.assertIn("property int maxSourceRows: 200", datasources_text)
        self.assertIn("MAX_SOURCE_ROWS", query_text)
        self.assertIn("MAX_MATCH_SNIPPET", query_text)
        self.assertIn("function boundedKeywords(", query_text)
        self.assertIn("function prepareRow(", query_text)
        self.assertIn("_matchText", query_text)
        self.assertIn("_matchText", palette_text)
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
    if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error("missing " + name);
}
const resultModel = {
  items: [], clear() { this.items = []; },
  append(item) { this.items.push(item); },
  get count() { return this.items.length; },
  get(index) { return this.items[index]; }
};
const clipLines = Array.from({length: 250},
  (_, i) => (i + 1) + "\t" + "x".repeat(500) + " filler " + (i + 1));
clipLines[149] = "150\t" + "x".repeat(500) + " early-needle-zzz";
clipLines[249] = "250\t" + "x".repeat(500) + " late-needle-zzz";
const messages = Array.from({length: 250},
  (_, i) => ({role: "user", text: "y".repeat(500) + " msg " + (i + 1)}));
messages[10] = {role: "user", text: "y".repeat(500) + " hist-old-zzz"};
messages[240] = {role: "user", text: "y".repeat(500) + " hist-early-zzz"};
const root = {
  mode: "", modeQuery: "early-needle-zzz", query: "early-needle-zzz", rows: [],
  calculatorResult: {matched: false, value: "", error: ""},
  selectedIndex: 0, requestedOpen: true,
  maxSourceRows: 200, maxMatchSnippet: 240,
  searchText() { return this.modeQuery; },
};
const dataSources = {clipboardText: clipLines.join("\n"), fileRows: [], todos: []};
const Query = {};
const PaletteText = {};
const PaletteModel = {};
const context = {
  root, resultModel, dataSources,
  Hyprland: {toplevels: {values: []}, workspaces: {values: []}},
  DesktopEntries: {applications: {values: []}},
  agent: {commands: [], messages},
  selectedIndex: 0,
  Query, PaletteText, PaletteModel,
  selector() { return root.mode.length === 1 && ">@%+#!/".includes(root.mode) ? root.mode : ""; },
  searchText() { return root.modeQuery; },
};
vm.createContext(context);
vm.runInContext(querySource, context);
Query.score = context.score;
Query.boundedKeywords = context.boundedKeywords;
Query.prepareRow = context.prepareRow;
vm.runInContext(textSource, context);
PaletteText.plainSnippet = context.plainSnippet;
vm.runInContext(modelSource, context);
PaletteModel.compareRows = context.compareRows;
for (const name of ["addRow", "rebuildModel"])
  vm.runInContext(extract(name), context);
function clipRows() { return root.rows.filter(row => row.kind === "clipboard"); }
function histRows() { return root.rows.filter(row => row.kind === "history"); }
root.modeQuery = "early-needle-zzz";
context.rebuildModel();
const earlyClip = clipRows();
if (earlyClip.length !== 1) throw new Error("early clipboard line was not searchable");
if (earlyClip[0].keywords.length > 240) throw new Error("clipboard keywords are not bounded");
if (earlyClip[0].keywords.indexOf("early-needle-zzz") >= 0)
  throw new Error("full clip value is still matched as a keyword");
if (typeof earlyClip[0]._matchText !== "string" || !earlyClip[0]._matchText)
  throw new Error("row match text is not precomputed");
if (earlyClip[0].payload !== "150") throw new Error("clipboard copy payload lost its id");
root.rows = []; resultModel.items = [];
root.modeQuery = "late-needle-zzz";
context.rebuildModel();
if (clipRows().length !== 0) throw new Error("clipboard line past the cap was searched");
root.rows = []; resultModel.items = [];
root.modeQuery = "hist-early-zzz";
context.rebuildModel();
const earlyHist = histRows();
if (earlyHist.length !== 1) throw new Error("recent agent message was not searchable");
if (earlyHist[0].keywords.length > 240) throw new Error("history keywords are not bounded");
if (earlyHist[0].payload.message.text.indexOf("hist-early-zzz") < 0)
  throw new Error("history open payload lost its full text");
root.rows = []; resultModel.items = [];
root.modeQuery = "hist-old-zzz";
context.rebuildModel();
if (histRows().length !== 0) throw new Error("agent message past the cap was searched");
console.log("ok");
'''
        completed = subprocess.run(
            ["node", "-e", script, str(PALETTE), str(QUERY), str(TEXT), str(MODEL)],
            check=True,
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.stdout.strip(), "ok")

    def test_async_sources_are_once_per_open_and_not_restarted_by_filtering(self):
        script = r'''
const fs = require("fs");
const vm = require("vm");
// ensure* helpers now live in PaletteDataSources.qml; loadModeData stays in
// the palette orchestrator and reaches them as dataSources.ensure*.
const paletteSource = fs.readFileSync(process.argv[1], "utf8");
const dataSource = fs.readFileSync(process.argv[2], "utf8");
function extractFrom(source, name) {
  const start = source.indexOf("function " + name + "(");
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === "{") depth++;
    if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error("missing " + name);
}
const todoProcess = {running: false}, clipboardProcess = {running: false};
// Palette + data-source state share one root here: the extracted data-source
// helpers address `root.*` exactly like the QML component, and loadModeData
// reaches them as dataSources.ensure* with palette-mirrored generations.
const root = {
  requestedOpen: true, paletteOpen: true, mode: "", modeQuery: "one", openingGeneration: 4,
  calculatorResult: {matched: false}, todoPending: false, todoLoaded: false, todoFailed: false,
  clipboardPending: false, clipboardLoaded: false, clipboardFailed: false,
  todoProcessGeneration: 0, clipboardProcessGeneration: 0,
  isUnifiedSearch() { return this.mode === "" && !!this.modeQuery; },
  calculatorOnly() { return false; },
  shouldSearchFiles() { return true; },
};
const fileDelay = {restart() { this.restarts = (this.restarts || 0) + 1; }};
const dataSources = root;
const context = {root, dataSources, todoProcess, clipboardProcess, fileDelay,
  isUnifiedSearch() { return root.mode === "" && !!root.modeQuery; },
  calculatorOnly() { return false; },
  shouldSearchFiles() { return true; }};
vm.createContext(context);
for (const name of ["ensureTodoData", "ensureClipboardData"])
  vm.runInContext(extractFrom(dataSource, name), context);
dataSources.ensureTodoData = context.ensureTodoData;
dataSources.ensureClipboardData = context.ensureClipboardData;
dataSources.scheduleFileSearch = function() { fileDelay.restart(); };
vm.runInContext(extractFrom(paletteSource, "loadModeData"), context);
context.loadModeData();
if (!root.todoPending || !root.clipboardPending || !todoProcess.running || !clipboardProcess.running)
  throw new Error("unified search did not start its two cached sources");
const firstGeneration = [root.todoProcessGeneration, root.clipboardProcessGeneration];
root.modeQuery = "two";
context.loadModeData();
if (root.todoProcessGeneration !== firstGeneration[0] || root.clipboardProcessGeneration !== firstGeneration[1])
  throw new Error("filtering restarted an in-flight source");

// Close invalidates the opening.  A later open gets a fresh pair, while the
// old pending state cannot make a stale completion look current.
root.requestedOpen = false; root.paletteOpen = false;
root.openingGeneration++;
todoProcess.running = false; clipboardProcess.running = false;
root.todoPending = root.clipboardPending = false;
root.todoLoaded = root.clipboardLoaded = false;
root.todoFailed = root.clipboardFailed = false;
root.requestedOpen = true; root.paletteOpen = true; root.modeQuery = "three";
context.loadModeData();
if (root.todoProcessGeneration !== root.openingGeneration ||
    root.clipboardProcessGeneration !== root.openingGeneration)
  throw new Error("reopened palette did not bind requests to its new generation");
console.log("ok");
'''
        completed = subprocess.run(
            ["node", "-e", script, str(PALETTE), str(DATASOURCES)],
            check=True,
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.stdout.strip(), "ok")

    def test_completion_handlers_reject_stale_file_results(self):
        script = r'''
const fs = require("fs");
const vm = require("vm");
// finish* helpers now live in PaletteDataSources.qml. They report through
// loaded()/loadFailed()/reloadRequested() signals; the harness wires loaded
// to the palette rebuildModel() the same way CommandPalette.onLoaded does.
const source = fs.readFileSync(process.argv[2], "utf8");
function extract(name) {
  const start = source.indexOf("function " + name + "(");
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === "{") depth++;
    if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error("missing " + name);
}
const root = {
  openingGeneration: 2, requestedOpen: true, mode: "", modeQuery: "new",
  todoPending: true, todoLoaded: false, todoFailed: false, todos: [],
  clipboardPending: true, clipboardLoaded: false, clipboardFailed: false, clipboardText: "",
  fileGeneration: 2, pendingFileQuery: "new", fileBusy: true, fileError: "",
  fileRows: [{title: "old", kind: "file"}], rebuilt: 0,
  calculatorOnly: false,
  get paletteOpen() { return this.requestedOpen; },
  set paletteOpen(v) { this.requestedOpen = v; },
  isUnifiedSearch() { return this.mode === "" && !!this.modeQuery; },
  shouldSearchFiles() { return this.mode === "file" || (this.isUnifiedSearch() && !this.calculatorOnly); },
  loaded(source) { this.rebuilt++; },
  loadFailed(source, message) { this.failedMessage = message; },
  reloadRequested() { this.reloads = (this.reloads || 0) + 1; },
  rebuildModel() { this.rebuilt++; },
  loadModeData() { this.reloads = (this.reloads || 0) + 1; }
};
const fileDelay = {restarts: 0, restart() { this.restarts++; }};
const fileTimeout = {restarts: 0, stops: 0, restart() { this.restarts++; }, stop() { this.stops++; }};
const context = {root, fileDelay, fileTimeout};
vm.createContext(context);
for (const name of ["isUnifiedSearch", "shouldSearchFiles", "finishTodoLoad", "finishClipboardLoad", "finishFileSearch"])
  vm.runInContext(extract(name), context);
// Exercise the real extracted guards, not hand-copied copies.
root.isUnifiedSearch = context.isUnifiedSearch;
root.shouldSearchFiles = context.shouldSearchFiles;
if (!root.shouldSearchFiles()) throw new Error("unified search should be file-eligible");
root.calculatorOnly = true;
// S-025: an arithmetic-looking unified query ranks the calculator first
// but never suppresses files.
if (!root.shouldSearchFiles()) throw new Error("calculator query must stay file-eligible");
root.calculatorOnly = false;
if (root.shouldSearchFiles() !== true) throw new Error("reset calculatorOnly broke eligibility");

context.finishTodoLoad(0, '[{"task":"fresh"}]', 2);
context.finishClipboardLoad(0, "1\tfresh", 2);
if (!root.todoLoaded || root.todos[0].task !== "fresh" || !root.clipboardLoaded || root.rebuilt !== 2)
  throw new Error("current completions were not applied");

// Query generation mismatch must leave the old result visible only until the
// replacement search is scheduled, never install its stale payload.
root.fileRows = [{title: "old", kind: "file"}]; root.fileBusy = true;
root.modeQuery = "newer"; root.pendingFileQuery = "newer";
context.finishFileSearch(0, '{"results":[{"name":"stale","path":"/stale"}]}', 1, "new");
if (root.fileRows[0].title !== "old" || fileDelay.restarts !== 1)
  throw new Error("stale query file completion was accepted");

root.mode = "ai"; root.fileBusy = true;
context.finishFileSearch(0, '{"results":[{"name":"wrong-mode","path":"/wrong"}]}', 2, "newer");
if (root.fileRows[0].title !== "old") throw new Error("mode-stale file completion was accepted");
root.requestedOpen = false; root.openingGeneration = 3; root.mode = ""; root.modeQuery = "new";
context.finishFileSearch(0, '{"results":[{"name":"closed","path":"/closed"}]}', 2, "new");
if (root.fileRows[0].title !== "old") throw new Error("closed palette accepted a completion");

root.requestedOpen = true; root.fileGeneration = 3; root.pendingFileQuery = "new"; root.fileBusy = true;
context.finishFileSearch(0, '{"results":[{"name":"fresh","path":"/fresh","uri":"file:///fresh"}]}', 3, "new");
if (root.fileRows[0].title !== "fresh") throw new Error("reopened palette rejected current completion");

// A current file completion for an arithmetic-looking unified query
// installs normally: the calculator ranks first but never suppresses
// files (S-025).
root.mode = ""; root.modeQuery = "2+2"; root.calculatorOnly = true;
root.pendingFileQuery = "2+2"; root.fileGeneration = 3;
root.fileRows = [{title: "old", kind: "file"}]; root.fileBusy = true;
context.finishFileSearch(0, '{"results":[{"name":"fresh-calc","path":"/fresh-calc","uri":"file:///fresh-calc"}]}', 3, "2+2");
if (root.fileRows[0].title !== "fresh-calc") throw new Error("calculator query rejected current file completion");
// A generation-stale completion for the same query still reschedules and
// never installs.
root.fileRows = [{title: "old", kind: "file"}]; root.fileBusy = true;
const restartsBeforeStale = fileDelay.restarts;
context.finishFileSearch(0, '{"results":[{"name":"stale-calc","path":"/stale-calc"}]}', 2, "old-query");
if (root.fileRows[0].title !== "old") throw new Error("calculator query installed generation-stale files");
if (fileDelay.restarts !== restartsBeforeStale + 1) throw new Error("generation-stale completion did not reschedule");
console.log("ok");
'''
        completed = subprocess.run(
            ["node", "-e", script, str(PALETTE), str(DATASOURCES)],
            check=True,
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.stdout.strip(), "ok")

    def test_clipboard_ingest_is_capped_at_max_source_rows(self):
        # S-056: cliphist ingest keeps the most recent maxSourceRows
        # lines; short payloads pass through unchanged.
        script = r'''
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
function extract(name) {
  const start = source.indexOf("function " + name + "(");
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === "{") depth++;
    if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error("missing " + name);
}
const root = {
  openingGeneration: 2, requestedOpen: true, paletteOpen: true,
  mode: "", modeQuery: "x", clipboardPending: true, clipboardLoaded: false,
  clipboardFailed: false, clipboardText: "", maxSourceRows: 200,
  isUnifiedSearch() { return this.mode === "" && !!this.modeQuery; },
  loaded() {}, reloadRequested() {},
};
const context = {root};
vm.createContext(context);
vm.runInContext(extract("isUnifiedSearch"), context);
root.isUnifiedSearch = context.isUnifiedSearch;
vm.runInContext(extract("finishClipboardLoad"), context);
const big = Array.from({length: 250}, (_, i) => (i + 1) + "\tvalue " + (i + 1)).join("\n");
context.finishClipboardLoad(0, big, 2);
const kept = root.clipboardText.split("\n");
if (kept.length !== 200) throw new Error("ingest kept " + kept.length + " lines");
if (kept[0] !== "1\tvalue 1" || kept[199] !== "200\tvalue 200")
  throw new Error("ingest did not keep the most recent lines");
context.finishClipboardLoad(0, "1\tfresh", 2);
if (root.clipboardText !== "1\tfresh") throw new Error("short payload changed at ingest");
console.log("ok");
'''
        completed = subprocess.run(
            ["node", "-e", script, str(DATASOURCES)],
            check=True,
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.stdout.strip(), "ok")

    def test_clip_mode_matches_bounded_snippet(self):
        # S-056: clip:/# match a bounded leading snippet per row, not
        # the full value; the payload id still decodes the full entry.
        # The DISPLAY is the same needle-centered 180-char snippet the
        # unified path shows (plainSnippet): the centered window is
        # display-only after the bounded match, never a deeper match.
        script = r'''
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const querySource = fs.readFileSync(process.argv[2], "utf8");
const textSource = fs.readFileSync(process.argv[3], "utf8");
function extract(name) {
  const start = source.indexOf("function " + name + "(");
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === "{") depth++;
    if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error("missing " + name);
}
const resultModel = {
  items: [], clear() { this.items = []; },
  append(item) { this.items.push(item); },
  get count() { return this.items.length; },
};
const late = "x".repeat(500) + " late-needle-zzz";
const early = "x".repeat(10) + " early-needle-zzz";
const longEarly = "x".repeat(10) + " early-needle-zzz" + "y".repeat(500);
const root = {
  mode: "clip", modeQuery: "late-needle-zzz", rows: [],
  calculatorResult: {matched: false, value: "", error: ""},
  selectedIndex: 0, requestedOpen: true,
  maxSourceRows: 200, maxMatchSnippet: 240,
  searchText() { return this.modeQuery; },
};
const dataSources = {clipboardText: "7\t" + late + "\n8\t" + early + "\n9\t" + longEarly,
  fileRows: [], todos: []};
const Query = {};
const PaletteText = {};
const context = {
  root, resultModel, dataSources, Query, PaletteText,
  selectedIndex: 0,
  selector() { return root.mode.length === 1 && ">@%+#!/".includes(root.mode) ? root.mode : ""; },
  searchText() { return root.modeQuery; },
};
vm.createContext(context);
vm.runInContext(querySource, context);
Query.score = context.score;
Query.boundedKeywords = context.boundedKeywords;
Query.prepareRow = context.prepareRow;
vm.runInContext(textSource, context);
PaletteText.plainSnippet = context.plainSnippet;
vm.runInContext(extract("rebuildModel"), context);
// A needle past the 240-char snippet never matches the full value.
context.rebuildModel();
if (root.rows.length !== 0) throw new Error("clip mode matched past the snippet bound");
// A needle inside the snippet still matches, with the entry id kept
// for decode-on-copy and the unified-style centered snippet shown.
root.rows = []; resultModel.items = [];
root.modeQuery = "early-needle-zzz";
context.rebuildModel();
if (root.rows.length !== 2) throw new Error("clip mode missed a snippet match");
const short = root.rows.find(row => row.payload === "8");
const long = root.rows.find(row => row.payload === "9");
if (!short || !long) throw new Error("clipboard copy payload lost its id");
if (short.title.indexOf("early-needle-zzz") < 0)
  throw new Error("clipboard row lost its display value");
if (long.title.length > 180) throw new Error("clipboard row display is not snippet-bounded");
if (long.title.indexOf("early-needle-zzz") < 0)
  throw new Error("clipboard snippet lost the needle");
if (long.title.indexOf("y".repeat(500)) >= 0)
  throw new Error("clipboard row still shows the full value");
// An empty needle still lists rows.
root.rows = []; resultModel.items = [];
root.modeQuery = "";
context.rebuildModel();
if (root.rows.length !== 3) throw new Error("clip mode empty needle listed " + root.rows.length);
console.log("ok");
'''
        completed = subprocess.run(
            ["node", "-e", script, str(PALETTE), str(QUERY), str(TEXT)],
            check=True,
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.stdout.strip(), "ok")

    def test_pending_flags_gate_loading_text_like_process_running(self):
        script = r'''
const fs = require("fs");
const vm = require("vm");
const paletteSource = fs.readFileSync(process.argv[1], "utf8");
const dataSource = fs.readFileSync(process.argv[2], "utf8");
function extractFrom(source, name) {
  const start = source.indexOf("function " + name + "(");
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === "{") depth++;
    if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error("missing " + name);
}
// noMatchText reads the extracted pending flags; ensure*/finish* own them
// exactly like the pre-refactor process.running gates.
const dataSources = {clipboardPending: false, todoPending: false, fileBusy: false,
  fileTruncated: false, fileError: ""};
const root = {mode: "clip", modeQuery: "x", calculatorResult: {matched: false},
  get clipboardPending() { return dataSources.clipboardPending; }};
const context = {root, dataSources};
vm.createContext(context);
vm.runInContext(extractFrom(paletteSource, "noMatchText"), context);
dataSources.clipboardPending = true;
if (context.noMatchText() !== "Loading clipboard…") throw new Error("clip pending did not gate loading text");
dataSources.clipboardPending = false;
if (context.noMatchText() !== "No clipboard matches") throw new Error("clip idle did not gate empty text");
root.mode = "!"; dataSources.todoPending = true;
if (context.noMatchText() !== "Loading TODOs…") throw new Error("todo pending did not gate loading text");
dataSources.todoPending = false;
if (context.noMatchText() !== "No TODOs found") throw new Error("todo idle did not gate empty text");
root.mode = "file"; root.modeQuery = "f"; dataSources.fileBusy = true;
if (context.noMatchText() !== "Searching files…") throw new Error("file busy did not gate loading text");
// ensure* sets pending (process starts), finish* clears it (process ends).
const dsRoot = {todoPending: false, todoLoaded: false, todoFailed: false,
  clipboardPending: false, clipboardLoaded: false, clipboardFailed: false,
  openingGeneration: 1, paletteOpen: true, mode: "!", modeQuery: "x",
  todos: [], clipboardText: "",
  isUnifiedSearch() { return this.mode === "" && !!this.modeQuery; },
  loaded() {}, reloadRequested() {}};
const dsContext = {root: dsRoot, todoProcess: {running: false}, clipboardProcess: {running: false}};
vm.createContext(dsContext);
for (const name of ["ensureTodoData", "ensureClipboardData", "finishTodoLoad", "finishClipboardLoad"])
  vm.runInContext(extractFrom(dataSource, name), dsContext);
dsContext.ensureTodoData();
if (!dsRoot.todoPending || !dsContext.todoProcess.running) throw new Error("ensure did not mark pending/running");
dsContext.finishTodoLoad(0, "[]", 1);
if (dsRoot.todoPending) throw new Error("finish did not clear pending");
console.log("ok");
'''
        completed = subprocess.run(
            ["node", "-e", script, str(PALETTE), str(DATASOURCES)],
            check=True,
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
