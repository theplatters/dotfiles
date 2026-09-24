#!/usr/bin/env python3
"""Python-owned sidecar storage for work-memory loops (annotations.db).

Lives beside the collector database
(``$XDG_STATE_HOME/quickshell/desktop-activity/annotations.db``) but never
touches the collector schema. Owns schema creation and access; WAL mode
with a bounded busy timeout; short transactions only (never hold a
transaction across a network call).

Conventions match the other helpers: JSON stdout, single-line
``error: ...`` on stderr with exit 1, no tracebacks, no state content in
diagnostics, list-form argv with ``shell=False`` by callers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import time
import uuid
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import project_sessions as _ps
import qscli


class AnnotationsError(ValueError):
    pass


def _error(message: str):
    raise AnnotationsError(message)


DB_FILENAME = "annotations.db"
ACTIVITY_SUBDIR = ("quickshell", "desktop-activity")
ENV_DB = "QS_ANNOTATIONS_DB"
INPUT_LIMIT = 1024 * 1024
# Transport cap; single source of truth is qscli.INPUT_LIMIT (same value).
STATE_KEY_LIMIT = 256
STATE_VALUE_BYTES = 64 * 1024
RAW_JSON_BYTES = 64 * 1024
TEXT_FIELD_LIMIT = 512
DIGEST_FIELD_LIMIT = 256
BUSY_TIMEOUT_MS = 5000
I64_MAX = 9223372036854775807

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS captures (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL, session_file TEXT NOT NULL, message_ref TEXT NOT NULL,
  kind TEXT NOT NULL, text TEXT NOT NULL, text_hash TEXT NOT NULL,
  actionable REAL, durable REAL,
  status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','accepted','dismissed')),
  created_ms INTEGER NOT NULL, applied_ms INTEGER, applied_target TEXT,
  UNIQUE(session_file, message_ref, text_hash)
);
CREATE TABLE IF NOT EXISTS drafts (
  draft_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, session_id TEXT,
  summary_key TEXT, evidence_digest TEXT NOT NULL, json TEXT NOT NULL,
  created_ms INTEGER NOT NULL, polished INTEGER NOT NULL DEFAULT 0,
  saved_journal_ms INTEGER, saved_page_ms INTEGER
);
CREATE TABLE IF NOT EXISTS prepared (
  token TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
  preview_json TEXT NOT NULL, revision TEXT,
  expires_ms INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reviews (
  day TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('evening','morning')),
  json TEXT NOT NULL, generated_ms INTEGER NOT NULL, polished INTEGER NOT NULL DEFAULT 0,
  saved_ms INTEGER, PRIMARY KEY (day, kind)
);
CREATE TABLE IF NOT EXISTS state (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_meta (
  session_id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL DEFAULT '',
  revision TEXT NOT NULL,
  created_ms INTEGER NOT NULL,
  updated_ms INTEGER NOT NULL,
  thought_ref TEXT NOT NULL DEFAULT '',
  todo_refs TEXT NOT NULL DEFAULT '[]',
  refs TEXT NOT NULL DEFAULT '[]',
  attended INTEGER NOT NULL DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS content_index USING fts5(
  text, page UNINDEXED, line UNINDEXED, kind UNINDEXED,
  session_id UNINDEXED, project_id UNINDEXED,
  tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS session_links (
  session_id TEXT NOT NULL,
  kind TEXT NOT NULL
    CHECK (kind IN ('capture','draft','agent_session',
                    'continued_from')),
  target TEXT NOT NULL,
  created_ms INTEGER NOT NULL,
  PRIMARY KEY (session_id, kind, target)
);
CREATE INDEX IF NOT EXISTS idx_session_links_kind_target
  ON session_links(kind, target);
"""

EXPECTED_TABLES = {
    "captures",
    "drafts",
    "prepared",
    "reviews",
    "state",
    "session_meta",
    "session_links",
    "content_index",
}

#: Derived, rebuildable full-text index over thought and TODO block text
#: (Phase 2c, plan §5.5). Keyed by page/line/kind/session/project; never a
#: source of truth — dropping it costs only a rescan
#: (``scripts/content_index.py rebuild``). Declaration discipline mirrors
#: ``activity_fts`` (exact columns/UNINDEXED/tokenizer, validated by
#: :func:`check_content_index`).
CONTENT_INDEX_SQL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS content_index USING fts5("
    "text, page UNINDEXED, line UNINDEXED, kind UNINDEXED,"
    " session_id UNINDEXED, project_id UNINDEXED,"
    " tokenize='unicode61 remove_diacritics 2')"
)

#: FTS5 shadow tables owned by ``content_index`` (SQLite-created).
CONTENT_INDEX_SHADOW_TABLES = (
    "content_index_data",
    "content_index_idx",
    "content_index_content",
    "content_index_docsize",
    "content_index_config",
)

SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _contains_dotdot(raw: str) -> bool:
    return ".." in raw.replace("\\", "/").split("/")


def default_db_path() -> Path:
    configured = os.environ.get("XDG_STATE_HOME", "")
    if isinstance(configured, str) and configured.strip():
        raw_cfg = configured.strip()
        if "\x00" in raw_cfg or len(raw_cfg) > 4096:
            _error("database path is unsafe")
        if _contains_dotdot(raw_cfg):
            _error("database path is unsafe")
        try:
            expanded_cfg = os.path.expanduser(raw_cfg)
        except Exception:
            _error("database path is unsafe")
        if _contains_dotdot(expanded_cfg) or ".." in Path(expanded_cfg).parts:
            _error("database path is unsafe")
    return _ps.state_home() / ACTIVITY_SUBDIR[0] / ACTIVITY_SUBDIR[1] / DB_FILENAME


def resolve_db_path(explicit=None) -> Path:
    if explicit is not None and isinstance(explicit, (str, Path)) and str(explicit).strip():
        raw = str(explicit).strip()
    else:
        env = os.environ.get(ENV_DB, "")
        if isinstance(env, str) and env.strip():
            raw = env.strip()
        else:
            return default_db_path()
    if "\x00" in raw or len(raw) > 4096:
        _error("database path is unsafe")
    # Reject '..' before any normalization: _private_dir normalizes via
    # abspath, which would lexically drop a symlinked component
    # (e.g. /base/link/../a.db with link -> elsewhere) while SQLite opens
    # the un-normalized string. Check the raw string and the expanded
    # path components before abspath normalization.
    if _contains_dotdot(raw):
        _error("database path is unsafe")
    expanded = os.path.expanduser(raw)
    if _contains_dotdot(expanded) or ".." in Path(expanded).parts:
        _error("database path is unsafe")
    candidate = Path(expanded)
    if not candidate.is_absolute():
        try:
            candidate = Path(os.path.abspath(os.path.join(os.getcwd(), expanded)))
        except OSError:
            _error("database path is unsafe")
    return candidate


