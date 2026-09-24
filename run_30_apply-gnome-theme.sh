#!/bin/sh
# Apply GNOME gsettings (skip cleanly on non-GNOME desktops).
set -eu
command -v gsettings >/dev/null 2>&1 || exit 0
cfg="${XDG_CONFIG_HOME:-$HOME/.config}"
exec sh "$cfg/desktop-theme/apply-gnome.sh"
