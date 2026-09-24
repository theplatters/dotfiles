#!/bin/sh
# Patch LibreOffice's live registrymodifications.xcu with the Rich Black scheme (idempotent).
set -eu
cfg="${XDG_CONFIG_HOME:-$HOME/.config}"
exec python3 "$cfg/desktop-theme/libreoffice/apply.py"
