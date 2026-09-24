# Roadmap

Ideas that are not implemented and not scheduled. Nothing here is a
contract; each item states the constraint it must respect and the open
questions that decide whether it ships.

## Jev (TypeSafe "System One") — decision-model sidecar

Status: **removed direction (Phase 2a, unify-agent-slim-sessions L2:
Jev deleted entirely — no labels, no triage, no capture filtering, no
review prioritization).** The notes below stay as background only: what
Jev is, the verified access shape, the constraints that ruled it out,
and the non-goals that remain policy. There are no Jev call sites left
(`scripts/jev.py`, `scripts/session_enrich.py`,
`scripts/session_triage.py` are deleted). The routing rule
(`docs/manifesto.md` §3) now assigns predefined-outcome classification
to the classifier tier; the second-look section below is the reopened,
bounded candidate set.

Background source:
<https://dev.to/valyuai/how-to-use-jev-a-practical-guide-to-typesafes-system-one-model-g5e>
(Sep 17 2026; Jev launched Sep 15 2026, early access waitlisted).

**What it is:** a hosted model that answers only typed questions —
`Choice` (one of up to 255 options), `Score` (2–10 ordered levels), and
`Noul` (yes/no probability) — in one parallel pass. $0.042/MTok input
with free output. It does not generate text or code, cannot use tools,
and knows nothing beyond the state you hand it (text only). All
published benchmarks are TypeSafe's own; treat them as unreproduced.

**Verified access (Sep 18 2026):** reachable through OpenRouter with the
existing ambient `OPENROUTER_API_KEY`; Jev is not listed in
`/api/v1/models` but is served as `~typesafe/jev-latest` (pinnable:
`typesafe/jev-1.13`) on the alpha endpoint
`POST https://openrouter.ai/api/alpha/decisions`. `/chat/completions`
rejects it. Request shape (all three question types verified in one
call):

```json
{"model": "~typesafe/jev-latest",
 "state": "<string | object | array of text>",
 "questions": {
   "refund_requested": {"type": "noul", "instructions": "…"},
   "department": {"type": "choice", "instructions": "…",
                  "criteria": {"billing": "…", "other": "…"}},
   "frustration": {"type": "score", "instructions": "…",
                   "criteria": ["Calm", "Frustrated but civil", "Very angry"]}}}
```

`answers.<name>` carries `noul`, or `choice`/`score` with
`probabilities`, `confidence`, and a `legend` for scores; `usage`
reports tokens and cost (~$0.00002 for the three-question test above).
Measured round-trip from this machine: **0.5–0.7 s** (three calls).
The alpha endpoint is undocumented and may change; treat it as
unstable.

### Constraint

Jev cannot replace the Pi agent. Every Quickshell surface that talks to
a user (`ProjectPlanner`, `JournalAssistant`, `CommandPalette`,
`project_recap.py`) depends on generative, tool-using conversation —
Jev returns no prose and no tool calls. The desktop collector
(`services/agent-orchestrator`) is deliberately local, deterministic,
and inference-free (`docs/desktop-work-sessions.md`,
`docs/desktop-project-context.md`); it stays that way. Activity
classification may only happen as a separate, explicitly triggered pass
over stored data (item 8), never in the collector's hot path.

### Removed call sites (Phase 2a)

The Phase 1 (bounded opt-in: command router, notification triage,
approval annotation, context selection, clipboard classification,
Zotero screening, journal triage) and Phase 2 (activity enrichment,
escalation/verification, project-switch suggestion) call sites are
deleted with the Jev path and will not be built. The Constraint and
Non-goals sections above stay as policy.

### Non-goals

- Replacing or proxying Pi chat sessions.
- Any inference in the collector hot path, or auto-switching
  projects/scopes/sessions from a classifier.
- Auto-approval or treating Jev as a security boundary.
- Silent journal/Logseq writes from a classifier.
- Background clipboard indexing to a hosted API.
- Generative summaries (`project_recap.py`) — Jev cannot write them.
- Arithmetic, counting, or date math: keep those in code
  (documented Jev weaknesses).

## Jev, second look — pre-filtering, context loading, advisory automation

Status (Sep 2026): **reopened candidates, unscheduled, opt-in.** This
section is the classifier tier of the routing rule
(`docs/manifesto.md` §3): the calls below send text to a hosted
endpoint, which the retired local-first rule forbade. The Phase 2a
removal stands for what it
removed (no classifier in the collector, no managed session objects, the
deleted scripts stay deleted); what is reopened is a bounded set of
typed-question passes whose answers only reorder, pre-fill, or suggest.
Unlike the deleted Phase 1 batch, J3–J5 may also run as advisory passes
on the automatic path.

Shared shape: at most **one fan-out call per UI event** — every question
for that event is answered in a single parallel pass (measured 0.5–0.7
s; ~$0.00002 for the three-question test; $0.042/MTok input, output
free), using the verified `POST /api/alpha/decisions` contract above
with `typesafe/jev-1.13` pinned. Everything is gated on a new `jev`
settings block (one key per call site, all default off, validated and
clamped like the `memory` block) plus a `dailyBudgetUsd` cap; all egress
text is bounded and passes `scripts/text_safety.py` first; and
`memory_tick.py`'s `jev_calls` counter (today a shape-only 0) becomes
real, stopping every advisory pass when the budget is spent.

