"""Settings-driven Logseq graph resolution.

The graph directory differs per machine, so it must come from
``--graph`` > ``LOGSEQ_GRAPH`` > ``settings.json`` (see
``scripts/quickshell_settings.py``) instead of a hardwired path.
"""
import sys as _sys
_sys.dont_write_bytecode = True

import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
SCRIPTS = REPO_ROOT / "scripts"
EXTENSION = (REPO_ROOT / ".pi" / "extensions" / "desktop-agent.ts").read_text(encoding="utf-8")
PLANNER_QML = (REPO_ROOT / "widgets" / "ProjectPlanner.qml").read_text(encoding="utf-8")
AGENDA_QML = (REPO_ROOT / "widgets" / "DailyAgenda.qml").read_text(encoding="utf-8")
DATASOURCES_QML = (REPO_ROOT / "widgets" / "PaletteDataSources.qml").read_text(encoding="utf-8")

HARDWIRED = "/home/franzs/Nextcloud/Documents/Notes"

sys.path.insert(0, str(SCRIPTS))
import quickshell_settings as settings
from logseq_common import GraphError, resolve_graph


class SettingsLoaderTests(unittest.TestCase):
    def test_graph_keys_and_nested_logseq_object(self):
        self.assertEqual(settings.settings_graph({"logseqGraph": "/g"}), "/g")
        self.assertEqual(settings.settings_graph({"logseq_graph": "/g"}), "/g")
        self.assertEqual(settings.settings_graph({"LOGSEQ_GRAPH": "/g"}), "/g")
        self.assertEqual(settings.settings_graph({"logseq": {"graph": "/g"}}), "/g")
        self.assertIsNone(settings.settings_graph({}))
        self.assertIsNone(settings.settings_graph({"logseqGraph": "   "}))

    def test_blank_values_count_as_missing(self):
        env = dict(os.environ)
        env.pop("LOGSEQ_GRAPH", None)
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            os.environ.update({k: v for k, v in env.items() if k in ("PATH",)})
            # Blank explicit falls through to settings instead of winning.
            self.assertEqual(settings.resolve_graph_raw("", {"logseqGraph": "/from-settings"}),
                             "/from-settings")
            self.assertEqual(settings.resolve_graph_raw("   ", {"logseqGraph": "/from-settings"}),
                             "/from-settings")
            self.assertIsNone(settings.resolve_graph_raw("", {}))
            self.assertIsNone(settings.resolve_graph_raw("   ", {}))

    def test_precedence_is_explicit_then_env_then_settings(self):
        with unittest.mock.patch.dict(os.environ, {"LOGSEQ_GRAPH": "/from-env"}, clear=False):
            self.assertEqual(settings.resolve_graph_raw("/from-cli", {"logseqGraph": "/from-settings"}),
                             "/from-cli")
            self.assertEqual(settings.resolve_graph_raw(None, {"logseqGraph": "/from-settings"}),
                             "/from-env")
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            # os.environ.clear removes everything; restore PATH for safety below.
            os.environ["PATH"] = "/usr/bin:/bin"
            self.assertEqual(settings.resolve_graph_raw(None, {"logseqGraph": "/from-settings"}),
                             "/from-settings")
            self.assertIsNone(settings.resolve_graph_raw(None, {}))

    def test_quickshell_settings_override_selects_alternate_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.json"
            first.write_text(json.dumps({"logseqGraph": "/first"}), encoding="utf-8")
            with unittest.mock.patch.dict(os.environ, {"QUICKSHELL_SETTINGS": str(first)}, clear=False):
                self.assertEqual(settings.load_settings()["logseqGraph"], "/first")
                self.assertEqual(settings.resolve_graph_raw(None), "/first")

    def test_missing_and_invalid_files_behave_like_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            self.assertEqual(settings.load_settings(missing), {})
            broken = Path(tmp) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            self.assertEqual(settings.load_settings(broken), {})
            listed = Path(tmp) / "list.json"
            listed.write_text("[]", encoding="utf-8")
            self.assertEqual(settings.load_settings(listed), {})

    def test_get_setting_returns_default_for_unknown_keys(self):
        self.assertEqual(settings.get_setting("nope", "dflt", {"other": 1}), "dflt")
        self.assertEqual(settings.get_setting("logseqGraph", "dflt", {"logseqGraph": "/g"}), "/g")


