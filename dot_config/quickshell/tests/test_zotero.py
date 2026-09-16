"""Tests for the Zotero registry link and ``scripts/zotero.py`` backend.

All HTTP is faked via ``zotero.set_transport``: no live Zotero, no live
writes. PDF extraction shells out to ``pdftotext`` only under a monkeypatched
``shutil.which``/``subprocess.run`` pair, or against temp files created by
the test itself.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import projects
import zotero


SID = "TESTSERVER01"
ROOT_KEY = "ABCDEFGH"
CHILD_KEY = "IJKL2345"
OTHER_KEY = "ZZZZ9999"
IN_KEY = "ITEM0001"
OUT_KEY = "ITEM0002"
TRASH_KEY = "ITEM0003"
ATT_PDF = "ATTACH01"
ATT_EPUB = "ATTACH02"

LINK = {"server_id": SID, "library_type": "user", "library_id": "0",
        "collection_key": ROOT_KEY, "include_subcollections": True}


def _item(key, version, collections, item_type="book", title="T",
          parent=None, deleted=False):
    data = {"key": key, "version": version, "itemType": item_type,
            "title": title, "creators": [], "date": "", "DOI": "",
            "url": "", "collections": collections, "tags": []}
    if parent:
        data["parentItem"] = parent
    if deleted:
        data["deleted"] = True
    return {"key": key, "version": version, "data": data,
            "meta": {}, "library": {"id": 0}}


class FakeZotero:
    """Minimal canned local-API server. Records mutations, never dials out."""

    def __init__(self, sid=SID, tmp_pdf=None):
        self.sid = sid
        self.calls = []
        self.live_versions = {IN_KEY: 10, OUT_KEY: 5}
        self.tmp_pdf = tmp_pdf

    def base_headers(self):
        return {"zotero-server-id": self.sid,
                "zotero-api-version": "3"}

    def collections_payload(self):
        return [
            {"key": ROOT_KEY, "version": 3,
             "data": {"key": ROOT_KEY, "name": "Root",
                      "parentCollection": False, "version": 3}},
            {"key": CHILD_KEY, "version": 4,
             "data": {"key": CHILD_KEY, "name": "Child",
                      "parentCollection": ROOT_KEY, "version": 4}},
            {"key": OTHER_KEY, "version": 5,
             "data": {"key": OTHER_KEY, "name": "Other",
                      "parentCollection": False, "version": 5}},
        ]

    def item_payload(self, key):
        if key == IN_KEY:
            return _item(IN_KEY, self.live_versions[IN_KEY], [ROOT_KEY],
                         title="In scope")
        if key == OUT_KEY:
            return _item(OUT_KEY, self.live_versions[OUT_KEY], [OTHER_KEY],
                         title="Out of scope")
        if key == TRASH_KEY:
            return _item(TRASH_KEY, 7, [ROOT_KEY], title="Trashed",
                         deleted=True)
        if key == ATT_PDF:
            entry = _item(ATT_PDF, 12, [], item_type="attachment",
                          title="paper.pdf", parent=IN_KEY)
            entry["data"]["contentType"] = "application/pdf"
            entry["data"]["filename"] = "paper.pdf"
            return entry
        if key == ATT_EPUB:
            entry = _item(ATT_EPUB, 13, [], item_type="attachment",
                          title="book.epub", parent=IN_KEY)
            entry["data"]["contentType"] = "application/epub+zip"
            entry["data"]["filename"] = "book.epub"
            return entry
        return None

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers),
                           "body": body})
        parsed = urlparse(url)
        path = parsed.path
        query = parse_qs(parsed.query)

        def ok(payload, extra=None):
            merged = dict(self.base_headers())
            if extra:
                merged.update(extra)
            return 200, merged, json.dumps(payload).encode("utf-8")

        if path == "/api/" and method == "GET":
            return ok({})
        if path == "/api/local/authorize" and method == "POST":
            return ok({"key": "GRANTKEY0123456789ABCDEFGHIJK",
                       "remember": True})
        if path == "/api/users/0/collections" and method == "GET":
            return ok(self.collections_payload(),
                      {"total-results": "3"})
        if path == f"/api/users/0/collections/{ROOT_KEY}/items" \
                and method == "GET":
            return ok([self.item_payload(IN_KEY)], {"total-results": "1"})
        if path == "/api/users/0/items" and method == "GET":
            items = [self.item_payload(IN_KEY), self.item_payload(OUT_KEY),
                     self.item_payload(TRASH_KEY)]
            return ok(items, {"total-results": "3"})
        if path.startswith("/api/users/0/items/") and method == "GET":
            rest = path[len("/api/users/0/items/"):]
            if rest.endswith("/children"):
                return ok([], {"total-results": "0"})
            if rest.endswith("/file/view/url"):
                key = rest.split("/")[0]
                if key == ATT_PDF and self.tmp_pdf is not None:
                    return (200, dict(self.base_headers()),
                            f"file://{self.tmp_pdf}".encode("utf-8"))
                return 404, dict(self.base_headers()), b"no file"
            payload = self.item_payload(rest)
            if payload is None:
                return 404, dict(self.base_headers()), b"not found"
            return ok(payload)
        if path == "/api/users/0/items" and method == "POST":
            return ok({"successful": {"0": {"key": "NEWITEM1",
                                            "version": 11}},
                       "successfulKeys": ["NEWITEM1"],
                       "failed": {}},
                      {"last-modified-version": "11"})
        if path == "/api/users/0/collections" and method == "POST":
            return ok({"successful": {"0": {"key": "NEWC0LL1",
                                            "version": 6}},
                       "failed": {}},
                      {"last-modified-version": "6"})
        if "/items/" in path and method in ("PATCH", "PUT"):
            return 200, {**self.base_headers(),
                         "last-modified-version": "12"}, b"{}"
        if "/collections/" in path and method in ("PATCH", "PUT"):
            return 200, {**self.base_headers(),
                         "last-modified-version": "6"}, b"{}"
        return 404, dict(self.base_headers()), b"unknown route"


def run_cli(args, stdin_text, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "zotero.py"), *args],
        input=stdin_text, text=True, capture_output=True, timeout=20,
        check=False, env=env)


class ZoteroRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.reg = str(Path(self.temp.name) / "projects.toml")
        self.old_env = os.environ.get(projects.ENV_VAR)
        os.environ.pop(projects.ENV_VAR, None)

    def tearDown(self):
        if self.old_env is None:
            os.environ.pop(projects.ENV_VAR, None)
        else:
            os.environ[projects.ENV_VAR] = self.old_env
        self.temp.cleanup()

    def test_absent_reads_as_null_and_toml_omits_key(self):
        created = projects.create_project({"name": "Solo"}, self.reg)
        self.assertIsNone(created["project"]["zotero_collection"])
        text = Path(self.reg).read_text(encoding="utf-8")
        self.assertNotIn("zotero", text)
        listed = projects.list_projects(self.reg)
        self.assertIsNone(listed["projects"][0]["zotero_collection"])

    def test_round_trip_inline_table_with_default(self):
        link = dict(LINK)
        del link["include_subcollections"]
        created = projects.create_project(
            {"name": "Linked", "zotero_collection": link}, self.reg)
        saved = created["project"]["zotero_collection"]
        self.assertEqual(saved, {**LINK, "include_subcollections": True})
        text = Path(self.reg).read_text(encoding="utf-8")
        self.assertIn("zotero_collection = {", text)
        self.assertIn('collection_key = "ABCDEFGH"', text)
        listed = projects.list_projects(self.reg)
        self.assertEqual(listed["projects"][0]["zotero_collection"], saved)

    def test_explicit_false_round_trips(self):
        link = dict(LINK, include_subcollections=False)
        created = projects.create_project(
            {"name": "L", "zotero_collection": link}, self.reg)
        self.assertFalse(
            created["project"]["zotero_collection"]["include_subcollections"])
        self.assertIn("include_subcollections = false",
                      Path(self.reg).read_text(encoding="utf-8"))

    def test_full_replacement_clears_link(self):
        created = projects.create_project(
            {"name": "L", "zotero_collection": LINK}, self.reg)
        pid = created["project"]["id"]
        updated = projects.update_project(
            {"id": pid, "revision": created["revision"], "name": "L2"},
            self.reg)
        self.assertIsNone(updated["project"]["zotero_collection"])
        nulled = projects.update_project(
            {"id": pid, "revision": updated["revision"], "name": "L3",
             "zotero_collection": None}, self.reg)
        self.assertIsNone(nulled["project"]["zotero_collection"])

    def test_user_zero_allowed_group_zero_rejected(self):
        ok_link = dict(LINK, library_type="user", library_id="0")
        created = projects.create_project(
            {"name": "U0", "zotero_collection": ok_link}, self.reg)
        self.assertEqual(
            created["project"]["zotero_collection"]["library_id"], "0")
        bad = dict(LINK, library_type="group", library_id="0")
        with self.assertRaises(projects.RegistryError):
            projects.create_project({"name": "G0", "zotero_collection": bad},
                                    self.reg)

    def test_strict_rejections(self):
        bad_links = [
            {"server_id": "", "library_type": "user", "library_id": "0",
             "collection_key": ROOT_KEY},
            {"server_id": "x" * 257, "library_type": "user",
             "library_id": "0", "collection_key": ROOT_KEY},
            {"server_id": "has space", "library_type": "user",
             "library_id": "0", "collection_key": ROOT_KEY},
            {"server_id": SID, "library_type": "person", "library_id": "0",
             "collection_key": ROOT_KEY},
            {"server_id": SID, "library_type": "user", "library_id": "12a",
             "collection_key": ROOT_KEY},
            {"server_id": SID, "library_type": "user", "library_id": "0",
             "collection_key": "abcdef12"},
            {"server_id": SID, "library_type": "user", "library_id": "0",
             "collection_key": "SHORT"},
            {"server_id": SID, "library_type": "user", "library_id": "0",
             "collection_key": ROOT_KEY, "include_subcollections": "yes"},
            {"server_id": SID, "library_type": "user", "library_id": "0",
             "collection_key": ROOT_KEY, "include_subcollections": 1},
            {"server_id": SID, "library_type": "user", "library_id": "0",
             "collection_key": ROOT_KEY, "bogus": 1},
            {"server_id": SID, "library_type": "user", "library_id": "0"},
            {"server_id": SID, "library_type": "user",
             "collection_key": ROOT_KEY},
            "not-a-table",
            {"server_id": SID, "library_type": "user",
             "library_id": "0" * 21, "collection_key": ROOT_KEY},
        ]
        for index, link in enumerate(bad_links):
            with self.assertRaises(projects.RegistryError, msg=f"case {index}"):
                projects.create_project(
                    {"name": f"Bad{index}", "zotero_collection": link},
                    self.reg)
        self.assertEqual(projects.list_projects(self.reg)["projects"], [])

    def test_corrupt_table_rejected_not_overwritten(self):
        created = projects.create_project(
            {"name": "L", "zotero_collection": LINK}, self.reg)
        with open(self.reg, "ab") as handle:
            handle.write(b'zotero_collection = "nope"\n')
        corrupt = Path(self.reg).read_bytes()
        with self.assertRaises(projects.RegistryError):
            projects.list_projects(self.reg)
        with self.assertRaises(projects.RegistryError):
            projects.create_project({"name": "new"}, self.reg)
        # Failed mutations left the corrupt bytes untouched (never silently
        # repaired or overwritten).
        self.assertEqual(Path(self.reg).read_bytes(), corrupt)
        self.assertEqual(created["project"]["zotero_collection"], LINK)


class ZoteroHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.reg = str(base / "projects.toml")
        self.prepare_dir = str(base / "prepare")
        self.key_file = str(base / "keys.json")
        self.old_env = dict(os.environ)
        os.environ.pop(projects.ENV_VAR, None)
        os.environ[zotero.PREPARE_DIR_ENV] = self.prepare_dir
        os.environ[zotero.KEY_FILE_ENV] = self.key_file
        os.environ[zotero.API_KEY_ENV] = "TESTKEY123"
        created = projects.create_project(
            {"name": "Paper", "zotero_collection": LINK}, self.reg)
        self.pid = created["project"]["id"]
        pdf = base / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake\n")
        self.fake = FakeZotero(tmp_pdf=str(pdf))
        zotero.set_transport(self.fake)

    def tearDown(self):
        zotero.set_transport(None)
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp.cleanup()

    def base(self):
        return zotero._resolve_base_url(None)

    def test_capabilities_shape(self):
        value = zotero.cmd_capabilities({}, self.base(), self.reg)
        self.assertEqual(value["backend"], "zotero-local")
        self.assertTrue(value["localOnly"])
        for op in ("add-item", "add-existing", "update-item",
                   "add-membership", "remove-membership",
                   "create-subcollection", "update-subcollection"):
            self.assertIn(op, value["operations"])
        self.assertTrue(any("DOI" in d for d in value["deferred"]))
        self.assertTrue(any("upload" in d for d in value["deferred"]))
        with self.assertRaises(zotero.ZoteroError):
            zotero.cmd_capabilities({"extra": 1}, self.base(), self.reg)

    def test_collections_identity(self):
        value = zotero.cmd_collections({}, self.base(), self.reg)
        self.assertEqual(value["server_id"], SID)
        self.assertEqual(value["library"], {"type": "user", "id": "0"})
        self.assertEqual(value["totalResults"], 3)
        by_key = {c["key"]: c for c in value["collections"]}
        self.assertEqual(by_key[CHILD_KEY]["parentCollection"], ROOT_KEY)
        with self.assertRaises(zotero.ZoteroError):
            zotero.cmd_collections({"library_type": "group",
                                    "library_id": "0"}, self.base(), self.reg)

    def test_search_filters_out_of_scope_and_trash(self):
        value = zotero.cmd_search({"project_id": self.pid}, self.base(),
                                  self.reg)
        keys = [i["key"] for i in value["items"]]
        self.assertIn(IN_KEY, keys)
        self.assertNotIn(OUT_KEY, keys)
        self.assertNotIn(TRASH_KEY, keys)
        self.assertEqual(value["scope"]["descendantCount"], 2)
        self.assertTrue(value["scopeFiltered"])

    def test_search_without_subcollections_hits_collection_endpoint(self):
        created = projects.create_project(
            {"name": "Narrow",
             "zotero_collection": dict(LINK, include_subcollections=False)},
            self.reg)
        value = zotero.cmd_search({"project_id": created["project"]["id"]},
                                  self.base(), self.reg)
        self.assertFalse(value["scope"]["include_subcollections"])
        self.assertTrue(any(f"/collections/{ROOT_KEY}/items" in c["url"]
                            for c in self.fake.calls))

    def test_item_scope_rejection(self):
        value = zotero.cmd_item({"project_id": self.pid, "item_key": IN_KEY},
                                self.base(), self.reg)
        self.assertEqual(value["item"]["key"], IN_KEY)
        with self.assertRaises(zotero.ZoteroError) as ctx:
            zotero.cmd_item({"project_id": self.pid, "item_key": OUT_KEY},
                            self.base(), self.reg)
        self.assertIn("scope", str(ctx.exception))

    def test_unlinked_project_rejected(self):
        created = projects.create_project({"name": "Plain"}, self.reg)
        with self.assertRaises(zotero.ZoteroError) as ctx:
            zotero.cmd_search({"project_id": created["project"]["id"]},
                              self.base(), self.reg)
        self.assertIn("not linked", str(ctx.exception))

    def test_server_mismatch_rejected(self):
        self.fake.sid = "OTHERSERVER9"
        with self.assertRaises(zotero.ZoteroError) as ctx:
            zotero.cmd_search({"project_id": self.pid}, self.base(), self.reg)
        self.assertIn("412", str(ctx.exception))

    def test_read_pdf_validation(self):
        with self.assertRaises(zotero.ZoteroError):
            zotero.cmd_read_pdf({"project_id": self.pid,
                                 "attachment_key": ATT_EPUB}, self.base(),
                                self.reg)
        with self.assertRaises(zotero.ZoteroError):
            zotero.cmd_read_pdf({"project_id": self.pid,
                                 "attachment_key": ATT_PDF,
                                 "start_page": 5, "end_page": 2},
                                self.base(), self.reg)
        with self.assertRaises(zotero.ZoteroError):
            zotero.cmd_read_pdf({"project_id": self.pid,
                                 "attachment_key": ATT_PDF,
                                 "start_page": 1, "end_page": 21},
                                self.base(), self.reg)

    def test_read_pdf_happy_path_with_fake_extractor(self):
        real_which = zotero.shutil.which
        real_run = zotero.subprocess.run

        def fake_which(_name):
            return "/usr/bin/pdftotext"

        def fake_run(_argv, **kwargs):
            class Done:
                returncode = 0
                stdout = b"Hello Quickshell PDF page one"
                stderr = b""
            return Done()

        zotero.shutil.which = fake_which  # type: ignore[assignment]
        zotero.subprocess.run = fake_run  # type: ignore[assignment]
        try:
            value = zotero.cmd_read_pdf(
                {"project_id": self.pid, "attachment_key": ATT_PDF,
                 "query": "quickshell"}, self.base(), self.reg)
        finally:
            zotero.shutil.which = real_which  # type: ignore[assignment]
            zotero.subprocess.run = real_run  # type: ignore[assignment]
        self.assertTrue(value["textAvailable"])
        self.assertEqual(value["pages"], {"start": 1, "end": 1})
        self.assertTrue(value["fileUrl"].startswith("file://"))
        self.assertEqual(len(value["matches"]), 1)
        self.assertEqual(value["matches"][0]["page"], 1)

    def test_prepare_and_apply_add_item(self):
        prepared = zotero.cmd_prepare(
            {"project_id": self.pid, "operation": "add-item",
             "params": {"itemType": "journalArticle", "title": "Hello"}},
            self.base(), self.reg)
        token = prepared["prepared"]
        self.assertRegex(token, r"^[A-Za-z0-9_-]{16,64}$")
        self.assertIn("Create", prepared["preview"]["summary"])
        applied = zotero.cmd_apply({"project_id": self.pid,
                                    "prepared": token}, self.base(), self.reg)
        self.assertTrue(applied["ok"])
        self.assertEqual(applied["result"]["key"], "NEWITEM1")
        # Single-use: replaying the token fails.
        with self.assertRaises(zotero.ZoteroError):
            zotero.cmd_apply({"project_id": self.pid, "prepared": token},
                             self.base(), self.reg)
        # The write carried auth headers, and no key leaked into outputs.
        writes = [c for c in self.fake.calls if c["method"] == "POST"
                  and c["url"].endswith("/items")]
        self.assertTrue(writes)
        self.assertEqual(writes[-1]["headers"].get("Zotero-API-Key"),
                         "TESTKEY123")
        self.assertEqual(writes[-1]["headers"].get("Zotero-Server-ID"), SID)
        blob = json.dumps(applied)
        self.assertNotIn("TESTKEY123", blob)
        self.assertNotIn("TESTKEY123", json.dumps(prepared))

    def test_prepare_rejects_bad_params(self):
        with self.assertRaises(zotero.ZoteroError):
            zotero.cmd_prepare(
                {"project_id": self.pid, "operation": "nope", "params": {}},
                self.base(), self.reg)
        with self.assertRaises(zotero.ZoteroError):
            zotero.cmd_prepare(
                {"project_id": self.pid, "operation": "add-item",
                 "params": {"itemType": "journalArticle", "title": "x",
                            "DOI": "not-a-doi"}}, self.base(), self.reg)
        with self.assertRaises(zotero.ZoteroError):
            zotero.cmd_prepare(
                {"project_id": self.pid, "operation": "add-item",
                 "params": {"itemType": "attachment", "title": "x"}},
                self.base(), self.reg)

    def test_apply_rejects_stale_version(self):
        prepared = zotero.cmd_prepare(
            {"project_id": self.pid, "operation": "update-item",
             "params": {"item_key": IN_KEY, "version": 10,
                        "patch": {"title": "New"}}}, self.base(), self.reg)
        self.fake.live_versions[IN_KEY] = 11
        with self.assertRaises(zotero.ZoteroError) as ctx:
            zotero.cmd_apply({"project_id": self.pid,
                              "prepared": prepared["prepared"]},
                             self.base(), self.reg)
        self.assertIn("since prepare", str(ctx.exception))

    def test_apply_rejects_registry_relink(self):
        prepared = zotero.cmd_prepare(
            {"project_id": self.pid, "operation": "create-subcollection",
             "params": {"name": "Sub"}}, self.base(), self.reg)
        listing = projects.list_projects(self.reg)
        projects.update_project(
            {"id": self.pid, "revision": listing["revision"], "name": "Paper",
             "zotero_collection": dict(LINK, collection_key=OTHER_KEY)},
            self.reg)
        with self.assertRaises(zotero.ZoteroError) as ctx:
            zotero.cmd_apply({"project_id": self.pid,
                              "prepared": prepared["prepared"]},
                             self.base(), self.reg)
        self.assertIn("since prepare", str(ctx.exception))

    def test_apply_rejects_tampered_token(self):
        with self.assertRaises(zotero.ZoteroError):
            zotero.cmd_apply({"project_id": self.pid,
                              "prepared": "../evil"}, self.base(), self.reg)
        with self.assertRaises(zotero.ZoteroError):
            zotero.cmd_apply({"project_id": self.pid,
                              "prepared": "a" * 24}, self.base(), self.reg)

    def test_membership_round_trip(self):
        add = zotero.cmd_prepare(
            {"project_id": self.pid, "operation": "add-membership",
             "params": {"item_key": IN_KEY, "collection_key": CHILD_KEY}},
            self.base(), self.reg)
        self.assertIn("Add item", add["preview"]["summary"])
        applied = zotero.cmd_apply({"project_id": self.pid,
                                    "prepared": add["prepared"]},
                                   self.base(), self.reg)
        self.assertIn(CHILD_KEY, applied["result"]["collections"])

    def test_subcollection_create_and_rename(self):
        made = zotero.cmd_prepare(
            {"project_id": self.pid, "operation": "create-subcollection",
             "params": {"name": "Sub"}}, self.base(), self.reg)
        done = zotero.cmd_apply({"project_id": self.pid,
                                 "prepared": made["prepared"]},
                                self.base(), self.reg)
        self.assertEqual(done["result"]["key"], "NEWC0LL1")
        rename = zotero.cmd_prepare(
            {"project_id": self.pid, "operation": "update-subcollection",
             "params": {"collection_key": CHILD_KEY, "version": 4,
                        "name": "Kid"}}, self.base(), self.reg)
        redone = zotero.cmd_apply({"project_id": self.pid,
                                   "prepared": rename["prepared"]},
                                  self.base(), self.reg)
        self.assertEqual(redone["result"]["changed"], ["name"])
        # The bound root cannot be reparented.
        with self.assertRaises(zotero.ZoteroError):
            zotero.cmd_prepare(
                {"project_id": self.pid, "operation": "update-subcollection",
                 "params": {"collection_key": ROOT_KEY, "version": 3,
                            "parentCollection": CHILD_KEY}},
                self.base(), self.reg)

    def test_authorize_never_prints_key(self):
        os.environ.pop(zotero.API_KEY_ENV, None)
        value = zotero.cmd_authorize({"appName": "tests"}, self.base(),
                                     self.reg)
        self.assertEqual(value["server_id"], SID)
        self.assertTrue(value["stored"])
        self.assertNotIn("key", {k.casefold(): v
                                 for k, v in value.items()})
        stored = json.loads(Path(self.key_file).read_text(encoding="utf-8"))
        self.assertIn(SID, stored["keys"])
        blob = json.dumps(value)
        self.assertNotIn(stored["keys"][SID], blob)

    def test_preview_returns_same_preview_and_binding(self):
        prepared = zotero.cmd_prepare(
            {"project_id": self.pid, "operation": "add-item",
             "params": {"itemType": "journalArticle", "title": "Hello"}},
            self.base(), self.reg)
        shown = zotero.cmd_preview(
            {"project_id": self.pid, "prepared": prepared["prepared"]},
            self.base(), self.reg)
        self.assertEqual(shown["preview"], prepared["preview"])
        self.assertEqual(shown["binding"], prepared["binding"])
        self.assertIsInstance(shown["expires_in"], int)
        self.assertGreaterEqual(shown["expires_in"], 0)
        self.assertLessEqual(shown["expires_in"],
                             int(zotero.PREPARE_TTL))
        self.assertNotIn("TESTKEY123", json.dumps(shown))

    def test_preview_does_not_consume(self):
        prepared = zotero.cmd_prepare(
            {"project_id": self.pid, "operation": "add-item",
             "params": {"itemType": "journalArticle", "title": "Hello"}},
            self.base(), self.reg)
        first = zotero.cmd_preview(
            {"project_id": self.pid, "prepared": prepared["prepared"]},
            self.base(), self.reg)
        second = zotero.cmd_preview(
            {"project_id": self.pid, "prepared": prepared["prepared"]},
            self.base(), self.reg)
        self.assertEqual(first["preview"], second["preview"])
        applied = zotero.cmd_apply({"project_id": self.pid,
                                    "prepared": prepared["prepared"]},
                                   self.base(), self.reg)
        self.assertTrue(applied["ok"])
        with self.assertRaises(zotero.ZoteroError):
            zotero.cmd_apply({"project_id": self.pid,
                              "prepared": prepared["prepared"]},
                             self.base(), self.reg)

    def test_preview_rejects_unknown_and_expired_token(self):
        with self.assertRaises(zotero.ZoteroError):
            zotero.cmd_preview({"project_id": self.pid,
                                "prepared": "a" * 24},
                               self.base(), self.reg)
        with self.assertRaises(zotero.ZoteroError):
            zotero.cmd_preview({"project_id": self.pid,
                                "prepared": "../evil"},
                               self.base(), self.reg)
        prepared = zotero.cmd_prepare(
            {"project_id": self.pid, "operation": "add-item",
             "params": {"itemType": "journalArticle", "title": "Hello"}},
            self.base(), self.reg)
        token = prepared["prepared"]
        path = Path(self.prepare_dir) / f"{token}.json"
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["created_at"] = 0
        path.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(zotero.ZoteroError) as ctx:
            zotero.cmd_preview({"project_id": self.pid, "prepared": token},
                               self.base(), self.reg)
        self.assertIn("unknown or expired", str(ctx.exception))

    def test_preview_rejects_cross_project(self):
        prepared = zotero.cmd_prepare(
            {"project_id": self.pid, "operation": "add-item",
             "params": {"itemType": "journalArticle", "title": "Hello"}},
            self.base(), self.reg)
        other = projects.create_project(
            {"name": "Other", "zotero_collection": LINK}, self.reg)
        with self.assertRaises(zotero.ZoteroError) as ctx:
            zotero.cmd_preview(
                {"project_id": other["project"]["id"],
                 "prepared": prepared["prepared"]},
                self.base(), self.reg)
        self.assertIn("different project", str(ctx.exception))

    def test_preview_rejects_registry_relink(self):
        prepared = zotero.cmd_prepare(
            {"project_id": self.pid, "operation": "create-subcollection",
             "params": {"name": "Sub"}}, self.base(), self.reg)
        listing = projects.list_projects(self.reg)
        projects.update_project(
            {"id": self.pid, "revision": listing["revision"], "name": "Paper",
             "zotero_collection": dict(LINK, collection_key=OTHER_KEY)},
            self.reg)
        with self.assertRaises(zotero.ZoteroError) as ctx:
            zotero.cmd_preview({"project_id": self.pid,
                                "prepared": prepared["prepared"]},
                               self.base(), self.reg)
        self.assertIn("since prepare", str(ctx.exception))

    def test_preview_rejects_unsupported_field(self):
        prepared = zotero.cmd_prepare(
            {"project_id": self.pid, "operation": "add-item",
             "params": {"itemType": "journalArticle", "title": "Hello"}},
            self.base(), self.reg)
        with self.assertRaises(zotero.ZoteroError) as ctx:
            zotero.cmd_preview({"project_id": self.pid,
                                "prepared": prepared["prepared"],
                                "extra": 1}, self.base(), self.reg)
        self.assertIn("unsupported field", str(ctx.exception))

    def test_capabilities_lists_preview(self):
        value = zotero.cmd_capabilities({}, self.base(), self.reg)
        self.assertIn("preview", value["commands"])
        self.assertIn("preview", value["resultSchemas"])

    def test_cli_error_envelope(self):
        completed = run_cli(
            ["--projects-file", self.reg, "search"],
            stdin_text=json.dumps({"project_id": self.pid, "limit": 999}),
            env_extra={zotero.PREPARE_DIR_ENV: self.prepare_dir,
                       zotero.KEY_FILE_ENV: self.key_file,
                       zotero.API_KEY_ENV: "TESTKEY123"})
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertIn("error:", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_cli_search_roundtrip(self):
        # Closed loopback port: fails closed with a useful error, never a
        # traceback, and never touches a real Zotero.
        completed = run_cli(
            ["--projects-file", self.reg,
             "--base-url", "http://127.0.0.1:1/api/", "search"],
            stdin_text=json.dumps({"project_id": self.pid, "limit": 10}),
            env_extra={zotero.PREPARE_DIR_ENV: self.prepare_dir,
                       zotero.KEY_FILE_ENV: self.key_file,
                       zotero.API_KEY_ENV: "TESTKEY123",
                       zotero.BASE_URL_ENV: ""})
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("error:", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertEqual(completed.stdout, "")


class ZoteroLibrariesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.reg = str(base / "projects.toml")
        self.prepare_dir = str(base / "prepare")
        self.key_file = str(base / "keys.json")
        self.old_env = dict(os.environ)
        os.environ.pop(projects.ENV_VAR, None)
        os.environ[zotero.PREPARE_DIR_ENV] = self.prepare_dir
        os.environ[zotero.KEY_FILE_ENV] = self.key_file
        os.environ.pop(zotero.API_KEY_ENV, None)
        self.calls = []
        self.old_page_limit = zotero.PAGE_LIMIT
        zotero.set_transport(None)

    def tearDown(self):
        zotero.set_transport(None)
        zotero.PAGE_LIMIT = self.old_page_limit
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp.cleanup()

    def base(self):
        return zotero._resolve_base_url(None)

    def install(self, groups, sid=SID, groups_sid=None, total=None,
                groups_status=200):
        groups_sid = groups_sid if groups_sid is not None else sid
        total_text = str(len(groups) if total is None else total)

        def transport(method, url, headers, body):
            self.calls.append({"method": method, "url": url,
                               "headers": dict(headers), "body": body})
            parsed = urlparse(url)
            path = parsed.path
            query = parse_qs(parsed.query)
            if path == "/api/" and method == "GET":
                return (200, {"zotero-server-id": sid,
                              "zotero-api-version": "3"}, b"{}")
            if path == "/api/users/0/groups" and method == "GET":
                if groups_status != 200:
                    return (groups_status,
                            {"zotero-server-id": groups_sid}, b"denied")
                limit = int(query.get("limit", ["1000"])[0])
                start = int(query.get("start", ["0"])[0])
                page = groups[start:start + limit]
                hdrs = {"zotero-server-id": groups_sid,
                        "total-results": total_text}
                return 200, hdrs, json.dumps(page).encode("utf-8")
            return 404, {"zotero-server-id": sid}, b"unknown route"

        zotero.set_transport(transport)
        return transport

    def test_libraries_personal_first_sorted_normalized(self):
        long_name = "L" * 300
        self.install([
            {"id": 12345, "name": "Shared project"},
            {"id": 999, "name": "beta"},
            {"id": 1000, "name": "Alpha"},
            {"id": 777, "name": long_name},
        ])
        value = zotero.cmd_libraries({}, self.base(), self.reg)
        self.assertEqual(value["server_id"], SID)
        libs = value["libraries"]
        self.assertEqual(libs[0],
                         {"type": "user", "id": "0", "name": "My Library"})
        # Sorted case-insensitively by name; long name truncated to 255.
        names = [entry["name"] for entry in libs[1:]]
        self.assertEqual(names, sorted(names, key=str.casefold))
        by_id = {entry["id"]: entry for entry in libs[1:]}
        self.assertEqual(by_id["12345"]["name"], "Shared project")
        self.assertEqual(by_id["999"]["name"], "beta")
        self.assertEqual(by_id["777"]["name"], "L" * 255)
        for entry in libs:
            self.assertIn(entry["type"], ("user", "group"))
            self.assertRegex(entry["id"], r"^[0-9]{1,20}$")
            self.assertLessEqual(len(entry["name"]), 255)
        self.assertEqual(value["totalResults"], 5)
        self.assertFalse(value["truncated"])
        # Read-only headers, no key, single GET per page.
        groups_calls = [c for c in self.calls if "groups" in c["url"]]
        self.assertTrue(groups_calls)
        for call in groups_calls:
            self.assertEqual(call["method"], "GET")
            self.assertEqual(call["headers"].get("Zotero-API-Version"), "3")
            self.assertEqual(call["headers"].get("Zotero-Server-ID"), SID)
            self.assertNotIn("Zotero-API-Key", call["headers"])

    def test_libraries_skips_malformed_and_dedupes(self):
        self.install([
            {"id": 111, "name": "Good"},
            {"id": 111, "name": "Duplicate second wins? no, kept first"},
            {"name": "missing id"},
            {"id": "abc", "name": "bad id"},
            {"id": "0", "name": "zero id"},
            {"id": 0, "name": "zero int"},
            {"id": "1" * 21, "name": "oversized"},
            {"id": 222, "name": "   "},
            {"id": 333, "name": 42},
            42,
            "nope",
            None,
            {"id": 444},  # missing name
            {"data": {"id": 555, "name": "Nested data shape"}},
        ])
        value = zotero.cmd_libraries({}, self.base(), self.reg)
        libs = value["libraries"]
        self.assertEqual(libs[0]["type"], "user")
        ids = [entry["id"] for entry in libs[1:]]
        self.assertEqual(sorted(ids), ["111", "555"])
        self.assertEqual(
            [e for e in libs if e["id"] == "111"][0]["name"], "Good")
        self.assertEqual(value["totalResults"], 3)

    def test_libraries_empty_group_list(self):
        self.install([])
        value = zotero.cmd_libraries({}, self.base(), self.reg)
        self.assertEqual(value["libraries"],
                         [{"type": "user", "id": "0",
                           "name": "My Library"}])
        self.assertEqual(value["totalResults"], 1)
        self.assertFalse(value["truncated"])

    def test_libraries_pagination_fetches_extra_page(self):
        zotero.PAGE_LIMIT = 2
        self.install([
            {"id": 1, "name": "b"},
            {"id": 2, "name": "a"},
            {"id": 3, "name": "c"},
        ])
        value = zotero.cmd_libraries({}, self.base(), self.reg)
        starts = sorted(
            int(parse_qs(urlparse(c["url"]).query)["start"][0])
            for c in self.calls if "groups" in c["url"])
        self.assertEqual(starts, [0, 2])
        self.assertEqual([e["name"] for e in value["libraries"][1:]],
                         ["a", "b", "c"])
        self.assertEqual(value["totalResults"], 4)
        self.assertFalse(value["truncated"])

    def test_libraries_rejects_unknown_field_and_non_object_stdin(self):
        for stdin_text in (json.dumps({"bogus": 1}), json.dumps([1, 2])):
            completed = run_cli(
                ["--projects-file", self.reg, "libraries"],
                stdin_text=stdin_text,
                env_extra={zotero.PREPARE_DIR_ENV: self.prepare_dir,
                           zotero.KEY_FILE_ENV: self.key_file})
            self.assertNotEqual(completed.returncode, 0, stdin_text)
            self.assertEqual(completed.stdout, "")
            self.assertIn("error:", completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)

    def test_libraries_server_id_change_rejected(self):
        self.install([{"id": 1, "name": "g"}], groups_sid="OTHERSERVER9")
        with self.assertRaises(zotero.ZoteroError) as ctx:
            zotero.cmd_libraries({}, self.base(), self.reg)
        self.assertIn("412", str(ctx.exception))

    def test_libraries_forbidden_surfaces_hint(self):
        self.install([], groups_status=403)
        with self.assertRaises(zotero.ZoteroError) as ctx:
            zotero.cmd_libraries({}, self.base(), self.reg)
        self.assertIn("Allow other applications", str(ctx.exception))

    def test_capabilities_includes_libraries(self):
        value = zotero.cmd_capabilities({}, self.base(), self.reg)
        self.assertIn("libraries", value["commands"])
        self.assertIn("libraries", value["resultSchemas"])
        self.assertEqual(value["resultSchemas"]["libraries"],
                         "{server_id, libraries[{type,id,name}], "
                         "totalResults, truncated}")
        self.assertEqual(value["limits"]["groupsMax"], zotero.GROUP_LIMIT)


if __name__ == "__main__":
    unittest.main()
