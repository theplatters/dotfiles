#!/usr/bin/env python3
"""Standalone persistent project registry backend (TOML file, no graph needed).

Registry location (first non-empty wins):

1. explicit ``--projects-file`` CLI argument,
2. ``QUICKSHELL_PROJECTS_FILE`` environment variable,
3. ``<repo-root>/projects.toml`` where repo-root is
   ``Path(__file__).resolve().parent.parent``.

``list``/``create``/``update``/``remove`` never require a Logseq graph.
Only ``import-logseq`` requires ``--graph`` (or LOGSEQ_GRAPH/settings.json).

TOML schema (strict; unknown fields are rejected, never silently preserved):

    version = 1

    [[projects]]
    id = "<uuid>"            # required, stable, unique
    name = "..."             # required, non-blank
    logseq_path = "pages/X.md"  # optional, omitted when empty
    local_folder = "/abs/path" or "~/path"  # optional, omitted when empty
    github_url = "https://github.com/owner/repo"  # optional, omitted when empty
    # optional, omitted when unlinked:
    # zotero_collection = { server_id = "<Zotero-Server-ID>",
    #   library_type = "user" | "group", library_id = "0" (digit string;
    #   user "0" is the server-bound user-library alias),
    #   collection_key = "ABCDEFGH" (8 uppercase alnum),
    #   include_subcollections = true (default) }

Validation contract:

- ``id`` must parse as a UUID (stored lowercase canonical).
- ``name`` must be non-blank after stripping (max 512 chars).
- ``logseq_path`` when non-empty must be a safe normalized graph-relative
  page path: ``pages/*.md`` (case-insensitive suffix), no backslash/NUL,
  not absolute, first segment ``pages``, no ``""/. /..`` components,
  length <= 4096. Stored as POSIX string.
- ``local_folder`` when non-empty must be absolute (``/abs``) or home
  (``~`` or ``~/...``), no NUL/backslash, length <= 4096, lexically
  normalized via ``posixpath.normpath``. It need not exist at registry
  write time; use-time callers (files_* integration) re-resolve strictly
  and apply the full ``project_files`` safety checks.
- ``github_url`` when non-empty must be an HTTPS ``github.com`` (or
  ``www.github.com``, normalized to ``github.com``) repository URL
  ``https://github.com/<owner>/<repo>`` (optional trailing ``/`` or
  ``.git`` stripped). Owner/repo segments match ``[A-Za-z0-9_.-]+``.
- ``zotero_collection`` is optional and unlinks when absent or ``null``
  (JSON) / omitted (TOML). When present it must be an object with exactly
  ``server_id`` (non-blank bounded string, 1..256 chars, no NUL/controls/
  whitespace), ``library_type`` (``user`` or ``group``), ``library_id``
  (digit string 1..20 chars; ``user`` libraries may use ``"0"`` as the
  local personal library pinned by ``server_id``; ``group`` libraries must
  be non-zero), ``collection_key`` (exactly 8 uppercase alphanumerics
  ``[A-Z0-9]{8}``), and optional ``include_subcollections`` (strict bool,
  defaults to ``true`` when omitted). Unknown sub-fields are rejected.
  TOML stores a linked value as an inline table
  (``zotero_collection = {server_id=..., ...}``) and omits the key when
  unlinked; JSON reads back linked values as objects and unlinked values
  as ``null``.
- Duplicate ``id`` values are rejected. Duplicate non-empty ``logseq_path``
  values are rejected (two registry entries must not claim the same note).
- The registry holds at most ``MAX_PROJECTS`` entries. The cap is enforced
  on read and, centrally in the serializer, on every mutation result before
  any write, so a mutation that would exceed the cap fails leaving the
  previous file bytes (and readability) unchanged.
- Optional empty values (``""`` / whitespace-only / missing) are stored as
  omitted TOML keys and read back as ``""``. A name-only project (all
  optionals empty) is valid.
- Corrupt TOML (unparseable, wrong version, wrong types, oversized,
  non-regular/symlink file) is never silently overwritten: mutations fail
  with a useful error under the registry lock.
- Unknown top-level keys, unknown ``[[projects]]`` keys, or a missing
  ``version`` are schema errors.

JSON responses (all commands)::

    {"projects": [{"id":..., "name":..., "logseq_path":"", "local_folder":"",
                   "github_url":"", "zotero_collection":null|{...},
                   "path":<logseq_path>, "page":<name>}],
     "revision": "<sha256 of registry file bytes, or sha256(b\"\") when missing>",
     "file": "<absolute registry path>"}

``path`` duplicates ``logseq_path`` and ``page`` duplicates ``name`` for
the UI worker. ``projects`` is sorted by ``(name.casefold(), id)``.
Mutations (``create``/``update``) additionally return ``project`` (the saved
record in the same shape). ``remove`` omits ``project``. ``import-logseq``
additionally returns ``warnings: [...]`` (strings) and ``imported: <int>``.

CLI::

    projects.py [--projects-file PATH] [--graph DIR] list
    projects.py [--projects-file PATH] create        # stdin JSON object
    projects.py [--projects-file PATH] update        # stdin JSON object
    projects.py [--projects-file PATH] remove        # stdin JSON object
    projects.py [--projects-file PATH] [--graph DIR] import-logseq  # no stdin

Stdin shapes:

- create: ``{"name":..., "logseq_path"?:..., "local_folder"?:...,
  "github_url"?:..., "zotero_collection"?:null|{...}}`` (unknown keys rejected).
- update: ``{"id":..., "revision":..., "name":..., "logseq_path"?:...,
  "local_folder"?:..., "github_url"?:..., "zotero_collection"?:null|{...}}``
  (full replacement; missing optionals clear to ``""`` and a missing/null
  ``zotero_collection`` unlinks; stale ``revision`` rejected).
- remove: ``{"id":..., "revision":...}`` (stale ``revision`` rejected).
- import-logseq: no stdin; scans the graph for new project notes only,
  never updates existing entries and never writes to the graph.

``import-logseq`` recognition (conservative):

- Reads every graph page via the existing ``project_planner`` safe readers.
- Parses only the leading page-property block (column-zero ``key:: value``
  lines; stops at the first other content, so body/nested ``- file::``
  lines and fenced samples never count).
- A note is a project when ``type:: project`` OR ``status:: project``
  (plain or ``[[project]]``, case-insensitive).
- ``file::`` is extracted via the existing ``project_files`` preamble
  parser. ``file://home/franzs/...`` (missing leading slash) is read as
  ``/home/franzs/...``. ``file://~...`` (e.g. ``file://~Arbeit/...``) is
  ambiguous: the folder is skipped (``""``) with a ``warnings`` entry
  rather than inventing a location. Markdown ``[label](target)`` targets,
  ``<autolinks>``, quotes and ``[[links]]`` are unwrapped with the same
  conservative helper. Duplicate ``file::`` lines skip the folder with a
  warning. Relative folder targets are skipped with a warning (registry
  only stores absolute or ``~/``).
- ``github::``/``url::`` leading values are unwrapped the same way
  (so ``url:: [Github](https://github.com/owner/repo)`` works) and kept
  only when they validate as GitHub repo URLs; otherwise skipped with a
  warning. Missing values are fine.
- Only notes whose non-empty ``logseq_path`` is not already registered are
  added (idempotent). Entries are never modified in place.

IO safety: bounded symlink-safe reads (``O_NOFOLLOW``, regular-file check,
size cap), atomic mode-preserving writes (temp ``O_EXCL|O_NOFOLLOW`` +
``fsync`` + ``os.replace`` + dir ``fsync``), bounded ``fcntl`` advisory
lock with timeout for all mutations (create merges atomically under the
lock; update/remove reject stale revisions).
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import posixpath
import re
import stat
import sys
import time
import uuid
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import qscli


class RegistryError(ValueError):
    pass


DEFAULT_FILE = Path(__file__).resolve().parent.parent / "projects.toml"
ENV_VAR = "QUICKSHELL_PROJECTS_FILE"

REGISTRY_LIMIT = 512 * 1024
# Grandfathered: narrower than qscli.INPUT_LIMIT (1 MiB) and load-bearing
# for this CLI's stdin transport, so the local _read_input below keeps
# this cap instead of qscli.read_input. Do not unify by accident.
INPUT_LIMIT = 256 * 1024
PATH_LIMIT = 4096
NAME_LIMIT = 512
GITHUB_LIMIT = 2048
SERVER_ID_LIMIT = 256
LIBRARY_ID_LIMIT = 20
MAX_PROJECTS = 5000
LOCK_TIMEOUT = 2.0
_HEX_REVISION = re.compile(r"^[0-9a-f]{64}$")
_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_COLLECTION_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")
_LIBRARY_ID_RE = re.compile(r"^[0-9]{1,20}$")
_MARKDOWN_LINK_RE = re.compile(r"\[.*?\]\(\s*(?P<target>.+?)\s*\)\s*$")
_PROPERTY_RE = re.compile(
    r"^(?P<key>[A-Za-z][A-Za-z0-9_-]*)\s*::\s*(?P<value>.*)$")

MISSING_REVISION = hashlib.sha256(b"").hexdigest()

_ALLOWED_PROJECT_KEYS = {"id", "name", "logseq_path", "local_folder",
                         "github_url", "zotero_collection"}
_ALLOWED_CREATE_KEYS = {"name", "logseq_path", "local_folder", "github_url",
                        "zotero_collection"}
_ALLOWED_UPDATE_KEYS = {"id", "revision", "name", "logseq_path",
                        "local_folder", "github_url", "zotero_collection"}
_ALLOWED_REMOVE_KEYS = {"id", "revision"}
_ALLOWED_ZOTERO_KEYS = {"server_id", "library_type", "library_id",
                        "collection_key", "include_subcollections"}


def _error(message: str) -> NoReturn:
    raise RegistryError(message)


# ---------------------------------------------------------------------------
# Registry path resolution
# ---------------------------------------------------------------------------

def resolve_registry_file(explicit=None) -> Path:
    """Resolve the registry file honoring --projects-file > env > default."""
    raw = None
    if explicit is not None and str(explicit).strip():
        raw = str(explicit).strip()
    elif isinstance(os.environ.get(ENV_VAR, ""), str) and \
            os.environ.get(ENV_VAR, "").strip():
        raw = os.environ.get(ENV_VAR, "").strip()
    else:
        return DEFAULT_FILE
    if len(raw) > PATH_LIMIT or "\x00" in raw:
        _error("registry path is too long or contains NUL")
    expanded = os.path.expanduser(raw)
    candidate = Path(expanded)
    if not candidate.is_absolute():
        # Lexical join only: never resolve symlinks here. Resolving would
        # dereference a user-supplied relative symlink before the O_NOFOLLOW
        # open, silently operating on the link target instead of rejecting
        # the link. os.path.abspath normalizes lexically (normpath) without
        # touching the filesystem.
        try:
            lexical = os.path.abspath(os.path.join(os.getcwd(), expanded))
        except OSError as exc:
            raise RegistryError("registry path is not accessible") from exc
        candidate = Path(lexical)
    return candidate


# ---------------------------------------------------------------------------
# Bounded symlink-safe IO + locking
# ---------------------------------------------------------------------------

def _read_fd(fd, limit):
    chunks = []
    remaining = limit + 1
    while remaining:
        try:
            chunk = os.read(fd, min(32768, remaining))
        except BlockingIOError as exc:
            raise RegistryError("registry is not a regular readable file") from exc
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    value = b"".join(chunks)
    if len(value) > limit:
        _error(f"registry is larger than {limit} bytes")
    return value


def _read_registry_bytes(path: Path) -> bytes | None:
    """Return file bytes, or None when the file does not exist.

    Symlinks, non-regular files, and oversized files are errors (never
    silently treated as missing).
    """
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RegistryError("registry path is unsafe or missing") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _error("registry path is not a regular file")
        return _read_fd(fd, REGISTRY_LIMIT)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _revision_of(raw: bytes | None) -> str:
    if raw is None:
        return MISSING_REVISION
    return hashlib.sha256(raw).hexdigest()


def _lock_path_for(registry: Path) -> Path:
    return Path(str(registry) + ".lock")


def _open_parent_dir(path: Path) -> int:
    parent = path.parent if str(path.parent) else Path(".")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        return os.open(str(parent), flags)
    except OSError as exc:
        raise RegistryError("registry directory is unsafe or missing") from exc


class _RegistryLock:
    """Bounded advisory lock for registry mutations (sibling .lock file)."""

    def __init__(self, registry: Path):
        self.registry = registry
        self.parent_fd = None
        self.lock_fd = None

    def __enter__(self):
        self.parent_fd = _open_parent_dir(self.registry)
        try:
            info = os.fstat(self.parent_fd)
            if not stat.S_ISDIR(info.st_mode):
                _error("registry directory is not a directory")
        except OSError as exc:
            self.close()
            raise RegistryError("registry directory is unsafe") from exc
        name = self.registry.name + ".lock"
        try:
            self.lock_fd = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=self.parent_fd,
            )
        except OSError as exc:
            self.close()
            raise RegistryError("cannot lock registry safely") from exc
        try:
            if not stat.S_ISREG(os.fstat(self.lock_fd).st_mode):
                _error("registry lock path is not a regular file")
        except OSError as exc:
            self.close()
            raise RegistryError("cannot lock registry safely") from exc
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.close()
                    _error("registry lock is busy; retry later")
            except OSError as exc:
                self.close()
                raise RegistryError("cannot lock registry safely") from exc
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        return self

    def close(self):
        for name in ("lock_fd", "parent_fd"):
            fd = getattr(self, name)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, name, None)

    def __exit__(self, *_args):
        self.close()


def _atomic_write_registry(path: Path, data: bytes) -> None:
    """Atomically replace the registry, preserving mode when replacing."""
    if len(data) > REGISTRY_LIMIT:
        _error("registry is too large to write")
    parent_fd = _open_parent_dir(path)
    try:
        try:
            info_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                              dir_fd=parent_fd)
        except FileNotFoundError:
            mode = 0o644
            info_fd = None
        except OSError as exc:
            raise RegistryError("registry path is unsafe") from exc
        else:
            try:
                info = os.fstat(info_fd)
                if not stat.S_ISREG(info.st_mode):
                    _error("registry path is not a regular file")
                mode = stat.S_IMODE(info.st_mode)
            finally:
                try:
                    os.close(info_fd)
                except OSError:
                    pass
        temporary = None
        temp_fd = None
        try:
            for attempt in range(10):
                candidate = f".projects-{os.getpid()}-{attempt}.tmp"
                try:
                    temp_fd = os.open(candidate,
                                      os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                      os.O_NOFOLLOW,
                                      mode, dir_fd=parent_fd)
                    temporary = candidate
                    break
                except FileExistsError:
                    continue
            if temp_fd is None or temporary is None:
                _error("cannot create temporary registry file")
            assert temp_fd is not None and temporary is not None
            os.fchmod(temp_fd, mode)
            view = memoryview(data)
            while view:
                view = view[os.write(temp_fd, view):]
            os.fsync(temp_fd)
            os.close(temp_fd)
            temp_fd = None
            try:
                os.replace(temporary, path.name, src_dir_fd=parent_fd,
                           dst_dir_fd=parent_fd)
            except OSError as exc:
                raise RegistryError(f"cannot replace registry safely: {exc}") from exc
            temporary = None
            try:
                os.fsync(parent_fd)
            except OSError as exc:
                raise RegistryError(f"cannot sync registry directory: {exc}") from exc
        finally:
            if temp_fd is not None:
                try:
                    os.close(temp_fd)
                except OSError:
                    pass
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=parent_fd)
                except OSError:
                    pass
    finally:
        try:
            os.close(parent_fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Field validation / normalization
# ---------------------------------------------------------------------------

def _validate_name(value: object) -> str:
    if not isinstance(value, str):
        _error("name must be a string")
    text = value.strip()
    if not text:
        _error("name must be non-blank")
    if len(text) > NAME_LIMIT:
        _error("name is too long")
    if "\x00" in text:
        _error("name contains NUL")
    return text


def _validate_logseq_path(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        _error("logseq_path must be a string")
    text = value.strip()
    if not text:
        return ""
    if len(text) > PATH_LIMIT or "\x00" in text or "\\" in text:
        _error("logseq_path is unsafe")
    path = Path(text)
    if path.is_absolute() or len(path.parts) < 2 or path.parts[0] != "pages":
        _error("logseq_path must name a page below pages/")
    if any(part in ("", ".", "..") for part in path.parts):
        _error("logseq_path contains an unsafe directory component")
    if path.suffix.casefold() != ".md":
        _error("logseq_path must name a markdown page")
    return path.as_posix()


def _normalize_local_folder(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        _error("local_folder must be a string")
    text = value.strip()
    # Strip surrounding quotes once (registry callers sometimes paste quoted).
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    if not text:
        return ""
    if len(text) > PATH_LIMIT or "\x00" in text or "\\" in text:
        _error("local_folder is unsafe")
    if text == "~" or text.startswith("~/"):
        rest = text[1:]  # "" or "/..."
        normalized_rest = posixpath.normpath(rest) if rest else ""
        # normpath("/") == "/"; "~/" + "/" would double; normalize carefully.
        if rest == "" or rest == "/":
            return "~"
        # rest always starts with "/" here; normpath keeps leading "/".
        if not normalized_rest.startswith("/"):
            _error("local_folder is unsafe")
        if normalized_rest == "/":
            return "~"
        return "~" + normalized_rest
    if not text.startswith("/"):
        _error("local_folder must be an absolute path or ~/ path")
    normalized = posixpath.normpath(text)
    if not normalized.startswith("/"):
        _error("local_folder is unsafe")
    return normalized


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1].strip()
    return value


def _unwrap_target(raw: str) -> str:
    value = _strip_quotes(raw.strip())
    if len(value) >= 2 and value.startswith("<") and value.endswith(">"):
        value = _strip_quotes(value[1:-1].strip())
    match = _MARKDOWN_LINK_RE.search(value)
    if match:
        value = _strip_quotes(match.group("target").strip())
    if value.startswith("[[") and value.endswith("]]"):
        value = _strip_quotes(value[2:-2].strip())
    return value


def _validate_github_url(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        _error("github_url must be a string")
    text = _unwrap_target(value)
    if not text:
        return ""
    if len(text) > GITHUB_LIMIT or "\x00" in text:
        _error("github_url is unsafe")
    try:
        parsed = urlparse(text)
    except ValueError as exc:
        raise RegistryError("github_url must be an HTTPS github.com repo URL") from exc
    if parsed.scheme != "https":
        _error("github_url must be an HTTPS github.com repo URL")
    host = (parsed.hostname or "").casefold()
    if host not in ("github.com", "www.github.com"):
        _error("github_url must be an HTTPS github.com repo URL")
    if parsed.params or parsed.query or parsed.fragment:
        _error("github_url must be an HTTPS github.com repo URL")
    path = parsed.path.strip()
    # Strip one trailing slash and one .git suffix for normalization.
    if path.endswith("/"):
        path = path.rstrip("/")
    if path.casefold().endswith(".git"):
        path = path[:len(path) - 4].rstrip("/")
    parts = [p for p in path.split("/") if p]
    if len(parts) != 2 or any(p in ("", ".", "..") for p in parts):
        _error("github_url must be an HTTPS github.com repo URL")
    for part in parts:
        if not _OWNER_REPO_RE.fullmatch(part):
            _error("github_url must be an HTTPS github.com repo URL")
    return f"https://github.com/{parts[0]}/{parts[1]}"


def _validate_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("id must be a UUID string")
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError as exc:
        raise RegistryError("id must be a UUID string") from exc


def _validate_revision(value: object) -> str:
    if not isinstance(value, str) or not _HEX_REVISION.fullmatch(value):
        _error("revision must be a SHA-256 hex digest")
    return value


def _validate_zotero_collection(value: object) -> dict | None:
    """Validate the optional ``zotero_collection`` registry field.

    ``None``/missing reads as unlinked (``None``). Otherwise the value must
    be a table with exactly ``server_id``, ``library_type``, ``library_id``,
    ``collection_key`` and optional ``include_subcollections`` (default
    ``True``). Unknown keys are rejected. ``server_id`` is a non-blank
    bounded string (1..256 chars, no NUL/controls/whitespace);
    ``library_type`` is ``'user'`` or ``'group'``; ``library_id`` is a digit
    string (1..20 chars; ``'0'`` is allowed only for ``user`` libraries as
    the server-bound local personal library alias pinned by ``server_id``);
    ``collection_key`` is 8 uppercase alphanumerics ``[A-Z0-9]{8}``;
    ``include_subcollections`` must be a strict bool. Returns the
    normalized dict (always with ``include_subcollections``).
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        _error("zotero_collection must be an object or null")
    for key in value:
        if key not in _ALLOWED_ZOTERO_KEYS:
            _error(f"zotero_collection has unsupported field: {key}")
    for req in ("server_id", "library_type", "library_id", "collection_key"):
        if req not in value:
            _error(f"zotero_collection is missing required field: {req}")
    server = value.get("server_id")
    if not isinstance(server, str):
        _error("zotero_collection server_id must be a string")
    server = server.strip()
    if not server or len(server) > SERVER_ID_LIMIT or "\x00" in server:
        _error("zotero_collection server_id is unsafe")
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in server):
        _error("zotero_collection server_id is unsafe")
    libtype = value.get("library_type")
    if libtype not in ("user", "group"):
        _error("zotero_collection library_type must be 'user' or 'group'")
    libid = value.get("library_id")
    if not isinstance(libid, str) or not _LIBRARY_ID_RE.fullmatch(libid):
        _error("zotero_collection library_id must be a digit string")
    if libtype == "group" and int(libid) == 0:
        _error("zotero_collection library_id '0' is allowed only for user libraries")
    ckey = value.get("collection_key")
    if not isinstance(ckey, str) or not _COLLECTION_KEY_RE.fullmatch(ckey):
        _error("zotero_collection collection_key must be 8 uppercase alphanumerics")
    inc = value.get("include_subcollections", True)
    if type(inc) is not bool:
        _error("zotero_collection include_subcollections must be a bool")
    return {
        "server_id": server,
        "library_type": libtype,
        "library_id": libid,
        "collection_key": ckey,
        "include_subcollections": inc,
    }


