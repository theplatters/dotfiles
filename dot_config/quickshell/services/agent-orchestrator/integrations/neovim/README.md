# Neovim integration (opt-in Lua publisher)

Opt-in publisher for actual `file`/`cwd` (plus collector-side git).
Nothing auto-installs or auto-starts: copy the Lua file and set the
flag below manually per Neovim config.

## Setup (manual prerequisites)

Locked release build plus a Hyprland session; then in `init.lua` (or
equivalent):

```lua
vim.g.qs_nvim_context_enable = true
-- optional: vim.g.qs_nvim_context_dir = "/run/user/1000/quickshell/nvim-context"
require("qs-context").setup()
```

Ensure `qs-context.lua` is on the runtime path (copy it to
`~/.config/nvim/lua/qs-context.lua` or point your plugin manager at
`services/agent-orchestrator/integrations/neovim`). Directory defaults
to `$XDG_RUNTIME_DIR/quickshell/nvim-context`, overridable via
`$QS_NVIM_CONTEXT_DIR` / `vim.g.qs_nvim_context_dir` (explicit dir wins).

## What it publishes

One atomic private (`0600`, exclusive create + rename) JSON file per
Neovim PID:

```text
$XDG_RUNTIME_DIR/quickshell/nvim-context/<pid>.json
# or $QS_NVIM_CONTEXT_DIR / vim.g.qs_nvim_context_dir when set
```

```json
{"pid": 4242, "kitty_window_id": 3, "file": "/home/user/notes/page.md", "cwd": "/home/user/notes", "updated_at_ms": 1710000000000}
```

Written on `BufEnter`, `BufWritePost`, `FocusGained`, `DirChanged`,
`VimEnter` plus a 10 s heartbeat; removed on `VimLeavePre`. Only paths
are published, never file contents. `file` is the actual buffer path,
but only for real local files: normal buffers (empty `buftype`) whose
name carries no URI scheme. Unnamed, help, terminal, nofile,
`fugitive://`, `term://`, `file://` and friends publish empty `file`
(relative paths resolved against `cwd` by the collector, which also
defensively rejects URI-like values); `kitty_window_id` comes from
`$KITTY_WINDOW_ID` (null
outside kitty). `updated_at_ms` is wall-clock epoch millis
(`gettimeofday`, not monotonic); records older than 30 s are ignored by
the collector (mtime fallback only when the field is absent).

Security: the directory must be euid-owned `0700` (no symlinks);
records are `<pid>.json` created exclusive `0600` plus atomic rename.
The collector re-validates directory and descriptor (owner, regular
file, no group/other bits, `O_NOFOLLOW`, 16 KiB bound, name==pid) and
ignores anything else. Full paths are stored verbatim in the private DB
(no redaction/retention) — use a throwaway DB if sensitive.

## Correlation (collector side)

- Standalone Neovim (`nvim`/`neovim` window class): the record PID must
  equal the focused Hyprland client PID. (Neovide and other GUI
  wrappers are not supported: no verified backend binding exists.)
- Neovim inside kitty: additionally requires kitty `ls` (peer-verified,
  unique focus) to show `nvim` as the **unique** foreground entry — any
  position in `foreground_processes` — with a fresh record for that PID
  (and matching kitty window id when both present). Background or
  ambiguous `nvim` never wins; the confident pane cwd is used instead.
- The collector resolves `file` against `cwd` for relative paths and runs
  bounded `git -C <dir> rev-parse` for root/branch (1 s each, 8 KiB):
  non-repos yield no git fields, detached HEAD yields
  `detached:<short-sha>`, worktrees resolve to their own root/branch,
  malicious/relative paths rejected; a transient branch miss is
  backfilled from last-good rather than persisted as a removal (see Rust
  docs).

Timing (no desktop polling): base persists first; enrichment is async
best-effort on a bounded worker with focus-generation stale rejection,
plus a 5 s application-only re-request (no compositor query) and a
bounded 60 s last-good for the same focus. Result is an additive
optional `resource` (`adapter: neovim`) with a `context` row only on
real change; repeats dedup. Phase 3 note: future search/session
consumers must treat file/cwd/git as best-effort/stale-tolerant and
re-validate before acting; no QML/search wiring exists yet.
