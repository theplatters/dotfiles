# Zotero active-reader integration (verified bridge, metadata only)

Nothing here auto-installs, auto-builds, or auto-starts. Collection is local
only (collector + private file + local Zotero APIs); there is no QML/search
wiring beyond the generic `resource`/`project` fields the store already
indexes.

## Architecture (why two pieces)

```
Zotero plugin (bootstrap.js)          bridge (qs-zotero-bridge.py)         collector
GET 127.0.0.1:23119/qs-active-reader  hyprctl activewindow -j + clients -j  QS_ZOTERO_CONTEXT_FILE
Zotero-INTERNAL reader only    +      (same tick, single-window invariant)  exact match + 30 s freshness
(process-global, no window id)        opaque window_id + pid binding   →   60 s last-good, mismatch evicts
```

- The **plugin** runs inside Zotero and answers `GET /qs-active-reader` with
  the Zotero-internal active reader: `server_id`, `library_type`/`library_id`,
  `item_key`/`attachment_key`, `title`, current `collections` +
  `ancestor_collections`, `version`, stable `zotero_uri`, plus `state`
  (`open`/`closed`), `readers_open`, `served_at_ms`, `seq`. It handles tab
  switches (reader focus events), multiple/detached windows (all open readers
  counted, the focused one reported), and reader closure (`state:"closed"`).
  Only metadata is captured; page numbers, annotations, full text, and file
  bytes are never returned.
- The **bridge** supplies the verified compositor binding the plugin cannot
  observe (see "Fail-closed native mapping" below) under a fail-closed
  **single Zotero window invariant with same-tick active window sampling**.
  In the same tick it reads `hyprctl activewindow -j`, `hyprctl clients -j`,
  and the plugin endpoint, then maps the process-global active reader to the
  focused window **only when exactly one client whose class contains
  "zotero" (case-insensitive) exists AND the active window address matches
  that single client address**. Zero or 2+ Zotero clients, a failed/malformed
  clients query, or an address mismatch never writes an open document record:
  a focused Zotero window gets an explicit tombstone
  (`{window_id, pid, updated_at_ms, state:"closed"}`) for immediate eviction,
  while a non-Zotero focus leaves the file untouched (`skip`). Closed/
  unreadable/invalid readers on the single-window path likewise become
  explicit tombstones so the collector evicts instead of serving the previous
  document.
- The **collector** (`QS_ZOTERO_CONTEXT_FILE`, app `zotero`) binds ONLY on an
  exact opaque `window_id` PLUS `pid` match with 30 s freshness (explicit
  `updated_at_ms`, else descriptor mtime + 5 s). Missing/unreadable files are
  transient (bounded 60 s last-good for the same focus); readable mismatches,
  stale records, and closed tombstones are confirmed mismatches that evict.
  Unconfigured Zotero windows carry **no resource** — there is deliberately
  no title-based attribution (a window title cannot prove the active tab).

## Fail-closed native mapping (proven from source)

