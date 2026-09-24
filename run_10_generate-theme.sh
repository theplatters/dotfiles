#!/bin/sh
# Regenerate the Rich Black generated theme files from Theme.qml (idempotent).
set -eu
cfg="${XDG_CONFIG_HOME:-$HOME/.config}"
exec python3 "$cfg/desktop-theme/generate.py" render
