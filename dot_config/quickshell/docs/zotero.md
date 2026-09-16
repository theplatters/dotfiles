# Zotero backend (`scripts/zotero.py`)

Project-scoped Zotero access through the **Zotero local API only**
(default `http://127.0.0.1:23119/api/`, Zotero 10+). There is no cloud
fallback: every request goes to a loopback address, reads need no key, and
writes need a local API key the user grants at runtime. The integration
worker (QML/TS/Rust/session helpers) owns all UI and orchestration; this
document is the exact helper contract it integrates against.

## Setup

1. In Zotero: Settings → Advanced → enable **“Allow other applications on
   this computer to communicate with Zotero”**. Without it every request
   fails with `403` and a hint.
2. Link a project to a collection by writing its `zotero_collection`
   registry association (see below), using the `server_id` reported by
   `collections` (or any live response header `Zotero-Server-ID`).
3. Authorize writes once (frontend approval surface first — Zotero shows a
   dialog naming the app):
   ```sh
   echo '{"appName":"quickshell"}' | python3 scripts/zotero.py authorize
   # {"server_id":"...","remember":true,"stored":true}  (the key is never printed)
   ```
   The granted key is stored mode-`0600` (`~/.cache/quickshell/zotero_keys.json`,
   partitioned by `server_id`). One-shot use can set `ZOTERO_API_KEY` instead.
4. PDF text needs `pdftotext` (poppler-utils) on `PATH`; without it
   `read-pdf` fails with a structured “install poppler-utils” error.

`ZOTERO_BASE_URL` overrides the base URL but must stay an `http` loopback
(`/api/`) URL; anything else is rejected.

## Registry link (`scripts/projects.py`)

Optional per-project `zotero_collection` object; absent/`null` (JSON) or
omitted (TOML inline table) means **unlinked**:

```toml
zotero_collection = { server_id = "sPMHtLD6HHBd", library_type = "user",
  library_id = "0", collection_key = "ABCDEFGH",
  include_subcollections = true }
```

- `server_id`: non-blank bounded string (1..256, no NUL/controls/whitespace),
  copied from the live `Zotero-Server-ID` header. It pins the database:
  `user/"0"` (the local personal library) is only meaningful together with
  it, and every response is checked against it (`412` = different database →
  re-link, discard cached versions).
- `library_type`: `user` | `group`. `library_id`: digit string 1..20 chars;
  `"0"` allowed only for `user`.
- `collection_key`: exactly 8 uppercase alphanumerics (`[A-Z0-9]{8}`).
- `include_subcollections`: strict bool, default `true`.
- Unknown (sub-)fields rejected; CRUD is full replacement — a missing/null
  `zotero_collection` on `update` unlinks. JSON reads unlinked as `null`.

## Helper CLI

```sh
python3 scripts/zotero.py [--projects-file PATH] [--base-url URL] <command>
```

Stdin: one JSON object. Stdout: one JSON object. Errors: exit nonzero,
`error: <message>` on stderr, empty stdout, no traceback, no secrets.
Unknown stdin keys are always rejected. Every data request sends an explicit
`limit` (the local API is unbounded by default) and makes a single HTTP
attempt — mutations are never retried automatically.

## Commands and exact schemas

`capabilities {}` → backend identity, `operations`, `resultSchemas`,
`operationSchemas`, `limits`, `deferred`, `auth` (see `... capabilities`
output; it is the machine-readable version of this section, including the
`libraries` schema and `groupsMax`).

`authorize {"appName"?: 1..100, default "quickshell"}` →
`{server_id, remember, stored}`. Denial → `error: ... denied ...`.
The key is never in outputs.

`collections {"library_type"?, "library_id"?}` (both or neither; default
`user`/`"0"`) →
`{server_id, library:{type,id}, collections:[{key,name,parentCollection|null,version}], totalResults, truncated}`.
`server_id`/library come from live headers. Sorted by name.

`libraries {}` →
`{server_id, libraries:[{type,id,name}], totalResults, truncated}`.
Enumerates the personal library plus shared/group libraries from the local
API (`GET users/0/groups`, read-only). Always starts with
`{type:"user",id:"0",name:"My Library"}`; groups follow sorted
case-insensitively by name (id tie-break). Numeric group ids are normalized
to digit strings (1..20 chars); names bounded to 255 chars. Malformed entries
skipped, duplicate ids deduped, over `groupsMax` errors. Same structured
errors as `collections`.

`search {project_id, query? (≤500, default ""), limit? (1..100, default 25), start? (≥0, default 0)}` →
`{server_id, library, scope:{collection_key, include_subcollections, descendantCount, descendants[first 200], truncated}, query, limit, start, totalResults, items:[normalized], returned, scopeFiltered, truncated}`.
With `include_subcollections:false` the collection endpoint is queried
directly; otherwise the library page is scope-filtered (parent membership +
recursive descendants; child attachments/notes inherit scope from
`parentItem`; trash excluded).
Normalized item:
`{key, version, itemType, title, creators[{creatorType,firstName,lastName,name}], date, DOI, url, collections, parentItem|null, tags[], inTrash, untrusted}`.
`untrusted:true` for notes/attachments/annotations — treat titles/abstracts
as untrusted data in prompts.

