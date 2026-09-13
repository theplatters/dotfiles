// Result-row ordering helper for the command palette.
//
// Owns compareRows(a, b).
//
// Contract:
// - compareRows(a, b): comparator for result rows. Sorts score-descending,
//   then by prefix/title/subtitle key (case-insensitive alpha), then by
//   insertion order as the final tiebreak. Returns negative/zero/positive
//   like Array.sort expects. Rows carry {prefix, title, subtitle, score,
//   order}; missing fields are treated as "" or 0.
//
// Callers: widgets/CommandPalette.qml (as PaletteModel, sorting each
// per-source group inside rebuildModel()) and tests via node vm.
//
// Purity: no QML-context access (no root/Qt/Hyprland); every input arrives
// as an argument. rebuildModel/rebuild stay in QML because they are coupled
// to resultModel, root rows, and the Hyprland/DesktopEntries/agent sources.

function compareRows(a, b) {
    let scoreDifference = (b.score || 0) - (a.score || 0);
    if (scoreDifference) return scoreDifference;
    let aKey = String(a.prefix || "") + "\u0000" + String(a.title || "") +
        "\u0000" + String(a.subtitle || "");
    let bKey = String(b.prefix || "") + "\u0000" + String(b.title || "") +
        "\u0000" + String(b.subtitle || "");
    let keyDifference = aKey.toLowerCase().localeCompare(bKey.toLowerCase());
    return keyDifference || ((a.order || 0) - (b.order || 0));
}
