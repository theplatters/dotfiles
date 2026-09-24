# Zen integration (honest fallback + automatic WebExtension)

Zen exposes no default remote tab API, so the collector is honest by
default and never invents a URL. Nothing auto-installs or auto-starts;
both modes are collect + local query only (no QML/search wiring yet).

## Default: title fallback (no config)

When the focused app is Zen, the collector stores:

```json
{"adapter": "zen-title", "title": "<focused window title>"}
```

`url` stays `null`. The adapter label makes the fallback explicit for
reviewers; title changes already classify as `title`, resource-only
explicit-URL changes classify as `context`.

Do **not** point the collector at Zen session databases to guess the
focused tab — that would be unreliable attribution and is unsupported.

## Opt-in explicit URL (PID + opaque window id, automatic publisher)

PID alone is insufficient with several Zen windows open. The explicit
record must bind **both** the opaque compositor window id and the client
PID:

Set `QS_ZEN_CONTEXT_FILE` to a private JSON file (`0600`, euid-owned,
`≤16 KiB`, no symlinks, `O_NOFOLLOW`-validated) the native host keeps
fresh (`updated_at_ms` wall-clock epoch millis, `≤30 s`, or fresh mtime
when the field is absent):

```json
{"window_id": "0xabc123", "pid": 1234, "url": "https://example.com/page", "title": "Example", "updated_at_ms": 1710000000000}
```

- `window_id` must equal the focused Hyprland window address **and**
  `pid` must equal the focused client PID, else the record is ignored
  (a readable record for another window — or stale, or URL/title both
  missing — is a confirmed mismatch and yields the title fallback; a
  missing/unreadable file is transient and serves the bounded last-good
  entry instead of oscillating).
- A readable `{}` is an explicit eviction tombstone (confirmed mismatch):
  the collector drops any last-good entry for that focus. Deleting the
  file is transient (serves bounded last-good) — prefer writing `{}` to
  clear immediately.
- `url` must start with `http://` or `https://`, else ignored (title may
  still apply).
- stale records are confirmed mismatches (title fallback), not last-good.

## Automatic publisher: WebExtension + native host (recommended)

```
Zen extension (extension/) --nativeMessaging--> host (qs-zen-native-host.py)
  per-window title marker                       Hyprland same-tick check
  tabs/windows reads only                       QS_ZEN_CONTEXT_FILE (0600, atomic)
```

- **Extension** (`extension/`, MV2 persistent background, `tabs` +
  `nativeMessaging` only, id `qs-zen-context@quickshell.local`): assigns
  each non-private window a random per-window marker `[qs-zen-<8 hex>] `
  via `browser.windows.update({titlePreface})`, then publishes the active
  tab of the last-focused window on tab activation/update, window
  focus/removal, tab removal, plus a 10 s heartbeat. Requests are
  serialized and coalesced with a generation counter re-checked after
  every `await`, and live focus is re-queried before sending, so an older
  async tab result is dropped best-effort instead of overwriting newer
  focus state (sampled checks, not a race proof). Private
  windows/tabs and non-`http(s)` URLs post `{type:"invalidate"}` instead
  of a URL; focus loss (`WINDOW_ID_NONE`) posts invalidate the same way.
  On port disconnect the host sees EOF and clears on its own.
- **Host** (`qs-zen-native-host.py`, stdlib only, runnable): stdio native
  host `local.quickshell.zen_context`. For each `{type:"update"}`
  message it validates the schema (`marker` shape, `http(s)` URL
  `≤2048`, title `≤1024`, `updatedAtMs` `≤30 s` old and `≤5 s` future —
  delayed records are rejected), samples `hyprctl activewindow -j`
  BEFORE and AFTER handling (list-form argv, 2 s timeout, 256 KiB cap),
  and writes **only** when both samples agree on address/pid/title AND
  the focused class is Zen AND the focused title starts with the exact
  message marker. Anything else (malformed, unsupported, private,
  class/marker mismatch, focus race, sampler failure) atomically writes
  `{}`. Stdin EOF clears to `{}` and exits 0. Native frames are
  `<u32LE length><UTF-8 JSON>`, bounded (`64 KiB`); a complete frame
  with malformed JSON invalidates to `{}` and stays connected, while an
  overlong length or truncated body invalidates to `{}` and closes
  (stream cannot be resynchronised). Diagnostics go to
  stderr **without** URLs, titles, or markers; stdout carries frames
  only. Parent/file checks mirror the collector (private `0700` parent,
  no symlinks, exclusive `0600` temp + rename).
- **Host manifest** (`local.quickshell.zen_context.json`) is the
  checked-in template: `{"name": "local.quickshell.zen_context", ...,
  "allowed_extensions": ["qs-zen-context@quickshell.local"]}`. The
  `path` field must be an absolute path to the host executable on the
  target machine — it is intentionally left as a placeholder here.
  `install.py` renders and installs the per-user manifest (see below);
  do not hand-edit the template into place.

### Multi-window mapping (why title markers)

