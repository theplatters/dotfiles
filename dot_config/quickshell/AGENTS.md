# Repository Guidelines

## Project Structure & Module Organization

This repository is a Quickshell configuration written in QML. The entry point is `shell.qml`, which wires together the top bar, popouts, notification server, and shared state. Reusable bar widgets live in `widgets/`, including audio, battery, media, network, tray, workspace, and notification components. Helpers live in `scripts/`, the worker bridge in `services/agent-orchestrator`, tests in `tests/`, agent policy in `.pi/`, extra docs in `docs/`, and shared theme values are in `theme/Theme.qml`, exposed through `theme/qmldir` as a singleton import.

Keep new UI modules close to their feature area. Use `widgets/` for panel and popout components, and `theme/` only for shared design tokens.

## Build, Test, and Development Commands

The shell itself has no build step or package manifest. Run the configuration directly with Quickshell:

```sh
quickshell -c /home/franzs/.config/quickshell
```

For a quick syntax/load check, start Quickshell from this directory and watch stderr for QML import, binding, or runtime errors:

```sh
quickshell -c .
```

Use ripgrep while navigating the codebase:

```sh
rg "Theme\\." widgets shell.qml
```

The planner/journal/palette workers additionally require the Rust bridge
binary before launching Quickshell. It is git-ignored and never
committed, so rebuild it manually after every pull; QML launches only
the built binary with no fallback, and a missing binary reports a build
error:

```sh
cargo build --locked --release --manifest-path services/agent-orchestrator/Cargo.toml
```

## Coding Style & Naming Conventions

Use QML conventions already present in the repo: 4-space indentation, PascalCase component filenames such as `BatteryModule.qml`, and lower camelCase ids and properties such as `networkPopup` or `compactMedia`. Prefer declarative bindings over imperative updates. Keep shared colors, radii, and typography in `Theme.qml` rather than duplicating literals across components.

Use short comments only when they clarify layout intent, service integration, or non-obvious interaction behavior. Use `widgets/WidgetButton.qml` + `widgets/WidgetIconButton.qml` + `widgets/icons/*.svg` for new controls instead of bespoke one-off buttons.

## Testing Guidelines

Python tests live in `tests/` (unittest-discoverable; pytest also
collects them) and Rust bridge tests live in
`services/agent-orchestrator/tests/`:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
cargo test --locked --manifest-path services/agent-orchestrator/Cargo.toml
```

Validate UI changes manually by launching Quickshell, interacting with the affected widget or popout, and checking logs for QML errors. For responsive behavior, test at narrow and wide monitor widths where compact flags such as `compactStats`, `compactNetwork`, `compactBattery`, and `compactMedia` (`widgets/Bar.qml`) may change behavior.

## Commit & Pull Request Guidelines

The current git history uses short, imperative commit subjects, for example `init` and `gruvbox theme`. Keep commits focused and use concise lower-case summaries when appropriate.

Pull requests should describe the changed UI behavior, list manual validation performed, and include screenshots or screen recordings for visual changes. Mention any dependencies on Hyprland, Wayland, Quickshell services, or external system tools.

## Agent-Specific Instructions

Before editing, check for user-local changes and avoid reverting unrelated work. Keep changes scoped to the requested component and preserve the existing QML structure unless a broader refactor is explicitly needed.
