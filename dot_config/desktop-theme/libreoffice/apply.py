#!/usr/bin/env python3
"""Fuse Rich Black desktop-theme settings into LibreOffice's registrymodifications.xcu.

Idempotent: parses Theme.qml tokens + rich-black.settings.json, then rewrites
only the targeted <item> entries in the XCU, leaving every other entry
byte-identical. The custom scheme node is compared SEMANTICALLY (entry colors
and visibility, ignoring attribute order / oor:op / entry order), because
LibreOffice rewrites the block on every run (reorders entries, flips
oor:op="fuse" to "replace"). Backs up the XCU to
registrymodifications.xcu.bak first, rotating the previous backup to .bak.1.

Usage:
    python3 apply.py [--xcu PATH] [--manifest PATH] [--theme PATH] [--dry-run]
"""

import argparse
import json
import os
import re
import shutil
import sys
import xml.dom.minidom
from xml.sax.saxutils import escape

HOME = os.path.expanduser("~")
DEFAULT_XCU = os.path.join(HOME, ".config/libreoffice/4/user/registrymodifications.xcu")
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MANIFEST = os.path.join(HERE, "rich-black.settings.json")
DEFAULT_THEME = os.path.join(HOME, ".config/quickshell/theme/Theme.qml")


def parse_theme_tokens(theme_path):
    """Minimal parser for Theme.qml `readonly property color NAME: "#HEX"` lines.

    Qt "#AARRGGBB" values are preserved WITH their alpha nibble ("#AARRGGBB")
    so resolve_color() can handle (or refuse) the alpha explicitly instead of
    silently dropping it. Plain "#RRGGBB" values are kept as-is.
    """
    tokens = {}
    with open(theme_path, encoding="utf-8") as f:
        for line in f:
            m = re.search(
                r"readonly\s+property\s+color\s+(\w+)\s*:\s*\"#([0-9A-Fa-f]{6,8})\"", line
            )
            if m:
                name, hexval = m.group(1), m.group(2)
                tokens[name] = "#" + hexval.upper()
    if not tokens:
        raise SystemExit(f"error: no color tokens parsed from {theme_path}")
    return tokens


def resolve_color(spec, tokens):
    """'token:NAME' or '#RRGGBB' -> (hex '#RRGGBB', decimal int for xs:int).

    LibreOffice packs config Colors as 0xRRGGBB (red in the HIGH byte):
    tools Color stores mValue = B | G<<8 | R<<16 (| T<<24) and serializes to
    the config layer as sal_Int32(mValue) (see include/tools/color.hxx,
    `static_assert(sal_uInt32(Color(0x12,0x34,0x56)) == 0x00123456)`).
    So the decimal is plain int(RRGGBB, 16) -- NO byte swap. (The old
    RGB_COLORDATA R|G<<8|B<<16 packing is a Win32-COLORREF-era macro and does
    NOT apply to config serialization.)

    Alpha cannot be represented in an xs:int Color, so #AARRGGBB inputs are
    only accepted when fully opaque (AA == FF); anything translucent is a
    hard error rather than a silently-corrupted color.
    """
    if spec.startswith("token:"):
        name = spec[len("token:"):]
        if name not in tokens:
            raise SystemExit(f"error: manifest references unknown Theme.qml token '{name}'")
        hexval = tokens[name]
    elif re.fullmatch(r"#[0-9A-Fa-f]{6}", spec):
        hexval = spec.upper()
    else:
        raise SystemExit(f"error: bad color spec {spec!r} (want 'token:NAME' or '#RRGGBB')")
    if len(hexval) == 9:  # '#AARRGGBB'
        alpha, hexval = hexval[1:3], "#" + hexval[3:]
        if alpha != "FF":
            raise SystemExit(
                f"error: color spec {spec!r} resolves to {hexval} with alpha "
                f"{alpha}: LibreOffice xs:int colors are opaque-only, refusing "
                f"to silently drop translucency"
            )
    r, g, b = int(hexval[1:3], 16), int(hexval[3:5], 16), int(hexval[5:7], 16)
    intval = (r << 16) | (g << 8) | b
    assert intval == int(hexval[1:], 16)
    return hexval, intval


def decode_color(intval):
    """Decimal xs:int back to '#RRGGBB' under the 0xRRGGBB packing (inverse of resolve_color)."""
    return "#%06X" % (int(intval) & 0xFFFFFF)


