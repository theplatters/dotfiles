#!/usr/bin/env python3
"""Deterministic Resume backend (Phase 6).

Builds an inspectable ``ResumePlan v1`` for one registry project from the
current sources of truth and optionally executes only backend-defined typed
operations. No LLM, no inference, no writes to projects/repositories/pages.

Sources of truth (never re-parsed here):

- ``projects.list_projects`` for the registry (stable ``id`` identity;
  the CURRENT registry ``name``/``logseq_path`` mapping wins).
- ``desktop_projects`` bounded Rust subprocess helpers (sessions,
  session-resources, last-activity) plus the durable local device id
  (``desktop_projects.device_id`` / Rust ``device-id``). SQLite is never
  parsed in Python.
- ``project_planner.read_page`` for the Logseq page reference/revision and
  its bounded TODOs (whole page content is never embedded in the plan).
- ``project_files`` validation/root resolution (registry ``local_folder``
  wins; the legacy page ``file::`` fallback is allowed via the current
  helpers when the registry maps no folder).
- ``project_sessions`` safe header/latest-session rules, read-only: planning
  never creates missing session directories.

CLI (JSON to stdout; single-line ``error:`` on stderr, exit 1, no traceback):

- ``list [--query TEXT] [--limit N]``
- ``plan --project ID_OR_NAME``
- ``execute --project ID_OR_NAME [--operations CSV]``

Name matching is deterministic: UUID exact; name exact (casefold) first;
otherwise a unique prefix, else a unique substring, resolves. Anything
unknown or ambiguous is rejected rather than guessed (``resume BeforeIT``
resolves uniquely to the current ``BeforeIT ECS-Rewrite`` registry entry
because it is the single prefix/substring match). ``list`` ranks the same
matches predictably (exact, prefix, substring, fuzzy subsequence) for
palette selection/preview.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import NoReturn
from urllib.parse import quote

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import desktop_projects as _dp

try:
    import projects as _projects
except ImportError:  # pragma: no cover - tests always have scripts on path
    _projects = None  # type: ignore


class ResumeError(ValueError):
    pass


def _error(message: str) -> NoReturn:
    raise ResumeError(message)


RESUME_VERSION = 1

LIST_DEFAULT_LIMIT = 20
LIST_MAX_LIMIT = 100
RESOURCE_FETCH_LIMIT = 50
SEARCH_SESSION_LIMIT = 1
# Balanced resource score: recency near session end carries the larger share.
RECENCY_WEIGHT = 0.6
FREQUENCY_WEIGHT = 0.4
MAX_SELECTED_FILES = 4
MAX_UNAVAILABLE_RESOURCES = 10
MAX_OPEN_TODOS = 10
MAX_REASON = 300
MAX_QUERY_CHARS = 256
PATH_LIMIT = 4096

OPERATION_KINDS = (
    "focus_workspace",
    "open_editor",
    "open_terminal",
    "open_logseq_page",
    "open_project_agent",
)
# Canonical execution order (plan order); --operations filters this order.
OP_ORDER = list(OPERATION_KINDS)

EXEC_TIMEOUT = 5.0
KITTY_TITLE_LIMIT = 80

WORKSPACE_MAX = 64
_WORKSPACE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-.: ]{0,63}$")


def _bound_reason(text: object, default: str = "unavailable") -> str:
    if not isinstance(text, str) or not text.strip():
        return default
    cleaned = " ".join(text.split())
    if len(cleaned) > MAX_REASON:
        cleaned = cleaned[:MAX_REASON].rstrip() + "…"
    return cleaned


# ---------------------------------------------------------------------------
# Registry + deterministic name matching
# ---------------------------------------------------------------------------

def _load_entries(registry_file=None) -> list[dict]:
    if _projects is None:
        _error("project registry helper is unavailable")
    assert _projects is not None
    try:
        response = _projects.list_projects(registry_file)
    except Exception as exc:
        raise ResumeError(f"project registry is unusable: {exc}") from exc
    entries = response.get("projects", [])
    if not isinstance(entries, list):
        _error("project registry is unusable")
    return [e for e in entries if isinstance(e, dict)]


def _is_subsequence(needle: str, haystack: str) -> bool:
    """Ordered subsequence (fuzzy) match on already-casefolded text."""
    if not needle:
        return False
    pos = 0
    for char in needle:
        found = haystack.find(char, pos)
        if found < 0:
            return False
        pos = found + 1
    return True


def _match_kind(name: str, needle_fold: str) -> str | None:
    """Rank one registry name against a casefolded query (best kind or None)."""
    hay = name.casefold()
    if hay == needle_fold:
        return "exact"
    if hay.startswith(needle_fold):
        return "prefix"
    if needle_fold in hay:
        return "substring"
    if _is_subsequence(needle_fold, hay):
        return "fuzzy"
    return None


_RANK_ORDER = {"exact": 0, "prefix": 1, "substring": 2, "fuzzy": 3}


def _validate_list_limit(value: object) -> int:
    if value is None:
        return LIST_DEFAULT_LIMIT
    if isinstance(value, bool):
        _error("limit must be an integer 1..100")
    if isinstance(value, int):
        limit = value
    elif isinstance(value, str) and value.strip():
        try:
            limit = int(value.strip(), 10)
        except ValueError as exc:
            raise ResumeError("limit must be an integer 1..100") from exc
    else:
        _error("limit must be an integer 1..100")
        raise AssertionError("unreachable")
    if limit < 1 or limit > LIST_MAX_LIMIT:
        _error(f"limit must be 1..{LIST_MAX_LIMIT}")
    return limit


def _validate_query(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        _error(f"--query must be nonempty (1..{MAX_QUERY_CHARS} chars)")
    trimmed = value.strip()
    if "\x00" in trimmed:
        _error("--query must not contain NUL")
    if len(trimmed) > MAX_QUERY_CHARS:
        _error(f"--query must be 1..{MAX_QUERY_CHARS} chars")
    return trimmed


def list_entries(query=None, limit: object = None, registry_file=None) -> dict:
    """Bounded palette listing with deterministic match ranking."""
    needle = _validate_query(query)
    bound = _validate_list_limit(limit)
    entries = _load_entries(registry_file)
    ranked: list[tuple[int, str, str, dict, str]] = []
    if needle is None:
        for entry in entries:
            ranked.append(
                (4, entry.get("name", "").casefold(), entry.get("id", ""),
                 entry, "all")
            )
    else:
        fold = needle.casefold()
        for entry in entries:
            kind = _match_kind(entry.get("name", ""), fold)
            if kind is None:
                continue
            ranked.append(
                (_RANK_ORDER[kind], entry.get("name", "").casefold(),
                 entry.get("id", ""), entry, kind)
            )
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    chosen = ranked[:bound]
    return {
        "query": needle,
        "limit": bound,
        "count": len(chosen),
        "entries": [
            {
                "id": entry.get("id", ""),
                "name": entry.get("name", ""),
                "logseq_path": entry.get("logseq_path", ""),
                "local_folder": entry.get("local_folder", ""),
                "github_url": entry.get("github_url", ""),
                "match": kind,
            }
            for (_rank, _name, _pid, entry, kind) in chosen
        ],
    }


def _candidate_names(entries: list[dict], limit: int = 5) -> str:
    names = sorted(
        {(e.get("name", "") or "").strip() for e in entries if e.get("name")}
    )
    shown = [n for n in names if n][:limit]
    extra = len(names) - len(shown)
    text = ", ".join(shown)
    if extra > 0:
        text += f", +{extra} more"
    return text or "?"


def resolve_identifier(value: object, registry_file=None) -> dict:
    """Resolve ID_OR_NAME to the current registry entry or raise.

    UUID exact first; then name exact (casefold); then unique prefix; then
    unique substring. Unknown or ambiguous identifiers are rejected, never
    guessed.
    """
    if not isinstance(value, str) or not value.strip():
        _error("project must be a UUID or registered project name")
    text = value.strip()
    if len(text) > 1024 or "\x00" in text:
        _error("project identifier is unsafe")
    entries = _load_entries(registry_file)
    # UUID exact (canonical lowercase compare).
    try:
        canonical = str(uuid.UUID(text))
    except ValueError:
        canonical = None
    if canonical is not None:
        for entry in entries:
            if str(entry.get("id", "")).lower() == canonical.lower():
                return entry
        _error(f"project '{text[:80]}' is unknown (no registry entry)")
    fold = text.casefold()
    exact = [e for e in entries if (e.get("name", "") or "").casefold() == fold]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        _error(
            f"project '{text[:80]}' is ambiguous "
            f"({len(exact)} exact name matches: "
            f"{_candidate_names(exact)}); use a UUID"
        )
    prefix = [
        e for e in entries if (e.get("name", "") or "").casefold().startswith(fold)
    ]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        _error(
            f"project '{text[:80]}' is ambiguous "
            f"({len(prefix)} prefix matches: "
            f"{_candidate_names(prefix)}); use a UUID"
        )
    substr = [e for e in entries if fold in (e.get("name", "") or "").casefold()]
    if len(substr) == 1:
        return substr[0]
    if len(substr) > 1:
        _error(
            f"project '{text[:80]}' is ambiguous "
            f"({len(substr)} matches: "
            f"{_candidate_names(substr)}); use a UUID"
        )
    _error(f"project '{text[:80]}' is unknown (no registry entry)")
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# Bounded fetch wrappers (patched in tests; all read-only)
# ---------------------------------------------------------------------------

def _fetch_device_id(binary, db):
    return _dp._fetch_device_id(binary, db)


def _fetch_project_sessions(binary, db, project_id, limit=RESOURCE_FETCH_LIMIT):
    return _dp._fetch_sessions(binary, db, project_id, limit)


def _fetch_search_sessions(binary, db, project_id, device_id,
                           limit=SEARCH_SESSION_LIMIT):
    """Indexed Rust ``search`` scoped to project UUID + device id.

    Session-recency order (``matched_at_ms`` is null for project/device-only
    searches), so ``limit=1`` answers "most recent session for this exact
    project on this device" without scanning foreign history.
    """
    return _dp._fetch_search(
        binary, db, project_id, None, None, device_id, None, None, None, limit
    )


def _fetch_current_device_session(binary, db, project_id, device_id):
    """Most-recent session for this exact project on this device (indexed).

    Uses the indexed Rust ``search`` path with BOTH the project UUID and the
    durable device id, so any number of newer foreign-device sessions can
    never hide local selection. The single returned row is still validated
    (project + device must match) before use.
    """
    found = _fetch_search_sessions(binary, db, project_id, device_id)
    if not isinstance(found, dict) or not isinstance(found.get("sessions"), list):
        _error("desktop helper search must be an object")
    return select_current_device_session(
        found.get("sessions"), project_id, device_id
    )


def _fetch_session_resources(binary, db, session_id, limit=RESOURCE_FETCH_LIMIT):
    return _dp._fetch_session_resources(binary, db, session_id, limit)


def _fetch_last_activity(binary, db, project_id):
    return _dp._fetch_last_activity(binary, db, project_id)


def _which(name: str) -> str | None:
    return shutil.which(name)


#: Executables each operation kind needs on PATH. ``open_project_agent``
#: needs none: it is delegated to ProjectPlanner, never spawned here.
REQUIRED_APPS: dict[str, tuple[str, ...]] = {
    "focus_workspace": ("hyprctl",),
    "open_editor": ("kitty", "nvim"),
    "open_terminal": ("kitty",),
    "open_logseq_page": ("xdg-open",),
    "open_project_agent": (),
}
APP_NAMES = ("kitty", "nvim", "hyprctl", "xdg-open")


def _resolve_app_availability(which=None) -> dict[str, bool]:
    """PATH presence per executable (booleans only: byte-stable, injectable)."""
    probe = _which if which is None else which
    return {name: bool(probe(name)) for name in APP_NAMES}


def _missing_apps(apps: dict, kind: str) -> list[str]:
    return [name for name in REQUIRED_APPS[kind] if not apps.get(name)]


# ---------------------------------------------------------------------------
# Session selection (current device only)
# ---------------------------------------------------------------------------

def select_current_device_session(
    sessions: object, project_id: str, device_id: str | None
) -> tuple[dict | None, str]:
    """Pick the most-recent session for this exact project on this device."""
    if device_id is None:
        return None, "local device identity is unavailable"
    if not isinstance(sessions, list):
        _error("desktop helper sessions must be a list")
    wanted = project_id.lower()
    local = device_id.lower()
    candidates = []
    for row in sessions:
        if not isinstance(row, dict):
            continue
        proj = row.get("project")
        row_pid = ""
        if isinstance(proj, dict) and isinstance(proj.get("id"), str):
            row_pid = proj["id"].lower()
        if row_pid != wanted:
            continue
        if not isinstance(row.get("device_id"), str):
            continue
        if row["device_id"].lower() != local:
            continue
        if not isinstance(row.get("session_id"), str):
            continue
        candidates.append(row)
    if not candidates:
        return None, "no work session recorded for this project on this device"
    candidates.sort(
        key=lambda r: (
            r.get("end_ms", 0) if isinstance(r.get("end_ms"), int) else 0,
            r.get("start_ms", 0) if isinstance(r.get("start_ms"), int) else 0,
            r.get("session_id", ""),
        )
    )
    return candidates[-1], ""


def _curate_session(row: dict) -> dict:
    """Stable session subset (no query-time liveness flapping)."""
    apps = row.get("applications", [])
    if not isinstance(apps, list):
        apps = []
    apps = sorted({str(a) for a in apps if isinstance(a, str) and a})[:32]
    curated: dict = {
        "session_id": row.get("session_id", ""),
        "start_ms": row.get("start_ms"),
        "end_ms": row.get("end_ms"),
        "event_count": row.get("event_count"),
        "status": row.get("status", ""),
        "ended_reason": row.get("ended_reason"),
        "applications": apps,
    }
    return curated


# ---------------------------------------------------------------------------
# Resource scoring + safe file selection
# ---------------------------------------------------------------------------

def _score_resources(rollups: list, session: dict | None = None) -> list:
    """Deterministic balanced order: recency near session end + frequency.

    Normalized recency within the session bounds (``last_seen`` mapped from
    ``[start_ms, end_ms]`` onto ``[0, 1]``, clamped; stable recency rank when
    the session has no usable span) carries weight ``RECENCY_WEIGHT`` and
    normalized occurrence frequency carries ``FREQUENCY_WEIGHT``, so an
    ancient frequent file cannot always beat files touched near the end.
    Ties break on the stable ``resource_key``. At most ``MAX_SELECTED_FILES``
    are selected downstream; scoring itself is total and deterministic.
    """
    cleaned: list[tuple[int, int, str, dict]] = []
    for item in rollups:
        if not isinstance(item, dict):
            continue
        count = item.get("occurrence_count")
        seen = item.get("last_seen_ms")
        key = item.get("resource_key", "")
        if not isinstance(count, int) or isinstance(count, bool):
            count = 0
        if not isinstance(seen, int) or isinstance(seen, bool):
            seen = 0
        if not isinstance(key, str):
            key = ""
        cleaned.append((count, seen, key, item))
    if not cleaned:
        return []
    max_count = max(count for count, _seen, _key, _item in cleaned)
    start = session.get("start_ms") if isinstance(session, dict) else None
    end = session.get("end_ms") if isinstance(session, dict) else None
    span = 0
    if (isinstance(start, int) and not isinstance(start, bool)
            and isinstance(end, int) and not isinstance(end, bool)
            and end > start):
        span = end - start
    by_seen = sorted({seen for _c, seen, _k, _i in cleaned}, reverse=True)
    rank_of = {seen: rank for rank, seen in enumerate(by_seen)}
    denom = len(by_seen) - 1
    scored: list[tuple[float, str, dict]] = []
    for count, seen, key, item in cleaned:
        frequency = (count / max_count) if max_count > 0 else 0.0
        if span > 0:
            recency = (seen - start) / span  # type: ignore[operator]
            recency = min(1.0, max(0.0, recency))
        elif denom > 0:
            recency = (denom - rank_of[seen]) / denom
        else:
            recency = 1.0
        score = RECENCY_WEIGHT * recency + FREQUENCY_WEIGHT * frequency
        scored.append((-score, key, item))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [item for (_neg, _key, item) in scored]


def _validate_relative(rel: str) -> str:
    """Normalized root-relative path or raise (mirrors project_files rules)."""
    if not isinstance(rel, str) or not rel or len(rel) > PATH_LIMIT:
        _error("resource path is not a bounded project-relative path")
    assert isinstance(rel, str)
    if "\\" in rel or "\x00" in rel:
        _error("resource path is unsafe")
    candidate = Path(rel)
    if candidate.is_absolute() or any(
        part in ("", ".", "..") for part in candidate.parts
    ):
        _error("resource path is not a normalized project-relative path")
    if rel == ".":
        _error("resource path names the project root, not a file")
    return candidate.as_posix()


def _select_files(
    scored: list, root: Path | None, root_reason: str
) -> tuple[list[dict], list[dict]]:
    """Select at most 4 regular files; everything else is unavailable+reason.

    Never raises for a bad resource: symlink loops (``Path.resolve`` /
    ``is_excluded`` ``RuntimeError``), missing files, and unsafe paths are
    recorded as unavailable entries. Symlink policy reuses the
    ``project_sessions`` component check (no new policy, no mutation):
    original local-file symlinks are rejected before canonicalization and
    symlinked ancestor components are rejected for portable/local files.
    Sensitive/protected exclusions apply to both the lexical and the
    resolved root-relative paths.
    """
    files: list[dict] = []
    unavailable: list[dict] = []

    def skip(identity: str, reason: str) -> None:
        if len(unavailable) < MAX_UNAVAILABLE_RESOURCES:
            unavailable.append(
                {"identity": identity[:256], "reason": _bound_reason(reason)}
            )

    if root is None:
        for item in scored:
            try:
                ident = _identity_of(item)  # type: ignore[arg-type]
            except Exception:
                ident = ""
            skip(ident, f"project folder unavailable: {root_reason}")
            if len(unavailable) >= MAX_UNAVAILABLE_RESOURCES:
                break
        return files, unavailable

    import project_files as _pf
    import project_sessions as _ps

    try:
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError):
        for item in scored:
            try:
                ident = _identity_of(item)  # type: ignore[arg-type]
            except Exception:
                ident = ""
            skip(ident, "project folder is not accessible")
            if len(unavailable) >= MAX_UNAVAILABLE_RESOURCES:
                break
        return files, unavailable

    import stat as _stat

    for item in scored:
        if len(files) >= MAX_SELECTED_FILES:
            break
        try:
            if not isinstance(item, dict):
                skip("", "resource has no file identity")
                continue
            portable = item.get("portable_identity")
            local = item.get("local_identity")
            count = item.get("occurrence_count", 0)
            seen = item.get("last_seen_ms", 0)
            rel: str | None = None
            try:
                ident = _identity_of(item)
            except Exception:
                ident = ""
            if isinstance(portable, str) and portable.startswith("file:"):
                rel = portable[len("file:"):]
            elif isinstance(local, str) and local.startswith("file:"):
                absolute = local[len("file:"):]
                try:
                    abs_path = Path(absolute)
                    if not abs_path.is_absolute():
                        skip(ident, "local resource is not absolute")
                        continue
                    # Reject an original local-file symlink before
                    # canonicalization (no-follow, no TOCTOU trust).
                    try:
                        orig_info = abs_path.lstat()
                    except OSError:
                        orig_info = None
                    if orig_info is not None and _stat.S_ISLNK(
                        orig_info.st_mode
                    ):
                        skip(ident, "local resource is a symlink, not reopened")
                        continue
                    # Reject symlinked ancestor components of the original.
                    try:
                        _ps._check_components(abs_path)  # noqa: SLF001
                    except (
                        _ps.SessionPathError,
                        OSError,
                        RuntimeError,
                        ValueError,
                    ) as exc:
                        skip(
                            ident,
                            f"local resource ancestor is a symlink, "
                            f"not reopened: {exc}",
                        )
                        continue
                    resolved_abs = abs_path.resolve(strict=True)
                    try:
                        rel = resolved_abs.relative_to(
                            resolved_root
                        ).as_posix()
                    except ValueError:
                        skip(
                            ident,
                            "local resource is outside the project folder",
                        )
                        continue
                except (OSError, RuntimeError) as exc:
                    skip(ident, "local resource is deleted or inaccessible")
                    continue
            else:
                if isinstance(portable, str) and portable.startswith(
                    ("url:", "page:", "cwd:")
                ):
                    skip(ident, "transient/remote resource is not reopened")
                elif isinstance(local, str) and local.startswith("cwd:"):
                    skip(ident, "directory resource is not reopened")
                else:
                    skip(ident, "resource has no file identity")
                continue
            assert rel is not None
            try:
                safe_rel = _validate_relative(rel)
            except ResumeError as exc:
                skip(ident, f"resource path is unsafe: {exc}")
                continue
            # Lexical exclusion first (spelling that names a sensitive
            # path stays excluded even if later replaced by a symlink).
            try:
                lexical_excluded = _pf.is_excluded(
                    resolved_root, Path(safe_rel)
                )
            except (OSError, RuntimeError):
                skip(ident, "resource is deleted or inaccessible")
                continue
            except Exception as exc:
                skip(ident, f"resource is unavailable: {exc}")
                continue
            if lexical_excluded:
                skip(ident, "resource is sensitive or protected")
                continue
            candidate = resolved_root.joinpath(*Path(safe_rel).parts)
            # Symlinked ancestor components (portable/local) are rejected
            # via the shared project_sessions component policy.
            try:
                _ps._check_components(candidate.parent)  # noqa: SLF001
            except (
                _ps.SessionPathError,
                OSError,
                RuntimeError,
                ValueError,
            ) as exc:
                skip(
                    ident,
                    f"resource ancestor is a symlink, not reopened: {exc}",
                )
                continue
            except Exception as exc:
                skip(ident, f"resource is unavailable: {exc}")
                continue
            try:
                info = candidate.lstat()
            except OSError:
                skip(ident, "resource is deleted or missing")
                continue
            except (RuntimeError, ValueError) as exc:
                skip(ident, f"resource is unavailable: {exc}")
                continue

            if _stat.S_ISLNK(info.st_mode):
                skip(ident, "resource is a symlink, not reopened")
                continue
            if not _stat.S_ISREG(info.st_mode):
                skip(ident, "resource is not a regular file")
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                skip(ident, "resource is deleted or inaccessible")
                continue
            except Exception as exc:
                skip(ident, f"resource is unavailable: {exc}")
                continue
            try:
                resolved_rel = resolved.relative_to(resolved_root).as_posix()
            except ValueError:
                skip(ident, "resource resolves outside the project folder")
                continue
            except (OSError, RuntimeError, ValueError) as exc:
                skip(ident, f"resource is unavailable: {exc}")
                continue
            # Resolved exclusion: an alias (via a surviving non-symlink
            # spelling, hardlink, or race) into .git/sensitive/protected
            # paths is still refused.
            try:
                resolved_excluded = _pf.is_excluded(
                    resolved_root, Path(resolved_rel)
                )
            except (OSError, RuntimeError):
                skip(ident, "resource is deleted or inaccessible")
                continue
            except Exception as exc:
                skip(ident, f"resource is unavailable: {exc}")
                continue
            if resolved_excluded:
                skip(ident, "resource is sensitive or protected")
                continue
            files.append(
                {
                    "relative": safe_rel,
                    "identity": ident[:256],
                    "occurrence_count": count if isinstance(count, int) else 0,
                    "last_seen_ms": seen if isinstance(seen, int) else 0,
                }
            )
        except Exception as exc:
            try:
                ident = _identity_of(item)  # type: ignore[arg-type]
            except Exception:
                ident = ""
            skip(ident or "<resource>", f"resource is unavailable: {exc}")
            continue
    files.sort(key=lambda f: f["relative"])
    return files, unavailable


def _identity_of(item: dict) -> str:
    portable = item.get("portable_identity")
    if isinstance(portable, str) and portable:
        return portable
    local = item.get("local_identity")
    if isinstance(local, str) and local:
        return local
    key = item.get("resource_key", "")
    return key if isinstance(key, str) else ""


# ---------------------------------------------------------------------------
# Project root (registry wins; legacy file:: fallback via current helpers)
# ---------------------------------------------------------------------------

def _resolve_project_root(
    entry: dict, graph, page_content: str | None
) -> tuple[Path | None, str, list[str]]:
    """Return (root|None, reason, warnings). Never creates directories."""
    import project_files as _pf

    warnings: list[str] = []
    folder = entry.get("local_folder", "")
    if isinstance(folder, str) and folder.strip():
        expanded = os.path.expanduser(folder.strip())
        candidate = Path(expanded)
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            return (
                None,
                f"registered project folder is not accessible: {exc}",
                warnings,
            )
        try:
            root = _pf._validate_root(resolved)
        except Exception as exc:
            return None, f"registered project folder is unusable: {exc}", warnings
        return root, "", warnings
    # No registry mapping: legacy page file:: fallback via current helpers.
    if graph is None or page_content is None:
        return None, "no project folder mapping", warnings
    try:
        root = _pf.resolve_root_from_content(graph, page_content)
    except Exception as exc:
        return None, f"no project folder mapping: {exc}", warnings
    return root, "", warnings


# ---------------------------------------------------------------------------
# Logseq page (reference/revision + bounded open TODOs; never whole content)
# ---------------------------------------------------------------------------

def _read_logseq(
    graph_arg, logseq_path: str
) -> tuple[dict | None, str | None, object, str]:
    """Return (info|None, content|None, graph|None, reason). Read-only."""
    if not isinstance(logseq_path, str) or not logseq_path.strip():
        return None, None, None, "no linked Logseq page"
    from logseq_common import resolve_graph as _resolve_graph
    import project_planner as _planner

    try:
        graph = _resolve_graph(graph_arg)
    except Exception as exc:
        return None, None, None, f"logseq graph is unavailable: {exc}"
    try:
        current = _planner.read_page(graph, logseq_path.strip())
    except Exception as exc:
        return None, None, graph, f"logseq page is unreadable: {exc}"
    info = {
        "path": current.get("path", ""),
        "page": current.get("page", ""),
        "graphName": current.get("graphName", ""),
        "revision": current.get("revision", ""),
        # Verbatim parser output from read_page (never re-parsed here).
        "todos": current.get("todos", []),
    }
    return info, current.get("content", ""), graph, ""


def _summarize_todos(todos: object) -> tuple[list[dict], int, int]:
    """Bounded open TODOs plus counts, verbatim from ``read_page``.

    Returns ``(open_todos[:MAX_OPEN_TODOS], open_count, total_count)`` where
    the items are the parser's own dicts. Page content is retained
    internally only for legacy ``file::`` root resolution, never for tasks.
    """
    if not isinstance(todos, list):
        return [], 0, 0
    valid = [t for t in todos if isinstance(t, dict)]
    open_all = [t for t in valid if not t.get("done")]
    return open_all[:MAX_OPEN_TODOS], len(open_all), len(valid)


# ---------------------------------------------------------------------------
# Pi session association (read-only; never creates scope directories)
# ---------------------------------------------------------------------------

def _pi_base_readonly():
    """Read-only Pi session base without creating anything.

    When invoked from an existing scoped Pi process (``QS_PROJECT_SESSION_SCOPE``
    set), a safe existing leaf is reused structurally: a scope lives at
    ``<base>/projects/<digest>``, so the leaf's parent IS the projects base
    for deriving target scopes (for the same target it naturally resolves the
    same digest). This honors custom ``PI_CODING_AGENT_SESSION_DIR`` bases
    instead of falling back to the default state home, and it never nests a
    scope inside the current one. An unsafe/missing leaf falls through to the
    ``PI_CODING_AGENT_SESSION_DIR`` / default resolution. Nothing is created.
    """
    import project_sessions as _ps

    scoped = os.environ.get("QS_PROJECT_SESSION_SCOPE", "")
    if isinstance(scoped, str) and scoped.strip():
        try:
            leaf = Path(scoped.strip())
            if (leaf.is_absolute() and leaf.is_dir()
                    and not leaf.is_symlink()):
                _ps._check_components(leaf)  # noqa: SLF001 - reuse symlink policy
                return leaf.parent
        except (OSError, RuntimeError, _ps.SessionPathError, ValueError):
            pass
    configured = os.environ.get("PI_CODING_AGENT_SESSION_DIR", "")
    if isinstance(configured, str) and configured.strip():
        base = Path(os.path.expanduser(configured.strip()))
        return base / "projects"
    return _ps.state_home() / "quickshell" / "project-sessions" / "projects"


def _pi_scope_for(graph, logseq_path: str) -> tuple[str | None, str]:
    """Derive the target scope path read-only (no existence check, no creation).

    The configured base/projects/scope ancestors are validated
    component-by-component (symlinked ancestors rejected, consistent with
    ``project_sessions``) before any scan; the scope path itself is only
    validated here, never created. Missing scopes stay a clean unavailable
    result via :func:`_pi_latest_in_scope`.
    """
    import project_sessions as _ps

    if not isinstance(logseq_path, str) or not logseq_path.strip():
        return None, "no linked Logseq page"
    if graph is None:
        return None, "logseq graph is unavailable"
    try:
        project = _ps.project_relative_page(graph, logseq_path.strip())
    except Exception as exc:
        return None, f"project page is unusable: {exc}"
    # Validate the configured base ancestors component-by-component before
    # deriving the digest scope (no creation, no following).
    try:
        base = _pi_base_readonly()
        _ps._check_components(Path(os.path.abspath(str(base))))  # noqa: SLF001
    except (
        _ps.SessionPathError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        return None, f"Pi session base is unsafe: {exc}"
    except Exception as exc:
        return None, f"Pi session base is unavailable: {exc}"
    try:
        graph_key = str(graph.resolve(strict=True))
    except (OSError, RuntimeError) as exc:
        return None, f"logseq graph is unavailable: {exc}"
    digest = hashlib.sha256(
        f"{graph_key}\0{project.as_posix()}".encode("utf-8")
    ).hexdigest()
    scope = Path(os.path.abspath(str(base))) / digest
    try:
        _ps._check_components(scope)  # noqa: SLF001
    except (
        _ps.SessionPathError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        return None, f"Pi session scope is unsafe: {exc}"
    except Exception as exc:
        return None, f"Pi session scope is unavailable: {exc}"
    return str(scope), ""


def _pi_latest_in_scope(scope: str) -> dict:
    """Describe the latest safe Pi session in an existing scope, read-only."""
    import project_sessions as _ps

    candidate = Path(scope)
    # Component-by-component validation before any scan, consistent with
    # project_sessions: symlinked base/projects/scope ancestors are
    # rejected. Missing scopes remain a clean unavailable result; nothing
    # is ever created here.
    try:
        _ps._check_components(candidate)  # noqa: SLF001 - reuse symlink policy
    except (_ps.SessionPathError, OSError, RuntimeError, ValueError) as exc:
        return {
            "available": False,
            "reason": _bound_reason(f"Pi session scope is unsafe: {exc}"),
        }
    except Exception as exc:
        return {
            "available": False,
            "reason": _bound_reason(f"Pi session scope is unavailable: {exc}"),
        }
    try:
        if not candidate.is_dir() or candidate.is_symlink():
            return {"available": False, "reason": "no saved Pi session"}
    except OSError as exc:
        return {"available": False, "reason": f"Pi session scope is unreadable: {exc}"}
    except (RuntimeError, ValueError) as exc:
        return {
            "available": False,
            "reason": _bound_reason(f"Pi session scope is unavailable: {exc}"),
        }
    try:
        latest = _ps.latest_session(candidate)
    except Exception as exc:
        return {"available": False, "reason": f"Pi session lookup failed: {exc}"}
    if latest is None:
        return {"available": False, "reason": "no saved Pi session"}
    try:
        header = _ps._session_header(latest)  # noqa: SLF001 - reuse safe reader
    except Exception:
        header = None
    if not header:
        return {"available": False, "reason": "saved Pi session is invalid"}
    return {
        "available": True,
        "scope": str(candidate),
        "session_file": str(latest),
        "session_id": str(header.get("id", "")),
        "reason": "",
    }


def _pi_scope_info(graph, logseq_path: str) -> dict:
    """Describe the latest safe Pi session for this graph+page, read-only."""
    scope, reason = _pi_scope_for(graph, logseq_path)
    if scope is None:
        return {"available": False, "reason": reason}
    info = _pi_latest_in_scope(scope)
    if info.get("available"):
        return info
    # The scope derivation itself succeeded; keep the reason from lookup.
    return info


# ---------------------------------------------------------------------------
# Workspace reliability (latest activity must belong to the selected session)
# ---------------------------------------------------------------------------

def _validate_workspace_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _error("workspace name is empty")
    assert isinstance(value, str)
    name = value.strip()
    if len(name) > WORKSPACE_MAX or "\x00" in name:
        _error("workspace name is overlong or contains NUL")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        _error("workspace name contains control characters")
    if not _WORKSPACE_RE.fullmatch(name):
        _error("workspace name contains unsafe characters")
    if name.casefold().startswith("special"):
        _error("workspace is a special workspace, not restored")
    return name


_WORKSPACE_NUMERIC_RE = re.compile(r"^[0-9]+$")


def _workspace_selector(name: str) -> str:
    """Backend-controlled Hyprland selector for a validated workspace name.

    Numeric observed names use the numeric selector directly; every
    non-numeric literal uses ``name:<literal>`` so names like ``previous``
    are never passed as dispatcher syntax. Callers must validate with
    :func:`_validate_workspace_name` first (or rely on this helper to
    re-validate); the selector is always re-derived at execution time,
    never trusted from supplied params.
    """
    validated = _validate_workspace_name(name)
    if _WORKSPACE_NUMERIC_RE.fullmatch(validated):
        return validated
    return f"name:{validated}"


# Allowlisted characters for a backend-derived Hyprland workspace selector:
# numeric ``1`` or ``name:<literal>`` where the literal matches
# :func:`_validate_workspace_name`. Double quotes, backslashes, braces, and
# other Lua syntax can never appear here; the check below stays fail-closed
# even if the allowlist ever widens.
_WORKSPACE_SELECTOR_RE = re.compile(
    r"^(?:[A-Za-z0-9_][A-Za-z0-9_\-.: ]{0,63}"
    r"|name:[A-Za-z0-9_][A-Za-z0-9_\-.: ]{0,63})$"
)


def _workspace_dispatch_argvs(name: str) -> tuple[list[str], list[str]]:
    """Backend-built ``hyprctl`` argv pair for a validated workspace name.

    Returns ``(modern_argv, legacy_argv)``. The modern form targets current
    Hyprland (verified on 0.56.2) Lua dispatcher::

        hyprctl dispatch 'hl.dsp.focus({workspace="<selector>"})'

    and the legacy form targets older Hyprland::

        hyprctl dispatch workspace <selector>

    The selector is always re-derived via :func:`_workspace_selector`
    (which re-validates), never taken from caller params, and the Lua
    payload is a fixed backend template with only the allowlisted
    selector interpolated. No shell is ever used; both argv are list-form
    for :func:`_run_checked`.
    """
    selector = _workspace_selector(name)
    if not _WORKSPACE_SELECTOR_RE.fullmatch(selector):
        _error("workspace selector contains unsafe characters")
    if any(
        ch in selector
        for ch in ('"', "'", "\\", "{", "}", "(", ")", ";", "$", "`", "\n")
    ):
        _error("workspace selector contains unsafe characters")
    modern = ["hyprctl", "dispatch", f'hl.dsp.focus({{workspace="{selector}"}})']
    legacy = ["hyprctl", "dispatch", "workspace", selector]
    return modern, legacy


def _hyprctl_output_ok(output: object) -> bool:
    """Recognized ``hyprctl`` success: trimmed stdout is exactly ``ok``.

    Live modern responses are ``ok\\n`` and the legacy standard is
    ``ok\\n``; anything else (empty, unknown text, or an error printed
    on stdout with exit zero) is a semantic rejection and fails closed.
    """
    return isinstance(output, str) and output.strip() == "ok"


def _hypr_attempt_detail(code: object, out: object, err: object) -> str:
    """Bounded ``exited N`` plus stdout/stderr diagnostics for one attempt.

    Both streams are trimmed and clipped to 32 chars so the combined
    modern+legacy double-failure reason still fits the 300-char
    ``_bound_reason`` budget with both labels, exit statuses, and both
    stdout/stderr summaries intact for normal integer exit codes; empty
    streams read as ``<empty>`` to keep the double-failure reason truthful.
    """
    out_s = (out if isinstance(out, str) else "") or ""
    err_s = (err if isinstance(err, str) else "") or ""
    out_s = out_s.strip()[:32]
    err_s = err_s.strip()[:32]
    return (
        f"exited {code} "
        f"(stdout: {out_s if out_s else '<empty>'}; "
        f"stderr: {err_s if err_s else '<empty>'})"
    )


def _select_workspace(
    last_activity: dict | None, session: dict | None
) -> tuple[str | None, str]:
    if session is None:
        return None, "no work session"
    if not isinstance(last_activity, dict):
        return None, "no activity recorded"
    row_session = last_activity.get("session_id")
    if not isinstance(row_session, str) or not row_session:
        return None, "latest activity has no session identity"
    if row_session.lower() != str(session.get("session_id", "")).lower():
        return None, "latest activity is from another session"
    snapshot = last_activity.get("snapshot")
    if not isinstance(snapshot, dict):
        return None, "latest activity has no snapshot"
    workspace = snapshot.get("workspace")
    if workspace is None:
        return None, "latest activity has no workspace"
    if not isinstance(workspace, dict):
        return None, "workspace shape is unknown"
    try:
        return _validate_workspace_name(workspace.get("name", "")), ""
    except ResumeError as exc:
        return None, f"workspace is unreliable: {exc}"


# ---------------------------------------------------------------------------
# Repository summary (observed session metadata only; no live git)
# ---------------------------------------------------------------------------

def _summarize_repository(rollups: list) -> dict:
    branches: dict[str, int] = {}
    remotes: dict[str, int] = {}
    roots: dict[str, int] = {}
    for item in rollups:
        if not isinstance(item, dict):
            continue
        resource = item.get("resource")
        if not isinstance(resource, dict):
            continue
        for key, store in (
            ("git_branch", branches),
            ("git_remote", remotes),
            ("git_root", roots),
        ):
            value = resource.get(key)
            if isinstance(value, str) and value.strip():
                clipped = value.strip()[:512]
                store[clipped] = store.get(clipped, 0) + 1
    if not branches and not remotes and not roots:
        return {"available": False, "reason": "no repository metadata observed"}
    def _pick(store: dict[str, int]) -> str | None:
        if not store:
            return None
        best = max(store.values())
        return sorted(k for k, v in store.items() if v == best)[0]
    return {
        "available": True,
        "branch": _pick(branches),
        "remote": _pick(remotes),
        "root_observed": _pick(roots),
        "reason": "",
    }


# ---------------------------------------------------------------------------
# Pure ResumePlan builder (no IO)
# ---------------------------------------------------------------------------

def _logseq_uri(graph_name: str, page: str) -> str:
    return f"logseq://graph/{quote(graph_name, safe='')}?page={quote(page, safe='')}"


def _canonical_app_availability(value: dict | None) -> dict[str, bool]:
    """Canonicalize an injected availability map without any PATH probing.

    Missing names default to ``False``; truthiness is normalized to bool so
    plans stay byte-stable and pure-testable.
    """
    if value is None:
        return {name: False for name in APP_NAMES}
    if not isinstance(value, dict):
        return {name: False for name in APP_NAMES}
    return {name: bool(value.get(name, False)) for name in APP_NAMES}


def build_resume_plan(
    *,
    entry: dict,
    device_id: str | None,
    device_reason: str = "",
    session_row: dict | None = None,
    session_reason: str = "",
    files: list | None = None,
    unavailable_resources: list | None = None,
    repository: dict | None = None,
    logseq_info: dict | None = None,
    open_todos: list | None = None,
    open_count: int = 0,
    total_todos: int = 0,
    logseq_reason: str = "",
    pi: dict | None = None,
    root: str | None = None,
    root_reason: str = "",
    workspace: str | None = None,
    workspace_reason: str = "",
    warnings: list | None = None,
    app_availability: dict | None = None,
    agent_scope: str | None = None,
    agent_scope_reason: str = "",
) -> dict:
    """Assemble ResumePlan v1 from already-gathered inputs (pure, no IO).

    ``app_availability`` maps executable name to PATH presence (see
    :func:`_resolve_app_availability`); when omitted a deterministic
    no-probe default (all unavailable) is used so this builder never calls
    ``shutil.which``. :func:`plan_for_project` performs the live probe and
    passes the map explicitly. ``agent_scope`` is the read-only derived Pi
    scope for the ``open_project_agent`` handoff (present even with no
    saved session).
    """
    files = list(files or [])
    unavailable_resources = list(unavailable_resources or [])
    open_todos = list(open_todos or [])
    warnings = list(warnings or [])
    repository = dict(repository or {"available": False, "reason": "unknown"})
    pi = dict(pi or {"available": False, "reason": "unknown"})
    apps = _canonical_app_availability(app_availability)

    session_out: dict | None = None
    if session_row is not None:
        session_out = _curate_session(session_row)

    if logseq_info is not None:
        logseq_out: dict = {
            "available": True,
            "path": logseq_info.get("path", ""),
            "page": logseq_info.get("page", ""),
            "graphName": logseq_info.get("graphName", ""),
            "revision": logseq_info.get("revision", ""),
            "open_todos": open_todos,
            "open_count": open_count,
            "total_count": total_todos,
            "reason": "",
        }
    else:
        logseq_out = {
            "available": False,
            "path": entry.get("logseq_path", ""),
            "page": "",
            "graphName": "",
            "revision": "",
            "open_todos": [],
            "open_count": 0,
            "total_count": 0,
            "reason": _bound_reason(logseq_reason),
        }

    operations: list[dict] = []
    missing_ws = _missing_apps(apps, "focus_workspace")
    if workspace is None:
        operations.append(
            {
                "id": "focus_workspace",
                "kind": "focus_workspace",
                "available": False,
                "reason": _bound_reason(workspace_reason),
            }
        )
    elif missing_ws:
        operations.append(
            {
                "id": "focus_workspace",
                "kind": "focus_workspace",
                "available": False,
                "reason": "; ".join(f"{name} is not available" for name in missing_ws),
            }
        )
    else:
        operations.append(
            {
                "id": "focus_workspace",
                "kind": "focus_workspace",
                "available": True,
                "reason": "",
                "params": {"workspace": workspace},
            }
        )
    missing_editor = _missing_apps(apps, "open_editor")
    missing_terminal = _missing_apps(apps, "open_terminal")
    if root is None:
        reason = _bound_reason(
            f"project folder unavailable: {root_reason}"
            if root_reason
            else "project folder unavailable"
        )
        operations.append(
            {"id": "open_editor", "kind": "open_editor",
             "available": False, "reason": reason}
        )
        operations.append(
            {"id": "open_terminal", "kind": "open_terminal",
             "available": False, "reason": reason}
        )
    else:
        if missing_editor:
            operations.append(
                {
                    "id": "open_editor",
                    "kind": "open_editor",
                    "available": False,
                    "reason": "; ".join(
                        f"{name} is not available" for name in missing_editor
                    ),
                }
            )
        else:
            operations.append(
                {
                    "id": "open_editor",
                    "kind": "open_editor",
                    "available": True,
                    "reason": "",
                    "params": {
                        "root": root,
                        "files": [f["relative"] for f in files],
                    },
                }
            )
        if missing_terminal:
            operations.append(
                {
                    "id": "open_terminal",
                    "kind": "open_terminal",
                    "available": False,
                    "reason": "; ".join(
                        f"{name} is not available" for name in missing_terminal
                    ),
                }
            )
        else:
            operations.append(
                {
                    "id": "open_terminal",
                    "kind": "open_terminal",
                    "available": True,
                    "reason": "",
                    "params": {"root": root},
                }
            )
    missing_logseq = _missing_apps(apps, "open_logseq_page")
    if logseq_info is None:
        operations.append(
            {
                "id": "open_logseq_page",
                "kind": "open_logseq_page",
                "available": False,
                "reason": _bound_reason(logseq_reason),
            }
        )
    elif missing_logseq:
        operations.append(
            {
                "id": "open_logseq_page",
                "kind": "open_logseq_page",
                "available": False,
                "reason": "; ".join(
                    f"{name} is not available" for name in missing_logseq
                ),
            }
        )
    else:
        operations.append(
            {
                "id": "open_logseq_page",
                "kind": "open_logseq_page",
                "available": True,
                "reason": "",
                "params": {
                    "uri": _logseq_uri(
                        str(logseq_info.get("graphName", "")),
                        str(logseq_info.get("page", "")),
                    ),
                    "page": logseq_info.get("page", ""),
                    "graphName": logseq_info.get("graphName", ""),
                },
            }
        )
    # Delegated handoff: available whenever a valid linked page can be handed
    # to ProjectPlanner, even with no saved Pi session (the planner then
    # creates a fresh scoped session). Params say whether a saved session
    # exists and identify it only when present.
    if logseq_info is not None and agent_scope is not None:
        has_saved = bool(pi.get("available"))
        operations.append(
            {
                "id": "open_project_agent",
                "kind": "open_project_agent",
                "available": True,
                "reason": "",
                "params": {
                    "page": logseq_info.get("page", ""),
                    "graphName": logseq_info.get("graphName", ""),
                    "scope": agent_scope,
                    "has_saved_session": has_saved,
                    "session_file": pi.get("session_file") if has_saved else None,
                    "session_id": pi.get("session_id") if has_saved else None,
                },
            }
        )
    else:
        if logseq_info is None:
            agent_reason = _bound_reason(logseq_reason)
        else:
            agent_reason = _bound_reason(agent_scope_reason or "Pi scope is unavailable")
        operations.append(
            {
                "id": "open_project_agent",
                "kind": "open_project_agent",
                "available": False,
                "reason": agent_reason,
            }
        )

    return {
        "version": RESUME_VERSION,
        "project": {
            "id": entry.get("id", ""),
            "name": entry.get("name", ""),
            "logseq_path": entry.get("logseq_path", ""),
            "local_folder": entry.get("local_folder", ""),
            "github_url": entry.get("github_url", ""),
        },
        "device_id": device_id,
        "device_reason": "" if device_id is not None else _bound_reason(
            device_reason or "local device identity is unavailable"
        ),
        "session": session_out,
        "session_reason": "" if session_out is not None else _bound_reason(
            session_reason or "no work session"
        ),
        "files": files,
        "unavailable_resources": unavailable_resources,
        "repository": repository,
        "logseq": logseq_out,
        "pi_session": pi,
        "workspace": (
            {"available": True, "name": workspace, "reason": ""}
            if workspace is not None
            else {"available": False, "name": None,
                  "reason": _bound_reason(workspace_reason)}
        ),
        "operations": operations,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Plan orchestration (reads only; no desktop actions, no writes)
# ---------------------------------------------------------------------------

def plan_for_project(
    identifier,
    *,
    registry_file=None,
    desktop_bin=None,
    db=None,
    graph=None,
    which=None,
) -> dict:
    """Build a fresh ResumePlan for ID_OR_NAME (read-only, no writes).

    ``which`` probes executable availability for plan-time operation gating
    (defaults to PATH lookup); it is injectable so plans stay deterministic
    and pure-testable.
    """
    binary = _dp.resolve_desktop_bin(desktop_bin)
    db_path = _dp.resolve_db_path(db)
    entry = resolve_identifier(identifier, registry_file)
    warnings: list[str] = []
    apps = _resolve_app_availability(which)

    try:
        device_id = _fetch_device_id(binary, db_path)
        device_reason = "" if device_id is not None else "no desktop database recorded"
    except (ResumeError, _dp.DesktopError) as exc:
        device_id = None
        device_reason = str(exc)
        warnings.append(f"device identity unavailable: {exc}")

    session_row: dict | None = None
    session_reason = ""
    if device_id is None:
        session_reason = device_reason
    else:
        try:
            session_row, session_reason = _fetch_current_device_session(
                binary, db_path, str(entry.get("id", "")), device_id
            )
        except (ResumeError, _dp.DesktopError) as exc:
            session_row, session_reason = None, str(exc)
            warnings.append(f"work sessions unavailable: {exc}")
        else:
            if session_row is None and session_reason.startswith("no work session"):
                warnings.append(session_reason)

    rollups: list = []
    if session_row is not None:
        try:
            fetched = _fetch_session_resources(
                binary, db_path, str(session_row.get("session_id", ""))
            )
            rollups = fetched if isinstance(fetched, list) else []
        except (ResumeError, _dp.DesktopError) as exc:
            warnings.append(f"session resources unavailable: {exc}")
            rollups = []

    last_activity: dict | None = None
    if session_row is not None:
        try:
            last_activity = _fetch_last_activity(
                binary, db_path, str(entry.get("id", ""))
            )
        except (ResumeError, _dp.DesktopError) as exc:
            warnings.append(f"latest activity unavailable: {exc}")
            last_activity = None

    logseq_info, page_content, graph_resolved, logseq_reason = _read_logseq(
        graph, entry.get("logseq_path", "")
    )
    open_todos: list = []
    open_count = 0
    total_todos = 0
    if logseq_info is not None:
        open_todos, open_count, total_todos = _summarize_todos(
            logseq_info.get("todos", [])
        )
        if open_count == 0:
            warnings.append("no open TODOs on the linked page")
    else:
        warnings.append(_bound_reason(logseq_reason))

    root, root_reason, root_warnings = _resolve_project_root(
        entry, graph_resolved, page_content
    )
    warnings.extend(root_warnings)
    if root is None:
        warnings.append(_bound_reason(root_reason))
    root_str = str(root) if root is not None else None

    scored = _score_resources(rollups, session_row)
    files, unavailable = _select_files(scored, root, root_reason)
    if session_row is not None and not files:
        warnings.append("no prior files selected for reopen")
    if len(unavailable) >= MAX_UNAVAILABLE_RESOURCES:
        warnings.append("skipped-resource list truncated")

    repository = _summarize_repository(rollups)
    if not repository.get("available"):
        warnings.append(_bound_reason(repository.get("reason", "")))

    agent_scope, agent_scope_reason = _pi_scope_for(
        graph_resolved, entry.get("logseq_path", "")
    )
    if agent_scope is None:
        pi = {"available": False, "reason": agent_scope_reason}
        warnings.append(_bound_reason(agent_scope_reason))
    else:
        pi = _pi_latest_in_scope(agent_scope)
        if not pi.get("available"):
            warnings.append(_bound_reason(pi.get("reason", "")))

    workspace, workspace_reason = _select_workspace(last_activity, session_row)
    if workspace is None:
        warnings.append(_bound_reason(workspace_reason))

    return build_resume_plan(
        entry=entry,
        device_id=device_id,
        device_reason=device_reason,
        session_row=session_row,
        session_reason=session_reason,
        files=files,
        unavailable_resources=unavailable,
        repository=repository,
        logseq_info=logseq_info,
        open_todos=open_todos,
        open_count=open_count,
        total_todos=total_todos,
        logseq_reason=logseq_reason,
        pi=pi,
        root=root_str,
        root_reason=root_reason,
        workspace=workspace,
        workspace_reason=workspace_reason,
        warnings=warnings,
        app_availability=apps,
        agent_scope=agent_scope,
        agent_scope_reason=agent_scope_reason,
    )


# ---------------------------------------------------------------------------
# Typed execution (fixed argv only; per-operation catch; continue on failure)
# ---------------------------------------------------------------------------

def _parse_operations(value: object) -> list[str] | None:
    """Validate --operations CSV against the fixed allowlist (or None=all)."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        _error("--operations must be a nonempty CSV of operation kinds")
    kinds = [part.strip() for part in value.split(",")]
    kinds = [k for k in kinds if k]
    if not kinds:
        _error("--operations must be a nonempty CSV of operation kinds")
    for kind in kinds:
        if kind not in OPERATION_KINDS:
            _error(
                f"unknown operation '{kind[:64]}'; "
                f"allowed: {', '.join(OPERATION_KINDS)}"
            )
    seen: list[str] = []
    for kind in OP_ORDER:
        if kind in kinds and kind not in seen:
            seen.append(kind)
    return seen


