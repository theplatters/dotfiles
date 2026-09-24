#!/usr/bin/env python3
"""Per-machine quickshell settings loaded from a JSON file.

The Logseq graph directory differs on every machine, so it must not be
hardwired into the config. This module is the single place that knows where
the JSON settings live and how ``LOGSEQ_GRAPH`` overrides them.

Precedence for the graph (highest first):

1. explicit ``--graph`` CLI argument (non-empty),
2. ``LOGSEQ_GRAPH`` environment variable (non-empty),
3. ``logseqGraph`` (or ``logseq_graph`` / ``LOGSEQ_GRAPH``) in settings.json,
4. otherwise: not configured (callers report a clear error).

Settings file lookup (first existing file wins):

- ``$QUICKSHELL_SETTINGS`` when it names an existing file,
- ``<cwd>/settings.json`` (QML launches helpers with the repo root as cwd),
- ``<scripts-dir>/../settings.json`` (the repo root next to this file),
- ``$XDG_CONFIG_HOME/quickshell/settings.json``,
- ``~/.config/quickshell/settings.json``.

The file is plain JSON, for example::

    {"logseqGraph": "/home/user/Nextcloud/Documents/Notes"}

Only the keys above are read; everything else is ignored so future settings
can share the same file. A missing or invalid file behaves like an empty
object — callers fall through to the "not configured" error instead of
crashing, so a fresh machine gets a clear message rather than a traceback.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SETTINGS_FILENAME = "settings.json"
SETTINGS_ENV = "QUICKSHELL_SETTINGS"
GRAPH_ENV = "LOGSEQ_GRAPH"

# Top-level keys accepted for the graph, in lookup order. Both camelCase and
# snake_case are accepted so QML/TS/Python callers agree on one file.
GRAPH_KEYS = ("logseqGraph", "logseq_graph", "LOGSEQ_GRAPH")


def _script_root() -> Path:
    return Path(__file__).resolve().parent.parent


def candidate_paths() -> list[Path]:
    """Return the settings locations in lookup order (deduplicated)."""
    candidates: list[Path] = []
    override = os.environ.get(SETTINGS_ENV, "").strip()
    if override:
        candidates.append(Path(override).expanduser())
    try:
        candidates.append(Path.cwd() / SETTINGS_FILENAME)
    except OSError:
        pass
    candidates.append(_script_root() / SETTINGS_FILENAME)
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        candidates.append(Path(xdg).expanduser() / "quickshell" / SETTINGS_FILENAME)
    candidates.append(Path.home() / ".config" / "quickshell" / SETTINGS_FILENAME)
    seen: set[str] = set()
    ordered: list[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            ordered.append(candidate)
    return ordered


def settings_path() -> Path | None:
    """Return the first existing settings file, or None."""
    for candidate in candidate_paths():
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def load_settings(path: Path | str | None = None) -> dict:
    """Load the JSON object at *path* (or the first candidate)."""
    target = Path(path).expanduser() if path is not None else settings_path()
    if target is None:
        return {}
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _graph_from_mapping(mapping: dict) -> str | None:
    for key in GRAPH_KEYS:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = mapping.get("logseq")
    if isinstance(nested, dict):
        for key in ("graph", "path", *GRAPH_KEYS):
            value = nested.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def settings_graph(settings: dict | None = None) -> str | None:
    """Return the configured graph without consulting CLI args or env.

    Env is deliberately excluded here so callers can implement the documented
    precedence explicitly; use :func:`resolve_graph_raw` for the full chain.
    """
    data = settings if settings is not None else load_settings()
    if not isinstance(data, dict):
        return None
    return _graph_from_mapping(data)


def resolve_graph_raw(explicit: str | Path | None = None,
                      settings: dict | None = None) -> str | None:
    """Return the raw graph string in precedence order, or None.

    Empty or whitespace-only values count as missing so QML can pass ``""``
    as "no override" and let the settings file decide.
    """
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    env = os.environ.get(GRAPH_ENV, "")
    if isinstance(env, str) and env.strip():
        return env.strip()
    return settings_graph(settings)


def get_setting(key: str, default=None, settings: dict | None = None):
    """Return one generic setting for future keys sharing this file."""
    data = settings if settings is not None else load_settings()
    if not isinstance(data, dict) or key not in data:
        return default
    return data[key]


_MEMORY_BOOL_DEFAULTS = {
    "enabled": True,
    "workLog": True,
    "sessionCapture": True,
    "dailyReview": True,
    "organise": False,
}

_MEMORY_INT_SPECS = {
    # key: (default, minimum, maximum). Upper bounds are hard maxima:
    # larger configured values clamp down, never pass through.
    "tickSeconds": (60, 10, 3600),
    "minSessionMs": (300000, 0, 86400000),
}

_MEMORY_TIME_DEFAULTS = {
    "reviewTime": "18:00",
    "morningTime": "07:00",
}

import re as _re

_HHMM_RE = _re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


def _strict_bool(value, default: bool) -> bool:
    return value if type(value) is bool else default


def _clamped_int(value, default: int, low: int, high: int) -> int:
    if type(value) is not int:
        return default
    if value < low:
        return low
    if value > high:
        return high
    return value


def _valid_hhmm(value, default: str) -> str:
    if not isinstance(value, str):
        return default
    text = value.strip()
    if _HHMM_RE.fullmatch(text):
        return text
    return default


def memory_settings(settings: dict | None = None) -> dict:
    """Return the validated ``memory`` block.

    The result is flat::

        {"enabled": bool, "tickSeconds": int, "workLog": bool,
         "sessionCapture": bool, "dailyReview": bool, "organise": bool,
         "minSessionMs": int, "reviewTime": "HH:MM", "morningTime": "HH:MM"}

    ``sessionCapture`` defaults to true (local regex scan, no network);
    ``organise`` gates the one explicit Pi call and defaults to false.
    Rules: strict bool typing (non-bool → default, never truthy coercion),
    integer clamping to safe ranges (tickSeconds 10..3600, minSessionMs
    0..86400000), and HH:MM validation for review/morning times.
    Malformed or missing input fails closed to defaults. Unknown keys
    (including the deleted ``jev``/``ledger`` blocks and polish flags)
    are ignored.
    """
    data = settings if settings is not None else load_settings()
    if not isinstance(data, dict):
        data = {}
    raw = data.get("memory")
    if not isinstance(raw, dict):
        raw = {}
    result: dict = {}
    for key, default in _MEMORY_BOOL_DEFAULTS.items():
        result[key] = _strict_bool(raw.get(key, default), default)
    for key, (default, low, high) in _MEMORY_INT_SPECS.items():
        result[key] = _clamped_int(raw.get(key, default), default, low, high)
    for key, default in _MEMORY_TIME_DEFAULTS.items():
        result[key] = _valid_hhmm(raw.get(key, default), default)
    return result