def format_value(vtype, value):
    if vtype == "boolean":
        return "true" if value else "false"
    if vtype in ("short", "int"):
        return str(int(value))
    return str(value)


def find_item_spans(text, path):
    """Yield (start, end) spans of every <item oor:path="PATH">...</item>."""
    pat = re.compile(
        r"<item oor:path=\"" + re.escape(path) + r"\">.*?</item>", re.DOTALL
    )
    return [(m.start(), m.end()) for m in pat.finditer(text)]


def ensure_prop(text, path, prop, value_str, changes, label):
    """Ensure one fused <prop> inside the <item> for path; return updated text.

    If several <item> blocks share the path, the one already containing the
    prop is used; otherwise the prop is appended to the first block with that
    path, or a brand-new <item> is inserted before </oor:items>.
    """
    spans = find_item_spans(text, path)
    target = None
    for s, e in spans:
        block = text[s:e]
        if re.search(r"<prop oor:name=\"" + re.escape(prop) + r"\"", block):
            target = (s, e)
            break
    if target is None:
        if spans:
            # Path exists but not this prop: append prop to first block.
            s, e = spans[0]
            block = text[s:e]
            new_prop = f'<prop oor:name="{prop}" oor:op="fuse"><value>{escape(value_str)}</value></prop>'
            new_block = block[: -len("</item>")] + new_prop + "</item>"
            text = text[:s] + new_block + text[e:]
            changes.append(f"ADD {label}: <item {path}> += {prop}={value_str}")
            return text
        # No such item at all: insert before closing root tag.
        close = text.rfind("</oor:items>")
        if close < 0:
            raise SystemExit("error: </oor:items> closing tag not found in XCU")
        new_item = (
            f'<item oor:path="{escape(path)}">'
            f'<prop oor:name="{prop}" oor:op="fuse"><value>{escape(value_str)}</value></prop>'
            f"</item>\n"
        )
        text = text[:close] + new_item + text[close:]
        changes.append(f"ADD {label}: new <item {path}> {prop}={value_str}")
        return text
    s, e = target
    block = text[s:e]
    m = re.search(
        r"(<prop oor:name=\"" + re.escape(prop) + r"\"[^>]*><value>)(.*?)(</value></prop>)",
        block,
        re.DOTALL,
    )
    if not m:
        raise SystemExit(f"error: cannot parse value of {path}/{prop}")
    old = m.group(2)
    if old == value_str:
        return text  # already correct -> idempotent no-op
    new_block = block[: m.start(2)] + escape(value_str) + block[m.end(2):]
    text = text[:s] + new_block + text[e:]
    changes.append(f"SET {label}: {path}/{prop}: {old!r} -> {value_str!r}")
    return text


def remove_items_with_path_prefix(text, prefix, changes, label):
    """Remove every <item> whose oor:path is under prefix; return updated text.

    Used to migrate away the per-entry scheme items (one <item> per
    .../ColorSchemes/RICH_BLACK/<Entry>) that LibreOffice 26.8 silently drops
    on startup; the scheme is instead stored as a single nested-node <item>.
    Only touches paths under our own scheme, nothing else.
    """
    pat = re.compile(
        r"<item oor:path=\"" + re.escape(prefix) + r"[^\"\]]*\">.*?</item>\n?", re.DOTALL
    )
    found = pat.findall(text)
    if not found:
        return text
    text = pat.sub("", text)
    changes.append(f"REMOVE {label}: dropped {len(found)} stale per-entry item(s) under {prefix}")
    return text


def build_scheme_item(scheme_path, scheme_name, entries):
    """Build the single nested-node <item> for the whole custom scheme.

    Mirrors how LibreOffice itself persists set members (cf. Histories
    ItemList entries in this same file): one <item> addressed at the set,
    with the member as <node oor:op="fuse">. `entries` maps entry name to
    (color_int, visible_or_None).
    """
    parts = [f'<item oor:path="{escape(scheme_path)}">',
             f'<node oor:name="{escape(scheme_name)}" oor:op="fuse">']
    for entry, (color_int, visible) in entries.items():
        parts.append(f'<node oor:name="{escape(entry)}" oor:op="fuse">')
        if visible is not None:
            vis = "true" if visible else "false"
            parts.append(f'<prop oor:name="IsVisible" oor:op="fuse"><value>{vis}</value></prop>')
        parts.append(f'<prop oor:name="Color" oor:op="fuse"><value>{color_int}</value></prop>')
        parts.append("</node>")
    parts.append("</node></item>\n")
    return "".join(parts)


