# Daily agenda

Backend-only planner over Logseq **project pages** (`scripts/daily_agenda.py`).
Project pages are the existing markdown pages below `pages/`; journals are
never listed or written by this helper. The concurrent UI worker builds the
calendar/picker on top — this document is the contract it must follow.

## Storage

Scheduling is a direct Logseq block property under the task, with no
external state:

```markdown
- TODO ship the thing
  quickshell-agenda:: 2026-09-13
```

- Only a non-bullet `quickshell-agenda:: YYYY-MM-DD` line **before the first
  child bullet** counts as the task's date. Properties on descendant TODOs
  never leak to the parent.
- An invalid date value is ignored (`scheduledDate == ""`).
- Selection inserts the property before child blocks using the parent's
  indentation (tabs preserved, otherwise two spaces), and updates/dedupes an
  existing value. Deselection removes every direct agenda property and leaves
  sibling properties, child blocks, and unrelated notes untouched.
- Newlines are preserved per line: CRLF stays CRLF, and a file without a
  final newline still has none after the write.

Completion is atomic: one locked, revision-checked write flips the TODO to
`DONE` (via the planner toggle) and appends a date-labelled progress child
at the end of that TODO's block:

```markdown
- DONE ship the thing
  - [2026-09-13] wrote tests
    second line stays nested
```

The child is `- [YYYY-MM-DD] first-note-line` (not a TODO/DONE task, so it
never appears in the agenda), with extra note lines indented two more spaces
so all text stays nested under the original TODO. A nonblank `note` is
required.

## CLI

`--graph PATH` selects the graph, otherwise `LOGSEQ_GRAPH` or `logseqGraph`
in `settings.json` (see `settings.example.json`). `list` reads its date from stdin; the mutating commands read their
full request from stdin. Success prints compact JSON to stdout and exits 0.
Any failure prints `{"error": "..."}` to stdout and exits nonzero. Stale
revisions, invalid lines, non-task lines, unsafe paths, oversize pages,
symlinks/FIFOs, and malformed types/dates are all rejected.

```sh
python3 scripts/daily_agenda.py --graph PATH list < list.json
python3 scripts/daily_agenda.py --graph PATH select < select.json
python3 scripts/daily_agenda.py --graph PATH complete < complete.json
python3 scripts/daily_agenda.py --graph PATH toggle < toggle.json
```

### `list` — stdin `{"date": "YYYY-MM-DD"}` (`date` optional, defaults to local today)

```json
{"date": "2026-09-13"}
```

Returns:

```json
{
  "date": "2026-09-13",
  "graphName": "Notes",
  "tasks": [
    {
      "path": "pages/Work.md",
      "page": "Work",
      "revision": "<sha256 of the page bytes>",
      "line": 1,
      "task": "ship the thing",
      "marker": "TODO",
      "done": false,
      "scheduledDate": "2026-09-13"
    }
  ],
  "truncated": false
}
```

- Covers all safe project pages: every open task plus done tasks scheduled
  on the requested date. Done tasks scheduled elsewhere (or unscheduled)
  are excluded; the UI filters the rest. Omit `date` (or pass `null`) to
  inspect local today.
- `scheduledDate` is `""` when unscheduled.
- Output is capped at 1000 tasks with `truncated: true` when capped.
  Tasks scheduled on the requested date are prioritized first so a cap never
  silently drops the selected day.

### `select` — stdin `{path, revision, line, date, selected}`

```json
{
  "path": "pages/Work.md",
  "revision": "<sha256>",
  "line": 1,
  "date": "2026-09-13",
  "selected": true
}
```

- `date` is any valid ISO calendar day; `selected` must be boolean.
- `selected: true` schedules/updates the property; `selected: false`
  removes it (no-op success when absent).
- Returns `{"page": <full read_page response>}` with fresh `revision`,
  `content`, and `todos`.

### `complete` — stdin `{path, revision, line, note, date}`

```json
{
  "path": "pages/Work.md",
  "revision": "<sha256>",
  "line": 1,
  "note": "shipped behind the flag\nsecond line",
  "date": "2026-09-13"
}
```

- `note` must be a nonblank string; `date` is a valid ISO day labelling the
  progress child.
- Atomically marks the TODO `DONE` and appends the progress child.
- Returns `{"page": <full read_page response>}`.

### `toggle` — stdin `{path, revision, line, done}`

```json
{"path": "pages/Work.md", "revision": "<sha256>", "line": 1, "done": true}
```

Reuses the planner toggle and returns `{"page": <full read_page response>}`.

## Expected UI

- **Calendar + agenda view:** a month/day calendar picks `date` for `list`;
  the agenda section shows selected-date tasks (open and done) first, with a
  picker/dropdown for other open tasks. Tapping a task toggles selection
  (`select`), checking it off uses `toggle`, and finishing with a note uses
  `complete` (note field required).
- **25/5 Pomodoro:** the picker may offer a 25-minute focus timer with a
  5-minute break; completing a focus session prompts for the progress note
  and calls `complete` with today's date. The timer itself is UI-only — the
  backend only stores the resulting date label and note.