def _public_record(item: dict) -> dict:
    zc = item.get("zotero_collection")
    return {
        "id": item["id"],
        "name": item["name"],
        "logseq_path": item.get("logseq_path", ""),
        "local_folder": item.get("local_folder", ""),
        "github_url": item.get("github_url", ""),
        "zotero_collection": dict(zc) if isinstance(zc, dict) else None,
        "path": item.get("logseq_path", ""),
        "page": item.get("name", ""),
    }


# ---------------------------------------------------------------------------
# TOML parse / serialize (strict)
# ---------------------------------------------------------------------------

def _parse_registry(raw: bytes | None) -> list[dict]:
    """Parse registry bytes into normalized internal records (sorted)."""
    if raw is None:
        return []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RegistryError("registry is not valid UTF-8") from exc
    import tomllib
    try:
        data = tomllib.loads(text)
    except Exception as exc:
        raise RegistryError(f"registry TOML is corrupt: {exc}") from exc
    if not isinstance(data, dict):
        _error("registry TOML is corrupt: top level must be a table")
    allowed_top = {"version", "projects"}
    for key in data:
        if key not in allowed_top:
            _error(f"registry has unsupported field: {key}")
    if data.get("version") != 1:
        _error("registry version must be 1")
    items = data.get("projects", [])
    if items is None:
        items = []
    if not isinstance(items, list):
        _error("registry [[projects]] must be an array")
    if len(items) > MAX_PROJECTS:
        _error("registry holds too many projects")
    normalized: list[dict] = []
    seen_ids: set[str] = set()
    seen_notes: set[str] = set()
    for entry in items:
        if not isinstance(entry, dict):
            _error("registry project entries must be tables")
        for key in entry:
            if key not in _ALLOWED_PROJECT_KEYS:
                _error(f"registry project has unsupported field: {key}")
        pid = _validate_id(entry.get("id"))
        if pid in seen_ids:
            _error("registry has a duplicate project id")
        seen_ids.add(pid)
        name = _validate_name(entry.get("name"))
        logseq_path = _validate_logseq_path(entry.get("logseq_path", ""))
        local_folder = _normalize_local_folder(entry.get("local_folder", ""))
        github_url = _validate_github_url(entry.get("github_url", ""))
        zotero_collection = _validate_zotero_collection(entry.get("zotero_collection"))
        if logseq_path:
            if logseq_path in seen_notes:
                _error("registry has a duplicate project note")
            seen_notes.add(logseq_path)
        normalized.append({
            "id": pid,
            "name": name,
            "logseq_path": logseq_path,
            "local_folder": local_folder,
            "github_url": github_url,
            "zotero_collection": zotero_collection,
        })
    normalized.sort(key=lambda item: (item["name"].casefold(), item["id"]))
    return normalized


