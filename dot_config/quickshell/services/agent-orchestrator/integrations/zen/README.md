# Zen integration (honest fallback + opt-in explicit)

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

## Opt-in explicit URL (PID + opaque window id, no bundled publisher)

PID alone is insufficient with several Zen windows open. The explicit
record must bind **both** the opaque compositor window id and the client
PID:

Set `QS_ZEN_CONTEXT_FILE` to a private JSON file (`0600`, euid-owned,
`≤16 KiB`, no symlinks, `O_NOFOLLOW`-validated) your own helper keeps
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
- `url` must start with `http://` or `https://`, else ignored (title may
  still apply).
- stale records are confirmed mismatches (title fallback), not last-good.

Publisher contract (no bundled producer — this is a known limitation):
the helper must learn the focused window id + pid from the compositor
(e.g. `hyprctl activewindow -j`) in the same sampling tick as the URL it
attributes, write atomically (exclusive `0600` temp + rename), and refresh
at least every ~10 s. A helper that cannot observe per-window focus must
not publish: unattributed global URLs are worse than the title fallback.

Timing/freshness (no desktop polling): base persists first; enrichment is
async best-effort with focus-generation stale rejection, plus a 5 s
application-only re-request (no compositor query) and a bounded 60 s
last-good for the same focus. Result is an additive optional `resource`
(`adapter: zen` vs `zen-title`).

Security/setup: full URLs and titles are stored verbatim in the private
DB (no redaction/retention); file contents never read. Prerequisites are
a locked release build, a Hyprland session, and the manually maintained
file above. Phase 3 note: future search/session consumers must treat the
URL as best-effort/stale-tolerant and re-validate; no automatic
install/start.

No browser extension is required; do not build one for this.
