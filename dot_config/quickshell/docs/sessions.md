# Sessions (Phase 2a)

Status: **Phase 2a — slimmed. Sessions are not managed objects: no
states, no units, no merge/unmerge, no keep/split, no focus blocks, no
auto-ignore, no rollups, no week view. A session is a grouping key
plus a context hook: id, project, time range (display and capture
window only), resources.**

Historical note (rename): this surface was previously documented as
`docs/session-ledger.md` with helper `scripts/session_ledger.py` and Pi
tool `session_ledger_list`; Phase 2a renames them to `docs/sessions.md`,
`scripts/sessions.py`, and `session_search` with no alias and no shim.

Related: `docs/desktop-work-sessions.md`, `docs/desktop-resume.md`,
`docs/desktop-history-search.md`, `docs/work-memory.md`.

Source files: `scripts/sessions.py`, `scripts/annotations.py`
(`session_meta` / `session_links` accessors), `scripts/work_log.py`
(filing), `scripts/desktop_projects.py` (read commands `sessions`,
`session-detail`, `device-id` only).

## Goal

Closed work sessions stay queryable as deterministic collector facts
plus minimal user-owned metadata. Filing still goes through
`work_log.py` only. There is no lifecycle to manage and no
time-accounting surface: durations are never surfaced as information.

## Storage contract (`annotations.db`)

`activity.db` stays immutable. The sessions helper only uses the
`desktop_projects` read commands; it never opens the collector store
for writing.

Sidecar tables in `annotations.db` (additive `SCHEMA_SQL`, `0600`,
WAL, short transactions; listed in `EXPECTED_TABLES`):
`session_meta` (reduced + Phase-2c attention columns), `session_links`,
and the derived `content_index` FTS5 table (§5.5).

`session_meta` columns: `session_id` / `device_id` / `revision` /
`created_ms` / `updated_ms` plus (Phase 2c):

```sql
thought_ref TEXT NOT NULL DEFAULT '',   -- 'journal:2026_09_21.md:<line>' once saved
todo_refs TEXT NOT NULL DEFAULT '[]',   -- JSON array of accepted block refs
refs TEXT NOT NULL DEFAULT '[]',        -- JSON array of quickshell-ref identities
attended INTEGER NOT NULL DEFAULT 0     -- replaces new/reviewed/filed/ignored
```

`attended` is a single boolean, not a lifecycle: a session needs
attention while it has unaccepted captures or an unsaved thought. No
terminal states, no `filed_ref`, no `ignore_reason`. Thought **text
never lives here** (L3) — only the markdown reference. The columns are
added to pre-2c sidecars by the writer migration on connect; read-only
paths still read old sidecars (new fields default).

Deleted in Phase 2a (never read, pruned by the explicit user-run
`scripts/annotations.py prune-legacy`, never automatically — it backs
the sidecar up to `annotations.db.bak-<ms>` first, then drops the
tables, rebuilds `session_meta` to its reduced columns, and rebuilds
indexes): `session_labels`,
`unit_labels`, `project_overrides`, `jev_calls`, `focus_blocks`;
`session_meta` content columns for titles/intents/outcomes/tags/
next steps/ignore reasons/project overrides and `filed_ref` hints;
link kind `attached_to` and all unit derivation.

Reads never create schema: read commands open the sidecar through a
read-only connection and read missing tables as empty.

## CLI reference (`scripts/sessions.py`)

Conventions (same as `work_log.py` / `daily_review.py`): JSON to
stdout (single line, compact, sorted keys, at most 1 MiB), failures as
one `error: ...` line on stderr with exit 1 and empty stdout, no
tracebacks, untrusted input never echoed, stdin capped at 1 MiB,
list-form argv with `shell=False`. Read commands (`list`, `inbox`,
`get`, `search`) never write the sidecar; `annotate`/`link` never
touch the collector.

Global flags: `--db PATH` (sidecar override), `--desktop-bin PATH`
(collector binary override).