def _toml_escape(value: str) -> str:
    out = []
    for char in value:
        code = ord(char)
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif char == "\b":
            out.append("\\b")
        elif char == "\f":
            out.append("\\f")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04X}")
        else:
            out.append(char)
    return "".join(out)


def _serialize_registry(items: list[dict]) -> bytes:
    if len(items) > MAX_PROJECTS:
        _error("registry holds too many projects")
    lines = ["version = 1", ""]
    for item in sorted(items, key=lambda e: (e["name"].casefold(), e["id"])):
        lines.append("[[projects]]")
        lines.append(f'id = "{_toml_escape(item["id"])}"')
        lines.append(f'name = "{_toml_escape(item["name"])}"')
        if item.get("logseq_path"):
            lines.append(f'logseq_path = "{_toml_escape(item["logseq_path"])}"')
        if item.get("local_folder"):
            lines.append(f'local_folder = "{_toml_escape(item["local_folder"])}"')
        if item.get("github_url"):
            lines.append(f'github_url = "{_toml_escape(item["github_url"])}"')
        zc = item.get("zotero_collection")
        if isinstance(zc, dict):
            inc = "true" if zc.get("include_subcollections", True) else "false"
            lines.append(
                "zotero_collection = { "
                f'server_id = "{_toml_escape(zc["server_id"])}", '
                f'library_type = "{_toml_escape(zc["library_type"])}", '
                f'library_id = "{_toml_escape(zc["library_id"])}", '
                f'collection_key = "{_toml_escape(zc["collection_key"])}", '
                f"include_subcollections = {inc} }}")
        lines.append("")
    text = "\n".join(lines)
    return text.encode("utf-8")


