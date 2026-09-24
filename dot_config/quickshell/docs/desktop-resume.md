# Desktop Resume backend (deterministic, Phase 6)

Deterministic per-project Resume backend: builds an inspectable
`ResumePlan v1` from the current sources of truth and optionally executes
only backend-defined typed operations. No LLM, no embeddings, no inference,
no sync. Planning performs no desktop actions and no project/repository
writes; execution spawns only fixed backend-built argv.

Related docs: `docs/desktop-project-context.md` (attribution contract),
`docs/desktop-work-sessions.md` (sessionization),
`docs/desktop-history-search.md` (session-centric retrieval),
`docs/project-planner.md` (planner-facing summary).

Source files: `scripts/desktop_resume.py` (backend), `scripts/desktop_projects.py`
(`device-id` wrapper), `services/agent-orchestrator/src/bin/qs-desktop-context.rs`
(`device-id` query), `widgets/CommandPalette.qml` and
`widgets/PaletteDataSources.qml` (preview/execution UI),
`widgets/ProjectPlanner.qml` (project/Pi handoff), and
`.pi/extensions/desktop-agent.ts` (read-only plan tool).

`open_project_agent` is delegated, never directly executed by the backend:
Quickshell hands it off to ProjectPlanner, which safely resumes the latest
scoped Pi session or starts a new scoped session when none exists. Pi exposes
plan inspection only; no model-callable Resume execution tool exists.

## CLI

Diagnostics to stderr; stdout carries ONLY JSON (single-line `error:`,
exit 1, no traceback). Global options mirror the existing infrastructure:
`--desktop-bin`, `--db`, `--projects-file`, `--graph`.

```sh
python3 scripts/desktop_resume.py list [--query TEXT] [--limit N]
python3 scripts/desktop_resume.py plan --project ID_OR_NAME
python3 scripts/desktop_resume.py execute --project ID_OR_NAME [--operations CSV]
```

- `list` prints `{query, limit, count, entries}` with bounded entries
  (`{id, name, logseq_path, local_folder, github_url, match}`) for palette
  selection/preview. `--query` is trimmed `1..256` chars; `--limit` is
  `1..100` (default 20). Ranking is deterministic: `exact`, then `prefix`,
  then `substring`, then fuzzy ordered-subsequence, ties by
  `(name.casefold(), id)`. No query lists every project sorted
  deterministically by `(name.casefold(), id)`, not registry order.
- `plan --project ID_OR_NAME` prints one `ResumePlan v1` object. No desktop
  actions, no project/repository writes. `build_resume_plan` is pure: it
  never probes `PATH` and canonicalizes an injected boolean availability
  map (omitted means all unavailable); `plan_for_project` performs the live
  probe and passes it explicitly.
- `execute --project ID_OR_NAME [--operations CSV]` rebuilds a fresh plan
  by stable registry identity (the caller never supplies paths/commands or
  a serialized plan), runs the selected backend-defined operations, and
  prints `{project, operations, count, results}` with one per-operation
  result (`ok` / `launched` / `partial` / `skipped` / `failed` /
  `delegated`). `ok` means a bounded checked action completed (workspace
  focus); `launched` means a detached GUI spawn (kitty, `xdg-open`)
  succeeded without confirmation that the app stays running (external-app
  caveat: detached launches are unconfirmed, only immediate spawn or
  handler failures are reported); `partial` means the editor launched with
  a subset of files plus `skipped` details; `delegated` is the Pi handoff.
  Failures are caught per operation (and per file inside the editor
  revalidation) and execution continues. `--operations` is a CSV subset of
  the fixed allowlist validated before planning (unknown kinds fail fast).

Name matching is deterministic and shared by `plan`/`execute`:

1. UUID exact (canonical, case-insensitive).
2. Name exact (casefold) — wins even when other names share the prefix.
3. Unique prefix (casefold) — e.g. `resume BeforeIT` resolves uniquely to
   the current `BeforeIT ECS-Rewrite` registry entry.
4. Unique substring (casefold).

Unknown or ambiguous identifiers are rejected, never guessed. Fuzzy
(subsequence) matches appear in `list` ranking only; `plan`/`execute`
reject them as unknown.

## ResumePlan v1

