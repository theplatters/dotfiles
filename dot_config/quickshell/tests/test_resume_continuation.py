"""Resume continuation: cached work-log enrichment for planner Ask prefill.

Backend (scripts/work_log.py continuation): read-only latest cached draft
summary, gated by memory.enabled / memory.workLog. No draft generation,
no labels, no polish, no network.
Planner (widgets/ProjectPlanner.qml): synchronous base prefill on Ask plus
async continuation Process upgrade guarded by project/path/interaction,
idle state, and exact-base draft match. Prefill only, never auto-send.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import annotations
import work_log
from work_log import WorkLogError

PID = "11111111-1111-4111-8111-111111111111"
PID2 = "22222222-2222-4222-8222-222222222222"
SID = "a" * 32
SID2 = "b" * 32
NOW = 5_000_000
SCRIPT = ROOT / "scripts" / "work_log.py"
PLANNER = (ROOT / "widgets" / "ProjectPlanner.qml").read_text(encoding="utf-8")


def seed_draft(conn, draft_id="dwl_test1", project=PID, session=SID,
               created=1000, sections=None, polished=None):
    if sections is None:
        sections = [
            {"id": "what_happened", "title": "What happened",
             "items": ["Session lasted 5m."]},
            {"id": "open_todos", "title": "Open TODOs",
             "items": ["No open TODOs."]},
            {"id": "next", "title": "Next",
             "items": ["Review the session."]},
        ]
    payload = {"sections": sections, "markdown": "# Session log"}
    if polished is not None:
        payload["polished_markdown"] = polished
        payload["polished"] = True
    annotations.save_draft(
        conn, draft_id=draft_id, project_id=project, session_id=session,
        summary_key="k", evidence_digest="d" * 64, payload=payload,
        created_ms=created)
    return payload


def seed_label(conn, session=SID, **over):  # pragma: no cover
    raise AssertionError("session labels are deleted; seed drafts only")


class ContinuationCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "annotations.db")
        self.conn = annotations.connect(self.db)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.temp.cleanup()

    def settings(self, **over):
        base = {"enabled": True, "workLog": True}
        base.update(over)
        return base


class ContinuationBackendTests(ContinuationCase):
    def test_summary_from_cached_draft(self):
        seed_draft(self.conn)
        out = work_log.continuation_text(
            PID, conn=self.conn, settings=self.settings())
        self.assertEqual(out["project"], PID)
        self.assertTrue(out["available"])
        self.assertIn("What happened:", out["text"])
        self.assertNotIn("Last session activity:", out["text"])
        self.assertLessEqual(len(out["text"]),
                             work_log.CONTINUATION_TEXT_LIMIT)

    def test_master_disabled_is_empty(self):
        seed_draft(self.conn)
        out = work_log.continuation_text(
            PID, conn=self.conn,
            settings=self.settings(enabled=False))
        self.assertEqual(out, {"project": PID, "available": False,
                               "text": ""})

    def test_worklog_off_is_empty(self):
        seed_draft(self.conn)
        out = work_log.continuation_text(
            PID, conn=self.conn,
            settings=self.settings(workLog=False))
        self.assertFalse(out["available"])
        self.assertEqual(out["text"], "")

    def test_summary_from_draft_alone(self):
        seed_draft(self.conn)
        out = work_log.continuation_text(
            PID, conn=self.conn, settings=self.settings())
        self.assertTrue(out["available"])
        self.assertNotIn("Last session activity:", out["text"])
        self.assertIn("What happened:", out["text"])

    def test_no_conn_is_empty(self):
        out = work_log.continuation_text(
            PID, conn=None, settings=self.settings())
        self.assertFalse(out["available"])
        self.assertEqual(out["text"], "")

    def test_no_draft_is_empty(self):
        out = work_log.continuation_text(
            PID, conn=self.conn, settings=self.settings())
        self.assertFalse(out["available"])

    def test_project_isolation(self):
        seed_draft(self.conn, project=PID2)
        out = work_log.continuation_text(
            PID, conn=self.conn, settings=self.settings())
        self.assertFalse(out["available"])

    def test_latest_draft_wins(self):
        seed_draft(self.conn, draft_id="dwl_old", created=1000,
                   sections=[{"id": "what_happened",
                              "title": "What happened",
                              "items": ["Old session."]}])
        seed_draft(self.conn, draft_id="dwl_new", created=2000,
                   sections=[{"id": "what_happened",
                              "title": "What happened",
                              "items": ["New session."]}])
        out = work_log.continuation_text(
            PID, conn=self.conn,
            settings=self.settings())
        self.assertTrue(out["available"])
        self.assertIn("New session.", out["text"])
        self.assertNotIn("Old session.", out["text"])

    def test_malformed_row_skipped(self):
        seed_draft(self.conn, draft_id="dwl_good", created=1000,
                   sections=[{"id": "what_happened",
                              "title": "What happened",
                              "items": ["Good session."]}])
        # Newer but corrupt JSON: must be skipped, not fatal.
        self.conn.execute(
            "INSERT INTO drafts(draft_id, project_id, session_id,"
            " summary_key, evidence_digest, json, created_ms)"
            " VALUES(?, ?, ?, ?, ?, ?, ?)",
            ("dwl_bad", PID, SID2, "k", "d" * 64, "{not json",
             2000))
        self.conn.commit()
        out = work_log.continuation_text(
            PID, conn=self.conn,
            settings=self.settings())
        self.assertTrue(out["available"])
        self.assertIn("Good session.", out["text"])

    def test_polished_text_never_used(self):
        seed_draft(self.conn, polished="# POLISHED-MARKER-xyz")
        out = work_log.continuation_text(
            PID, conn=self.conn, settings=self.settings())
        self.assertTrue(out["available"])
        self.assertNotIn("POLISHED-MARKER-xyz", out["text"])

    def test_bounded_and_sanitized(self):
        big = "X" * 5000 + "\x00\x01control"
        seed_draft(self.conn, sections=[
            {"id": "what_happened", "title": "What happened",
             "items": [big]},
            {"id": "open_todos", "title": "Open TODOs",
             "items": ["a task"]}])
        out = work_log.continuation_text(
            PID, conn=self.conn, settings=self.settings())
        self.assertTrue(out["available"])
        self.assertLessEqual(len(out["text"]), 2400)
        self.assertNotIn("\x00", out["text"])
        self.assertNotIn("XXXX", out["text"][2400:] if len(out["text"]) > 2400
                         else "impossible-marker")

    def test_invalid_project_raises(self):
        with self.assertRaises(WorkLogError):
            work_log.continuation_text(
                "nope", conn=self.conn, settings=self.settings())

    def _insert_raw_draft(self, draft_id, payload_json, created,
                          project=PID, session=SID):
        self.conn.execute(
            "INSERT INTO drafts(draft_id, project_id, session_id,"
            " summary_key, evidence_digest, json, created_ms)"
            " VALUES(?, ?, ?, ?, ?, ?, ?)",
            (draft_id, project, session, "k", "d" * 64,
             payload_json, created))
        self.conn.commit()

    def test_oversized_draft_json_skipped(self):
        seed_draft(self.conn, draft_id="dwl_good", created=1000,
                   sections=[{"id": "what_happened",
                              "title": "What happened",
                              "items": ["Good session."]}])
        big = json.dumps({"sections": [{"id": "what_happened",
                                         "title": "What happened",
                                         "items": ["Oversized session."]}],
                          "pad": "X" * 300_000})
        self.assertGreater(len(big.encode("utf-8")),
                           work_log._PAYLOAD_BYTES)
        self._insert_raw_draft("dwl_big", big, created=2000)
        out = work_log.continuation_text(
            PID, conn=self.conn,
            settings=self.settings())
        self.assertTrue(out["available"])
        self.assertIn("Good session.", out["text"])
        self.assertNotIn("Oversized session.", out["text"])

    def test_oversized_draft_json_only_unavailable(self):
        big = json.dumps({"sections": [{"id": "what_happened",
                                         "title": "What happened",
                                         "items": ["Oversized session."]}],
                          "pad": "X" * 300_000})
        self._insert_raw_draft("dwl_big", big, created=2000)
        out = work_log.continuation_text(
            PID, conn=self.conn, settings=self.settings())
        self.assertFalse(out["available"])
        self.assertEqual(out["text"], "")

    def test_oversized_session_id_rejected(self):
        payload = json.dumps({"sections": [
            {"id": "what_happened", "title": "What happened",
             "items": ["Good session."]}]})
        self._insert_raw_draft("dwl_big_sid", payload, created=1000,
                               session="S" * 10_000)
        out = work_log.continuation_text(
            PID, conn=self.conn, settings=self.settings())
        self.assertTrue(out["available"])
        self.assertIn("Good session.", out["text"])

    def test_huge_session_id_not_prefetched(self):
        payload = json.dumps({"sections": [
            {"id": "what_happened", "title": "What happened",
             "items": ["Good session."]}]})
        self._insert_raw_draft("dwl_huge_sid", payload, created=1000,
                               session="S" * 10_000)
        latest = work_log._latest_cached_draft(self.conn, PID)
        self.assertIsNotNone(latest)
        assert latest is not None
        # Bounded SQL prefetch: huge ids read back as NULL, never allocated.
        self.assertIsNone(latest.get("session_id"))
        # Summary is still preserved through the public path.
        out = work_log.continuation_text(
            PID, conn=self.conn,
            settings=self.settings())
        self.assertTrue(out["available"])
        self.assertIn("Good session.", out["text"])

    def test_nul_padded_oversized_json_excluded(self):
        # length(TEXT) stops at an embedded NUL, so '{}\0'+2MB would pass a
        # character-length guard; the BLOB-byte guard must exclude it.
        seed_draft(self.conn, draft_id="dwl_good", created=1000,
                   sections=[{"id": "what_happened",
                              "title": "What happened",
                              "items": ["Good session."]}])
        raw = "{}\x00" + "X" * work_log._PAYLOAD_BYTES
        self._insert_raw_draft("dwl_nul", raw, created=2000)
        out = work_log.continuation_text(
            PID, conn=self.conn,
            settings=self.settings())
        self.assertTrue(out["available"])
        self.assertIn("Good session.", out["text"])

    def test_multibyte_oversized_json_excluded_by_bytes(self):
        # Under the cap in characters but over it in UTF-8 bytes: only a
        # byte-counting guard excludes this row.
        cap = work_log._PAYLOAD_BYTES
        pad = "\u00e9" * (cap - 2000)
        blob = json.dumps({"sections": [{"id": "what_happened",
                                          "title": "What happened",
                                          "items": ["Padded session."]}],
                           "pad": pad}, ensure_ascii=False)
        self.assertLess(len(blob), cap)
        self.assertGreater(len(blob.encode("utf-8")), cap)
        self._insert_raw_draft("dwl_wide", blob, created=1000)
        out = work_log.continuation_text(
            PID, conn=self.conn,
            settings=self.settings())
        self.assertFalse(out["available"])
        self.assertEqual(out["text"], "")

    def test_sql_guards_pinned(self):
        import inspect as _inspect
        draft_src = _inspect.getsource(work_log._latest_cached_draft)
        self.assertIn("CAST(json AS BLOB)", draft_src)
        self.assertIn("CAST(session_id AS BLOB)", draft_src)
        self.assertIn("ELSE NULL END AS session_id", draft_src)
        # No label reader survives on this path.
        path_src = _inspect.getsource(work_log.continuation_text)
        self.assertNotIn("get_label", path_src)
        self.assertNotIn("session_labels", path_src)

    def test_readonly_parent_symlink_rejected_fail_soft(self):
        import os as _os
        import stat as _stat
        real = Path(self.temp.name) / "real"
        real.mkdir()
        conn = annotations.connect(str(real / "annotations.db"))
        try:
            seed_draft(conn)
        finally:
            conn.close()
        link = Path(self.temp.name) / "link"
        _os.symlink(str(real), str(link))
        self.assertIsNone(
            work_log._open_readonly_conn(str(link / "annotations.db")))
        # Fail-soft: nothing created or repaired through the link.
        self.assertFalse((link / "annotations.db-wal").exists())
        self.assertTrue((real / "annotations.db").exists())

    def test_readonly_parent_world_writable_rejected_no_repair(self):
        import os as _os
        import stat as _stat
        sub = Path(self.temp.name) / "ww"
        sub.mkdir()
        conn = annotations.connect(str(sub / "annotations.db"))
        conn.close()
        _os.chmod(sub, 0o777)
        try:
            self.assertIsNone(
                work_log._open_readonly_conn(str(sub / "annotations.db")))
            # No permission repair: mode untouched.
            self.assertEqual(_stat.S_IMODE(_os.stat(sub).st_mode), 0o777)
        finally:
            _os.chmod(sub, 0o700)

    def test_readonly_missing_parent_is_none_no_creation(self):
        missing = str(Path(self.temp.name) / "no-such-dir"
                      / "annotations.db")
        self.assertIsNone(work_log._open_readonly_conn(missing))
        self.assertFalse(Path(missing).exists())
        self.assertFalse((Path(self.temp.name) / "no-such-dir").exists())

    def test_never_generates_drafts(self):
        before = self.conn.execute(
            "SELECT COUNT(*) AS n FROM drafts").fetchone()["n"]
        work_log.continuation_text(
            PID, conn=self.conn, settings=self.settings())
        after = self.conn.execute(
            "SELECT COUNT(*) AS n FROM drafts").fetchone()["n"]
        self.assertEqual(before, after)


def run_cli(args, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        text=True, capture_output=True, timeout=30, check=False, env=env)


class ContinuationCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.db = str(self.base / "annotations.db")
        self.settings_file = str(self.base / "settings.json")
        Path(self.settings_file).write_text(
            json.dumps({"memory": {"enabled": True, "workLog": True}}),
            encoding="utf-8")
        conn = annotations.connect(self.db)
        try:
            seed_draft(conn)
        finally:
            conn.close()
        self.env = {"QS_ANNOTATIONS_DB": self.db,
                    "QUICKSHELL_SETTINGS": self.settings_file}

    def tearDown(self):
        self.temp.cleanup()

    def test_cli_success_shape(self):
        completed = run_cli(["--db", self.db, "continuation",
                             "--project", PID], env_extra=self.env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["project"], PID)
        self.assertTrue(payload["available"])
        self.assertIsInstance(payload["text"], str)
        self.assertLessEqual(len(payload["text"]), 2400)
        self.assertEqual(set(payload.keys()),
                         {"project", "available", "text"})

    def test_cli_canonicalizes_uuid(self):
        completed = run_cli(["--db", self.db, "continuation",
                             "--project", PID.upper()],
                            env_extra=self.env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["project"], PID)

    def test_cli_invalid_project_bounded(self):
        completed = run_cli(["--db", self.db, "continuation",
                             "--project", "nope"], env_extra=self.env)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("error:", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(len(completed.stderr.strip().splitlines()), 1)

    def test_cli_missing_project_bounded(self):
        completed = run_cli(["--db", self.db, "continuation"],
                            env_extra=self.env)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("error:", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_cli_missing_db_is_empty_no_creation(self):
        missing = str(self.base / "no-such-dir" / "annotations.db")
        completed = run_cli(["--db", missing, "continuation",
                             "--project", PID], env_extra=self.env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["text"], "")
        self.assertFalse(Path(missing).exists())

    def test_cli_unknown_project_is_empty(self):
        completed = run_cli(["--db", self.db, "continuation",
                             "--project", PID2], env_extra=self.env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["text"], "")

    def test_cli_disabled_settings_empty(self):
        Path(self.settings_file).write_text(
            json.dumps({"memory": {"enabled": True, "workLog": False}}),
            encoding="utf-8")
        completed = run_cli(["--db", self.db, "continuation",
                             "--project", PID], env_extra=self.env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(json.loads(completed.stdout)["available"])


def extract_function(source, name):
    start = source.index("function " + name + "(")
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError("unterminated function: " + name)


@unittest.skipUnless(shutil.which("node"), "node is required for QML JS coverage")
class ContinuationPlannerTests(unittest.TestCase):
    def run_node(self, script):
        completed = subprocess.run(["node", "-e", script], text=True,
                                   capture_output=True)
        if completed.returncode:
            raise AssertionError(completed.stderr)
        return json.loads(completed.stdout)

    def test_base_and_bounded_append(self):
        fns = {n: extract_function(PLANNER, n)
               for n in ("resumeContinuationText",)}
        script = f"""
