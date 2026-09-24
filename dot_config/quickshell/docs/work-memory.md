# Work → Memory / Memory → Work — scheduled drafts (B) + capture inbox (D) + evening review / morning plan (E) + resume selection/continuation (F2/F4)

Status: **Phase 3 rewrite (unify-agent-slim-sessions). A (shared
plumbing), B (work-log drafts), D (capture inbox), E (review), and
F2/F4 (Loop 2 resume selection + Ask Pi continuation) implemented.
Workstream C (session enrichment/association), Jev, polish paths,
auto-ignore, unit triage, and managed session states are deleted and
are not described here.**

Related: plan `/home/franzs/.opencode/plan/unify-agent-slim-sessions.md`
(read fully before coding), `docs/sessions.md`,
`docs/desktop-resume.md`, `docs/desktop-work-sessions.md`.

Source files: `scripts/annotations.py` (sidecar tables `captures` /
`drafts` / `prepared` / `reviews` / `state` / `session_meta` /
`session_links` / `content_index`), `scripts/memory_tick.py` (`tick` /
`run` / `status` with jobs `draft`, `scan`, `review`),
`scripts/work_log.py` (drafts + the only session-log filing path),
`scripts/session_capture.py` (local scan + page-only export),
`scripts/daily_review.py` (deterministic review + journal save),
`scripts/journal_assistant.py` (thought write path),
`scripts/thought_organise.py` (the one explicit model rewrite, L8),
`scripts/content_index.py` (derived search index + rebuild),
`scripts/text_safety.py` (shared sensitive-text gate),
`widgets/MemoryScheduler.qml` (resident tick driver),
`widgets/SessionCard.qml` / `widgets/CaptureInbox.qml` /
`widgets/ReviewCard.qml` (the owning cards, planner Daily tab only),
`scripts/quickshell_settings.py` (`memory_settings()`).

## Locked rules this file implements (L1 / L3 / L8)

- **L1 — Single save target: project page.** `work_log.py prepare`
  accepts only `--target page` (any other value fails closed); the
  journal-target branch is deleted. Session logs file to the project
  page's `Session log(s)` heading only. Journal writing survives
  through three other paths — the Journal tab, the Review card's
  **Save to journal** (a deliberate carve-out: the review lives in
  the journal, and its wording is pinned in
  `tests/test_widget_controls.py` so any drift from the verb set
  below is a visible edit), and thought capture (§Thought path) —
  sessions just stop offering it.
- **L3 — Markdown is the only durable store.** `annotations.db`
  holds ephemeral state (scan offsets, single-use preview tokens) and
  **derived, rebuildable indexes** (`content_index`) only. No
  user-authored content persists in the sidecar: thought text lives
  in the journal, TODO text on project pages, session logs on
  project pages. Dropping `content_index` costs a rescan, never data
  (`scripts/content_index.py rebuild`).
- **L8 — Exactly one model path remains: explicit "Organise".**
  `scripts/thought_organise.py prepare --text … [--session ID]` →
  `{prepared, preview}`; `apply --prepared TOKEN`. One isolated `pi`
  subprocess reusing `project_recap.build_pi_argv` lockdown flags,
  60 s timeout, output ≤ 4 KiB, gated on `memory.organise` (strict
  `True`, default `false`). Preview-first, never automatic, never
  silent. Saving still goes through `journal_assistant` prepare →
  exact preview → Confirm → append. The automatic tick path makes
  zero model calls.

Three content types, each with exactly one home and one writer
(§0.2 target model):

| Content | Home | Writer | Provenance |
| --- | --- | --- | --- |
| TODOs (auto-captured or quick-added) | project page, task list | `project_planner.py update_page` | `quickshell-session::`, `quickshell-ref::` |
| Thoughts (manual, AI-organised) | journal day page | `journal_assistant.py append` | `quickshell-session::`, `quickshell-at::`, `quickshell-ref::` |
| Session log (deterministic record) | project page, `Session log(s)` heading | `work_log.py apply` | `quickshell-worklog:: <project>:<session>` |