```json
{
  "version": 1,
  "project": {"id": "...", "name": "...", "logseq_path": "...",
              "local_folder": "...", "github_url": "..."},
  "device_id": "32hex|null",
  "device_reason": "...",
  "session": {"session_id": "...", "start_ms": 0, "end_ms": 0,
              "event_count": 0, "status": "...", "ended_reason": null,
              "applications": ["..."]},
  "session_reason": "...",
  "files": [{"relative": "...", "identity": "...",
             "occurrence_count": 0, "last_seen_ms": 0}],
  "unavailable_resources": [{"identity": "...", "reason": "..."}],
  "repository": {"available": true, "branch": "...", "remote": "...",
                 "root_observed": "...", "reason": ""},
  "logseq": {"available": true, "path": "...", "page": "...",
             "graphName": "...", "revision": "...",
             "open_todos": [{"line": 0, "task": "...", "marker": "..."}],
             "open_count": 0, "total_count": 0, "reason": ""},
  "pi_session": {"available": true, "scope": "...",
                 "session_file": "...", "session_id": "...", "reason": ""},
  "workspace": {"available": true, "name": "...", "reason": ""},
  "operations": [{"id": "...", "kind": "...", "available": true,
                  "reason": "", "params": {}}],
  "warnings": ["..."]
}
```

- `project` is the CURRENT registry entry (current name/mapping wins over
  any historical name; renames never rewrite stored history rows).
- `device_id` is the durable local identity (`device_info` singleton via
  the Rust `device-id` query + `desktop_projects.device_id` wrapper; never
  parsed from SQLite in Python). Missing DB reads as `null` with a reason.
- `session` is the most-recent work session for that exact project on the
  current device only: the plan uses the indexed Rust `search` path with
  BOTH the project UUID and the durable device id (`limit=1`,
  session-recency order), so any number of newer foreign-device sessions
  can never hide local selection; the single returned row is still
  validated (project + device must match) before use. `null` with
  `session_reason` when absent. The plan keeps a stable subset only
  (`session_id`, bounds, counts, persisted `status`/`ended_reason`, sorted
  applications): query-time liveness (`active`/`effective_status`) is
  deliberately excluded so repeated plans over unchanged inputs are
  byte-stable. No timestamps record plan construction.
- `files` holds at most 4 regular files plus bounded `unavailable_resources`
  with reasons (deleted/missing/outside/symlink/sensitive/transient/URL/
  page/cwd). Scoring is an explicitly balanced deterministic mix:
  normalized recency within the session bounds (stable recency rank when
  the session has no usable span) weighted 0.6 plus normalized occurrence
  frequency weighted 0.4, ties on the stable `resource_key` — an ancient
  frequent file cannot always beat files touched near the end. Whole page
  content is never embedded.
- `repository` carries observed session metadata (branch/remote/root) where
  available; no live git is run during planning.
- `logseq` carries page availability/reference/revision plus bounded open
  TODOs and counts — never whole page content. TODOs come verbatim from
  `project_planner.read_page` (counts and open items reuse its parser
  output; page content is retained internally only for the legacy `file::`
  root fallback).
- `pi_session` is the latest safe Pi session for the graph+page scope when
  one exists (read-only derivation; missing scopes/sessions report an
  explicit reason and planning creates no directories).
- `operations` use stable ids/kinds with `available` + reason. `params`
  appear only when available. Missing executables (`hyprctl`, `kitty` +
  `nvim` for the editor, `xdg-open`) are detected at plan time
  (injectable, boolean-only, byte-stable) and rechecked at execution, so
  reasons appear in the plan, not only after Execute.
- `open_project_agent` is available whenever a valid linked page can be
  handed to ProjectPlanner — even with no saved Pi session (the planner
  then creates a fresh scoped session). Its params carry the derived scope
  plus `has_saved_session` and identify the saved session file/id only
  when one exists. `pi_session` itself stays explicitly unavailable when
  none exists.
- `warnings` collects partial/missing-state notes.

## Sources of truth and safety rules

- Registry: `projects.list_projects`. Resource/session identity is by
  stable id; history never remaps on rename.
- Sessions/resources/activity/device: bounded `desktop_projects` Rust
  subprocess helpers (`search` for the current-device session lookup,
  `session-resources`, `last-activity`, `device-id`). Resource rollups are
  fetched bounded (50) per selected session, scored by the balanced
  recency+frequency mix above, and at most 4 regular files are selected.