The extension cannot observe Hyprland window ids, and the host cannot
observe browser window ids, so the two sides rendezvous on a random
per-window title marker. Each Zen window gets its own marker; the host
accepts a message only when the *focused compositor* title starts with
that message's marker. With two Zen windows open, only the focused
window's marker matches — the other window's URL is rejected best-effort instead of leaking into the
focused record. No page-title heuristics are used: the marker prefix is
the rendezvous proof, and the before/after resample narrows the focus-race window best-effort (a tab query that spans a focus switch is dropped on sampled checks, not proven race-free).

### Visible title-marker tradeoff

`titlePreface` is a visible prefix: every Zen window title becomes
`[qs-zen-a1b2c3d4] <original title>` in the compositor (window list,
`hyprctl` output, and the stored `zen-title` fallback). The marker is
random per window per browser session (not derived from content, not
stable across restarts) and carries no browsing data itself, but it is
visible to anyone who can see window titles, and it perturbs exact title
matching (the collector stores the prefixed title verbatim). This is the
documented cost of fail-closed multi-window attribution without a
browser-provided OS-window id. If the prefix is unacceptable, do not
install the extension and keep the honest `zen-title` fallback (or the
manual `push`/`capture` flow below, which adds no prefix).

Until the marker propagates to the compositor title, the host fails
closed to `{}` — expect a brief `zen-title` period after browser start
or window creation before the first `zen` resource appears.

### Install / signing (live signed install, Flatpak)

Prerequisites: Zen (Firefox-compatible; manifest requires Gecko 115+),
Hyprland with `hyprctl`, Python 3.11+ (host, stdlib only), the release
collector build. `extension/` behavior is covered by mocked-`browser`
`node` tests, and the live signed build below is verified installed.

Live state (this machine): extension `0.1.1` approved private/unlisted
on AMO and manually installed from the AMO-signed XPI into the Flatpak
Zen app-profile at
`~/.var/app/app.zen_browser.zen/.zen/m7glhe53.Default (alpha)/extensions/qs-zen-context@quickshell.local.xpi`
(size `12556`, sha256
`0727756a68d4a5480f2032697d9d83fac22a1e8a9a7eb71f90892ded44bb8465`).
Source URI is the AMO download
(`addons.mozilla.org`, file `5044671`); see the unlisted developer
versions page on AMO (no direct link here). The installed
`background.js` is identical to the current `extension/background.js`;
the installed manifest is AMO-normalized but semantically the same
(`data_collection_permissions.required: ["none"]`,
`strict_min_version` 115+, permissions `tabs` + `nativeMessaging`,
id `qs-zen-context@quickshell.local`). An `extensions.json`
`active:false visible:false` entry is a stale snapshot, not definitive —
live proof is the running bridge, the visible `[qs-zen-<8 hex>]` title
marker, the fresh bound record, and collector rows with `adapter zen`
and URL.

Native bridge (running): `--mode flatpak` manifest + wrapper are
registered (`~/.mozilla/native-messaging-hosts/local.quickshell.zen_context.json`
→ wrapper `~/.local/share/quickshell/zen-native-host/qs-zen-native-host-flatpak.sh`
→ `flatpak-spawn --host python3 <host> "$@"` with manifest/wrapper
args). The `QS_ZEN_CONTEXT_FILE` record is fresh and bound
(window id + PID + marker), and collector latest rows show `adapter zen`
with URL.

Flatpak grants (applied, Zen restarted to pick them up):

```sh
flatpak override --user --talk-name=org.freedesktop.Flatpak \
  --filesystem=$HOME/.local/share/quickshell/zen-native-host:ro \
  --filesystem=$HOME/.mozilla/native-messaging-hosts:ro \
  app.zen_browser.zen
```

`--talk-name=org.freedesktop.Flatpak` is broad host-command execution;
the filesystem grants are narrow read-only (wrapper dir + manifest dir)
so the sandbox sees the manifest and resolves the wrapper at the same
absolute path. Installer `install.py` never executes this override —
it only prints it for review.

Installer `install.py` (`--mode native` | `--mode flatpak`, default is a
dry-run plan, `--install` writes) only packages/copies/registers the
native host — it does NOT install the extension into Zen (Zen
installation was the manual AMO-signed XPI above). By default it
packages `extension/` into a deterministic unsigned `.xpi` and registers
the per-user native host. The two modes share one manifest filename
(`~/.mozilla/native-messaging-hosts/local.quickshell.zen_context.json`),
so only one mode may be installed at a time. For the signed build it
copies an externally signed XPI to the local `--xpi` destination for
records/inspection only (not into the profile):

```sh
# Register the native bridge and copy an already signed XPI to a separate
# local artifact for inspection. This does not install into Zen and does not
# overwrite the existing unsigned ~/Downloads/qs-zen-context.xpi:
python3 services/agent-orchestrator/integrations/zen/install.py --install --mode flatpak --signed-xpi "$HOME/Downloads/qs-zen-context-signed.xpi" --xpi /tmp/opencode/qs-zen-context-signed-copy.xpi
# Optional AMO unlisted signing from source (operator credentials at runtime only):
#   AMO_JWT_ISSUER=... AMO_JWT_SECRET=... python3 .../install.py --install --sign-amo --xpi /tmp/opencode/qs-zen-context.xpi
#   (`--sign-amo` runs `npx web-ext sign --channel unlisted`; `--sign-amo` and `--signed-xpi` are mutually exclusive.)
```

