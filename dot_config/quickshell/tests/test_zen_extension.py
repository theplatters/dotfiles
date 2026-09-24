"""Tests for the Zen WebExtension (manifest contract + mocked behavior).

Static checks always run (manifest fields, required background patterns).
Behavioral checks shell out to ``node`` with fully mocked ``browser`` APIs:
no live browser, no network, no clipboard, no profile reads.
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
EXT_DIR = (
    ROOT
    / "services"
    / "agent-orchestrator"
    / "integrations"
    / "zen"
    / "extension"
)
MANIFEST_PATH = EXT_DIR / "manifest.json"
BACKGROUND_PATH = EXT_DIR / "background.js"

HOST_NAME = "local.quickshell.zen_context"
EXTENSION_ID = "qs-zen-context@quickshell.local"

NODE_AVAILABLE = shutil.which("node") is not None


def _load_manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _background() -> str:
    return BACKGROUND_PATH.read_text(encoding="utf-8")


class ExtensionManifestTests(unittest.TestCase):
    def test_manifest_is_mv2_persistent_with_required_permissions(self):
        manifest = _load_manifest()
        self.assertEqual(manifest["manifest_version"], 2)
        self.assertTrue(manifest["version"])
        self.assertIn("tabs", manifest["permissions"])
        self.assertIn("nativeMessaging", manifest["permissions"])
        background = manifest["background"]
        self.assertIn("background.js", background["scripts"])
        self.assertTrue(background.get("persistent"))

    def test_manifest_pins_fixed_extension_id(self):
        manifest = _load_manifest()
        # Legacy `applications` key must be gone (current AMO schema).
        self.assertNotIn("applications", manifest)
        gecko = manifest["browser_specific_settings"]["gecko"]
        self.assertEqual(gecko["id"], EXTENSION_ID)
        self.assertIn("@", gecko["id"])

    def test_manifest_requires_gecko_floor_and_no_data_collection(self):
        manifest = _load_manifest()
        gecko = manifest["browser_specific_settings"]["gecko"]
        # Gecko floor: strict_min_version 115+ (e.g. "115.0").
        major = int(str(gecko["strict_min_version"]).split(".")[0])
        self.assertGreaterEqual(major, 115)
        # Data disclosure: exclusively ["none"].
        self.assertEqual(
            gecko["data_collection_permissions"]["required"], ["none"])
        # No legacy `applications` key alongside the current schema.
        self.assertNotIn("applications", manifest)

    def test_manifest_pins_version(self):
        manifest = _load_manifest()
        self.assertEqual(manifest["version"], "0.1.1")

    def test_background_targets_fixed_host_name(self):
        source = _background()
        self.assertIn(HOST_NAME, source)

    def test_no_forbidden_capabilities_in_extension(self):
        source = _background()
        # Strip block/line comments: the doc header honestly names what the
        # extension does NOT do (clipboard/network/...), so only scan code.
        code = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
        code = re.sub(r"//.*", "", code)
        lowered = code.lower()
        for banned in ("navigator.clipboard", "execcommand", "wl-paste",
                       "wl-copy", "hyprctl", "fetch(", "xmlhttprequest",
                       "browser.cookies", "browser.history",
                       "browser.bookmarks", "<all_urls>"):
            self.assertNotIn(banned, lowered)

    def test_background_uses_title_preface_marker(self):
        source = _background()
        self.assertIn("titlePreface", source)
        self.assertIn("[qs-zen-", source)

    def test_background_serializes_and_coalesces(self):
        source = _background()
        # Generation guard re-checked after awaits; heartbeat bound.
        self.assertRegex(source, r"seq|generation")
        self.assertIn("HEARTBEAT_MS", source)
        self.assertIn("10000", source)
        self.assertIn("postMessage", source)
        self.assertIn("invalidate", source)
        self.assertIn("connectNative", source)
        self.assertIn("onDisconnect", source)


@unittest.skipUnless(NODE_AVAILABLE, "node is required for extension JS coverage")
class ExtensionBehaviorTests(unittest.TestCase):
    def run_node(self, script: str):
        completed = subprocess.run(
            ["node", "-e", script], text=True, capture_output=True, timeout=30,
        )
        if completed.returncode:
            raise AssertionError(
                f"node failed:\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
            )
        return json.loads(completed.stdout)

    def harness(self, body: str) -> str:
        return f"""
