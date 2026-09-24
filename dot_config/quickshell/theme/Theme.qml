pragma Singleton

import QtQuick

QtObject {
    // Rich-black hierarchy. Keep all surfaces here so modules do not invent
    // their own shades (and so the bar remains readable on OLED displays).
    //
    readonly property color transparent: "#07070700"
    readonly property color bg: "#070707"
    readonly property color base: "#0C0C0C"
    readonly property color mantle: "#111111"
    readonly property color surface0: "#161616"
    readonly property color surface1: "#1D1D1D"
    readonly property color surface2: "#262626"

    readonly property color text: "#F5F5F5"
    readonly property color subtext1: "#A1A1A1"
    readonly property color subtext0: "#6F6F6F"

    readonly property color border: "#1D1D1D"
    readonly property color focusBorder: "#33F5F5F5"
    readonly property color accent: "#D6D6D6"
    readonly property color accentMuted: "#969696"

    readonly property color scrim: "#B0070707"

    readonly property color red: "#C59D9D"
    readonly property color green: "#A8B6A4"
    readonly property color mauve: "#C8C8C8"
    readonly property color teal: "#A8B8B8"
    readonly property color pink: "#BDBDBD"
    readonly property color blue: "#AAB3BD"
    readonly property color base3: "#F5F5F5"

    readonly property string fontFamily: Qt.fontFamilies().indexOf("Inter") >= 0 ? "Inter" : "Noto Sans"
    readonly property string fallbackFontFamily: "Noto Sans"
    // Icon glyphs (audio/network/battery/media/etc.) live in the Unicode
    // Private Use Area and are only provided by Nerd Fonts. PUA codepoints
    // never fall back automatically, so icon Text items must set
    // font.family: Theme.iconFontFamily explicitly — installing the font
    // alone is not enough.
    readonly property string iconFontFamily: {
        var fams = Qt.fontFamilies();
        var prefs = ["Symbols Nerd Font", "JetBrainsMono Nerd Font", "BlexMono Nerd Font", "BlexMono Nerd Font Mono", "BlexMono Nerd Font Propo", "CaskaydiaCove Nerd Font", "FiraCode Nerd Font"];
        for (var i = 0; i < prefs.length; ++i) {
            if (fams.indexOf(prefs[i]) >= 0)
                return prefs[i];
        }
        return "Noto Sans";
    }
    readonly property int controlRadius: 12
    readonly property int chipRadius: 7
    readonly property int cardRadius: 16
    readonly property int largeRadius: 20
    // Shared control sizing. WidgetButton targets a 40-44px touch height;
    // WidgetIconButton keeps a 44px hit target with a centered 22px glyph
    // so tab, journal, panel, close, and history controls stay coherent.
    readonly property int controlMinHeight: 40
    readonly property int iconButtonSize: 44
    readonly property int iconSize: 22
    readonly property int iconSizeSmall: 18
    readonly property int motionFast: 140
    readonly property int motionPanel: 200
    readonly property int motionExit: 220
}