Markers are non-bullet property lines before the first child bullet,
following the `quickshell-agenda::` pattern. Full write-path detail
(markers, refs, index writeback): `docs/sessions.md` (§Thought write
path, §TODO accept path, §`content_index`).

## Architecture

`memory_tick.py` is the only automatic path. `MemoryScheduler.qml`
(resident `Timer` + one `Process`, `running` guard, generation
counter, `signal dataChanged()`) runs `memory_tick.py tick` every
`tickSeconds` (default 60 s, only when `memory.enabled`). All three
jobs are local-only: `default_draft` runs the `work_log` adapter,
`default_scan` runs the `session_capture` adapter (local regex scan,
no network since the Jev filter was deleted), `default_review` runs
the `daily_review` adapter. `RUN_JOBS = ("draft", "scan", "review")` —
there are no `enrich` / `triage` / `autoignore` jobs, no decider, no
budget ledger (`jev_calls` is always 0, kept as a shape-only key).

Failure policy (S-038, closed Phase 3): background jobs are all
local, and auto ticks never surface errors — failures fail soft
(`skipped` / `deferred` with a bounded reason) and retry next tick;
`dataChanged()` fires only when a payload reports `changed === true`.
Manual actions surface bounded inline `error: ...` text on the owning
card, never modal. There is no modal error path in the tick, the
scheduler, or `DailyAgenda.qml` (its error properties —
`agendaError`, `captureError`, `reviewError`, `ledgerError`,
`thoughtError`, `organiseError` — are inline card text by
construction).

## Sidecar storage: `annotations.db`

Same private dir as `activity.db`
(`$XDG_STATE_HOME/quickshell/desktop-activity/annotations.db`), `0600`,
same symlink/private-dir checks, `WAL` + bounded `busy_timeout`. Never
holds a transaction across a network call (and no job makes one —
the only subprocess with network reach is the explicit Organise call,
which never runs on the tick).

| Table | Purpose |
| --- | --- |
| `captures` | candidate TODOs/decisions with dedupe hash and `new/accepted/dismissed` status; dismissed rows stay as dedupe history |
| `drafts` | work-log drafts (deterministic payload + `saved_journal_ms` / `saved_page_ms` markers) |
| `prepared` | single-use, expiring exact-preview tokens for page/journal writes |
| `reviews` | one deterministic review per `(day, kind)` + `saved_ms` |
| `state` | ephemeral keys only: `last_seen_session_ms`, `last_review_day:<kind>`, `tick_last_ms`, `last_capture_ref:<session_file>` — never durable content |
| `session_meta` | reduced columns + Phase-2c attention columns (`thought_ref`, `todo_refs`, `refs`, `attended`); thought **text** never lives here, only the markdown reference — see `docs/sessions.md` |
| `session_links` | link kinds `capture` / `draft` / `agent_session` / `continued_from` (no `attached_to`) |
| `content_index` | derived FTS5 over thought/TODO block text (`text`, `page`, `line`, `kind`, `session_id`, `project_id`; `unicode61 remove_diacritics 2`), populated per successful write, rebuilt by `content_index.py rebuild` |

Deleted tables (`session_labels`, `unit_labels`,
`project_overrides`, `jev_calls`, `focus_blocks`) plus the removed
`session_meta` content columns are never read; the explicit,
user-run `scripts/annotations.py prune-legacy` (backs the sidecar up
to `annotations.db.bak-<ms>` first) drops them. Nothing runs it
automatically.

## Settings (`settings.json`, via `memory_settings()`)

```json
{"memory": {
  "enabled": true, "tickSeconds": 60,
  "workLog": true, "sessionCapture": true, "dailyReview": true,
  "organise": false,
  "minSessionMs": 300000, "reviewTime": "18:00", "morningTime": "07:00"
}}
```

Nine keys, all JSON-only by design (streamlining D4 — document, no
shell surface). `workLog` / `dailyReview` are local, default on.
`sessionCapture` defaults **true** (it is a local regex scan now, no
network, no key). `organise` gates the one explicit Pi call, default
`false`. `enabled: false` stops the scheduler. Malformed values fail
closed to defaults. Clamps: `tickSeconds` 10..3600, `minSessionMs`
0..86400000. Restart the shell after changing
`enabled`/`tickSeconds` (scheduler reads them at startup). There is
no `jev` block, no `ledger` block, and no polish/prioritization
flags.

