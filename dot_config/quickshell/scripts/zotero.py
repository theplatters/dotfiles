#!/usr/bin/env python3
"""Zotero local-API backend bound to the project registry (``scripts/projects.py``).

Talks only to the Zotero **local** API (default ``http://127.0.0.1:23119/api/``;
Zotero 10+). There is no cloud fallback: every request goes to a loopback
address and every project-scoped request is bound to the project's exact
``zotero_collection`` registry association (``server_id``, library,
``collection_key``). Reads require no key; writes (Zotero 10+) require a
local API key obtained through the ``authorize`` command (``POST
/api/local/authorize``) and held in a mode-``0600`` cache partitioned by
``server_id``. Keys never appear in stdout, previews, or error text.

Scope rule: an item is visible only when it belongs to the bound collection
(parent membership) or, when ``include_subcollections`` is true, to one of
its recursive descendants. Child attachments/notes without their own
collection membership inherit scope from their ``parentItem``. Mutations may
only touch objects inside that scope; there is no library delete operation.

CLI::

    python3 scripts/zotero.py [--projects-file PATH] [--base-url URL] <command>

Stdin is one JSON object, stdout is one JSON object. Failures exit nonzero
with ``error: <message>`` on stderr, empty stdout, and no traceback. Unknown
stdin keys are rejected. All commands send an explicit ``limit`` (the local
API is unbounded by default) and make a single HTTP attempt: mutations are
never retried automatically.

Commands and stdin shapes:

- ``capabilities`` ``{}``: backend identity, supported operations, limits,
  deferred pieces, and auth model. No secrets.
- ``authorize`` ``{"appName"?: str}``: ``POST /api/local/authorize``; stores
  the granted key in the secure cache and returns ``{server_id, remember,
  stored}``. The key itself is never printed.
- ``collections`` ``{"library_type"?: ..., "library_id"?: ...}`` (both or
  neither; defaults to the local personal library ``user/"0"``): lists
  collections plus the ``server_id``/library identity from live headers.
- ``search`` ``{"project_id", "query"?, "limit"?, "start"?}``: scoped item
  search (quicksearch ``q``). ``limit`` 1..100 (default 25), ``start`` >= 0.
- ``item`` ``{"project_id", "item_key"}``: one scope-checked item plus its
  children.
- ``read-pdf`` ``{"project_id", "attachment_key", "query"?, "start_page"?,
  "end_page"?}``: scope-checked stored-file PDF text via ``pdftotext``
  (max 20 pages per call, bounded output, per-page refs). No OCR, no
  model-supplied paths.
- ``prepare`` ``{"project_id", "operation", "params"?: {...}, ...flat}``:
  validates, binds the exact registry association and live object versions,
  stores a private server-side plan, and returns ``{prepared, preview}``.
  ``prepared`` is an opaque random token (a filename in a ``0700`` dir),
  not a model-owned authorization: the model cannot edit the plan.
- ``apply`` ``{"project_id", "prepared"}``: reloads the stored plan,
  revalidates everything independently (registry binding, ``server_id``,
  object versions, param shapes), then executes exactly one mutation with
  ``Zotero-Server-ID`` + ``Zotero-API-Key`` preconditions. Single-use on
  success (the plan file is deleted); failures keep the plan so an
  unambiguous error can be inspected, but the caller must re-``prepare``
  after a version conflict. Frontend approval must precede ``apply``.
- ``preview`` ``{"project_id", "prepared"}``: reloads the stored plan,
  revalidates the token, same-project ownership, and unchanged registry
  binding, then returns the backend-canonical ``preview`` bound to the
  exact stored plan (``{preview, binding, expires_in}``). Never consumes
  the token and never mutates anything; ``apply`` still succeeds
  afterwards. Frontend approval must display this ``preview``.

Supported ``prepare``/``apply`` operations: ``add-item`` (structured
metadata), ``add-existing`` (explicit identified item into the bound
collection), ``update-item`` (version-checked patch), ``add-membership`` /
``remove-membership`` (collection membership inside the scope subtree),
``create-subcollection`` / ``update-subcollection`` (rename/move inside the
scope; the bound root can be renamed but never reparented). No library
delete. ``DOI``/``URL`` values are stored verbatim as metadata; automatic
metadata retrieval from DOI/URL and PDF file upload/import are deferred
(see ``capabilities`` ``deferred`` and ``docs/zotero.md``).

Object ``version`` values are local versions partitioned by ``server_id``:
a stored plan is valid only against the ``server_id`` it was prepared on.
Imported titles/notes/abstracts are untrusted data (flagged ``untrusted``).

Environment: ``QUICKSHELL_PROJECTS_FILE`` (registry, via ``projects.py``),
``ZOTERO_BASE_URL`` (loopback-only override), ``ZOTERO_API_KEY`` (write key
override, preferred for one-shot use), ``QUICKSHELL_ZOTERO_PREPARE_DIR``
and ``QUICKSHELL_ZOTERO_KEY_FILE`` (test/override hooks for the plan store
and key cache).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NoReturn

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import qscli


class ZoteroError(ValueError):
    pass


DEFAULT_BASE_URL = "http://127.0.0.1:23119/api/"
BASE_URL_ENV = "ZOTERO_BASE_URL"
API_KEY_ENV = "ZOTERO_API_KEY"
PREPARE_DIR_ENV = "QUICKSHELL_ZOTERO_PREPARE_DIR"
KEY_FILE_ENV = "QUICKSHELL_ZOTERO_KEY_FILE"

# NOTE: stdin transport now goes through qscli.read_input (1 MiB cap).
# This narrower 256 KiB bound is retained for the key-cache and
# prepared-plan store reads/writes below, not for stdin. Do not unify
# by accident.
INPUT_LIMIT = 256 * 1024
HTTP_TIMEOUT = 10.0
RESPONSE_LIMIT = 8 * 1024 * 1024
PAGE_LIMIT = 1000          # per-page limit for collection pagination
COLLECTION_LIMIT = 5000    # max collections materialized per library
GROUP_LIMIT = 1000         # max group libraries materialized per account
SEARCH_LIMIT_MAX = 100
QUERY_LIMIT = 500
TITLE_LIMIT = 1000
TEXT_FIELD_LIMIT = 8000
NAME255_LIMIT = 255
DOI_LIMIT = 255
URL_LIMIT = 2048
TAG_LIMIT = 100
TAGS_MAX = 50
CREATORS_MAX = 20
PDF_MAX_BYTES = 64 * 1024 * 1024
PDF_TEXT_LIMIT = 200 * 1024
PDF_PAGES_MAX = 20
PDF_PAGE_MAX = 10000
PDF_TIMEOUT = 20.0
PREPARE_TTL = 900.0
APP_NAME_LIMIT = 100

_ITEM_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_DOI_RE = re.compile(r"^10\.[0-9]{4,9}/\S+$")
_OPERATIONS = ("add-item", "add-existing", "update-item", "add-membership",
               "remove-membership", "create-subcollection",
               "update-subcollection")
_ITEM_TYPES = {
    "book", "bookSection", "journalArticle", "magazineArticle",
    "newspaperArticle", "thesis", "letter", "manuscript", "interview",
    "film", "artwork", "webpage", "attachment", "note", "annotation",
    "conferencePaper", "report", "bill", "hearing", "patent", "statute",
    "email", "map", "blogPost", "tvBroadcast", "radioBroadcast", "podcast",
    "forumPost", "videoRecording", "audioRecording", "presentation",
    "computerProgram", "document", "encyclopediaArticle",
    "dictionaryEntry",
}
_PATCH_FIELDS = {
    "title", "abstractNote", "date", "DOI", "url", "publicationTitle",
    "publisher", "place", "volume", "issue", "pages", "language",
    "shortTitle", "archive", "accessDate", "extra", "tags", "creators",
}


def _error(message: str) -> NoReturn:
    raise ZoteroError(message)


# ---------------------------------------------------------------------------
# Base URL / transport
# ---------------------------------------------------------------------------

def _resolve_base_url(explicit=None) -> str:
    raw = None
    if explicit is not None and str(explicit).strip():
        raw = str(explicit).strip()
    elif isinstance(os.environ.get(BASE_URL_ENV, ""), str) and \
            os.environ.get(BASE_URL_ENV, "").strip():
        raw = os.environ.get(BASE_URL_ENV, "").strip()
    else:
        raw = DEFAULT_BASE_URL
    if len(raw) > 2048 or "\x00" in raw:
        _error("zotero base URL is unsafe")
    try:
        parsed = urllib.parse.urlparse(raw)
    except ValueError as exc:
        raise ZoteroError("zotero base URL is unsafe") from exc
    if parsed.scheme != "http":
        _error("zotero base URL must be http loopback")
    host = (parsed.hostname or "").casefold()
    if host not in ("127.0.0.1", "localhost", "::1"):
        _error("zotero base URL must be a loopback address")
    if parsed.username or parsed.password:
        _error("zotero base URL must not embed credentials")
    port = parsed.port or 80
    if not 1 <= port <= 65535:
        _error("zotero base URL has a bad port")
    path = parsed.path or "/"
    if not path.startswith("/api/") and path != "/api":
        _error("zotero base URL must point at /api/")
    if not path.endswith("/"):
        path = path + "/"
    return f"http://{parsed.hostname}:{port}{path}"


# Transport indirection for tests: a callable
#   (method, url, headers, body) -> (status, headers_dict, body_bytes).
_TRANSPORT = None


def set_transport(fn) -> None:
    """Install a fake HTTP transport (tests only)."""
    global _TRANSPORT
    _TRANSPORT = fn


def _read_bounded(stream, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining > 0:
        chunk = stream.read(min(32768, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _http(method: str, url: str, headers: dict | None = None,
          body: bytes | None = None,
          timeout: float = HTTP_TIMEOUT) -> tuple[int, dict, bytes]:
    headers = dict(headers or {})
    if _TRANSPORT is not None:
        status, resp_headers, resp_body = _TRANSPORT(method, url, headers,
                                                     body)
        if not isinstance(status, int):
            _error("zotero transport returned a bad status")
        lowered = {str(k).casefold(): v for k, v in dict(resp_headers).items()}
        if not isinstance(resp_body, (bytes, bytearray)):
            _error("zotero transport returned a bad body")
        return status, lowered, bytes(resp_body)
    request = urllib.request.Request(url, data=body, headers=headers,
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_headers = {k.casefold(): v
                           for k, v in response.headers.items()}
            return response.status, raw_headers, _read_bounded(
                response, RESPONSE_LIMIT + 1)
    except urllib.error.HTTPError as exc:
        try:
            raw_headers = {k.casefold(): v for k, v in exc.headers.items()}
        except Exception:
            raw_headers = {}
        try:
            payload = _read_bounded(exc, RESPONSE_LIMIT + 1)
        except Exception:
            payload = b""
        return exc.code, raw_headers, payload
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ZoteroError(
            "cannot reach the Zotero local API; is Zotero running with "
            "'Allow other applications on this computer to communicate "
            f"with Zotero' enabled? ({exc})") from exc


def _check_api_error(status: int, headers: dict, body: bytes,
                     what: str) -> None:
    if 200 <= status < 300:
        if len(body) > RESPONSE_LIMIT:
            _error(f"{what} response is too large")
        return
    snippet = body[:300].decode("utf-8", errors="replace").strip()
    suffix = f": {snippet}"[:320] if snippet else ""
    if status == 401:
        _error(f"{what} is not authorized; run the authorize command "
               "and approve the dialog in Zotero")
    if status == 403:
        _error(f"{what} was forbidden; enable 'Allow other applications "
               f"on this computer to communicate with Zotero'{suffix}")
    if status == 404:
        _error(f"{what} was not found{suffix}")
    if status == 412:
        _error(f"{what}: Zotero server changed (412); the registry link "
               "points at a different database — re-link the collection")
    if status == 428:
        _error(f"{what} needs the Zotero-Server-ID precondition (428)")
    if status == 429:
        _error(f"{what} was rate limited (429); retry later")
    _error(f"{what} failed with HTTP {status}{suffix}")


def _decode_json(body: bytes, what: str):
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ZoteroError(f"{what} returned invalid JSON") from exc


# ---------------------------------------------------------------------------
# Secure key cache + prepare store (partitioned by server_id)
# ---------------------------------------------------------------------------

def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME", "")
    if isinstance(base, str) and base.strip():
        root = Path(os.path.expanduser(base.strip()))
    else:
        root = Path.home() / ".cache"
    return root / "quickshell"


def _key_file() -> Path:
    override = os.environ.get(KEY_FILE_ENV, "")
    if isinstance(override, str) and override.strip():
        return Path(os.path.expanduser(override.strip()))
    return _cache_dir() / "zotero_keys.json"


def _load_key_cache() -> dict:
    path = _key_file()
    try:
        with open(path, "rb") as handle:
            raw = handle.read(INPUT_LIMIT + 1)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ZoteroError("cannot read the Zotero key cache") from exc
    if len(raw) > INPUT_LIMIT:
        _error("zotero key cache is too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ZoteroError("zotero key cache is corrupt") from exc
    if not isinstance(data, dict):
        _error("zotero key cache is corrupt")
    keys = data.get("keys", {})
    if not isinstance(keys, dict):
        _error("zotero key cache is corrupt")
    out: dict[str, str] = {}
    for key, value in keys.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if 1 <= len(key) <= 256 and 1 <= len(value) <= 256:
            out[key] = value
    return out


def _store_key_cache(keys: dict) -> None:
    path = _key_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
    except OSError as exc:
        raise ZoteroError("cannot create the Zotero key cache") from exc
    payload = json.dumps({"v": 1, "keys": keys},
                         separators=(",", ":")).encode("utf-8")
    tmp = path.parent / f".zotero-keys-{os.getpid()}.tmp"
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        raise ZoteroError("cannot write the Zotero key cache") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _get_api_key(server_id: str) -> str:
    env = os.environ.get(API_KEY_ENV, "")
    if isinstance(env, str) and env.strip():
        key = env.strip()
        if 1 <= len(key) <= 256 and "\x00" not in key:
            return key
    cached = _load_key_cache().get(server_id, "")
    if cached:
        return cached
    _error("no Zotero write key for this server; run the authorize "
           "command and approve the dialog in Zotero")
    return ""  # unreachable


def _remember_key(server_id: str, key: str) -> None:
    if not isinstance(key, str) or not key.strip() or len(key) > 256 \
            or "\x00" in key:
        _error("zotero granted an unusable key")
    keys = _load_key_cache()
    keys[server_id] = key.strip()
    _store_key_cache(keys)


def _prepare_dir() -> Path:
    override = os.environ.get(PREPARE_DIR_ENV, "")
    if isinstance(override, str) and override.strip():
        return Path(os.path.expanduser(override.strip()))
    return _cache_dir() / "zotero_prepare"


def _write_plan(plan: dict) -> str:
    directory = _prepare_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    except OSError as exc:
        raise ZoteroError("cannot create the prepare store") from exc
    token = secrets.token_urlsafe(24)
    if not _TOKEN_RE.fullmatch(token):
        token = secrets.token_hex(16)
    path = directory / f"{token}.json"
    payload = json.dumps({"v": 1, "created_at": time.time(), "plan": plan},
                         ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    if len(payload) > INPUT_LIMIT:
        _error("prepared plan is too large")
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        raise ZoteroError("cannot store the prepared plan") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
    except OSError as exc:
        raise ZoteroError("cannot store the prepared plan") from exc
    return token


def _read_plan(token: object) -> tuple[dict, Path]:
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        _error("prepared token is unknown or expired")
    path = _prepare_dir() / f"{token}.json"
    try:
        with open(path, "rb") as handle:
            raw = handle.read(INPUT_LIMIT + 1)
    except FileNotFoundError as exc:
        raise ZoteroError("prepared token is unknown or expired") from exc
    except OSError as exc:
        raise ZoteroError("cannot read the prepared plan") from exc
    if len(raw) > INPUT_LIMIT:
        _error("prepared plan is too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ZoteroError("prepared plan is corrupt") from exc
    if not isinstance(data, dict) or not isinstance(data.get("plan"), dict):
        _error("prepared plan is corrupt")
    created = data.get("created_at", 0)
    try:
        age = time.time() - float(created)
    except (TypeError, ValueError):
        age = PREPARE_TTL + 1
    if age < 0 or age > PREPARE_TTL:
        _error("prepared token is unknown or expired")
    return data["plan"], path


# ---------------------------------------------------------------------------
# Registry binding
# ---------------------------------------------------------------------------

def _projects_module():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import projects
    return projects


def _load_binding(project_id: object, registry_file=None) -> dict:
    projects = _projects_module()
    pid = _validate_project_id(project_id)
    try:
        listing = projects.list_projects(registry_file)
    except projects.RegistryError as exc:
        raise ZoteroError(str(exc)) from exc
    for entry in listing.get("projects", []):
        if entry.get("id") == pid:
            zc = entry.get("zotero_collection")
            if not isinstance(zc, dict):
                _error("project is not linked to a Zotero collection")
            return {
                "project_id": pid,
                "project_name": entry.get("name", ""),
                "server_id": zc["server_id"],
                "library_type": zc["library_type"],
                "library_id": zc["library_id"],
                "collection_key": zc["collection_key"],
                "include_subcollections": bool(
                    zc.get("include_subcollections", True)),
            }
    _error("project id is unknown")
    return {}  # unreachable


def _prefix(binding: dict) -> str:
    if binding["library_type"] == "group":
        return f"groups/{binding['library_id']}"
    if binding["library_id"] == "0":
        return "users/0"
    return f"users/{binding['library_id']}"


def _library_identity(binding: dict) -> dict:
    return {"type": binding["library_type"], "id": binding["library_id"]}


# ---------------------------------------------------------------------------
# Field validators
# ---------------------------------------------------------------------------

def _validate_project_id(value: object) -> str:
    projects = _projects_module()
    try:
        return projects._validate_id(value)
    except projects.RegistryError as exc:
        raise ZoteroError(str(exc)) from exc


def _validate_item_key(value: object, what: str = "item_key") -> str:
    if not isinstance(value, str) or not _ITEM_KEY_RE.fullmatch(value.strip()):
        _error(f"{what} must be 8 uppercase alphanumerics")
    return value.strip()


def _validate_collection_key(value: object, what: str = "collection_key") -> str:
    if not isinstance(value, str) or not _ITEM_KEY_RE.fullmatch(value.strip()):
        _error(f"{what} must be 8 uppercase alphanumerics")
    return value.strip()


def _validate_bounded_text(value: object, what: str, limit: int,
                           required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        _error(f"{what} must be a string")
    assert isinstance(value, str)
    text = value.strip()
    if required and not text:
        _error(f"{what} must be non-blank")
    if len(text) > limit or "\x00" in text:
        _error(f"{what} is too long or unsafe")
    return text


def _validate_limit(value: object) -> int:
    if value is None:
        return 25
    if isinstance(value, bool) or not isinstance(value, int):
        _error("limit must be an integer 1..100")
    if not 1 <= value <= SEARCH_LIMIT_MAX:
        _error("limit must be an integer 1..100")
    return value


def _validate_start(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        _error("start must be a non-negative integer")
    if value < 0 or value > 100000:
        _error("start is out of range")
    return value


def _validate_version(value: object, what: str = "version") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _error(f"{what} must be a non-negative integer")
    return value


def _validate_creators(value: object) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > CREATORS_MAX:
        _error(f"creators must be a list of at most {CREATORS_MAX}")
    out: list[dict] = []
    for entry in value:
        if not isinstance(entry, dict):
            _error("creators entries must be objects")
        for key in entry:
            if key not in ("creatorType", "firstName", "lastName", "name"):
                _error(f"creators entry has unsupported field: {key}")
        ctype = entry.get("creatorType", "author")
        if not isinstance(ctype, str) or not ctype.strip() \
                or len(ctype) > 100 or "\x00" in ctype:
            _error("creators creatorType must be a bounded string")
        first = entry.get("firstName", "")
        last = entry.get("lastName", "")
        name = entry.get("name", "")
        for field in (first, last, name):
            if not isinstance(field, str) or len(field) > 500 \
                    or "\x00" in field:
                _error("creators names must be bounded strings")
        if not (first.strip() or last.strip() or name.strip()):
            _error("creators entries need a name")
        out.append({"creatorType": ctype.strip(), "firstName": first.strip(),
                    "lastName": last.strip(), "name": name.strip()})
    return out


def _validate_tags(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > TAGS_MAX:
        _error(f"tags must be a list of at most {TAGS_MAX}")
    out: list[str] = []
    for tag in value:
        if not isinstance(tag, str):
            _error("tags must be strings")
        text = tag.strip()
        if len(text) > TAG_LIMIT or "\x00" in text:
            _error("tag is too long or unsafe")
        if text:
            out.append(text)
    return out


def _validate_doi(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        _error("DOI must be a string")
    text = value.strip()
    if not text:
        return ""
    if len(text) > DOI_LIMIT or "\x00" in text or not _DOI_RE.match(text):
        _error("DOI must look like 10.xxxx/xxxx")
    return text


def _validate_url(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        _error("url must be a string")
    text = value.strip()
    if not text:
        return ""
    if len(text) > URL_LIMIT or "\x00" in text:
        _error("url is too long or unsafe")
    try:
        parsed = urllib.parse.urlparse(text)
    except ValueError as exc:
        raise ZoteroError("url is not a valid http(s) URL") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        _error("url must be an http(s) URL")
    return text


# ---------------------------------------------------------------------------
# Live API helpers
# ---------------------------------------------------------------------------

def _server_id_from_base(base: str) -> tuple[str, dict]:
    status, headers, body = _http("GET", base, {"Zotero-API-Version": "3"})
    _check_api_error(status, headers, body, "zotero server identity")
    server_id = headers.get("zotero-server-id", "")
    if not server_id:
        # Fall back to a versioned probe; every Zotero 10+ response carries it.
        status, headers, body = _http(
            "GET", base + "users/0/collections?limit=1",
            {"Zotero-API-Version": "3"})
        _check_api_error(status, headers, body, "zotero server identity")
        server_id = headers.get("zotero-server-id", "")
    if not server_id or len(server_id) > 256:
        _error("zotero server did not report a Zotero-Server-ID")
    return server_id, headers


def _collection_list(base: str, prefix: str, server_id: str,
                     extra_headers: dict | None = None) -> tuple[list[dict], str]:
    headers = {"Zotero-API-Version": "3", "Zotero-Server-ID": server_id}
    if extra_headers:
        headers.update(extra_headers)
    collections: list[dict] = []
    total: int | None = None
    start = 0
    live_server = server_id
    while True:
        query = urllib.parse.urlencode({"limit": PAGE_LIMIT, "start": start})
        url = f"{base}{prefix}/collections?{query}"
        status, resp_headers, body = _http("GET", url, headers)
        _check_api_error(status, resp_headers, body, "zotero collections")
        live_server = resp_headers.get("zotero-server-id", live_server)
        if live_server != server_id:
            _error("zotero collections: Zotero server changed (412); the "
                   "registry link points at a different database")
        try:
            total = int(resp_headers.get("total-results", "") or "0") or None
        except ValueError:
            total = None
        payload = _decode_json(body, "zotero collections")
        if not isinstance(payload, list):
            _error("zotero collections returned an unexpected shape")
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            data = entry.get("data", entry)
            if not isinstance(data, dict):
                continue
            key = data.get("key", entry.get("key", ""))
            if not isinstance(key, str) or not key:
                continue
            collections.append({
                "key": key,
                "name": str(data.get("name", ""))[:NAME255_LIMIT + 1],
                "parentCollection": data.get("parentCollection") or None,
                "version": entry.get("version", data.get("version", 0)),
            })
            if len(collections) > COLLECTION_LIMIT:
                _error("zotero library holds too many collections")
        if total is not None and len(collections) >= total:
            break
        if len(payload) < PAGE_LIMIT:
            break
        start += PAGE_LIMIT
        if start >= COLLECTION_LIMIT:
            break
    truncated = total is not None and total > len(collections)
    return collections, live_server


def _group_list(base: str, server_id: str) -> tuple[list[dict], str, bool]:
    """List shared/group libraries for the local account (read-only).

    Returns ``(groups, live_server_id, truncated)`` where ``groups`` holds
    ``{type, id, name}`` dicts sorted case-insensitively by name (id
    tie-break). Group ids arrive as JSON numbers and are normalized to
    digit strings; malformed entries are skipped and duplicate ids
    deduped. Pagination mirrors :func:`_collection_list`.
    """
    headers = {"Zotero-API-Version": "3", "Zotero-Server-ID": server_id}
    by_id: dict[str, dict] = {}
    total: int | None = None
    start = 0
    live_server = server_id
    while True:
        query = urllib.parse.urlencode({"limit": PAGE_LIMIT, "start": start})
        url = f"{base}users/0/groups?{query}"
        status, resp_headers, body = _http("GET", url, headers)
        _check_api_error(status, resp_headers, body, "zotero groups")
        live_server = resp_headers.get("zotero-server-id", live_server)
        if live_server != server_id:
            _error("zotero groups: Zotero server changed (412); the "
                   "registry link points at a different database")
        try:
            total = int(resp_headers.get("total-results", "") or "0") or None
        except ValueError:
            total = None
        payload = _decode_json(body, "zotero groups")
        if not isinstance(payload, list):
            _error("zotero groups returned an unexpected shape")
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            data = entry.get("data", entry)
            if not isinstance(data, dict):
                continue
            raw_id = data.get("id", entry.get("id"))
            if isinstance(raw_id, bool):
                continue
            if isinstance(raw_id, int):
                if raw_id <= 0:
                    continue
                gid = str(raw_id)
            elif isinstance(raw_id, str):
                gid = raw_id.strip()
            else:
                continue
            if not re.fullmatch(r"[0-9]{1,20}", gid):
                continue
            try:
                if int(gid) == 0:
                    continue
            except ValueError:
                continue
            raw_name = data.get("name", entry.get("name"))
            if not isinstance(raw_name, str):
                continue
            name = raw_name.strip()
            if not name:
                continue
            if len(name) > NAME255_LIMIT:
                name = name[:NAME255_LIMIT]
            if gid in by_id:
                continue
            by_id[gid] = {"type": "group", "id": gid, "name": name}
            if len(by_id) > GROUP_LIMIT:
                _error("zotero account holds too many groups")
        if total is not None and len(by_id) >= total:
            break
        if len(payload) < PAGE_LIMIT:
            break
        start += PAGE_LIMIT
        if start >= GROUP_LIMIT:
            break
    groups = sorted(by_id.values(),
                    key=lambda e: (e["name"].casefold(), e["id"]))
    truncated = total is not None and total > len(by_id)
    return groups, live_server, truncated


def _descendants(collections: list[dict], root: str) -> set[str]:
    children: dict[str, list[str]] = {}
    keys = set()
    for entry in collections:
        keys.add(entry["key"])
        parent = entry.get("parentCollection")
        if isinstance(parent, str) and parent:
            children.setdefault(parent, []).append(entry["key"])
    if root not in keys:
        _error("bound Zotero collection was not found in the library; "
               "re-link the project")
    seen = {root}
    queue = [root]
    while queue:
        current = queue.pop(0)
        for child in children.get(current, []):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return seen


def _norm_item(entry: dict) -> dict:
    data = entry.get("data", {}) if isinstance(entry, dict) else {}
    if not isinstance(data, dict):
        data = {}
    creators = data.get("creators", []) if isinstance(
        data.get("creators", []), list) else []
    tags = data.get("tags", []) if isinstance(data.get("tags", []), list) \
        else []
    tag_names: list[str] = []
    for tag in tags[:TAGS_MAX]:
        if isinstance(tag, dict) and isinstance(tag.get("tag"), str):
            tag_names.append(tag["tag"])
        elif isinstance(tag, str):
            tag_names.append(tag)
    item_type = str(data.get("itemType", ""))
    title = data.get("title") or data.get("name") or data.get("caseName") or ""
    collections = data.get("collections", [])
    if not isinstance(collections, list):
        collections = []
    parent = data.get("parentItem") or None
    return {
        "key": str(data.get("key", entry.get("key", ""))),
        "version": entry.get("version", data.get("version", 0)),
        "itemType": item_type,
        "title": str(title)[:2000],
        "creators": creators[:CREATORS_MAX],
        "date": str(data.get("date", ""))[:100],
        "DOI": str(data.get("DOI", ""))[:DOI_LIMIT + 1],
        "url": str(data.get("url", ""))[:URL_LIMIT + 1],
        "collections": [c for c in collections if isinstance(c, str)],
        "parentItem": parent if isinstance(parent, str) else None,
        "tags": tag_names,
        "inTrash": bool(data.get("deleted", False)
                        or entry.get("meta", {}).get("deleted", False))
        if isinstance(entry.get("meta"), dict) else bool(
            data.get("deleted", False)),
        "untrusted": item_type in ("note", "attachment", "annotation"),
    }


def _fetch_item(base: str, prefix: str, server_id: str, item_key: str,
                extra_headers: dict | None = None) -> tuple[dict, str]:
    headers = {"Zotero-API-Version": "3", "Zotero-Server-ID": server_id}
    if extra_headers:
        headers.update(extra_headers)
    url = f"{base}{prefix}/items/{item_key}"
    status, resp_headers, body = _http("GET", url, headers)
    _check_api_error(status, resp_headers, body, "zotero item")
    live = resp_headers.get("zotero-server-id", server_id)
    if live != server_id:
        _error("zotero item: Zotero server changed (412)")
    payload = _decode_json(body, "zotero item")
    if not isinstance(payload, dict):
        _error("zotero item returned an unexpected shape")
    return payload, live


def _fetch_children(base: str, prefix: str, server_id: str,
                    item_key: str) -> list[dict]:
    url = (f"{base}{prefix}/items/{item_key}/children?"
           + urllib.parse.urlencode({"limit": PAGE_LIMIT}))
    status, resp_headers, body = _http(
        "GET", url, {"Zotero-API-Version": "3",
                     "Zotero-Server-ID": server_id})
    _check_api_error(status, resp_headers, body, "zotero item children")
    payload = _decode_json(body, "zotero item children")
    if not isinstance(payload, list):
        return []
    return [_norm_item(e) for e in payload if isinstance(e, dict)][:100]


def _scope_check(base: str, prefix: str, server_id: str, norm: dict,
                 descendants: set[str],
                 parent_cache: dict) -> bool:
    if any(c in descendants for c in norm.get("collections", [])):
        return True
    parent = norm.get("parentItem")
    if not parent or not isinstance(parent, str):
        return False
    if parent in parent_cache:
        parent_norm = parent_cache[parent]
    else:
        payload, _ = _fetch_item(base, prefix, server_id, parent)
        parent_norm = _norm_item(payload)
        parent_cache[parent] = parent_norm
    return any(c in descendants for c in parent_norm.get("collections", []))


def _resolve_scope(base: str, binding: dict) -> tuple[set[str], list[dict], str]:
    prefix = _prefix(binding)
    collections, live = _collection_list(base, prefix, binding["server_id"])
    if live != binding["server_id"]:
        _error("zotero server changed (412); the registry link points at "
               "a different database — re-link the collection")
    descendants = _descendants(collections, binding["collection_key"])
    return descendants, collections, live


def _scope_summary(binding: dict, descendants: set[str]) -> dict:
    keys = sorted(descendants)
    shown = keys[:200]
    return {
        "collection_key": binding["collection_key"],
        "include_subcollections": binding["include_subcollections"],
        "descendantCount": len(descendants),
        "descendants": shown,
        "truncated": len(keys) > len(shown),
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_capabilities(payload: dict, base: str, registry_file=None) -> dict:
    if not isinstance(payload, dict):
        _error("capabilities payload must be an object")
    for key in payload:
        _error(f"capabilities has an unsupported field: {key}")
    return {
        "backend": "zotero-local",
        "base_url": base,
        "apiVersion": "3",
        "localOnly": True,
        "commands": ["capabilities", "authorize", "collections", "libraries",
                     "search", "item", "read-pdf", "prepare", "preview",
                     "apply"],
        "operations": list(_OPERATIONS),
        "resultSchemas": {
            "collections": "{server_id, library{type,id}, collections[{key,"
                           "name,parentCollection,version}], totalResults,"
                           " truncated}",
            "libraries": "{server_id, libraries[{type,id,name}], "
                         "totalResults, truncated}",
            "search": "{server_id, library, scope{collection_key,"
                      "include_subcollections,descendantCount,descendants[],"
                      "truncated}, query, limit, start, totalResults, "
                      "items[normalized], returned, scopeFiltered, truncated}",
            "item": "{server_id, library, scope, item(normalized), "
                    "data(full), children[normalized], collections_in_scope}",
            "read-pdf": "{server_id, attachment{key,version,parentItem,"
                        "filename,contentType}, fileUrl, pages{start,end}, "
                        "textAvailable, reason?, extracted[{page,chars,"
                        "truncated,text}], matches[{page,snippet}]?}",
            "prepare": "{prepared(opaque token), preview{operation,summary,"
                       "versions,...}, binding, expires_in}",
            "preview": "{preview{operation,summary,versions,...}, binding, "
                       "expires_in}",
            "apply": "{ok, operation, binding, result, libraryVersion}",
        },
        "operationSchemas": {
            "add-item": "{itemType, title, creators?, date?, DOI?, url?, "
                        "tags?, abstractNote?, extra?, collections?="
                        "[bound]}",
            "add-existing": "{item_key, collection_key?=bound}",
            "update-item": "{item_key, version, patch{title?,abstractNote?,"
                           "date?,DOI?,url?,publicationTitle?,publisher?,"
                           "place?,volume?,issue?,pages?,language?,"
                           "shortTitle?,archive?,accessDate?,extra?,tags?,"
                           "creators?}}",
            "add-membership": "{item_key, collection_key}",
            "remove-membership": "{item_key, collection_key}",
            "create-subcollection": "{name, parent_key?=bound}",
            "update-subcollection": "{collection_key, version, name?, "
                                    "parentCollection?}",
        },
        "limits": {
            "searchLimitMax": SEARCH_LIMIT_MAX,
            "queryMax": QUERY_LIMIT,
            "collectionsMax": COLLECTION_LIMIT,
            "groupsMax": GROUP_LIMIT,
            "pdfPagesMax": PDF_PAGES_MAX,
            "pdfTextMax": PDF_TEXT_LIMIT,
            "pdfFileMax": PDF_MAX_BYTES,
            "prepareTtlSeconds": int(PREPARE_TTL),
            "httpTimeoutSeconds": HTTP_TIMEOUT,
        },
        "deferred": [
            "automatic metadata retrieval from DOI/URL (DOI/URL are stored "
            "verbatim as item metadata; no Crossref/translation-server "
            "lookup is performed)",
            "PDF file upload/import (the three-phase stored-file upload "
            "flow is not wired; add-item covers metadata only)",
            "full-text content writes (PUT fulltext) and saved-search "
            "execution endpoints",
            "automatic OCR for scanned PDFs (read-pdf reports "
            "textAvailable=false instead)",
        ],
        "auth": {
            "readsRequireKey": False,
            "writesRequireKey": True,
            "how": "run the authorize command and approve the dialog in "
                   "Zotero, or set ZOTERO_API_KEY; keys are cached "
                   "mode-0600 partitioned by server_id and never printed",
        },
    }


def cmd_authorize(payload: dict, base: str, registry_file=None) -> dict:
    if not isinstance(payload, dict):
        _error("authorize payload must be an object")
    for key in payload:
        if key != "appName":
            _error(f"authorize has an unsupported field: {key}")
    app = payload.get("appName", "quickshell")
    app_name = _validate_bounded_text(app, "appName", APP_NAME_LIMIT)
    server_id, _ = _server_id_from_base(base)
    body = json.dumps({"appName": app_name}).encode("utf-8")
    status, resp_headers, resp_body = _http(
        "POST", base + "local/authorize",
        {"Content-Type": "application/json",
         "Zotero-API-Version": "3", "Zotero-Server-ID": server_id}, body)
    if status == 403:
        _error("zotero authorization was denied in Zotero")
    _check_api_error(status, resp_headers, resp_body, "zotero authorize")
    payload_out = _decode_json(resp_body, "zotero authorize")
    if not isinstance(payload_out, dict) or not payload_out.get("key"):
        _error("zotero authorize returned an unexpected shape")
    remember = bool(payload_out.get("remember", False))
    _remember_key(server_id, str(payload_out["key"]))
    return {"server_id": server_id, "remember": remember, "stored": True}


def cmd_collections(payload: dict, base: str, registry_file=None) -> dict:
    if not isinstance(payload, dict):
        _error("collections payload must be an object")
    for key in payload:
        if key not in ("library_type", "library_id"):
            _error(f"collections has an unsupported field: {key}")
    libtype = payload.get("library_type", "user")
    libid = payload.get("library_id", "0")
    if ("library_type" in payload) != ("library_id" in payload):
        _error("collections needs both library_type and library_id")
    if libtype not in ("user", "group"):
        _error("library_type must be 'user' or 'group'")
    if not isinstance(libid, str) or not re.fullmatch(r"[0-9]{1,20}", libid):
        _error("library_id must be a digit string")
    if libtype == "group" and int(libid) == 0:
        _error("library_id '0' is allowed only for user libraries")
    prefix = f"groups/{libid}" if libtype == "group" else (
        "users/0" if libid == "0" else f"users/{libid}")
    server_id, _ = _server_id_from_base(base)
    collections, live = _collection_list(base, prefix, server_id)
    collections.sort(key=lambda e: (str(e["name"]).casefold(), e["key"]))
    return {
        "server_id": live,
        "library": {"type": libtype, "id": libid},
        "collections": collections,
        "totalResults": len(collections),
        "truncated": False,
    }


def cmd_libraries(payload: dict, base: str, registry_file=None) -> dict:
    if not isinstance(payload, dict):
        _error("libraries payload must be an object")
    for key in payload:
        _error(f"libraries has an unsupported field: {key}")
    server_id, _ = _server_id_from_base(base)
    groups, live, truncated = _group_list(base, server_id)
    libraries = [{"type": "user", "id": "0", "name": "My Library"}, *groups]
    return {
        "server_id": live,
        "libraries": libraries,
        "totalResults": len(libraries),
        "truncated": truncated,
    }


def cmd_search(payload: dict, base: str, registry_file=None) -> dict:
    if not isinstance(payload, dict):
        _error("search payload must be an object")
    for key in payload:
        if key not in ("project_id", "query", "limit", "start"):
            _error(f"search has an unsupported field: {key}")
    if "project_id" not in payload:
        _error("search needs project_id")
    binding = _load_binding(payload.get("project_id"), registry_file)
    query = payload.get("query", "")
    if query is None:
        query = ""
    if not isinstance(query, str):
        _error("query must be a string")
    query = query.strip()
    if len(query) > QUERY_LIMIT or "\x00" in query:
        _error("query is too long or unsafe")
    limit = _validate_limit(payload.get("limit"))
    start = _validate_start(payload.get("start"))
    descendants, _, live = _resolve_scope(base, binding)
    prefix = _prefix(binding)
    headers = {"Zotero-API-Version": "3",
               "Zotero-Server-ID": binding["server_id"]}
    if binding["include_subcollections"]:
        params: dict = {"limit": limit, "start": start}
        if query:
            params["q"] = query
        url = f"{base}{prefix}/items?{urllib.parse.urlencode(params)}"
    else:
        params = {"limit": limit, "start": start}
        if query:
            params["q"] = query
        url = (f"{base}{prefix}/collections/{binding['collection_key']}"
               f"/items?{urllib.parse.urlencode(params)}")
    status, resp_headers, body = _http("GET", url, headers)
    _check_api_error(status, resp_headers, body, "zotero search")
    if resp_headers.get("zotero-server-id", live) != binding["server_id"]:
        _error("zotero search: Zotero server changed (412)")
    try:
        total = int(resp_headers.get("total-results", "") or "0")
    except ValueError:
        total = 0
    raw_items = _decode_json(body, "zotero search")
    if not isinstance(raw_items, list):
        _error("zotero search returned an unexpected shape")
    parent_cache: dict = {}
    items: list[dict] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        norm = _norm_item(entry)
        if norm.get("inTrash"):
            continue
        if binding["include_subcollections"]:
            if not _scope_check(base, prefix, binding["server_id"], norm,
                                descendants, parent_cache):
                continue
        items.append(norm)
        if len(items) >= limit:
            break
    return {
        "server_id": binding["server_id"],
        "library": _library_identity(binding),
        "scope": _scope_summary(binding, descendants),
        "query": query,
        "limit": limit,
        "start": start,
        "totalResults": total,
        "items": items,
        "returned": len(items),
        "scopeFiltered": bool(binding["include_subcollections"]),
        "truncated": total > start + len(raw_items),
    }


def cmd_item(payload: dict, base: str, registry_file=None) -> dict:
    if not isinstance(payload, dict):
        _error("item payload must be an object")
    for key in payload:
        if key not in ("project_id", "item_key"):
            _error(f"item has an unsupported field: {key}")
    if "project_id" not in payload or "item_key" not in payload:
        _error("item needs project_id and item_key")
    binding = _load_binding(payload.get("project_id"), registry_file)
    item_key = _validate_item_key(payload.get("item_key"))
    descendants, _, _ = _resolve_scope(base, binding)
    prefix = _prefix(binding)
    raw, _ = _fetch_item(base, prefix, binding["server_id"], item_key)
    norm = _norm_item(raw)
    if not _scope_check(base, prefix, binding["server_id"], norm,
                        descendants, {}):
        _error("zotero item is outside the linked collection scope")
    children = _fetch_children(base, prefix, binding["server_id"], item_key)
    data = raw.get("data", {}) if isinstance(raw.get("data"), dict) else {}
    return {
        "server_id": binding["server_id"],
        "library": _library_identity(binding),
        "scope": _scope_summary(binding, descendants),
        "item": norm,
        "data": data,
        "children": children,
        "collections_in_scope": [c for c in norm.get("collections", [])
                                 if c in descendants],
    }


# ---------------------------------------------------------------------------
# PDF reading (bounded pdftotext, no model paths, no OCR)
# ---------------------------------------------------------------------------

def _file_view_url(base: str, prefix: str, server_id: str,
                   attachment_key: str) -> str:
    url = f"{base}{prefix}/items/{attachment_key}/file/view/url"
    status, resp_headers, body = _http(
        "GET", url, {"Zotero-API-Version": "3",
                     "Zotero-Server-ID": server_id})
    _check_api_error(status, resp_headers, body, "zotero attachment file")
    try:
        text = body.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ZoteroError("zotero attachment file URL is not valid") from exc
    if not text.startswith("file://") or len(text) > 4096:
        _error("zotero attachment file URL is unexpected")
    return text


def _local_path_from_file_url(file_url: str) -> Path:
    parsed = urllib.parse.urlparse(file_url)
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        _error("zotero attachment file URL is not local")
    path = urllib.parse.unquote(parsed.path)
    if not path.startswith("/") or len(path) > 4096 or "\x00" in path:
        _error("zotero attachment path is unsafe")
    candidate = Path(path)
    try:
        fd = os.open(str(candidate), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ZoteroError("zotero attachment file is not accessible") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _error("zotero attachment is not a regular file")
        if info.st_size > PDF_MAX_BYTES:
            _error("zotero attachment PDF is too large")
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return candidate


def _pdftotext_pages(path: Path, start: int, end: int) -> list[dict]:
    binary = shutil.which("pdftotext")
    if binary is None or not isinstance(binary, str):
        _error("pdftotext is not available; install poppler-utils to "
               "enable PDF text extraction")
    assert isinstance(binary, str)
    pages: list[dict] = []
    total = 0
    for page in range(start, end + 1):
        try:
            proc = subprocess.run(
                [binary, "-layout", "-f", str(page), "-l", str(page),
                 str(path), "-"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=PDF_TIMEOUT, check=False)
        except FileNotFoundError as exc:
            raise ZoteroError("pdftotext is not available") from exc
        except subprocess.TimeoutExpired as exc:
            raise ZoteroError("PDF text extraction timed out") from exc
        except OSError as exc:
            raise ZoteroError("cannot run pdftotext") from exc
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8",
                                        errors="replace").strip()[:200]
            raise ZoteroError(
                f"PDF text extraction failed for page {page}"
                + (f": {detail}" if detail else ""))
        full = proc.stdout.decode("utf-8", errors="replace")
        room = PDF_TEXT_LIMIT - total
        if room <= 0:
            pages.append({"page": page, "chars": 0, "truncated": True,
                          "text": ""})
            continue
        clipped = full[:room]
        total += len(clipped)
        pages.append({"page": page, "chars": len(clipped),
                      "truncated": len(clipped) < len(full),
                      "text": clipped})
    return pages


def cmd_read_pdf(payload: dict, base: str, registry_file=None) -> dict:
    if not isinstance(payload, dict):
        _error("read-pdf payload must be an object")
    for key in payload:
        if key not in ("project_id", "attachment_key", "query", "start_page",
                       "end_page"):
            _error(f"read-pdf has an unsupported field: {key}")
    if "project_id" not in payload or "attachment_key" not in payload:
        _error("read-pdf needs project_id and attachment_key")
    binding = _load_binding(payload.get("project_id"), registry_file)
    attachment_key = _validate_item_key(payload.get("attachment_key"),
                                        "attachment_key")
    query = payload.get("query", "")
    if query is None:
        query = ""
    if not isinstance(query, str):
        _error("query must be a string")
    query = query.strip()
    if len(query) > QUERY_LIMIT or "\x00" in query:
        _error("query is too long or unsafe")
    start_page = payload.get("start_page", 1)
    end_page = payload.get("end_page", start_page)
    for label, value in (("start_page", start_page), ("end_page", end_page)):
        if isinstance(value, bool) or not isinstance(value, int):
            _error(f"{label} must be an integer")
    if not 1 <= start_page <= PDF_PAGE_MAX or not 1 <= end_page <= PDF_PAGE_MAX:
        _error("page numbers are out of range")
    if end_page < start_page:
        _error("end_page must not precede start_page")
    if end_page - start_page + 1 > PDF_PAGES_MAX:
        _error(f"at most {PDF_PAGES_MAX} pages per read-pdf call")
    descendants, _, _ = _resolve_scope(base, binding)
    prefix = _prefix(binding)
    raw, _ = _fetch_item(base, prefix, binding["server_id"], attachment_key)
    norm = _norm_item(raw)
    data = raw.get("data", {}) if isinstance(raw.get("data"), dict) else {}
    if norm.get("itemType") != "attachment":
        _error("read-pdf needs a stored-file attachment key")
    content_type = str(data.get("contentType", ""))
    if content_type != "application/pdf":
        _error("read-pdf supports only PDF attachments")
    if not _scope_check(base, prefix, binding["server_id"], norm,
                        descendants, {}):
        _error("zotero attachment is outside the linked collection scope")
    file_url = _file_view_url(base, prefix, binding["server_id"],
                              attachment_key)
    path = _local_path_from_file_url(file_url)
    pages = _pdftotext_pages(path, start_page, end_page)
    combined = "".join(p["text"] for p in pages)
    if not combined.strip():
        return {
            "server_id": binding["server_id"],
            "attachment": {
                "key": norm["key"], "version": norm["version"],
                "parentItem": norm.get("parentItem"),
                "filename": str(data.get("filename", ""))[:255],
                "contentType": content_type},
            "fileUrl": file_url,
            "pages": {"start": start_page, "end": end_page},
            "textAvailable": False,
            "reason": "no extractable text (scanned image PDF?); "
                      "OCR is not performed automatically",
            "extracted": pages,
            "matches": None,
        }
    matches = None
    if query:
        folded = combined.casefold()
        needle = query.casefold()
        matches = []
        pos = 0
        while True:
            at = folded.find(needle, pos)
            if at < 0 or len(matches) >= 50:
                break
            begin = max(0, at - 120)
            stop = min(len(combined), at + len(query) + 120)
            snippet = " ".join(combined[begin:stop].split())
            # Attribute the page by cumulative offsets.
            offset = 0
            page_no = pages[-1]["page"] if pages else start_page
            for entry in pages:
                offset += entry["chars"]
                if at < offset:
                    page_no = entry["page"]
                    break
            matches.append({"page": page_no, "snippet": snippet[:400]})
            pos = at + max(1, len(needle))
    return {
        "server_id": binding["server_id"],
        "attachment": {
            "key": norm["key"], "version": norm["version"],
            "parentItem": norm.get("parentItem"),
            "filename": str(data.get("filename", ""))[:255],
            "contentType": content_type},
        "fileUrl": file_url,
        "pages": {"start": start_page, "end": end_page},
        "textAvailable": True,
        "extracted": pages,
        "matches": matches,
    }


# ---------------------------------------------------------------------------
# prepare / apply
# ---------------------------------------------------------------------------

def _split_prepare(payload: dict) -> tuple[str, str, dict]:
    if not isinstance(payload, dict):
        _error("prepare payload must be an object")
    if "project_id" not in payload or "operation" not in payload:
        _error("prepare needs project_id and operation")
    operation = payload.get("operation")
    if operation not in _OPERATIONS:
        _error(f"operation must be one of: {', '.join(_OPERATIONS)}")
    params = payload.get("params", None)
    if params is None:
        params = {k: v for k, v in payload.items()
                  if k not in ("project_id", "operation")}
    elif not isinstance(params, dict):
        _error("params must be an object")
    else:
        for key in payload:
            if key not in ("project_id", "operation", "params"):
                _error(f"prepare has an unsupported field: {key}")
    project_id = _validate_project_id(payload.get("project_id"))
    assert isinstance(operation, str)
    return project_id, operation, params


def _validate_patch(patch: object) -> dict:
    if not isinstance(patch, dict) or not patch:
        _error("patch must be a non-empty object")
    out: dict = {}
    for key, value in patch.items():
        if key not in _PATCH_FIELDS:
            _error(f"patch has an unsupported field: {key}")
        if key == "tags":
            out[key] = _validate_tags(value)
        elif key == "creators":
            out[key] = [dict(c) for c in _validate_creators(value)]
        elif key == "title":
            out[key] = _validate_bounded_text(value, "patch.title",
                                              TITLE_LIMIT)
        elif key in ("abstractNote", "extra"):
            out[key] = _validate_bounded_text(value, f"patch.{key}",
                                              TEXT_FIELD_LIMIT,
                                              required=False)
        elif key == "DOI":
            out[key] = _validate_doi(value)
        elif key == "url":
            out[key] = _validate_url(value)
        else:
            out[key] = _validate_bounded_text(value, f"patch.{key}", 1000,
                                              required=False)
    return out


def _prepare_operation(base: str, binding: dict, operation: str,
                       params: dict) -> tuple[dict, dict]:
    """Validate params against live state; return (plan_params, preview)."""
    descendants, collections, _ = _resolve_scope(base, binding)
    prefix = _prefix(binding)
    by_key = {c["key"]: c for c in collections}

    def need_collection(value: object, what: str,
                        allow_bound: bool = True) -> str:
        key = _validate_collection_key(value, what)
        if key not in descendants:
            _error(f"{what} is outside the linked collection scope")
        if not allow_bound and key == binding["collection_key"]:
            _error(f"{what} must not be the bound root collection")
        return key

    if operation == "add-item":
        for key in params:
            if key not in ("itemType", "title", "creators", "date", "DOI",
                           "url", "tags", "abstractNote", "extra",
                           "collections"):
                _error(f"add-item params has unsupported field: {key}")
        item_type = params.get("itemType", "")
        if item_type not in _ITEM_TYPES or item_type in ("attachment",
                                                         "annotation"):
            _error("add-item itemType must be a regular item type "
                   "(attachments/annotations are not created this way)")
        title = _validate_bounded_text(params.get("title"), "title",
                                       TITLE_LIMIT)
        creators = _validate_creators(params.get("creators"))
        date = _validate_bounded_text(params.get("date"), "date", 100,
                                      required=False)
        doi = _validate_doi(params.get("DOI"))
        url = _validate_url(params.get("url"))
        tags = _validate_tags(params.get("tags"))
        abstract = _validate_bounded_text(params.get("abstractNote"),
                                          "abstractNote", TEXT_FIELD_LIMIT,
                                          required=False)
        extra = _validate_bounded_text(params.get("extra"), "extra",
                                       TEXT_FIELD_LIMIT, required=False)
        raw_cols = params.get("collections", [binding["collection_key"]])
        if not isinstance(raw_cols, list) or not raw_cols or len(raw_cols) > 20:
            _error("collections must be a non-empty list of at most 20")
        cols = [need_collection(c, "collections entry") for c in raw_cols]
        plan = {"itemType": item_type, "title": title, "creators": creators,
                "date": date, "DOI": doi, "url": url, "tags": tags,
                "abstractNote": abstract, "extra": extra,
                "collections": sorted(set(cols))}
        preview = {"operation": operation,
                   "summary": f"Create {item_type} {title!r} in "
                              f"{len(cols)} collection(s)",
                   "newItem": dict(plan),
                   "versions": {},
                   "note": "DOI/URL are stored verbatim; no metadata "
                           "lookup is performed (deferred)."}
        return plan, preview

    if operation in ("add-existing", "add-membership"):
        for key in params:
            if key not in ("item_key", "collection_key"):
                _error(f"{operation} params has unsupported field: {key}")
        if "item_key" not in params:
            _error(f"{operation} needs item_key")
        item_key = _validate_item_key(params.get("item_key"))
        target = _validate_collection_key(
            params.get("collection_key", binding["collection_key"]),
            "collection_key")
        if target not in descendants:
            _error("collection_key is outside the linked collection scope")
        raw, _ = _fetch_item(base, prefix, binding["server_id"], item_key)
        norm = _norm_item(raw)
        if norm.get("inTrash"):
            _error("item is in the trash")
        if target in norm.get("collections", []):
            _error("item is already in that collection")
        plan = {"item_key": item_key, "collection_key": target,
                "version": int(norm.get("version", 0)),
                "collections": list(norm.get("collections", []))}
        preview = {"operation": operation,
                   "summary": f"Add item {item_key} to collection {target}",
                   "item": norm,
                   "targetCollection": by_key.get(target, {"key": target}),
                   "versions": {"item_version": plan["version"]}}
        return plan, preview

    if operation == "remove-membership":
        for key in params:
            if key not in ("item_key", "collection_key"):
                _error("remove-membership params has unsupported field: "
                       f"{key}")
        if "item_key" not in params or "collection_key" not in params:
            _error("remove-membership needs item_key and collection_key")
        item_key = _validate_item_key(params.get("item_key"))
        target = _validate_collection_key(params.get("collection_key"),
                                          "collection_key")
        if target not in descendants:
            _error("collection_key is outside the linked collection scope")
        raw, _ = _fetch_item(base, prefix, binding["server_id"], item_key)
        norm = _norm_item(raw)
        if target not in norm.get("collections", []):
            _error("item is not in that collection")
        plan = {"item_key": item_key, "collection_key": target,
                "version": int(norm.get("version", 0)),
                "collections": list(norm.get("collections", []))}
        preview = {"operation": operation,
                   "summary": f"Remove item {item_key} from {target}",
                   "item": norm,
                   "versions": {"item_version": plan["version"]}}
        return plan, preview

    if operation == "update-item":
        for key in params:
            if key not in ("item_key", "version", "patch"):
                _error(f"update-item params has unsupported field: {key}")
        if "item_key" not in params or "version" not in params \
                or "patch" not in params:
            _error("update-item needs item_key, version, and patch")
        item_key = _validate_item_key(params.get("item_key"))
        version = _validate_version(params.get("version"))
        patch = _validate_patch(params.get("patch"))
        raw, _ = _fetch_item(base, prefix, binding["server_id"], item_key)
        norm = _norm_item(raw)
        if not _scope_check(base, prefix, binding["server_id"], norm,
                            descendants, {}):
            _error("zotero item is outside the linked collection scope")
        if int(norm.get("version", -1)) != version:
            _error("item version is stale; re-prepare against the live item")
        plan = {"item_key": item_key, "version": version, "patch": patch}
        preview = {"operation": operation,
                   "summary": f"Update item {item_key} "
                              f"(fields: {', '.join(sorted(patch))})",
                   "before": norm, "after": patch,
                   "versions": {"item_version": version}}
        return plan, preview

    if operation == "create-subcollection":
        for key in params:
            if key not in ("name", "parent_key"):
                _error("create-subcollection params has unsupported field: "
                       f"{key}")
        name = _validate_bounded_text(params.get("name"), "name",
                                      NAME255_LIMIT)
        parent = _validate_collection_key(
            params.get("parent_key", binding["collection_key"]),
            "parent_key")
        if parent not in descendants:
            _error("parent_key is outside the linked collection scope")
        plan = {"name": name, "parent_key": parent}
        preview = {"operation": operation,
                   "summary": f"Create subcollection {name!r} under {parent}",
                   "parent": by_key.get(parent, {"key": parent}),
                   "versions": {}}
        return plan, preview

    if operation == "update-subcollection":
        for key in params:
            if key not in ("collection_key", "version", "name",
                           "parentCollection"):
                _error("update-subcollection params has unsupported field: "
                       f"{key}")
        if "collection_key" not in params or "version" not in params:
            _error("update-subcollection needs collection_key and version")
        key = _validate_collection_key(params.get("collection_key"))
        if key not in descendants:
            _error("collection_key is outside the linked collection scope")
        version = _validate_version(params.get("version"))
        current = by_key.get(key)
        if current is None:
            _error("collection was not found in the library")
        if int(current.get("version", -1)) != version:
            _error("collection version is stale; re-prepare")
        changes: dict = {}
        if "name" in params:
            changes["name"] = _validate_bounded_text(params.get("name"),
                                                     "name", NAME255_LIMIT)
        if "parentCollection" in params:
            new_parent = params.get("parentCollection")
            if new_parent is not None:
                new_parent = _validate_collection_key(new_parent,
                                                      "parentCollection")
                if new_parent not in descendants:
                    _error("parentCollection is outside the linked scope")
                if new_parent == key:
                    _error("a collection cannot be its own parent")
            if key == binding["collection_key"] and new_parent != current.get(
                    "parentCollection"):
                _error("the bound root collection cannot be reparented "
                       "(rename only)")
            changes["parentCollection"] = new_parent
        if not changes:
            _error("update-subcollection needs name and/or parentCollection")
        plan = {"collection_key": key, "version": version, "changes": changes}
        preview = {"operation": operation,
                   "summary": f"Update collection {key}: "
                              f"{', '.join(sorted(changes))}",
                   "before": current, "after": changes,
                   "versions": {"collection_version": version}}
        return plan, preview

    _error(f"unknown operation: {operation}")
    return {}, {}  # unreachable


def cmd_prepare(payload: dict, base: str, registry_file=None) -> dict:
    project_id, operation, params = _split_prepare(payload)
    binding = _load_binding(project_id, registry_file)
    plan_params, preview = _prepare_operation(base, binding, operation,
                                              params)
    plan = {
        "v": 1,
        "project_id": project_id,
        "operation": operation,
        "params": plan_params,
        "preview": preview,
        "binding": {
            "server_id": binding["server_id"],
            "library_type": binding["library_type"],
            "library_id": binding["library_id"],
            "collection_key": binding["collection_key"],
            "include_subcollections": binding["include_subcollections"],
        },
    }
    token = _write_plan(plan)
    return {
        "prepared": token,
        "preview": preview,
        "binding": {
            "server_id": binding["server_id"],
            "library": _library_identity(binding),
            "collection_key": binding["collection_key"],
            "include_subcollections": binding["include_subcollections"],
        },
        "expires_in": int(PREPARE_TTL),
    }


def cmd_preview(payload: dict, base: str, registry_file=None) -> dict:
    if not isinstance(payload, dict):
        _error("preview payload must be an object")
    for key in payload:
        if key not in ("project_id", "prepared"):
            _error(f"preview has an unsupported field: {key}")
    if "project_id" not in payload or "prepared" not in payload:
        _error("preview needs project_id and prepared")
    project_id = _validate_project_id(payload.get("project_id"))
    plan, plan_path = _read_plan(payload.get("prepared"))
    if plan.get("project_id") != project_id:
        _error("prepared plan belongs to a different project")
    operation = plan.get("operation")
    if operation not in _OPERATIONS:
        _error("prepared plan has an unknown operation")
    params = plan.get("params")
    if not isinstance(params, dict):
        _error("prepared plan is corrupt")
    preview = plan.get("preview")
    if not isinstance(preview, dict):
        _error("prepared plan is corrupt")
    binding = _load_binding(project_id, registry_file)
    saved_binding = plan.get("binding", {})
    for field in ("server_id", "library_type", "library_id",
                  "collection_key", "include_subcollections"):
        if saved_binding.get(field) != binding.get(field):
            _error("registry link changed since prepare; re-prepare")
    try:
        with open(plan_path, "rb") as handle:
            stored = json.loads(handle.read(INPUT_LIMIT + 1).decode("utf-8"))
        created = float(stored.get("created_at", 0))
        remaining = int(PREPARE_TTL - (time.time() - created))
    except (OSError, ValueError, TypeError, UnicodeDecodeError,
            json.JSONDecodeError, AttributeError):
        remaining = int(PREPARE_TTL)
    if remaining < 0:
        remaining = 0
    if remaining > int(PREPARE_TTL):
        remaining = int(PREPARE_TTL)
    return {
        "preview": preview,
        "binding": {
            "server_id": binding["server_id"],
            "library": _library_identity(binding),
            "collection_key": binding["collection_key"],
            "include_subcollections": binding["include_subcollections"],
        },
        "expires_in": remaining,
    }


def _write_headers(server_id: str, api_key: str,
                   write_token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json",
               "Zotero-API-Version": "3",
               "Zotero-Server-ID": server_id,
               "Zotero-API-Key": api_key}
    if write_token:
        headers["Zotero-Write-Token"] = write_token
    return headers


def _library_version(headers: dict):
    raw = headers.get("last-modified-version", "")
    try:
        return int(raw)
    except (ValueError, TypeError):
        return raw or None


def cmd_apply(payload: dict, base: str, registry_file=None) -> dict:
    if not isinstance(payload, dict):
        _error("apply payload must be an object")
    for key in payload:
        if key not in ("project_id", "prepared"):
            _error(f"apply has an unsupported field: {key}")
    if "project_id" not in payload or "prepared" not in payload:
        _error("apply needs project_id and prepared")
    project_id = _validate_project_id(payload.get("project_id"))
    plan, plan_path = _read_plan(payload.get("prepared"))
    if plan.get("project_id") != project_id:
        _error("prepared plan belongs to a different project")
    operation = plan.get("operation")
    if operation not in _OPERATIONS:
        _error("prepared plan has an unknown operation")
    params = plan.get("params")
    if not isinstance(params, dict):
        _error("prepared plan is corrupt")
    binding = _load_binding(project_id, registry_file)
    saved_binding = plan.get("binding", {})
    for field in ("server_id", "library_type", "library_id",
                  "collection_key", "include_subcollections"):
        if saved_binding.get(field) != binding.get(field):
            _error("registry link changed since prepare; re-prepare")
    server_id = binding["server_id"]
    live_server, _ = _server_id_from_base(base)
    if live_server != server_id:
        _error("zotero server changed (412); the registry link points at "
               "a different database — re-link the collection")
    # Independently revalidate: live versions, scope, and param shapes.
    descendants, collections, _ = _resolve_scope(base, binding)
    by_key = {c["key"]: c for c in collections}
    prefix = _prefix(binding)
    api_key = _get_api_key(server_id)
    write_token = secrets.token_hex(16)
    result: dict

    if operation == "add-item":
        # Revalidate shapes (never trust stored sizes blindly).
        item_type = params.get("itemType")
        if item_type not in _ITEM_TYPES:
            _error("prepared plan is corrupt")
        title = _validate_bounded_text(params.get("title"), "title",
                                       TITLE_LIMIT)
        creators = _validate_creators(params.get("creators", []))
        tags = _validate_tags(params.get("tags", []))
        cols = params.get("collections", [])
        if not isinstance(cols, list) or not cols:
            _error("prepared plan is corrupt")
        for col in cols:
            if col not in descendants:
                _error("prepared target left the linked scope; re-prepare")
        body_obj: dict = {"itemType": item_type, "title": title,
                          "collections": sorted(set(cols))}
        if creators:
            body_obj["creators"] = creators
        for field in ("date", "DOI", "url", "abstractNote", "extra"):
            value = params.get(field, "")
            if value:
                body_obj[field] = value
        if tags:
            body_obj["tags"] = [{"tag": t} for t in tags]
        status, resp_headers, resp_body = _http(
            "POST", f"{base}{prefix}/items",
            _write_headers(server_id, api_key, write_token),
            json.dumps([body_obj]).encode("utf-8"))
        _check_api_error(status, resp_headers, resp_body, "zotero add-item")
        outcome = _decode_json(resp_body, "zotero add-item")
        try:
            created = outcome["successful"]["0"]
        except (KeyError, TypeError, IndexError) as exc:
            raise ZoteroError("zotero add-item reported failure") from exc
        try:
            plan_path.unlink()
        except OSError:
            pass
        result = {"key": created.get("key"), "version": created.get("version")}
        return {"ok": True, "operation": operation,
                "binding": saved_binding, "result": result,
                "libraryVersion": _library_version(resp_headers)}

    if operation in ("add-existing", "add-membership"):
        item_key = _validate_item_key(params.get("item_key"))
        target = _validate_collection_key(params.get("collection_key"))
        if target not in descendants:
            _error("prepared target left the linked scope; re-prepare")
        raw, _ = _fetch_item(base, prefix, server_id, item_key)
        norm = _norm_item(raw)
        if int(norm.get("version", -1)) != int(params.get("version", -2)):
            _error("item changed since prepare; re-prepare")
        if target in norm.get("collections", []):
            _error("item is already in that collection")
        merged = sorted(set(norm.get("collections", [])) | {target})
        status, resp_headers, resp_body = _http(
            "PATCH", f"{base}{prefix}/items/{item_key}",
            _write_headers(server_id, api_key),
            json.dumps({"version": norm["version"],
                        "collections": merged}).encode("utf-8"))
        _check_api_error(status, resp_headers, resp_body,
                         "zotero add-membership")
        try:
            plan_path.unlink()
        except OSError:
            pass
        result = {"key": item_key, "collection_key": target,
                  "collections": merged}
        return {"ok": True, "operation": operation,
                "binding": saved_binding, "result": result,
                "libraryVersion": _library_version(resp_headers)}

    if operation == "remove-membership":
        item_key = _validate_item_key(params.get("item_key"))
        target = _validate_collection_key(params.get("collection_key"))
        if target not in descendants:
            _error("prepared target left the linked scope; re-prepare")
        raw, _ = _fetch_item(base, prefix, server_id, item_key)
        norm = _norm_item(raw)
        if int(norm.get("version", -1)) != int(params.get("version", -2)):
            _error("item changed since prepare; re-prepare")
        if target not in norm.get("collections", []):
            _error("item is not in that collection")
        merged = sorted(set(norm.get("collections", [])) - {target})
        status, resp_headers, resp_body = _http(
            "PATCH", f"{base}{prefix}/items/{item_key}",
            _write_headers(server_id, api_key),
            json.dumps({"version": norm["version"],
                        "collections": merged}).encode("utf-8"))
        _check_api_error(status, resp_headers, resp_body,
                         "zotero remove-membership")
        try:
            plan_path.unlink()
        except OSError:
            pass
        result = {"key": item_key, "collection_key": target,
                  "collections": merged}
        return {"ok": True, "operation": operation,
                "binding": saved_binding, "result": result,
                "libraryVersion": _library_version(resp_headers)}

    if operation == "update-item":
        item_key = _validate_item_key(params.get("item_key"))
        version = _validate_version(params.get("version"))
        patch = _validate_patch(params.get("patch"))
        raw, _ = _fetch_item(base, prefix, server_id, item_key)
        norm = _norm_item(raw)
        if not _scope_check(base, prefix, server_id, norm, descendants, {}):
            _error("zotero item is outside the linked collection scope")
        if int(norm.get("version", -1)) != version:
            _error("item changed since prepare; re-prepare")
        body_obj = dict(patch)
        body_obj["version"] = version
        if "tags" in body_obj:
            body_obj["tags"] = [{"tag": t} for t in body_obj["tags"]]
        status, resp_headers, resp_body = _http(
            "PATCH", f"{base}{prefix}/items/{item_key}",
            _write_headers(server_id, api_key),
            json.dumps(body_obj).encode("utf-8"))
        _check_api_error(status, resp_headers, resp_body, "zotero update-item")
        try:
            plan_path.unlink()
        except OSError:
            pass
        result = {"key": item_key, "patched": sorted(patch)}
        return {"ok": True, "operation": operation,
                "binding": saved_binding, "result": result,
                "libraryVersion": _library_version(resp_headers)}

    if operation == "create-subcollection":
        name = _validate_bounded_text(params.get("name"), "name",
                                      NAME255_LIMIT)
        parent = _validate_collection_key(params.get("parent_key"))
        if parent not in descendants:
            _error("prepared parent left the linked scope; re-prepare")
        status, resp_headers, resp_body = _http(
            "POST", f"{base}{prefix}/collections",
            _write_headers(server_id, api_key, write_token),
            json.dumps([{"name": name,
                         "parentCollection": parent}]).encode("utf-8"))
        _check_api_error(status, resp_headers, resp_body,
                         "zotero create-subcollection")
        outcome = _decode_json(resp_body, "zotero create-subcollection")
        try:
            created = outcome["successful"]["0"]
        except (KeyError, TypeError, IndexError) as exc:
            raise ZoteroError(
                "zotero create-subcollection reported failure") from exc
        try:
            plan_path.unlink()
        except OSError:
            pass
        result = {"key": created.get("key"),
                  "version": created.get("version")}
        return {"ok": True, "operation": operation,
                "binding": saved_binding, "result": result,
                "libraryVersion": _library_version(resp_headers)}

    if operation == "update-subcollection":
        key = _validate_collection_key(params.get("collection_key"))
        version = _validate_version(params.get("version"))
        changes = params.get("changes")
        if not isinstance(changes, dict) or not changes:
            _error("prepared plan is corrupt")
        if key not in descendants:
            _error("prepared collection left the linked scope; re-prepare")
        current = by_key.get(key)
        if current is None:
            _error("collection was not found in the library")
        if int(current.get("version", -1)) != version:
            _error("collection changed since prepare; re-prepare")
        body_obj = {"version": version}
        if "name" in changes:
            body_obj["name"] = _validate_bounded_text(changes.get("name"),
                                                      "name", NAME255_LIMIT)
        if "parentCollection" in changes:
            new_parent = changes.get("parentCollection")
            if new_parent is not None:
                new_parent = _validate_collection_key(new_parent)
                if new_parent not in descendants:
                    _error("prepared parent left the linked scope")
            if key == binding["collection_key"] and new_parent != current.get(
                    "parentCollection"):
                _error("the bound root collection cannot be reparented")
            body_obj["parentCollection"] = new_parent
        status, resp_headers, resp_body = _http(
            "PATCH", f"{base}{prefix}/collections/{key}",
            _write_headers(server_id, api_key),
            json.dumps(body_obj).encode("utf-8"))
        _check_api_error(status, resp_headers, resp_body,
                         "zotero update-subcollection")
        try:
            plan_path.unlink()
        except OSError:
            pass
        result = {"key": key, "changed": sorted(changes)}
        return {"ok": True, "operation": operation,
                "binding": saved_binding, "result": result,
                "libraryVersion": _library_version(resp_headers)}

    _error(f"unknown operation: {operation}")
    return {}  # unreachable


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# CLI (shared plumbing lives in qscli.py; argv stays byte-identical)
# ---------------------------------------------------------------------------

def _parse_args(argv) -> argparse.Namespace:
    parser = qscli.SafeParser(description=__doc__)
    qscli.add_global_flags(parser, projects_file=True)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("command",
                        choices=("capabilities", "authorize", "collections",
                                 "libraries", "search", "item", "read-pdf",
                                 "prepare", "preview", "apply"))
    return parser.parse_args(argv if argv is not None else sys.argv[1:])


def _dispatch(args: argparse.Namespace) -> dict:
    try:
        base = _resolve_base_url(args.base_url)
        payload = qscli.read_input()
        if args.command == "capabilities":
            return cmd_capabilities(payload, base, args.projects_file)
        elif args.command == "authorize":
            return cmd_authorize(payload, base, args.projects_file)
        elif args.command == "collections":
            return cmd_collections(payload, base, args.projects_file)
        elif args.command == "libraries":
            return cmd_libraries(payload, base, args.projects_file)
        elif args.command == "search":
            return cmd_search(payload, base, args.projects_file)
        elif args.command == "item":
            return cmd_item(payload, base, args.projects_file)
        elif args.command == "read-pdf":
            return cmd_read_pdf(payload, base, args.projects_file)
        elif args.command == "prepare":
            return cmd_prepare(payload, base, args.projects_file)
        elif args.command == "preview":
            return cmd_preview(payload, base, args.projects_file)
        else:
            return cmd_apply(payload, base, args.projects_file)
    except Exception as exc:
        # Never leak the write-key override into diagnostics: redact the
        # ZOTERO_API_KEY value before run_main formats the error.
        try:
            message = str(exc)
        except Exception:
            raise
        secret = os.environ.get(API_KEY_ENV, "")
        if secret and secret in message:
            raise ZoteroError(
                message.replace(secret, "[redacted]")) from exc
        if not message:
            raise ZoteroError("zotero request failed") from exc
        raise


_BOUNDED_EXCEPTIONS = (ZoteroError, OSError, TypeError, ValueError,
                       UnicodeError, RecursionError, OverflowError)


def main(argv=None) -> int:
    return qscli.run_main(_parse_args, _dispatch, "zotero",
                          _BOUNDED_EXCEPTIONS, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