Future updates: bump `version` in `extension/manifest.json`, repackage,
sign via AMO unlisted, then upload/install the new signed XPI in Zen via
`about:addons` → Install Add-on From File — running the installer alone
does not update Zen. Any `extension/` source
change requires a new signed version — the installed signed XPI does
not track the working tree.

Unsigned temporary load (`about:debugging#/runtime/this-firefox` →
Load Temporary Add-on) is dev-only and not the current state; do **not**
use it for the signed build, and never disable signature enforcement.

Opt the collector in (same session that runs the collector):
`export QS_ZEN_CONTEXT_FILE="${XDG_STATE_HOME:-$HOME/.local/state}/quickshell/desktop-activity/zen-context.json"`
(then start the collector / use `current` as usual). The record arrives
via the native host file; nothing here writes the collector DB directly.
Focus Zen and switch tabs: the record appears within ~10 s
(heartbeat) and evicts to `{}` when you focus a private window,
an about: page, or leave Zen.

Fail-closed notes: quitting the browser (or disabling the extension)
disconnects the port — the host sees EOF and clears to `{}`. More than
one Zen window is separated best-effort by distinct markers (sampled
checks, not proven race-free); closing the
focused window re-publishes the newly focused one on its focus event.

## Manual fallback: `scripts/zen_context.py` push/capture (legacy, no prefix)

Legacy manual publisher (`scripts/zen_context.py`, not scheduled): explicit
pushes and captures (same-tick `hyprctl activewindow -j`
binding, atomic `0600` writes, no network, no title prefix):

```sh
python3 scripts/zen_context.py push --url "$(wl-paste)"   # Zen must be focused
python3 scripts/zen_context.py capture                    # Ctrl+L/Ctrl+C in Zen, fail closed
python3 scripts/zen_context.py status                     # inspect the record
python3 scripts/zen_context.py clear                      # back to title fallback
```

`push` refuses non-Zen focus, non-`http(s)` URLs, public/symlinked
targets, and malformed `hyprctl` output; `status` never writes.
`capture` verifies the focused Zen window, clears the clipboard, injects
Ctrl+L / Ctrl+C into that exact window (Lua `send_shortcut`, no shell
interpolation), dismisses the URL bar, and publishes the clipboard URL
only when the copy produced a fresh `http(s)` value — a failed copy
leaves the record untouched. The previous clipboard content is lost by
design (recoverable from cliphist). No keybind is provided (SUPER+Z was
removed live). The default path is
`$QS_ZEN_CONTEXT_FILE` (else
`${XDG_STATE_HOME:-$HOME/.local/state}/quickshell/desktop-activity/zen-context.json`), so the
helper and the collector agree without extra configuration. There is no
remote-debugging poller (rejected: it would need the debugger port open
and still could not bind windows safely). A one-shot capture binds only
while the record is fresh (30 s), so re-capture after switching tabs.

Timing/freshness (no desktop polling): base persists first; enrichment is
async best-effort with focus-generation stale rejection, plus a 5 s
application-only re-request (no compositor query) and a bounded 60 s
last-good for the same focus. Result is an additive optional `resource`
(`adapter: zen` vs `zen-title`).

Security/setup: full URLs and titles are stored verbatim in the private
DB (no redaction/retention); the record arrives via the native-host file
and nothing here writes the DB directly. The extension +
host add no new network, clipboard, key-injection, or profile-database
reads (the legacy manual `capture` flow is the only clipboard/key path,
and it is opt-in per invocation). Prerequisites are a locked release build, a
Hyprland session, and the native-host-maintained file above. Phase 3 note:
future search/session consumers must treat the URL as
best-effort/stale-tolerant and re-validate; no automatic install/start.

## Tests (mocked, never live)

- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_zen_native_host tests.test_zen_extension tests.test_zen_install -v`
  covers installer packaging/manifest/wrapper planning (dry-run safe,
  never executes `flatpak override`) plus native framing (round-trip,
  split reads, overlong/truncated invalidate-and-close vs complete-frame
  malformed invalidate-and-continue),
  private parent/file checks, schema/URL/timestamp validation,
  multi-window marker isolation, before/after focus-race rejection,
  privacy (URLs never in stderr/acks), EOF clearing, and the extension
  manifest contract plus mocked-`browser` behavior via `node` (marker
  shape, `titlePreface` assignment, update vs invalidate routing, stale
  generation and focus-race drops) — all with fixture samplers and mocked
  `browser` APIs, never a live Zen/Hyprland. Node-gated behavior tests
  skip cleanly when `node` is absent.
- `cargo test --locked --manifest-path services/agent-orchestrator/Cargo.toml`
  covers the unchanged collector contract (`read_zen_explicit` binding,
  freshness, `{}`-evicts/deletion-serves).