def _response(registry: Path, raw: bytes | None, items: list[dict]) -> dict:
    return {
        "projects": [_public_record(item) for item in items],
        "revision": _revision_of(raw),
        "file": str(registry),
    }


# ---------------------------------------------------------------------------
# Public operations (graph-free except import)
# ---------------------------------------------------------------------------

def list_projects(registry_file=None) -> dict:
    registry = resolve_registry_file(registry_file)
    raw = _read_registry_bytes(registry)
    items = _parse_registry(raw)
    return _response(registry, raw, items)


def _check_unknown(payload: dict, allowed: set[str], what: str) -> None:
    for key in payload:
        if key not in allowed:
            _error(f"{what} has an unsupported field: {key}")


def create_project(payload: dict, registry_file=None) -> dict:
    if not isinstance(payload, dict):
        _error("create payload must be an object")
    _check_unknown(payload, _ALLOWED_CREATE_KEYS, "create payload")
    name = _validate_name(payload.get("name"))
    logseq_path = _validate_logseq_path(payload.get("logseq_path", ""))
    local_folder = _normalize_local_folder(payload.get("local_folder", ""))
    github_url = _validate_github_url(payload.get("github_url", ""))
    zotero_collection = _validate_zotero_collection(payload.get("zotero_collection"))
    registry = resolve_registry_file(registry_file)
    with _RegistryLock(registry):
        raw = _read_registry_bytes(registry)
        items = _parse_registry(raw)
        if logseq_path and any(e["logseq_path"] == logseq_path for e in items):
            _error("a project with this note already exists")
        new_id = str(uuid.uuid4())
        while any(e["id"] == new_id for e in items):
            new_id = str(uuid.uuid4())
        record = {"id": new_id, "name": name, "logseq_path": logseq_path,
                  "local_folder": local_folder, "github_url": github_url,
                  "zotero_collection": zotero_collection}
        items.append(record)
        items.sort(key=lambda e: (e["name"].casefold(), e["id"]))
        encoded = _serialize_registry(items)
        _atomic_write_registry(registry, encoded)
        raw = encoded
    response = _response(registry, raw, items)
    response["project"] = _public_record(record)
    return response


