# quickshell config

Quickshell configuration: `shell.qml` wires the top bar, popouts,
notification server, and shared state. UI components live in `widgets/`,
helpers in `scripts/`, the worker bridge in `services/agent-orchestrator`,
tests in `tests/`, agent policy in `.pi/`, and shared tokens in `theme/Theme.qml`.

Run directly with Quickshell (no build step for the shell itself):

```sh
quickshell -c /home/franzs/.config/quickshell
```

## Planner/journal/palette workers

Project planner, journal, and command-palette workers all run through the
resident Rust bridge, `services/agent-orchestrator` (binary
`qs-agent-orchestrator`), driven by `widgets/ScopedAgent.qml`. The
palette uses `--mode palette`: direct `pi --mode rpc --approve` with no
scoped wrapper, scope vars cleared (`QS_PROJECT_PATH`,
`QS_PROJECT_SESSION_SCOPE`, `QS_JOURNAL_MODE`,
`QS_JOURNAL_SESSION_SCOPE`), and its general tool scope intact.
`LOGSEQ_GRAPH`/`PI_CODING_AGENT_SESSION_DIR` are inherited, and scoped
session dirs are pinned by `QS_*_SESSION_SCOPE` handshakes. Scoped
project/journal wrappers keep their page/journal scopes. Rust
owns Pi lifecycle/parsing, request correlation, and the approval
protocol; the TypeScript extension and Python helpers still own graph
safety and tools. New op: `requestStats` (palette `stats` via
`request('get_session_stats')`). `ai:` free-form prompts, `/skills`,
image prompts, session commands (`new`, `switch`, `rename`, `model`,
`compact`, `stats`, `stop`), and approvals are covered. Closing the
palette leaves its worker running (reopen shows pending approvals);
closing the planner pauses idle scoped workers via `stopIdle`.
Shared controls are `widgets/WidgetButton.qml` and `widgets/WidgetIconButton.qml`
with `widgets/icons/*.svg` assets.
The planner tasks panel collapses to an overlay below 600px width and
the transcript uses `conversationStore` + `syncConversation` for stable scroll.

Rust/Cargo is a build prerequisite. The release binary is git-ignored
and never committed — rebuild it manually after every pull before
launching Quickshell. QML launches only the built binary with no
fallback; a missing binary reports a build error.

```sh
cargo build --locked --release --manifest-path services/agent-orchestrator/Cargo.toml
```

Docs:

- `services/agent-orchestrator/README.md` — build, CLI, stdin/update
  protocol, bounds, tests.
- `docs/project-planner.md` — planner/journal behavior.
- `.pi/README.md` — palette, skills, and scoped-tool policy.

## Tests

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```