def _kitty_title(project_name: str) -> str:
    cleaned = " ".join(str(project_name or "").split())
    if not cleaned:
        cleaned = "project"
    title = f"Resume: {cleaned}"
    if len(title) > KITTY_TITLE_LIMIT:
        title = title[:KITTY_TITLE_LIMIT].rstrip() + "…"
    return "".join(ch if ord(ch) >= 32 and ord(ch) != 127 else " " for ch in title)


def _run_checked(argv: list[str], timeout: float = EXEC_TIMEOUT):
    """Fixed list-form argv, no shell, bounded; returns (code, out, err)."""
    completed = subprocess.run(
        argv,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    try:
        out = completed.stdout.decode("utf-8", errors="replace")
    except Exception:
        out = ""
    try:
        err = completed.stderr.decode("utf-8", errors="replace")
    except Exception:
        err = ""
    return completed.returncode, out[-2000:], err[-2000:]


def _spawn_detached(argv: list[str], cwd: str | None = None) -> None:
    """Detached spawn, list-form argv, no shell; raises on failure."""
    with open(os.devnull, "rb") as null_in, open(
        os.devnull, "wb"
    ) as null_out:
        subprocess.Popen(
            argv,
            shell=False,
            stdin=null_in,
            stdout=null_out,
            stderr=null_out,
            cwd=cwd,
            start_new_session=True,
        )


def _execute_one(
    operation: dict,
    plan: dict,
    *,
    run_checked=None,
    spawn_detached=None,
    which=None,
) -> dict:
    """Execute one typed operation; never raises (returns a result object)."""
    op_id = str(operation.get("id", ""))
    kind = str(operation.get("kind", ""))
    project_name = plan.get("project", {}).get("name", "")
    if run_checked is None:
        run_checked = _run_checked
    if spawn_detached is None:
        spawn_detached = _spawn_detached
    if which is None:
        which = _which
    try:
        # Defense in depth for direct Python API use: only backend-defined
        # operations (stable id == kind, fixed allowlist) may execute.
        if kind not in OPERATION_KINDS or op_id != kind:
            return {"id": op_id, "kind": kind, "status": "skipped",
                    "reason": f"unknown operation '{kind[:64]}'"}
        if not operation.get("available"):
            return {
                "id": op_id,
                "kind": kind,
                "status": "skipped",
                "reason": _bound_reason(
                    operation.get("reason", "operation is unavailable")
                ),
            }
        params = operation.get("params", {})
        if not isinstance(params, dict):
            return {"id": op_id, "kind": kind, "status": "skipped",
                    "reason": "operation params are invalid"}
        if kind == "focus_workspace":
            workspace = params.get("workspace", "")
            try:
                name = _validate_workspace_name(workspace)
            except ResumeError as exc:
                return {"id": op_id, "kind": kind, "status": "skipped",
                        "reason": f"workspace is unreliable: {exc}"}
            # Re-derive the backend-controlled selector at execution time;
            # never trust a supplied selector string. Numeric literals use
            # the numeric selector, all other literals use name:<literal>.
            # The Lua payload is a fixed backend template with only the
            # allowlisted selector interpolated (fail-closed on anything
            # unexpected); no shell is ever used.
            try:
                modern_argv, legacy_argv = _workspace_dispatch_argvs(name)
            except ResumeError as exc:
                return {"id": op_id, "kind": kind, "status": "skipped",
                        "reason": f"workspace is unreliable: {exc}"}
            if which("hyprctl") is None:
                return {"id": op_id, "kind": kind, "status": "failed",
                        "reason": "hyprctl is not available"}
            # Compatibility: current Hyprland (verified on 0.56.2) expects
            # the Lua dispatcher form; older Hyprland expects the legacy
            # ``workspace <selector>`` form. Try modern first, then fall
            # back to legacy when modern is rejected. A rejection is a
            # nonzero exit OR exit zero without a recognized ``ok``
            # response: hyprctl can print an error on stdout and exit
            # zero, so trimmed stdout must be exactly ``ok`` (live
            # modern ``ok\n``, legacy ``ok\n``); unknown/empty output
            # fails closed.
            code, out, err = run_checked(modern_argv, EXEC_TIMEOUT)
            if code == 0 and _hyprctl_output_ok(out):
                return {"id": op_id, "kind": kind, "status": "ok",
                        "reason": "", "argv": modern_argv}
            modern_part = f"modern {_hypr_attempt_detail(code, out, err)}"
            legacy_code, legacy_out, legacy_err = run_checked(
                legacy_argv, EXEC_TIMEOUT
            )
            if legacy_code == 0 and _hyprctl_output_ok(legacy_out):
                return {"id": op_id, "kind": kind, "status": "ok",
                        "reason": "", "argv": legacy_argv}
            legacy_part = (
                f"legacy "
                f"{_hypr_attempt_detail(legacy_code, legacy_out, legacy_err)}"
            )
            # Truthful when both attempts fail: report both exit codes plus
            # bounded stdout/stderr (including semantic exit-0 rejections);
            # no argv is returned on double failure.
            return {"id": op_id, "kind": kind, "status": "failed",
                    "reason": _bound_reason(
                        f"hyprctl workspace focus failed "
                        f"({modern_part}; {legacy_part})"
                    )}
        if kind in ("open_editor", "open_terminal"):
            root = params.get("root", "")
            if not isinstance(root, str) or not root:
                return {"id": op_id, "kind": kind, "status": "failed",
                        "reason": "project folder is unavailable"}
            root_path = Path(root)
            try:
                resolved = root_path.resolve(strict=True)
            except (OSError, RuntimeError):
                return {"id": op_id, "kind": kind, "status": "failed",
                        "reason": "project folder is not accessible"}
            except Exception as exc:
                return {"id": op_id, "kind": kind, "status": "failed",
                        "reason": _bound_reason(
                            f"project folder is unavailable: {exc}")}
            if not resolved.is_dir():
                return {"id": op_id, "kind": kind, "status": "failed",
                        "reason": "project folder is not a directory"}
            if which("kitty") is None:
                return {"id": op_id, "kind": kind, "status": "failed",
                        "reason": "kitty is not available"}
            if kind == "open_editor" and which("nvim") is None:
                return {"id": op_id, "kind": kind, "status": "failed",
                        "reason": "nvim is not available"}
            title = _kitty_title(project_name)
            if kind == "open_editor":
                files = params.get("files", [])
                if isinstance(files, list):
                    rels = [
                        f for f in files if isinstance(f, str) and f
                    ]
                else:
                    rels = []
                # Revalidate at execution time: only still-present,
                # non-sensitive regular files inside the root. Each file
                # is isolated so one bad entry never aborts the operation.
                kept: list[str] = []
                skipped: list[dict] = []
                import stat as _stat
                import project_files as _pf
                import project_sessions as _ps

                for rel in rels:
                    try:
                        try:
                            safe = _validate_relative(rel)
                        except ResumeError as exc:
                            skipped.append(
                                {
                                    "file": rel[:256],
                                    "reason": _bound_reason(
                                        f"resource path is unsafe: {exc}"
                                    ),
                                }
                            )
                            continue
                        try:
                            if _pf.is_excluded(resolved, Path(safe)):
                                skipped.append(
                                    {
                                        "file": safe[:256],
                                        "reason": "resource is sensitive "
                                        "or protected",
                                    }
                                )
                                continue
                        except (OSError, RuntimeError):
                            skipped.append(
                                {
                                    "file": rel[:256],
                                    "reason": "resource is deleted or "
                                    "inaccessible",
                                }
                            )
                            continue
                        except Exception as exc:
                            skipped.append(
                                {
                                    "file": rel[:256],
                                    "reason": _bound_reason(
                                        f"resource is unavailable: {exc}"
                                    ),
                                }
                            )
                            continue
                        candidate = resolved.joinpath(*Path(safe).parts)
                        try:
                            _ps._check_components(  # noqa: SLF001
                                candidate.parent
                            )
                        except (
                            _ps.SessionPathError,
                            OSError,
                            RuntimeError,
                            ValueError,
                        ) as exc:
                            skipped.append(
                                {
                                    "file": safe[:256],
                                    "reason": _bound_reason(
                                        "resource ancestor is a symlink, "
                                        f"not reopened: {exc}"
                                    ),
                                }
                            )
                            continue
                        except Exception as exc:
                            skipped.append(
                                {
                                    "file": safe[:256],
                                    "reason": _bound_reason(
                                        f"resource is unavailable: {exc}"
                                    ),
                                }
                            )
                            continue
                        try:
                            info = candidate.lstat()
                        except OSError:
                            skipped.append(
                                {
                                    "file": safe[:256],
                                    "reason": "resource is deleted "
                                    "or missing",
                                }
                            )
                            continue
                        except (RuntimeError, ValueError) as exc:
                            skipped.append(
                                {
                                    "file": safe[:256],
                                    "reason": _bound_reason(
                                        f"resource is unavailable: {exc}"
                                    ),
                                }
                            )
                            continue
                        if _stat.S_ISLNK(info.st_mode):
                            skipped.append(
                                {
                                    "file": safe[:256],
                                    "reason": "resource is a symlink, "
                                    "not reopened",
                                }
                            )
                            continue
                        if not _stat.S_ISREG(info.st_mode):
                            skipped.append(
                                {
                                    "file": safe[:256],
                                    "reason": "resource is not a regular file",
                                }
                            )
                            continue
                        try:
                            resolved_c = candidate.resolve(strict=True)
                        except (OSError, RuntimeError):
                            skipped.append(
                                {
                                    "file": safe[:256],
                                    "reason": "resource is deleted or "
                                    "inaccessible",
                                }
                            )
                            continue
                        except Exception as exc:
                            skipped.append(
                                {
                                    "file": safe[:256],
                                    "reason": _bound_reason(
                                        f"resource is unavailable: {exc}"
                                    ),
                                }
                            )
                            continue
                        try:
                            resolved_rel = resolved_c.relative_to(
                                resolved
                            ).as_posix()
                        except ValueError:
                            skipped.append(
                                {
                                    "file": safe[:256],
                                    "reason": "resource resolves outside "
                                    "the project folder",
                                }
                            )
                            continue
                        except (OSError, RuntimeError, ValueError) as exc:
                            skipped.append(
                                {
                                    "file": safe[:256],
                                    "reason": _bound_reason(
                                        f"resource is unavailable: {exc}"
                                    ),
                                }
                            )
                            continue
                        try:
                            if _pf.is_excluded(
                                resolved, Path(resolved_rel)
                            ):
                                skipped.append(
                                    {
                                        "file": safe[:256],
                                        "reason": "resource is sensitive "
                                        "or protected",
                                    }
                                )
                                continue
                        except (OSError, RuntimeError):
                            skipped.append(
                                {
                                    "file": safe[:256],
                                    "reason": "resource is deleted or "
                                    "inaccessible",
                                }
                            )
                            continue
                        except Exception as exc:
                            skipped.append(
                                {
                                    "file": safe[:256],
                                    "reason": _bound_reason(
                                        f"resource is unavailable: {exc}"
                                    ),
                                }
                            )
                            continue
                        kept.append(safe)
                    except Exception as exc:
                        skipped.append(
                            {
                                "file": str(rel)[:256],
                                "reason": _bound_reason(
                                    f"resource is unavailable: {exc}"
                                ),
                            }
                        )
                        continue
                if not rels:
                    # The plan intentionally listed zero files: opening the
                    # project root in Neovim remains valid.
                    argv = ["kitty", "--directory", str(resolved),
                            "--title", title, "nvim", "-n"]
                    spawn_detached(argv, str(resolved))
                    return {"id": op_id, "kind": kind, "status": "launched",
                            "reason": "", "argv": argv}
                if not kept:
                    detail = "; ".join(
                        f"{s['file']}: {s['reason']}" for s in skipped[:4]
                    ) or "all planned files are unavailable"
                    return {
                        "id": op_id,
                        "kind": kind,
                        "status": "failed",
                        "reason": _bound_reason(
                            f"no planned files remain: {detail}"
                        ),
                        "skipped": skipped[:MAX_UNAVAILABLE_RESOURCES],
                    }
                argv = ["kitty", "--directory", str(resolved), "--title", title,
                        "nvim", "-n", "--", *kept]
                spawn_detached(argv, str(resolved))
                if skipped:
                    detail = "; ".join(
                        f"{s['file']}: {s['reason']}" for s in skipped[:4]
                    )
                    return {
                        "id": op_id,
                        "kind": kind,
                        "status": "partial",
                        "reason": _bound_reason(
                            f"{len(skipped)} file(s) unavailable: {detail}"
                        ),
                        "argv": argv,
                        "files": kept,
                        "skipped": skipped[:MAX_UNAVAILABLE_RESOURCES],
                    }
                return {"id": op_id, "kind": kind, "status": "launched",
                        "reason": "", "argv": argv, "files": kept}
            else:
                argv = ["kitty", "--directory", str(resolved), "--title", title]
            spawn_detached(argv, str(resolved))
            # Detached GUI launch: the spawn succeeding means launched
            # (unconfirmed), not a completed synchronous action.
            return {"id": op_id, "kind": kind, "status": "launched",
                    "reason": "", "argv": argv}
        if kind == "open_logseq_page":
            uri = params.get("uri", "")
            if not isinstance(uri, str) or not uri.startswith("logseq://"):
                return {"id": op_id, "kind": kind, "status": "failed",
                        "reason": "logseq URI is invalid"}
            if which("xdg-open") is None:
                return {"id": op_id, "kind": kind, "status": "failed",
                        "reason": "xdg-open is not available"}
            argv = ["xdg-open", uri]
            # Bounded checked execution so immediate URI-handler failures
            # are reported instead of claimed as success.
            code, _out, err = run_checked(argv, EXEC_TIMEOUT)
            if code != 0:
                return {"id": op_id, "kind": kind, "status": "failed",
                        "reason": _bound_reason(
                            f"xdg-open exited {code}: {err.strip()[:160]}"
                            or f"xdg-open exited {code}"
                        )}
            return {"id": op_id, "kind": kind, "status": "launched",
                    "reason": "", "argv": argv}
        if kind == "open_project_agent":
            # Delegated, never directly executed: Quickshell hands off to
            # ProjectPlanner, which safely resumes the latest scoped session.
            return {
                "id": op_id,
                "kind": kind,
                "status": "delegated",
                "reason": "handed off to ProjectPlanner; "
                          "not directly executed by the backend",
            }
        return {"id": op_id, "kind": kind, "status": "skipped",
                "reason": f"unknown operation kind '{kind[:64]}'"}
    except Exception as exc:  # per-operation catch; continue with the rest
        return {"id": op_id, "kind": kind, "status": "failed",
                "reason": _bound_reason(f"{exc}")}


def execute_operations(
    plan: dict,
    *,
    operations=None,
    run_checked=None,
    spawn_detached=None,
    which=None,
) -> dict:
    """Execute selected plan operations; continue after partial failures."""
    wanted = _parse_operations(operations)
    by_id = {op.get("id"): op for op in plan.get("operations", [])
             if isinstance(op, dict)}
    results: list[dict] = []
    for op_id in OP_ORDER:
        operation = by_id.get(op_id)
        if operation is None:
            continue
        if wanted is not None and operation.get("kind") not in wanted:
            continue
        kwargs: dict = {}
        if run_checked is not None:
            kwargs["run_checked"] = run_checked
        if spawn_detached is not None:
            kwargs["spawn_detached"] = spawn_detached
        if which is not None:
            kwargs["which"] = which
        results.append(_execute_one(operation, plan, **kwargs))
    project = plan.get("project", {})
    return {
        "project": {"id": project.get("id", ""), "name": project.get("name", "")},
        "operations": [op.get("kind") for op in results],
        "count": len(results),
        "results": results,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Deterministic Resume backend (plan + typed execution)."
    )
    parser.add_argument("--desktop-bin", default=None,
                        help="qs-desktop-context binary; defaults to "
                        "QS_DESKTOP_CONTEXT_BIN or "
                        "<repo>/services/agent-orchestrator/target/release/"
                        "qs-desktop-context")
    parser.add_argument("--db", default=None,
                        help="activity DB path; defaults to QS_DESKTOP_DB / "
                        "QS_DESKTOP_CONTEXT_DB or the binary default")
    parser.add_argument("--projects-file", default=None,
                        help="registry TOML file; defaults to "
                        "QUICKSHELL_PROJECTS_FILE or <repo>/projects.toml")
    parser.add_argument("--graph", default=None,
                        help="graph directory for Logseq page/session scope; "
                        "defaults to LOGSEQ_GRAPH or logseqGraph in settings.json")
    parser.add_argument("command", choices=("list", "plan", "execute"))
    parser.add_argument("--project", default=None,
                        help="project UUID or name (plan/execute only)")
    parser.add_argument("--query", default=None,
                        help="name filter text for list only")
    parser.add_argument("--limit", default=None,
                        help="bounded list limit 1..100 (default 20; list only)")
    parser.add_argument("--operations", default=None,
                        help="CSV subset of operation kinds "
                        f"({','.join(OPERATION_KINDS)}; execute only)")
    try:
        args = parser.parse_args(argv)
        if args.command == "list":
            if args.project:
                _error("list does not accept --project")
            if args.operations is not None:
                _error("list does not accept --operations")
            value = list_entries(args.query, args.limit, args.projects_file)
        elif args.command == "plan":
            if not args.project or not args.project.strip():
                _error("plan requires --project ID_OR_NAME")
            if args.query is not None:
                _error("plan does not accept --query")
            if args.limit is not None:
                _error("plan does not accept --limit")
            if args.operations is not None:
                _error("plan does not accept --operations")
            value = plan_for_project(
                args.project,
                registry_file=args.projects_file,
                desktop_bin=args.desktop_bin,
                db=args.db,
                graph=args.graph,
            )
        else:
            if not args.project or not args.project.strip():
                _error("execute requires --project ID_OR_NAME")
            if args.query is not None:
                _error("execute does not accept --query")
            if args.limit is not None:
                _error("execute does not accept --limit")
            # Rebuild a fresh plan by stable registry identity; the caller
            # never supplies paths/commands or a serialized plan. Validate
            # the operation selection first so unknown kinds fail fast.
            _parse_operations(args.operations)
            plan = plan_for_project(
                args.project,
                registry_file=args.projects_file,
                desktop_bin=args.desktop_bin,
                db=args.db,
                graph=args.graph,
            )
            value = execute_operations(plan, operations=args.operations)
        print(json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")))
        return 0
    except SystemExit as exc:
        return 0 if exc.code == 0 else 1
    except (ResumeError, OSError, TypeError, ValueError,
            UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