def _lstat(path: Path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _ensure_private_dir(path: Path) -> Path:
    try:
        return _ps._private_dir(path)
    except AttributeError:
        # Fallback if the private helper ever moves; should not happen.
        _error("database directory is unsafe")
    except Exception:
        _error("database directory is unsafe")
    raise AssertionError("unreachable")


def _check_owner(info: os.stat_result) -> None:
    """Reject files not owned by the current user (ownership, not just mode).

    ``project_sessions._private_dir`` only validates directories; file
    ownership/mode for the db and every SQLite sidecar (``-wal``/``-shm``/
    ``-journal``) is enforced here so a foreign-owned file can never be
    opened, even when its permission bits look private.
    """
    try:
        euid = os.geteuid()
    except AttributeError:
        return
    if info.st_uid != euid:
        _error("database file is unsafe")


def _reject_file_safety(path: Path, *, what: str = "database file is unsafe") -> None:
    """Reject symlinks, non-regular files, loose modes, and foreign owners.

    Directory safety comes from ``project_sessions._private_dir`` (dirs
    only); this guard owns the file-level contract for the db itself.
    """
    info = _lstat(path)
    if info is None:
        return
    if stat.S_ISLNK(info.st_mode):
        _error(what)
    if not stat.S_ISREG(info.st_mode):
        _error(what)
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077 != 0:
        _error(what)
    _check_owner(info)


def _check_sidecars(db_path: Path) -> None:
    """Reject unsafe SQLite sidecars (``-wal``/``-shm``/``-journal``).

    Missing sidecars are fine. Present ones must be regular, private
    (``0600``), owned files and never symlinks -- same contract as the db.
    """
    for suffix in SIDECAR_SUFFIXES:
        sidecar = Path(str(db_path) + suffix)
        try:
            info = sidecar.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            _error("database file is unsafe")
        if stat.S_ISLNK(info.st_mode):
            _error("database file is unsafe")
        if not stat.S_ISREG(info.st_mode):
            _error("database file is unsafe")
        if stat.S_IMODE(info.st_mode) & 0o077 != 0:
            _error("database file is unsafe")
        _check_owner(info)


def _init_schema(conn: sqlite3.Connection) -> None:
    try:
        conn.executescript(SCHEMA_SQL)
    except sqlite3.Error:
        _error("database is unavailable")
    _ensure_session_meta_columns(conn)


#: Phase-2c ``session_meta`` columns with their ADD COLUMN declarations.
#: Existing sidecars predate them; the writer migration below adds whatever
#: is missing so old databases keep working without a prune.
_SESSION_META_ADD_COLUMNS = (
    ("thought_ref", "TEXT NOT NULL DEFAULT ''"),
    ("todo_refs", "TEXT NOT NULL DEFAULT '[]'"),
    ("refs", "TEXT NOT NULL DEFAULT '[]'"),
    ("attended", "INTEGER NOT NULL DEFAULT 0"),
)


def _ensure_session_meta_columns(conn: sqlite3.Connection) -> None:
    try:
        info = conn.execute("PRAGMA table_info(session_meta)").fetchall()
    except sqlite3.Error:
        _error("database is unavailable")
    have: set[str] = set()
    for item in info:
        try:
            have.add(str(item["name"]))
        except (IndexError, KeyError, TypeError):
            continue
    for column, ddl in _SESSION_META_ADD_COLUMNS:
        if column in have:
            continue
        try:
            conn.execute(f"ALTER TABLE session_meta ADD COLUMN {column} {ddl}")
        except sqlite3.Error:
            _error("database is unavailable")
    try:
        conn.commit()
    except sqlite3.Error:
        _error("database is unavailable")


def connect(db=None) -> sqlite3.Connection:
    """Open (creating 0600) the sidecar and return a sqlite.Row connection."""
    path = resolve_db_path(db)
    if not path.name or path.name in (".", ".."):
        _error("database path is unsafe")
    if ".." in path.parts:
        _error("database path is unsafe")
    parent = path.parent
    if not str(parent) or str(parent) in ("", "."):
        _error("database path is unsafe")
    # Use the validated parent for the actual open so SQLite never opens
    # the pre-normalization string (symlink + '..' divergence).
    validated_parent = _ensure_private_dir(parent)
    path = validated_parent / path.name
    _reject_file_safety(path)
    _check_sidecars(path)
    existed = _lstat(path) is not None
    old_umask = None
    try:
        try:
            old_umask = os.umask(0o077)
        except OSError:
            _error("database is unavailable")
        try:
            conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000.0,
                                   isolation_level=None)
        except OSError:
            _error("database is unavailable")
        except sqlite3.Error:
            _error("database is unavailable")
    finally:
        if old_umask is not None:
            try:
                os.umask(old_umask)
            except OSError:
                pass
    try:
        # Re-check after open to close the replace-after-check window.
        _reject_file_safety(path)
        _check_sidecars(path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            _error("database is unavailable")
        # Short transactions only: autocommit mode (isolation_level=None)
        # with explicit commits per mutation below.
        _init_schema(conn)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        # A freshly created file must already be private; an existing one
        # was validated above. Re-validate after chmod.
        _reject_file_safety(path)
        _ = existed
        return conn
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_session_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("session id is invalid")
    text = value.strip()
    if len(text) != 32 or any(c not in "0123456789abcdefABCDEF" for c in text):
        _error("session id is invalid")
    return text.lower()


def _validate_project_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("project id is invalid")
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        _error("project id is invalid")


def _validate_state_key(value: object) -> str:
    if not isinstance(value, str) or not value:
        _error("state key is invalid")
    if "\x00" in value or len(value) > STATE_KEY_LIMIT:
        _error("state key is invalid")
    if not value.strip():
        _error("state key is invalid")
    return value


def _validate_state_value(value: object) -> str:
    if not isinstance(value, str):
        _error("state value is invalid")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        _error("state value is invalid")
    if "\x00" in value or size > STATE_VALUE_BYTES:
        _error("state value is invalid")
    return value


def _validate_ms(value: object, *, what: str = "timestamp is invalid") -> int:
    if isinstance(value, bool):
        _error(what)
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = int(value.strip(), 10)
        except ValueError:
            _error(what)
    else:
        _error(what)
    if parsed < 0 or parsed > I64_MAX:
        _error(what)
    return parsed


def _validate_digest(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("evidence digest is invalid")
    text = value.strip()
    if len(text) > DIGEST_FIELD_LIMIT or "\x00" in text:
        _error("evidence digest is invalid")
    return text


def _validate_raw_json(value: object, *, limit: int = RAW_JSON_BYTES, what: str = "raw payload is invalid") -> str:
    if not isinstance(value, str) or not value:
        _error(what)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        _error(what)
    if size > limit:
        _error(what)
    try:
        json.loads(value)
    except (json.JSONDecodeError, ValueError, RecursionError):
        _error(what)
    return value


def _validate_bounded_text(value: object, *, what: str = "text is invalid") -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        _error(what)
    text = value.strip()
    if len(text) > TEXT_FIELD_LIMIT or "\x00" in text:
        _error(what)
    return text


# ---------------------------------------------------------------------------
# Drafts / prepared exports (work-log sidecar; plan section 7B)
# ---------------------------------------------------------------------------

_DRAFT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_JSON_PAYLOAD_BYTES = 256 * 1024
_REVISION_FIELD_LIMIT = 128
_PREPARED_KINDS = ("journal", "page", "capture-page", "capture-today",
                   "review-journal", "organise")


def _validate_draft_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("draft id is invalid")
    text = value.strip()
    if len(text) > 128 or not _DRAFT_ID_RE.fullmatch(text):
        _error("draft id is invalid")
    return text


def _validate_token(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("prepared token is invalid")
    text = value.strip()
    if len(text) > 128 or not _TOKEN_RE.fullmatch(text):
        _error("prepared token is invalid")
    return text


def _validate_session_opt(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return _validate_session_id(value)


def _validate_summary_key(value: object) -> str:
    if not isinstance(value, str):
        _error("summary key is invalid")
    if "\x00" in value or len(value) > TEXT_FIELD_LIMIT:
        _error("summary key is invalid")
    return value


def _validate_json_payload(value: object) -> str:
    if not isinstance(value, dict):
        _error("draft payload is invalid")
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))
    except (TypeError, ValueError, RecursionError, OverflowError):
        _error("draft payload is invalid")
    try:
        size = len(encoded.encode("utf-8"))
    except UnicodeEncodeError:
        _error("draft payload is invalid")
    if size > _JSON_PAYLOAD_BYTES:
        _error("draft payload is too large")
    return encoded


def _validate_prepared_json(value: object, *, what: str) -> str:
    if not isinstance(value, dict):
        _error(what)
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))
    except (TypeError, ValueError, RecursionError, OverflowError):
        _error(what)
    try:
        size = len(encoded.encode("utf-8"))
    except UnicodeEncodeError:
        _error(what)
    if size > _JSON_PAYLOAD_BYTES:
        _error(what)
    return encoded


def _validate_kind(value: object) -> str:
    if not isinstance(value, str) or value.strip() not in _PREPARED_KINDS:
        _error("prepared kind is invalid")
    return value.strip()