const vm = require("vm");
const context = {{
  projectName(p) {{ return (p && p.name) || ""; }},
}};
context.root = context;
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
const base = context.resumeContinuationText({{id: "p1", name: "Demo"}});
if (!base.includes("Demo")) throw new Error("base lost project name: " + base);
const enriched = context.resumeContinuationText({{id: "p1", name: "Demo"}}, "Last session activity: coding.");
if (!enriched.startsWith(base) || !enriched.includes("coding")) throw new Error("append broken: " + enriched);
const huge = context.resumeContinuationText({{id: "p1", name: "Demo"}}, "y".repeat(5000));
if (huge.length > base.length + 2 + 2400) throw new Error("cached text unbounded: " + huge.length);
const dirty = context.resumeContinuationText({{id: "p1", name: "Demo"}}, "a\\u0000b\\u0001c");
if (dirty.includes("\\u0000")) throw new Error("controls leaked");
console.log(JSON.stringify({{base, len: huge.length}}));
"""
        value = self.run_node(script)
        self.assertIn("Demo", value["base"])

    def test_start_guards_and_argv(self):
        fns = {n: extract_function(PLANNER, n)
               for n in ("startResumeContinuation", "continuationUuid")}
        script = f"""
const vm = require("vm");
const context = {{
  continuationBusy: false, continuationRetiring: false,
  continuationGeneration: 0, continuationProcessGeneration: 0,
  continuationLaunchGeneration: 0, continuationProjectId: "",
  continuationPath: "", continuationInteraction: -1, continuationBase: "",
  continuationStartFailed: false, continuationStarted: false,
  continuationBusy: false, requestedOpen: true, closing: false,
  activeTab: "projects", interactionGeneration: 3,
}};
context.root = context;
context.Quickshell = {{ shellPath(v) {{ return "/qs/" + v; }} }};
context.continuationProcess = {{ running: false, command: [], stdinEnabled: true }};
context.continuationTimeout = {{ restarted: 0, restart() {{ this.restarted++; }} }};
vm.createContext(context);
for (const value of {json.dumps(list(fns.values()))}) {{
  const fn = vm.runInContext("(" + value + ")", context);
  context[fn.name] = fn;
}}
const out = {{}};
out.badUuid = context.startResumeContinuation("nope", "pages/A.md", "base");
out.badPath = context.startResumeContinuation("{PID}", "", "base");
out.badBase = context.startResumeContinuation("{PID}", "pages/A.md", "");
if (out.badUuid || out.badPath || out.badBase) throw new Error("guard failed: " + JSON.stringify(out));
if (!context.startResumeContinuation("{PID}", "pages/A.md", "base text")) throw new Error("valid start refused");
out.cmd = context.continuationProcess.command.join(" ");
out.busy = context.continuationBusy;
out.stored = [context.continuationProjectId, context.continuationPath, context.continuationBase];
context.continuationBusy = true;
out.secondBlocked = context.startResumeContinuation("{PID}", "pages/A.md", "base text");
console.log(JSON.stringify(out));
"""
        value = self.run_node(script)
        self.assertIn("continuation", value["cmd"])
        self.assertIn(PID, value["cmd"])
        self.assertIn("work_log.py", value["cmd"])
        self.assertTrue(value["busy"])
        self.assertEqual(value["stored"],
                         [PID, "pages/A.md", "base text"])
        self.assertFalse(value["secondBlocked"])

    def test_finish_installs_only_on_exact_base_idle(self):
        fns = {n: extract_function(PLANNER, n)
               for n in ("finishResumeContinuation",
                         "validContinuationPayload",
                         "resumeContinuationText")}
        script = f"""