def update_project(payload: dict, registry_file=None) -> dict:
    if not isinstance(payload, dict):
        _error("update payload must be an object")
    _check_unknown(payload, _ALLOWED_UPDATE_KEYS, "update payload")
    pid = _validate_id(payload.get("id"))
    revision = _validate_revision(payload.get("revision"))
    name = _validate_name(payload.get("name"))
    logseq_path = _validate_logseq_path(payload.get("logseq_path", ""))
    local_folder = _normalize_local_folder(payload.get("local_folder", ""))
    github_url = _validate_github_url(payload.get("github_url", ""))
    zotero_collection = _validate_zotero_collection(payload.get("zotero_collection"))
    registry = resolve_registry_file(registry_file)
    with _RegistryLock(registry):
        raw = _read_registry_bytes(registry)
        items = _parse_registry(raw)
        if _revision_of(raw) != revision:
            _error("project registry revision is stale; reload")
        target = None
        for entry in items:
            if entry["id"] == pid:
                target = entry
                break
        if target is None:
            _error("project id is unknown")
        assert target is not None
        if logseq_path and any(e["id"] != pid and e["logseq_path"] == logseq_path
                               for e in items):
            _error("another project already uses this note")
        target.update({"name": name, "logseq_path": logseq_path,
                       "local_folder": local_folder, "github_url": github_url,
                       "zotero_collection": zotero_collection})
        items.sort(key=lambda e: (e["name"].casefold(), e["id"]))
        encoded = _serialize_registry(items)
        _atomic_write_registry(registry, encoded)
        raw = encoded
        saved = dict(target)
    response = _response(registry, raw, items)
    response["project"] = _public_record(saved)
    return response