const vm = require("vm");
const fs = require("fs");
const source = fs.readFileSync({json.dumps(str(BACKGROUND_PATH))}, "utf8");
function makeBrowser(over) {{
  const posted = [];
  const updated = [];
  const listeners = {{}};
  const state = Object.assign({{
    window: {{ id: 1, focused: true, incognito: false }},
    tab: {{ id: 5, windowId: 1, active: true, incognito: false,
            url: "https://example.com/page", title: "Example" }},
  }}, over || {{}});
  const browser = {{
    windows: {{
      WINDOW_ID_NONE: -1,
      update: (id, info) => {{ updated.push([id, info]); return Promise.resolve({{}}); }},
      getLastFocused: (q) => Promise.resolve(Object.assign({{}}, state.window)),
      getCurrent: (q) => Promise.resolve(Object.assign({{}}, state.window)),
      getAll: () => Promise.resolve([Object.assign({{}}, state.window)]),
      onFocusChanged: {{ addListener: (fn) => {{ listeners.focus = fn; }} }},
      onCreated: {{ addListener: (fn) => {{ listeners.created = fn; }} }},
      onRemoved: {{ addListener: (fn) => {{ listeners.removed = fn; }} }},
    }},
    tabs: {{
      query: (q) => {{
        if (typeof state.queryImpl === "function") return state.queryImpl(q);
        return Promise.resolve([Object.assign({{}}, state.tab)]);
      }},
      onActivated: {{ addListener: (fn) => {{ listeners.activated = fn; }} }},
      onUpdated: {{ addListener: (fn) => {{ listeners.updated = fn; }} }},
      onRemoved: {{ addListener: (fn) => {{ listeners.removedTab = fn; }} }},
    }},
    runtime: {{
      connectNative: (name) => {{
        if (name !== {json.dumps(HOST_NAME)}) throw new Error("wrong host");
        return {{
          postMessage: (msg) => {{ posted.push(msg); }},
          disconnect: () => {{}},
          onDisconnect: {{ addListener: () => {{}} }},
        }};
      }},
    }},
  }};
  return {{ browser, posted, updated, listeners, state }};
}}
const tick = () => new Promise((r) => setTimeout(r, 5));
{body}
"""

    def test_marker_format_and_http_filter(self):
        script = self.harness("""
const ctx = makeBrowser();
const sandbox = { browser: ctx.browser, chrome: undefined,
  crypto: require("crypto").webcrypto, setInterval, clearInterval,
  Date, console };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const QS = sandbox.QSZen;
const out = {};
out.host = QS.HOST_NAME;
out.beat = QS.HEARTBEAT_MS;
const m = QS.newMarker();
out.markerOk = QS.MARKER_RE.test(m);
out.http = QS.isSupportedUrl("https://example.com/x");
out.http2 = QS.isSupportedUrl("http://example.com/");
out.ftp = QS.isSupportedUrl("ftp://example.com/");
out.rel = QS.isSupportedUrl("example.com");
out.js = QS.isSupportedUrl("javascript:alert(1)");
console.log(JSON.stringify(out));
""")
        value = self.run_node(script)
        self.assertEqual(value["host"], HOST_NAME)
        self.assertLessEqual(value["beat"], 10000)
        self.assertTrue(value["markerOk"])
        self.assertTrue(value["http"])
        self.assertTrue(value["http2"])
        self.assertFalse(value["ftp"])
        self.assertFalse(value["rel"])
        self.assertFalse(value["js"])

    def test_ensure_marker_skips_private_and_sets_preface(self):
        script = self.harness("""
(async () => {
  const ctx = makeBrowser();
  const sandbox = { browser: ctx.browser, chrome: undefined,
    crypto: require("crypto").webcrypto, setInterval, clearInterval,
    Date, console };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  const QS = sandbox.QSZen;
  const out = {};
  const priv = await QS._ensureMarker(9, true);
  out.privateNull = (priv === null);
  out.privateUpdates = ctx.updated.length;
  const m = await QS._ensureMarker(1, false);
  out.markerOk = QS.MARKER_RE.test(m || "");
  out.updateCall = ctx.updated.length > 0 ? ctx.updated[0][1] : null;
  out.prefaceOk = !!out.updateCall && QS.MARKER_RE.test(out.updateCall.titlePreface || "");
  console.log(JSON.stringify(out));
})();
""")
        value = self.run_node(script)
        self.assertTrue(value["privateNull"])
        self.assertEqual(value["privateUpdates"], 0)
        self.assertTrue(value["markerOk"])
        self.assertTrue(value["prefaceOk"])

    def test_publish_sends_update_then_rechecks_stale_generation(self):
        script = self.harness("""