def parse_scheme_block(block):
    """Parse one nested scheme <item> block into {entry: (color_or_None, visible_or_None)}.

    Tolerant of LibreOffice's rewrites: entry order, prop order (Color vs
    IsVisible), oor:op values (fuse/replace/none) and whitespace are all
    ignored. An empty/missing <value> (LO persists COL_AUTO "automatic" colors
    as void) yields color None.
    """
    entries = {}
    # Tempered body ((?!<node ).)*? matches only LEAF nodes (real entries);
    # the outer scheme node contains nested <node>s so it never matches.
    for m in re.finditer(
        r"<node oor:name=\"([^\"]+)\"[^>]*>((?:(?!<node ).)*?)</node>", block, re.DOTALL
    ):
        name, body = m.group(1), m.group(2)
        cm = re.search(r"<prop oor:name=\"Color\"[^>]*><value>(.*?)</value></prop>", body, re.DOTALL)
        if not cm:
            cm = re.search(r"<prop oor:name=\"Color\"[^>]*/>", body)
            color = None
        else:
            raw = cm.group(1).strip()
            color = int(raw) if re.fullmatch(r"-?\d+", raw) else None
        vm = re.search(
            r"<prop oor:name=\"IsVisible\"[^>]*><value>(.*?)</value></prop>", body, re.DOTALL
        )
        visible = None
        if vm:
            visible = vm.group(1).strip() == "true"
        entries[name] = (color, visible)
    return entries


def ensure_scheme_item(text, scheme_path, scheme_name, entries, changes, notes):
    """Ensure the single nested-node scheme <item>; return updated text.

    Compares SEMANTICALLY: the stored block is parsed into
    entry -> (color, visible) and diffed against the desired dict, so
    LibreOffice's own rewrites (reordered entries, oor:op fuse->replace,
    Color-before-IsVisible) do not trigger a rewrite. Extra stored entries
    (e.g. added via Tools > Options) are tolerated and left alone (reported
    via `notes`, not `changes`, so they never trigger a rewrite); missing
    or mismatched manifest entries trigger a rewrite to the canonical form.
    """
    spans = find_item_spans(text, scheme_path)
    for s, e in spans:
        block = text[s:e]
        if re.search(r"<node oor:name=\"" + re.escape(scheme_name) + r"\"", block):
            stored = parse_scheme_block(block)
            problems = []
            for entry, (want_color, want_vis) in entries.items():
                if entry not in stored:
                    problems.append(f"{entry}: missing (want {want_color})")
                else:
                    got_color, got_vis = stored[entry]
                    if got_color != want_color:
                        got_hex = decode_color(got_color) if got_color is not None else "automatic(void)"
                        problems.append(
                            f"{entry}: color {got_color} ({got_hex}) != {want_color} ({decode_color(want_color)})"
                        )
                    if want_vis is not None and got_vis != want_vis:
                        problems.append(f"{entry}: IsVisible {got_vis} != {want_vis}")
            if not problems:
                extra = sorted(set(stored) - set(entries))
                if extra:
                    notes.append(
                        f"NOTE ColorSchemes/{scheme_name}: {len(entries)} manifest "
                        f"entries match; {len(extra)} extra stored entrie(s) left "
                        f"alone: {', '.join(extra)}"
                    )
                return text  # already correct -> idempotent no-op
            desired = build_scheme_item(scheme_path, scheme_name, entries)
            text = text[:s] + desired.rstrip("\n") + text[e:]
            changes.append(
                f"SET ColorSchemes/{scheme_name}: rewrote nested scheme item "
                f"({len(problems)} mismatched entrie(s): {'; '.join(problems[:5])}"
                f"{'...' if len(problems) > 5 else ''})"
            )
            return text
    close = text.rfind("</oor:items>")
    if close < 0:
        raise SystemExit("error: </oor:items> closing tag not found in XCU")
    desired = build_scheme_item(scheme_path, scheme_name, entries)
    text = text[:close] + desired + text[close:]
    changes.append(
        f"ADD ColorSchemes/{scheme_name}: new nested scheme item ({len(entries)} entries)"
    )
    return text


