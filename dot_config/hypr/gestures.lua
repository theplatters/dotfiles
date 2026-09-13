hl.gesture({ fingers = 3, direction = "horizontal", action = "workspace" })
hl.gesture({ fingers = 3, direction = "down", mods = "ALT", action = "close" })
hl.gesture({ fingers = 3, direction = "up", scale = 1.5, action = "fullscreen" })
hl.gesture({
	fingers = 4,
	direction = "up",
	action = function()
		hl.exec_cmd("kitty")
	end,
})
