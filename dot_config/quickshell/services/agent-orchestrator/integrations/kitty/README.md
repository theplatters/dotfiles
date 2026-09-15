# Kitty integration (opt-in)

The collector never guesses: kitty enrichment attaches **only** after
instance identity plus unique focus are both established. Without it,
the base snapshot persists unenriched and the failure never breaks
collection.

Result shape (additive optional `resource`; same table, schema v1):
`{"adapter": "kitty", "cwd": "/abs/dir", "git_root": "…",
"git_branch": "…"}`, or `"neovim"` when Neovim-inside-kitty wins (see
neovim README). Adapter-only shells normalize to absent; a cwd/resource
change under unchanged focus persists as a `context` row, repeats dedup.

## Opt-in config (least privilege, manual; nothing auto-installs/starts)

`kitty.conf`:

```text
allow_remote_control socket-only
listen_on unix:${XDG_RUNTIME_DIR}/kitty-{kitty_pid}.sock
```

Kitty expands `$VAR`/`${VAR}` itself when loading the config, so
`${XDG_RUNTIME_DIR}` above resolves without shell help. `{kitty_pid}`
is a placeholder kitty replaces with its own PID — and only when that
exact placeholder is present does kitty skip auto-appending the PID;
any other suffix gets `-<pid>` appended automatically, so do not invent
your own PID suffix. Never use `allow_remote_control yes` (full control
incl. sending input). `socket-only` is a transport restriction — only
local-socket peers may use remote control at all — not a read-only
flag: anything that can connect can run remote commands (including our
read-only `ls`), so keep the socket private regardless. Do not place
the socket under a public parent.

Changing `listen_on` requires a full kitty restart, not a config
reload. After restarting, read the concrete address from inside that
instance (e.g. `echo $KITTY_LISTEN_ON`, typically
`unix:/run/user/1000/kitty-12345.sock` where `12345` is that kitty's
PID) and point the collector at the same socket:

```sh
export QS_KITTY_SOCKET="/run/user/1000/kitty-12345.sock"
# alternatively KITTY_LISTEN_ON is honored (unix: address form):
# export KITTY_LISTEN_ON="unix:/run/user/1000/kitty-12345.sock"
```

Both forms are normalized to the `unix:` address `kitty @ --to` requires.
Without either variable the kitty provider is disabled and the base
collector is unaffected. Enrichment additionally requires the Hyprland
`pid` field for the focused window; without a focused PID it stays off.
Prerequisites: locked release build, Hyprland session with
`XDG_RUNTIME_DIR`, kitty restarted with the above config.

## How correlation works

1. `kitty @ --to unix:$QS_KITTY_SOCKET ls` is run bounded (1.5 s,
   256 KiB, no shell, argv only).
2. Instance identity via `SO_PEERCRED` on the socket: the peer PID must
   equal the focused Hyprland client PID. Otherwise the `ls` output
   belongs to an unrelated kitty instance and is discarded. (Real
   `kitty ls` carries no OS-window pid; pane `pid` is the shell and
   foreground pids are the editors — numeric id==pid conflation is
   rejected outright.)
3. Within the bound instance, the OS window, tab, and pane must each be
   the unique `is_focused` entry; any ambiguity (zero or several) yields
   no resource.
4. Pane cwd: the unanimous foreground-process cwd when the group agrees
   on exactly one absolute path, else the confidently-focused pane `cwd`.
   Disagreeing group members are never picked first-wins. `/proc`
   lookups happen only through these correlated fields.
5. Neovim inside kitty takes precedence **only** as the unique
   foreground `nvim` (any position in `foreground_processes`) with a
   fresh (`≤30 s`) validated private record for that PID (and matching
   kitty window id when both present). Background or ambiguous `nvim`
   never wins; the pane cwd is used instead.
6. The collector attaches `git_root`/`git_branch` via bounded
   `git -C <dir> rev-parse` (1 s each, 8 KiB; non-repos yield no fields,
   detached HEAD yields `detached:<short-sha>`, worktrees resolve to
   their own root). A transient branch miss leaves the field unset for
   last-good fill rather than persisting a removal.

Timing/freshness (no desktop polling): base persists first on the event
thread; enrichment completes asynchronously on a bounded worker and
persists only when still on the same focus (focus-generation stale
rejection). The live base is additionally re-requested on a 5 s
application-only cadence (no compositor query). Transient `None` serves
the bounded last-good entry for the same focus (60 s, app + window id
+ client PID key) instead of oscillating; a present-but-unequal peer is
a confirmed mismatch that evicts instead of serving. A verified unique
foreground nvim with a missing record is transient only for that exact
pane + editor PID (recorded alongside the cached value, never
persisted) — a different pane or a new editor PID clears, evicts, and
falls back to the verified pane cwd instead of serving old; an
unresolvable or ambiguous pane focus is likewise a confirmed mismatch.
A changed or ambiguous foreground falls back to kitty cwd.
Slow/malformed/disconnected responses yield `None`.

Validate manually with `./qs-kitty-ls.sh` (pretty-print with `jq`).

Security: absolute paths stored verbatim in the private DB (no
redaction/retention); file contents never read. Phase 3 note: future
search/session consumers must treat `cwd`/git as best-effort and
re-validate before acting (5 s refresh, 30 s nvim freshness, 60 s
last-good); nothing here wires QML/search/session.
