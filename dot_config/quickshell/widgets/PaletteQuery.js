// Query parsing and result-row scoring helpers for the command palette.
//
// Owns parseQuery(value) and score(row, needle).
//
// Contracts:
// - parseQuery(value): value is the raw palette input text. Returns
//   {mode, text} where mode is the explicit prefix ("clip", "ai", "file",
//   "resume", "=", ">", "@", "%", "+", "#", "!", "/") or "" for unified search, and
//   text is the trimmed remainder used as the search needle.
// - score(row, needle): row carries {title, subtitle, keywords}; needle is
//   the search text. Returns 1000 for exact, 500-100 for substring,
//   fuzzy points for ordered matches, 1 for empty needle, -1 for no match.
//
// Callers: widgets/CommandPalette.qml (as Query, parsing in rebuild() and
// scoring in addRow()) and tests via node vm.
//
// Purity: no QML-context access (no root/Qt/Hyprland); every input arrives
// as an argument. rebuildModel/rebuild stay in QML because they are coupled
// to resultModel, root rows, and the Hyprland/DesktopEntries/agent sources.

const PREFIX_TO_MODE = {
  clip: "clip",
  clipboard: "clip",
  ai: "ai",
  file: "file",
  calc: "=",
  resume: "resume",
  project: "resume",
};

// Dynamically build the regex from the map keys to keep them in sync
const prefixKeys = Object.keys(PREFIX_TO_MODE).join("|");
const PREFIX_REGEX = new RegExp(`^(${prefixKeys})(?::|\\s|$)`);

const SPECIAL_MODE_CHARS = new Set(">@%+#!/");

function parseQuery(value) {
  const text = (value || "").trim();
  const lower = text.toLowerCase();

  // 1. Check for named prefixes (clip, ai, file, calc, resume/project)
  const prefixMatch = PREFIX_REGEX.exec(lower);
  if (prefixMatch) {
    const mode = PREFIX_TO_MODE[prefixMatch[1]];
    const remainingText = text.substring(prefixMatch[0].length).trim();
    return { mode, text: remainingText };
  }

  // 2. Check for equals sign prefix
  if (text.startsWith("=")) {
    return { mode: "=", text: text.substring(1).trim() };
  }

  // 3. Check for special character prefixes
  const firstChar = text[0];
  if (text.length > 0 && SPECIAL_MODE_CHARS.has(firstChar)) {
    return { mode: firstChar, text: text.substring(1).trim() };
  }

  // 4. Default fallback
  return { mode: "", text };
}

function score(row, needle) {
  if (!needle) return 1;
  let title = (row.title + " " + row.subtitle + " " + (row.keywords || "")).toLowerCase();
  let value = needle.toLowerCase();
  if (title === value) return 1000;
  let substringAt = title.indexOf(value);
  // Keep a real substring match useful even when it occurs late in a
  // long clipboard/history message.  Fuzzy matches should not outrank
  // that direct match merely because 500 - index became negative.
  if (substringAt >= 0) return 500 - Math.min(400, substringAt);
  let at = 0;
  let points = 0;
  for (let character of value) {
    at = title.indexOf(character, at);
    if (at < 0) return -1;
    points += 10;
    at++;
  }
  return points;
}
