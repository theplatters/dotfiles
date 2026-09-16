"""XPI/manifest contract for the QS Active Reader Zotero plugin.

Zotero 10 rejects an XPI whose manifest omits any of
``applications.zotero.{id,update_url,strict_max_version}`` at install time
(verified against the shipped add-on manager of Zotero 10.0.2), and
``strict_min_version`` must not contain ``*``. These tests pin that contract
and, when the built XPI is present, that it matches the tracked sources.
"""

import json
import re
import shutil
import subprocess
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).parents[1]
PLUGIN_DIR = ROOT / "services" / "agent-orchestrator" / "integrations" / "zotero"
MANIFEST_PATH = PLUGIN_DIR / "manifest.json"
BOOTSTRAP_PATH = PLUGIN_DIR / "bootstrap.js"

ZOTERO_KEYS = ("id", "update_url", "strict_min_version", "strict_max_version")


def _load_manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


class ZoteroPluginManifestTests(unittest.TestCase):
    def test_manifest_declares_required_zotero_fields(self):
        manifest = _load_manifest()
        self.assertEqual(manifest["manifest_version"], 2)
        self.assertTrue(manifest["version"])
        zotero = manifest["applications"]["zotero"]
        for key in ZOTERO_KEYS:
            self.assertIsInstance(zotero.get(key), str, key)
            self.assertTrue(zotero[key].strip(), key)
        self.assertIn("@", zotero["id"])
        self.assertTrue(zotero["update_url"].startswith("https://"))
        # Zotero rejects '*' in strict_min_version and recommends a concrete
        # `x.x.*` cap for strict_max_version.
        self.assertNotIn("*", zotero["strict_min_version"])
        self.assertRegex(zotero["strict_max_version"], r"^\d+\.\d+\.\*$")

    def test_manifest_version_range_targets_supported_zotero_lines(self):
        zotero = _load_manifest()["applications"]["zotero"]
        minimum = tuple(int(part) for part in zotero["strict_min_version"].split("."))
        maximum = tuple(
            int(part) for part in zotero["strict_max_version"].rstrip("*").rstrip(".").split(".")
        )
        # Zotero 7.0 is the lowest version the used APIs support, and the cap
        # must cover the tested line (never an unbounded `*`).
        self.assertGreaterEqual(minimum, (7, 0))
        self.assertGreater(maximum, minimum)

    def test_built_xpi_matches_tracked_sources(self):
        manifest = _load_manifest()
        xpi = PLUGIN_DIR / f"qs-active-reader-{manifest['version']}.xpi"
        if not xpi.exists():
            self.skipTest("XPI not built (run the packaging step in README.md)")
        with zipfile.ZipFile(xpi) as archive:
            self.assertEqual(
                sorted(info.filename for info in archive.infolist()),
                ["bootstrap.js", "manifest.json"],
            )
            self.assertEqual(archive.read("manifest.json"), MANIFEST_PATH.read_bytes())
            self.assertEqual(archive.read("bootstrap.js"), BOOTSTRAP_PATH.read_bytes())


