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
- `widgets/CalendarPopout.qml` — clock-anchored keyboard-capable
  `PanelWindow` hosting the planner UI (layer-shell `OnDemand` focus so
  its text fields are typeable); the bar clock (`widgets/Bar.qml`
  `clockAnchor`) toggles it.
- `widgets/ProjectPlanner.qml` — genuine `Daily` tab (`icons/history.svg`)
  embedding the same `DailyPlanner`; agenda writes arrive via `pageWritten`
  and refresh the page cache through `applyAgendaPage` without touching
  agents or drafts. Its `Add to day` action also targets the selected date.

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

## Safety

Reuses `scripts/project_planner.py` internals (`_GraphLock`,
`_page_bytes`, `_replace_page`, `_validate_relative_path`,
`_validate_revision`, `_split_lines`, `_toggle_line`, `_task`) without
modifying the planner: advisory graph lock, exact SHA-256 revision check,
O_NOFOLLOW symlink refusal, regular-file/size/UTF-8 checks, atomic replace
with relocated-directory detection, and bounded JSON transport. Lines that
are not bullet tasks are rejected for property writes.
