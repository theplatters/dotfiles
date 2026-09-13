// Region-selection geometry helpers for the command palette.
//
// Owns safeNumber(object, name, fallback), normalizedSelection(startX, startY,
// endX, endY), geometryForSelection(selection, offset), and
// clampSelectionCoordinate(value, limit).
//
// Contracts:
// - safeNumber(object, name, fallback): returns object[name] when it is a
//   finite number, else fallback. Never throws.
// - normalizedSelection(startX, startY, endX, endY): normalizes a drag into
//   {x, y, width, height} with floored origin, ceiled extent, and a 4px
//   minimum size. Returns null when any coordinate is not finite.
// - geometryForSelection(selection, offset): formats a normalized selection
//   as the "x,y WxH" string grim expects, shifted by the screen offset
//   {x, y} the QML caller passes from screenOffset(). A missing offset
//   defaults to {x: 0, y: 0}. Returns "" for null/unusable selections.
// - clampSelectionCoordinate(value, limit): clamps value into [0, limit].
//
// Callers: widgets/CommandPalette.qml (as PaletteGeometry; screenOffset()
// stays in QML because it needs Hyprland/screen and passes its result in as
// offset), widgets/PaletteCapture.qml (normalizedSelection,
// geometryForSelection, and clampSelectionCoordinate for the region overlay),
// and tests/test_palette_geometry.py plus
// tests/test_palette_routing.py via node vm.
//
// Purity: no QML-context access (no root/Qt/Hyprland/screen); every input
// arrives as an argument.

function safeNumber(object, name, fallback) {
    try {
        if (!object) return fallback;
        let value = object[name];
        return typeof value === "number" && isFinite(value) ? value : fallback;
    } catch (error) {
        return fallback;
    }
}

function normalizedSelection(startX, startY, endX, endY) {
    if (![startX, startY, endX, endY].every(value => typeof value === "number" && isFinite(value)))
        return null;
    let left = Math.floor(Math.min(startX, endX));
    let top = Math.floor(Math.min(startY, endY));
    let right = Math.ceil(Math.max(startX, endX));
    let bottom = Math.ceil(Math.max(startY, endY));
    return { x: left, y: top, width: Math.max(4, right - left),
             height: Math.max(4, bottom - top) };
}

function geometryForSelection(selection, offset) {
    if (!selection) return "";
    let origin = offset || { x: 0, y: 0 };
    let x = Math.round(selection.x + origin.x);
    let y = Math.round(selection.y + origin.y);
    let width = Math.max(4, Math.round(selection.width));
    let height = Math.max(4, Math.round(selection.height));
    if (!isFinite(x) || !isFinite(y) || !isFinite(width) || !isFinite(height)) return "";
    return x + "," + y + " " + width + "x" + height;
}

function clampSelectionCoordinate(value, limit) {
    return Math.max(0, Math.min(limit, value));
}