(async () => {
  const ctx = makeBrowser();
  const sandbox = { browser: ctx.browser, chrome: undefined,
    crypto: require("crypto").webcrypto, setInterval, clearInterval,
    Date, console };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  const QS = sandbox.QSZen;
  QS._reset();
  await tick();
  QS._reset();
  QS._setPort({ postMessage: (m) => ctx.posted.push(m), disconnect: () => {},
                onDisconnect: { addListener: () => {} } });
  const gen = QS._state().getSeq();
  await QS._doPublish(gen, "test");
  const out = {};
  out.firstType = ctx.posted.length ? ctx.posted[0].type : null;
  out.firstMarkerOk = ctx.posted.length ? QS.MARKER_RE.test(ctx.posted[0].marker || "") : false;
  out.firstUrl = ctx.posted.length ? ctx.posted[0].url : null;
  // Advance the generation (simulates a newer focus event), then replay the
  // OLD generation: it must send nothing.
  QS._requestPublish("newer");
  await tick();
  const before = ctx.posted.length;
  await QS._doPublish(gen, "stale-replay");
  out.staleSent = ctx.posted.length - before;
  console.log(JSON.stringify(out));
})();
""")
        value = self.run_node(script)
        self.assertEqual(value["firstType"], "update")
        self.assertTrue(value["firstMarkerOk"])
        self.assertEqual(value["firstUrl"], "https://example.com/page")
        self.assertEqual(value["staleSent"], 0)

    def test_publish_invalidates_on_unsupported_states(self):
        script = self.harness("""
(async () => {
  const out = {};
  async function once(over) {
    const ctx = makeBrowser(over);
    const sandbox = { browser: ctx.browser, chrome: undefined,
      crypto: require("crypto").webcrypto, setInterval, clearInterval,
      Date, console };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(source, sandbox);
    const QS = sandbox.QSZen;
    QS._reset();
    await tick();
    QS._reset();
    QS._setPort({ postMessage: (m) => ctx.posted.push(m), disconnect: () => {},
                  onDisconnect: { addListener: () => {} } });
    await QS._doPublish(QS._state().getSeq(), "test");
    return ctx.posted.length ? ctx.posted[0].type : null;
  }
  out.nonHttp = await once({ tab: { id: 5, windowId: 1, active: true,
    incognito: false, url: "about:blank", title: "blank" } });
  out.incognitoTab = await once({ tab: { id: 5, windowId: 1, active: true,
    incognito: true, url: "https://example.com/", title: "t" } });
  out.incognitoWin = await once({ window: { id: 1, focused: true, incognito: true } });
  out.unfocused = await once({ window: { id: 1, focused: false, incognito: false } });
  console.log(JSON.stringify(out));
})();
""")
        value = self.run_node(script)
        self.assertEqual(value["nonHttp"], "invalidate")
        self.assertEqual(value["incognitoTab"], "invalidate")
        self.assertEqual(value["incognitoWin"], "invalidate")
        self.assertEqual(value["unfocused"], "invalidate")

    def test_supported_url_enforces_length_and_control_bounds(self):
        script = self.harness("""
const ctx = makeBrowser();
const sandbox = { browser: ctx.browser, chrome: undefined,
  crypto: require("crypto").webcrypto, setInterval, clearInterval,
  Date, console };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const QS = sandbox.QSZen;
const base = "https://example.com/";
const exact = base + "a".repeat(2048 - base.length);
const oversize = base + "a".repeat(2048 - base.length + 1);
const out = {};
out.limit = QS.MAX_URL_CHARS;
out.exactLen = exact.length;
out.exact = QS.isSupportedUrl(exact);
out.oversizeLen = oversize.length;
out.oversize = QS.isSupportedUrl(oversize);
out.nonHttp = QS.isSupportedUrl("ftp://example.com/");
out.aboutBlank = QS.isSupportedUrl("about:blank");
out.controlLF = QS.isSupportedUrl("https://example.com/a\\nb");
out.controlCR = QS.isSupportedUrl("https://example.com/a\\rb");
out.controlTab = QS.isSupportedUrl("https://example.com/a\\tb");
out.controlNul = QS.isSupportedUrl("https://example.com/a\\x00b");
console.log(JSON.stringify(out));
""")
        value = self.run_node(script)
        self.assertEqual(value["limit"], 2048)
        self.assertEqual(value["exactLen"], 2048)
        self.assertTrue(value["exact"])
        self.assertEqual(value["oversizeLen"], 2049)
        self.assertFalse(value["oversize"])
        self.assertFalse(value["nonHttp"])
        self.assertFalse(value["aboutBlank"])
        self.assertFalse(value["controlLF"])
        self.assertFalse(value["controlCR"])
        self.assertFalse(value["controlTab"])
        self.assertFalse(value["controlNul"])

    def test_publish_invalidates_oversized_and_control_urls_without_truncation(self):
        script = self.harness("""
