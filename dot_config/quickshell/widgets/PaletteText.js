// Plain-text snippet and screen-capture failure helpers for the command palette.
//
// Owns plainSnippet(value, limit, needle), boundedCaptureDetail(value), and
// captureFailure(code, errorText).
//
// Contracts:
// - plainSnippet(value, limit, needle): value is any raw clipboard/history
//   text, limit is the maximum returned length (defaults to 180), needle is
//   the optional search term used to center the match window. Returns a
//   whitespace-collapsed snippet with "…" markers when truncated.
// - boundedCaptureDetail(value): collapses whitespace like plainSnippet and
//   hard-caps the result at 240 characters for capture notices.
// - captureFailure(code, errorText): code is the capture process exit code
//   (130 means the user cancelled), errorText is the raw stderr text the QML
//   caller reads from captureError.text. Returns the bounded notice string,
//   "Screen capture cancelled…" for cancels, "Screen capture failed…" else.
//
// Callers: widgets/CommandPalette.qml (as PaletteText) and
// widgets/PaletteCapture.qml (captureFailure for the capture notice), plus
// tests/test_palette_text.py plus tests/test_palette_unified.py via node vm.
//
// Purity: no QML-context access (no root/Qt/Hyprland/captureError); every
// input arrives as an argument.

function plainSnippet(value, limit, needle) {
    let text = String(value || "").replace(/[\u0000-\u001f\u007f]/g, " ")
        .replace(/\s+/g, " ").trim();
    limit = limit || 180;
    if (text.length <= limit) return text;
    let lower = text.toLowerCase(), match = String(needle || "").toLowerCase();
    let at = match ? lower.indexOf(match) : -1;
    let start = at > Math.floor(limit * 0.55) ? Math.max(0, at - Math.floor(limit * 0.35)) : 0;
    let end = Math.min(text.length, start + limit);
    let snippet = text.substring(start, end);
    return (start ? "…" : "") + snippet.substring(0, limit - (start ? 1 : 0) - (end < text.length ? 1 : 0))
        + (end < text.length ? "…" : "");
}

function boundedCaptureDetail(value) {
    let detail = String(value || "")
        .replace(/[\u0000-\u001f\u007f]/g, " ")
        .replace(/\s+/g, " ")
        .trim();
    if (detail.length > 240) detail = detail.substring(0, 237) + "…";
    return detail;
}

function captureFailure(code, errorText) {
    let detail = boundedCaptureDetail(errorText);
    let cancelled = code === 130 || /\b(cancel(?:led|ed|l?ation)?|abort(?:ed)?|interrupt(?:ed)?)\b/i.test(detail);
    let message = cancelled ? "Screen capture cancelled" : "Screen capture failed";
    return boundedCaptureDetail(message + (detail ? ": " + detail : ""));
}