- **Scope:** project pages only. Journal pages are out of scope; the planner
  never creates pages. Every write is an explicit user action (selection,
  toggle, or completion with a note) — no model call is involved.

## Implemented UI

- `widgets/DailyAgenda.qml` — single shared state (owned by `shell.qml`
  as `dailyAgenda`): `selectedDate`, one serialized backend operation
  (`list`/`select`/`complete`/`toggle` via `Process` stdin), the frozen
  completion snapshot (`completionTask` + `completionNote`, cleared only
  by explicit cancel/save), and the wall-clock Pomodoro
  (`focusMinutes`/`breakMinutes`, `pomoPhase`/`pomoRunning`/`pomoDeadline`).
- `widgets/DailyPlanner.qml` — reusable UI bound to that state: Monday-start
  month grid (prev/next/Today/sync), scheduled list (toggle, remove,
  `Finish…`), picker with search (`Add to day` schedules the picked open task
  on the currently selected date via `select` with `selected: true`),
  completion editor with a required note and `Complete & save`, Pomodoro
  controls, reload, and busy/error display.
- `widgets/DailyPlannerPane.qml` — parity wrapper around the reusable
  UI. Both daily surfaces instantiate this wrapper (never `DailyPlanner`
  directly) so their wiring cannot drift: one `agenda` binding, one
  `compact` flag, one `projectPlanningRequested` relay, one
  `syncViewToDate` passthrough.
- `widgets/CalendarPopout.qml` — clock-anchored keyboard-capable
  `PanelWindow` hosting the planner wrapper in **compact** mode
  (`compact: true`: month grid, Scheduled list, open task picker,
  Pomodoro, and the quick-add input only); the bar clock
  (`widgets/Bar.qml` `clockAnchor`) toggles it.
- `widgets/ProjectPlanner.qml` — genuine `Daily` tab (`icons/history.svg`)
  embedding the same `DailyPlannerPane` wrapper in full mode
  (`compact: false`); agenda writes arrive via `pageWritten`
  and refresh the page cache through `applyAgendaPage` without touching
  agents or drafts. Its `Add to day` action also targets the selected date.

## Mirror model

`DailyAgenda` (`widgets/DailyAgenda.qml`, owned once by `shell.qml` as
`dailyAgenda`) is the single state owner for the date, drafts, Pomodoro,
capture inbox, review payload, and session ledger. `DailyPlannerPane` is
the single UI owner for the daily workflow. The CalendarPopout and the
ProjectPlanner Daily tab are deliberate mirrors: two views of the same
state through the same wrapper. Neither is secondary; closing either
surface never stops the timer or drops a draft. Since Phase 3 the two
mirrors render **different card sets from one component** via the
`compact` flag (slim-down, §6.3): the popout (`compact: true`) keeps
the month grid, the Scheduled-for-day list, the open task picker, the
Pomodoro, and the quick-add input; Captured, Review, and SessionCard
render in the planner Daily tab only (`compact: false`). A
source-level parity guard pins this: both surfaces must instantiate
`DailyPlannerPane` (and must not instantiate bare `DailyPlanner`),
the popout must pass `compact: true` while the planner passes
`compact: false`, and the three planner-only cards must be gated on
`!root.compact` — covered by `tests/test_daily_planner_ui.py`.

## Week rollup ownership

Phase 2a note: the ledger week rollup (`rollup`) and the "This week"
section are removed along with units and states; `ReviewCard` keeps
Evening/Morning only and `setReviewKind` accepts only
`"evening"`/`"morning"`.

## Card conventions (P4)

One chrome rule for every agenda-backed card (`DailyPlanner`,
`CaptureInbox`, `ReviewCard`, `SessionCard`):

- **Header:** `RowLayout` (`fillWidth`, spacing 8) with a bold title
  (`Theme.text`, `fillWidth`, `ElideRight`). Surface titles
  (`Daily planner`, `Journal assistant`) set 17–19 px; card headers
  (`Review`, `Sessions`, `Captured`) use the default
  size. A live count rides in the title in parens (`Sessions (N)`,
  `Captured (N)`); an optional date context is a second text in
  `Theme.subtext0` 12 px. Actions are right-aligned `WidgetButton`s;
  icon-only supplemental actions (`Sync view`, `Scan now`) stay
  `WidgetIconButton`s with a `tooltipText`. Section labels without
  actions (`Scheduled`, `Add open tasks`) stay `Theme.accentMuted`.
- **Preview:** the shared `widgets/PreviewPanel.qml` (destination +
  revision line, bounded readonly monospace block at 96 px, `Applying…`
  / bounded error lines, `Cancel` / `Confirm`). `Confirm` is gated on
  preview + single-use token + idle ladder; `Cancel` (and Escape on the
  session card) discards without writing.
- **Refresh / loading:** every card offers `Refresh` (the planner's old
  `Reload` was renamed) showing `Loading…` while its ladder is busy.
  `CaptureInbox` keeps its supplemental `Scan now` next to `Refresh`.
