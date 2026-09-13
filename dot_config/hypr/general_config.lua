hl.config({
	general = {
		gaps_in = 8,
		gaps_out = 16,
		border_size = 1,
		col = {
			active_border = "rgba(1D1D1Dff)",
			inactive_border = "rgba(ffffff1f)",
		},
		-- Set to true to enable resizing windows by clicking and dragging on borders and gaps
		resize_on_border = false,

		-- Please see https://wiki.hypr.land/Configuring/Advanced-and-Cool/Tearing/ before you turn this on
		allow_tearing = false,

		layout = "dwindle",
	},
	decoration = {
		rounding = 16,
		rounding_power = 2,

		-- Change transparency of focused and unfocused windows
		active_opacity = 1.0,
		inactive_opacity = 1.0,

		shadow = {
			enabled = true,
			range = 20,
			render_power = 2,
			color = 0x55000000,
		},

		blur = {
			enabled = false,
		},

		glow = {
			enabled = false,
		},
	},

	animations = {
		enabled = true,
	},
	misc = {
		force_default_wallpaper = 0,
		disable_hyprland_logo = true,
	},
})

-- Ref https://wiki.hypr.land/Configuring/Basics/Workspace-Rules/

-- See https://wiki.hypr.land/Configuring/Layouts/Dwindle-Layout/ for more
hl.config({
	dwindle = {
		preserve_split = true, -- You probably want this
	},
})

-- See https://wiki.hypr.land/Configuring/Layouts/Master-Layout/ for more
hl.config({
	master = {
		new_status = "master",
	},
})