## CLI

JSON stdin/stdout (`project_planner.py` style); validation failures
are single-line `error: ...` on stderr + exit 1; auto path soft-fails
to `{"available": false, "reason": ...}` with exit 0; bounded IO,
atomic writes, no tracebacks, no state in diagnostics; list-form
argv, `shell=False`.

```sh
python3 scripts/annotations.py init
python3 scripts/annotations.py state-get --key KEY
python3 scripts/annotations.py state-set --key KEY --value VALUE
python3 scripts/memory_tick.py tick
python3 scripts/memory_tick.py run --job draft|scan|review [--project UUID] [--session ID]
python3 scripts/memory_tick.py status
python3 scripts/content_index.py rebuild [--db PATH] [--graph PATH]
python3 scripts/content_index.py check
python3 scripts/thought_organise.py prepare --text TEXT [--session ID]
python3 scripts/thought_organise.py apply --prepared TOKEN
```

`tick` → `{available, enabled:{…},
processed:{sessions,drafts,captures,review}, jev_calls: 0,
deferred:[…], changed}`. `status` → enabled flags plus pending
counts. `run --job …` is the manual/UI trigger; `draft` needs
`--session ID` or `--project UUID`, `scan` takes an optional
`--project UUID` (else the current project), `review` is time-gated
per `morningTime`/`reviewTime` (see §E below).

## Workstream B — work-log drafts

Deterministic, local-only work-log drafts from closed work sessions.
`scripts/work_log.py` builds them; `scripts/annotations.py`
persists drafts + single-use prepared tokens; `memory_tick.py
default_draft` runs the adapter on every tick. The Session card
stages them through the capture/journal ladders with exact previews.
No model calls, no network in B — there is no polish path.

CLI (`python3 scripts/work_log.py [--db PATH] [--graph PATH]
<command>`):

```sh
python3 scripts/work_log.py draft --project UUID [--session ID] [--refresh]
python3 scripts/work_log.py draft --draft-id ID
python3 scripts/work_log.py prepare --draft-id ID --target page
python3 scripts/work_log.py apply --prepared TOKEN
python3 scripts/work_log.py continuation --project UUID
```

JSON single-line compact sorted output; validation/domain failures
are `error: ...` on stderr + exit 1. `--target` accepts only `page`
(anything else fails closed — the journal branch is deleted).
Without `--session`, `draft --project` resolves the project's
newest closed session from a bounded recent listing (skipped when
the listed sessions are all still open), else it fails with
`error: no closed session for project`.

Draft JSON: `{draft_id, project, project_source, project_name,
session (32-hex or null), session_start_ms, session_end_ms,
sections:[{id,title,items[]}], context, markdown,
properties:{quickshell-worklog:"<project>:<session>"}, labels: null,
evidence_digest, summary_key, suggested: false, created_ms, cached,
saved:{journal_ms,page_ms}}`. Sections are always present in order:
**What happened** (≤ 4 bullets) / **Open TODOs** / **Decisions** /
**Next** — each at most 8 items of at most 240 chars, no file
contents. `Changes` and `Files` are not prose sections: they are a
collapsed `context` strip (`{summary: "3 commits · 7 files", detail:
[…]}` with the detail behind an expandable). `markdown` is capped
at 16384 chars. `labels` is always null (no enrichment exists);
`saved` re-reads the persisted markers so a second save of the same
target is refused twice (DB saved flag + bounded marker scan for
`quickshell-worklog:: <project>:<session>` in the target).

Project resolution: stored attribution or explicit only — the
override/suggestion branches are deleted. A session with no project
produces no draft (`skipped`/`no_project`).

Caching: drafts are keyed by (project, session, evidence_digest)
with deterministic id `dwl_` + sha256; a cache hit returns the
stored payload with `cached: true` unless `--refresh` regenerates.