def remove_project(payload: dict, registry_file=None) -> dict:
    if not isinstance(payload, dict):
        _error("remove payload must be an object")
    _check_unknown(payload, _ALLOWED_REMOVE_KEYS, "remove payload")
    pid = _validate_id(payload.get("id"))
    revision = _validate_revision(payload.get("revision"))
    registry = resolve_registry_file(registry_file)
    with _RegistryLock(registry):
        raw = _read_registry_bytes(registry)
        items = _parse_registry(raw)
        if _revision_of(raw) != revision:
            _error("project registry revision is stale; reload")
        remaining = [e for e in items if e["id"] != pid]
        if len(remaining) == len(items):
            _error("project id is unknown")
        encoded = _serialize_registry(remaining)
        _atomic_write_registry(registry, encoded)
        raw = encoded
        items = remaining
    return _response(registry, raw, items)


def lookup_local_folder(logseq_path: str, registry_file=None) -> str:
    """Return the registered ``local_folder`` for *logseq_path*, or ``""``.

    Missing registry files behave like an empty registry (``""``). Corrupt
    registries raise :class:`RegistryError` so callers fail closed instead
    of silently using a stale ``file::`` folder.
    """
    if not isinstance(logseq_path, str) or not logseq_path:
        return ""
    registry = resolve_registry_file(registry_file)
    raw = _read_registry_bytes(registry)
    if raw is None:
        return ""
    items = _parse_registry(raw)
    normalized = _validate_logseq_path(logseq_path)
    for entry in items:
        if entry["logseq_path"] == normalized:
            return entry.get("local_folder", "")
    return ""


