"""Tests for the verified Zotero reader bridge (single-window invariant).

All Hyprland/Zotero I/O is faked by monkeypatching the bridge module
functions (``_hypr_active`` / ``_hypr_clients`` / ``_fetch_plugin``): no live
``hyprctl``, no live Zotero, no network. The real ``_atomic_write`` is used
except where the test explicitly captures the payload, so file mode (0600),
atomicity, and the 16 KiB bound are exercised for real.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
BRIDGE_PATH = (
    ROOT
    / "services"
    / "agent-orchestrator"
    / "integrations"
    / "zotero"
    / "qs-zotero-bridge.py"
)


def _load_bridge():
    old_flag = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location(
            "qs_zotero_bridge_under_test", str(BRIDGE_PATH)
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = old_flag


bridge = _load_bridge()

NOW_MS = 1710000000000
ADDR = "0xabc123"
PID = 1234
OTHER_ADDR = "0xdef456"


def _active(window_id=ADDR, pid=PID, cls="zotero"):
    return {"window_id": window_id, "pid": pid, "class": cls}


def _clients_single(addr=ADDR, cls="zotero", pid=PID):
    return [{"address": addr, "class": cls, "pid": pid}]


def _valid_snap(**over):
    snap = {
        "state": "open",
        "server_id": "sPMHtLD6HHBd",
        "library_type": "user",
        "library_id": "0",
        "item_key": "ABCD1234",
        "attachment_key": "EFGH5678",
        "title": "Paper title",
        "collections": ["AAAAAAAA"],
        "ancestor_collections": ["BBBBBBBB"],
        "version": 42,
        "zotero_uri": "zotero://select/library/items/ABCD1234",
    }
    snap.update(over)
    return snap


def _read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class BridgeTickTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "qs-zotero-context.json")
        self._orig_active = bridge._hypr_active
        self._orig_clients = bridge._hypr_clients
        self._orig_fetch = bridge._fetch_plugin
        self._orig_write = bridge._atomic_write

    def tearDown(self):
        bridge._hypr_active = self._orig_active
        bridge._hypr_clients = self._orig_clients
        bridge._fetch_plugin = self._orig_fetch
        bridge._atomic_write = self._orig_write
        self.temp.cleanup()

    def _patch(self, active, clients, snap):
        bridge._hypr_active = lambda _hyprctl: active
        bridge._hypr_clients = lambda _hyprctl: clients
        bridge._fetch_plugin = lambda _endpoint: snap

    def test_single_client_writes_open_record_with_mode_0600(self):
        self._patch(_active(), _clients_single(), _valid_snap())
        outcome = bridge.tick("http://127.0.0.1:9/", self.path, "hyprctl", NOW_MS)
        self.assertEqual(outcome, "wrote")
        record = _read_json(self.path)
        self.assertEqual(record["window_id"], ADDR)
        self.assertEqual(record["pid"], PID)
        self.assertEqual(record["updated_at_ms"], NOW_MS)
        self.assertEqual(record["server_id"], "sPMHtLD6HHBd")
        self.assertEqual(record["library_type"], "user")
        self.assertEqual(record["library_id"], "0")
        self.assertEqual(record["item_key"], "ABCD1234")
        self.assertEqual(record["attachment_key"], "EFGH5678")
        self.assertEqual(record["title"], "Paper title")
        self.assertEqual(record["collections"], ["AAAAAAAA"])
        self.assertEqual(record["ancestor_collections"], ["BBBBBBBB"])
        self.assertEqual(record["version"], 42)
        self.assertEqual(
            record["zotero_uri"], "zotero://select/library/items/ABCD1234"
        )
        self.assertNotIn("state", record)
        mode = os.stat(self.path).st_mode & 0o777
        self.assertEqual(oct(mode), "0o600")
        self.assertLessEqual(
            os.path.getsize(self.path), bridge.FILE_LIMIT
        )

    def test_two_clients_focused_zotero_writes_tombstone_no_item(self):
        two = [
            {"address": ADDR, "class": "zotero", "pid": PID},
            {"address": OTHER_ADDR, "class": "zotero", "pid": 9999},
        ]
        self._patch(_active(), two, _valid_snap())
        outcome = bridge.tick("http://127.0.0.1:9/", self.path, "hyprctl", NOW_MS)
        self.assertEqual(outcome, "tombstone")
        record = _read_json(self.path)
        self.assertEqual(record["state"], "closed")
        self.assertEqual(record["window_id"], ADDR)
        self.assertEqual(record["pid"], PID)
        self.assertEqual(record["updated_at_ms"], NOW_MS)
        self.assertNotIn("item_key", record)
        self.assertNotIn("server_id", record)

    def test_two_clients_focused_non_zotero_skips_untouched(self):
        two = [
            {"address": ADDR, "class": "zotero", "pid": PID},
            {"address": OTHER_ADDR, "class": "zotero", "pid": 9999},
        ]
        sentinel = b'{"sentinel":true}'
        Path(self.path).write_bytes(sentinel)
        before = Path(self.path).read_bytes()
        self._patch(
            _active(window_id="0x99", pid=5555, cls="firefox"),
            two,
            _valid_snap(),
        )
        outcome = bridge.tick("http://127.0.0.1:9/", self.path, "hyprctl", NOW_MS)
        self.assertEqual(outcome, "skip")
        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_zero_clients_non_zotero_focus_skips(self):
        Path(self.path).write_bytes(b"keepme")
        self._patch(
            _active(window_id="0x99", pid=5555, cls="Code"),
            [{"address": "0x99", "class": "Code", "pid": 5555}],
            _valid_snap(),
        )
        outcome = bridge.tick("http://127.0.0.1:9/", self.path, "hyprctl", NOW_MS)
        self.assertEqual(outcome, "skip")
        self.assertEqual(Path(self.path).read_bytes(), b"keepme")

    def test_zero_clients_zotero_focus_tombstones(self):
        # No Zotero client in the list, but focus claims to be Zotero
        # (race/stale): evict, never write an open record.
        self._patch(
            _active(),
            [{"address": ADDR, "class": "firefox", "pid": PID}],
            _valid_snap(),
        )
        outcome = bridge.tick("http://127.0.0.1:9/", self.path, "hyprctl", NOW_MS)
        self.assertEqual(outcome, "tombstone")
        record = _read_json(self.path)
        self.assertEqual(record["state"], "closed")
        self.assertNotIn("item_key", record)

    def test_plugin_closed_single_window_tombstones(self):
        self._patch(
            _active(),
            _clients_single(),
            {"state": "closed", "readers_open": 0},
        )
        outcome = bridge.tick("http://127.0.0.1:9/", self.path, "hyprctl", NOW_MS)
        self.assertEqual(outcome, "tombstone")
        record = _read_json(self.path)
        self.assertEqual(
            record,
            {
                "window_id": ADDR,
                "pid": PID,
                "updated_at_ms": NOW_MS,
                "state": "closed",
            },
        )

    def test_plugin_unreadable_never_writes_open_record(self):
        # Unreadable plugin (None) leaves the file untouched (transient),
        # even on the single-window Zotero path — never an open record.
        Path(self.path).write_bytes(b"stale")
        self._patch(_active(), _clients_single(), None)
        outcome = bridge.tick("http://127.0.0.1:9/", self.path, "hyprctl", NOW_MS)
        self.assertEqual(outcome, "skip")
        self.assertEqual(Path(self.path).read_bytes(), b"stale")

    def test_plugin_invalid_fields_tombstone_never_open(self):
        bad_snaps = [
            _valid_snap(server_id=""),
            _valid_snap(server_id="   "),
            _valid_snap(server_id="x" * 129),
            _valid_snap(library_type="person"),
            _valid_snap(library_id="12a"),
            _valid_snap(library_id=""),
            _valid_snap(library_type="group", library_id="0"),
            _valid_snap(item_key="short"),
            _valid_snap(item_key="abcdef12"),
            _valid_snap(item_key=None),
            _valid_snap(attachment_key="bad!!"),
            _valid_snap(collections=["not-a-key!!"]),
            _valid_snap(collections="AAAAAAAA"),
            _valid_snap(ancestor_collections=["x" * 7]),
            _valid_snap(collections=["AAAAAAAA"] * 257),
        ]
        for index, snap in enumerate(bad_snaps):
            with self.subTest(case=index):
                if Path(self.path).exists():
                    Path(self.path).unlink()
                self._patch(_active(), _clients_single(), snap)
                outcome = bridge.tick(
                    "http://127.0.0.1:9/", self.path, "hyprctl", NOW_MS
                )
                self.assertEqual(outcome, "tombstone", msg=f"case {index}")
                record = _read_json(self.path)
                self.assertEqual(record["state"], "closed")
                self.assertNotIn("item_key", record)

    def test_plugin_invalid_with_non_zotero_focus_skips(self):
        self._patch(
            _active(window_id="0x99", pid=5555, cls="firefox"),
            _clients_single(),
            {"state": "closed"},
        )
        outcome = bridge.tick("http://127.0.0.1:9/", self.path, "hyprctl", NOW_MS)
        self.assertEqual(outcome, "skip")
        self.assertFalse(Path(self.path).exists())

    def test_clients_query_failure_no_open_record(self):
        # Failure (None) + focused Zotero => tombstone; + non-Zotero => skip.
        self._patch(_active(), None, _valid_snap())
        outcome = bridge.tick("http://127.0.0.1:9/", self.path, "hyprctl", NOW_MS)
        self.assertEqual(outcome, "tombstone")
        record = _read_json(self.path)
        self.assertEqual(record["state"], "closed")
        self.assertNotIn("item_key", record)

        Path(self.path).unlink()
        bridge._hypr_active = lambda _h: _active(
            window_id="0x99", pid=5555, cls="firefox"
        )
        bridge._hypr_clients = lambda _h: None
        outcome = bridge.tick("http://127.0.0.1:9/", self.path, "hyprctl", NOW_MS)
        self.assertEqual(outcome, "skip")
        self.assertFalse(Path(self.path).exists())

    def test_clients_malformed_shape_no_open_record(self):
        for bad in ({"not": "a list"}, [42], [{"class": "zotero"}]):
            with self.subTest(bad=bad):
                if Path(self.path).exists():
                    Path(self.path).unlink()
                self._patch(_active(), bad, _valid_snap())
                outcome = bridge.tick(
                    "http://127.0.0.1:9/", self.path, "hyprctl", NOW_MS
                )
                self.assertEqual(outcome, "tombstone")
                self.assertNotIn("item_key", _read_json(self.path))

    def test_multi_window_never_attaches_reader_to_any_window(self):
        # The plugin's process-global reader must not leak into any payload
        # when two Zotero windows exist — capture the written payload.
        two = [
            {"address": ADDR, "class": "zotero", "pid": PID},
            {"address": OTHER_ADDR, "class": "Zotero", "pid": 9999},
        ]
        seen = []
        real_write = bridge._atomic_write

        def capture(path, payload):
            seen.append(dict(payload))
            return real_write(path, payload)

        bridge._hypr_active = lambda _h: _active()
        bridge._hypr_clients = lambda _h: two
        bridge._fetch_plugin = lambda _e: _valid_snap()
        bridge._atomic_write = capture
        outcome = bridge.tick("http://127.0.0.1:9/", self.path, "hyprctl", NOW_MS)
        self.assertEqual(outcome, "tombstone")
        self.assertTrue(seen)
        for payload in seen:
            self.assertNotIn("item_key", payload)
            self.assertNotIn("attachment_key", payload)
            self.assertNotIn("server_id", payload)

    def test_address_mismatch_tombstones(self):
        # Exactly one Zotero client, but focus is a *different* Zotero window
        # (sampling race): evict, never attribute.
        self._patch(
            _active(window_id=OTHER_ADDR, pid=7777, cls="Zotero"),
            _clients_single(addr=ADDR),
            _valid_snap(),
        )
        outcome = bridge.tick("http://127.0.0.1:9/", self.path, "hyprctl", NOW_MS)
        self.assertEqual(outcome, "tombstone")
        record = _read_json(self.path)
        self.assertEqual(record["window_id"], OTHER_ADDR)
        self.assertEqual(record["pid"], 7777)
        self.assertEqual(record["state"], "closed")
        self.assertNotIn("item_key", record)

    def test_class_matching_is_case_insensitive(self):
        self._patch(
            _active(cls="ZOTERO"),
            [{"address": ADDR, "class": "Zotero", "pid": PID}],
            _valid_snap(),
        )
        outcome = bridge.tick("http://127.0.0.1:9/", self.path, "hyprctl", NOW_MS)
        self.assertEqual(outcome, "wrote")
        self.assertEqual(_read_json(self.path)["item_key"], "ABCD1234")

    def test_active_query_failure_skips_untouched(self):
        Path(self.path).write_bytes(b"keep")
        bridge._hypr_active = lambda _h: None
        bridge._hypr_clients = lambda _h: _clients_single()
        bridge._fetch_plugin = lambda _e: _valid_snap()
        outcome = bridge.tick("http://127.0.0.1:9/", self.path, "hyprctl", NOW_MS)
        self.assertEqual(outcome, "skip")
        self.assertEqual(Path(self.path).read_bytes(), b"keep")

    def test_bounded_payload_truncation_and_dedup(self):
        snap = _valid_snap(
            title="  " + "T" * 2000 + "  ",
            collections=["AAAAAAAA", "AAAAAAAA", "BBBBBBBB"],
            ancestor_collections=["CCCCCCCC"],
            version=7,
            zotero_uri="zotero://select/library/items/ABCD1234",
        )
        self._patch(_active(), _clients_single(), snap)
        outcome = bridge.tick("http://127.0.0.1:9/", self.path, "hyprctl", NOW_MS)
        self.assertEqual(outcome, "wrote")
        record = _read_json(self.path)
        self.assertEqual(len(record["title"]), 1024)
        self.assertEqual(record["collections"], ["AAAAAAAA", "BBBBBBBB"])
        raw = Path(self.path).read_bytes()
        self.assertLessEqual(len(raw), bridge.FILE_LIMIT)

    def test_single_zotero_address_helper(self):
        self.assertIsNone(bridge._single_zotero_address(None))
        self.assertIsNone(bridge._single_zotero_address({"a": 1}))
        self.assertIsNone(bridge._single_zotero_address([]))
        self.assertEqual(
            bridge._single_zotero_address(_clients_single()), ADDR
        )
        self.assertIsNone(
            bridge._single_zotero_address(
                [
                    {"address": ADDR, "class": "zotero"},
                    {"address": OTHER_ADDR, "class": "zotero"},
                ]
            )
        )
        # initialClass fallback counts too.
        self.assertEqual(
            bridge._single_zotero_address(
                [{"address": ADDR, "initialClass": "Zotero"}]
            ),
            ADDR,
        )


if __name__ == "__main__":
    unittest.main()
