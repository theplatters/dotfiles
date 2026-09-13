# Quickshell Configuration: Quickshell-Desktop

This project is a custom desktop shell (bar and sidebar) built for Wayland using [Quickshell](https://github.com/outfoxxed/quickshell). It provides a modern, modular, and highly customizable environment with integrated Gemini AI, TODO management, and News feeds.

## Project Overview

- **Core Technology:** QML (QtQuick) via Quickshell.
- **Environment:** Wayland (utilizing `Quickshell.Wayland`).
- **Aesthetic:** High-contrast dark theme (Catppuccin-inspired) defined in `theme/Theme.qml`.
- **Main Components:**
    - **Top Bar:** Displays system stats (CPU, Mem, Disk), clock, audio, battery, tray, and notification tray.
    - **Retractable Sidebar:** Features tabs for Gemini (AI chat), TODOs, and News.

## Architecture

```text
/home/franzs/.config/quickshell/
├── shell.qml           # Main entry point and ShellRoot
├── theme/              # Centralized styling
│   ├── Theme.qml       # Color palette and shared styles (Singleton)
│   └── qmldir          # Singleton registration
├── sidebar/            # Sidebar views and components
│   ├── Sidebar.qml     # Retractable container
│   ├── GeminiView.qml  # AI Chat interface
│   ├── TodoView.qml    # Task management
│   └── NewsView.qml    # RSS/News feed
└── widgets/            # Reusable bar modules
    ├── Bar.qml         # The top panel container
    ├── AudioModule.qml
    ├── NotificationModule.qml # Expandable notification tray
    ├── StatModule.qml  # Generic module for shell command outputs
    └── ...
```

## Building and Running

### Prerequisites
- [Quickshell](https://github.com/outfoxxed/quickshell) installed on your system.
- Qt6 libraries.
- Standard Linux utilities: `free`, `top`, `python3`.

### Running the Shell
To start the shell, run the following command from the project root:
```bash
quickshell
```

**Note on Notifications:** This project includes a built-in notification server. To prevent conflicts with other notification daemons like `dunst`, you should disable or mask them:
```bash
systemctl --user mask dunst.service
killall dunst
```

To reload or test specific components:
```bash
quickshell --path path/to/component.qml
```

## Development Conventions

- **Modularity:** Keep widgets small and focused. Reuse `StatModule.qml` for any shell-command-based monitoring.
- **Styling:** Always use properties from `Theme` (e.g., `Theme.mauve`, `Theme.radius`) to ensure consistency.
- **Processes:** Use `Quickshell.Io.Process` for interacting with system commands.
- **Gemini Integration:** The API key is stored in `.env` and loaded dynamically via a `Process` in `GeminiView.qml`.

## Known TODOs / Ideas
- [ ] Implement actual persistence for `TodoView.qml`.
- [ ] Configure `NewsView.qml` with real RSS feeds.
- [ ] Add more interactivity to `StatModule` (e.g., click to open a monitor).