(async () => {
  const out = {};
  async function publishWithUrl(url) {
    const ctx = makeBrowser({ tab: { id: 5, windowId: 1, active: true,
      incognito: false, url: url, title: "t" } });
    const sandbox = { browser: ctx.browser, chrome: undefined,
      crypto: require("crypto").webcrypto, setInterval, clearInterval,
      Date, console };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(source, sandbox);
    const QS = sandbox.QSZen;
    QS._reset();
    await tick();
    QS._reset();
    QS._setPort({ postMessage: (m) => ctx.posted.push(m), disconnect: () => {},
                  onDisconnect: { addListener: () => {} } });
    await QS._doPublish(QS._state().getSeq(), "test");
    return ctx.posted.length ? ctx.posted[0] : null;
  }
  const base = "https://example.com/";
  const exact = base + "a".repeat(2048 - base.length);
  const oversize = base + "a".repeat(2048 - base.length + 1);
  const exactMsg = await publishWithUrl(exact);
  out.exactType = exactMsg ? exactMsg.type : null;
  out.exactUrlLen = exactMsg ? exactMsg.url.length : null;
  out.exactUrlIntact = exactMsg ? (exactMsg.url === exact) : false;
  const overMsg = await publishWithUrl(oversize);
  out.oversizeType = overMsg ? overMsg.type : null;
  out.oversizeHasUrl = !!(overMsg && overMsg.url);
  const ctrlMsg = await publishWithUrl("https://example.com/a\\nb");
  out.controlType = ctrlMsg ? ctrlMsg.type : null;
  out.controlHasUrl = !!(ctrlMsg && ctrlMsg.url);
  const nonHttpMsg = await publishWithUrl("ftp://example.com/x");
  out.nonHttpType = nonHttpMsg ? nonHttpMsg.type : null;
  console.log(JSON.stringify(out));
})();
""")
        value = self.run_node(script)
        self.assertEqual(value["exactType"], "update")
        self.assertEqual(value["exactUrlLen"], 2048)
        self.assertTrue(value["exactUrlIntact"])
        self.assertEqual(value["oversizeType"], "invalidate")
        self.assertFalse(value["oversizeHasUrl"])
        self.assertEqual(value["controlType"], "invalidate")
        self.assertFalse(value["controlHasUrl"])
        self.assertEqual(value["nonHttpType"], "invalidate")

    def test_focus_moves_between_awaits_sends_nothing_stale(self):
        script = self.harness("""
(async () => {
  const ctx = makeBrowser();
  let calls = 0;
  const winA = { id: 1, focused: true, incognito: false };
  const winB = { id: 2, focused: true, incognito: false };
  ctx.browser.windows.getLastFocused = (q) => {
    calls++;
    // First call (pre-tab-query) sees window A; the recheck after the tab
    // query sees window B: the in-flight tab result must not be sent.
    return Promise.resolve(calls === 1 ? Object.assign({}, winA) : Object.assign({}, winB));
  };
  ctx.browser.tabs.query = (q) => Promise.resolve([
    { id: 5, windowId: 1, active: true, incognito: false,
      url: "https://a.example/", title: "A" }]);
  const sandbox = { browser: ctx.browser, chrome: undefined,
    crypto: require("crypto").webcrypto, setInterval, clearInterval,
    Date, console };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  const QS = sandbox.QSZen;
  QS._reset();
  await tick();
  QS._reset();
  QS._setPort({ postMessage: (m) => ctx.posted.push(m), disconnect: () => {},
                onDisconnect: { addListener: () => {} } });
  await QS._doPublish(QS._state().getSeq(), "race");
  console.log(JSON.stringify({ sent: ctx.posted.length }));
})();
""")
        value = self.run_node(script)
        self.assertEqual(value["sent"], 0)


if __name__ == "__main__":
    unittest.main()