- **Empty states:** one pattern — `Nothing <thing> <scope>.` plus the
  next action: `Nothing scheduled for this day yet. Pick open tasks
  below.` (planner), `Nothing captured for this day yet. Press Scan
  now to look for new activity.` (capture), `No sessions for this day
  yet. Sessions appear here after the collector closes them.`
  (session card). The review `Not due yet — …` text is a
  due-gate, not an empty state, and is unchanged.
- **Truncation:** one pattern — `showing X of Y`, silent unless
  actually capped (D6): `The task list was capped; scheduled tasks
  are shown first.` (planner picker), `showing X of Y` behind the
  SessionCard (`agenda.ledgerTotal`, day-list cap 50), CaptureInbox
  (`agenda.captureTotal`, cap 20), and Review top-3 (pre-slice
  `top_total`) lines. Day-list backend reasons surface bounded
  through `ledgerStatus`/`ledgerNotice`.
- **Nested lists:** inner task/session/capture/review lists keep fixed
  bounded heights with their own `ScrollBar.AsNeeded` (scheduled 168,
  picker 168, captured 148, review top 132, sessions 280); the outer
  `Flickable` (calendar card, planner Daily tab) scrolls the card
  column. Outer-scroll-only was rejected: unbounding the inner lists
  risks layout loops and unbounded growth on short screens, while the
  bounded heights are already tuned per content type.

## Palette AI (constrained, palette-only)

Palette mode registers two constrained tools over the same
`scripts/daily_agenda.py` `list`/`select` backend and direct
`quickshell-agenda` property; no alternative storage is created. They are
unavailable in scoped project/journal modes.

- `logseq_agenda_list` — `{"date"?: "YYYY-MM-DD"}` (defaults to local today).
  Returns the same `date`/`graphName`/`tasks`/`truncated` shape as `list`,
  with `path`, `page`, `line`, `task`, `marker`, `done`, `revision`, and
  `scheduledDate` per entry. Read-only discovery for natural-language
  matching; when several tasks match, the model must ask the user to clarify
  instead of guessing.
- `logseq_agenda_add` — `{"path", "line", "revision", "date"?}` (defaults to
  local today). Schedules one existing open task with `selected: true`. The
  tool fresh-reads the listing, validates the exact open task and revision,
  shows `task`/`project`/`date` plus the old schedule when moving for
  mandatory UI confirmation, then writes with the unchanged approved
  `revision`/`path`/`line`/`date`. Denial, missing UI, abort, timeout, a
  stale revision, an already-done line, or a scoped session performs no
  write; the backend rechecks the SHA-256 revision atomically.

Example palette prompt:

```text
ai: Add the TODO about rereading chapter 4 of the ... project to my daily todos
```

The model calls `logseq_agenda_list`, picks (or clarifies) the exact TODO,
then calls `logseq_agenda_add`. The confirmation shows the task, the project
(`page` + `path:line`), the target date (local today when omitted), and the
previous schedule (`unscheduled` or the old `YYYY-MM-DD` when moving). Only
an explicit approval schedules it.

## Palette `todo:` quick-add (Phase 2b)

`todo: <text>` in the palette routes to the current project page's
task list; with no current project (or no linked note), it falls back
to today's journal as a plain `- TODO` block. The ladder reuses the
existing flows: `desktop_projects.py current-project`, then
`project_planner.py page` → exact preview → Confirm →
`project_planner.py update` with `{project_id, revision, content}` and
a revision recheck (or `journal_assistant.py context` → `prepare` →
Confirm → `append`). Provenance mirrors `session_capture`
(`quickshell-session::` plus the gated `quickshell-ref::`, preferring
the seen row pinned by **Add TODO here** for one quick-add, else the
most recent ambient resource). The Confirm panel shows the exact block;
**palette stays open on Confirm** so several TODOs can be entered in a
row. Enter starts prepare and never confirms; only the explicit
Confirm button writes.

## Popout quick-add (compact CalendarPopout only, Phase 3 §6.3)

The slim popout keeps a `todo:` quick-add input (`Quick-add TODO`,
`quickAddField`) because the tray composer is gone and the popout no
longer hosts the Session card. Same contract as the palette path
above — project page, else today's journal, via prepare → exact
preview → Confirm (`quickAddConfirm` / `quickAddCancel`), one
serialized `Process`, Enter never confirms an armed preview, stale
revisions drop the preview with `Page changed; press Enter to retry`.
One deliberate limitation: the popout writes a **bare block with no
provenance** (no `quickshell-session::` / `quickshell-ref::`). This
surface has no ambient block and no pinned seen row, so there is
nothing to cite; the palette owns the session-scoped flow. The input
clears on apply and the popout stays open for serial entry.

## Safety

Reuses `scripts/project_planner.py` internals (`_GraphLock`,
`_page_bytes`, `_replace_page`, `_validate_relative_path`,
`_validate_revision`, `_split_lines`, `_toggle_line`, `_task`) without
modifying the planner: advisory graph lock, exact SHA-256 revision check,
O_NOFOLLOW symlink refusal, regular-file/size/UTF-8 checks, atomic replace
with relocated-directory detection, and bounded JSON transport. Lines that
are not bullet tasks are rejected for property writes.