@unittest.skipUnless(shutil.which("node"), "node is required for plugin JS coverage")
class ZoteroPluginBootstrapTests(unittest.TestCase):
    def run_node(self, script):
        completed = subprocess.run(
            ["node", "-e", script], text=True, capture_output=True
        )
        if completed.returncode:
            raise AssertionError(completed.stderr)
        return json.loads(completed.stdout)

    def test_init_registers_endpoint_and_answers_closed_without_reader(self):
        script = f"""
const vm = require("vm");
const source = require("fs").readFileSync({json.dumps(str(BOOTSTRAP_PATH))}, "utf8");
const endpoints = {{}};
const sandbox = {{ Zotero: {{ Server: {{ Endpoints: endpoints }} }} }};
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
sandbox.QSActiveReader.init({{ id: "qs-active-reader@quickshell.local", version: "0.1.0", rootURI: "resource://qs/" }});
const out = {{}};
const Handler = endpoints["/qs-active-reader"];
out.registered = typeof Handler === "function";
const handler = new Handler();
let response = null;
handler.init(null, (code, contentType, body) => {{ response = [code, contentType, JSON.parse(body)]; }});
out.code = response[0];
out.contentType = response[1];
out.state = response[2].state;
out.generator = response[2].generator;
out.hasServerID = "server_id" in response[2];
console.log(JSON.stringify(out));
"""
        value = self.run_node(script)
        self.assertTrue(value["registered"])
        self.assertEqual(value["code"], 200)
        self.assertEqual(value["contentType"], "application/json")
        self.assertEqual(value["state"], "closed")
        self.assertEqual(value["generator"], "qs-active-reader/0.1.0")
        # No reader, no library identity: the closed snapshot never guesses a
        # server ID (fail closed).
        self.assertFalse(value["hasServerID"])

    def test_server_id_prefers_in_process_accessor_without_http(self):
        script = f"""
const vm = require("vm");
const source = require("fs").readFileSync({json.dumps(str(BOOTSTRAP_PATH))}, "utf8");
let xhrCount = 0;
class XHR {{ constructor() {{ xhrCount++; }} }}
const sandbox = {{
  Zotero: {{ Server: {{ LocalAPI: {{ getServerID: () => "AbCd1234EfGh" }} }} }},
  XMLHttpRequest: XHR,
}};
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const out = {{}};
out.id = sandbox.QSActiveReader._serverID();
out.xhr = xhrCount;
out.cached = sandbox.QSActiveReader._serverID();
out.xhrAfterSecond = xhrCount;
console.log(JSON.stringify(out));
"""
        value = self.run_node(script)
        self.assertEqual(value["id"], "AbCd1234EfGh")
        self.assertEqual(value["xhr"], 0)
        self.assertEqual(value["cached"], "AbCd1234EfGh")
        self.assertEqual(value["xhrAfterSecond"], 0)

    def test_server_id_falls_back_to_opted_in_local_api_read(self):
        script = f"""
const vm = require("vm");
const source = require("fs").readFileSync({json.dumps(str(BOOTSTRAP_PATH))}, "utf8");
const requests = [];
class XHR {{
  constructor() {{ requests.push(this); this.headers = {{}}; }}
  open(method, url) {{ this.method = method; this.url = url; }}
  setRequestHeader(name, value) {{ this.headers[name] = value; }}
  send() {{}}
  getResponseHeader(name) {{ return name === "Zotero-Server-ID" ? "Zz09YyXx8WwV" : null; }}
}}
const sandbox = {{
  Zotero: {{ Server: {{ LocalAPI: {{ getServerID: () => {{ throw new Error("not loaded"); }} }} }} }},
  XMLHttpRequest: XHR,
}};
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const out = {{}};
out.id = sandbox.QSActiveReader._serverID();
out.count = requests.length;
out.method = requests[0].method;
out.url = requests[0].url;
out.allowed = requests[0].headers["Zotero-Allowed-Request"];
out.second = sandbox.QSActiveReader._serverID();
out.countAfterSecond = requests.length;
console.log(JSON.stringify(out));
"""
        value = self.run_node(script)
        self.assertEqual(value["id"], "Zz09YyXx8WwV")
        self.assertEqual(value["count"], 1)
        self.assertEqual(value["method"], "GET")
        self.assertIn("/api/", value["url"])
        # Zotero 10 drops browser-looking requests without this opt-in header.
        self.assertEqual(value["allowed"], "1")
        self.assertEqual(value["second"], "Zz09YyXx8WwV")
        self.assertEqual(value["countAfterSecond"], 1)

    def test_server_id_is_empty_and_never_throws_without_any_source(self):
        script = f"""
const vm = require("vm");
const source = require("fs").readFileSync({json.dumps(str(BOOTSTRAP_PATH))}, "utf8");
class XHR {{ constructor() {{ throw new Error("no server"); }} }}
const sandbox = {{ Zotero: {{ Server: {{}} }}, XMLHttpRequest: XHR }};
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const out = {{}};
out.id = sandbox.QSActiveReader._serverID();
out.again = sandbox.QSActiveReader._serverID();
console.log(JSON.stringify(out));
"""
        value = self.run_node(script)
        self.assertEqual(value["id"], "")
        self.assertEqual(value["again"], "")


if __name__ == "__main__":
    unittest.main()