# ---------------------------------------------------------------------------
# import-logseq (graph-backed, idempotent, never rewrites the graph)
# ---------------------------------------------------------------------------

def _leading_properties(content: str) -> dict[str, list[str]]:
    """Parse the leading ``key:: value`` block (column zero only).

    Mirrors the ``project_files`` preamble rule: leading blank lines are
    skipped; scanning stops at the first line of any other content, so
    body blocks, nested properties, headings, lists, fences and drawers
    never count.
    """
    content = content.lstrip("\ufeff")
    props: dict[str, list[str]] = {}
    for line in re.split(r"\r\n|\r|\n", content):
        if line.strip() == "":
            continue
        if not line or line[0] in (" ", "\t", "-", "*", "#", ">", "|", ":",
                                   "`", "~", "{", "}"):
            break
        match = _PROPERTY_RE.match(line)
        if match is None:
            break
        props.setdefault(match.group("key").casefold(), []).append(
            match.group("value").strip())
    return props


def _is_project_value(value: str) -> bool:
    text = _strip_quotes(value.strip())
    if text.startswith("[[") and text.endswith("]]"):
        text = _strip_quotes(text[2:-2].strip())
    return text.casefold() == "project"


def _normalize_import_folder(raw_value: str) -> tuple[str, str | None]:
    """Return ``(folder, warning_or_None)`` for one raw ``file::`` value."""
    from urllib.parse import unquote
    value = _unwrap_target(raw_value)
    if not value or "\x00" in value:
        return "", "project file property is empty; folder skipped"
    lowered = value.casefold()
    if lowered.startswith("file://"):
        rest = value[len("file://"):]
        if rest.casefold().startswith("localhost"):
            rest = rest[len("localhost"):]
        if rest == "":
            return "", "project file property is empty; folder skipped"
        if rest.startswith("/"):
            value = _strip_quotes(unquote(rest).strip())
        elif rest.startswith("~"):
            return "", (
                f"ambiguous folder target skipped: {raw_value.strip()[:120]}")
        elif "/" in rest:
            # The graph spells absolute targets without the leading slash
            # (``file://home/franzs/...`` means ``/home/franzs/...``). A
            # ``file://<host>/...`` spelling with a dotted hostname is not
            # local and is skipped instead of repaired.
            head = rest.partition("/")[0]
            if head not in ("", ".", "..") and "." not in head:
                value = _strip_quotes(unquote("/" + rest).strip())
            else:
                return "", (
                    f"non-local folder target skipped: {raw_value.strip()[:120]}")
        else:
            return "", (
                f"non-local folder target skipped: {raw_value.strip()[:120]}")
        if not value or "\x00" in value:
            return "", "project file property is empty; folder skipped"
    # At this point value is a plain path target.
    if len(value) > PATH_LIMIT or "\x00" in value or "\\" in value:
        return "", (f"unsafe folder target skipped: {raw_value.strip()[:120]}")
    if value.startswith("~"):
        # Only "~" or "~/" spellings map to a home path; "~name/..." is
        # ambiguous (user vs. relative) and must not be invented.
        if value == "~" or value.startswith("~/"):
            try:
                return _normalize_local_folder(value), None
            except RegistryError:
                return "", (
                    f"unsafe folder target skipped: {raw_value.strip()[:120]}")
        return "", (
            f"ambiguous folder target skipped: {raw_value.strip()[:120]}")
    if value.startswith("/"):
        try:
            return _normalize_local_folder(value), None
        except RegistryError:
            return "", (
                f"unsafe folder target skipped: {raw_value.strip()[:120]}")
    # Bare "home/franzs/..." (missing leading slash after file://) is the
    # known graph spelling: interpret as "/home/franzs/...".
    if "/" in value and not value.startswith(("/", "~")):
        # Only repair the file://-without-slash shape; plain relative
        # file:: targets stay skipped.
        if raw_value.strip().casefold().startswith("file://"):
            try:
                return _normalize_local_folder("/" + value), None
            except RegistryError:
                return "", (
                    f"unsafe folder target skipped: {raw_value.strip()[:120]}")
    return "", (f"relative folder target skipped: {raw_value.strip()[:120]}")