const vm = require("vm");
const FNS = {json.dumps(list(fns.values()))};
function runCase(over, code, output, generation) {{
  const r = {{
    continuationGeneration: 5, continuationBusy: true,
    continuationProjectId: "{PID}",
    continuationPath: "pages/A.md", continuationInteraction: 3,
    continuationBase: "BASE", continuationDraftRevision: 7,
    draftRevision: 7,
    requestedOpen: true, closing: false, activeTab: "projects",
    interactionGeneration: 3, selectedProjectId: "{PID}",
    selectedPath: "pages/A.md",
    pageBusy: false, pageRetiring: false, toggleBusy: false,
    toggleRetiring: false, sendBusy: false,
    continuationTimeout: {{ stop() {{}} }},
    drafts: {{"pages/A.md": "BASE"}},
    projects: [{{id: "{PID}", name: "Demo", logseq_path: "pages/A.md"}}],
  }};
  Object.assign(r, over || {{}});
  // Deep-merge nested draft maps so overrides replace them wholesale.
  if (over && over.drafts) r.drafts = over.drafts;
  r.root = r;
  r.hasBusyAgent = () => false;
  r.approvalOpen = () => false;
  r.projectById = (id) => (r.projects || []).find(p => p.id === id) || null;
  r.projectName = (p) => (p && p.name) || "";
  r.draftFor = (p) => r.drafts[p] || "";
  r.setDraft = (p, v) => {{ r.drafts[p] = v; r.draftRevision = Number(r.draftRevision || 0) + 1; }};
  vm.createContext(r);
  for (const value of FNS) {{
    const fn = vm.runInContext("(" + value + ")", r);
    r[fn.name] = fn.bind(r);
  }}
  r.finishResumeContinuation(code, output, "", generation);
  return JSON.stringify(r.drafts);
}}
const out = {{}};
const payload = JSON.stringify({{project: "{PID}", available: true, text: "Last session activity: coding."}});
const badPayload = JSON.stringify({{project: "{PID}", available: false, text: ""}});
const BASE_DRAFTS = JSON.stringify({{"pages/A.md": "BASE"}});
const cases = {{
// Happy path installs base + enrichment (checked separately below).
happy: [runCase({{}}, 0, payload, 5), null],
// Edited draft is never overwritten.
edited: [runCase({{drafts: {{"pages/A.md": "BASE user edit"}}}}, 0, payload, 5), JSON.stringify({{"pages/A.md": "BASE user edit"}})],
// Sent draft (cleared) is never resurrected.
sent: [runCase({{drafts: {{"pages/A.md": ""}}}}, 0, payload, 5), JSON.stringify({{"pages/A.md": ""}})],
// Stale project dropped.
staleProject: [runCase({{selectedProjectId: "{PID2}"}}, 0, payload, 5), BASE_DRAFTS],
// Stale interaction dropped.
staleInteraction: [runCase({{interactionGeneration: 4}}, 0, payload, 5), BASE_DRAFTS],
// Busy send blocks.
busySend: [runCase({{sendBusy: true}}, 0, payload, 5), BASE_DRAFTS],
// Failure code / unavailable / bad JSON preserve baseline.
failCode: [runCase({{}}, 1, payload, 5), BASE_DRAFTS],
badJson: [runCase({{}}, 0, "not json", 5), BASE_DRAFTS],
unavailable: [runCase({{}}, 0, badPayload, 5), BASE_DRAFTS],
// ABA restore (edit reverted verbatim) still counts as changed.
aba: [runCase({{drafts: {{"pages/A.md": "BASE"}}, draftRevision: 9}}, 0, payload, 5), BASE_DRAFTS],
// Sent draft cleared then retyped verbatim still counts as changed.
sendRestore: [runCase({{drafts: {{"pages/A.md": "BASE"}}, draftRevision: 12}}, 0, payload, 5), BASE_DRAFTS],
// A->B->A: delayed A result after moving to B never installs on B.
movedToB: [runCase({{selectedProjectId: "{PID2}", selectedPath: "pages/B.md",
  drafts: {{"pages/A.md": "BASE", "pages/B.md": ""}}}}, 0, payload, 5),
  JSON.stringify({{"pages/A.md": "BASE", "pages/B.md": ""}})],
// Closed planner drops the delayed result.
closed: [runCase({{requestedOpen: false}}, 0, payload, 5), BASE_DRAFTS],
// Stale generation dropped.
staleGen: [runCase({{}}, 0, payload, 4), BASE_DRAFTS],
}};
for (const [name, [actual, expected]] of Object.entries(cases)) {{
  if (name === "happy") {{
    if (!String(actual).includes("coding")) throw new Error("happy path did not install: " + actual);
    out.happy = actual;
  }} else if (String(actual) !== String(expected)) {{
    throw new Error(name + " mismatch: got " + JSON.stringify(actual));
  }}
  if ((name === "aba" || name === "sendRestore") && String(actual).includes("coding"))
    throw new Error(name + " overwrote despite revision latch");
}}
console.log(JSON.stringify({{happy: out.happy}}));
"""
        value = self.run_node(script)
        self.assertIn("coding", value["happy"])

    def test_retirement_latch_blocks_relaunch_until_exit(self):
        fns = {n: extract_function(PLANNER, n)
               for n in ("startResumeContinuation",
                         "invalidateResumeContinuation",
                         "cancelResumeContinuation", "continuationUuid")}
        script = f"""