class ResolveGraphTests(unittest.TestCase):
    def test_resolve_graph_prefers_explicit_then_env_then_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph = Path(tmp) / "graph"
            (graph / "pages").mkdir(parents=True)
            (graph / "journals").mkdir()
            other = Path(tmp) / "other"
            (other / "pages").mkdir(parents=True)
            (other / "journals").mkdir()
            cfg = Path(tmp) / "settings.json"
            cfg.write_text(json.dumps({"logseqGraph": str(graph)}), encoding="utf-8")
            with unittest.mock.patch.dict(os.environ, {"LOGSEQ_GRAPH": str(other),
                                                       "QUICKSHELL_SETTINGS": str(cfg)}, clear=False):
                self.assertEqual(resolve_graph(str(graph)), graph.resolve())
                self.assertEqual(resolve_graph(""), other.resolve())
                self.assertEqual(resolve_graph(None), other.resolve())
            with unittest.mock.patch.dict(os.environ, {"QUICKSHELL_SETTINGS": str(cfg)}, clear=False):
                env = dict(os.environ)
                env.pop("LOGSEQ_GRAPH", None)
                with unittest.mock.patch.dict(os.environ, {}, clear=True):
                    os.environ.update({k: v for k, v in env.items()
                                       if k in ("PATH", "QUICKSHELL_SETTINGS", "HOME", "XDG_CONFIG_HOME")})
                    os.environ["QUICKSHELL_SETTINGS"] = str(cfg)
                    self.assertEqual(resolve_graph(None), graph.resolve())
                    self.assertEqual(resolve_graph("  "), graph.resolve())

    def test_resolve_graph_errors_clearly_when_unconfigured(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty.json"
            empty.write_text("{}", encoding="utf-8")
            with unittest.mock.patch.dict(os.environ, {"QUICKSHELL_SETTINGS": str(empty)}, clear=False):
                env = dict(os.environ)
                env.pop("LOGSEQ_GRAPH", None)
                with unittest.mock.patch.dict(os.environ, {}, clear=True):
                    os.environ.update({k: v for k, v in env.items()
                                       if k in ("PATH", "QUICKSHELL_SETTINGS")})
                    os.environ["QUICKSHELL_SETTINGS"] = str(empty)
                    with self.assertRaises(GraphError) as ctx:
                        resolve_graph(None)
                    self.assertIn("settings.json", str(ctx.exception))

    def test_helpers_resolve_via_settings_without_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph = Path(tmp) / "Notes"
            (graph / "pages").mkdir(parents=True)
            (graph / "journals").mkdir()
            (graph / "pages" / "Work.md").write_text("- TODO ship it\n", encoding="utf-8")
            cfg = Path(tmp) / "settings.json"
            cfg.write_text(json.dumps({"logseqGraph": str(graph)}), encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k != "LOGSEQ_GRAPH"}
            env["QUICKSHELL_SETTINGS"] = str(cfg)
            env["PYTHONPATH"] = str(SCRIPTS) + os.pathsep + env.get("PYTHONPATH", "")
            # logseq_todos with an empty positional arg falls through to settings.
            out = subprocess.run([sys.executable, str(SCRIPTS / "logseq_todos.py"), ""],
                                 capture_output=True, text=True, env=env, cwd=str(REPO_ROOT))
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("ship it", out.stdout)
            # project_planner list without --graph falls through to settings.
            out = subprocess.run([sys.executable, str(SCRIPTS / "project_planner.py"), "list"],
                                 capture_output=True, text=True, env=env, cwd=str(REPO_ROOT))
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("Work", out.stdout)


class NoHardwiredPathTests(unittest.TestCase):
    def test_python_helpers_have_no_hardwired_default(self):
        for name in ("logseq_graph.py", "project_sessions.py", "project_planner.py",
                     "journal_assistant.py", "daily_agenda.py", "logseq_todos.py"):
            source = (SCRIPTS / name).read_text(encoding="utf-8")
            self.assertNotIn(HARDWIRED, source, name)
        for name in ("logseq_graph.py", "project_planner.py",
                     "journal_assistant.py", "daily_agenda.py"):
            source = (SCRIPTS / name).read_text(encoding="utf-8")
            self.assertIn("settings.json", source, name)

    def test_qml_has_no_hardwired_graph_path(self):
        for name, source in (("ProjectPlanner.qml", PLANNER_QML),
                             ("DailyAgenda.qml", AGENDA_QML),
                             ("PaletteDataSources.qml", DATASOURCES_QML)):
            self.assertNotIn(HARDWIRED, source, name)
        self.assertIn('Quickshell.env("LOGSEQ_GRAPH") || ""', PLANNER_QML)
        self.assertIn("function graphArg()", AGENDA_QML)
        self.assertIn("settings.json", AGENDA_QML + PLANNER_QML + DATASOURCES_QML)

    def test_extension_resolves_graph_from_settings(self):
        self.assertNotIn(HARDWIRED, EXTENSION)
        self.assertIn("settings.json", EXTENSION)
        self.assertIn("function resolveGraph", EXTENSION)
        self.assertIn("logseqGraph", EXTENSION)
        self.assertIn("QUICKSHELL_SETTINGS", EXTENSION)

    def test_settings_template_and_ignore(self):
        example = json.loads((REPO_ROOT / "settings.example.json").read_text(encoding="utf-8"))
        self.assertTrue(example.get("logseqGraph"))
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("settings.json", gitignore)

    def test_settings_template_memory_block_matches_plan_defaults(self):
        example = json.loads((REPO_ROOT / "settings.example.json").read_text(encoding="utf-8"))
        memory = example.get("memory")
        self.assertIsInstance(memory, dict)
        self.assertEqual(memory, {
            "enabled": True,
            "tickSeconds": 60,
            "workLog": True,
            "sessionCapture": True,
            "dailyReview": True,
            "organise": False,
            "minSessionMs": 300000,
            "reviewTime": "18:00",
            "morningTime": "07:00",
        })


class MemorySettingsTests(unittest.TestCase):
    def test_defaults_when_missing_or_malformed(self):
        defaults = settings.memory_settings({})
        self.assertTrue(defaults["enabled"])
        self.assertEqual(defaults["tickSeconds"], 60)
        self.assertTrue(defaults["workLog"])
        self.assertTrue(defaults["sessionCapture"])
        self.assertTrue(defaults["dailyReview"])
        self.assertFalse(defaults["organise"])
        self.assertEqual(defaults["minSessionMs"], 300000)
        self.assertEqual(defaults["reviewTime"], "18:00")
        self.assertEqual(defaults["morningTime"], "07:00")
        self.assertEqual(set(defaults.keys()),
                         {"enabled", "tickSeconds", "workLog",
                          "sessionCapture", "dailyReview", "organise",
                          "minSessionMs", "reviewTime", "morningTime"})
        # Non-dict memory blocks and non-dict settings fail closed.
        self.assertEqual(settings.memory_settings({"memory": None})["tickSeconds"], 60)
        self.assertEqual(settings.memory_settings({"memory": []})["tickSeconds"], 60)
        self.assertEqual(settings.memory_settings([])["tickSeconds"], 60)

    def test_deleted_blocks_ignored(self):
        value = settings.memory_settings({"memory": {"enabled": False}})
        self.assertFalse(value["enabled"])
        # Deleted Jev/ledger/polish keys never surface.
        crowded = settings.memory_settings({"memory": {
            "jev": {"model": "x"}, "ledger": {"autoIgnore": True},
            "workLogPolish": True, "sessionEnrichment": True,
            "associationSuggestions": True, "reviewPolish": True,
            "reviewPrioritization": True}})
        for gone in ("jev", "ledger", "workLogPolish",
                     "sessionEnrichment", "associationSuggestions",
                     "reviewPolish", "reviewPrioritization"):
            self.assertNotIn(gone, crowded)
        self.assertEqual(value["tickSeconds"], 60)

    def test_strict_bool_typing_never_coerces(self):
        for key in ("enabled", "workLog", "sessionCapture",
                    "dailyReview", "organise"):
            default = settings.memory_settings({})[key]
            for bad in (0, 1, "true", "false", "", None, [], {}):
                value = settings.memory_settings({"memory": {key: bad}})
                self.assertIs(value[key], default, (key, bad))

    def test_integer_clamping(self):
        self.assertEqual(
            settings.memory_settings({"memory": {"tickSeconds": 5}})["tickSeconds"], 10)
        self.assertEqual(
            settings.memory_settings({"memory": {"tickSeconds": 99999}})["tickSeconds"], 3600)
        self.assertEqual(
            settings.memory_settings({"memory": {"minSessionMs": -1}})["minSessionMs"], 0)
        self.assertEqual(
            settings.memory_settings({"memory": {"minSessionMs": 10 ** 12}})["minSessionMs"],
            86400000)
        # Bool is not an int (type(True) is bool): falls back to default.
        self.assertEqual(
            settings.memory_settings({"memory": {"tickSeconds": True}})["tickSeconds"], 60)
        self.assertEqual(
            settings.memory_settings({"memory": {"tickSeconds": "60"}})["tickSeconds"], 60)

    def test_hhmm_validation(self):
        self.assertEqual(
            settings.memory_settings({"memory": {"reviewTime": "07:30"}})["reviewTime"],
            "07:30")
        for bad in ("7:30", "24:00", "18:60", "evening", "", None, 1800, "  "):
            self.assertEqual(
                settings.memory_settings({"memory": {"reviewTime": bad}})["reviewTime"],
                "18:00", bad)
            self.assertEqual(
                settings.memory_settings({"memory": {"morningTime": bad}})["morningTime"],
                "07:00", bad)


if __name__ == "__main__":
    unittest.main()
