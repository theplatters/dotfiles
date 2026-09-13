import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
HELPER = ROOT / "scripts" / "project_planner.py"
SESSION_WRAPPER = ROOT / "scripts" / "project_sessions.py"
EXTENSION = (ROOT / ".pi" / "extensions" / "desktop-agent.ts").read_text(encoding="utf-8")
BUN = shutil.which("bun")


class ProjectHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.graph = Path(self.temp.name) / "Notes"
        (self.graph / "pages").mkdir(parents=True)
        (self.graph / "journals").mkdir()
        self.page = self.graph / "pages" / "Project.md"
        self.page.write_text("- TODO ship it\n- DONE already\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def run_helper(self, command, payload, check=False):
        return subprocess.run(
            [sys.executable, str(HELPER), "--graph", str(self.graph), command],
            input=json.dumps(payload), text=True, capture_output=True, timeout=3,
            check=check,
        )

    def test_page_returns_revision_content_and_todos(self):
        response = self.run_helper("page", {"path": "pages/Project.md"}, True)
        value = json.loads(response.stdout)
        self.assertEqual(value["path"], "pages/Project.md")
        self.assertEqual(value["page"], "Project")
        self.assertEqual(value["graphName"], "Notes")
        self.assertEqual(value["revision"], hashlib.sha256(self.page.read_bytes()).hexdigest())
        self.assertEqual(value["content"], self.page.read_text())
        self.assertEqual(value["todos"][0]["task"], "ship it")

    def test_update_is_atomic_and_rejects_stale_revision_without_writing(self):
        revision = hashlib.sha256(self.page.read_bytes()).hexdigest()
        changed = "- TODO ship it\n- TODO new progress\n"
        response = self.run_helper("update", {
            "path": "pages/Project.md", "revision": revision, "content": changed,
        }, True)
        value = json.loads(response.stdout)
        self.assertEqual(value["content"], changed)
        self.assertEqual(self.page.read_text(), changed)
        stale = self.run_helper("update", {
            "path": "pages/Project.md", "revision": revision, "content": "must not write\n",
        })
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("stale", stale.stderr)
        self.assertEqual(self.page.read_text(), changed)

    def test_page_and_request_bounds_are_enforced(self):
        self.page.write_bytes(b"x" * (128 * 1024 + 1))
        oversized_page = self.run_helper("page", {"path": "pages/Project.md"})
        self.assertNotEqual(oversized_page.returncode, 0)
        self.assertIn("128 KiB", oversized_page.stderr)
        too_large = self.run_helper("page", {"path": "pages/Project.md"}, False)
        self.assertLessEqual(len(too_large.stderr.encode()), 1024 * 1024)


class ProjectPolicyContractTests(unittest.TestCase):
    def test_scope_gate_allows_only_project_tools(self):
        self.assertIn('if (projectMode() && !["logseq_project_read", "logseq_project_update", "logseq_project_files", "logseq_project_read_file", "logseq_project_git"].includes(event.toolName))', EXTENSION)
        self.assertIn('return { block: true, reason: "Project mode permits only the constrained project tools" }', EXTENSION)

    def test_approval_denial_and_exact_preview_happen_before_write(self):
        self.assertIn('if (!ctx.hasUI) throw new Error("project update denied: UI confirmation unavailable")', EXTENSION)
        self.assertIn('const preview = `Selected page: ${current.path}\\nRevision: ${p.revision}\\n\\nExact replacement content:\\n${p.content}`;', EXTENSION)
        approval = EXTENSION.index('if (!await ask(ctx, "Approve selected project page update", preview))')
        write = EXTENSION.index('projectHelper(ctx, "update"', approval)
        self.assertLess(approval, write)
        self.assertIn('throw new Error("project update denied by user")', EXTENSION[approval:write])

    def test_bounded_request_and_stale_errors_are_exposed(self):
        self.assertIn("const PROJECT_MAX_INPUT = 1024 * 1024", EXTENSION)
        self.assertIn("const PROJECT_MAX_OUTPUT = 1024 * 1024", EXTENSION)
        self.assertIn("const PROJECT_MAX_PAGE_BYTES = 128 * 1024", EXTENSION)
        self.assertIn('JSON.stringify(updatePayload)', EXTENSION)
        self.assertIn('project helper output exceeded ${PROJECT_MAX_OUTPUT} bytes', EXTENSION)
        self.assertIn('stale revision; read the page again and request a new approval', EXTENSION)

    def test_project_path_is_environment_only_and_start_has_no_implicit_prompt(self):
        self.assertIn('environment["QS_PROJECT_SESSION_SCOPE"]', (ROOT / "scripts" / "project_sessions.py").read_text(encoding="utf-8"))
        self.assertIn('return { path: process.env.QS_PROJECT_PATH, ...fields }', EXTENSION)
        self.assertNotIn("p.path", EXTENSION[EXTENSION.index("logseq_project_read"):])

    def test_project_folder_tools_are_pinned_and_read_only(self):
        self.assertIn('projectHelper(ctx, "files-list", projectPayload({})', EXTENSION)
        self.assertIn('projectHelper(ctx, "files-read", filePayload', EXTENSION)
        self.assertIn('projectHelper(ctx, "files-git", projectPayload({})', EXTENSION)
        # Model supplies only a folder-relative file; page/root stay pinned.
        self.assertIn('const filePayload = projectPayload({ file: p.file })', EXTENSION)
        self.assertIn('"files-list" | "files-read" | "files-git"', EXTENSION)
        self.assertIn("PROJECT_MAX_FILE_BYTES", EXTENSION)
        self.assertIn("PROJECT_MAX_DIFF_BYTES", EXTENSION)
        self.assertIn("project_files.py", EXTENSION)
        helper = (ROOT / "scripts" / "project_files.py").read_text(encoding="utf-8")
        self.assertIn("shell=False", helper)
        self.assertIn("--no-ext-diff", helper)
        self.assertIn("--no-textconv", helper)
        self.assertIn("--no-renames", helper)
        self.assertIn("--ignore-submodules=all", helper)
        self.assertIn(":(literal)", helper)
        self.assertIn(":(exclude,literal)", helper)
        self.assertIn("GIT_NO_LAZY_FETCH", helper)
        self.assertIn("start_new_session=True", helper)
        self.assertIn("refusing", helper)
        self.assertIn("operation aborted", helper)
        self.assertIn("DIR_VISIT_LIMIT", helper)
        self.assertIn("O_NOFOLLOW", helper)
        self.assertIn("O_DIRECTORY", helper)

    def test_project_session_picker_and_switch_hook_are_scope_checked(self):
        self.assertIn('const directory = projectMode() ? projectSessionDir()', EXTENSION)
        self.assertIn('listed.filter((session) => validProjectSession(session?.path, directory!))', EXTENSION)
        self.assertIn('pi.on("session_before_switch"', EXTENSION)
        self.assertIn('return { cancel: true }', EXTENSION)
        self.assertIn('SessionManager.list(ctx.cwd, directory)', EXTENSION)
        self.assertIn('String(s.firstMessage ?? "").trim() || "Untitled"', EXTENSION)

    @unittest.skipUnless(BUN, "bun is required for executable extension tests")
    def test_extension_runtime_scope_approval_abort_stale_and_utf8_transport(self):
        """Execute the TypeScript extension against a small mocked Pi API.

        This deliberately does not invoke a model. The child-process mock emits
        JSON and diagnostics split inside UTF-8 code points, exercising the
        actual registration, approval, abort, stale-revision, and transport
        paths rather than only checking their source text.
        """
        bun = BUN
        assert bun is not None
        source = "\n".join(line for line in EXTENSION.splitlines()
                              if not line.startswith("import "))
        source = source.replace("export default function desktopAgent", "function desktopAgent")
        script = r'''
import { EventEmitter } from "node:events";
import { dirname as pathDirname, isAbsolute, join, normalize, resolve } from "node:path";

const dirname = pathDirname;
const fileURLToPath = () => "/repo/.pi/extensions/desktop-agent.ts";
const existsSync = () => true;
const realpathSync = { native: (value) => value };
const SessionManager = {};
const Type = { Object: (value) => value, String: () => ({}), Optional: (value) => value };
const tools = {};
const hooks = {};
const calls = [];
const confirmations = [];
let approve = true;
let failUpdate = false;
let failStdin = false;
let gitNonRepo = false;

function splitUtf8(child, value, needle) {
    const bytes = Buffer.from(value);
    const marker = Buffer.from(needle);
    const at = bytes.indexOf(marker);
    child.stdout.emit("data", bytes.subarray(0, at + 1));
    child.stdout.emit("data", bytes.subarray(at + 1));
}

function splitStderr(child, value, needle) {
    const bytes = Buffer.from(value);
    const marker = Buffer.from(needle);
    const at = bytes.indexOf(marker);
    child.stderr.emit("data", bytes.subarray(0, at + 1));
    child.stderr.emit("data", bytes.subarray(at + 1));
}

function spawn(command, args) {
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    child.stdin = new EventEmitter();
    child.stdin.write = () => {};
    child.stdin.end = () => {};
    child.kill = () => { child.killed = true; };
    calls.push({ command, args });
    queueMicrotask(() => {
        child.emit("spawn");
        if (failStdin) {
            child.stdin.emit("error", new Error("EPIPE"));
            return;
        }
        const operation = args[args.length - 1];
        if (operation === "page") {
            splitUtf8(child, JSON.stringify({path: "pages/Project.md", page: "Project",
                graphName: "Notes", revision: "fresh", content: "before € after", todos: []}), "€");
            child.emit("close", 0, null);
        } else if (operation === "files-list") {
            splitUtf8(child, JSON.stringify({path: "pages/Project.md", page: "Project",
                graphName: "Notes", root: "/repo", entries: [{path: "a.txt", size: 1}], truncated: false}), "a");
            child.emit("close", 0, null);
        } else if (operation === "files-read") {
            splitUtf8(child, JSON.stringify({path: "pages/Project.md", page: "Project",
                graphName: "Notes", root: "/repo", file: "a.txt", size: 1, content: "hi €"}), "€");
            child.emit("close", 0, null);
        } else if (operation === "files-git") {
            if (gitNonRepo) {
                child.stdout.emit("data", Buffer.from(JSON.stringify({path: "pages/Project.md", page: "Project",
                    graphName: "Notes", root: "/repo", isRepo: false, unborn: false, head: null, lastTouching: null,
                    toplevel: null, prefix: "", changes: [], untracked: [], truncated: false,
                    diff: "", diffTruncated: false, message: "not a git repository"})));
            } else {
                splitUtf8(child, JSON.stringify({path: "pages/Project.md", page: "Project",
                    graphName: "Notes", root: "/repo", isRepo: true, unborn: false, head: null,
                    changes: [], untracked: [], diff: "diff €", diffTruncated: false, truncated: false}), "€");
            }
            child.emit("close", 0, null);
        } else if (failUpdate) {
            splitStderr(child, "diagnostic é€ failure", "€");
            child.emit("close", 1, null);
        } else {
            splitUtf8(child, JSON.stringify({path: "pages/Project.md", page: "Project",
                graphName: "Notes", revision: "new", content: "replacement €", todos: []}), "€");
            child.emit("close", 0, null);
        }
    });
    return child;
}

''' + source + r'''

const pi = {
    on(name, callback) { hooks[name] = callback; },
    registerTool(tool) { tools[tool.name] = tool; },
    registerCommand() {},
};
process.env.LOGSEQ_GRAPH = "/graph";
process.env.QS_PROJECT_PATH = "pages/Project.md";
desktopAgent(pi);
const ctx = { cwd: "/work", hasUI: true,
    ui: { confirm: async (_title, message) => { confirmations.push(message); return approve; } } };
const noUi = { cwd: "/work", hasUI: false, ui: ctx.ui };
const assert = (value, message) => { if (!value) throw new Error(message); };
const update = () => tools.logseq_project_update.execute("id", {
    revision: "fresh", content: "replacement €" }, undefined, undefined, ctx);

const gate = await hooks.tool_call({ toolName: "bash", input: { command: "cat escape" } }, ctx);
assert(gate?.block === true, "scoped shell was not blocked");
assert(Object.keys(tools).join(",") === "logseq_project_read,logseq_project_update,logseq_project_files,logseq_project_read_file,logseq_project_git",
       "scoped registration exposed unrelated tools");
const read = await tools.logseq_project_read.execute("id", {}, undefined, undefined, ctx);
assert(read.content[0].text.includes("before € after"), "split UTF-8 stdout was corrupted");
const listed = await tools.logseq_project_files.execute("id", {}, undefined, undefined, ctx);
assert(listed.content[0].text.includes("a.txt"), "project files listing failed");
const fileRead = await tools.logseq_project_read_file.execute("id", { file: "a.txt" }, undefined, undefined, ctx);
assert(fileRead.content[0].text.includes("hi €"), "project file read failed");
assert(calls[calls.length - 1].args.at(-1) === "files-read", "file read used wrong helper command");
const gitInfo = await tools.logseq_project_git.execute("id", {}, undefined, undefined, ctx);
assert(gitInfo.content[0].text.includes("diff"), "project git tool failed");
gitNonRepo = true;
const gitEmpty = await tools.logseq_project_git.execute("id", {}, undefined, undefined, ctx);
assert(gitEmpty.content[0].text.includes("not a git repository"), "non-repo git schema was rejected");
gitNonRepo = false;
const fileGate = await hooks.tool_call({ toolName: "logseq_project_read_file", input: { file: "a.txt" } }, ctx);
assert(!fileGate?.block, "pinned project file tool was blocked");
const traversalGate = await hooks.tool_call({ toolName: "read", input: { path: "/tmp/x" } }, ctx);
assert(traversalGate?.block === true, "generic read was not blocked in project mode");

const beforeNoUi = calls.length;
try { await tools.logseq_project_update.execute("id", { revision: "fresh", content: "x" }, undefined, undefined, noUi); throw new Error("no-UI update succeeded"); }
catch (error) { assert(String(error).includes("UI confirmation"), "no-UI denial missing"); }
assert(calls.length === beforeNoUi, "no-UI denial spawned a writer");

const aborted = new AbortController();
aborted.abort();
try { await tools.logseq_project_update.execute("id", { revision: "fresh", content: "x" }, aborted.signal, undefined, ctx); throw new Error("aborted update succeeded"); }
catch (error) { assert(String(error).includes("aborted"), "abort error missing"); }

const beforeStale = calls.length;
try { await tools.logseq_project_update.execute("id", { revision: "stale", content: "x" }, undefined, undefined, ctx); throw new Error("stale update succeeded"); }
catch (error) { assert(String(error).includes("stale revision"), "stale error missing"); }
assert(calls.length === beforeStale + 1, "stale update did not perform only a fresh read");
assert(confirmations.length === 0, "stale update requested approval");

approve = false;
try { await update(); throw new Error("denied update succeeded"); }
catch (error) { assert(String(error).includes("denied"), "approval denial missing"); }
assert(calls[calls.length - 1].args.at(-1) === "page", "denial invoked update helper");

approve = true;
failStdin = true;
try { await update(); throw new Error("stdin error update succeeded"); }
catch (error) { assert(String(error).includes("stdin failed") && String(error).includes("EPIPE"), "stdin error was not returned safely"); }
failStdin = false;

failUpdate = true;
try { await update(); throw new Error("diagnostic update succeeded"); }
catch (error) { assert(String(error).includes("diagnostic é€ failure"), "split UTF-8 stderr was corrupted"); }
assert(confirmations.at(-1).includes("Exact replacement content:\nreplacement €"), "preview was not exact");

failUpdate = false;
const approved = await update();
assert(approved.content[0].text.includes("replacement €"), "approved update did not return full response");

const beforeBounds = calls.length;
try { await tools.logseq_project_update.execute("id", { revision: "fresh", content: "€".repeat(131072) }, undefined, undefined, ctx); throw new Error("oversized content succeeded"); }
catch (error) { assert(String(error).includes("128 KiB"), "byte content bound missing"); }
assert(calls.length === beforeBounds, "oversized content reached helper");

process.env.QS_PROJECT_PATH = "x".repeat(1024 * 1024);
try { await tools.logseq_project_update.execute("id", { revision: "fresh", content: "x" }, undefined, undefined, ctx); throw new Error("oversized serialized request succeeded"); }
catch (error) { assert(String(error).includes("1 MiB"), "serialized request bound missing"); }
assert(calls.length === beforeBounds, "oversized serialized request reached helper");
console.log(JSON.stringify({ ok: true, confirmations: confirmations.length }));
'''
        with tempfile.NamedTemporaryFile("w", suffix=".ts", encoding="utf-8") as handle:
            handle.write(script)
            handle.flush()
            completed = subprocess.run([bun, handle.name], text=True,
                                       capture_output=True, timeout=8)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertEqual(json.loads(completed.stdout.splitlines()[-1])["ok"], True)

    @unittest.skipUnless(shutil.which("pi"), "pi is not installed")
    def test_installed_pi_loads_scoped_extension_without_a_prompt(self):
        # This is a control-only RPC smoke test: it never sends a prompt, so it
        # cannot contact a provider or write the fixture page.
        with tempfile.TemporaryDirectory() as sessions:
            graph = Path(sessions) / "graph"
            (graph / "pages").mkdir(parents=True)
            (graph / "journals").mkdir()
            page = graph / "pages" / "Project.md"
            page.write_text("- TODO keep this fixture unchanged\n", encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(SESSION_WRAPPER), "--project", "pages/Project.md"],
                cwd=ROOT,
                env=dict(os.environ, LOGSEQ_GRAPH=str(graph),
                          QS_PROJECT_PATH="pages/Project.md",
                          PI_CODING_AGENT_SESSION_DIR=sessions, PI_OFFLINE="1"),
                input=json.dumps({"type": "get_commands", "id": "project-test"}) + "\n",
                text=True, capture_output=True, timeout=8,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
            matches = [item for item in responses
                       if item.get("id") == "project-test" and item.get("success")]
            self.assertEqual(len(matches), 1, completed.stdout)
            names = {item["name"] for item in matches[0]["data"]["commands"]}
            self.assertIn("desktop-sessions", names)
            scoped = Path(sessions) / "projects"
            self.assertTrue(scoped.exists())
            self.assertTrue(any(path.is_dir() for path in scoped.iterdir()))
            self.assertEqual(page.read_text(encoding="utf-8"), "- TODO keep this fixture unchanged\n")


if __name__ == "__main__":
    unittest.main()