const vm = require("vm");
const FNS = {json.dumps(list(fns.values()))};
function makeSandbox(running, over) {{
  const r = {{
    continuationBusy: true, continuationStarted: true,
    continuationStartFailed: false, continuationRetiring: false,
    continuationGeneration: 5, continuationProcessGeneration: 5,
    continuationLaunchGeneration: 5, continuationProjectId: "{PID}",
    continuationPath: "pages/A.md", continuationInteraction: 3,
    continuationBase: "BASE", draftRevision: 7,
    continuationDraftRevision: 7,
    requestedOpen: true, closing: false, activeTab: "projects",
    interactionGeneration: 3,
    continuationTimeout: {{ stop() {{}}, restart() {{}} }},
  }};
  Object.assign(r, over || {{}});
  r.root = r;
  r.Quickshell = {{ shellPath(v) {{ return "/qs/" + v; }} }};
  r.continuationProcess = {{ running: running, command: [], stdinEnabled: false }};
  vm.createContext(r);
  for (const value of FNS) {{
    const fn = vm.runInContext("(" + value + ")", r);
    r[fn.name] = fn.bind(r);
  }}
  return r;
}}
const out = {{}};
// Idle invalidate clears the latch and bumps the generation.
let idle = makeSandbox(false, {{continuationBusy: false, continuationStarted: false}});
idle.invalidateResumeContinuation();
out.idleRetiring = idle.continuationRetiring;
out.idleGen = idle.continuationGeneration;
// In-flight invalidate keeps retiring until the exit is consumed.
let flight = makeSandbox(true);
const genBefore = flight.continuationGeneration;
flight.invalidateResumeContinuation();
out.flightRetiring = flight.continuationRetiring;
out.flightGenBumped = flight.continuationGeneration > genBefore;
// Relaunch while retiring is prohibited (timeout->retry / close-reopen gap).
out.relaunchBlocked = flight.startResumeContinuation("{PID}", "pages/A.md", "BASE");
// Timeout path also retires and blocks relaunch.
let busy = makeSandbox(false);
busy.continuationBusy = true;
busy.continuationStarted = false;
busy.continuationProcess.running = true;
busy.cancelResumeContinuation();
out.timeoutRetiring = busy.continuationRetiring;
out.timeoutBlocked = busy.startResumeContinuation("{PID}", "pages/A.md", "BASE");
// Gap: process already stopped but started/busy still set (onExited
// pending) — invalidate must preserve the latch, not clear it.
let gap = makeSandbox(false, {{continuationBusy: true, continuationStarted: true}});
const gapGen = gap.continuationGeneration;
gap.invalidateResumeContinuation();
out.gapRetiring = gap.continuationRetiring;
out.gapGenBumped = gap.continuationGeneration > gapGen;
out.gapBlocked = gap.startResumeContinuation("{PID}", "pages/A.md", "BASE");
// Second close after a timeout: retiring already stands with busy/started
// clear — the latch must survive, and the generation must still bump.
let second = makeSandbox(false, {{continuationBusy: false, continuationStarted: false,
  continuationRetiring: true}});