- **Local library API** (`http://localhost:23119/api/`, Zotero docs "Local
  API"): serves items/collections/versions from the local database, requires
  the Advanced preference opt-in (else `403`), reports `Zotero-Server-ID` on
  Zotero 10+ (earlier servers have no stable id and never match a pinned
  registry entry). It exposes **library data only — no focus, no active tab,
  no reader state, no window handle**. Polling it cannot attribute a reader.
- **In-process JS API** (Zotero docs "JavaScript API", `zotero/zotero`
  `chrome/content/zotero` incl. `zoteroPane.js`, `zotero/reader`): reader state
  lives in `Zotero.Reader` (tab/window manager, `registerEventListener`
  `open`/`close`/`focus`), library selection in
  `Zotero.getActiveZoteroPane().getSelectedItems()`, objects in
  `Zotero.Items`/`Zotero.Collections`. The documented surface exposes
  **no native OS window handle** (no `nsIBaseWindow` native id, no Hyprland
  address/PID mapping). Zotero 7 docs additionally note only one main window
  is officially supported, so detached readers/tabs must be handled as
  in-process tabs — exactly what the plugin reports.
- **Consequence**: the plugin cannot fabricate `window_id`/`pid`, and the
  collector must never attribute a process-global "last active reader" as if
  it were per-window correlation. The minimal robust route is the bridge
  above: the plugin returns process-global Zotero-internal state per tick,
  and the helper maps it to compositor focus only under the single Zotero
  window invariant with same-tick active window sampling (exactly one Zotero
  client whose address matches the active window). If the bridge is not
  running, more than one Zotero window exists, or focus is not the single
  Zotero window, no open document is recorded and the collector fails closed.
  This limitation is explicit and documented — not retried with heuristics.

## Backend: local API (localhost:23119/api, Zotero 10+ semantics)

- Base URL `http://localhost:23119/api/` (docs "Local API"); enable in
  Settings → Advanced → "Allow other applications on this computer to
  communicate with Zotero". The plugin itself needs no API key (in-process);
  any out-of-process enrichment that later calls `/api/` directly uses
  unauthenticated reads (never forwarded externally) and reads
  `Zotero-Server-ID` / `Zotero-API-Version` response headers to qualify
  identity (`server_id` + `library_type`/`library_id`) and discard cached
  versions on `412`. Writes are out of scope (Zotero 10+ local keys not used).
- Membership/ancestors for project matching come from the plugin snapshot
  (current `item.getCollections()` + parent-collection walk at request time),
  so hierarchy changes apply immediately with no extra `/api/` round trip.
  The collector never reads `zotero.sqlite` directly, never uses cloud sync,
  and never extracts content.

## Install / package (manual)

Prerequisites: Zotero 7.0+ desktop (verified against Zotero 10.0.2; the
manifest caps the tested line with `strict_max_version 10.0.*`), Hyprland with
`hyprctl`, Python 3.11+ (bridge, stdlib only), the release collector build.

```sh
# 1. Package the plugin (from this directory):
rm -f qs-active-reader-0.1.0.xpi
zip -r qs-active-reader-0.1.0.xpi manifest.json bootstrap.js
#    Zotero 10 requires applications.zotero.{id,update_url,strict_max_version};
#    an XPI missing update_url/strict_max_version is rejected at install
#    ("may not be compatible"/manifest error). Keep
#    tests/test_zotero_plugin_manifest.py green after manifest edits.
# 2. In Zotero: Tools → Plugins → gear → Install Add-on From File → pick the .xpi.
#    Restart Zotero when prompted.
# 3. Enable the local server: Settings → Advanced →
#    "Allow other applications on this computer to communicate with Zotero".
# 4. Verify the endpoint (Zotero running, a PDF/EPUB open):
curl -s http://127.0.0.1:23119/qs-active-reader | python3 -m json.tool
# 5. Run the bridge (same session that runs the collector):
export QS_ZOTERO_CONTEXT_FILE="$XDG_RUNTIME_DIR/qs-zotero-context.json"
python3 services/agent-orchestrator/integrations/zotero/qs-zotero-bridge.py --once
python3 services/agent-orchestrator/integrations/zotero/qs-zotero-bridge.py --loop --interval 5 &
# 6. Opt the collector in:
export QS_ZOTERO_CONTEXT_FILE="$XDG_RUNTIME_DIR/qs-zotero-context.json"
#    (then start the collector / use `current` as usual)
```

Version compatibility (verified against the shipped add-on manager of Zotero
10.0.2 and the documented Zotero plugin requirements):

- `applications.zotero` must contain `id`, `update_url`, **and**
  `strict_max_version`. Zotero 10 rejects an XPI whose manifest omits
  `update_url` or `strict_max_version` before installation
  (`applications.zotero.update_url not provided` /
  `... strict_max_version not provided` — surfaced as the install-time
  "may not be compatible"/invalid-extension message). `strict_min_version`
  must not contain `*`; Zotero packages no "unbounded max" value.
- `strict_min_version 7.0` is the lowest version the used APIs support
  (bootstrapped, no XUL overlay; version-dependent reader accessors are
  probed defensively and degrade to `state:"closed"`).
- `strict_max_version 10.0.*` is the tested Zotero line; bump it (and rebuild
  the XPI) when testing a newer Zotero.
- `update_url` points at `https://qs-active-reader.invalid/updates.json`, a
  guaranteed-non-resolving placeholder: the plugin is local/sideloaded with no
  update channel. Update checks against it simply fail with no external
  traffic; replace it with a hosted Mozilla-style `updates.json` if the plugin
  is ever published.
- Local-API `Server-ID` semantics require Zotero 10+ for pinned `server_id`
  matching. On Zotero 10 the plugin reads it in-process
  (`Zotero.Server.LocalAPI.getServerID()`): the local HTTP server now drops
  browser-looking requests (User-Agent starting with `Mozilla/` or any
  `Origin`) unless they send `Zotero-Allowed-Request`, so the guarded
  local-API header read survives only as a fallback for Zotero 7-9. Earlier
  servers report `server_id:""` and match nothing (fail closed, never a guess).

## Opt-in config

| Variable | Meaning |
|---|---|
| `QS_ZOTERO_CONTEXT_FILE` | Private JSON file the bridge keeps fresh (`0600`, euid-owned, `≤16 KiB`, no symlinks, `O_NOFOLLOW`). Unset = integration disabled (honest no-resource, never a base failure). |
| `QS_ZOTERO_ENDPOINT` (bridge only) | Plugin URL (default `http://127.0.0.1:23119/qs-active-reader`). |

Published file shape (verified binding + metadata only):

```json
{"window_id":"0xabc123","pid":1234,"updated_at_ms":1710000000000,
 "server_id":"sPMHtLD6HHBd","library_type":"user","library_id":"0",
 "item_key":"ABCD1234","attachment_key":"EFGH5678","title":"Paper title",
 "collections":["AAAAAAAA"],"ancestor_collections":["BBBBBBBB"],
 "version":42,"zotero_uri":"zotero://select/library/items/ABCD1234"}
```

Closed reader tombstone: `{"window_id":"…","pid":…,"updated_at_ms":…,
"state":"closed"}` (evicts; never serves last-good).

## Project attribution (`zotero_collection`)

Registry (`scripts/projects.py`, strict): optional `zotero_collection = {
server_id, library_type ("user"|"group"), library_id (digit string; user
`"0"` is the server-bound personal-library alias), collection_key (8
uppercase alnum), include_subcollections (bool, default true) }`, or omitted/
`null` when unlinked.

Matching (Rust `project_context`, before `file`): qualified
server+library must agree (alias rule above), then the entry's
`collection_key` must be in the reader's direct `collections` — or, when
`include_subcollections` is true, in `ancestor_collections` (descendants
included via the published ancestors). Exactly one distinct claiming project
wins (`matched_by:"zotero_collection"`); two distinct claimants are ambiguous
with **no file fallback**; zero claims fall through to existing
file/cwd/git matching. Titles never match.

History/session behavior: append-time attribution only (later registry edits
never rewrite rows); the session resource key is the stable document
(`portable:zotero:<server>:<type>/<lib>:item:<item>:att:<att>` — titles,
versions, collections, URIs, and page turns never affect it).

## Security / limits

- Private records only: `0600` euid-owned regular files, `O_NOFOLLOW` opens,
  descriptor mtime, bounded time/output/input. File *contents* beyond the
  published metadata are never read; URLs/titles/URIs are stored verbatim in
  the private DB (no redaction/retention controls).
- No direct `zotero.sqlite` reads, no cloud/sync polling, no title-based
  attribution, no content extraction (pages/annotations/full text/file bytes),
  no install/start of live integrations by the collector or tests.
- `stat`/`canonicalize` paths stay lazily bounded as elsewhere; the bridge
  polls at ≤25 s (default 5 s) and never queries the compositor beyond
  `activewindow` + `clients`.
- Known limitation: without the running bridge there is no verified binding
  and Zotero windows stay unattributed — by design (fail closed).
  Multi-window correctness is fail-closed: with zero or 2+ Zotero clients the
  bridge never writes an open document (focused Zotero gets a `closed`
  tombstone for immediate eviction, non-Zotero focus leaves the file
  untouched); tab switches/detaches/closures on the single-window path
  converge within one interval.

## Tests (mocked, never live)

`cargo test zotero` covers private/stale/mismatch files, multi-window
isolation, reader closure eviction, descendant inclusion/exclusion,
ambiguity (no fallback), server/library collisions (incl. the user/`"0"`
alias), stable document identity across title/version/page changes, history
append/search round-trips, and old-JSON compatibility — all with fixture
files and the mocked publisher shape, never a live Zotero/Hyprland.
