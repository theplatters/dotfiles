"""UUID scope + Zotero QML/Pi integration (fake helper, no backend).

Covers: UUID session dirs isolated from legacy page scopes, no-note project
helpers, explicit legacy migration only, scoped tool allowlist/approval
denial, and QML contracts. Final backend alignment (exact prepare/apply
schemas for add-item/add-existing/update/membership/subcollection) happens
once scripts/zotero.py exists; until then the extension fails closed.
"""
import sys as _sys
_sys.dont_write_bytecode = True
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).parents[1]
SESSIONS = ROOT / "scripts" / "project_sessions.py"
PLANNER = ROOT / "scripts" / "project_planner.py"
SCOPED = (ROOT / "widgets" / "ScopedAgent.qml").read_text(encoding="utf-8")
PLANNER_QML = (ROOT / "widgets" / "ProjectPlanner.qml").read_text(encoding="utf-8")
EXTENSION = (ROOT / ".pi" / "extensions" / "desktop-agent.ts").read_text(encoding="utf-8")
BUN = shutil.which("bun")

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

class UuidSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.graph = self.root / "graph"
        (self.graph / "pages").mkdir(parents=True)
        (self.graph / "pages" / "Project.md").write_text("# p\n", encoding="utf-8")
        self.env = dict(os.environ, LOGSEQ_GRAPH=str(self.graph), XDG_STATE_HOME=str(self.root / "state"))
        self.mod = load("pss_uuid", SESSIONS)
        self.pid = str(uuid.uuid4())
        self.header = json.dumps({"type":"session","version":3,"id":"x","timestamp":"2026-01-01T00:00:00.000Z","cwd":"/tmp"})+"\n"

    def tearDown(self):
        self.tmp.cleanup()

    def test_uuid_scope_is_by_id_and_isolated_from_legacy(self):
        base = self.root / "base"
        graph = self.mod.resolved_graph(str(self.graph))
        page = self.mod.project_relative_page(graph, "pages/Project.md")
        legacy = self.mod.scope_for(graph, page, base)
        new = self.mod.scope_for_id(base, self.pid)
        self.assertIn("by-id", new.parts)
        self.assertTrue(str(new).endswith(self.pid))
        self.assertNotEqual(legacy, new)
        self.assertFalse(str(new).startswith(str(legacy)))
        # latest only scans active scope
        f1 = new / "a.jsonl"; f1.write_text(self.header, encoding="utf-8")
        f2 = legacy / "b.jsonl"; f2.write_text(self.header, encoding="utf-8")
        self.assertEqual(self.mod.latest_session(new), f1)
        self.assertEqual(self.mod.latest_session(legacy), f2)

    def test_missing_id_and_missing_page_refused(self):
        args = self.mod.parse_args(["--project-id", self.pid])
        self.assertEqual(args.project_id, self.pid)
        with self.assertRaises(Exception):
            self.mod.validate_project_id("not-a-uuid")

    def test_explicit_legacy_restore_allowed_but_auto_scan_never_mixes(self):
        base = self.root / "base"
        graph = self.mod.resolved_graph(str(self.graph))
        page = self.mod.project_relative_page(graph, "pages/Project.md")
        legacy = self.mod.scope_for(graph, page, base)
        new = self.mod.scope_for_id(base, self.pid)
        cached = legacy / "old.jsonl"; cached.write_text(self.header, encoding="utf-8")
        # explicit legacy scope allows the file
        cmd = self.mod.command_for(new, str(cached), False, None, legacy)
        self.assertIn(str(cached), cmd)
        # without explicit legacy scope it is refused
        with self.assertRaises(Exception):
            self.mod.command_for(new, str(cached), False, None, None)
        # auto latest in new scope ignores legacy dir
        self.assertIsNone(self.mod.latest_session(new) if not list(new.glob("*.jsonl")) else self.mod.latest_session(new))
        # new file in new scope is picked
        fresh = new / "n.jsonl"; fresh.write_text(self.header, encoding="utf-8")
        self.assertEqual(self.mod.latest_session(new), fresh)

    def test_wrapper_runs_with_project_id_and_empty_path(self):
        bindir = self.root / "bin"; bindir.mkdir()
        fake = bindir / "pi"
        fake.write_text("#!/usr/bin/env python3\nimport json,os,sys\nprint(json.dumps({'argv':sys.argv[1:],'dir':os.environ.get('PI_CODING_AGENT_SESSION_DIR'),'pid':os.environ.get('QS_PROJECT_ID'),'path':os.environ.get('QS_PROJECT_PATH')}))\n", encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        env = dict(self.env, PATH=f"{bindir}:{os.environ['PATH']}")
        completed = subprocess.run([sys.executable, str(SESSIONS), "--project-id", self.pid], env=env, text=True, capture_output=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertIn("by-id", value["dir"])
        self.assertEqual(value["pid"], self.pid.lower())
        self.assertEqual(value["path"], "")

class NoNotePlannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.graph = Path(self.tmp.name) / "g"
        (self.graph / "pages").mkdir(parents=True)
        (self.graph / "journals").mkdir(parents=True)
        (self.graph / "pages" / "P.md").write_text("- TODO t\n", encoding="utf-8")
        self.reg = Path(self.tmp.name) / "reg.toml"
        self.pid_note = str(uuid.uuid4())
        self.pid_zot = str(uuid.uuid4())
        # minimal registry with one noted + one zotero-only project
        self.reg.write_text(f'version = 1\n\n[[projects]]\nid = "{self.pid_note}"\nname = "Noted"\nlogseq_path = "pages/P.md"\nlocal_folder = ""\n\n[[projects]]\nid = "{self.pid_zot}"\nname = "Zot"\nlocal_folder = ""\n', encoding="utf-8")
        self.env = dict(os.environ, QUICKSHELL_PROJECTS_FILE=str(self.reg))

    def tearDown(self):
        self.tmp.cleanup()

    def run_helper(self, cmd, payload):
        env = dict(os.environ, QUICKSHELL_PROJECTS_FILE=str(self.reg))
        return subprocess.run([sys.executable, str(PLANNER), "--graph", str(self.graph), cmd], input=json.dumps(payload), env=env, text=True, capture_output=True, timeout=5)

    def test_note_tools_fail_clearly_without_note(self):
        r = self.run_helper("page", {"project_id": self.pid_zot})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no linked note", r.stderr)
        r2 = self.run_helper("files-list", {"project_id": self.pid_zot})
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("no linked folder", r2.stderr)

    def test_noted_uuid_resolves_fresh(self):
        r = self.run_helper("page", {"project_id": self.pid_note})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["path"], "pages/P.md")

class PolicyContractTests(unittest.TestCase):
    def test_allowlist_includes_zotero_and_blocks_generic(self):
        self.assertIn('"zotero_search"', EXTENSION)
        self.assertIn('"zotero_apply"', EXTENSION)
        self.assertIn('"zotero_collections"', EXTENSION)
        self.assertIn('Project mode permits only the constrained project tools', EXTENSION)
        self.assertIn('Generic traversal is disabled', EXTENSION)
        self.assertIn('zotero.py', EXTENSION)
        self.assertIn('pinnedProjectId()', EXTENSION)
        self.assertIn('resolveZoteroProjectId', EXTENSION)
        self.assertIn('cross-project access denied', EXTENSION)
        self.assertIn('zotero tools are unavailable in journal mode', EXTENSION)
        self.assertIn('No keys in output', EXTENSION)
        self.assertIn('never ingest a whole library', EXTENSION.lower() if False else EXTENSION)
        self.assertIn('library deletion is never performed', EXTENSION)

    def test_qml_uuid_and_zotero_contracts(self):
        self.assertIn('property string projectId', SCOPED)
        self.assertIn('--project-id', SCOPED)
        self.assertIn('function isUuid', PLANNER_QML)
        self.assertIn('function agentForId', PLANNER_QML)
        self.assertIn('function agentForProject', PLANNER_QML)
        self.assertIn('function sendWithoutNote', PLANNER_QML)
        self.assertIn('projectFormZoteroCollectionKey', PLANNER_QML)
        self.assertIn('function zoteroFormLink', PLANNER_QML)
        self.assertIn('function zoteroUnlink', PLANNER_QML)
        self.assertIn('Pick Zotero collection', PLANNER_QML)
        # legacy compat retained
        self.assertIn('property var agentCache: ({})', PLANNER_QML)
        self.assertIn('text: root.draftFor(root.selectedPath)', PLANNER_QML)
        self.assertIn('startPage(selectedPath, "send"', PLANNER_QML)

class ZoteroApplyApprovalContractTests(unittest.TestCase):
    def apply_segments(self):
        first = EXTENSION.index('name: "zotero_apply"')
        second = EXTENSION.index('name: "zotero_apply"', first + 1)
        return EXTENSION[first:first + 4000], EXTENSION[second:second + 4000]

    def test_preview_command_in_helper_union(self):
        self.assertIn('"prepare" | "preview" | "apply"', EXTENSION)

    def test_apply_schema_keeps_optional_preview_param(self):
        self.assertIn('preview: Type.Optional(Type.String())', EXTENSION)

    def test_apply_bound_to_backend_preview_not_model_text(self):
        project, palette = self.apply_segments()
        for segment in (project, palette):
            ui = segment.index('if (!ctx.hasUI)')
            token = segment.index('prepared must be the opaque token')
            preview = segment.index('zoteroHelper(ctx, "preview", { project_id: pid, prepared: token }')
            ask = segment.index('await ask(ctx, "Approve Zotero mutation"')
            apply = segment.index('zoteroHelper(ctx, "apply", { project_id: pid, prepared: token }')
            self.assertLess(ui, token)
            self.assertLess(token, preview)
            self.assertLess(preview, ask)
            self.assertLess(ask, apply)
            self.assertIn('from stored plan', segment)
            self.assertIn('zotero helper returned an invalid mutation preview', segment)
            self.assertIn('ZOTERO_MAX_PREVIEW_BYTES', segment)
            self.assertIn('/^[A-Za-z0-9_\\-]{16,64}$/', segment)
            self.assertIn('library deletion is never performed', segment)
        self.assertEqual(EXTENSION.count('zoteroHelper(ctx, "preview", { project_id: pid, prepared: token }'), 2)
        # No model-supplied preview reaches the backend or the dialog.
        self.assertNotIn('previewRaw', EXTENSION)
        self.assertNotIn('Apply the prepared mutation.', EXTENSION)
        self.assertNotIn('pass the preview from zotero_prepare', EXTENSION)
        self.assertIn('but ignored', EXTENSION)

    def test_palette_prepare_matches_project_validation(self):
        palette_start = EXTENSION.index('if (!projectMode() && !journalMode())')
        palette = EXTENSION[palette_start:]
        prepare = palette[palette.index('name: "zotero_prepare"'):palette.index('name: "zotero_prepare"') + 2500]
        self.assertIn('operation must be bounded text', prepare)
        self.assertIn('library/collection deletion is not permitted', prepare)
        self.assertIn('zotero prepare request exceeds 1 MiB', prepare)
        self.assertIn('typeof out.prepared', prepare)
        self.assertIn('zotero preview is oversized', prepare)

    @unittest.skipUnless(BUN, "bun is required for executable extension tests")
    def test_apply_runtime_preview_then_apply_with_backend_text(self):
        """Execute the Zotero apply gates against a mocked spawn helper.

        Stubs spawn (as the project-agent runtime test does) so the real
        scripts/zotero.py is never invoked, and asserts the spawned command
        sequence is `preview` then `apply` with the approval dialog showing
        only the backend preview string.
        """
        assert BUN is not None
        bun = BUN
        source = "\n".join(line for line in EXTENSION.splitlines()
                            if not line.startswith("import "))
        source = source.replace("export default function desktopAgent", "function desktopAgent")
        script = r'''
import { EventEmitter } from "node:events";

const fileURLToPath = () => "/repo/.pi/extensions/desktop-agent.ts";
const dirname = (value) => "/repo/.pi/extensions";
const existsSync = () => true;
const lstatSync = () => { throw new Error("unused"); };
const realpathSync = { native: (value) => value };
const readFileSync = () => "{}";
const isAbsolute = (value) => value.startsWith("/");
const join = (...parts) => parts.join("/");
const normalize = (value) => value;
const relative = () => "";
const resolve = (...parts) => parts.join("/");
const SessionManager = {};
const Type = { Object: (value) => value, String: () => ({}), Optional: (value) => value };
const calls = [];
const confirmations = [];
let approve = true;

function spawn(command, args) {
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    child.stdin = new EventEmitter();
    child.stdin.write = (data) => { child.written = (child.written || "") + String(data); };
    child.stdin.end = () => {};
    child.kill = () => { child.killed = true; };
    queueMicrotask(() => {
        child.emit("spawn");
        const operation = args[args.length - 1];
        let payload = {};
        try { payload = JSON.parse(child.written || "{}"); } catch { payload = {}; }
        calls.push({ operation, payload });
        if (operation === "preview") {
            child.stdout.emit("data", Buffer.from(JSON.stringify({ preview: "BACKEND-PREVIEW-CANONICAL",
                binding: { server_id: "s1", library: "u/0", collection_key: "ABCDEFGH", include_subcollections: true },
                expires_in: 60 })));
            child.emit("close", 0, null);
        } else if (operation === "prepare") {
            child.stdout.emit("data", Buffer.from(JSON.stringify({ prepared: "Abcdefghijklmnop_1", preview: "PREPARE-PREVIEW" })));
            child.emit("close", 0, null);
        } else if (operation === "apply") {
            child.stdout.emit("data", Buffer.from(JSON.stringify({ ok: true, applied: 1 })));
            child.emit("close", 0, null);
        } else {
            child.stdout.emit("data", Buffer.from(JSON.stringify({})));
            child.emit("close", 0, null);
        }
    });
    return child;
}

''' + source + r'''

const TOKEN = "Abcdefghijklmnop_1";
const EVIL = "CALLER-EVIL-PREVIEW-should-never-appear";
const PID = "11111111-2222-3333-4444-555555555555";
const PID2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
const assert = (value, message) => { if (!value) throw new Error(message); };
const ops = () => calls.map((call) => call.operation);

// Project mode: pinned UUID, no journal flag.
process.env.QS_PROJECT_ID = PID;
delete process.env.QS_PROJECT_PATH;
delete process.env.QS_JOURNAL_MODE;
const pi = { on() {}, registerCommand() {} };
const tools = {};
pi.registerTool = (tool) => { tools[tool.name] = tool; };
desktopAgent(pi);
const ctx = { cwd: "/work", hasUI: true,
    ui: { confirm: async (_title, message) => { confirmations.push(message); return approve; } } };
const noUi = { cwd: "/work", hasUI: false, ui: ctx.ui };

// Project apply uses the backend preview, never the caller-supplied one.
const beforeProject = calls.length;
const projectOut = await tools.zotero_apply.execute("id", { prepared: TOKEN, preview: EVIL }, undefined, undefined, ctx);
assert(projectOut.content[0].text.includes('"applied": 1'), "project apply did not return backend result");
assert(JSON.stringify(ops().slice(beforeProject)) === JSON.stringify(["preview", "apply"]), "project apply did not run preview then apply");
const projectPreviewCall = calls[beforeProject];
assert(projectPreviewCall.payload.prepared === TOKEN && projectPreviewCall.payload.project_id === PID, "project preview payload wrong");
assert(!("preview" in projectPreviewCall.payload), "model preview was sent to the backend");
const projectApplyCall = calls[beforeProject + 1];
assert(JSON.stringify(Object.keys(projectApplyCall.payload).sort()) === JSON.stringify(["prepared", "project_id"]), "project apply payload must be {project_id, prepared} only");
const projectMessage = confirmations.at(-1);
assert(projectMessage.includes("BACKEND-PREVIEW-CANONICAL"), "approval dialog missing backend preview");
assert(!projectMessage.includes(EVIL), "approval dialog used caller-supplied preview");

// Invalid tokens are rejected before any helper call.
const beforeBadToken = calls.length;
try { await tools.zotero_apply.execute("id", { prepared: "short" }, undefined, undefined, ctx); throw new Error("bad token apply succeeded"); }
catch (error) { assert(String(error).includes("opaque token"), "bad token error missing"); }
assert(calls.length === beforeBadToken, "bad token reached the helper");

// Missing UI denies with no helper call.
const beforeNoUi = calls.length;
try { await tools.zotero_apply.execute("id", { prepared: TOKEN }, undefined, undefined, noUi); throw new Error("no-UI apply succeeded"); }
catch (error) { assert(String(error).includes("UI confirmation"), "no-UI denial missing"); }
assert(calls.length === beforeNoUi, "no-UI denial spawned a helper");

// Denial performs no write: preview runs, apply never does.
approve = false;
const beforeDeny = calls.length;
try { await tools.zotero_apply.execute("id", { prepared: TOKEN }, undefined, undefined, ctx); throw new Error("denied apply succeeded"); }
catch (error) { assert(String(error).includes("denied"), "approval denial missing"); }
assert(JSON.stringify(ops().slice(beforeDeny)) === JSON.stringify(["preview"]), "denial performed a write");
approve = true;

// Palette mode: no pin, explicit project UUID required.
delete process.env.QS_PROJECT_ID;
delete process.env.QS_PROJECT_PATH;
delete process.env.QS_JOURNAL_MODE;
const tools2 = {};
const pi2 = { on() {}, registerTool(tool) { tools2[tool.name] = tool; }, registerCommand() {} };
desktopAgent(pi2);
const beforePalette = calls.length;
const paletteOut = await tools2.zotero_apply.execute("id", { project_id: PID2, prepared: TOKEN, preview: EVIL }, undefined, undefined, ctx);
assert(paletteOut.content[0].text.includes('"applied": 1'), "palette apply did not return backend result");
assert(JSON.stringify(ops().slice(beforePalette)) === JSON.stringify(["preview", "apply"]), "palette apply did not run preview then apply");
const paletteMessage = confirmations.at(-1);
assert(paletteMessage.includes("BACKEND-PREVIEW-CANONICAL"), "palette approval missing backend preview");
assert(!paletteMessage.includes(EVIL), "palette approval used caller-supplied preview");

// Palette prepare rejects library/collection deletion phrasing.
const beforeDelete = calls.length;
try { await tools2.zotero_prepare.execute("id", { project_id: PID2, operation: "delete collection X", params: {} }, undefined, undefined, ctx); throw new Error("deletion prepare succeeded"); }
catch (error) { assert(String(error).includes("not permitted"), "palette deletion rejection missing"); }
assert(calls.length === beforeDelete, "rejected palette prepare reached the helper");
console.log(JSON.stringify({ ok: true }));
'''
        with tempfile.NamedTemporaryFile("w", suffix=".ts", encoding="utf-8") as handle:
            handle.write(script)
            handle.flush()
            completed = subprocess.run([bun, handle.name], text=True,
                                       capture_output=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertEqual(json.loads(completed.stdout.splitlines()[-1])["ok"], True)

if __name__ == "__main__":
    unittest.main()