const secondGen = second.continuationGeneration;
second.invalidateResumeContinuation();
out.secondRetiring = second.continuationRetiring;
out.secondGenBumped = second.continuationGeneration > secondGen;
out.secondBlocked = second.startResumeContinuation("{PID}", "pages/A.md", "BASE");
console.log(JSON.stringify(out));
"""
        value = self.run_node(script)
        self.assertFalse(value["idleRetiring"])
        self.assertEqual(value["idleGen"], 6)
        self.assertTrue(value["flightRetiring"])
        self.assertTrue(value["flightGenBumped"])
        self.assertFalse(value["relaunchBlocked"])
        self.assertTrue(value["timeoutRetiring"])
        self.assertFalse(value["timeoutBlocked"])
        self.assertTrue(value["gapRetiring"])
        self.assertTrue(value["gapGenBumped"])
        self.assertFalse(value["gapBlocked"])
        self.assertTrue(value["secondRetiring"])
        self.assertTrue(value["secondGenBumped"])
        self.assertFalse(value["secondBlocked"])

    def test_stale_completion_never_stops_newer_timer(self):
        fns = {n: extract_function(PLANNER, n)
               for n in ("finishResumeContinuation",
                         "handleContinuationStartFailure",
                         "validContinuationPayload",
                         "resumeContinuationText")}
        script = f"""
