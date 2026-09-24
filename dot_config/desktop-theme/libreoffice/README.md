# LibreOffice — Rich Black desktop theme

Makes LibreOffice usable with the Rich Black theme: **white document page
(WYSIWYG) + dark chrome + readable text + dark monochrome icons**, with chrome
colors drawn from `quickshell/theme/Theme.qml` (read-only source of truth).

## Files

- `rich-black.settings.json` — declarative manifest (LO keys + `RICH_BLACK`
  color-scheme entries referencing Theme.qml tokens). Human-editable.
- `apply.py` — idempotent applier. Parses Theme.qml tokens, fuses the manifest
  into `~/.config/libreoffice/4/user/registrymodifications.xcu` with
  `oor:op="fuse"` semantics, rewriting only targeted `<item>` entries and
  leaving everything else byte-identical. Before writing it rotates the
  previous backup `.bak` → `.bak.1` (one prior generation kept), then backs
  up to `registrymodifications.xcu.bak`; prints every change.

Run: `python3 apply.py` (repeat safely; `--dry-run` to preview). Re-running
reports "everything already up to date" even after LibreOffice itself has
rewritten the file, because the scheme node is compared semantically (entry
colors/visibility only — entry order, prop order and `oor:op` fuse/replace
differences are ignored).

## Keys set (all confirmed in `/usr/lib/libreoffice/share/registry/main.xcd`)

| Path | Prop | Value | Schema source |
|---|---|---|---|
| `/org.openoffice.Office.Common/Accessibility` | `IsAutomaticFontColor` (xs:boolean) | `true` (was `false`) | main.xcd Accessibility group: "Always use automatic font color for screen display" |
| `/org.openoffice.Office.Common/Misc` | `SymbolStyle` (xs:string) | `sifr_dark` (was `colibre`) | main.xcd: `"auto"`/default/theme name |
| `/org.openoffice.Office.Common/Appearance` | `LibreOfficeTheme` (xs:short) | `1` (was `0`) | **Theme-engine switch — the brown-chrome fix.** Enum per `include/vcl/themecolors.hxx` `ThemeState`: 0=DISABLED, 1=ENABLED, 2=RESET. At 0, `ColorConfig_Impl::SetupTheme()` (`svtools/source/config/colorcfg.cxx`) returns early, the registry scheme is never cached into `ThemeColors`, and `VclPluginCanUseThemeColors()` is false — so the gtk3 backend ignores every `ColorScheme` chrome entry and paints toolbars/menus/popups from system/GTK colors (brown, translucent). At 1 the `RICH_BLACK` chrome loads and menus paint opaque. `ApplicationAppearance` stays `2` (Dark). |
| `/org.openoffice.Office.Common/Appearance` | `ApplicationAppearance` (xs:short) | `2` (kept) | main.xcd "Application Colors"; enum 0=System/Automatic, 1=Light, 2=Dark (Tools ▸ Options ▸ Application Colors ▸ Appearance); 2 observed dark |
| `/org.openoffice.Office.Common/Appearance` | `UseOnlyWhiteDocBackground` (xs:boolean) | `true` (kept) | main.xcd: "Use white document background", default true |
| `/org.openoffice.Office.UI/ColorScheme` | `CurrentColorScheme` (xs:string) | `RICH_BLACK` (was `..._DARK`) | main.xcd UI group ColorScheme |

## Icon theme: `sifr_dark`

`colibre` ships dark glyphs (invisible on dark bars). `sifr_dark` is the same
monochrome symbolic style with near-white glyphs — verified by unzipping:
`sifr_dark/.../save.svg` uses `fill="#efefef"`, `sifr/.../save.svg` uses
`fill="#2e3436"`. Installed at
`/usr/lib/libreoffice/share/config/images_sifr_dark(_svg).zip`
(alongside `breeze_dark`, `colibre_dark`, `sukapura_dark` alternatives).
Monochrome symbolic fits the theme best, hence Sifr over Breeze/Colibre.

## Custom scheme `RICH_BLACK` — achieved

