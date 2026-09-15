hl.on("hyprland.start", function()
	hl.exec_cmd(TERMINAL)
	hl.exec_cmd("quickshell & hyprpaper")
	hl.exec_cmd("evolution")
	hl.exec_cmd("wl-paste --watch cliphist store")
	-- Desktop-activity collector (private SQLite history, singleton per DB).
	-- Replace a stale instance left by a previous compositor run (it still
	-- holds the DB lock and its old instance signature is dead), then start
	-- the fresh one. `pkill -x` matches the 15-char comm name; the sleep only
	-- runs when something was actually killed (bounded SIGTERM drain).
	hl.exec_cmd("pkill -x qs-desktop-cont && sleep 3 ; /home/franzs/.config/quickshell/services/agent-orchestrator/target/release/qs-desktop-context collect")
	hl.dsp.workspace.toggle_special("journal")
end)