const vm = require("vm");
const FNS = {json.dumps(list(fns.values()))};
const r = {{
  // Newer flight (generation 6) with its watchdog running.
  continuationGeneration: 6, continuationBusy: true,
  continuationProjectId: "{PID}", continuationPath: "pages/A.md",
  continuationInteraction: 3, continuationBase: "BASE",
  continuationDraftRevision: 7, draftRevision: 7,
  requestedOpen: true, closing: false, activeTab: "projects",
  interactionGeneration: 3, selectedProjectId: "{PID}",
  selectedPath: "pages/A.md",
  pageBusy: false, pageRetiring: false, toggleBusy: false,
  toggleRetiring: false, sendBusy: false,
  continuationTimeout: {{ stops: 0, stop() {{ this.stops++; }} }},
  drafts: {{"pages/A.md": "BASE"}},
  projects: [{{id: "{PID}", name: "Demo", logseq_path: "pages/A.md"}}],
}};
r.root = r;
r.hasBusyAgent = () => false;
r.approvalOpen = () => false;
r.projectById = (id) => (r.projects || []).find(p => p.id === id) || null;
r.projectName = (p) => (p && p.name) || "";
r.draftFor = (p) => r.drafts[p] || "";
r.setDraft = (p, v) => {{ r.drafts[p] = v; }};
vm.createContext(r);
for (const value of FNS) {{
  const fn = vm.runInContext("(" + value + ")", r);
  r[fn.name] = fn.bind(r);
}}
const out = {{}};
const payload = JSON.stringify({{project: "{PID}", available: true, text: "Late."}});
// Stale finish (generation 5) touches neither the timer nor the latch.
r.finishResumeContinuation(0, payload, "", 5);
out.staleStops = r.continuationTimeout.stops;
out.staleBusy = r.continuationBusy;
out.staleDraft = r.drafts["pages/A.md"];
// Stale start-failure likewise leaves the newer timer alone.
r.handleContinuationStartFailure(5);
out.staleFailStops = r.continuationTimeout.stops;
console.log(JSON.stringify(out));
"""
        value = self.run_node(script)
        self.assertEqual(value["staleStops"], 0)
        self.assertTrue(value["staleBusy"])
        self.assertEqual(value["staleDraft"], "BASE")
        self.assertEqual(value["staleFailStops"], 0)

    def test_finish_strict_mode_success(self):
        # QML runs JavaScript in strict mode: any assignment to an
        # undeclared variable throws. Compile the planner functions
        # strictly and run the happy path to prove all locals declared.
        fns = {n: extract_function(PLANNER, n)
               for n in ("finishResumeContinuation",
                         "validContinuationPayload",
                         "resumeContinuationText")}
        script = f"""
