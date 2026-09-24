"""Shared Rich Black theme tokens.

The single source of truth is the QML singleton
``~/.config/quickshell/theme/Theme.qml``.  This module parses that file
with regular expressions -- ``readonly property color NAME: "#.."``
and ``readonly property int NAME: N`` (with or without a trailing
semicolon) -- so every consumer (GTK CSS,
Qt QSS, qt6ct palette, ...) derives its values from the same tokens
instead of inventing shades.

Dependency-free, importable, no side effects on import.

Conventions (matching Qt/QML color syntax):
* 6-digit ``#RRGGBB`` values are opaque.
* 8-digit ``#AARRGGBB`` values carry an alpha channel first
  (``focusBorder`` and ``scrim`` in Theme.qml).
"""

from __future__ import annotations

import re
from pathlib import Path

#: Default location of the authoritative token file.
THEME_QML = (
    Path(__file__).resolve().parent.parent / "quickshell" / "theme" / "Theme.qml"
)

_COLOR_RE = re.compile(
    r"""readonly\s+property\s+color\s+(\w+)\s*:\s*"([^"]+)"\s*;?"""
)
_INT_RE = re.compile(r"""readonly\s+property\s+int\s+(\w+)\s*:\s*(\d+)\s*;?""")

_HEX6_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_HEX8_RE = re.compile(r"^#[0-9a-fA-F]{8}$")

_cache: dict | None = None


def _parse(path: Path) -> dict:
    """Parse Theme.qml, returning {name: hex-string | int}."""
    text = Path(path).read_text(encoding="utf-8")
    data: dict = {}
    for match in _COLOR_RE.finditer(text):
        data[match.group(1)] = match.group(2)
    for match in _INT_RE.finditer(text):
        data[match.group(1)] = int(match.group(2))
    if not data:
        raise ValueError(f"No theme tokens found in {path}")
    return data


def tokens(path: str | Path | None = None) -> dict:
    """Return all tokens: color names -> original hex string, int names -> int.

    Hex strings keep their original form (``#RRGGBB`` or ``#AARRGGBB``).
    """
    global _cache
    if path is None:
        if _cache is None:
            _cache = _parse(THEME_QML)
        return dict(_cache)
    return _parse(Path(path))


def _split_hex(value: str) -> tuple[int | None, int, int, int]:
    """Split a token hex into (alpha|None, r, g, b); 8-digit is #AARRGGBB."""
    value = value.strip()
    if _HEX6_RE.match(value):
        return (
            None,
            int(value[1:3], 16),
            int(value[3:5], 16),
            int(value[5:7], 16),
        )
    if _HEX8_RE.match(value):
        return (
            int(value[1:3], 16),
            int(value[3:5], 16),
            int(value[5:7], 16),
            int(value[7:9], 16),
        )
    raise ValueError(f"Unsupported color literal: {value!r}")


def _lookup(name: str, path: str | Path | None = None) -> str:
    data = tokens(path)
    if name not in data:
        raise KeyError(f"Unknown color token: {name!r}")
    value = data[name]
    if isinstance(value, int):
        raise TypeError(f"Token {name!r} is an int, not a color")
    return value


def css(name: str, path: str | Path | None = None) -> str:
    """GTK/CSS color literal for a token.

    Opaque colors render as ``#rrggbb``; alpha colors (``focusBorder``,
    ``scrim``) render as ``rgba(r, g, b, a)`` with alpha in 0..1,
    which is what GTK CSS expects.
    """
    data = tokens(path)
    if name not in data:
        raise KeyError(f"Unknown color token: {name!r}")
    value = data[name]
    if isinstance(value, int):
        raise TypeError(f"Token {name!r} is an int, not a color")
    alpha, red, green, blue = _split_hex(value)
    if alpha is None:
        return f"#{value[1:].lower()}"
    alpha_text = f"{alpha / 255:.3f}".rstrip("0").rstrip(".")
    return f"rgba({red}, {green}, {blue}, {alpha_text})"


def qss(name: str, path: str | Path | None = None) -> str:
    """Qt QSS color literal for a token.

    Opaque colors render as ``#rrggbb``; alpha colors render as
    ``rgba(r, g, b, a)`` with alpha in 0..255, which is what QSS
    expects.
    """
    data = tokens(path)
    if name not in data:
        raise KeyError(f"Unknown color token: {name!r}")
    value = data[name]
    if isinstance(value, int):
        raise TypeError(f"Token {name!r} is an int, not a color")
    alpha, red, green, blue = _split_hex(value)
    if alpha is None:
        return f"#{value[1:].lower()}"
    return f"rgba({red}, {green}, {blue}, {alpha})"


if __name__ == "__main__":
    import pprint

    pprint.pprint(tokens())
    for _demo in ("focusBorder", "scrim", "accent"):
        print(f"css({_demo}) = {css(_demo)}")
        print(f"qss({_demo}) = {qss(_demo)}")