Save flow: `prepare` stages an exact preview (single-use expiring
token, kind `page`), the UI shows destination + revision + exact
addition/block with Confirm/Cancel, and `apply --prepared TOKEN`
consumes the token *before* writing (at-most-once), revalidates the
revision inside `project_planner.update_page`, then stamps
`saved_page_ms`. Page exports target the page's first `Session
log(s)` heading (case-insensitive; `#`-marked or plain bullet
anywhere, or raw `# Session log(s)` at column 0): the block is
appended as its last child, indented one tab deeper. When no such
heading exists, a top-level `- ## Session logs` heading is created
at the end of the page first.

Auto-draft (`memory_tick.py default_draft`): local, no network,
`jev_calls` 0. `ok` when a draft is produced/returned, `skipped` +
`no_project` for the terminal no-project case, `deferred` +
`evidence_unavailable` for no closed session / unreadable evidence
(retried next tick); unexpected internal errors degrade to
`deferred` + `failed`.

Continuation for Ask Pi (F4):

```sh
python3 scripts/work_log.py continuation --project UUID
```

Bounded JSON contract: `{project, available, text}` with `project`
the canonical UUID, `available` a bool, and `text` at most 2400
chars. Invalid CLI arguments or project UUIDs produce `error: ...`
(exit 1); every missing/disabled state returns `{available: false,
text: ""}` with exit 0 and preserves the already-prefilled
baseline: master `enabled` off, `workLog` off, no sidecar DB, no
cached draft for the project, malformed cached rows, or no usable
summary. The backend checks the newest cached draft for the project
and summarizes its frozen `sections` list — it never generates or
refreshes a draft, never touches the network (`jev_calls` 0) or the
activity store. The palette/planner prefills the baseline first,
then appends this text fail-soft when `available`.

Privacy: drafts are local-only. No network in B; the only model
call in this file's scope is the explicit Organise action, which
shells out to `pi` with the bounded thought text only.

## Workstream D — capture inbox

`session_capture.py` scans a project's Pi agent session JSONL for
candidate TODOs/decisions with a **local regex pass only** — there is
no Jev filter. The DailyAgenda "Captured" UI polls this backend; the
QML itself is owned separately.

```sh
python3 scripts/session_capture.py scan [--project UUID] [--limit N]
python3 scripts/session_capture.py list [--day YYYY-MM-DD] [--status new|accepted|dismissed|all] [--limit N]
python3 scripts/session_capture.py set-status --id N --status accepted|dismissed
python3 scripts/session_capture.py prepare --capture N [--session ID] [--ref IDENTITY]
python3 scripts/session_capture.py apply --prepared TOKEN
```

`--db` overrides the annotations sidecar (global). `list` is
read-only (no directory creation, never mutates rows) and returns
`{captures, day, status, total}` — `total` is the day-window count
for the card's `showing X of Y` truncation line (S-041, closed Phase
3). `set-status` only allows `accepted|dismissed` and refuses
unknown ids. JSON single-line compact sorted output;
validation/domain failures are `error: ...` on stderr + exit 1, no
tracebacks, no state in diagnostics.

Scan/offset/dedupe semantics:

- Project: `--project UUID` when given, else the CURRENT project
  (`{project: {id, name}}` or `None`); an unknown/missing project
  reports `skipped`/`no_project` without touching the filesystem.
- Session file: resolved via `project_sessions.scope_for_id(None,
  pid)` + `latest_session(scope)` (read-only; missing scope/session
  reports `skipped`/`no_session`). Regular-file safety checks (no
  symlinks, component checks) mirror `desktop_resume`; a rejected
  file reports `no_session`.
- Offset: sidecar state key `last_capture_ref:<session_file>` holds
  the next JSONL line index to read (string integer, default 0).
  Each scan reads new lines sequentially, bounded to 256 KiB / 1000
  lines, and stops early once `--limit` (1..8, default 8)
  candidates are collected so the next tick continues after them.
  Malformed JSONL lines are skipped.
- Line shapes: line 0 is `{"type":"session",…}`; only
  `{"type":"message","message":{"role":"user|assistant","content":[…]}}`
  records are scanned, only `content` items with `{"type":"text"}`
  (each text split into lines). `toolResult` roles, `thinking` /
  `toolCall` items, and full transcripts never leave the box.
