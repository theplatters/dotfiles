import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
EXTENSION = (ROOT / ".pi" / "extensions" / "desktop-agent.ts").read_text(encoding="utf-8")
BUN = shutil.which("bun")


@unittest.skipUnless(BUN, "bun is required for executable extension tests")
class JournalExtensionTests(unittest.TestCase):
    def test_strict_tools_prepare_preview_and_safe_append_transport(self):
        bun = BUN
        assert bun is not None
        source = "\n".join(line for line in EXTENSION.splitlines() if not line.startswith("import "))
        source = source.replace("export default function desktopAgent", "function desktopAgent")
        source = source.replace("const JOURNAL_HELPER_TIMEOUT = 10_000;", "const JOURNAL_HELPER_TIMEOUT = 20;")
        script = r'''
import { EventEmitter } from "node:events";
import { dirname as pathDirname, join, isAbsolute, resolve, normalize, relative } from "node:path";
const dirname = pathDirname;
const fileURLToPath = () => "/repo/.pi/extensions/desktop-agent.ts";
const existsSync = () => true;
const realpathSync = { native: (value) => value };
const safeScope = "/journal-scope", safeSession = safeScope + "/safe.jsonl";
const foreignPaths = ["/project-scope/project.jsonl", "/palette.jsonl", "/other-graph/journal.jsonl"];
const SessionManager = {list: async () => [
  {path: safeSession, name: "safe", firstMessage: "safe"},
  ...foreignPaths.map(path => ({path, name: "foreign", firstMessage: "foreign"}))
]};
const Type = { Object: (value) => value, String: () => ({}), Optional: (value) => value };
const tools = {}, hooks = {}, commands = {}, calls = [], previews = [];
let approve = true, mode = "normal", waitingForConfirmation = false, releaseConfirmation;
const lstatSync = (path) => ({
  isSymbolicLink: () => false,
  isDirectory: () => path === safeScope,
  isFile: () => path.endsWith(".jsonl"),
  mode: path === safeScope ? 0o40700 : 0o100600,
});
function spawn(command, args) {
  const child = new EventEmitter();
  child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  child.stdin = new EventEmitter();
  child.stdin.write = (value) => { child.payload = JSON.parse(value); };
  child.stdin.end = () => {};
  child.kill = () => { child.killed = true; };
  calls.push({command, args, child});
  queueMicrotask(() => {
    child.emit("spawn");
    const op = args.at(-1);
    if (mode === "timeout") return;
    if ((mode === "prepare-stale" && op === "prepare") || (mode === "append-stale" && op === "append")) {
      child.stderr.emit("data", Buffer.from("stale revision")); child.emit("close", 1, null); return;
    }
    let value;
    if (op === "context") value = {graphName: "Notes", date: today, path: `journals/${today}.md`, revision: "r", content: "", pageNames: [], recentJournals: [], matches: [], truncated: false};
    else if (op === "prepare") value = {date: today, path: `journals/${today}.md`, revision: "r", addition: "- exact €\n"};
    else value = {date: today, path: `journals/${today}.md`, revision: "new", line: 1};
    const bytes = Buffer.from(JSON.stringify(value));
    child.stdout.emit("data", bytes.subarray(0, 4)); child.stdout.emit("data", bytes.subarray(4));
    child.emit("close", 0, null);
  });
  return child;
}
    const today = `${new Date().getFullYear()}_${String(new Date().getMonth()+1).padStart(2,"0")}_${String(new Date().getDate()).padStart(2,"0")}`;
''' + source + r'''
const pi = { on(name, callback) { hooks[name] = callback; }, registerTool(tool) { tools[tool.name] = tool; }, registerCommand(name, command) { commands[name] = command; } };
process.env.QS_JOURNAL_MODE = "1"; process.env.QS_PROJECT_PATH = ""; process.env.LOGSEQ_GRAPH = "/graph";
process.env.PI_CODING_AGENT_SESSION_DIR = safeScope; process.env.QS_JOURNAL_SESSION_SCOPE = safeScope;
desktopAgent(pi);
const ctx = {cwd: "/work", hasUI: true, ui: {confirm: async (_title, message) => {
  previews.push(message);
  if (waitingForConfirmation) return await new Promise(resolve => { releaseConfirmation = () => resolve(approve); });
  return approve;
}}};
const noUi = {cwd: "/work", hasUI: false, ui: ctx.ui};
const assert = (value, message) => { if (!value) throw new Error(message); };
assert(Object.keys(tools).join(",") === "logseq_journal_context,logseq_journal_append", "journal mode exposed extra tools");
const blocked = await hooks.tool_call({toolName: "bash", input: {command: "touch note"}}, ctx);
assert(blocked?.block === true, "generic tool escaped journal allowlist");
assert(await hooks.session_before_switch({reason: "new"}) === undefined, "new session hook was rejected");
for (const path of foreignPaths) {
  const result = await hooks.session_before_switch({reason: "resume", targetSessionFile: path});
  assert(result?.cancel === true, "foreign session escaped scope: " + path);
}
assert(await hooks.session_before_switch({reason: "resume", targetSessionFile: safeSession}) === undefined, "safe session was rejected");
let picked;
await commands["desktop-sessions"].handler([], {cwd: "/work", hasUI: true, waitForIdle: async () => {},
  ui: {select: async (_title, options) => { assert(options.length === 1 && options[0].includes(safeSession), "picker exposed foreign session"); return options[0]; }, notify() {}},
  switchSession: async path => { picked = path; return {}; }});
assert(picked === safeSession, "picker switched outside journal scope");
const context = await tools.logseq_journal_context.execute("id", {}, undefined, undefined, ctx);
assert(context.content[0].text.includes("Notes"), "context tool did not return helper value");
const beforeNoUi = calls.length;
try { await tools.logseq_journal_append.execute("id", {date: today, revision: "r", text: "x"}, undefined, undefined, noUi); throw new Error("no-ui append succeeded"); }
catch (error) { assert(String(error).includes("UI confirmation"), "no-ui denial missing"); }
assert(calls.length === beforeNoUi, "no-ui append spawned a helper");
const aborted = new AbortController(); aborted.abort();
try { await tools.logseq_journal_append.execute("id", {date: today, revision: "r", text: "x"}, aborted.signal, undefined, ctx); throw new Error("abort succeeded"); }
catch (error) { assert(String(error).includes("aborted"), "abort denial missing"); }
approve = false;
try { await tools.logseq_journal_append.execute("id", {date: today, revision: "r", text: "x"}, undefined, undefined, ctx); throw new Error("denial succeeded"); }
catch (error) { assert(String(error).includes("denied"), "approval denial missing"); }
assert(calls.at(-1).args.at(-1) === "prepare", "denial performed a write");
approve = true;
const appended = await tools.logseq_journal_append.execute("id", {date: today, revision: "r", text: "x"}, undefined, undefined, ctx);
assert(appended.content[0].text.includes('"line": 1'), "append response missing");
assert(calls.at(-1).args.at(-1) === "append", "approved append did not write");
assert(previews.at(-1).includes("Destination: journals/" + today + ".md") && previews.at(-1).includes("- exact €\n"), "preview was not exact");
assert(JSON.stringify(calls.at(-1).child.payload) === JSON.stringify({date: today, revision: "r", addition: "- exact €\n"}), "append payload was not exactly prepared");
waitingForConfirmation = true;
const duringConfirm = new AbortController();
const pending = tools.logseq_journal_append.execute("id", {date: today, revision: "r", text: "x"}, duringConfirm.signal, undefined, ctx);
await new Promise(resolve => setTimeout(resolve, 5));
duringConfirm.abort(); if (releaseConfirmation) releaseConfirmation();
try { await pending; throw new Error("abort during confirmation succeeded"); }
catch (error) { assert(String(error).includes("aborted"), "abort during confirmation missing"); }
waitingForConfirmation = false;
const beforeStalePrepare = calls.length; mode = "prepare-stale";
try { await tools.logseq_journal_append.execute("id", {date: today, revision: "r", text: "x"}, undefined, undefined, ctx); throw new Error("stale prepare succeeded"); }
catch (error) { assert(String(error).includes("stale revision"), "stale prepare was not propagated"); }
assert(calls.length === beforeStalePrepare + 1 && calls.at(-1).args.at(-1) === "prepare", "stale prepare requested a write");
mode = "append-stale";
try { await tools.logseq_journal_append.execute("id", {date: today, revision: "r", text: "x"}, undefined, undefined, ctx); throw new Error("stale append succeeded"); }
catch (error) { assert(String(error).includes("stale revision"), "stale append was not propagated"); }
mode = "timeout";
try { await tools.logseq_journal_context.execute("id", {}, undefined, undefined, ctx); throw new Error("timeout succeeded"); }
catch (error) { assert(String(error).includes("timed out"), "transport timeout was not rejected"); }
console.log(JSON.stringify({ok: true}));
'''
        with tempfile.NamedTemporaryFile("w", suffix=".ts", encoding="utf-8") as handle:
            handle.write(script)
            handle.flush()
            completed = subprocess.run([bun, handle.name], text=True,
                                       capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertEqual(json.loads(completed.stdout.splitlines()[-1])["ok"], True)

    def test_palette_helper_abort_kills_and_rejects_without_journal_scope(self):
        bun = BUN
        assert bun is not None
        source = "\n".join(line for line in EXTENSION.splitlines() if not line.startswith("import "))
        source = source.replace("export default function desktopAgent", "function desktopAgent")
        script = r'''
import { EventEmitter } from "node:events";
import { dirname as pathDirname, join, isAbsolute, resolve, normalize } from "node:path";
const dirname = pathDirname, fileURLToPath = () => "/repo/.pi/extensions/desktop-agent.ts";
const existsSync = () => true, realpathSync = {native: value => value};
const Type = {Object: value => value, String: () => ({}), Optional: value => value};
let childRef;
function spawn() {
  const child = new EventEmitter(); childRef = child;
  child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  child.kill = () => { child.killed = true; queueMicrotask(() => child.emit("close", null, "SIGTERM")); };
  queueMicrotask(() => child.emit("spawn")); return child;
}
process.env.QS_JOURNAL_MODE = ""; process.env.QS_PROJECT_PATH = ""; process.env.LOGSEQ_GRAPH = "/graph";
''' + source + r'''
const controller = new AbortController();
const pending = helper({cwd: "/work"}, ["search", "--", "needle"], controller.signal);
controller.abort();
try { await pending; throw new Error("palette helper abort resolved"); }
catch (error) {
  if (!String(error).includes("aborted")) throw error;
  if (!childRef?.killed) throw new Error("palette helper was not killed");
}
console.log(JSON.stringify({ok: true}));
'''
        with tempfile.NamedTemporaryFile("w", suffix=".ts", encoding="utf-8") as handle:
            handle.write(script)
            handle.flush()
            completed = subprocess.run([bun, handle.name], text=True,
                                       capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertEqual(json.loads(completed.stdout.splitlines()[-1])["ok"], True)


if __name__ == "__main__":
    unittest.main()