- Portable identity (`file:<project-relative>`) resolves against the
  current canonical mapped project root with normalized-relative,
  strict-containment, existence, regular-file, non-symlink (final and
  symlinked ancestors via the shared `project_sessions` component policy),
  and sensitive/protected checks on both lexical and resolved
  root-relative paths. Current-device local absolute resources reject
  original symlinks before canonicalization and likewise reject symlinked
  ancestors, and are used only when they resolve inside the current mapped
  root (converted to relative identity). Stale absolute paths are never
  trusted directly. Bad resources (including `resolve`/`is_excluded`
  `RuntimeError` from symlink loops) are recorded unavailable and never
  abort planning. The registry `local_folder` wins (fail closed, no silent
  fallback); with no registry mapping the legacy page `file::` fallback
  via the current `project_files` helpers is allowed.
- Logseq: `project_planner.read_page` for reference/revision/TODOs.
- Pi scope: `project_sessions` safe header/latest-session rules, derived
  read-only (the digest formula mirrors `scope_for` but no directory is
  ever created). Configured base/projects/scope ancestors are validated
  component-by-component (symlinked ancestors rejected); missing scopes
  stay clean unavailable results without creation. When invoked from an
  existing scoped Pi process
  (`QS_PROJECT_SESSION_SCOPE` set), a safe existing leaf reuses its parent
  as the projects base (honoring custom bases, never nesting); an
  unsafe/missing leaf falls back to `PI_CODING_AGENT_SESSION_DIR`/default.
- Workspace: the latest project activity is honored only when its
  `session_id` equals the selected current-device session; its structured
  snapshot workspace is validated (non-empty, `1..64` chars, no control/
  NUL, allowlisted characters, no `special` workspaces). Otherwise the
  workspace — and its operation — is unavailable with a reason.

## Typed execution

No shell strings and no caller-supplied commands: exact list-form argv is
built only by the operation handlers (`shell=False` everywhere).

| Kind | Action |
| --- | --- |
| `focus_workspace` | Modern-first `hyprctl dispatch 'hl.dsp.focus({workspace="<selector>"})'` (bounded, checked), falling back to legacy `hyprctl dispatch workspace <selector>` when the modern form is rejected (older Hyprland). Success requires exit zero AND a recognized `ok` response: trimmed stdout must be exactly `ok` (live modern response is `ok\n`, legacy standard is `ok\n`); hyprctl can print an error on stdout and exit zero, so unknown/empty output fails closed and the legacy form is attempted after a modern rejection. The selector is backend-derived at execution time from the validated literal: numeric names use the numeric selector, all other literals use `name:<literal>` (so `previous` runs as `name:previous`, never as dispatcher syntax). The Lua payload is a fixed backend template with only the allowlisted selector interpolated; both attempts use list-form argv with no shell, workspaces are revalidated before either attempt, unsafe names stay `skipped` (fail-closed), and a double failure reports both exit statuses with bounded stdout/stderr diagnostics (including semantic exit-0 rejections) and returns no `argv`, while the returned `argv` is always the command that succeeded. |
| `open_editor` | Detached `kitty --directory ROOT --title ... nvim -n [-- files...]` with `cwd=ROOT`; requires both `kitty` and `nvim`, files revalidated per file (lexical + resolved sensitive/protected exclusion, symlink/ancestor rejection) at execution time. Reports `launched` when all planned files survive (or the plan intentionally listed zero files, opening the root), `partial` with `skipped` details when a subset survives, and `failed` without launching when a nonempty planned list has no survivors. File args stay list-form behind `--`. |
| `open_terminal` | Detached `kitty --directory ROOT --title ...` with `cwd=ROOT`; reports `launched` (unconfirmed) on spawn success. |
| `open_logseq_page` | Bounded checked `xdg-open <logseq://graph/…?page=…>`; immediate nonzero handler failures report `failed`, success reports `launched` (unconfirmed). |
| `open_project_agent` | Delegated: reports `delegated`, never spawns Pi. Quickshell hands off to ProjectPlanner. |

Unavailable operations report `skipped` with reasons; missing apps
(`hyprctl`/`kitty`/`nvim`/`xdg-open`), missing/deleted folders/files, renamed
projects, no session, no TODO/Logseq, stale/foreign sessions, and partial
failures are all graceful and per-operation. As defense in depth,
`_execute_one` only executes when `id == kind` and the kind is in the fixed
allowlist; anything else is skipped.

## Quickshell integration

The existing command palette recognizes `resume` (and the `project` alias).
For example, `resume BeforeIT` lists matching registry projects, loads the
selected project plan asynchronously, and shows a compact preview containing
the last session time, selected files, observed repository/branch, Logseq
page, open-TODO count, and saved/new Pi-session status. The same palette card
offers `Resume`, `Ask Pi`, and `History`:

- `Resume` (palette) opens an operation checkbox confirmation listing the
  available operations with all available operations selected by default,
  then executes a freshly rebuilt backend plan limited to the checked
  subset via `--operations`; an empty selection is disallowed and `Cancel`
  performs no action. It then hands the stable project UUID to the
  existing ProjectPlanner. The Overview popup `Resume` button is unchanged
  (its existing preview/Confirm flow still executes all available operations).
- `Ask Pi` opens the same project-scoped planner and prefills—but does not
  send—a deterministic continuation request: the baseline draft is
  prefilled immediately, then the cached local continuation from
  `python3 scripts/work_log.py continuation --project UUID`
  (`{project, available, text}`, `text` at most 2400 chars) is appended
  fail-soft while `memory.enabled` is on. The summary is included only when `memory.workLog` is on and
  the stored label line only when `memory.sessionEnrichment` is on; the
  backend derives the summary only from cached deterministic work-log
  sections and reads stored labels when enabled (no fresh draft, no
  polished text), with no new network calls. When unavailable
  the baseline remains unchanged; it never auto-sends and never
  overwrites an edited draft (see `docs/work-memory.md`).
- `History` opens the project's existing scoped transcript.

The handoff waits for the palette exit animation, then ProjectPlanner reloads
the authoritative registry and selects by stable UUID. It reuses the existing
`selectProject`/`agentFor` path and all busy, history, approval, and scoped
session checks. A project without a linked note remains usable as metadata,
but no project Pi worker is started for it.

## Pi integration

`desktop_resume_plan` is the eighth read-only desktop tool and is available in
palette, project, and journal scopes. It can inspect a plan by UUID or safe
registry name/prefix. In project mode an omitted project resolves only from
the pinned `QS_PROJECT_PATH` through an exact registry-page match; outside
project mode omission fails closed. The tool never executes Resume, switches
sessions, writes repository contents, or generates a summary.

ProjectPlanner safely resumes the latest valid JSONL session in the existing
graph+project scope. If none exists, it starts a fresh scoped Pi session. On
the first explicit request, Pi can combine the compact ResumePlan with the
existing pinned Logseq/project tools; no automatic AI summary is produced.

## Tests and validation

- Python: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest
  tests.test_desktop_resume` (plan construction, indexed current-device
  lookup incl. foreign-flood hiding and a real-binary project+device
  `limit=1` boundary, balanced scoring/limit, verbatim TODOs,
  plan-time app availability (pure builder, no `PATH` probe),
  agent handoff with/without saved sessions,
  scoped-leaf base reuse, portable mapping, deleted/outside/symlink/
  ancestor-symlink/`.git`-alias/loop resources (never aborting),
  workspace `name:` selector derivation, modern-first Lua dispatcher with
  legacy fallback (modern success on `ok\n`, fallback success,
  both-fail reporting with bounded stdout/stderr plus exit status,
  modern zero-exit stdout error to legacy `ok`, legacy zero-exit stdout
  error, both zero-exit errors, stdout-only failure diagnostics, byte
  `CompletedProcess` through the real `_run_checked`, numeric/named
  selectors, no caller-controlled Lua), checked `xdg-open` plus
  `launched`/`partial` outcomes, editor unavailable-file reporting
  (partial/failed vs. valid zero-file root open), Pi scope symlink
  validation with no creation, renames,
  ambiguity, missing apps, exact-spawn no-mutation with a fake runner,
  safe argv (incl. unknown-op skip and sensitive-file exclusion),
  CLI errors). Planning and fake-runner execution tests assert no
  project-content mutation.
- Rust: `cargo test --locked --manifest-path
  services/agent-orchestrator/Cargo.toml` (includes the `device-id`
  arg-gate tests in `src/bin/qs-desktop-context.rs`); `rustfmt --check`
  the touched binary file.
- Manual: `list --query BeforeIT` resolves one prefix entry; `plan` over a
  missing DB stays null-sessioned with reasons and creates nothing;
  `execute --operations open_terminal` runs only that kind.

## Limitations

- Pi can inspect `desktop_resume_plan`, but actual desktop restoration remains
  an explicit user action in the Quickshell palette. The model cannot execute
  Resume or synthesize restoration commands.
- History proves observation/focus, not edits; resources are
  best-effort/stale-tolerant and revalidated before acting.
- Titles/app names/URLs/paths are stored verbatim in the private local DB
  (no redaction, no retention controls); skip the collector or use a
  throwaway `--db` if sensitive.