- Candidates: first regex hit per text line, in order `TODO:`,
  `DECISION:`, `Next:`, `FIXME:`, `^\s*[-*] \[ \]` (kinds
  todo/decision/next/fixme/checkbox). Text is the stripped matched
  line with control chars removed, truncated to 400 chars, then
  dropped when the shared `scripts/text_safety.py` gate returns
  `None` (sensitive paths/words, control chars) — filtering is
  local, before any store. In-batch duplicates plus existing
  `captures` rows for the same `session_file` (any status,
  normalized whitespace-collapsed casefolded sha256) are skipped.
- No candidates (or only duplicates/sensitive): the ref advances
  past the scanned window and the scan reports
  `skipped`/`no_candidates`.

Gating: `sessionCapture` must be `true` (else `disabled`); there is
no key and no budget check. `enabled: false` stops the scheduler
entirely. `memory_tick.default_scan` wraps the adapter (import
failure → `not_implemented`; unexpected error →
`deferred`/`failed`).

Prepare/apply write path — single save target (L1, page-only; there
is no `--target` flag and the response still reports `target:
"page"`):

- Only `new` captures can be prepared; `prepare --capture N`
  (optional `--session 32HEX` + `--ref` provenance identity) reads
  the project page and builds the appended block: `- TODO <text>`
  plus `quickshell-session::` (only with a session) and gated
  `quickshell-ref::` (only when the 160-char sensitive-path gate
  passes) marker lines. The exact marker-inclusive text is stored
  as a single-use expiring token (kind `capture-page`, 600 s TTL).
  Old `capture-today` tokens are rejected at `apply` (no compat
  read; the single-use burn still applies).
- `apply --prepared TOKEN` consumes the token at-most-once, writes
  through `project_planner.update_page` (stale revision → bounded
  error, token stays burned), then `mark_capture_applied` (status
  `accepted` + `applied_ms`, only from `new`). A second apply of
  the same token, or a capture already accepted/dismissed, is
  refused with a bounded error. On success with a session, the
  block ref is appended to `session_meta.todo_refs`, the ref
  identity to `session_meta.refs`, and the exact block text is
  inserted into `content_index` (kind `todo`) — see
  `docs/sessions.md` §TODO accept path.
- `list` adds `project_name` from the registry (bounded; empty
  string on failure) to each row (day window local-midnight
  inclusive-exclusive, status `new|accepted|dismissed|all`).

Failure behavior: auto ticks never surface modal errors; scan
failures report `skipped` (`disabled`, `no_project`, `no_session`,
`no_candidates`, `filtered`) or `deferred` (`capped`, `failed`)
with `jev_calls` 0 and `changed` only when rows were stored. Manual
CLI validation failures exit 1 with single-line `error: ...`.

## Workstream E — evening review / morning plan

`daily_review.py` builds one deterministic review per `(day, kind)`
(`evening`/`morning`) and stores it in the `reviews` table. The
DailyAgenda "Review" UI polls this backend; the QML itself is owned
separately. Top-3 is deterministic order only — no prioritization,
no polish, no model calls.

```sh
python3 scripts/daily_review.py get --kind evening|morning [--date YYYY-MM-DD] [--refresh]
python3 scripts/daily_review.py prepare-save --kind evening|morning [--date YYYY-MM-DD]
python3 scripts/daily_review.py apply --prepared TOKEN
```

`--db` overrides the annotations sidecar (global), `--date`
defaults to local today. JSON single-line compact sorted output;
validation/domain failures are `error: ...` on stderr + exit 1, no
tracebacks, no state in diagnostics.

Scheduling / idempotence (tick adapter `daily_review.review`,
wired as `memory_tick.default_review` like `default_scan`):

- `dailyReview` must be `true` (else `disabled`, including the
  manual `run --job review` path).
- Due kinds per tick (local time from the tick `now_ms`): `morning`
  once local time reaches `morningTime` (default 07:00),
  `evening` once it reaches `reviewTime` (default 18:00), each only
  when sidecar state `last_review_day:<kind>` differs from today.
  Morning is processed first, then evening.