Still policy, even here: Jev is never a security boundary and never
auto-approves anything; a Jev answer never writes to the graph, the
journal, or a sidecar on its own (writes keep prepare → exact preview →
Confirm); the collector hot path stays inference-free — Jev runs as a
separate pass over stored or in-flight text; no arithmetic, counting, or
date math in Jev; any failure or unavailability (the alpha endpoint is
undocumented and may change) degrades to today's deterministic
behavior, never to a hang or a different write path. Reopened from the
Phase 1/2a deletions: context selection (J1), the command router (J2),
capture filtering (J3), review prioritization (J4), notification triage
(J5), approval annotation (J6) — plus J7, new.

### J1. Query pre-filter → Pi context loading

Before a prompt is sent to Pi (palette `ai:` / Ask Pi, planner and
overview agent boxes), one Jev pass asks a `noul` per candidate context
block — the baseline draft plus the F4 continuation, open TODOs from the
project page, `session_search` hits, `seen:` resources — "does this
block belong in the prompt for this query?". Kept blocks are appended as
toggleable chips, visible and droppable before send.

*Constraints:* selection only includes or omits — it never rewrites the
query and never invents context; the deterministic baseline (draft +
continuation) is present regardless of the answer; unselected blocks are
not sent to Pi either (the pass reduces prompt size and ambient leakage
together); per-block and total request caps; no auto-send, no new write
path.

*Open questions:* chips vs. a "N blocks selected" summary line; run on
submit with the 0.5–0.7 s shown as pending, or behind an explicit "with
context" affordance; behavior on Jev failure (fall back to all baseline
blocks, or to no optional blocks).

### J2. Free-text query router

Unprefixed palette input gets one Jev pass: `choice` over destinations
(Pi project agent, journal draft, `seen:`/file search, window action,
calculator, quick capture) plus `noul` "is this an agent request at
all". Typed prefixes (`>`, `@`, `%`, `session:`, `seen:`, `todo:`,
`clip:`, `zot:`) stay deterministic and never reach Jev.

*Constraints:* the router proposes and the row is confirmed exactly like
today's rows — a wrong route is always recoverable with Escape and no
side effects; the prefix grammar, calculator-first ranking, and the
"never suppress a source" rule (S-025) stay authoritative; Jev fills
only the bare-text gap.

*Open questions:* re-sort rows vs. a single hint row; per-destination
confidence threshold; whether accepted routes feed P1 frecency.

### J3. Capture screening — advisory, optionally automatic

The local regex scan keeps producing candidates; a Jev `noul` per
candidate ("real TODO/decision vs. noise") reorders the Captured card,
and under an explicit `jev.autoScreen` opt-in marks noise `dismissed`
automatically — the first call site allowed to change state without a
click.

*Constraints:* auto-dismiss is a local state change only, never text
deletion: rows stay in `captures` with their answer visible, and
dismissed rows remain dedupe history exactly as today; the regex scan
and the `text_safety.py` gate run before Jev sees anything; Accept is
always one explicit click and never Jev-gated; Jev failure leaves the
card exactly as today.