93 entries under `/org.openoffice.Office.UI/ColorScheme/ColorSchemes`
covering the full `ColorScheme` template from main.xcd (every `Doc*`,
`Writer*`, `Calc*`, `Author1-9`, `BASIC*`, `SQL*`, `Window*`, `Menu*`,
etc.; `Color` stored as decimal `xs:int` **0xRRGGBB**, plus `IsVisible` where the
template declares it). Token map highlights: `AppBackground`/`BaseColor`=bg
#070707, `FaceColor`/`WindowColor`=base #0C0C0C,
`MenuColor`/`ButtonColor`/`FieldColor`=mantle #111111, text entries=text
#F5F5F5, selection/highlight=accent #D6D6D6 with highlight-text=bg #070707
(dark-on-light, readable), links=blue #AAB3BD, visited=mauve #C8C8C8,
spell=red, grammar=green, `DocColor`=white #FFFFFF (WYSIWYG page).

Color encoding (verified, NOT byte-swapped): LibreOffice `tools::Color`
stores `mValue = B | G<<8 | R<<16 (| T<<24)` and serializes to the config
layer as `sal_Int32(mValue)` — i.e. config decimals are plain 0xRRGGBB
(proof: `include/tools/color.hxx`,
`static_assert(sal_uInt32(Color(0x12,0x34,0x56)) == 0x00123456)`; the old
`RGB_COLORDATA` R|G<<8|B<<16 packing is a Win32-COLORREF-era macro and does
not apply to config serialization). So `apply.py` stores `int(RRGGBB, 16)`
directly, asserts the round-trip for every entry, and prints hue samples
(`Links #AAB3BD → 11187133 → #AAB3BD`). Chrome entries are all grays
(R=G=B) and therefore encoding-immune anyway — the brown chrome was never a
byte-order issue (see `LibreOfficeTheme` above).

Alpha policy: Theme.qml `#AARRGGBB` tokens keep their alpha; `xs:int` colors
are opaque-only, so a non-`FF` alpha is a hard error instead of a silently
corrupted color. No manifest entry uses translucent tokens today.

Storage-format lesson (LibreOffice 26.8, verified with headless runs): the
scheme MUST be one nested-node
`<item path=".../ColorSchemes"><node name="RICH_BLACK" fuse>…` (the way LO
itself persists set members, cf. Histories ItemList entries). 93 separate
per-entry `<item>`s are silently dropped by LO on startup (leaving a dangling
`CurrentColorScheme`). The nested form survives with all values intact — LO
only reorders entries and flips `oor:op` fuse→replace on save, which the
semantic compare in `apply.py` tolerates.

Failure signature to watch: if LO ever stops recognizing the scheme node,
`ColorConfig_Impl::GetCurrentSchemeName()` silently resets
`CurrentColorScheme` to `COLOR_SCHEME_LIBREOFFICE_AUTOMATIC` on next launch.
So "CCS still reads RICH_BLACK after a run" = scheme resolves; if it ever
flips to AUTOMATIC, the node form needs revisiting (reliable fallback would
be `CurrentColorScheme=COLOR_SCHEME_LIBREOFFICE_DARK`, the known-good gray
baseline — not needed today).

Transparency note: `xs:int` colors carry no alpha channel, so every scheme
background (`MenuColor`, `MenuBorderColor`, `FaceColor`, …) is opaque by
construction. The translucent burger-menu/deck panels were the same
`LibreOfficeTheme=0` fallback (GTK-painted popups), fixed by the same switch.

## Verification

- `apply.py` re-run → "everything already up to date", including after LO's
  own rewrites (headless + GUI runs).
- XCU valid XML; diff of user layer vs backup: only intended changes, nothing
  removed (LO appends its own unrelated PickList/thumbnail churn on GUI runs).
- `soffice --version` → `LibreOffice 26.8.0.3 680(Build:3)`, exit 0.
- `soffice --headless --convert-to pdf` succeeds with the config live, and the
  `RICH_BLACK` scheme + all keys survive the run (`CurrentColorScheme` still
  `RICH_BLACK`, i.e. LO resolves the custom scheme instead of resetting to
  AUTOMATIC).
- Live GUI check (Impress, `LibreOfficeTheme=1`): template chooser, repair
  dialog, and full editor all render dark-gray chrome — sampled pixels:
  workspace `#080808` (≈ bg #070707), menubar `#1B1B1B`, ribbon `#0A0A0A`,
  sidebar deck `#141414`/`#101010`, status bar near-black; warm-spread ≤ 2
  everywhere (zero brown). Slide page pure `#FFFFFF`. Sidebar/deck panels
  fully opaque; `sifr_dark` icons render near-white and readable.