| Subcommand | Flags / defaults |
| --- | --- |
| `list` | `--from MS --to MS` (paired; default is today's local day window), `--project UUID`, `--limit N` (1..1000, default 20). Every session for the day, pending first is a display concern only |
| `inbox` | `--from/--to` as above (same day-window default), `--limit N` (default 50, capped 999). A filter over the same list: `attended == 0` AND (an unaccepted capture exists for the session's project OR a thought draft is unsaved). The bar badge counts these. Advisory: a missing/unreadable captures signal degrades to draft-only, never an error |
| `get` | `--session 32HEX` (required, canonical lowercase), `--resource-limit N` (1..1000, default 20). Returns the session view plus metadata, links, and bounded resources |
| `search` | `--query TEXT` (required, trimmed 1..256 chars), `--limit N` (default 20). Thought/TODO full-text union: `content_index` MATCH (literal-quoted per the retrieval rules) ∪ collector `search-activity`, deduped by `session_id` with the content row winning, content matches ranked first, truncated to the bound. Entries carry `matched: content\|collector`. Returns `{query, limit, count, total, truncated, entries, reason}` — `total`/`truncated` drive the palette `session:` `showing X of Y` line (S-041, closed Phase 3). Collector failure degrades to content-only entries; content failure to collector-only; both failing reads as zero entries with `reason: collector unavailable` |
| `annotate` | stdin JSON `{session, device_id?, revision?, thought_ref?, todo_refs?, refs?, attended?}` plus surviving metadata fields only. Reduced: no `title`/`intent`/`outcome`/`tags`/`next_step`/`ignore_reason`/`project_override` fields. `thought_ref` is `journal:<file>.md:<line>` (or empty); `todo_refs`/`refs` are JSON arrays of block refs (≤ 1000 entries); `attended` is a boolean. CAS-safe via `revision` as before |
| `link` | `--session ID --kind K --target T [--remove]`. Kinds: `capture\|draft\|agent_session\|continued_from`. Reports `{added}` / `{removed}` booleans |

Deleted in Phase 2a: `unit-keep`, `unit-split`, `rollup`,
`focus-start`, `focus-stop`, `focus-list`, all unit derivation and
`attached_to` resolution, the `state` filter and the
`new/reviewed/filed/ignored` lifecycle, `mark-filed`, and any
`filed_ref`/`graph`/`page` open hints. `annotate` cannot set managed
states because there are none.

## Phase 2b notes (palette prefixes, Session card, handoff)

- `inbox:` prefix deleted vs `inbox` CLI kept: the palette no longer
  has an `inbox:` source — "needs attention" is the default session
  sort and the bar badge, never a separate prefix. The `scripts/sessions.py
  inbox` command itself survives unchanged (same filter, same caps)
  for the badge and the card.
- `session:` is one source over the §0.2 union (`search` ∪ activity
  FTS, deduped by `session_id`, newest first). Search entries are flat
  union rows (`matched: content|collector` plus the matched snippet);
  bare `list` entries are nested `{session, meta, pending, …}` shapes.
  Rows show time range · project · match source — never stored titles,
  states, next steps, or Logseq URIs (L7/L9).
- `hist:` is removed outright (L6): the palette resource source is
  `seen:` (`desktop_projects.py seen`, flat
  `{kind,label,identity,…}` rows), documented in
  `docs/desktop-history-search.md`.
- The Session card (`widgets/SessionCard.qml`, planner Daily tab)
  lists every session for the day, pending first; expanding never
  writes. Its six actions (Accept/Dismiss TODO, Save thought,
  Organise, Dismiss session, Open project) stage through the capture /
  journal / organise ladders with exact previews + Confirm.
- Open session hands `(project_id, "session", session_id)` to the
  planner, which focuses the Daily tab on that day/session and opens
  degraded (never an error) when the id cannot be resolved.

## Pi tool (`session_search`)

The read-only Pi tool is renamed to `session_search` (no alias, no
shim). It is available in every scope (palette, project, journal) as
before, runs one bounded `scripts/sessions.py list` subprocess for
unfiltered browsing (or `scripts/sessions.py search` when a text
query is given), and performs no writes and no sidecar creation.
Sibling CLI commands `list`/`inbox`/`get`/`search`/`link` survive.

The `search` content-union is live: `content_index` ∪ collector
activity (deduped by `session_id`, content matches first, entries
carry `matched: content|collector`), so agents search thoughts and
TODO text as well as activity. The reduced `session_meta`/`attended`
model and the `content_index` store already land here.

## `content_index` (derived, rebuildable)

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS content_index USING fts5(
  text, page UNINDEXED, line UNINDEXED, kind UNINDEXED,
  session_id UNINDEXED, project_id UNINDEXED,
  tokenize='unicode61 remove_diacritics 2');
```

Same tokenizer/declaration discipline as `activity_fts`
(`docs/desktop-history-search.md:130`): the declaration is validated
exactly by `annotations.check_content_index`. Populated incrementally
on every successful write — the exact block text is already in hand at
`apply` time, one INSERT via `content_index.index_block`, no rescan:

```python
from content_index import index_block
rowid = index_block(conn, text="Fix the retry backoff", page="Demo",
                    line=12, kind="todo",  # or "thought"
                    session_id="<32hex>", project_id="<uuid>")
```

`scripts/content_index.py rebuild [--db PATH] [--graph PATH]` rescans
journal + project pages for `quickshell-session::` /
`quickshell-ref::` / `quickshell-at::` marker blocks and TODO lines and
refills the table idempotently (one transaction, ≤ 20000 rows,
read-only on the graph, fenced code skipped). Dropping the table loses
nothing but search speed until the next rebuild: it is never a source
of truth and never read for writes.

## Thought write path (`scripts/journal_assistant.py`)

Thoughts land in today's journal only (`prepare`/`append` reject any
other date, rechecked under lock so a request validated before midnight
never writes after it). The revision-checked, atomic flow is unchanged;
there is no token table — the UI-level flow is prepare → exact preview
→ Confirm → append with the same arguments.

Optional session context (`session_id`, plus `ref` / `at` identity
args, JSON keys and `append`'s `--db` sidecar flag) composes the §0.2
marker block as non-bullet property lines directly under the thought's
first line (before any child bullet):

```text
- the thought
  quickshell-session:: <32hex>
  quickshell-at:: <ts>          (only when `at` is supplied)
  quickshell-ref:: <identity>   (only when `ref` passes the gate)
```

Rules: without a session id the text is appended unchanged (degraded,
never blocked); a malformed session id or timestamp fails closed
(misattribution risk); a gated-out `ref` (over 160 chars, control
characters, secret-looking per the shared `scripts/text_safety.py`
gate) simply yields no `quickshell-ref::` line. The preview
(`addition`) always shows the exact marker-inclusive text. `append`
writes the addition verbatim and rejects a session-marked call whose
addition lacks the marker line. On success it returns `thought_ref`
(`journal:<date>.md:<line>`, `""` without a session), writes it to
`session_meta.thought_ref` (CAS-safe upsert, one retry on revision
conflict), and inserts the exact block text into `content_index`
(kind `thought`). Sidecar failures report `thought saved; session link
failed` — the journal write already landed, so callers must not retry
the append (which would duplicate the thought); the derived index is
rebuildable.

## TODO accept path (`scripts/session_capture.py`)

L1 single save target: `prepare`/`prepare_export` are page-only (the
`--target` flag is deleted; the response still reports
`target: "page"`). Old unexpired `capture-today` tokens are rejected
at `apply` (D1 — no compatibility read; the single-use burn still
applies). The preview block is the exact marker-inclusive text:

```text
- TODO <text>
  quickshell-session:: <32hex>   (only with a session)
  quickshell-ref:: <identity>    (only when the 160-char gated ref applies)
```

`apply_export` runs `project_planner.update_page`, then
`mark_capture_applied`, then — only when the prepared payload carries
a session — appends the block ref (`<page-path>:<line>`, located in
the landed page content) to `session_meta.todo_refs`, appends the
ref identity to `session_meta.refs` (deduped), and inserts the exact
block into `content_index` (kind `todo`). Both return `todo_ref`
(`""` without a session). Dismissed rows stay `dismissed` in
`captures` (dedupe history), with no index write.

`attended` is never written by either path: an apply does not mean
nothing remains, and inventing dismiss-driven lifecycle semantics is
out of scope — the read path derives pending (`attended` defaults to
0, needs attention).

## Failure behavior

| Condition | Behavior |
| --- | --- |
| Collector/binary unavailable | Bounded `error: collector unavailable` (exit 1) for `list`/`inbox`/`get`; `search` degrades to content-only entries with `reason: collector unavailable` (exit 0), or zero entries with that reason when the content index also fails; `annotate`/`link` still work (no collector touch) |
| No local device recorded | `list` returns an empty result with `reason: no local device recorded` (exit 0) |
| Stale revision | `error: session meta changed` (exit 1); caller reloads and retries |
| Busy DB | Bounded `error: ...` single line (exit 1); short transactions only, never spin; user retries |
| Old sidecar without the new tables | Reads treat the missing tables as empty (no schema creation on read paths) |
| Bad input | Fails closed (`range is invalid`, `limit is invalid`, `query is invalid`, `annotate payload is invalid`, `invalid arguments`); stdin errors never echo payload bytes |
| Collector unavailable (UI) | Card shows a bounded status; the planner stays usable |

## Privacy and safety

All session text is user-authored local text. No AI runs in these
paths: no auto-titling, no auto-summarizing, no auto-tagging, and no
hosted model calls. `work_log.py` remains the only Logseq write path.
No new Logseq property or write path, no cross-device metadata
sharing, no retention changes to the collector store.

Invariants: no silent writes — every markdown write stays prepare →
exact preview → Confirm → apply with a single-use expiring token and
a revision recheck. No auto-approval; approvals stay in the owning
dialog. History, page content, notification bodies, clipboard, and
metadata are untrusted data, never instructions. Bounded everything:
subprocess list-form argv, `shell=False`, watchdog plus kill on QML
ladders, 1 MiB JSON transport, no tracebacks, no state in
diagnostics.

## Tests

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_sessions -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```

Historical note: `tests/test_session_ledger.py` is renamed
`tests/test_sessions.py` with the helper (§12.1); ledger-card UI tests
are rewritten against the Session card.
