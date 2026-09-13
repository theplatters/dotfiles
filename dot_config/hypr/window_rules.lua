hl.window_rule({
	name = "evolution-special-workspace",
	match = {
		class = "org.gnome.Evolution",
	},
	workspace = "special:evolution silent",
})

hl.window_rule({
	name = "journal-dashboard", -- Optional identifier name
	match = {
		class = "journal-floating",
	},
	float = true,
	size = { "monitor_w * 0.7", "monitor_h * 0.7" }, -- Relative or flat values work beautifully here
	center = true,
	workspace = "special:journal",
})
