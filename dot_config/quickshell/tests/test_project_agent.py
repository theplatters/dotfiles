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
        self.assertIn('if (projectMode() && !["logseq_project_read", "logseq_project_update", "logseq_project_files", "logseq_project_read_file", "logseq_project_git", "project_folder_list", "project_folder_read", "project_folder_write", "logseq_agenda_list", "logseq_agenda_add", "zotero_search", "zotero_item", "zotero_read_pdf", "zotero_collections", "zotero_prepare", "zotero_apply"].includes(event.toolName))', EXTENSION)
        self.assertIn('return { block: true, reason: "Project mode permits only the constrained project tools" }', EXTENSION)
        # Folder tools are project-mode only: palette/journal callers are blocked.
        self.assertIn('if (event.toolName.startsWith("project_folder_") && !projectMode())', EXTENSION)
        self.assertIn('Project folder tools are available only in project mode', EXTENSION)

    def test_palette_creation_tools_are_registered_palette_only(self):
        palette = EXTENSION.index('if (!projectMode() && !journalMode())')
        create_project = EXTENSION.index('name: "create_project"')
        create_page = EXTENSION.index('name: "create_logseq_page"')
        self.assertLess(palette, create_project)
        self.assertLess(palette, create_page)
        # Both live in the first palette block (search/todos/append), before
        # the agenda block that follows it.
        agenda = EXTENSION.index('name: "logseq_agenda_list"')
        self.assertLess(create_project, agenda)
        self.assertLess(create_page, agenda)
        # Schemas carry the fixed parameter names.
        project_schema = EXTENSION[EXTENSION.index('const createProjectSchema'):
                                   EXTENSION.index('const createLogseqPageSchema')]
        for param in ("name", "logseq_page", "project_folder", "github_url", "create_folder"):
            self.assertIn(param, project_schema)
        page_schema = EXTENSION[EXTENSION.index('const createLogseqPageSchema'):
                                EXTENSION.index('const createLogseqPageSchema') + 2000]
        for param in ("name", "template", "template_page", "properties"):
            self.assertIn(param, page_schema)

    def test_create_logseq_page_prepare_preview_commit(self):
        self.assertIn('projectHelper(ctx, "create-page"', EXTENSION)
        self.assertIn('{ stage: "prepare"', EXTENSION)
        self.assertIn('{ stage: "commit"', EXTENSION)
        self.assertIn('expected_sha256', EXTENSION)
        self.assertIn('Target: ${target}', EXTENSION)
        self.assertIn('Exact content:', EXTENSION)
        self.assertIn('if (!await ask(ctx, "Approve Logseq page creation", preview))', EXTENSION)
        self.assertIn('throw new Error("logseq page creation denied by user")', EXTENSION)
        self.assertIn('if (!ctx.hasUI) throw new Error("logseq page creation denied: UI confirmation unavailable")', EXTENSION)

    def test_create_project_preflight_confirm_commit(self):
        self.assertIn('PROJECTS_LIST_HELPER, "create"', EXTENSION)
        self.assertIn('"create-dir-preflight"', EXTENSION)
        self.assertIn('"create-dir"', EXTENSION)
        self.assertIn('expected_parent_dev', EXTENSION)
        self.assertIn('expected_parent_ino', EXTENSION)
        self.assertIn('Registry entry (projects.toml)', EXTENSION)
        self.assertIn('Folder: create ', EXTENSION)
        self.assertIn('already exists (no action)', EXTENSION)
        self.assertIn('create_folder requires project_folder', EXTENSION)
        self.assertIn('const createFolder = createRaw === true', EXTENSION)
        self.assertIn('if (!await ask(ctx, "Approve project creation", preview))', EXTENSION)
        self.assertIn('throw new Error("project creation denied by user")', EXTENSION)
        self.assertIn('if (!ctx.hasUI) throw new Error("project creation denied: UI confirmation unavailable")', EXTENSION)

    def test_creation_tool_backend_contracts_agree(self):
        # FIX 1: bare "a/b" maps to pages/a___b.md in the tool, exactly
        # like the backend `_normalize_create_target`.
        self.assertIn('split("/").join("___")', EXTENSION)
        self.assertNotIn('return `pages/${base}.md`', EXTENSION)
        # FIX 3: properties bounds mirror the backend
        # `_validate_create_properties` (16 entries / 64-char keys /
        # 512-char values) so bad input fails before any approval.
        self.assertIn('entries.length > 16', EXTENSION)
        self.assertNotIn('entries.length > 64', EXTENSION)
        self.assertIn('Buffer.byteLength(key, "utf8") > 64', EXTENSION)
        self.assertIn('Buffer.byteLength(value, "utf8") > 512', EXTENSION)
        planner = (ROOT / "scripts" / "project_planner.py").read_text(
            encoding="utf-8")
        self.assertIn('_CREATE_PROPERTIES_LIMIT = 16', planner)
        self.assertIn('at most 64 chars', planner)
        self.assertIn('at most 512 chars', planner)
        # FIX 5: the commit binds the prepared target end to end.
        self.assertIn('expected_target: target', EXTENSION)
        self.assertIn('expected_target', planner)
        self.assertIn('target changed, re-run prepare', planner)
        # FIX 8: a malformed create-dir commit response fails the tool
        # before any registry write.
        self.assertIn('made.created !== true && made.exists !== true',
                      EXTENSION)
        self.assertIn('project helper returned an invalid create-dir result',
                      EXTENSION)

    def test_creation_tools_stay_out_of_scoped_allowlists(self):
        gate = EXTENSION[EXTENSION.index('if (projectMode() && !['):
                         EXTENSION.index('Project mode permits only the constrained project tools')]
        self.assertNotIn('create_project', gate)
        self.assertNotIn('create_logseq_page', gate)
        self.assertNotIn('create-page', gate)
        reads = EXTENSION[EXTENSION.index('const DESKTOP_READ_TOOLS'):
                          EXTENSION.index(';', EXTENSION.index('const DESKTOP_READ_TOOLS'))]
        self.assertNotIn('create_project', reads)
        self.assertNotIn('create_logseq_page', reads)
        self.assertIn('const DESKTOP_READ_TOOLS = ["desktop_current_context", "desktop_project_todos", "desktop_project_logseq_context", "desktop_project_activity", "desktop_current_session", "desktop_search_activity", "desktop_get_session", "desktop_resume_plan", "session_search"]', EXTENSION)
        # Registered (palette-only) yet denied in scoped modes: unlisted tools
        # already fail closed at the tool_call gate.
        self.assertIn('name: "create_project"', EXTENSION)
        self.assertIn('name: "create_logseq_page"', EXTENSION)

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
        # Registry-linked folder tools (list/read/write) are pinned by
        # QS_PROJECT_ID with no graph and no file:: fallback.
        self.assertIn('projectFolderHelper(ctx, "list", projectIdPayload({})', EXTENSION)
        self.assertIn('projectFolderHelper(ctx, "read"', EXTENSION)
        self.assertIn('projectFolderHelper(ctx, "preflight"', EXTENSION)
        self.assertIn('projectFolderHelper(ctx, "write"', EXTENSION)
        self.assertIn('name: "project_folder_list"', EXTENSION)
        self.assertIn('name: "project_folder_read"', EXTENSION)
        self.assertIn('name: "project_folder_write"', EXTENSION)
        self.assertIn("project_folder.py", EXTENSION)
        self.assertIn("PROJECT_FOLDER_HELPER", EXTENSION)
        self.assertIn("registry-linked", EXTENSION)
        self.assertIn('if (!ctx.hasUI) throw new Error("project folder write denied: UI confirmation unavailable")', EXTENSION)
        self.assertIn('if (!await ask(ctx, "Approve linked folder file write", preview))', EXTENSION)
        self.assertIn('throw new Error("project folder write denied by user")', EXTENSION)
        self.assertIn("stale revision; read the file again", EXTENSION)
        self.assertIn("revision is required to update an existing file", EXTENSION)
        # Approval is bound to the inspected destination identity.
        self.assertIn("expected_root", EXTENSION)
        self.assertIn("Destination:", EXTENSION)
        folder_helper = (ROOT / "scripts" / "project_folder.py").read_text(encoding="utf-8")
        self.assertIn("project folder changed; read the file again", folder_helper)
        self.assertIn("O_NOFOLLOW", folder_helper)
        self.assertIn("O_DIRECTORY", folder_helper)
        self.assertIn("O_EXCL", folder_helper)
        self.assertIn("lookup_local_folder", folder_helper)
        self.assertIn("preflight", folder_helper)
        self.assertIn("_FolderLock", folder_helper)
        self.assertIn("project folder is busy", folder_helper)
        self.assertNotIn("subprocess", folder_helper)
        self.assertNotIn("shell=False", folder_helper)
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

    def test_trusted_helpers_and_registry_are_protected(self):
        helper = (ROOT / "scripts" / "project_files.py").read_text(encoding="utf-8")
        for name in ("projects.py", "zotero.py", "desktop_projects.py",
                     "desktop_resume.py", "daily_agenda.py",
                     "palette_files.py", "project_overview.py",
                     "project_recap.py", "project_session_changes.py",
                     "quickshell_settings.py"):
            self.assertIn(f'"{name}"', helper)
            self.assertIn(name, EXTENSION)
        self.assertIn("_active_registry_files", helper)
        self.assertIn("_TRUSTED_SCRIPTS_DIR", helper)
        self.assertIn("_is_inside_trusted_scripts", helper)
        self.assertIn(".project-folder.lock", helper)
        folder_helper = (ROOT / "scripts" / "project_folder.py").read_text(encoding="utf-8")
        self.assertIn("expected_root", folder_helper)
        self.assertIn("decimal strings", folder_helper)
        # String identity (never numeric) end-to-end.
        self.assertIn('typeof v.root_dev !== "string"', EXTENSION)
        self.assertIn('typeof boundDevRaw !== "string"', EXTENSION)
        self.assertIn("MAX_SAFE_INTEGER", EXTENSION)
        system = (ROOT / ".pi" / "SYSTEM.md").read_text(encoding="utf-8")
        self.assertIn("active project registry file", system)

    def test_project_session_picker_and_switch_hook_are_scope_checked(self):
        self.assertIn('const directory = projectMode() ? projectSessionDir()', EXTENSION)
        self.assertIn('listed.filter((session) => validProjectSession(session?.path, directory!))', EXTENSION)
        self.assertIn('pi.on("session_before_switch"', EXTENSION)
        self.assertIn('return { cancel: true }', EXTENSION)
        self.assertIn('SessionManager.list(ctx.cwd, directory)', EXTENSION)
        self.assertIn('String(s.firstMessage ?? "").trim() || "Untitled"', EXTENSION)

    def test_coherent_desktop_history_surface(self):
        for name in ("desktop_current_context", "desktop_project_todos",
                     "desktop_project_logseq_context", "desktop_project_activity",
                     "desktop_current_session", "desktop_search_activity",
                     "desktop_get_session", "desktop_resume_plan",
                     "session_search"):
            self.assertIn(f'name: "{name}"', EXTENSION)
        for name in ("desktop_current_project", "desktop_project_resources",
                     "desktop_work_sessions", "desktop_session_resources",
                     "desktop_session_events"):
            self.assertNotIn(f'name: "{name}"', EXTENSION)
        self.assertIn("Intl.DateTimeFormat().resolvedOptions().timeZone", EXTENSION)
        self.assertIn("DESKTOP_LOCAL_TZ", EXTENSION)
        self.assertIn("start-inclusive/end-exclusive", EXTENSION)
        self.assertIn("never pass natural-language ranges", EXTENSION)
        self.assertIn('"current-context"', EXTENSION)
        self.assertIn('"search-activity"', EXTENSION)
        self.assertIn('"get-session"', EXTENSION)
        self.assertIn('"project-activity"', EXTENSION)
        system = (ROOT / ".pi" / "SYSTEM.md").read_text(encoding="utf-8")
        self.assertIn("Prefer session search", system)
        self.assertIn("desktop_get_session", system)
        self.assertIn("not file edits", system)
        self.assertIn("matched_at_ms", system)

    def test_scope_allowlists_state_desktop_exception(self):
        system = (ROOT / ".pi" / "SYSTEM.md").read_text(encoding="utf-8")
        self.assertGreaterEqual(
            system.count("nine read-only desktop tools"), 2)
        self.assertIn("nine-tool desktop read-only exception", system)
        self.assertIn("desktop exception never permits writes", system)
        skill = (ROOT / ".pi" / "skills" / "logseq-graph" / "SKILL.md"
                 ).read_text(encoding="utf-8")
        self.assertIn("exception in every scope", skill)
        self.assertIn("not file edits", EXTENSION)
        self.assertIn("qualify the claim", EXTENSION)

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
assert(Object.keys(tools).join(",") === "logseq_agenda_list,logseq_agenda_add,logseq_project_read,logseq_project_update,logseq_project_files,logseq_project_read_file,logseq_project_git,project_folder_list,project_folder_read,project_folder_write,zotero_search,zotero_item,zotero_read_pdf,zotero_collections,zotero_prepare,zotero_apply,desktop_current_context,desktop_project_todos,desktop_project_logseq_context,desktop_project_activity,desktop_current_session,desktop_search_activity,desktop_get_session,desktop_resume_plan,session_search",
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

    @unittest.skipUnless(BUN, "bun is required for executable extension tests")
    def test_extension_runtime_folder_write_binding_approval(self):
        """Exercise project_folder_write approval binding via mocked helpers.

        Covers preflight-bound preview (full destination), denial, missing
        UI, abort, stale revision, and registry-switch rejection without
        touching the filesystem.
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
let preflightRoot = "/linked";
let preflightRev = "a".repeat(64);
let preflightExists = true;
let writeShouldFailRegistrySwitch = false;

function spawn(command, args) {
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    child.stdin = new EventEmitter();
    child.stdin.write = (data) => { try { child._input = JSON.parse(String(data)); } catch {} };
    child.stdin.end = () => {};
    child.kill = () => { child.killed = true; };
    calls.push({ command, args });
    queueMicrotask(() => {
        child.emit("spawn");
        const helper = String(args[0] || "");
        const operation = args[args.length - 1];
        const isFolder = helper.includes("project_folder.py");
        if (isFolder && operation === "preflight") {
            child.stdout.emit("data", Buffer.from(JSON.stringify({
                root: preflightRoot, root_dev: "11", root_ino: "22",
                file: "a.txt", exists: preflightExists,
                ...(preflightExists ? { revision: preflightRev, size: 2 } : {}),
            })));
            child.emit("close", 0, null);
        } else if (isFolder && operation === "write") {
            const payload = child._input || {};
            if (writeShouldFailRegistrySwitch) {
                child.stderr.emit("data", Buffer.from("error: project folder changed; read the file again"));
                child.emit("close", 1, null);
            } else if (payload.expected_root !== preflightRoot
                       || payload.expected_root_dev !== "11"
                       || payload.expected_root_ino !== "22") {
                child.stderr.emit("data", Buffer.from("error: project folder changed; read the file again"));
                child.emit("close", 1, null);
            } else {
                child.stdout.emit("data", Buffer.from(JSON.stringify({
                    root: preflightRoot, file: "a.txt", size: 3,
                    revision: "b".repeat(64), created: false,
                })));
                child.emit("close", 0, null);
            }
        } else if (isFolder && operation === "list") {
            child.stdout.emit("data", Buffer.from(JSON.stringify({
                root: preflightRoot, entries: [{path: "a.txt", size: 2}], truncated: false,
            })));
            child.emit("close", 0, null);
        } else if (isFolder && operation === "read") {
            child.stdout.emit("data", Buffer.from(JSON.stringify({
                root: preflightRoot, file: "a.txt", size: 2, content: "hi",
                revision: preflightRev,
            })));
            child.emit("close", 0, null);
        } else {
            child.stdout.emit("data", Buffer.from(JSON.stringify({ ok: true })));
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
process.env.QS_PROJECT_ID = "00000000-0000-4000-8000-000000000001";
delete process.env.QS_PROJECT_PATH;
desktopAgent(pi);
const ctx = { cwd: "/work", hasUI: true,
    ui: { confirm: async (_title, message) => { confirmations.push(message); return approve; } } };
const noUi = { cwd: "/work", hasUI: false, ui: ctx.ui };
const assert = (value, message) => { if (!value) throw new Error(message); };

const folderGate = await hooks.tool_call({ toolName: "project_folder_write", input: { file: "a.txt" } }, ctx);
assert(!folderGate?.block, "pinned folder write was blocked");

const listed = await tools.project_folder_list.execute("id", {}, undefined, undefined, ctx);
assert(listed.content[0].text.includes("a.txt"), "folder list failed");
const read = await tools.project_folder_read.execute("id", { file: "a.txt" }, undefined, undefined, ctx);
assert(read.content[0].text.includes("hi"), "folder read failed");

const rev = "a".repeat(64);
const beforeNoUi = calls.length;
try { await tools.project_folder_write.execute("id", { file: "a.txt", content: "new", revision: rev }, undefined, undefined, noUi); throw new Error("no-UI write succeeded"); }
catch (error) { assert(String(error).includes("UI confirmation"), "no-UI denial missing"); }
assert(calls.length === beforeNoUi, "no-UI denial ran preflight");

const aborted = new AbortController();
aborted.abort();
try { await tools.project_folder_write.execute("id", { file: "a.txt", content: "new", revision: rev }, aborted.signal, undefined, ctx); throw new Error("aborted write succeeded"); }
catch (error) { assert(String(error).includes("aborted"), "abort error missing"); }

const beforeStale = calls.length;
const beforeConfirmations = confirmations.length;
try { await tools.project_folder_write.execute("id", { file: "a.txt", content: "new", revision: "0".repeat(64) }, undefined, undefined, ctx); throw new Error("stale write succeeded"); }
catch (error) { assert(String(error).includes("stale revision"), "stale error missing: " + error); }
assert(confirmations.length === beforeConfirmations, "stale write requested approval");
assert(calls.length === beforeStale + 1, "stale write did not stop after preflight");

approve = false;
try { await tools.project_folder_write.execute("id", { file: "a.txt", content: "new", revision: rev }, undefined, undefined, ctx); throw new Error("denied write succeeded"); }
catch (error) { assert(String(error).includes("denied by user"), "approval denial missing"); }
assert(confirmations.at(-1).includes("Destination: /linked/a.txt"), "preview missed full destination");
assert(confirmations.at(-1).includes("Exact replacement content:\nnew"), "preview was not exact");

approve = true;
writeShouldFailRegistrySwitch = true;
try { await tools.project_folder_write.execute("id", { file: "a.txt", content: "new", revision: rev }, undefined, undefined, ctx); throw new Error("registry-switch write succeeded"); }
catch (error) { assert(String(error).includes("project folder changed"), "registry-switch error missing: " + error); }
writeShouldFailRegistrySwitch = false;

const written = await tools.project_folder_write.execute("id", { file: "a.txt", content: "new", revision: rev }, undefined, undefined, ctx);
assert(written.content[0].text.includes("b".repeat(8).slice(0, 8)) || written.content[0].text.includes("/linked"), "approved write did not return response");
assert(calls[calls.length - 1].args.at(-1) === "write", "approved write used wrong helper command");

preflightExists = false;
try { await tools.project_folder_write.execute("id", { file: "missing.txt", content: "new", revision: rev }, undefined, undefined, ctx); throw new Error("missing-target update succeeded"); }
catch (error) { assert(String(error).includes("does not exist"), "missing-target error missing"); }
preflightExists = true;

console.log(JSON.stringify({ ok: true, confirmations: confirmations.length }));
'''
        with tempfile.NamedTemporaryFile("w", suffix=".ts", encoding="utf-8") as handle:
            handle.write(script)
            handle.flush()
            completed = subprocess.run([bun, handle.name], text=True,
                                       capture_output=True, timeout=8)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertEqual(json.loads(completed.stdout.splitlines()[-1])["ok"], True)

    @unittest.skipUnless(BUN, "bun is required for executable extension tests")
    def test_extension_runtime_palette_creation_approval_binding(self):
        """Exercise create_logseq_page/create_project via mocked helpers.

        Covers palette registration, no-UI denial and abort before any
        spawn, properties bounds before approval (FIX 3), denial after
        prepare performing no commit, the bare-name namespace mapping
        (FIX 1), the commit target binding (FIX 5), the create-dir commit
        shape gate before any registry write (FIX 8), and the registry
        logseq_path agreement — without touching the filesystem.
        """
        bun = BUN
        assert bun is not None
        source = "\n".join(line for line in EXTENSION.splitlines()
                              if not line.startswith("import "))
        source = source.replace("export default function desktopAgent", "function desktopAgent")
        script = r'''
import { EventEmitter } from "node:events";
import { createHash } from "node:crypto";
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
const payloads = [];
const confirmations = [];
let approve = true;
let malformedCreateDir = false;
const CONTENT = "type:: project\n- hello\n";
const SHA = createHash("sha256").update(CONTENT).digest("hex");
const TARGET = "pages/Demo___Page.md";

function spawn(command, args) {
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    child.stdin = new EventEmitter();
    child.stdin.write = (data) => { try { child._input = JSON.parse(String(data)); } catch {} };
    child.stdin.end = () => {};
    child.kill = () => { child.killed = true; };
    calls.push({ command, args });
    queueMicrotask(() => {
        child.emit("spawn");
        const helper = String(args[0] || "");
        const operation = args[args.length - 1];
        const finish = (code, out, err) => {
            if (out) child.stdout.emit("data", Buffer.from(out));
            if (err) child.stderr.emit("data", Buffer.from(err));
            child.emit("close", code, null);
        };
        if (helper.includes("project_planner.py") && operation === "create-page") {
            const payload = child._input || {};
            payloads.push({ helper: "create-page", ...payload });
            if (payload.stage === "prepare") {
                finish(0, JSON.stringify({ stage: "prepare", target: TARGET, content: CONTENT,
                    content_sha256: SHA, template: "", template_including_parent: false }));
            } else if (payload.stage === "commit") {
                if (payload.expected_target !== TARGET) finish(1, "", "error: target changed, re-run prepare");
                else if (payload.expected_sha256 !== SHA) finish(1, "", "error: content changed, re-run prepare");
                else finish(0, JSON.stringify({ stage: "commit", created: true, target: TARGET, content_sha256: SHA }));
            } else finish(1, "", "error: bad stage");
        } else if (helper.includes("project_folder.py") && operation === "create-dir-preflight") {
            const payload = child._input || {};
            payloads.push({ helper: "create-dir-preflight", ...payload });
            finish(0, JSON.stringify({ path: "/home/user/work", exists: false, is_dir: false,
                parent_path: "/home/user", parent_dev: 11, parent_ino: 22 }));
        } else if (helper.includes("project_folder.py") && operation === "create-dir") {
            const payload = child._input || {};
            payloads.push({ helper: "create-dir", ...payload });
            if (malformedCreateDir) finish(0, JSON.stringify({ path: "/home/user/work" }));
            else if (payload.expected_parent_dev !== 11 || payload.expected_parent_ino !== 22)
                finish(1, "", "error: directory changed; request a new preflight");
            else finish(0, JSON.stringify({ path: "/home/user/work", created: true }));
        } else if (helper.includes("projects.py") && operation === "create") {
            const payload = child._input || {};
            payloads.push({ helper: "projects-create", ...payload });
            finish(0, JSON.stringify({ id: "11111111-1111-4111-8111-111111111111",
                name: payload.name || "", logseq_path: payload.logseq_path || "",
                local_folder: payload.local_folder || "" }));
        } else {
            finish(0, JSON.stringify({ ok: true }));
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
delete process.env.QS_PROJECT_ID;
delete process.env.QS_PROJECT_PATH;
delete process.env.QS_JOURNAL_MODE;
process.env.LOGSEQ_GRAPH = "/graph";
desktopAgent(pi);
const ctx = { cwd: "/work", hasUI: true,
    ui: { confirm: async (_title, message) => { confirmations.push(message); return approve; } } };
const noUi = { cwd: "/work", hasUI: false, ui: ctx.ui };
const assert = (value, message) => { if (!value) throw new Error(message); };

assert(tools.create_project && tools.create_logseq_page, "palette creation tools not registered");

const beforeNoUi = calls.length;
try { await tools.create_logseq_page.execute("id", { name: "Demo/Page" }, undefined, undefined, noUi); throw new Error("no-UI page succeeded"); }
catch (error) { assert(String(error).includes("UI confirmation"), "page no-UI denial missing: " + error); }
try { await tools.create_project.execute("id", { name: "Demo" }, undefined, undefined, noUi); throw new Error("no-UI project succeeded"); }
catch (error) { assert(String(error).includes("UI confirmation"), "project no-UI denial missing: " + error); }
assert(calls.length === beforeNoUi, "no-UI denial spawned a helper");

const aborted = new AbortController();
aborted.abort();
try { await tools.create_logseq_page.execute("id", { name: "Demo/Page" }, aborted.signal, undefined, ctx); throw new Error("aborted page succeeded"); }
catch (error) { assert(String(error).includes("aborted"), "abort error missing"); }

const many = {};
for (let i = 0; i < 17; i++) many["k" + i] = "v";
const beforeBounds = calls.length;
try { await tools.create_logseq_page.execute("id", { name: "Demo/Page", properties: many }, undefined, undefined, ctx); throw new Error("17 props succeeded"); }
catch (error) { assert(String(error).includes("16"), "entries bound missing: " + error); }
try { await tools.create_logseq_page.execute("id", { name: "Demo/Page", properties: { ["k".repeat(65)]: "v" } }, undefined, undefined, ctx); throw new Error("long key succeeded"); }
catch (error) { assert(String(error).includes("too long"), "key bound missing: " + error); }
try { await tools.create_logseq_page.execute("id", { name: "Demo/Page", properties: { k: "x".repeat(513) } }, undefined, undefined, ctx); throw new Error("long value succeeded"); }
catch (error) { assert(String(error).includes("too long"), "value bound missing: " + error); }
assert(calls.length === beforeBounds, "bounds rejection spawned a helper");

approve = false;
const beforeDeny = calls.length;
try { await tools.create_logseq_page.execute("id", { name: "Demo/Page" }, undefined, undefined, ctx); throw new Error("denied page succeeded"); }
catch (error) { assert(String(error).includes("denied by user"), "page denial missing: " + error); }
assert(calls.length === beforeDeny + 1, "denial did not stop after prepare");
assert(payloads.filter((p) => p.helper === "create-page" && p.stage === "commit").length === 0, "denied page reached commit");

approve = true;
const made = await tools.create_logseq_page.execute("id", { name: "Demo/Page" }, undefined, undefined, ctx);
assert(made.content[0].text.includes("pages/Demo___Page.md"), "page commit response missing target");
const preparePayload = payloads.find((p) => p.helper === "create-page" && p.stage === "prepare");
assert(preparePayload && preparePayload.page === "pages/Demo___Page.md", "prepare used wrong page: " + JSON.stringify(preparePayload));
const commitPayload = payloads.find((p) => p.helper === "create-page" && p.stage === "commit");
assert(commitPayload && commitPayload.expected_target === "pages/Demo___Page.md", "commit missed expected_target");
assert(commitPayload.expected_sha256 === SHA, "commit missed sha");
assert(confirmations.at(-1).includes("Target: pages/Demo___Page.md"), "preview missed target");

const project = await tools.create_project.execute("id", { name: "Demo", logseq_page: "a/b",
    project_folder: "/home/user/work", create_folder: true }, undefined, undefined, ctx);
assert(project.content[0].text.includes("Demo"), "project create response missing");
const dirPayload = payloads.find((p) => p.helper === "create-dir");
assert(dirPayload && dirPayload.expected_parent_dev === 11 && dirPayload.expected_parent_ino === 22, "create-dir missed binding");
const regPayload = payloads.find((p) => p.helper === "projects-create");
assert(regPayload && regPayload.logseq_path === "pages/a___b.md", "registry got wrong logseq_path: " + JSON.stringify(regPayload));
assert(confirmations.at(-1).includes("pages/a___b.md"), "project preview missed normalized page");

malformedCreateDir = true;
const regCount = payloads.filter((p) => p.helper === "projects-create").length;
try { await tools.create_project.execute("id", { name: "Bad", project_folder: "/home/user/work", create_folder: true }, undefined, undefined, ctx); throw new Error("malformed create-dir succeeded"); }
catch (error) { assert(String(error).includes("invalid create-dir result"), "malformed error missing: " + error); }
assert(payloads.filter((p) => p.helper === "projects-create").length === regCount, "malformed create-dir reached the registry");
malformedCreateDir = false;

approve = false;
const beforePDeny = calls.length;
try { await tools.create_project.execute("id", { name: "Denied" }, undefined, undefined, ctx); throw new Error("denied project succeeded"); }
catch (error) { assert(String(error).includes("denied by user"), "project denial missing: " + error); }
assert(calls.length === beforePDeny, "denied project spawned a create");

console.log(JSON.stringify({ ok: true }));
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