*Open questions:* advisory tier default on/off; one-click undo for
auto-dismissed rows vs. the existing status list; threshold calibration
(TypeSafe's benchmarks are unreproduced).

### J4. Review top-3 scoring

The `daily_review` candidate contract already reserves `score: null` —
fill it with a Jev `score` (2–10 ordered levels over criteria like
"unblocks other work", "small win", "blocks someone") over the bounded
TODO candidates and the day's sections, so top-3 becomes top-3-by-score
with `prioritized: true` and a visible score per row.

*Constraints:* deterministic order stays the fallback and the tie-break
and `--refresh` can recompute; candidate text keeps the existing ≤
240-char `text_safety` cap; "Add to tomorrow" is still the agenda
`select` with revision checks; nothing is scheduled without the click.

*Open questions:* criteria set and separate evening/morning prompts;
persist scores in the `reviews` row or recompute per refresh;
interaction with the ≤ 12-candidate cap.

### J5. Notification triage and escalation

On receipt (or on a bounded tick over new history rows), one Jev pass
per notification: `noul` "needs action?", `score` urgency, `choice`
category. The result reorders a triage view in the notification history
and pre-fills the shell-level "→ TODO" / "→ journal" actions (I3).

*Constraints:* never auto-dismiss, never `invoke()` an action identifier
from a Jev answer, never let a Jev answer suppress, rank away, or DND a
notification (I5's auto-DND stays time-based); payload text stays
untrusted and bounded exactly as today and passes `text_safety.py`
before egress; no notification content in any store beyond the existing
history.

*Open questions:* per-notification vs. batched on the tick (0.5–0.7 s
latency vs. cost); per-app overrides; whether urgency is shown or only
orders rows.

### J6. Approval risk annotation

A `score` over the bounded approval payload ("how far does this reach
outside the sandbox?") rendered as an advisory chip on
`PaletteApprovalDialog` and the planner approval route.

*Constraints:* annotation only — the exact preview, the consequence
line, and the Reject/Accept path are unchanged, and auto-approval stays
a non-goal; the chip must not read as a verdict (an advisory "low" is
not permission); payloads stay untrusted.

*Open questions:* wording that cannot be mistaken for a security
verdict; whether the misreading risk is worth it at all — this item is
the likeliest to be declined.

### J7. Zotero screening for `zot:`

`choice`/`score` over bounded title/abstract text against the current
project's state, ranking library hits ("worth entering the project's
reading list?") for the W5 `zot:` source.

*Constraints:* read-only under the existing `docs/zotero.md` bounds;
item metadata is untrusted text through `text_safety.py`; ranking
reorders and never hides — the deterministic order stays available; no
downloads, no writes.

*Open questions:* which fields may leave the machine; per-query calls
vs. one bounded batch per project; whether a screening answer is
remembered (new durable state, needs its own rule — see L3).

## Work → Memory / Memory → Work — milestones A–C

Status (Sep 2026, updated Phase 3): **A shared plumbing and B
work-log drafts implemented** (`scripts/annotations.py` + sidecar
`annotations.db`, `scripts/memory_tick.py` `tick`/`run`/`status`,
`widgets/MemoryScheduler.qml`, `memory` settings with clamps). Details:
`docs/work-memory.md`; plan:
`/home/franzs/.opencode/plan/work-memory-loops-1-2.md`. The Jev path
is deleted in Phase 2a (`scripts/jev.py`,
`scripts/session_enrich.py`, `scripts/session_triage.py` removed; no
labels, no polish, no prioritization, no model calls on the automatic
path).

**B work-log drafts implemented** (`scripts/work_log.py` draft/
`prepare`/`apply`, `annotations.db` drafts/prepared storage,
`memory_tick.py` local auto-draft, exact-preview + Confirm save flow;
details: `docs/work-memory.md`).
**C session enrichment/association deleted** in Phase 2a (unify-agent-
slim-sessions L2: no gated Jev labels, no label chips, no
Confirm/Dismiss suggestion controls, no sidecar overrides).
Deterministic attribution remains authoritative.
See `docs/work-memory.md`.
**F1/F3 Loop 2 surfaces implemented**: the overview popup gains a Resume
button (bounded `desktop_resume.py plan` preview incl. unavailable
operations, explicit Confirm, then `execute` + planner handoff with
`openProject(id, "resume", msg)`), and the palette gains a `seen:` desktop
activity source (flat resource rows via `desktop_projects.py seen`
with Open / Copy / Ask-Pi / Add-TODO actions). Both are
local UI only, no new writes or model calls.
**D capture inbox implemented** (`scripts/session_capture.py` scan/
list/set-status/prepare/apply + `memory_tick.py default_scan` over the
`annotations.db` `captures` table; DailyAgenda capture state +
`widgets/CaptureInbox.qml` "Captured" card in the daily planner with
Accept to the project page, Dismiss, and "Scan now" via
`memory_tick.py run --job scan`; opt-in `sessionCapture`, local regex
scan only, no model calls). **E evening review / morning plan
implemented** (`scripts/daily_review.py` get/refresh/
prepare-save/apply + `memory_tick.py` once-per-day review after
`reviewTime`/`morningTime` over the `annotations.db` `reviews` table;
DailyAgenda review state + `widgets/ReviewCard.qml` Review card in the
daily planner with Evening/Morning tabs, deterministic top-3 Add to
tomorrow via the existing agenda select, exact-preview Save to journal,
and Refresh; no prioritization, no polish, no model calls).
**F2/F4 implemented** (Phase 3): F2 palette `Resume` operation
selection (checkbox confirmation over `desktop_resume.py plan`
operations, empty selection disallowed, `Cancel` performs no action) and
F4 enriched `Ask Pi` continuation (cached `work_log.py continuation`
summary appended to the baseline draft fail-soft). Nothing remains of
this workstream: A, B, D, E, and F1–F4 are implemented and C is
deleted.

## Desktop surfaces — clipboard, timeline, stats, OCR, approvals, polkit

Status (Sep 2026): **candidates, unscheduled.** Six independent surfaces,
grouped because each one has a bounded local data source and existing
in-tree plumbing, and none of them may run inference in a hot path. Each
item states its constraints and the open questions that decide whether it
ships.

### 1. Clipboard history surface

Status: **candidate, unscheduled — the keybind is already reserved.**
`~/.config/hypr/keybinds.lua` binds `mainMod + C` to
`quickshell:clipboard`, but the shell defines no global shortcut with
that name (`CommandPalette.qml` has `commandPalette`, `ProjectPlanner.qml`
has `projectPlanner`), so the chord is dead today. The palette already
searches clipboard history read-only (`clip:`/`clipboard`/`#` over
`cliphist list` text mode) but offers no browsing, no images, and no
actions beyond copy.

**What it is:** `widgets/ClipboardPopup.qml`, anchored and revealed like
`MediaPopout`/`ControlCenter` (anchor tracking and sizing conventions:
`docs/control-center.md`), plus a `GlobalShortcut { name: "clipboard" }`
and `IpcHandler` mirroring `CommandPalette.qml`. Rows support fuzzy
filtering (`cliphist list`), delete/wipe (`cliphist delete`/`wipe`),
image entries previewed through a bounded temp file (`cliphist decode`),
and explicit row actions.

**Constraints:**

- Clipboard content is untrusted input, never instructions. A local
  deterministic filter excludes password-like entries before any model
  call, and there is no background indexing of cliphist to a hosted
  API.
- Image decode is bounded (bytes/timeout, temp file cleaned up); no
  unbounded base64 in QML properties, and clipboard content is never
  written to the activity store.
- Row actions reuse existing plumbing only: copy via `wl-copy`, OCR via
  local `tesseract`, explain/translate through the palette worker (with
  its approval protocol), journal drafts through
  `journal_assistant.py`, results shown as existing palette row kinds.
- Pinning has no cliphist feature; it needs a small local sidecar
  (documented schema, bounded size) or a deliberate decision to skip
  pins — never a second clipboard store.
- A clear-on-lock rule is required so copied secrets do not outlive a
  lock (`WlSessionLock`/hyprlock interaction).

**Open questions:** dedicated popup vs. a palette `clip:` browse mode
(the reserved chord argues for the popup; the palette already owns
search); TTL/max-entries policy for pins; whether OCR/translation of a
clipboard entry may leave the machine (opt-in, default off); what "paste"
can mean on Wayland (copy-only vs. key injection) without a
compositor-specific virtual-keyboard dependency.

### 2. Day timeline (local activity, read-only)

Status: **removed (Phase 3, unify-agent-slim-sessions L7 + §12.2).**
Sessions are not managed objects and durations are never surfaced as
information; the Session card lists every session for the selected day
(pending first, then time descending), which answers "what did I do
today" without a time-of-day view. Retrieval stays in palette
`seen:` / `session:` and Pi `session_search`. The notes below stay as
background only.

**What it is:** a read-only timeline for the selected day (session blocks
colored by project, app/resource tooltip, click-through to the existing
`seen:` actions and `desktop_resume.py plan` preview), embedded in
`CalendarPopout` or a dedicated popout.

**Constraints:**

- Read-only, deterministic (no inference), no screenshots: the collector
  owns a deterministic job under the routing rule (`docs/manifesto.md`
  §3) and the UI only renders stored rows.
- One bounded day query (`[from, to)` epoch-ms, session limit ≤ 1000);
  render aggregated session blocks, never one QML item per activity row,
  and surface truncation honestly.
- Timezone/day boundaries come from local time over session
  `start_ms`/`end_ms`; one documented rule, applied consistently with the
  calendar popout and daily agenda.
- Sessions carry device provenance; the view either filters to this
  device or labels foreign sessions, per a documented choice.
- Click-through stays preview-first: session detail or a resume plan is
  explicit, nothing is focused/moved/resumed automatically.

### 3. System stats popout and cheaper sampling

Status: **candidate, unscheduled.** The bar's three `StatModule`s fork
`free`, `top -bn1`, and `python3 -c shutil.disk_usage` every 5 s and show
plain text (`widgets/StatModule.qml`, `Bar.qml`).

**What it is:** a stats popout with bounded sparklines (CPU total/
per-core, memory, disk usage and I/O, network throughput) plus the
current values; the inline modules keep their compact text and gain at
most a mini graph.

**Constraints:**

- Remove the per-tick process forks: read `/proc` (QML `FileView`) or add
  one bounded sampler to the resident bridge. This is a performance
  requirement, not a preference.
- CPU percentages come from deltas between samples; one sampler owns the
  state so no two consumers disagree.
- Missing sensors/GPU degrade to an explicit "unavailable", never a fake
  value; sampling cost on battery is stated and the interval bounded.
- The history ring is fixed-size and lost on restart (nothing persisted).
- The popup follows the anchor/reveal conventions in
  `docs/control-center.md`; the bar sizing contract (`sideReserve`,
  compact flags) must not change.

**Open questions:** bridge op vs. QML-side sampling; keep the inline text
modules at all; per-core vs. aggregate by default; which
temperature/GPU sources are portable across machines.

### 4. Screenshot OCR extension

Status: **candidate, unscheduled.** `widgets/ScreenshotModule.qml` runs
`hyprshot -m region|output`; palette `>` actions capture a region for Pi
through `scripts/screen_capture.py` (grim/slurp, bounded geometry/bytes,
private process groups). `tesseract` is installed locally; no OCR path
exists.

**What it is:** after a region capture, an explicit "Copy text" / "Send
to agent" action: local `tesseract` OCR with the result on the clipboard
by default, or routed into a journal draft / Pi prompt as an explicit
follow-up.

**Constraints:**

- OCR runs locally; an image (or its text) leaves the machine only
  through the existing explicit agent/approval paths. The default action
  is clipboard-local.
- Never OCR in the background: screenshots may contain secrets; temp
  images are deleted after use and OCR text is never written to the
  activity store or sidecars.
- Capture bounds from `screen_capture.py` (geometry, 8 MiB, timeouts)
  apply unchanged; a missing `tesseract`/language pack produces a bounded
  inline error, never a hang.
- Routing stays preview-first: journal/Pi destinations go through the
  existing prepare/preview/confirm protocols.

**Open questions:** entry point (post-capture toast vs. palette action
vs. screenshot module menu); output routing default; image retention
(delete after OCR?); whether OCR text may enter a Pi prompt automatically
or only after preview.

### 5. Agent activity indicator + unified approval inbox

Status: **candidate, unscheduled.** Three resident workers run through
`widgets/ScopedAgent.qml` (palette, project, journal), and pending
approvals can outlive their surface (closing the palette leaves its
worker running; the planner uses the same bridge approval path;
`widgets/PaletteApprovalDialog.qml` owns the palette UI). There is no
global activity indicator and no aggregate of pending approvals.

**What it is:** one bar chip showing worker activity (idle / streaming /
awaiting approval, aggregated) that focuses the owning surface, plus an
optional unified list of pending approvals whose rows route the decision
back to the exact owning dialog.

**Constraints:**

- The Rust bridge owns lifecycle, parsing, and approval state; QML only
  aggregates the snapshots `ScopedAgent` already projects. No Pi parsing
  in QML.
- Never auto-approve, auto-expand, or expire differently from the owning
  dialog; payloads stay exact and untrusted, and the existing
  approval/preview protocol is the only decision path.
- Multiple workers may await approval at once; counts and focus targets
  stay per worker with a deterministic tie-break, and expired entries
  disappear without being resurrected.
- DND suppresses notification banners, never approval affordances; no new
  persistence.

**Open questions:** bar slot (tray capsule vs. right slot); count vs.
latest-only; whether the unified list is read-only ("jump") or
decision-capable through the existing dialogs (jump-only is the safer
first cut); bridge protocol addition for a cross-worker summary vs.
QML-side aggregation.

### 6. Polkit authentication agent

Status: **candidate, unscheduled.** `Quickshell.Services.Polkit`
(`PolkitAgent`, `AuthFlow`) and `Quickshell.Services.Pam` are available in
the installed module set (0.3.1); the shell has no polkit agent and
`widgets/PasswordPopup.qml` is WiFi-specific, so privileged prompts fall
to whatever external agent is configured (or none).

**What it is:** a themed polkit agent surface (identity selection, visible
prompt/response, error state, cancel) registered through `PolkitAgent`,
in the same visual language as `PasswordPopup.qml`.

**Constraints:**

- Fail closed: no fallback prompts, no retries from stored secrets, no
  credential logging or persistence; responses live only for the active
  request.
- Exactly one agent may own the session bus name: enabling this must
  document and handle replacing/disabling the current agent so a session
  never ends up with none.
- Lock-screen interaction is explicit: define whether polkit prompts can
  appear while locked and keep approval/notification surfaces off the
  lock, consistent with the chosen lock path.
- Handle `showInfo` / hidden responses without rendering an empty prompt;
  cancel and failure paths stay bounded and inline.

**Open questions:** replace vs. coexist with the current external agent;
always-on registration vs. lazy; how to test end-to-end without a
privileged side effect (follow the existing UI test harness), and whether
a screen-unlock PAM flow (`WlSessionLock`) is ever in scope.

## Integration, polish, and productivity — candidate batch 2

Status (Sep 2026): **candidates, unscheduled.** Nineteen items, grouped
by the quality they target. Nothing here is a contract; each entry keeps
this file's convention — what it is, the constraints it must respect, and
the open questions that decide whether it ships. Items already tracked
above (clipboard surface, day timeline, stats popout, screenshot OCR,
agent activity/approvals, polkit) and the Jev/Memory workstreams are out
of scope for this batch.

### Integration — surfaces sharing state

**I1. Quick Capture overlay (`mainMod + N`).** One focused input that
turns a thought into the right artifact without opening the planner:
today's journal, the current project's page task, or a Pi request with
ambient project context, plus a compact recent-captures list.

*Constraints:* every write goes through the existing prepare/preview/
confirm and revision-checked helpers (`journal_assistant.py`,
`project_planner.py`, `daily_agenda.py`) — never a silent append; reuse
the palette input stack rather than a second text component; no new store
(drafts follow the existing sidecar patterns); works with no project
selected (journal-only) and with no graph configured (agent only);
bounded input; Escape leaves no trace.

*Open questions:* default destination and whether it is remembered per
project; whether the agent handoff opens the planner or stays an inline
palette turn; whether captures should also appear in the CaptureInbox
review flow.

**I2. Calendar bridge + "next meeting" chip.** A bar chip (`in 12m ·
Standup`) fed by the user's real calendar, with a join-link action,
auto-DND while a meeting is active, and an optional journal template when
one starts. Evolution is already launched at startup; the shell currently
knows nothing about events.

*Constraints:* calendar data is external untrusted text; read-only — the
shell never writes events; auto-DND must be visible in the chip and
reversible, and must never suppress approvals; the chip only opens a join
URL, it never auto-joins; a stale cache degrades to "no next event"
rather than a wrong countdown; polling is bounded.

*Open questions:* source (EDS over `gdbus`/python-gi vs. an explicitly
configured CalDAV/ICS cache in `settings.json`); which calendars; poll
cadence; auto-DND for all events vs. tagged ones; whether meeting start
drafts a journal entry.

**I3. Notification actions + inline reply.** `shell.qml` already enables
`actionsSupported`, and the API exposes `actions`,
`inlineReplySupported`/`sendInlineReply`, and `urgency`; today the popout
only fires the default action. Render action buttons, inline reply, and
urgency routing (critical banners, low history-only), plus shell-level
"→ TODO" / "→ journal" actions that open Quick Capture prefilled.

*Constraints:* notification payloads stay untrusted and bounded as
today; only existing action identifiers are `invoke()`d, never
constructed; reply text goes only to `sendInlineReply` and is not
persisted; critical urgency keeps banner behavior; history retains every
notification regardless of routing; the shell capture actions never write
directly.

*Open questions:* action overflow behavior; per-app vs. global urgency
policy and where it is configured; reply surface (popout vs. history
card); whether capture actions are banner buttons or a context menu.

**I4. Live session HUD.** Status: **removed (Phase 3,
unify-agent-slim-sessions L7).** Elapsed wall-clock time is never
surfaced as information, sessions are not managed objects, and the
session-log card it would open was deleted in Phase 2a. The bar badge
(count of sessions needing attention, on its own slower 60 s cadence
while the project state itself arrives on the resident stream) is the only session indicator. The notes below stay as
background only.

Make the collector visible: a bar chip with
the active work session's project, elapsed wall-clock time, and observed
change count; click opens the session-log card.

*Constraints:* read-only over the `current-context`/session helper APIs;
elapsed time derives from the stored `start_ms` and stays approximate by
design (never billable); "no session" is a first-class state; polling
merges with the existing project-module cadence instead of adding a new
forking loop; no store writes. The sessions badge already
refreshes on its own slower 60 s cadence while the project state itself
arrives on the resident stream,
so the HUD can follow the same pattern.

*Open questions:* expand `CurrentProjectModule` vs. a separate chip;
show change count, Pomodoro phase, or both; how it relates to the
roadmap day timeline ("now" vs. "the day").

**I5. Idle/lock-aware shell mode.** On idle or lock, auto-enable DND and
pause background polling/ticks; on resume, show a "welcome back" digest
(new notifications, finished agent runs, new captures).

*Constraints:* `IdleMonitor` and logind `LockedHint` are signals, not
guarantees — behavior fails to "no automatic change" when unavailable;
pausing covers polling and scheduler ticks only, never approval handling,
notification receipt, or bridge state; auto-DND must be distinguishable
from user DND so resume re-enables exactly what it disabled;
MemoryScheduler resume must not drop captures or re-run a day's review
(`last_review_day` semantics unchanged); the digest is UI-only
aggregation, no new store.

*Open questions:* idle timeout and whether it is configured in
`settings.json` or the control center; lock signal source
(`org.freedesktop.login1` `LockedHint` covers hyprlock; `WlSessionLock`
only if an in-shell lock ever exists); whether Pomodoro keeps running
while idle; digest contents.

**I6. Project scratchpad.** A dropdown terminal scoped to the current
project's authoritative `local_folder`, toggled from the bar or palette,
following the existing `special:journal` window-rule pattern.

*Constraints:* opens only for a registry project with a valid
`local_folder` (existing folder resolution and protected-path rules
apply); no command is ever auto-run; workspace/window rules must not
collide with `special:journal` or `special:evolution`; per-project vs.
one-shell-with-cwd is an explicit choice; closing the window is never
destructive.

*Open questions:* one class-per-project scratchpad vs. one shell updated
with cwd; bar indicator; special workspace vs. floating window; per
project vs. global palette toggle.

**I7. Shell writes ledger + revert.** One surface answering "what has
this shell written to my notes": every block filed through
`work_log.py`, `session_capture.py`, `journal_assistant.py`, or
`daily_review.py` carries a marker (`quickshell-worklog::`,
`quickshell-session::`/`quickshell-ref::`, `quickshell-review::<day>-<kind>`,
`quickshell-agenda::`). A `writes:` palette source plus a planner card
lists them (destination, kind, timestamp, session/project) with one
action: **Revert** — remove the marked block through the existing
prepare → exact preview → Confirm ladder with revision checks.

*Constraints:* the ledger is derived from markers in markdown and
rebuildable (L3; the `content_index.py rebuild` pattern) — no new
durable store, no user-authored content in the sidecar; revert removes
only the marked block (markers are property lines before the first child
bullet, so block boundaries are unambiguous), never surrounding user
text and never a non-shell block; one bounded graph scan per open,
`showing X of Y` when capped (D6); Revert is a write verb and gets its
own pinned entry in `tests/test_widget_controls.py` like **Save to
project**.

*Open questions:* scan-on-open vs. a small derived index table in
`content_index`'s style; whether reverting a `quickshell-worklog::`
block also clears `saved_page_ms` so the draft can be re-filed (yes, or
the double-save guard strands it); card home — planner Daily tab vs.
project overview.

**I8. Settings surface.** An in-shell editor for `settings.json` — the
`memory` block first (nine toggles plus `tickSeconds` / `minSessionMs`
/ `reviewTime` / `morningTime`) — replacing "edit the file and restart
the shell" for every value that does not need one. Listed here because
D4 kept a settings surface as "a roadmap candidate" and this file never
carried it.

*Constraints:* values are written back through the same validation and
clamp rules as `memory_settings()` (the UI cannot express an invalid
value; malformed still fails closed to defaults); keys that need a shell
restart (`enabled`, `tickSeconds`) say so instead of pretending; secrets
are never rendered or stored — presence only; shipping this reverses D4
(JSON-only by design) and the reversal is logged in `docs/streamlining.md`,
not applied silently.

*Open questions:* which keys beyond `memory` (`logseqGraph`,
`organise`); control-center card vs. planner tab; whole-file atomic
rewrite with stable key order vs. bounded merge; whether saves need the
prepare/preview/Confirm ladder or plain Save (settings are not graph
content).

### Polish — consistency and feel

**P1. Frecency for everything.** Usage-decay ranking for palette rows
across kinds, plus a "Recently used" empty state, so the palette learns
the user instead of staying a pure string matcher (`PaletteQuery.js`).

*Constraints:* local-only bounded store, no model calls, no clipboard or
agent content — identifiers and kinds only; frecency never outranks an
explicit prefix or exact match; a missing/corrupt store falls back to
today's string scoring exactly; decay math lives in a pure JS module
testable like the existing palette tests.

*Open questions:* storage location (palette-owned JSON vs. annotations
sidecar); which kinds participate; half-life; whether "Recently used"
needs its own prefix or shows on empty input.

**P2. Banners on the focused monitor.** The single notification banner
surface follows `Hyprland.focusedMonitor` instead of staying pinned to
the default screen.

*Constraints:* exactly one banner per notification remains invariant; no
duplicate surfaces on focus changes; re-anchoring uses the verified
`updateAnchor()`/TransformWatcher pattern and never steals keyboard
focus; focus changes while a banner is visible re-anchor without
replaying animations; falls back to current default-screen behavior when
Hyprland data is unavailable.

*Open questions:* follow focused monitor vs. pointer, and whether it is
configurable; whether history stays global (likely yes).

**P3. Keyboard grammar + cheat sheet.** A `?`-style overlay listing
palette prefixes, global shortcuts, popout keys, and wheel semantics;
plus a consistency pass (wheel on clock = day nav, workspace capsule =
workspace cycle, media = seek/volume).

*Constraints:* the overlay is documentation and must not drift — static
tables plus pointers that `.pi/README.md` and `docs/control-center.md`
stay authoritative; one key dismisses it; wheel consistency must respect
the documented bar scroller behavior; new controls carry Accessible
names, in line with `test_widget_controls.py`/`test_visual_style.py`
conventions.

*Open questions:* palette `?` mode vs. standalone overlay; curated vs.
auto-listed `GlobalShortcut`s; how far the wheel pass goes without
breaking existing scrollers.

**P4. Backend health card.** A diagnostics surface probing the bridge
binary, `pi`, `cliphist`, `tesseract`, the graph path, key presence, and
scheduler status, with the exact remediation command from the README per
row.

*Constraints:* probes run only while the surface is open, are bounded
and read-only — no auto-fixes, no network calls, no config writes;
secrets are never printed (presence of `OPENROUTER_API_KEY` only);
missing tools show the documented command; helper invocation is
list-form with validated paths, never interpolated shell.

*Open questions:* control-center card vs. palette `> diagnostics`;
required checks; whether to include bridge build info and scheduler
last-tick without exposing work-memory contents.

**P5. Power profiles + auto rules.** `powerprofilesctl` quick control in
the control center, with opt-in rules (battery → power-saver, AC →
balanced/performance) and a sticky manual override.

*Constraints:* no silent switching — the active profile is always
visible and manual choices win for the session; missing
`powerprofilesctl` degrades to a disabled/hidden control using the
brightness/night-light availability pattern; battery state comes from
UPower as today.

*Open questions:* auto rules default on/off; mapping; whether the rules
are configurable in `settings.json`; interaction with the existing Awake
toggle.

### Productivity — keyboard-first workflows

**W1. Window actions in palette rows.** `@` rows today only focus;
modifier actions make the palette a window manager: move to workspace,
float/pin, close, and `%` workspace rows to send the focused window.

*Constraints:* destructive close keeps the confirm-on-second-Enter
pattern; dispatch only through `Hyprland.dispatch` with validated
addresses/ids re-checked at action time (the palette already re-finds
windows by address for focus), never string-concatenated user text;
actions are discoverable in a footer/help; no new IPC surface.

*Open questions:* modifier map and Enter conflicts; whether `%` rows act
on the focused window; named/special workspace destinations; where the
legend lives.

**W2. Selection actions.** Act on the primary selection
(`wl-paste -p`): translate, summarize, explain, or send to the journal
with an exact preview.

*Constraints:* reading the selection happens only on explicit
invocation; selection text is untrusted and never auto-sent — hosted
model actions use the palette's existing explicit request path, and the
local password-like filter rule (same filter as session capture) applies
before any transfer; no background watching; bounded length; nothing persisted. If
the clipboard surface (above) ships first, this is a source inside it,
not a second UI.

*Open questions:* palette prefix vs. global hotkey; reuse the
`screen-translation` skill vs. an inline prompt; result to clipboard vs.
result card.

**W3. Daily triage mode.** A keyboard-driven pass over open TODOs,
captures, and review top-3: `j`/`k` navigation, schedule today/tomorrow,
snooze/drop, and start a Pomodoro on the selected task.

*Constraints:* scheduling uses the existing agenda `select` and capture
status operations with revision checks; "drop" is local dismissal/snooze,
never task-text deletion — project pages remain the source of truth;
triage state is ephemeral; lists keep existing caps and surface
truncation; no auto-scheduling, no model calls.

*Open questions:* entry point (Daily tab, palette mode, overlay); snooze
vs. hide-for-today; whether Pomodoro associates with the selected task;
how much of the (now implemented) Review card flow this duplicates.

**W4. Clock-time reminders.** `remind me at 14:00 …` in the palette,
fired by a resident tick, with optional journal line on completion.

*Constraints:* new bounded sidecar with a documented schema, fail-closed
parsing (an unparsable time creates nothing); one resident tick following
`MemoryScheduler` conventions (opt-in, bounded frequency, errors never
surfaced); missed reminders surface once as missed after downtime, never
as a catch-up storm; reminder text is bounded user input; no model calls;
no Logseq page-schema changes.

*Open questions:* dedicated sidecar vs. an `annotations.db` table;
deterministic local parsing only (no model); snooze semantics; whether
timers fire while idle; future agenda times (would need a new page
property — out of scope until decided).

**W5. Zotero in the palette.** A `zot:` source for library search with
copy-citation, open-PDF, and "discuss in project" actions. The planner
has a library picker; the palette cannot search it.

*Constraints:* read-only over `scripts/zotero.py` under the existing
`docs/zotero.md` bounds; item metadata is untrusted text; no downloads
beyond what the helper already permits; project handoff reuses the
existing planner/agent handoff, no new context-injection path; an
unavailable library shows an explicit error row; bounded results/query
per palette quotas.

*Open questions:* row fields; citation format(s); refresh/caching of the
library index; whether items can be referenced into a project note via
existing file tools or only into chat.

**W6. Standup digest.** `> standup` composes the recent sessions,
today's agenda, and repository changes into a copyable update.

*Constraints:* deterministic first (session store + agenda +
`project_session_changes`), with any Pi rewrite opt-in and previewable
before anything is kept; missing evidence is reported as unavailable,
never invented (`project_recap.py` precedent); copy-only by default, no
auto-posting, no project writes; bounded length.

*Open questions:* window (yesterday vs. last 24h); current project vs.
all projects; save-to-journal via the existing review save flow vs.
copy-only; how to avoid duplicating the Review card flow.

## Opencode sessions ↔ desktop sessions — association, inspection, ControlCenter prompt

Status (Sep 2026): **removed (Phase 3, unify-agent-slim-sessions L2
+ L7).** The deterministic join's Jev advisory scorer is deleted with
the Jev path; the `session_links` `agent_session` kind has no opencode
producer; sessions are not managed objects, so there is nothing to
attach a prompt surface to. The notes below stay as background only:
the two namespaces still never meet. There is no opencode helper
(`scripts/opencode_sessions.py` was never built) and no ControlCenter
prompt surface.

**Background only:** the removed proposal was a bounded, opt-in
bridge in three parts — (1) association of desktop sessions to
opencode sessions via deterministic join (project UUID /
`local_folder` cwd / git remote overlap + time overlap) with Jev only
as an advisory scorer where ambiguous; (2) inspection of linked
opencode sessions from the session surfaces as read-only metadata
with click-through to a bounded transcript preview; (3) a
ControlCenter tab/section listing linked + active opencode sessions
with an explicit prompt box. All three are moot: (1) needs the
deleted Jev scorer and a producer that never existed, (2) cites
surfaces that no longer exist (ledger card rows, `inbox:` rows,
overview Ledger card, day timeline), and (3) is a new surface for
moot linking. Kept policy, if any bridge is ever reconsidered:
collector stays deterministic and local, attribution stays
authoritative with explicit Confirm/Dismiss, no auto-linking, no
silent writes, managed-service access only with fail-soft
"unavailable", read-only first, prompt send preview-first and
explicit.