def main():
    ap = argparse.ArgumentParser(description="Apply Rich Black theme to LibreOffice XCU")
    ap.add_argument("--xcu", default=DEFAULT_XCU)
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST)
    ap.add_argument("--theme", default=DEFAULT_THEME)
    ap.add_argument("--dry-run", action="store_true", help="print changes, write nothing")
    args = ap.parse_args()

    tokens = parse_theme_tokens(args.theme)
    with open(args.manifest, encoding="utf-8") as f:
        manifest = json.load(f)

    if os.path.exists(args.xcu):
        with open(args.xcu, encoding="utf-8") as f:
            text = f.read()
        # Fail fast on corrupt input before touching anything.
        xml.dom.minidom.parseString(text.encode("utf-8"))
    else:
        # Fresh desktop: LibreOffice has not created its user profile yet.
        # Start from a minimal valid user-registry overlay so the theme is
        # applied on first launch instead of crashing on a missing file.
        text = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry"'
            ' xmlns:xs="http://www.w3.org/2001/XMLSchema"'
            ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
            '</oor:items>\n'
        )
        os.makedirs(os.path.dirname(args.xcu), exist_ok=True)

    changes = []
    notes = []

    for item in manifest.get("simple_items", []):
        value_str = format_value(item["type"], item["value"])
        text = ensure_prop(text, item["path"], item["prop"], value_str, changes,
                           f'{item["path"]}/{item["prop"]}')

    scheme = manifest.get("color_scheme", {})
    scheme_name = scheme.get("name", "RICH_BLACK")
    scheme_path = "/org.openoffice.Office.UI/ColorScheme/ColorSchemes"
    # Migrate away any stale per-entry items from the earlier storage format
    # (one <item> per .../RICH_BLACK/<Entry>): LO 26.8 drops those on startup.
    text = remove_items_with_path_prefix(
        text, f"{scheme_path}/{scheme_name}/", changes, "color-scheme"
    )
    entries = {}
    hexvals = {}
    for entry, spec in scheme.get("entries", {}).items():
        hexval, intval = resolve_color(spec["color"], tokens)
        entries[entry] = (intval, spec.get("visible"))
        hexvals[entry] = hexval
    # Encoding self-check: every stored int must decode back to its #RRGGBB.
    for entry, (intval, _vis) in entries.items():
        if decode_color(intval) != hexvals[entry]:
            raise SystemExit(
                f"error: encoding round-trip failed for {entry}: "
                f"{hexvals[entry]} -> {intval} -> {decode_color(intval)}"
            )
    text = ensure_scheme_item(text, scheme_path, scheme_name, entries, changes, notes)

    # Validate the result is still well-formed XML.
    xml.dom.minidom.parseString(text.encode("utf-8"))

    if not changes:
        for n in notes:
            print(f"apply.py: {n}")
        print("apply.py: everything already up to date, no changes.")
        return 0

    print(f"apply.py: {len(changes)} change(s):")
    for c in changes:
        print(f"  {c}")
    for n in notes:
        print(f"  {n}")
    print(f"  (tokens used: {', '.join(f'{k}={v}' for k, v in sorted(tokens.items()))})")
    # Show the encoding is right on real hue (non-gray) tokens: stored decimal
    # must decode to the intended #RRGGBB under the 0xRRGGBB packing.
    samples = [e for e in ("Links", "Spell", "CalcGrid", "SmartTags") if e in entries]
    for e in samples:
        print(f"  (encoding check: {e} {hexvals[e]} -> {entries[e][0]} -> {decode_color(entries[e][0])})")
    if args.dry_run:
        print("apply.py: --dry-run, wrote nothing.")
        return 0

    if os.path.exists(args.xcu):
        backup = args.xcu + ".bak"
        if os.path.exists(backup):
            # Rotate: keep one prior generation instead of clobbering it.
            shutil.copy2(backup, backup + ".1")
            print(f"apply.py: rotated prior backup {backup} -> {backup}.1")
        shutil.copy2(args.xcu, backup)
        print(f"apply.py: backed up {args.xcu} -> {backup}")
    else:
        print(f"apply.py: {args.xcu} did not exist (fresh profile), no backup")
    with open(args.xcu, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    print(f"apply.py: wrote {args.xcu} (XML re-validated OK)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