def _extract_import_candidates(graph) -> tuple[list[dict], list[str]]:
    """Scan the graph for project notes; return (candidates, warnings)."""
    import project_planner
    from logseq_common import graph_path as _graph_path
    graph = _graph_path(graph)
    listing = project_planner.list_projects(graph)
    candidates: list[dict] = []
    warnings: list[str] = []
    for entry in listing.get("projects", []):
        rel = entry.get("path", "")
        try:
            current = project_planner.read_page(graph, rel)
        except Exception as exc:
            warnings.append(f"unreadable page skipped: {rel} ({exc})")
            continue
        props = _leading_properties(current.get("content", ""))
        type_hits = [v for v in props.get("type", []) if _is_project_value(v)]
        status_hits = [v for v in props.get("status", []) if _is_project_value(v)]
        if not type_hits and not status_hits:
            continue
        name = current.get("page", "")
        try:
            name = _validate_name(name)
        except RegistryError as exc:
            warnings.append(f"page skipped ({rel}): {exc}")
            continue
        try:
            logseq_path = _validate_logseq_path(rel)
        except RegistryError as exc:
            warnings.append(f"page skipped ({rel}): {exc}")
            continue
        # file:: via the existing preamble parser (same boundary rules).
        try:
            import project_files
            file_values = project_files.extract_file_values(
                current.get("content", ""))
        except Exception:
            file_values = []
        folder = ""
        if len(file_values) > 1:
            warnings.append(
                f"duplicate file:: skipped for {logseq_path}; folder left empty")
        elif len(file_values) == 1:
            folder, warning = _normalize_import_folder(file_values[0])
            if warning:
                warnings.append(f"{logseq_path}: {warning}")
        github_url = ""
        for key in ("github", "url"):
            for raw_value in props.get(key, []):
                candidate = _unwrap_target(raw_value)
                if not candidate:
                    continue
                try:
                    github_url = _validate_github_url(candidate)
                    break
                except RegistryError:
                    continue
            if github_url:
                break
        if not github_url:
            raw_seen = [v for k in ("github", "url") for v in props.get(k, [])]
            if any(v.strip() for v in raw_seen):
                warnings.append(
                    f"{logseq_path}: github/url target skipped (not a github.com repo URL)")
        candidates.append({"name": name, "logseq_path": logseq_path,
                           "local_folder": folder, "github_url": github_url})
    return candidates, warnings


def import_logseq(graph, registry_file=None) -> dict:
    """Import new project notes from the graph into the registry.

    Idempotent: notes already registered (by non-empty ``logseq_path``) are
    skipped; existing entries are never modified; the graph is never
    written.
    """
    from logseq_common import resolve_graph as _resolve_graph
    graph = _resolve_graph(graph)
    candidates, warnings = _extract_import_candidates(graph)
    registry = resolve_registry_file(registry_file)
    with _RegistryLock(registry):
        raw = _read_registry_bytes(registry)
        items = _parse_registry(raw)
        known = {e["logseq_path"] for e in items if e["logseq_path"]}
        seen_in_batch: set[str] = set()
        added = 0
        for candidate in candidates:
            note = candidate["logseq_path"]
            if not note or note in known or note in seen_in_batch:
                continue
            seen_in_batch.add(note)
            new_id = str(uuid.uuid4())
            while any(e["id"] == new_id for e in items):
                new_id = str(uuid.uuid4())
            items.append({"id": new_id, **candidate})
            added += 1
        items.sort(key=lambda e: (e["name"].casefold(), e["id"]))
        encoded = _serialize_registry(items)
        # Avoid rewriting identical bytes (keeps revision stable when idle).
        if raw is None or encoded != raw:
            _atomic_write_registry(registry, encoded)
            raw = encoded
    response = _response(registry, raw, items)
    response["warnings"] = warnings
    response["imported"] = added
    return response


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read_input():
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    try:
        data = stream.read(INPUT_LIMIT + 1)
    except OSError as exc:
        raise RegistryError("cannot read JSON input") from exc
    if isinstance(data, str):
        data = data.encode("utf-8")
    if len(data) > INPUT_LIMIT:
        _error("JSON input is too large")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryError(f"invalid JSON input: {exc}") from exc
    if not isinstance(value, dict):
        _error("JSON input must be an object")
    return value


# ---------------------------------------------------------------------------
# CLI (shared plumbing lives in qscli.py; argv stays byte-identical)
# ---------------------------------------------------------------------------

def _parse_args(argv) -> argparse.Namespace:
    parser = qscli.SafeParser(description=__doc__)
    qscli.add_global_flags(parser, projects_file=True, graph=True)
    parser.add_argument("command", choices=("list", "create", "update",
                                            "remove", "import-logseq"))
    return parser.parse_args(argv if argv is not None else sys.argv[1:])


def _dispatch(args: argparse.Namespace) -> dict:
    if args.command == "list":
        return list_projects(args.projects_file)
    elif args.command == "create":
        return create_project(_read_input(), args.projects_file)
    elif args.command == "update":
        return update_project(_read_input(), args.projects_file)
    elif args.command == "remove":
        return remove_project(_read_input(), args.projects_file)
    else:
        return import_logseq(args.graph, args.projects_file)


# ValueError covers RegistryError (subclass) plus GraphError (subclass)
# from the graph helpers used by import-logseq; all user-facing failures
# stay one line, no traceback.
_BOUNDED_EXCEPTIONS = (RegistryError, OSError, TypeError, ValueError,
                       UnicodeError, RecursionError, OverflowError)


def main(argv=None) -> int:
    return qscli.run_main(_parse_args, _dispatch, "projects",
                          _BOUNDED_EXCEPTIONS, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