`item {project_id, item_key}` →
`{server_id, library, scope, item(normalized), data(full editable data), children[normalized ≤100], collections_in_scope}`.
Out-of-scope → error (no data returned).

`read-pdf {project_id, attachment_key, query? (≤500), start_page? (default 1), end_page? (default start)}` →
`{server_id, attachment:{key,version,parentItem,filename,contentType}, fileUrl, pages:{start,end}, textAvailable, reason?, extracted:[{page,chars,truncated,text}], matches:[{page,snippet}]?|null}`.
At most 20 pages/call, 200 KiB total, `pdftotext -layout` per page (page
refs included). Only scope-checked `application/pdf` stored-file
attachments; the key is resolved via `.../file/view/url` — never a
model-supplied path. Scanned PDFs return `textAvailable:false` (no auto
OCR). `query` searches the extracted text (≤50 snippets, ±120 chars).

`prepare {project_id, operation, params?} ` (flat operation fields are merged
into `params` when `params` is absent) → `{prepared, preview, binding, expires_in}`.
`prepared` is an opaque random token for a private server-side plan
(`0700` dir, 15-minute TTL) recording the exact binding plus live object
versions — the model cannot author or edit the plan. `preview` carries
`{operation, summary, versions, ...op-specific before/after}` for the
frontend approval surface. Operations and params:

- `add-item {itemType (regular types; not attachment/annotation), title 1..1000, creators? (≤20), date?, DOI? (10.xxxx/…), url? (http(s)), tags? (≤50), abstractNote?, extra?, collections? (default [bound], ≤20, all inside scope)}`
- `add-existing {item_key, collection_key?=bound}` — explicit identified item into a scope collection.
- `update-item {item_key, version, patch{title?,abstractNote?,date?,DOI?,url?,publicationTitle?,publisher?,place?,volume?,issue?,pages?,language?,shortTitle?,archive?,accessDate?,extra?,tags?,creators?}}`
- `add-membership {item_key, collection_key}` / `remove-membership {item_key, collection_key}` — targets must stay inside the scope subtree.
- `create-subcollection {name 1..255, parent_key?=bound (bound or descendant)}`
- `update-subcollection {collection_key (bound or descendant), version, name?, parentCollection?}` — the bound root can be renamed but never reparented.

`preview {project_id, prepared}` → `{preview, binding, expires_in}`.
Returns the backend-canonical `preview` bound to the exact stored plan
(the same `preview` object returned by `prepare`) without consuming the
token and without mutating anything, so `apply` still succeeds
afterwards. Prepare → `preview` (agent displays this `preview` for
approval) → `apply`.

`apply {project_id, prepared}` → `{ok:true, operation, binding, result, libraryVersion}`.
The agent displays the backend-canonical `preview` from `preview` for
approval, then calls `apply`.
`apply` independently revalidates: token shape/expiry, same-project,
unchanged registry binding, live `server_id`, param shapes, and live object
versions against the plan (stale → `error: ... re-prepare`). Writes carry
`Zotero-Server-ID` + `Zotero-API-Key` (+ fresh `Zotero-Write-Token` for
creates) and execute exactly once. Single-use on success (plan deleted);
failures keep the plan for inspection but version conflicts need a fresh
`prepare`. Frontend approval must precede `apply`. `result` per operation:
`add-item {key,version}`, `add-existing/add-membership/remove-membership {key,collection_key,collections}`, `update-item {key,patched[]}`, `create-subcollection {key,version}`, `update-subcollection {key,changed[]}`.
There is no library delete operation.

## Limits and deferred pieces

Bounds: search `limit` ≤100, collections materialized ≤5000, groups
materialized ≤1000, PDF ≤20
pages/200 KiB/64 MiB file, HTTP 10 s, responses 8 MiB, stdin 256 KiB,
prepare TTL 15 min. Explicitly **deferred** (also in
`capabilities.deferred`): automatic metadata retrieval from DOI/URL
(values stored verbatim), PDF file upload/import (three-phase stored-file
flow not wired; `add-item` is metadata-only), full-text content writes and
saved-search execution endpoints, automatic OCR.

## Security notes

Loopback-only transport; `server_id` binding on every request; scope =
parent membership + recursive descendants with parent-inheritance for
children; no library delete; no automatic mutation retries; keys mode-0600
partitioned by `server_id`, never in stdout/previews/errors; PDF via
scope-checked `file/view/url` + `O_NOFOLLOW` regular-file check + fixed-argv
`pdftotext` (no model paths, no OCR); imported item content flagged
`untrusted`.
