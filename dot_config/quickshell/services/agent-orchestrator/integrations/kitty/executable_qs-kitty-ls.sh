#!/bin/sh
# qs-kitty-ls.sh: manual validation helper for the kitty integration.
# Prints the structured `kitty @ ls` JSON the collector parses. No parsing
# here does correlation; the Rust collector verifies instance identity via
# the socket peer PID (SO_PEERCRED == focused compositor PID), then selects
# the unique focused OS window/tab/pane (no positional guesses). Pane cwd
# prefers the unanimous foreground-process cwd, else the focused pane cwd.
# Neovim wins only as the unique foreground nvim with a fresh correlated
# record.
#
# Usage (concrete per-instance socket; read it from inside kitty via
# `echo $KITTY_LISTEN_ON` after restarting with the config below):
#   QS_KITTY_SOCKET=/run/user/1000/kitty-12345.sock ./qs-kitty-ls.sh
#   KITTY_LISTEN_ON=unix:/run/user/1000/kitty-12345.sock ./qs-kitty-ls.sh
#
# kitty.conf (opt-in, least privilege — socket-only, never `yes`):
#   allow_remote_control socket-only
#   listen_on unix:${XDG_RUNTIME_DIR}/kitty-{kitty_pid}.sock
#
# Kitty expands ${VAR} itself and replaces the exact {kitty_pid}
# placeholder with its own PID (any other suffix gets -<pid> appended
# automatically). Changing listen_on needs a full restart, not a reload.
# Keep the socket under a private runtime parent (0700, euid-owned);
# identity comes from SO_PEERCRED, not the file name.
#
# The collector reads QS_KITTY_SOCKET (bare path) first, then KITTY_LISTEN_ON
# (unix: address); both are normalized to the `unix:` address form kitty
# `--to` requires. Without either, the kitty provider is disabled and the
# base collector is unaffected. Focused-PID correlation requires the
# Hyprland `pid` field; without it kitty enrichment stays off (never a
# guess). Timeouts/output are bounded in Rust (1.5 s, 256 KiB); this script
# adds its own 2 s timeout when available.
set -u
sock="${QS_KITTY_SOCKET:-}"
if [ -z "$sock" ] && [ -n "${KITTY_LISTEN_ON:-}" ]; then
  sock="$KITTY_LISTEN_ON"
fi
if [ -z "$sock" ]; then
  echo "qs-kitty-ls: set QS_KITTY_SOCKET (or KITTY_LISTEN_ON) to the kitty remote-control socket" >&2
  exit 2
fi
# Normalize to the unix: address form kitty --to requires.
case "$sock" in
  unix:*) to="$sock" ;;
  /*) to="unix:$sock" ;;
  *)
    echo "qs-kitty-ls: socket must be an absolute path (got ${sock%%:*})" >&2
    exit 2
    ;;
esac
if command -v timeout >/dev/null 2>&1; then
  exec timeout 2 kitty @ --to "$to" ls
else
  exec kitty @ --to "$to" ls
fi
