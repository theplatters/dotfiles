# Streamlining

Status (Sep 2026): **active workstream — seeded by a six-part baseline
audit.** One coherence pass over the desktop environment. The shell has
accreted surfaces (bar modules, popouts, ControlCenter tabs, palette
modes, planner tabs) faster than it has settled on shared rules; this
workstream fixes that with a running report → fix loop. This file is the
ledger of findings and the record of the rules they are fixed against.
It is not a roadmap: unimplemented ideas stay in `docs/roadmap.md`.

Raw audit reports (bar, popouts/CC, palette, planner/daily,
cross-cutting) are staged at `/tmp/opencode/streamlining-audit/` for the
fix phase; ask if they should be committed instead.

## How the loop works

Report anything, in any format — a vague feeling is enough ("the
calendar popout feels cramped", "why does the wheel do nothing here?").
Reports are triaged into one of three lanes:

- **A — quick fix.** Local, unambiguous, no behavior decision: fixed
  directly, tests run, row closed with the commit.
- **B — decision.** The fix needs an intended behavior/look or removes a
  surface: 2–3 options with a recommendation, then it lands as a quick
  fix.
- **C — class.** The report exposes a pattern across surfaces: logged and
  folded into a themed pass so the class is fixed once, not symptom by
  symptom.

Structural changes (merging/removing/moving tabs or features) are
proposed before implementation and need explicit approval. Fixes that
only enforce an already-decided rule land directly. No new features
unless removing an inconsistency requires one.

Definition of done for a finding: the rule it violates (or the chosen
rule) is stated; the change is in code; `PYTHONDONTWRITEBYTECODE=1
python3 -m unittest discover -s tests` passes; the owning doc is updated
when behavior changes; the surface has been eyeballed in the running
shell; the row is closed with the commit.

## Coherence dimensions

1. **Visual language** — Theme tokens are the single source for color,
   radii, type, and motion; spacing, density, iconography, and accent
   usage are consistent across surfaces.
2. **Interaction grammar** — the same gesture means the same thing
   everywhere: Enter/Escape/arrows/wheel, click-outside, hover,
   focus/keyboard parity, disclosed shortcuts.
3. **Surface ownership (IA)** — each piece of information and each
   control has exactly one home; bar space and tab counts are earned;
   no duplicated or orphaned affordances.
4. **State semantics** — DND vs notifications vs approvals, feature
   toggles, polling/ticks, and failure behavior follow one documented
   rule per concern.
5. **Implementation health** — no dead code or dead chords, docs match
   behavior, tests pin the invariants, no conflicting handlers.

## Severity

- **S1 — broken:** doesn't work, overlaps, or is unreachable.
- **S2 — incoherent:** works, but violates a rule or duplicates another
  surface.
- **S3 — polish:** spacing, wording, alignment, motion.

## Findings

Statuses: `triaged → fixing → fixed → verified`, plus `decision-needed`
(lane B), `pass-candidate` (lane C), `parked`, `declined`. Area tags:
bar, popout, cc, palette, planner, state, impl.

The S-045–S-064 batch was filed by the Sep 2026 manifesto audit
(`docs/manifesto.md`); each row cites the principle it violates.

| ID | Area | Symptom | Rule / expected | Sev | Lane | Status |
|----|------|---------|-----------------|-----|------|--------|
| S-001 | bar | Media capsule transports: the fill-level toggle MouseArea covers MediaModule's transport/title hit targets, so clicks there do not act on the control under the cursor (audit: toggles the popout). | One click owner per region: transports act, capsule body toggles. | S1 | A | verified |
| S-002 | bar/state | Notification history is a per-bar ListModel; bells on different monitors diverge in count and dismiss state. | One history store at shell level; per-bar bells are views. | S1 | A | verified |
| S-003 | bar | Stats capsule highlights on hover but has no click action. | Real button or no affordance (stats popout is roadmap). | S1 | A | verified |
| S-004 | bar/cc | Legacy Audio/Network/Battery popups, their Bar props, and the battery click fallback are unreachable; battery click silently no-ops; BatteryPopup tooltip/Power card are separate code. | One wired path per control; delete unreachable branches. | S2 | A | verified |
| S-005 | impl/state | Poller proliferation: 3 stat forks + project poll per bar (multiplied per monitor, no visibility gate), 3 clock timers, ledger reads from 3 surfaces (75 s loops in overview). | One gated shell-level poller per concern; consumers bind. | S2 | A | verified (single owner; gating follow-up S-053/S-054 — done) |
| S-006 | popout | Popouts do not close each other; two surfaces can stack. | Exclusive-open per screen. | S2 | C | verified |
| S-007 | popout | Escape closes only ControlCenter; other dismissible popouts ignore it. | Escape closes every dismissible popout. | S2 | C | verified |
| S-008 | popout | Click-outside dismisses only the Wi-Fi password dialog. | One outside-click rule for anchored popouts. | S2 | C | verified |
| S-009 | popout | MediaPopout anchors from a show-time global rect; docs specify item-relative anchors with updateAnchor tracking. | One anchor grammar (docs/control-center.md). | S2 | A | verified |
| S-010 | popout | PasswordPopup is shell-global while sibling popouts are per-screen; mid-close reopen flashes from zero on several popouts. | Per-screen or explicit screen-follow; reversible motion. | S2 | C | verified |
| S-011 | cc | ControlCenter and NetworkPanel cross-assign selectedTab/activeTab in both directions; tab clicks use two different paths; the hidden inner selector stays live. | One-way routing: CC owns selectedTab, panel is slave; one click path. | S2 | A | verified |
| S-012 | cc | Bluetooth is reachable only after entering CC through Network; no bar entry exists; entry points are undocumented. | Every tab reachable from its entry point or documented as second-level. | S2 | B | verified |
| S-013 | cc | Same control families differ across CC/panels: native Slider vs custom switch, two tab visual languages. | One component per family (tabs, switches, sliders). | S2 | C | pass-candidate |
| S-014 | popout/cc | Ownership split (bell = history, CC = DND intent, banner = ephemeral toast outside the popout lifecycle) is undocumented. | Document the split in docs/control-center.md. | S2 | A | verified |
| S-015 | bar | Media full mode is blocked below 1400 px even with free space; collapse thresholds (1400/1200/1150/1000) are undocumented and can drop the SSID early. | Drive compactness from measured budget; document collapse order. | S2 | A | verified |
| S-016 | bar/visual | Audio visualizer is the only colored surface (#a6e3a1), radius 2, five 100 ms timers; click fakes play state; comment says 50 ms. | Tokenize, drive from real Mpris state, one timer. | S2 | A | verified (dead component remains — S-061) |
| S-017 | bar | Two screenshot owners: bar right-click = output only, palette = region/output/window with unmap delay; window capture is bar-invisible. | One owner; bar routes through the palette helper. | S2 | A | verified |
| S-018 | bar | Tray modules each have a different subset of hover/wheel/tooltip/keyboard behavior. | One interaction rule for every clickable element. | S2 | C | pass-candidate |
| S-019 | palette | Three "session" concepts (Pi session switch vs ledger `session:`/`inbox:` vs activity `hist:`) and hist/session rows are identical but read different stores. | One name per concept; rows state their source. | S2 | C | verified |
| S-020 | palette | "Resume" means execute-with-confirm in `resume:` but handoff elsewhere; Enter has ~7 meanings; close-vs-stay is unpredictable; footer claims a single "run". | One verb grammar: labelled primary action, consistent close/stay. | S2 | C | verified |
| S-021 | palette | Three confirm dialects (bare second-Enter, checkbox overlay, approval Reject/Accept); Esc rejects while backdrop-dismiss defers. | One confirm pattern; dismiss gestures labelled by effect. | S2 | C | verified |
| S-022 | palette | Limits, timeouts, error rows, bare-prefix behavior, and per-kind action sets differ per source with no visible logic. | One source convention: caps reported, one error row + Retry, one empty-query rule, full action set per kind. | S2 | C | verified — truncation follow-up in S-041 |
| S-023 | palette | Capture overlay does not say which action is armed; save-screenshot and capture-for-Pi share one list. | Show the armed action; group by intent. | S2 | A | verified |
| S-024 | palette | Approval dialog raw-JSON dumps arguments with generic titles. | Same bounded exact-preview + consequence line as planner/journal. | S2 | A | verified (palette only; planner parity open — S-059) |
| S-025 | palette | Arithmetic-looking queries silently suppress all file rows. | Never suppress a source; rank calculator first instead. | S2 | A | verified |
| S-026 | planner | The full daily workflow is instantiated in both CalendarPopout and the planner Daily tab. | One canonical home or guaranteed-identical instantiations, documented. | S2 | A | verified |
| S-027 | planner | Overview Ledger card duplicates LedgerCard rows (different window, read-only, different empty text). | One owner, or state the window in the header. | S2 | A | verified |
| S-028 | planner | Ledger week rollup lives in the Review card's third tab. | Week rollup owned by the ledger surface. | S2 | A | verified |
| S-029 | planner/copy | Filing verbs differ (`Save to…` vs `File…`) and Saved/filed/Filed statuses overload; session vocabulary drifts across UI and docs. | One verb + glossary: work session → ledger entry → draft → filed. | S2 | C | verified |
| S-030 | planner/visual | Card chrome differs: headers, preview panels, empty/loading/refresh/truncation wording. | One card convention with shared preview-panel component. | S2 | C | verified |
| S-031 | planner/interaction | Session controls differ (menu vs three buttons); Dismiss/Ignore/Restore overloaded; fixed-height inner lists inside outer Flickables. | One control and one list rule per job. | S2 | C | verified |
| S-032 | impl/visual | Scrim color `#B0070707` duplicated in four modal backdrops. | Add `Theme.scrim` and use it. | S2 | A | verified |
| S-033 | impl/visual | Chip/badge radii 6/7/4/2 plus a literal controlRadius value; 18 px glyphs vs iconSize 22; literal "transparent" fills. | Token-only sizes, radii, colors. | S2 | A | verified |
| S-034 | impl/visual | Motion literals 150/160/200/260 and a one-off easing outside Theme tokens. | Motion tokens with one easing per gesture. | S2 | A | verified |
| S-035 | impl | test_visual_style / test_widget_controls pin exact source strings, so copy/layout passes break them. | Re-pin on objectNames/behavior before the copy passes. | S2 | A | verified |
| S-036 | impl | Dead surfaces ship: HoverModule, PowerlineArrow; `Theme.radius` alias is load-bearing; `Theme.transparent` unused. | Delete dead surfaces and aliases. | S3 | A | verified |
| S-037 | impl/docs | Palette docs drift: stale F2 comment, README mis-maps `@`/`%`/`+`, `resume:`/`project:` undocumented, no help mode, dead screenshot-region branch. | One true prefix table + help; delete the dead branch. | S3 | A | verified |
| S-038 | impl/state | Background failures surfaced inconsistently: MemoryScheduler ticks never surfaced errors while other background failures showed inline. | One background-failure policy (D5): auto ticks never surface errors (fail soft, retry next tick); manual actions show an inline bounded error on the owning card, never modal. | S2 | B | verified (Phase 3: MemoryScheduler fails soft with `dataChanged` only on `changed`; DailyAgenda error props are inline card text; no modal path) |
| S-039 | impl | `mainMod+C` (`quickshell:clipboard`) chord is dead; no shortcut exists. | Add the surface or release the chord. | S2 | B | parked (roadmap) |
| S-040 | impl/IA | Nine memory settings toggles are JSON-only and invisible in the shell. | Surface toggles or document JSON-only by design. | S2 | A | verified |
| S-041 | palette/impl | Capped palette sources (resume/hist/session) cannot report truncation: the Python helpers expose no truncated/total signal. | Every capped source reports truncation (S-022 rule). Phase 3: `sessions.py search` returns `{total, truncated}`, `list_captures` returns `total`, `seen` returns `{rows, truncated, total}`; SessionCard/CaptureInbox/Review render `showing X of Y` (probe-limited bare-`seen:` totals render `Y+`). | S3 | B | verified |
| S-042 | popout | Bell popup (`expanded` state) does not participate in exclusive-open; clicking the bell while another bar popout is open stacks two surfaces. | Every dropdown surface follows the same exclusive-open rule (D1). | S3 | A | reported |
| S-043 | bar | Live-report follow-up: clock clicks sometimes do not complete (2 of 4 presses during a live test produced no `clicked` event), so the calendar popup does not open on the first click; handler itself runs in ~80 ms when it fires. | One click = one toggle; hit target must reliably deliver. | S2 | B | investigating |
| S-044 | planner/daily | Calendar/palette/planner content is cold after a hot reload or shell start: the panel shows immediately but its data (agenda, captures, review, sessions) waits on Python helper round-trips; measured 0.35 s warm, up to ~1.5 s cold, and concurrent reloads make it worse. | Show last-known content immediately, refresh in the background (stale-while-revalidate); only show loading when there is no data yet. | S2 | B | proposed |
| S-045 | palette | `seen:` **Add TODO here** pins the resource ref but drops the row's `project_id`, so a resource observed in project B becomes a TODO on the current project A (misattribution by construction). | Manifesto §1: capture lands in the right destination; carry the row's project as the target. | S2 | A | verified (TODO carries the row's project; single-use pin; behavioural tests) |
| S-046 | palette/planner | `todo:` and the planner quick-add have no project target (current project else today's journal), and the planner's selected project is explicitly not carried; capturing into another project requires making it current by desktop evidence first. | Manifesto §1: capture is one gesture to the right destination; decide picker vs. carried selection vs. documented current-project-only. | S2 | B | triaged (D7 adopted; fix pending) |
| S-047 | bar/planner | The needs-attention badge counts cross-project sessions, but the capsule click opens the current project's overview, which has no attention list or link; the planner Daily tab is an undisclosed second step. | Manifesto §1: navigation never dead-ends; the badge click should land on the attention list. | S2 | B | triaged (D8 adopted; fix pending) |
| S-048 | state | Approval copy promises "closing the palette defers the request — it reopens safely", but the bridge expires approvals at min(timeout, 120 s) and cancels them; a pending approval also blocks leaving the planner/journal until Stop. | Manifesto §2: nothing waits on me without a safe way to defer it. Make deferral real (renew while surfaced), drop the expiry for UI-held requests, or stop promising it. | S2 | B | verified (real deferral: surface/defer ops, fail-closed only for un-surfaced, round-robin re-surface, overflow answered cancelled, navigation unblocked; copy literal) |
| S-049 | state | The approval queue silently ignores requests past 32 pending (not stored, not answered; Pi left hanging until its own timeout) and shows no backing-up indicator. | Manifesto §2: the assistant never silently loses my decisions; surface queue pressure. | S2 | B | fixing (overflow answered cancelled shipped with S-048 batch; queue-depth indicator pending — D9) |
| S-050 | palette/planner/state | The classifier tier is empty: review top-3 truncates deterministically, capture screening is regex-only, notification triage and bare-text routing have no path, Zotero screening is unimplemented. | Manifesto §3b: predefined-outcome non-deterministic decisions belong to a classifier; ship through the bounded opt-in Jev batch (roadmap) or decline each explicitly. | S2 | C | pass-candidate |
| S-051 | pi/impl | History time resolution ("yesterday", "around 14:00" → epoch-ms) is delegated to the LLM even though the roadmap's own rule says arithmetic, counting, and date math stay in code. | Manifesto §3a; resolve ranges in a helper or accept bounded ISO ranges. | S2 | B | triaged (D10 adopted; fix pending) |
| S-052 | pi/planner | Agenda candidate matching is delegated to the LLM over a predefined candidate list; the choice shape fits a classifier. | Manifesto §3b: choice over a predefined set belongs to a classifier, deterministic fallback preserved. | S2 | B | triaged (D11 carve-out adopted; Jev-batch migration item) |
| S-053 | bar | SystemStats forks ~9 processes per 5 s tick forever (sh+free+awk, sh+top+grep+awk, sh+python3) with no visibility/idle gate; S-005 consolidated per-monitor copies but did not gate or de-fork. | Manifesto §5: no per-tick forks a single sampler can own; the roadmap already calls this a performance requirement, not a preference. | S2 | A | verified (native /proc sampling for mem/cpu; one disk fork per 60 s; live-shell glance pending) |
| S-054 | state | CurrentProjectSource forks python3 + a Rust compositor snapshot every 3 s plus a badge fork every 60 s, unconditionally; no idle/lock gate exists (I5 unscheduled). | Manifesto §5: poll only what is visible or needed; gate on idle/lock or move to the resident bridge. | S2 | A | verified (resident `desktop_projects.py watch` stream; zero forks in steady state; 60 s badge fork remains — S-065) |
| S-055 | planner/state | Hidden daily cards (CaptureInbox, ReviewCard, SessionCard, UnmappedFoldersCard) reload their backend from `Component.onCompleted` without a visible guard, per monitor at shell start. | Manifesto §5: no backend work for surfaces nobody sees; gate on visible (the sibling handlers already do). | S2 | A | verified (onCompleted reloads gated on visible) |
| S-056 | palette/impl | Unified palette scoring runs over the entire uncapped `cliphist` output (full values as keywords) and all agent messages on every keystroke; cost scales with total source text, not with the 40 rows shown. | Manifesto §5: bounded per-keystroke work; cap the source list, score snippets, cache. | S2 | A | verified (source/snippet caps + per-rebuild match cache; leading-snippet depth trade-off documented) |
| S-057 | impl/state | Palette and planner each own an AmbientContext resolver forking the same three Python helpers. | Manifesto §4/§5: one owner per concern (S-005 pattern); share one source. | S2 | A | verified (single shell-level AmbientContext, scope-tagged blocks, no boot forks) |
| S-058 | impl/visual | The shared `WidgetButton` is bypassed by ~37 raw `Button`s (PasswordPopup, ProjectPlanner, JournalAssistant, CommandPalette, PaletteApprovalDialog) that re-implement the same recipe without pressed/focus/disabled parity. | Manifesto §4: one component per control family. | S2 | C | pass-candidate |
| S-059 | planner/impl | The planner approval dialog duplicates the palette's logic and lacks the consequence/destination lines, so the same request renders differently per surface (S-024 fixed only the palette). | Manifesto §4: one preview/approval grammar. | S2 | A | verified (shared ApprovalGrammar.js; planner parity incl. consequence/destination + Defer) |
| S-060 | impl/visual | Bar/ControlCenter text never sets `font.family` while card/palette text does, and 167 literal `font.pixelSize` values remain with no type tokens; with Inter installed the layers can render different typefaces. | Manifesto §4: one visual language; add type tokens and opt the old layer in. | S2 | C | pass-candidate |
| S-061 | impl | Residual token drift after S-033/034/036: literal radii (NotificationPopout 12, TrayModule 8/1, AudioPanel 4, MediaPopout 4), 26 literal "transparent" fills, unused Theme aliases (blue/pink/teal/base3), dead `AudioDisplay.qml` with a one-off `Easing.InOutQuad`. | Manifesto §4: token-only sizes/colors/motion; delete dead surfaces. | S3 | A | verified (aliases deleted; radii tokenised/pill; 26 transparent literals tokenised; AudioDisplay deleted) |
| S-062 | planner/impl | The quick-add TODO write ladder is implemented twice (DailyPlanner and CommandPalette) with parallel Process/state machines. | Manifesto §4: one implementation per flow. | S2 | C | pass-candidate |
| S-063 | impl/docs | Doc drift from the manifesto audit: control-center.md keeps the superseded bell-ListModel note before correcting itself; daily-agenda.md references removed week-rollup headers; ScreenshotAction.qml says the bar may consume it "in a later wave" though ScreenshotModule already does; README/roadmap claim the badge reuses the project poll cadence while it has its own 60 s timer. | Manifesto §4: docs match behavior. | S3 | A | verified (docs/comment drift fixed) |
| S-064 | impl/ledger | Several `verified` rows are partial or checked at one call site: S-021 (three confirm states), S-024 (palette only), S-028 (rollup deleted, not moved), S-033/34/35 (literals/easing/pins remain), S-037 (no help mode), S-005 (gating), S-016 (dead component). | Manifesto §4: verification means the stated rule holds across surfaces; re-verify or reopen. | S2 | B | triaged (D12 adopted; re-verify pass pending) |
| S-065 | impl/state | The session badge still forks `python3 scripts/sessions.py inbox` unconditionally every 60 s (the last per-tick fork in `widgets/CurrentProjectSource.qml`'s orbit; S-054 moved only the project state into the resident stream). | Manifesto §5: no per-tick forks a single sampler can own; `sessions.py`'s inbox query is in-process computable (pure SQLite read over the session ledger, zero subprocesses) — fold it into the resident `desktop_projects.py watch` stream (or an in-process tick of it) so the badge follows the same zero-fork pipeline. | S3 | A | reported |

## Decision log

Rules chosen during the workstream that findings are judged against.

- **D1 (adopted)** — Popout grammar and dead-surface cleanup:
  exclusive-open per screen; Escape closes every dismissible popout;
  click-outside closes anchored popouts; one item-relative anchor grammar
  with `updateAnchor`; delete legacy popups/props, HoverModule,
  PowerlineArrow, and dead aliases; the bar routes screenshots through the
  palette helper. Delete, do not deprecate.
- **D2 (adopted)** — Palette grammar: one name per session concept
  (`>` = Pi session, `session:`/`inbox:` = work ledger, `hist:` = desktop
  activity); one Resume verb with preview-before-execute; one confirm
  pattern; dismiss gestures labelled by effect (reject vs defer). Both
  backends stay.
- **D3 (adopted)** — Planner/daily IA: both daily instantiations stay as
  deliberate mirrors with a parity guard; week rollup moves to the ledger
  surface; the overview ledger card is de-duplicated; the mirror model is
  documented.
- **D4 (adopted)** — Settings: the `memory` block stays JSON-only by
  design; document it and keep a settings surface as a roadmap candidate.
- **D5 (adopted, Phase 3)** — Background-failure policy (closes S-038):
  background jobs are all local; auto ticks never surface errors (fail
  soft with a bounded reason, retry next tick, `dataChanged` only on
  `changed`); manual actions show an inline bounded error on the owning
  card, never modal. There is no modal error path in the tick, the
  scheduler, or the daily cards.
- **D6 (adopted, Phase 3)** — Truncation pattern (closes S-041): every
  capped list reports `showing X of Y`, silent unless actually capped.
  Probe-limited totals (bare `seen:`) render `Y+`. Card headers always
  show the count (`Sessions · day (N)`), never conditionally.
- **D7 (adopted)** — TODO destination (decides S-046): the planner
  quick-add carries the planner's selected project as its target
  (logseq-linkage gate kept, today's-journal fallback unchanged);
  `todo:` keeps the current-project default; the destination is always
  labelled in the composer/confirm; the `seen:` pin stays the
  single-use cross-project override. No picker step.
- **D8 (adopted)** — Badge routing (decides S-047): the needs-attention
  badge owns its click and lands on the planner Daily tab's SessionCard
  — the attention list the number counts; a tooltip names it ("N
  sessions need attention"). Data-source ride-along candidate: S-065.
- **D9 (adopted)** — Approval queue pressure (decides S-049): overflow
  policy ratified — past 32 pending the incoming request is answered
  `cancelled` with a diagnostic (fail-closed, cap stays 32; shipped with
  the S-048 batch). Queue depth becomes visible: `deferredCount`
  renders as a bounded "N queued" in the approval surfaces, silent
  unless backed up (D6 style). No eviction of parked requests.
- **D10 (adopted)** — History time resolution (decides S-051): a
  deterministic resolver over a closed period vocabulary (`today`,
  `yesterday`, `this-week`, …, plus ISO date/range escape hatch)
  resolves `[fromMs, toMs)` in the Pi extension layer; the LLM selects
  the period token and never performs date math (Manifesto §3a).
- **D11 (adopted)** — Agenda candidate matching (decides S-052): stays
  LLM over the predefined `logseq_agenda_list` candidates behind its
  existing guards (exact path/line/revision identity validation,
  ask-before-write, never-guess); documented as the standing §3b
  exception and scoped as a named migration item into the S-050
  classifier batch.
- **D12 (adopted)** — Verification scope (decides S-064): a bounded
  re-verify pass over the cited partial rows (S-021, S-024, S-028,
  S-033/34/35, S-037, S-005, S-016) with surface-by-surface evidence;
  rows already covered by Wave 6 (S-005, S-016, S-024) close with a
  note; the rest are re-checked and either closed honestly (the observed
  behavior accepted as the decision, stated) or reopened.

## Phase 3 overflow/layout audit (§6.2 findings log)

Measured against the P4 card convention (`docs/daily-agenda.md:207`).
Violations found and fixed in the same commit:

- **SessionCard header count was conditional** — it showed a count
  only when the pending-marker helper returned > 0, so the header
  flickered between `Sessions · day` and `Sessions · day (N)`. Now
  always `Sessions · day (N)` over the rendered rows.
- **Resume lived where rows live** — it duplicated palette `resume:`
  and the overview Resume button. Now a single header **Resume**
  (`sessionResumeButton` → `headerResumeProject()` →
  `openProjectRequested`), never in rows; navigation only, never a
  write.
- **Three day lists capped silently** — the session day list (client
  cap 50), the capture list (cap 20), and the review top-3 (cap 3)
  had no total behind them. Backends now report totals
  (`agenda.ledgerTotal`, `agenda.captureTotal`, pre-slice
  `top_total`) and the cards render `showing X of Y` lines
  (`sessionTruncation` / `captureTruncation` / `reviewTruncation`),
  silent unless `total > shown`.
- **Two palette sources capped silently** — `seen:` and `session:`
  exposed no truncated/total signal (the S-041 symptom). Both
  backends report it now (`storeSeenTruncation` /
  `storeSessionTruncation`); the palette renders the same
  `showing X of Y` pattern, with `Y+` when the bare-`seen:` total is
  probe-limited (at-or-below shown while truncated).
- **Review body rendered raw markdown as plain text** —
  `ReviewCard.qml` showed `entry.markdown` via `Text.PlainText`.
  Generated bodies now render through shared
  `widgets/MarkdownBody.qml` (`MarkdownText`, wrap, 12-line bound +
  elide, theme tokens only). Deliberate non-fix: `PreviewPanel`
  stays `PlainText` + monospace (exact-preview invariant — Confirm
  shows the exact bytes), and user-authored text (capture rows,
  thought editor, journal composer) stays `PlainText`
  (untrusted-input rule).
- **Verb drift** — the file Confirm now reads **Save to project**
  (single target, L1) via `PreviewPanel.confirmText`; journal and
  organise previews keep generic **Confirm**. Two carve-outs stay
  and are test-pinned: Review **Save to journal** (L1) and
  attribution **Add to project…** / **Ignore**. Every card control
  carries `Accessible.name` + `Accessible.description`.
- **Bounds kept** — scheduled 168, picker 168, captured 148, review
  top 132, sessions 280; no unbounded body remains
  (`MarkdownBody.bodyMaxLines` 12). No retune was needed.

## Open questions

Undecided, to be brought up when relevant: S-012 (Bluetooth bar entry),
S-039 (clipboard chord, parked for the roadmap).

S-046/S-047/S-049/S-051/S-052/S-064 resolved — D7–D12 adopted Sep
2026; fixes pending.
S-048 (approval deferral) resolved — real deferral chosen and shipped
Sep 2026.

## Current plan

- **Wave 1 (done, verified)** — token foundation and test re-pin (S-032
  to S-035); docs for popout/notification ownership and JSON-only
  settings (S-014, S-040).
- **Wave 2 (done, verified)** — bar/shell layer (S-001 to S-005, S-015,
  S-016); palette pass P1 (S-019 to S-025, S-037); planner ownership
  moves (S-026 to S-028).
- **Wave 3 (done, verified)** — popout grammar P2 and CC routing (S-006
  to S-012); planner card conventions P4 (S-029 to S-031); bar wiring
  for S-017.
- **Wave 4 (candidates)** — S-013 (CC control components), S-018 (tray
  interaction grammar), S-042 (bell popout exclusive-open), plus live
  reports.
- **Wave 5 (done, verified, Phase 3)** — card polish: S-038
  (background-failure policy D5) and S-041 (truncation reporting D6)
  closed; §6.2 overflow/layout audit logged above. S-042/S-043/S-044
  left as-is (not observed again).
- **Wave 6 (in progress, Sep 2026)** — manifesto audit batch S-045–S-065:
  lane A rows (S-045, S-053–S-057, S-059, S-061, S-063) and S-048
  (real deferral) implemented and verified Sep 2026; lane B decisions
  made (D7–D12); remaining: the lane B fixes and the S-064 re-verify
  pass, the lane C passes (S-050, S-058, S-060, S-062), plus the badge
  row S-065.

## Themed passes

- **P0 (done)** — Re-pin string-pinned tests (S-035).
- **P1 (done)** — Palette grammar and source conventions (S-019 to S-025, S-037).
- **P2 (done)** — Popout grammar (S-006 to S-010).
- **P3 (partial)** — Bar interaction grammar and budget: S-015 done;
  S-018 (tray module grammar) pending.
- **P4 (done)** — Planner card conventions (S-029 to S-031).
- **P5 (planned)** — ControlCenter control components (S-013).