const vm = require("vm");
const FNS = {json.dumps(list(fns.values()))};
const r = {{
  continuationGeneration: 5, continuationBusy: true,
  continuationProjectId: "{PID}",
  continuationPath: "pages/A.md", continuationInteraction: 3,
  continuationBase: "BASE", continuationDraftRevision: 7,
  draftRevision: 7,
  requestedOpen: true, closing: false, activeTab: "projects",
  interactionGeneration: 3, selectedProjectId: "{PID}",
  selectedPath: "pages/A.md",
  pageBusy: false, pageRetiring: false, toggleBusy: false,
  toggleRetiring: false, sendBusy: false,
  continuationTimeout: {{ stop() {{}} }},
  drafts: {{"pages/A.md": "BASE"}},
  projects: [{{id: "{PID}", name: "Demo", logseq_path: "pages/A.md"}}],
}};
r.root = r;
r.hasBusyAgent = () => false;
r.approvalOpen = () => false;
r.projectById = (id) => (r.projects || []).find(p => p.id === id) || null;
r.projectName = (p) => (p && p.name) || "";
r.draftFor = (p) => r.drafts[p] || "";
r.setDraft = (p, v) => {{ r.drafts[p] = v; }};
vm.createContext(r);
for (const value of FNS) {{
  const fn = vm.runInContext('"use strict";(' + value + ')', r);
  r[fn.name] = fn.bind(r);
}}
const payload = JSON.stringify({{project: "{PID}", available: true, text: "Last session activity: coding."}});
r.finishResumeContinuation(0, payload, "", 5);
if (!String(r.drafts["pages/A.md"]).includes("coding"))
  throw new Error("strict-mode happy path did not install: " + r.drafts["pages/A.md"]);
console.log(JSON.stringify({{draft: r.drafts["pages/A.md"]}}));
"""
        value = self.run_node(script)
        self.assertIn("coding", value["draft"])

    def test_finish_installs_during_display_page_read(self):
        # The different-project Ask reselect starts a display-only page
        # read before the lookup returns; pageBusy/pageRetiring must not
        # drop the enrichment, and finish must never send or read pages.
        finish_src = extract_function(PLANNER, "finishResumeContinuation")
        self.assertNotIn("startPage(", finish_src)
        self.assertNotIn(".prompt(", finish_src)
        self.assertNotIn("send(", finish_src)
        fns = {n: extract_function(PLANNER, n)
               for n in ("finishResumeContinuation",
                         "validContinuationPayload",
                         "resumeContinuationText")}
        script = f"""
const vm = require("vm");
const FNS = {json.dumps(list(fns.values()))};
function runCase(over) {{
  const r = {{
    continuationGeneration: 5, continuationBusy: true,
    continuationProjectId: "{PID}",
    continuationPath: "pages/A.md", continuationInteraction: 3,
    continuationBase: "BASE", continuationDraftRevision: 7,
    draftRevision: 7,
    requestedOpen: true, closing: false, activeTab: "projects",
    interactionGeneration: 3, selectedProjectId: "{PID}",
    selectedPath: "pages/A.md",
    pageBusy: false, pageRetiring: false, toggleBusy: false,
    toggleRetiring: false, sendBusy: false,
    continuationTimeout: {{ stop() {{}} }},
    drafts: {{"pages/A.md": "BASE"}},
    projects: [{{id: "{PID}", name: "Demo", logseq_path: "pages/A.md"}}],
  }};
  Object.assign(r, over || {{}});
  if (over && over.drafts) r.drafts = over.drafts;
  r.root = r;
  r.hasBusyAgent = () => false;
  r.approvalOpen = () => false;
  r.projectById = (id) => (r.projects || []).find(p => p.id === id) || null;
  r.projectName = (p) => (p && p.name) || "";
  r.draftFor = (p) => r.drafts[p] || "";
  r.setDraft = (p, v) => {{ r.drafts[p] = v; r.draftRevision = Number(r.draftRevision || 0) + 1; }};
  vm.createContext(r);
  for (const value of FNS) {{
    const fn = vm.runInContext("(" + value + ")", r);
    r[fn.name] = fn.bind(r);
  }}
  const payload = JSON.stringify({{project: "{PID}", available: true, text: "Last session activity: coding."}});
  r.finishResumeContinuation(0, payload, "", 5);
  return r.drafts["pages/A.md"];
}}
const out = {{}};
out.pageBusy = runCase({{pageBusy: true}});
out.pageRetiring = runCase({{pageRetiring: true}});
// Mutation/send/agent gates still hold.
out.toggleBusy = runCase({{toggleBusy: true}});
console.log(JSON.stringify(out));
"""
        value = self.run_node(script)
        self.assertIn("coding", value["pageBusy"])
        self.assertIn("coding", value["pageRetiring"])
        self.assertEqual(value["toggleBusy"], "BASE")

    def test_failed_start_after_cancel_releases_latch(self):
        # Pending launch -> invalidate (or timeout) -> Quickshell failed
        # startup emits runningChanged but NO onExited: the latch must
        # release so a retry can launch. A started flight keeps its latch.
        fns = {n: extract_function(PLANNER, n)
               for n in ("handleProcessRunningChanged",
                         "handleContinuationStartFailure",
                         "startResumeContinuation", "continuationUuid")}
        script = f"""