- `morning` generates any time it is due (popup-open generation has
  no activity requirement). `evening` additionally requires ≥ 1
  session in today's window (bounded `evidence.list_sessions`
  probe); with zero sessions the tick reports `skipped` /
  `no_activity` WITHOUT setting `last_review_day:evening`, so
  late-evening work is still reviewed on a later tick.
- Manual `get` mirrors the gates: a stored review is returned
  without regeneration (read-only path creates nothing); a missing
  `morning` generates on demand any time, a missing `evening`
  before `reviewTime` returns `{found: false, reason: "not_due"}`
  (exit 0); `--refresh` regenerates (evening still respects the
  time gate).
- On successful generate + store the adapter writes the review and
  sets `last_review_day:<kind>`; any generation sets `changed`.
  Nothing due reports `skipped` / `not_due`.

Deterministic sections for the date D (all bounded; a failed graph
read omits/flags its section under `sections.unavailable`, never an
exception):

- `projects`: sessions in [D 00:00, D+1 00:00) aggregated per
  project (`project_id`, `name`, `sessions`, `minutes`), ≤ 8 by
  minutes desc; unattributed sessions count as name `"other"`.
- `changes`: one `project_session_changes.session_changes` call per
  worked project (≤ 8, latest session) → `commits` as short hashes
  (`"abc1234..def5678"`) or `"none"`; failures read as
  `note: "unavailable"`.
- `captures`: annotation captures created in the day window →
  `{new, accepted, applied}` (`applied` counts `applied_ms` in the
  window).
- `todos`: `daily_agenda.list_agenda(graph, D)` →
  `{scheduled, completed}` where `scheduled` counts rows with
  `scheduledDate == D` (done or not) and `completed` counts the
  done subset on D.
- `journal`: presence via
  `journal_assistant._current_journal(graph, D-as-`YYYY_MM_DD`,
  create=False)` → `{present}`.
- `markdown`: deterministic rendering of sections + top-3, ≤ 8 KiB.

Tomorrow top-3: candidates are the first ≤ 12 open TODOs from
`logseq_todos.todos(graph)` in its deterministic order, texts
capped to ≤ 240 chars via the shared `text_safety` gate (sensitive
items dropped, fail closed). Each item carries `{task, path, line,
page, revision, score: null, target_date}` where `revision` is the
current per-page revision (`null` when unreadable; a stale revision
at click time surfaces the bounded `select` error and the user
refreshes) and `target_date` is D+1 for `evening`, D for `morning`.
Top-3 = first 3 candidates in order, `prioritized: false`, zero
model calls. The card reports `showing X of Y` when the candidate
list was capped (S-041, closed Phase 3).

Prepare-save / apply journal path (the L1 carve-out — reviews
always land in the journal, never a project page):

- `prepare-save` builds the export text (deterministic markdown)
  plus the `quickshell-review::<day>-<kind>` marker; reads the
  CURRENT journal via `journal_assistant.context` and refuses when
  the marker is already present (`review already saved to
  journal`); stages an exact preview through
  `journal_assistant.prepare` as a single-use `review-journal`
  token (payload `{kind, day, path, date, revision, addition}`,
  10-minute TTL, `expires_in: 600`). Reviews always land in
  today's journal (`journal_assistant` rejects any other date), so
  a backfilled `--date` still saves to the current journal with
  the day distinct in the marker.
- `apply --prepared TOKEN` consumes the token at-most-once
  (`consume_prepared`), appends via `journal_assistant.append`,
  then `mark_review_saved`. A stale revision burns the token with
  a bounded error (`journal write failed`, then `unknown or
  expired`); double apply is refused; `saved_ms` is stamped on
  success.

Failure behavior: auto ticks report `skipped` (`disabled`,
`not_due`, `no_activity`, `graph_unavailable`) or `deferred`
(`failed`, `capped`, `evidence_unavailable`) with `jev_calls` 0
and `changed` only when a review was generated; state is never set
on failure. Manual CLI validation failures exit 1 with
single-line `error: ...`.

## Thought path and Organise (L8)