def _validate_revision_opt(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        _error("revision is invalid")
    text = value.strip()
    if len(text) > _REVISION_FIELD_LIMIT or "\x00" in text:
        _error("revision is invalid")
    return text


def _validate_polished_flag(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if value in (0, 1):
        return int(value)
    _error("polished flag is invalid")
    raise AssertionError("unreachable")


def _validate_markdown(value: object) -> str:
    if not isinstance(value, str):
        _error("markdown is invalid")
    if "\x00" in value:
        _error("markdown is invalid")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        _error("markdown is invalid")
    if size > _JSON_PAYLOAD_BYTES:
        _error("markdown is too large")
    return value


def _draft_row_to_dict(row: sqlite3.Row) -> dict | None:
    try:
        payload = json.loads(row["json"])
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    return {
        "draft_id": row["draft_id"],
        "project_id": row["project_id"],
        "session_id": row["session_id"],
        "summary_key": row["summary_key"],
        "evidence_digest": row["evidence_digest"],
        "draft": payload,
        "created_ms": row["created_ms"],
        "polished": bool(row["polished"]),
        "saved_journal_ms": row["saved_journal_ms"],
        "saved_page_ms": row["saved_page_ms"],
    }


def get_draft(conn: sqlite3.Connection, draft_id: str) -> dict | None:
    """Return one stored draft row or None (missing / unparseable JSON)."""
    canonical = _validate_draft_id(draft_id)
    try:
        row = conn.execute(
            "SELECT draft_id, project_id, session_id, summary_key,"
            " evidence_digest, json, created_ms, polished,"
            " saved_journal_ms, saved_page_ms"
            " FROM drafts WHERE draft_id = ?",
            (canonical,)).fetchone()
    except sqlite3.Error:
        _error("database is unavailable")
    if row is None:
        return None
    return _draft_row_to_dict(row)


def find_draft(conn: sqlite3.Connection, project_id: str, session_id,
               evidence_digest: str) -> dict | None:
    """Return the newest draft for project+session+digest or None.

    The session comparison is NULL aware (``IS`` matches NULL rows).
    """
    canonical_project = _validate_project_id(project_id)
    canonical_session = _validate_session_opt(session_id)
    digest = _validate_digest(evidence_digest)
    try:
        row = conn.execute(
            "SELECT draft_id, project_id, session_id, summary_key,"
            " evidence_digest, json, created_ms, polished,"
            " saved_journal_ms, saved_page_ms"
            " FROM drafts WHERE project_id = ? AND session_id IS ?"
            " AND evidence_digest = ?"
            " ORDER BY created_ms DESC LIMIT 1",
            (canonical_project, canonical_session, digest)).fetchone()
    except sqlite3.Error:
        _error("database is unavailable")
    if row is None:
        return None
    return _draft_row_to_dict(row)


def save_draft(conn: sqlite3.Connection, *, draft_id: str, project_id: str,
               session_id, summary_key: str, evidence_digest: str,
               payload: dict, created_ms: int, polished=None) -> None:
    """Upsert a draft payload keyed by draft_id.

    Never clears ``saved_journal_ms`` / ``saved_page_ms``; the ``polished``
    column is updated only when ``polished`` is not None. When ``polished``
    is None and the new payload carries no polished text, the stored
    ``polished_markdown``/``polished`` payload fields are carried over so
    a same-digest refresh cannot drop polished text while the flag
    survives.
    """
    canonical = _validate_draft_id(draft_id)
    canonical_project = _validate_project_id(project_id)
    canonical_session = _validate_session_opt(session_id)
    key = _validate_summary_key(summary_key)
    digest = _validate_digest(evidence_digest)
    if not isinstance(payload, dict):
        _error("draft payload is invalid")
    merged = dict(payload)
    stamp = _validate_ms(created_ms)
    if polished is None:
        try:
            existing = conn.execute(
                "SELECT json FROM drafts WHERE draft_id = ?",
                (canonical,)).fetchone()
        except sqlite3.Error:
            _error("database is unavailable")
        if existing is not None:
            try:
                old = json.loads(existing["json"])
            except (ValueError, TypeError, RecursionError):
                old = None
            if isinstance(old, dict):
                old_markdown = old.get("polished_markdown")
                if not merged.get("polished_markdown") \
                        and isinstance(old_markdown, str) and old_markdown:
                    merged["polished_markdown"] = old_markdown
                    merged["polished"] = True
    encoded = _validate_json_payload(merged)
    try:
        if polished is None:
            conn.execute(
                "INSERT INTO drafts(draft_id, project_id, session_id,"
                " summary_key, evidence_digest, json, created_ms)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(draft_id) DO UPDATE SET"
                " project_id=excluded.project_id,"
                " session_id=excluded.session_id,"
                " summary_key=excluded.summary_key,"
                " evidence_digest=excluded.evidence_digest,"
                " json=excluded.json,"
                " created_ms=excluded.created_ms",
                (canonical, canonical_project, canonical_session, key,
                 digest, encoded, stamp))
        else:
            flag = _validate_polished_flag(polished)
            conn.execute(
                "INSERT INTO drafts(draft_id, project_id, session_id,"
                " summary_key, evidence_digest, json, created_ms, polished)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(draft_id) DO UPDATE SET"
                " project_id=excluded.project_id,"
                " session_id=excluded.session_id,"
                " summary_key=excluded.summary_key,"
                " evidence_digest=excluded.evidence_digest,"
                " json=excluded.json,"
                " created_ms=excluded.created_ms,"
                " polished=excluded.polished",
                (canonical, canonical_project, canonical_session, key,
                 digest, encoded, stamp, flag))
        conn.commit()
    except sqlite3.Error:
        _error("database is unavailable")


def mark_draft_polished(conn: sqlite3.Connection, draft_id: str,
                        markdown: str, *, now_ms: int) -> bool:
    """Store polished markdown on a draft; False when the draft is missing."""
    canonical = _validate_draft_id(draft_id)
    text = _validate_markdown(markdown)
    _validate_ms(now_ms)
    try:
        row = conn.execute(
            "SELECT json FROM drafts WHERE draft_id = ?",
            (canonical,)).fetchone()
    except sqlite3.Error:
        _error("database is unavailable")
    if row is None:
        return False
    try:
        payload = json.loads(row["json"])
    except (ValueError, TypeError, RecursionError):
        return False
    if not isinstance(payload, dict):
        return False
    payload["polished_markdown"] = text
    payload["polished"] = True
    encoded = _validate_json_payload(payload)
    try:
        conn.execute(
            "UPDATE drafts SET json = ?, polished = 1 WHERE draft_id = ?",
            (encoded, canonical))
        conn.commit()
    except sqlite3.Error:
        _error("database is unavailable")
    return True


def mark_draft_saved(conn: sqlite3.Connection, draft_id: str, target: str,
                     *, now_ms: int) -> bool:
    """Stamp a draft's journal/page save marker; False when missing."""
    canonical = _validate_draft_id(draft_id)
    stamp = _validate_ms(now_ms)
    if target == "journal":
        column = "saved_journal_ms"
    elif target == "page":
        column = "saved_page_ms"
    else:
        _error("save target is invalid")
        raise AssertionError("unreachable")
    try:
        row = conn.execute(
            "SELECT draft_id FROM drafts WHERE draft_id = ?",
            (canonical,)).fetchone()
    except sqlite3.Error:
        _error("database is unavailable")
    if row is None:
        return False
    try:
        conn.execute(
            f"UPDATE drafts SET {column} = ? WHERE draft_id = ?",
            (stamp, canonical))
        conn.commit()
    except sqlite3.Error:
        _error("database is unavailable")
    return True


def create_prepared(conn: sqlite3.Connection, *, token: str, kind: str,
                    payload: dict, preview: dict, revision,
                    expires_ms: int) -> None:
    """Store a single-use prepared export; duplicate tokens are rejected."""
    canonical = _validate_token(token)
    bounded_kind = _validate_kind(kind)
    encoded_payload = _validate_prepared_json(
        payload, what="prepared payload is invalid")
    encoded_preview = _validate_prepared_json(
        preview, what="prepared preview is invalid")
    bounded_revision = _validate_revision_opt(revision)
    expiry = _validate_ms(expires_ms)
    try:
        conn.execute(
            "INSERT INTO prepared(token, kind, payload_json, preview_json,"
            " revision, expires_ms, used)"
            " VALUES(?, ?, ?, ?, ?, ?, 0)",
            (canonical, bounded_kind, encoded_payload, encoded_preview,
             bounded_revision, expiry))
        conn.commit()
    except sqlite3.IntegrityError:
        _error("prepared token already exists")
    except sqlite3.Error:
        _error("database is unavailable")


def get_prepared(conn: sqlite3.Connection, token: str) -> dict | None:
    """Return one prepared row (parsed) or None."""
    canonical = _validate_token(token)
    try:
        row = conn.execute(
            "SELECT token, kind, payload_json, preview_json, revision,"
            " expires_ms, used FROM prepared WHERE token = ?",
            (canonical,)).fetchone()
    except sqlite3.Error:
        _error("database is unavailable")
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"])
        preview = json.loads(row["preview_json"])
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(payload, dict) or not isinstance(preview, dict):
        return None
    return {
        "token": row["token"],
        "kind": row["kind"],
        "payload": payload,
        "preview": preview,
        "revision": row["revision"],
        "expires_ms": row["expires_ms"],
        "used": int(row["used"]),
    }


def consume_prepared(conn: sqlite3.Connection, token: str,
                     *, now_ms: int) -> dict | None:
    """Atomically burn one prepared token (single-use + expiry).

    ``BEGIN IMMEDIATE`` serializes racers; the write lock is never held
    across any subprocess/network call (this function performs none).
    Returns the parsed row or None (missing, used, or expired).
    """
    canonical = _validate_token(token)
    stamp = _validate_ms(now_ms)
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error:
        _error("database is unavailable")
    try:
        row = conn.execute(
            "SELECT token, kind, payload_json, preview_json, revision,"
            " expires_ms, used FROM prepared WHERE token = ?",
            (canonical,)).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        try:
            used = int(row["used"])
            expiry = int(row["expires_ms"])
        except (TypeError, ValueError):
            conn.execute("ROLLBACK")
            return None
        if used != 0 or expiry < stamp:
            conn.execute("ROLLBACK")
            return None
        conn.execute("UPDATE prepared SET used = 1 WHERE token = ?",
                     (canonical,))
        conn.execute("COMMIT")
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        _error("database is unavailable")
    try:
        payload = json.loads(row["payload_json"])
        preview = json.loads(row["preview_json"])
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(payload, dict) or not isinstance(preview, dict):
        return None
    return {
        "token": row["token"],
        "kind": row["kind"],
        "payload": payload,
        "preview": preview,
        "revision": row["revision"],
        "expires_ms": row["expires_ms"],
        "used": 1,
    }


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def get_state(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    _validate_state_key(key)
    try:
        row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        _error("database is unavailable")
    if row is None:
        return default
    return row["value"]


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    _validate_state_key(key)
    _validate_state_value(value)
    try:
        conn.execute("INSERT INTO state(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, value))
        conn.commit()
    except sqlite3.Error:
        _error("database is unavailable")


# ---------------------------------------------------------------------------
# Captures (work-memory loop 1.2; plan section 7D)
# ---------------------------------------------------------------------------

_CAPTURE_KINDS = ("todo", "decision", "next", "fixme", "checkbox")
_CAPTURE_STATUSES = ("new", "accepted", "dismissed")
_CAPTURE_TEXT_LIMIT = 400
_CAPTURE_SESSION_FILE_LIMIT = 1024
_CAPTURE_MESSAGE_REF_LIMIT = 128


def _validate_capture_kind(value: object) -> str:
    if not isinstance(value, str) or value.strip() not in _CAPTURE_KINDS:
        _error("capture kind is invalid")
    return value.strip()


def _validate_capture_status(value: object) -> str:
    if not isinstance(value, str) or value.strip() not in _CAPTURE_STATUSES:
        _error("capture status is invalid")
    return value.strip()


def _validate_session_file(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("session file is invalid")
    text = value.strip()
    if len(text) > _CAPTURE_SESSION_FILE_LIMIT or "\x00" in text:
        _error("session file is invalid")
    return text


def _validate_message_ref(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("message reference is invalid")
    text = value.strip()
    if len(text) > _CAPTURE_MESSAGE_REF_LIMIT or "\x00" in text:
        _error("message reference is invalid")
    return text


def _validate_capture_id(value: object) -> int:
    if isinstance(value, bool):
        _error("capture id is invalid")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = int(value.strip(), 10)
        except ValueError:
            _error("capture id is invalid")
    else:
        _error("capture id is invalid")
    if parsed < 1:
        _error("capture id is invalid")
    return parsed


def _validate_capture_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("capture text is invalid")
    text = value.strip()
    if len(text) > _CAPTURE_TEXT_LIMIT or "\x00" in text:
        _error("capture text is invalid")
    if any(ord(c) < 32 for c in text):
        _error("capture text is invalid")
    return text


def _validate_probability(value: object, *, what: str = "probability is invalid") -> float | None:
    # Restored: still required by insert_capture (captures survive the
    # slim-down; only the Jev callers are gone).
    if value is None:
        return None
    if isinstance(value, bool):
        _error(what)
    if not isinstance(value, (int, float)):
        _error(what)
    try:
        prob = float(value)
    except (OverflowError, ValueError, TypeError):
        _error(what)
    if prob != prob or prob in (float("inf"), float("-inf")):
        _error(what)
    if prob < 0 or prob > 1:
        _error(what)
    return prob


def _normalize_capture_text(text: str) -> str:
    return " ".join(text.split()).casefold()


def capture_text_hash(session_file, text) -> str | None:
    """Normalized dedupe hash for a capture candidate, or None.

    Hash is sha256 over whitespace-collapsed casefolded text (the
    ``text_hash`` column). ``session_file`` is validated for shape so
    callers cannot dedupe across unsafe identities; the hash itself
    covers only the text so the same line in different files stays
    distinct via the ``UNIQUE(session_file, message_ref, text_hash)``
    key plus the per-file existence check.
    """
    try:
        _validate_session_file(session_file)
    except AnnotationsError:
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    normalized = _normalize_capture_text(text)
    if not normalized:
        return None
    import hashlib as _hashlib
    return _hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def find_capture(conn: sqlite3.Connection, session_file: str,
                 text_hash: str) -> dict | None:
    """Return one capture row for session_file + text_hash, any status."""
    bounded_file = _validate_session_file(session_file)
    if not isinstance(text_hash, str) or not text_hash.strip():
        _error("capture hash is invalid")
    digest = text_hash.strip()
    if len(digest) > 256 or "\x00" in digest:
        _error("capture hash is invalid")
    try:
        row = conn.execute(
            "SELECT id, project_id, session_file, message_ref, kind, text,"
            " text_hash, actionable, durable, status, created_ms,"
            " applied_ms, applied_target"
            " FROM captures WHERE session_file = ? AND text_hash = ?"
            " ORDER BY id ASC LIMIT 1",
            (bounded_file, digest)).fetchone()
    except sqlite3.Error:
        _error("database is unavailable")
    if row is None:
        return None
    return dict(row)


def insert_capture(conn: sqlite3.Connection, *, project_id: str,
                   session_file: str, message_ref: str, kind: str,
                   text: str, actionable, durable,
                   created_ms: int) -> int:
    """Insert one capture candidate (status 'new'); idempotent on dedupe key."""
    canonical_project = _validate_project_id(project_id)
    bounded_file = _validate_session_file(session_file)
    bounded_ref = _validate_message_ref(message_ref)
    bounded_kind = _validate_capture_kind(kind)
    bounded_text = _validate_capture_text(text)
    bounded_actionable = _validate_probability(actionable)
    bounded_durable = _validate_probability(durable)
    stamp = _validate_ms(created_ms)
    digest = capture_text_hash(bounded_file, bounded_text)
    if digest is None:
        _error("capture text is invalid")
    assert digest is not None
    try:
        conn.execute(
            "INSERT OR IGNORE INTO captures(project_id, session_file,"
            " message_ref, kind, text, text_hash, actionable, durable,"
            " status, created_ms)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)",
            (canonical_project, bounded_file, bounded_ref, bounded_kind,
             bounded_text, digest, bounded_actionable, bounded_durable,
             stamp))
        conn.commit()
    except sqlite3.Error:
        _error("database is unavailable")
    try:
        row = conn.execute(
            "SELECT id FROM captures WHERE session_file = ?"
            " AND message_ref = ? AND text_hash = ?",
            (bounded_file, bounded_ref, digest)).fetchone()
    except sqlite3.Error:
        _error("database is unavailable")
    if row is None:
        _error("database is unavailable")
    return int(row["id"])


def list_captures(conn: sqlite3.Connection, *, day_start_ms: int,
                  day_end_ms: int, status: str = "new",
                  limit: int = 20) -> list[dict]:
    """List captures created in [day_start_ms, day_end_ms) by status."""
    start = _validate_ms(day_start_ms)
    end = _validate_ms(day_end_ms)
    if end < start:
        _error("day window is invalid")
    if not isinstance(status, str) or not status.strip():
        _error("capture status is invalid")
    bounded_status = status.strip()
    if bounded_status not in (*_CAPTURE_STATUSES, "all"):
        _error("capture status is invalid")
    if isinstance(limit, bool):
        _error("limit is invalid")
    if isinstance(limit, str) and limit.strip():
        try:
            limit = int(limit.strip(), 10)
        except ValueError:
            _error("limit is invalid")
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        _error("limit is invalid")
    try:
        if bounded_status == "all":
            rows = conn.execute(
                "SELECT id, project_id, session_file, message_ref, kind,"
                " text, actionable, durable, status, created_ms,"
                " applied_ms, applied_target"
                " FROM captures WHERE created_ms >= ? AND created_ms < ?"
                " ORDER BY created_ms ASC, id ASC LIMIT ?",
                (start, end, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, project_id, session_file, message_ref, kind,"
                " text, actionable, durable, status, created_ms,"
                " applied_ms, applied_target"
                " FROM captures WHERE created_ms >= ? AND created_ms < ?"
                " AND status = ? ORDER BY created_ms ASC, id ASC LIMIT ?",
                (start, end, bounded_status, limit)).fetchall()
    except sqlite3.Error:
        _error("database is unavailable")
    return [dict(r) for r in rows]


def set_capture_status(conn: sqlite3.Connection, capture_id,
                       status: str) -> bool:
    """Set a capture's status; False when the id is unknown."""
    canonical = _validate_capture_id(capture_id)
    bounded = _validate_capture_status(status)
    try:
        cursor = conn.execute(
            "UPDATE captures SET status = ? WHERE id = ?",
            (bounded, canonical))
        conn.commit()
    except sqlite3.Error:
        _error("database is unavailable")
    return cursor.rowcount > 0


def mark_capture_applied(conn: sqlite3.Connection, capture_id, target: str,
                         *, now_ms: int) -> bool:
    """Mark a 'new' capture accepted+applied; False when not applicable."""
    canonical = _validate_capture_id(capture_id)
    if not isinstance(target, str) or target.strip() not in ("page", "today"):
        _error("capture target is invalid")
    bounded_target = target.strip()
    stamp = _validate_ms(now_ms)
    try:
        cursor = conn.execute(
            "UPDATE captures SET status = 'accepted', applied_ms = ?,"
            " applied_target = ? WHERE id = ? AND status = 'new'",
            (stamp, bounded_target, canonical))
        conn.commit()
    except sqlite3.Error:
        _error("database is unavailable")
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Reviews (work-memory loop 1.3; plan section 7E)
# ---------------------------------------------------------------------------

_REVIEW_KINDS = ("evening", "morning")
_REVIEW_JSON_BYTES = 64 * 1024
_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def _validate_review_day(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("review day is invalid")
    text = value.strip()
    if not _DAY_RE.fullmatch(text):
        _error("review day is invalid")
    import datetime as _datetime
    try:
        _datetime.date.fromisoformat(text)
    except ValueError:
        _error("review day is invalid")
    return text


def _validate_review_kind(value: object) -> str:
    if not isinstance(value, str) or value.strip() not in _REVIEW_KINDS:
        _error("review kind is invalid")
    return value.strip()


def _validate_review_payload(value: object) -> str:
    if not isinstance(value, dict):
        _error("review payload is invalid")
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))
    except (TypeError, ValueError, RecursionError, OverflowError):
        _error("review payload is invalid")
    try:
        size = len(encoded.encode("utf-8"))
    except UnicodeEncodeError:
        _error("review payload is invalid")
    if size > _REVIEW_JSON_BYTES:
        _error("review payload is too large")
    try:
        parsed = json.loads(encoded)
    except (ValueError, RecursionError):
        _error("review payload is invalid")
    if not isinstance(parsed, dict):
        _error("review payload is invalid")
    return encoded


def get_review(conn: sqlite3.Connection, day: str,
               kind: str) -> dict | None:
    """Return one stored review row or None (missing/unparseable JSON)."""
    canonical_day = _validate_review_day(day)
    canonical_kind = _validate_review_kind(kind)
    try:
        row = conn.execute(
            "SELECT day, kind, json, generated_ms, polished, saved_ms"
            " FROM reviews WHERE day = ? AND kind = ?",
            (canonical_day, canonical_kind)).fetchone()
    except sqlite3.Error:
        _error("database is unavailable")
    if row is None:
        return None
    try:
        payload = json.loads(row["json"])
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    return {
        "day": row["day"],
        "kind": row["kind"],
        "json": payload,
        "generated_ms": row["generated_ms"],
        "polished": bool(row["polished"]),
        "saved_ms": row["saved_ms"],
    }


def save_review(conn: sqlite3.Connection, day: str, kind: str,
                payload: dict, generated_ms: int) -> None:
    """Upsert a review payload keyed by (day, kind).

    Never clears ``polished`` / ``saved_ms``; a same-day refresh keeps
    the polish flag and the saved marker while replacing the payload.
    """
    canonical_day = _validate_review_day(day)
    canonical_kind = _validate_review_kind(kind)
    encoded = _validate_review_payload(payload)
    stamp = _validate_ms(generated_ms)
    try:
        conn.execute(
            "INSERT INTO reviews(day, kind, json, generated_ms)"
            " VALUES(?, ?, ?, ?)"
            " ON CONFLICT(day, kind) DO UPDATE SET"
            " json=excluded.json,"
            " generated_ms=excluded.generated_ms",
            (canonical_day, canonical_kind, encoded, stamp))
        conn.commit()
    except sqlite3.Error:
        _error("database is unavailable")


def mark_review_polished(conn: sqlite3.Connection, day: str,
                         kind: str) -> bool:
    """Set a review's polished flag; False when the row is missing."""
    canonical_day = _validate_review_day(day)
    canonical_kind = _validate_review_kind(kind)
    try:
        cursor = conn.execute(
            "UPDATE reviews SET polished = 1 WHERE day = ? AND kind = ?",
            (canonical_day, canonical_kind))
        conn.commit()
    except sqlite3.Error:
        _error("database is unavailable")
    return cursor.rowcount > 0


def mark_review_saved(conn: sqlite3.Connection, day: str, kind: str,
                      now_ms: int) -> bool:
    """Stamp a review's journal-save marker; False when missing."""
    canonical_day = _validate_review_day(day)
    canonical_kind = _validate_review_kind(kind)
    stamp = _validate_ms(now_ms)
    try:
        cursor = conn.execute(
            "UPDATE reviews SET saved_ms = ? WHERE day = ? AND kind = ?",
            (stamp, canonical_day, canonical_kind))
        conn.commit()
    except sqlite3.Error:
        _error("database is unavailable")
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Session ledger: session_meta + session_links (reduced, no managed state)
# ---------------------------------------------------------------------------

SESSION_LINK_KINDS = ("capture", "draft", "agent_session",
                      "continued_from")
LINK_TARGET_LIMIT = 512
META_BULK_LIMIT = 1000

_SESSION_META_COLUMNS = (
    "session_id", "device_id", "revision", "created_ms", "updated_ms",
    "thought_ref", "todo_refs", "refs", "attended",
)


def _validate_link_kind(value: object) -> str:
    if not isinstance(value, str):
        _error("link kind is invalid")
    text = value.strip()
    if text not in SESSION_LINK_KINDS:
        _error("link kind is invalid")
    return text


def _validate_revision(value: object) -> str:
    if not isinstance(value, str):
        _error("revision is invalid")
    text = value.strip()
    if len(text) != 32 or any(c not in "0123456789abcdefABCDEF" for c in text):
        _error("revision is invalid")
    return text.lower()


def _validate_device_opt(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        _error("device id is invalid")
    if not value.strip():
        return ""
    text = value.strip()
    if len(text) != 32 or any(c not in "0123456789abcdefABCDEF" for c in text):
        _error("device id is invalid")
    return text.lower()


def _validate_link_target(value: object) -> str:
    if not isinstance(value, str):
        _error("link target is invalid")
    text = value.strip()
    if not text:
        _error("link target is invalid")
    if "\x00" in text:
        _error("link target is invalid")
    if len(text) > LINK_TARGET_LIMIT:
        _error("link target is invalid")
    return text


_THOUGHT_REF_RE = re.compile(r"\Ajournal:[A-Za-z0-9_.\-]+\.md:[1-9][0-9]*\Z")
_REF_ITEM_LIMIT = 512
_REF_LIST_LIMIT = 1000


def _validate_thought_ref(value: object) -> str:
    """Validate a saved-thought block ref (``journal:<file>.md:<line>``).

    Empty (never saved yet) is the default; anything else must match the
    ``journal:…md:<line>`` shape with no control characters. Thought
    *text* never lives here (L3) — only the markdown reference.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        _error("thought ref is invalid")
    text = value.strip()
    if text == "":
        return ""
    if len(text) > _REF_ITEM_LIMIT or "\x00" in text:
        _error("thought ref is invalid")
    if any(ord(c) < 32 for c in text):
        _error("thought ref is invalid")
    if not _THOUGHT_REF_RE.fullmatch(text):
        _error("thought ref is invalid")
    return text


def _validate_ref_list(value: object, *, what: str) -> str:
    """Validate a JSON array of block/ref identities; return canonical JSON.

    Accepts a list of strings or an already-encoded JSON array string.
    Items are stripped non-empty strings (≤ 512 chars, no control
    characters); at most ``_REF_LIST_LIMIT`` entries (bounded).
    """
    if isinstance(value, str):
        if not value.strip():
            _error(what)
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError, RecursionError):
            _error(what)
    elif isinstance(value, list):
        parsed = value
    else:
        _error(what)
    if not isinstance(parsed, list):
        _error(what)
    assert isinstance(parsed, list)
    if len(parsed) > _REF_LIST_LIMIT:
        _error(what)
    out: list[str] = []
    for item in parsed:
        if not isinstance(item, str) or not item.strip():
            _error(what)
        assert isinstance(item, str)
        text = item.strip()
        if len(text) > _REF_ITEM_LIMIT or "\x00" in text:
            _error(what)
        if any(ord(c) < 32 for c in text):
            _error(what)
        out.append(text)
    try:
        return json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError, OverflowError):
        _error(what)
    raise AssertionError("unreachable")


def _validate_attended(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if value in (0, 1):
        return int(value)
    _error("attended flag is invalid")
    raise AssertionError("unreachable")


def _validate_meta_limit(value: object, default: int = 5) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        _error("limit is invalid")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = int(value.strip(), 10)
        except ValueError:
            _error("limit is invalid")
    else:
        _error("limit is invalid")
    if parsed < 1 or parsed > 20:
        _error("limit is invalid")
    return parsed


def _row_get(row: sqlite3.Row, name: str, default):
    try:
        value = row[name]
    except (IndexError, KeyError, TypeError):
        return default
    return default if value is None else value


def _parse_ref_list(raw: object) -> list:
    if not isinstance(raw, str) or not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, str)]


def _session_meta_row_to_dict(row: sqlite3.Row) -> dict:
    thought = _row_get(row, "thought_ref", "")
    if not isinstance(thought, str):
        thought = ""
    try:
        attended_raw = _row_get(row, "attended", 0)
        attended = int(attended_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        attended = 0
    return {
        "session_id": row["session_id"],
        "device_id": row["device_id"],
        "revision": row["revision"],
        "created_ms": row["created_ms"],
        "updated_ms": row["updated_ms"],
        "thought_ref": thought,
        "todo_refs": _parse_ref_list(_row_get(row, "todo_refs", "[]")),
        "refs": _parse_ref_list(_row_get(row, "refs", "[]")),
        "attended": 1 if attended == 1 else 0,
    }


def _meta_select_columns(conn: sqlite3.Connection) -> str:
    """Column list for ``session_meta`` reads, tolerating old sidecars.

    Writer connections migrate the table (see ``_init_schema``), but
    read-only connections never run migrations — an old sidecar without
    the Phase-2c columns must still read (new fields default) instead of
    failing the SELECT.
    """
    try:
        info = conn.execute("PRAGMA table_info(session_meta)").fetchall()
    except sqlite3.Error:
        _error("database is unavailable")
    have: set[str] = set()
    for item in info:
        try:
            have.add(str(item["name"]))
        except (IndexError, KeyError, TypeError):
            continue
    ordered = [column for column in _SESSION_META_COLUMNS if column in have]
    if not ordered:
        # Missing table (or an empty pragma): keep the full list so the
        # SELECT surfaces the same unavailable-table error as before.
        return ", ".join(_SESSION_META_COLUMNS)
    return ", ".join(ordered)


def _session_link_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "session_id": row["session_id"],
        "kind": row["kind"],
        "target": row["target"],
        "created_ms": row["created_ms"],
    }


def get_session_meta(conn: sqlite3.Connection, session_id: str) -> dict | None:
    canonical = _validate_session_id(session_id)
    columns = _meta_select_columns(conn)
    try:
        row = conn.execute(
            f"SELECT {columns} FROM session_meta WHERE session_id = ?",
            (canonical,)).fetchone()
    except sqlite3.Error:
        _error("database is unavailable")
    if row is None:
        return None
    return _session_meta_row_to_dict(row)


def list_session_meta(conn: sqlite3.Connection,
                      session_ids: list) -> dict[str, dict]:
    if not isinstance(session_ids, list):
        _error("session list is invalid")
    if len(session_ids) > META_BULK_LIMIT:
        _error("session list is too large")
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in session_ids:
        canonical = _validate_session_id(raw)
        if canonical not in seen:
            seen.add(canonical)
            ordered.append(canonical)
    if not ordered:
        return {}
    out: dict[str, dict] = {}
    columns = _meta_select_columns(conn)
    for start in range(0, len(ordered), 500):
        chunk = ordered[start:start + 500]
        placeholders = ",".join("?" for _ in chunk)
        try:
            rows = conn.execute(
                f"SELECT {columns} FROM session_meta"
                f" WHERE session_id IN ({placeholders})",
                tuple(chunk)).fetchall()
        except sqlite3.Error:
            _error("database is unavailable")
        for row in rows:
            parsed = _session_meta_row_to_dict(row)
            out[parsed["session_id"]] = parsed
    return out


def upsert_session_meta(conn: sqlite3.Connection, *, session_id: str,
                        device_id=None,
                        revision=None, now_ms: int,
                        expect_revision=None,
                        thought_ref=None, todo_refs=None,
                        refs=None, attended=None) -> dict:
    """Create or CAS-update one ``session_meta`` row.

    The Phase-2c fields (``thought_ref``/``todo_refs``/``refs``/
    ``attended``) update only when their argument is not None; an update
    always rotates ``revision``. Thought *text* never lives here (L3) —
    only the journal block ref.
    """
    canonical = _validate_session_id(session_id)
    stamp = _validate_ms(now_ms)
    bounded_expect = None
    if expect_revision is not None:
        bounded_expect = _validate_revision(expect_revision)
    candidate_revision = None
    if revision is not None:
        candidate_revision = _validate_revision(revision)
    v_device = None if device_id is None else _validate_device_opt(device_id)
    v_thought = None if thought_ref is None \
        else _validate_thought_ref(thought_ref)
    v_todos = None if todo_refs is None else _validate_ref_list(
        todo_refs, what="todo refs are invalid")
    v_refs = None if refs is None else _validate_ref_list(
        refs, what="refs are invalid")
    v_attended = None if attended is None else _validate_attended(attended)
    try:
        row = conn.execute(
            f"SELECT {_meta_select_columns(conn)}"
            " FROM session_meta WHERE session_id = ?",
            (canonical,)).fetchone()
    except sqlite3.Error:
        _error("database is unavailable")
    if row is None:
        if bounded_expect is not None:
            _error("session meta changed")
        eff_device = v_device if v_device is not None else ""
        rev = candidate_revision if candidate_revision is not None \
            else secrets.token_hex(16)
        try:
            conn.execute(
                "INSERT INTO session_meta(session_id, device_id,"
                " revision, created_ms, updated_ms,"
                " thought_ref, todo_refs, refs, attended)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (canonical, eff_device, rev, stamp, stamp,
                 v_thought if v_thought is not None else "",
                 v_todos if v_todos is not None else "[]",
                 v_refs if v_refs is not None else "[]",
                 v_attended if v_attended is not None else 0))
            conn.commit()
        except sqlite3.IntegrityError:
            _error("session meta changed")
        except sqlite3.Error:
            _error("database is unavailable")
        result = get_session_meta(conn, canonical)
        assert result is not None
        return result
    current_revision = row["revision"]
    if bounded_expect is None or bounded_expect != str(current_revision).lower():
        _error("session meta changed")
    stored = _session_meta_row_to_dict(row)
    eff_device = v_device if v_device is not None else row["device_id"]
    eff_thought = v_thought if v_thought is not None \
        else stored["thought_ref"]
    eff_todos = v_todos if v_todos is not None else json.dumps(
        stored["todo_refs"], ensure_ascii=False, separators=(",", ":"))
    eff_refs = v_refs if v_refs is not None else json.dumps(
        stored["refs"], ensure_ascii=False, separators=(",", ":"))
    eff_attended = v_attended if v_attended is not None \
        else stored["attended"]
    fresh = secrets.token_hex(16)
    try:
        cursor = conn.execute(
            "UPDATE session_meta SET device_id = ?,"
            " revision = ?, updated_ms = ?,"
            " thought_ref = ?, todo_refs = ?, refs = ?, attended = ?"
            " WHERE session_id = ? AND revision = ?",
            (eff_device, fresh, stamp, eff_thought, eff_todos,
             eff_refs, eff_attended, canonical, current_revision))
        conn.commit()
    except sqlite3.Error:
        _error("database is unavailable")
    if cursor.rowcount == 0:
        _error("session meta changed")
    result = get_session_meta(conn, canonical)
    assert result is not None
    return result


def list_drafts_for_session(conn: sqlite3.Connection, session_id: str,
                            limit: int = 5) -> list[dict]:
    canonical = _validate_session_id(session_id)
    bounded = _validate_meta_limit(limit, default=5)
    try:
        rows = conn.execute(
            "SELECT draft_id, saved_journal_ms, saved_page_ms, polished,"
            " created_ms FROM drafts WHERE session_id = ?"
            " ORDER BY created_ms DESC, draft_id ASC LIMIT ?",
            (canonical, bounded)).fetchall()
    except sqlite3.Error:
        _error("database is unavailable")
    return [{
        "draft_id": r["draft_id"],
        "saved_journal_ms": r["saved_journal_ms"],
        "saved_page_ms": r["saved_page_ms"],
        "polished": bool(r["polished"]),
        "created_ms": r["created_ms"],
    } for r in rows]


def add_session_link(conn: sqlite3.Connection, *, session_id: str,
                     kind: str, target: str, now_ms: int) -> bool:
    canonical = _validate_session_id(session_id)
    bounded_kind = _validate_link_kind(kind)
    bounded_target = _validate_link_target(target)
    stamp = _validate_ms(now_ms)
    try:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO session_links(session_id, kind, target,"
            " created_ms) VALUES(?, ?, ?, ?)",
            (canonical, bounded_kind, bounded_target, stamp))
        conn.commit()
    except sqlite3.Error:
        _error("database is unavailable")
    return cursor.rowcount > 0


def remove_session_link(conn: sqlite3.Connection, *, session_id: str,
                        kind: str, target: str) -> bool:
    canonical = _validate_session_id(session_id)
    bounded_kind = _validate_link_kind(kind)
    bounded_target = _validate_link_target(target)
    try:
        cursor = conn.execute(
            "DELETE FROM session_links WHERE session_id = ? AND kind = ?"
            " AND target = ?",
            (canonical, bounded_kind, bounded_target))
        conn.commit()
    except sqlite3.Error:
        _error("database is unavailable")
    return cursor.rowcount > 0


def list_session_links(conn: sqlite3.Connection,
                       session_id: str) -> list[dict]:
    canonical = _validate_session_id(session_id)
    try:
        rows = conn.execute(
            "SELECT session_id, kind, target, created_ms FROM session_links"
            " WHERE session_id = ? ORDER BY kind ASC, target ASC",
            (canonical,)).fetchall()
    except sqlite3.Error:
        _error("database is unavailable")
    return [_session_link_row_to_dict(r) for r in rows]


def list_session_links_bulk(conn: sqlite3.Connection,
                             session_ids: list,
                             kind=None) -> dict[str, list[dict]]:
    """Bounded bulk map of session links, keyed by session_id.

    Only sessions with at least one link appear in the result. Invalid
    ``session_ids`` entries map to ``session list is invalid``; more
    than ``META_BULK_LIMIT`` entries map to ``session list is too
    large``.
    """
    if not isinstance(session_ids, list):
        _error("session list is invalid")
    if len(session_ids) > META_BULK_LIMIT:
        _error("session list is too large")
    bounded_kind = None
    if kind is not None:
        bounded_kind = _validate_link_kind(kind)
    ordered: list[str] = []
    seen: set[str] = set()
    try:
        for raw in session_ids:
            canonical = _validate_session_id(raw)
            if canonical not in seen:
                seen.add(canonical)
                ordered.append(canonical)
    except AnnotationsError as exc:
        if str(exc) in ("session list is invalid",
                        "session list is too large"):
            raise
        _error("session list is invalid")
    if not ordered:
        return {}
    out: dict[str, list[dict]] = {}
    for start in range(0, len(ordered), 500):
        chunk = ordered[start:start + 500]
        placeholders = ",".join("?" for _ in chunk)
        if bounded_kind is None:
            sql = (
                "SELECT session_id, kind, target, created_ms"
                " FROM session_links"
                f" WHERE session_id IN ({placeholders})"
                " ORDER BY session_id ASC, kind ASC, target ASC"
            )
            params: tuple = tuple(chunk)
        else:
            sql = (
                "SELECT session_id, kind, target, created_ms"
                " FROM session_links"
                f" WHERE session_id IN ({placeholders}) AND kind = ?"
                " ORDER BY session_id ASC, kind ASC, target ASC"
            )
            params = tuple(chunk) + (bounded_kind,)
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            _error("database is unavailable")
        for row in rows:
            parsed = _session_link_row_to_dict(row)
            out.setdefault(parsed["session_id"], []).append(parsed)
    return out


# ---------------------------------------------------------------------------
# content_index declaration check (derived FTS5, plan §5.5)
# ---------------------------------------------------------------------------

_CONTENT_INDEX_DDL_RE = re.compile(
    r"\A\s*CREATE\s+VIRTUAL\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"content_index\s+USING\s+fts5\s*\(\s*text\s*,"
    r"\s*page\s+UNINDEXED\s*,\s*line\s+UNINDEXED\s*,"
    r"\s*kind\s+UNINDEXED\s*,\s*session_id\s+UNINDEXED\s*,"
    r"\s*project_id\s+UNINDEXED\s*,"
    r"\s*tokenize\s*=\s*'unicode61\s+remove_diacritics\s+2'\s*\)\s*;?\s*\Z",
    re.IGNORECASE | re.DOTALL)


def check_content_index(conn: sqlite3.Connection) -> dict:
    """Validate the ``content_index`` FTS5 declaration (exact, like activity_fts).

    Returns ``{"ok": True, "reason": ""}`` when the table exists with
    exactly the §5.5 declaration (``text`` plus ``page``/``line``/``kind``/
    ``session_id``/``project_id`` ``UNINDEXED``, ``unicode61
    remove_diacritics 2`` tokenizer) and its FTS5 shadow tables are
    present. Otherwise ``ok`` is False with a bounded reason (missing,
    unexpected declaration, or incomplete shadow). Never throws for a
    merely absent/misshapen index; SQLite failures still map to
    ``database is unavailable``.
    """
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table'"
            " AND name = 'content_index'").fetchone()
    except sqlite3.Error:
        _error("database is unavailable")
    if row is None:
        return {"ok": False, "reason": "content index is missing"}
    try:
        sql = row["sql"]
    except (IndexError, KeyError, TypeError):
        sql = None
    if not isinstance(sql, str) or not _CONTENT_INDEX_DDL_RE.match(sql):
        return {"ok": False,
                "reason": "content index declaration is unexpected"}
    try:
        names = {str(item[0]) for item in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    except sqlite3.Error:
        _error("database is unavailable")
    if any(shadow not in names for shadow in CONTENT_INDEX_SHADOW_TABLES):
        return {"ok": False, "reason": "content index is incomplete"}
    return {"ok": True, "reason": ""}


# ---------------------------------------------------------------------------
# Legacy prune (one-shot, explicit, user-run only)
# ---------------------------------------------------------------------------

#: Phase-2a-deleted tables. Current ``SCHEMA_SQL`` never creates them;
#: they linger only in sidecars written before the slim-down.
LEGACY_TABLES = (
    "session_labels",
    "unit_labels",
    "project_overrides",
    "jev_calls",
    "focus_blocks",
)

#: Phase-2a-deleted ``session_meta`` content columns. The reduced table
#: keeps session_id/device_id/revision/created_ms/updated_ms plus the
#: Phase-2c thought/todo/attended columns (thought_ref, todo_refs, refs,
#: attended).
LEGACY_SESSION_META_COLUMNS = (
    "title",
    "intent",
    "outcome",
    "tags",
    "next_step",
    "state",
    "project_override",
    "filed_ref",
    "ignore_reason",
)

#: Indexes owned by deleted storage (dropped with their tables, but
#: removed explicitly so a partial legacy schema cannot leave them).
LEGACY_INDEXES = (
    "idx_session_meta_state",
    "idx_focus_blocks_single_open",
)

REDUCED_SESSION_META_COLUMNS = (
    "session_id",
    "device_id",
    "revision",
    "created_ms",
    "updated_ms",
    "thought_ref",
    "todo_refs",
    "refs",
    "attended",
)


def prune_legacy(db=None, *, now_ms=None) -> dict:
    """One-shot prune of Phase-2a-deleted storage; explicit user-run only.

    Backs the sidecar up to ``annotations.db.bak-<ms>`` FIRST (before any
    write), then drops ``session_labels``, ``unit_labels``,
    ``project_overrides``, ``jev_calls`` and ``focus_blocks``, rebuilds
    ``session_meta`` down to its reduced columns (including the Phase-2c
    ``thought_ref``/``todo_refs``/``refs``/``attended`` columns, defaulted
    when the old table predates them), and rebuilds the
    surviving indexes. Surviving rows are preserved; the backup keeps the
    pre-prune content so a failed run is recoverable by file restore.

    Nothing in the shell runs this automatically: no QML ladder,
    scheduler job, or helper invokes it (pinned by test).
    """
    path = resolve_db_path(db)
    if not path.name or path.name in (".", ".."):
        _error("database path is unsafe")
    try:
        exists = path.is_file()
    except OSError:
        _error("database is unavailable")
    if not exists:
        _error("database not found")
    if now_ms is None:
        stamp = int(time.time() * 1000)
        if stamp < 0 or stamp > I64_MAX:
            _error("database is unavailable")
    else:
        stamp = _validate_ms(now_ms)
    backup = path.parent / f"{path.name}.bak-{stamp}"
    try:
        if backup.exists():
            _error("backup already exists")
    except OSError:
        _error("database is unavailable")
    # Checkpoint WAL into the db BEFORE copying so the backup holds every
    # committed row even when a -wal sidecar is present. connect() only
    # creates surviving tables; it never drops anything.
    checkpoint = connect(db)
    try:
        try:
            checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            _error("database is unavailable")
    finally:
        try:
            checkpoint.close()
        except Exception:
            pass
    try:
        shutil.copyfile(str(path), str(backup))
    except OSError:
        _error("database is unavailable")
    try:
        os.chmod(backup, 0o600)
    except OSError:
        pass
    conn = connect(db)
    try:
        try:
            tables = {str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()}
        except sqlite3.Error:
            _error("database is unavailable")
        dropped: list[str] = []
        for table in LEGACY_TABLES:
            if table in tables:
                try:
                    conn.execute(f"DROP TABLE {table}")
                except sqlite3.Error:
                    _error("database is unavailable")
                dropped.append(table)
        for index in LEGACY_INDEXES:
            try:
                conn.execute(f"DROP INDEX IF EXISTS {index}")
            except sqlite3.Error:
                _error("database is unavailable")
        try:
            info = conn.execute(
                "PRAGMA table_info(session_meta)").fetchall()
        except sqlite3.Error:
            _error("database is unavailable")
        pruned_meta = False
        if info:
            have = [str(row[1]) for row in info]
            if set(have) != set(REDUCED_SESSION_META_COLUMNS):
                have_set = set(have)
                selects: list[str] = []
                for column in REDUCED_SESSION_META_COLUMNS:
                    if column in have_set:
                        if column in ("device_id", "thought_ref"):
                            selects.append(f"COALESCE({column}, '')")
                        elif column in ("todo_refs", "refs"):
                            selects.append(f"COALESCE({column}, '[]')")
                        elif column == "attended":
                            selects.append(f"COALESCE({column}, 0)")
                        else:
                            selects.append(column)
                    elif column in ("device_id", "thought_ref"):
                        selects.append("''")
                    elif column in ("todo_refs", "refs"):
                        selects.append("'[]'")
                    elif column == "attended":
                        selects.append("0")
                    elif column in ("created_ms", "updated_ms"):
                        selects.append("0")
                    else:
                        selects.append("''")
                try:
                    conn.execute(
                        "CREATE TABLE session_meta_pruned("
                        " session_id TEXT PRIMARY KEY,"
                        " device_id TEXT NOT NULL DEFAULT '',"
                        " revision TEXT NOT NULL,"
                        " created_ms INTEGER NOT NULL,"
                        " updated_ms INTEGER NOT NULL,"
                        " thought_ref TEXT NOT NULL DEFAULT '',"
                        " todo_refs TEXT NOT NULL DEFAULT '[]',"
                        " refs TEXT NOT NULL DEFAULT '[]',"
                        " attended INTEGER NOT NULL DEFAULT 0)")
                    conn.execute(
                        "INSERT INTO session_meta_pruned("
                        + ", ".join(REDUCED_SESSION_META_COLUMNS) + ")"
                        " SELECT " + ", ".join(selects)
                        + " FROM session_meta")
                    conn.execute("DROP TABLE session_meta")
                    conn.execute(
                        "ALTER TABLE session_meta_pruned"
                        " RENAME TO session_meta")
                except sqlite3.Error:
                    _error("database is unavailable")
                pruned_meta = True
        _init_schema(conn)
        try:
            conn.execute("VACUUM")
        except sqlite3.Error:
            _error("database is unavailable")
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {"ok": True, "backup": str(backup),
            "dropped_tables": sorted(dropped),
            "pruned_session_meta": pruned_meta}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# CLI (shared plumbing lives in qscli.py; argv stays byte-identical)
# ---------------------------------------------------------------------------

# Grandfathered: programmatic callers (tests/test_annotations.py) expect
# annotations._emit to raise AnnotationsError; transport is qscli.emit.
def _emit(value: dict) -> None:
    try:
        qscli.emit(value)
    except ValueError as exc:
        _error(str(exc))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = qscli.SafeParser(description=__doc__)
    qscli.add_global_flags(parser, db=True)
    sub = parser.add_subparsers(dest="command", required=True,
                                parser_class=qscli.SafeParser)
    sub.add_parser("init")
    p_sget = sub.add_parser("state-get")
    p_sget.add_argument("--key", required=True)
    p_sset = sub.add_parser("state-set")
    p_sset.add_argument("--key", required=True)
    p_sset.add_argument("--value", required=True)
    sub.add_parser("prune-legacy")
    return parser.parse_args(argv if argv is not None else sys.argv[1:])


def _dispatch(args: argparse.Namespace) -> dict:
    if args.command == "prune-legacy":
        # Explicit user-run only: the backup happens inside
        # prune_legacy before any write, and no shell path calls it.
        return prune_legacy(args.db)
    conn = connect(args.db)
    try:
        if args.command == "init":
            return {"ok": True, "path": str(resolve_db_path(args.db))}
        elif args.command == "state-get":
            value = get_state(conn, args.key)
            return {"key": args.key, "value": value}
        elif args.command == "state-set":
            set_state(conn, args.key, args.value)
            return {"ok": True, "key": args.key}
        else:
            _error("unknown command")
    finally:
        try:
            conn.close()
        except Exception:
            pass


_BOUNDED_EXCEPTIONS = (AnnotationsError, OSError, TypeError, ValueError,
                        UnicodeError, sqlite3.Error, RecursionError,
                        OverflowError)


def main(argv: list[str] | None = None) -> int:
    return qscli.run_main(_parse_args, _dispatch, "annotations",
                          _BOUNDED_EXCEPTIONS, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
