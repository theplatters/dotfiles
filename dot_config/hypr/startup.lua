hl.on("hyprland.start", function()
	hl.exec_cmd(TERMINAL)
	hl.exec_cmd("quickshell & hyprpaper")
	hl.exec_cmd("evolution")
	hl.exec_cmd("wl-paste --watch cliphist store")
	-- Polkit authentication agent. Hyprland ships none and the installed
	-- hyprpolkitagent unit is only WantedBy=graphical-session.target, which
	-- this session never starts, so enable is not enough -- start it here.
	-- Without an agent, polkit prompts (pkexec, admin tools) fail silently.
	hl.exec_cmd("systemctl --user start hyprpolkitagent")
	-- Desktop-activity collector (private SQLite history, singleton per DB).
	-- Replace a stale instance left by a previous compositor run (it still
	-- holds the DB lock and its old instance signature is dead), then start
	-- the fresh one. `pkill -x` matches the 15-char comm name; the sleep only
	-- runs when something was actually killed (bounded SIGTERM drain).
	hl.exec_cmd(
		"pkill -x qs-desktop-cont && sleep 3 ; QS_ZEN_CONTEXT_FILE=$HOME/.local/state/quickshell/desktop-activity/zen-context.json $HOME/.config/quickshell/services/agent-orchestrator/target/release/qs-desktop-context collect"
	)
end)