Thoughts land in today's journal only (`prepare`/`append` reject
any other date). Without a session id the text is appended
unchanged (degraded, never blocked); a malformed session id fails
closed; a gated-out `ref` simply yields no `quickshell-ref::` line.
The preview always shows the exact marker-inclusive text. On
success the block ref is written to `session_meta.thought_ref` and
the exact block text is inserted into `content_index` (kind
`thought`). Full marker rules: `docs/sessions.md` §Thought write
path.

Organise (`scripts/thought_organise.py prepare --text … [--session
ID]` → `{prepared, preview}`; `apply --prepared TOKEN`) rewrites
one draft for readability/links with a fixed system prompt
("preserve meaning, uncertainty, and links; normalize to Logseq
block style; never invent facts"). The preview replaces the draft
text in the Session card editor; saving still goes through the
journal prepare → exact preview → Confirm flow. Never automatic,
never silent, gated on `memory.organise`.

## `content_index` (derived, rebuildable)

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS content_index USING fts5(
  text, page UNINDEXED, line UNINDEXED, kind UNINDEXED,
  session_id UNINDEXED, project_id UNINDEXED,
  tokenize='unicode61 remove_diacritics 2');
```

Same tokenizer/declaration discipline as `activity_fts`
(`docs/desktop-history-search.md:130`), validated exactly by
`annotations.check_content_index`. Populated incrementally on every
successful thought/TODO write (one INSERT via
`content_index.index_block`, no rescan); `rebuild [--graph PATH]`
rescan is idempotent, bounded (≤ 20000 rows), read-only on the
graph. `sessions.py search` is `content_index MATCH` ∪ collector
`search-activity`, deduped by `session_id`, content matches first —
so agents search thoughts and TODO text, not just activity. The Pi
tool is `session_search`, available in every scope. Full contract:
`docs/sessions.md` §`content_index`.

## Loop 2 surfaces (overview Resume; palette `seen:`; F2/F4)

Loop 2 surfaces (overview popup Resume button; palette `seen:`
activity search; palette `Resume` operation selection (F2); `Ask Pi`
cached continuation (F4)) are local-only and add no sidecar state
beyond the existing helpers. F2/F4 are implemented with no new
network or model calls (see `docs/desktop-resume.md`): F2 adds a
palette `Resume` operation checkbox confirmation (all available
operations selected by default; uncheck for a subset; empty
selection disallowed; `Cancel` performs no action; Overview popup
`Resume` unchanged), and F4 enriches `Ask Pi` by prefilling the
baseline draft immediately, then appending the cached local
continuation (deterministic work-log section summary only —
there are no session labels anymore) fail-soft when available —
unavailable leaves the baseline unchanged, with no auto-send and no
overwrite of an edited draft. The review UI (E) is
`widgets/ReviewCard.qml` (Evening/Morning tabs, top-3 Add to
tomorrow/today, exact-preview Save to journal, Refresh; generated
body renders through shared `widgets/MarkdownBody.qml`, user rows
stay `PlainText`) driven by review state in
`widgets/DailyAgenda.qml` and embedded in the planner Daily tab
next to the capture inbox. The Session card
(`widgets/SessionCard.qml`, planner Daily tab only) hosts the
thought editor + Organise alongside Accept/Dismiss TODO.
`memory_tick.py run --job review` runs the real `daily_review`
adapter; `run --job draft` runs the local work-log adapter, and
`run --job scan` runs the local capture adapter.

## Verb glossary (Phase 3, §6.4)

One verb set on Session/Capture/Review cards: **Accept**,
**Dismiss**, **Save thought**, **Save to project**, **Add to today**,
**Organise**, **Refresh**, **Confirm**/**Cancel** (previews only).
**Resume** lives in the SessionCard header (it duplicates palette
`resume:` and the overview Resume button), never in rows. Every
control carries `Accessible.name`/`description` per
`tests/test_widget_controls.py`. Two deliberate carve-outs from the
set, both pinned in tests so drift is a visible edit:

- Review keeps **Save to journal** (L1: the review lives in the
  journal).
- The attribution-hygiene card keeps **Add to project…** /
  **Ignore** (deterministic folder mapping fix, no inference).

`Saved to journal.` / `Saved to project page.` report a completed
write. `Dismiss` declines without any write and says so in its
accessible description. Every write goes prepare → exact preview →
`Confirm`, and previews always offer `Cancel` (discards without
writing). The file Confirm on the Session card reads **Save to
project** (single target, L1); journal/organise previews keep the
generic **Confirm**.

## Deleted in Phase 2a/3 (do not reintroduce)

`scripts/jev.py`, `scripts/session_enrich.py`,
`scripts/session_triage.py`, `scripts/session_units.py`;
`session_labels`, `unit_labels`, `project_overrides`, `jev_calls`,
`focus_blocks`; unit keep/split, merge/unmerge, `Ignore`/`Restore`
ledger states, `new/reviewed/filed/ignored` lifecycle, focus
blocks, rollups, week view, auto-ignore, polish
(`work_log`/`daily_review`), review prioritization, capture Jev
filtering, the `--target journal|today` branches, `filed_uri` /
"Open in Logseq", and the overview Session-log / "Sessions · last
5" / label-chip / suggestion rows. There are no Jev call sites
left, no network on the automatic path, and no managed session
objects: a session is a grouping key plus a context hook (see
`docs/sessions.md`).

> Policy note (manifesto §3): the deletions above stand for the
> Phase 2a/3 shapes they removed, but the retired local-first rule no
> longer bans model calls as such. Predefined-outcome classification
> (screening, prioritization, triage) belongs to the classifier tier and
> may be reintroduced only through the bounded, opt-in batch in
> `docs/roadmap.md` (Jev, second look). The safety envelope is
> unchanged: never a security boundary, never auto-approve, no silent
> writes, `text_safety.py` before egress.

## Prerequisites / manual commands / tests

```sh
cargo build --locked --release --manifest-path services/agent-orchestrator/Cargo.toml
python3 scripts/memory_tick.py tick
python3 scripts/memory_tick.py status
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```

Unit tests (`test_annotations.py`, `test_memory_tick.py`,
`test_work_log.py`, `test_session_capture.py`,
`test_daily_review.py`, `test_content_index.py`,
`test_journal_assistant.py`, `test_thought_organise.py`,
`test_session_card_ui.py`, `test_capture_inbox_ui.py`,
`test_review_card_ui.py`) cover tick gating/scheduling, draft /
prepare / apply round-trips, capture scan / offset / dedupe /
prepare / apply round-trips, review scheduling / idempotence /
prepare-save / apply round-trips, index insert / rebuild, and the
card verb + truncation pins; structural tests
`test_memory_scheduler_ui.py` and `test_project_overview_ui.py`
cover the scheduler and the recap + Resume popup. With
`organise: false` a tick makes zero model calls.

## Limitations

- The Session/Capture/Review card QML is owned separately; the
  backends here (draft, scan, review generation, journal save) are
  implemented and tested. Drafts (B), capture scanning (D), and
  review generation (E) are all local; the only model call in
  scope is the explicit user-triggered Organise rewrite.
- Open/deferred session ID sets in the sidecar are bounded to 1000
  entries (oldest dropped on overflow); the discovery frontier plus
  per-ID rechecks cover the rest.
- Per-tick ID rechecks are bounded (25 deferred + 25 open IDs,
  deferred first, oldest first); rechecked open IDs rotate to the
  end of the stored list for eventual coverage, and rechecks stop
  once the tick time budget is spent, so deep backlogs cannot push
  a tick past the QML watchdog.
- On a persist-retry the local draft adapter may be invoked again
  for the same session; `work_log.py` upserts by (project,
  session, evidence_digest) with a deterministic draft id, so
  retries are idempotent.
- `status`/`recent` open the sidecar read-only and never mutate
  rows, but SQLite may create `-wal`/`-shm` sidecars while reading;
  if the state directory is not writable they exit 1 with a bounded
  error and the popup fails soft.
- Writes keep the existing revision + exact-preview + confirm
  protocol with an explicit user action as approval (B save flow:
  `prepare` → inline preview → Confirm → `apply`, single-use
  expiring token, double-save refused).