const vm = require("vm");
const FNS = {json.dumps(list(fns.values()))};
function makeSandbox(over) {{
  const r = {{
    continuationBusy: false, continuationStarted: false,
    continuationStartFailed: false, continuationRetiring: true,
    continuationGeneration: 6, continuationProcessGeneration: 6,
    continuationLaunchGeneration: 6, continuationProjectId: "{PID}",
    continuationPath: "pages/A.md", continuationInteraction: 3,
    continuationBase: "BASE", draftRevision: 7,
    continuationDraftRevision: 7,
    requestedOpen: true, closing: false, activeTab: "projects",
    interactionGeneration: 3,
    continuationTimeout: {{ stop() {{}}, restart() {{}} }},
  }};
  Object.assign(r, over || {{}});
  r.root = r;
  r.Quickshell = {{ shellPath(v) {{ return "/qs/" + v; }} }};
  r.continuationProcess = {{ running: false, command: [], stdinEnabled: false }};
  vm.createContext(r);
  for (const value of FNS) {{
    const fn = vm.runInContext("(" + value + ")", r);
    r[fn.name] = fn.bind(r);
  }}
  return r;
}}
const out = {{}};
// Cancelled never-started flight: failed-startup runningChanged (no exit
// will ever follow) releases the latch, then a retry launches.
let cancelled = makeSandbox({{}});
cancelled.handleProcessRunningChanged("continuation");
out.released = cancelled.continuationRetiring;
out.retry = cancelled.startResumeContinuation("{PID}", "pages/A.md", "BASE");
out.retryCmd = cancelled.continuationProcess.command.join(" ");
// Started flight keeps its latch until onExited (no exit yet).
let started = makeSandbox({{continuationStarted: true, continuationBusy: true}});
started.handleProcessRunningChanged("continuation");
out.startedHeld = started.continuationRetiring;
out.startedBlocked = started.startResumeContinuation("{PID}", "pages/A.md", "BASE");
console.log(JSON.stringify(out));
"""
        value = self.run_node(script)
        self.assertFalse(value["released"])
        self.assertTrue(value["retry"])
        self.assertIn("continuation", value["retryCmd"])
        self.assertTrue(value["startedHeld"])
        self.assertFalse(value["startedBlocked"])

    def test_send_invalidates_continuation(self):
        self.assertIn("invalidateResumeContinuation",
                      extract_function(PLANNER, "send"))
        self.assertIn("invalidateResumeContinuation",
                      extract_function(PLANNER, "sendWithoutNote"))

    def test_no_autosend_markers(self):
        for name in ("consumePendingOpenProject", "startResumeContinuation",
                     "finishResumeContinuation"):
            text = extract_function(PLANNER, name)
            self.assertNotIn(".prompt(", text)
            self.assertNotIn("createObject", text)
            self.assertNotIn("worker.start", text)
        consume = extract_function(PLANNER, "consumePendingOpenProject")
        self.assertNotIn("send()", consume)
        self.assertIn("setDraft", consume)
        # Ask branches trigger enrichment; history branches must not. Scope
        # to the already-selected region (the function handles both the
        # already-selected and the different-project paths).
        selected_region = consume[consume.index("if (alreadySelected)"):
                                  consume.index("// Different project")]
        ask_block = selected_region[selected_region.index('if (act === "ask")'):
                                    selected_region.index('} else if (act === "history")')]
        self.assertIn("startResumeContinuation", ask_block)
        history_block = selected_region[selected_region.index('} else if (act === "history")'):]
        self.assertNotIn("startResumeContinuation", history_block)
        # Exactly the two Ask branches call it (already-selected + reselect).
        self.assertEqual(consume.count("startResumeContinuation"), 2)
        self.assertIn("continuationTimeout", PLANNER)
        self.assertIn("continuationRetiring", PLANNER)
        self.assertIn("invalidateResumeContinuation", PLANNER)
        close_start = PLANNER.rindex("    function close() {")
        self.assertIn("invalidateResumeContinuation",
                      PLANNER[close_start:close_start + 2000])


if __name__ == "__main__":
    unittest.main()
