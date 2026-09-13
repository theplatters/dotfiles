-- Calm, non-spring motion. Hyprland speeds are deciseconds (100ms each).
hl.curve("richBlackEase", { type = "bezier", points = { { 0.22, 1 }, { 0.36, 1 } } })
hl.curve("richBlackSoft", { type = "bezier", points = { { 0.4, 0 }, { 0.2, 1 } } })
hl.curve("richBlackLinear", { type = "bezier", points = { { 0, 0 }, { 1, 1 } } })

hl.animation({ leaf = "global", enabled = true, speed = 2.2, bezier = "richBlackEase" })
hl.animation({ leaf = "border", enabled = true, speed = 1.4, bezier = "richBlackSoft" })

-- Windows use a barely perceptible 98% pop-in, with opacity handled by fade.
hl.animation({ leaf = "windows", enabled = true, speed = 2.2, bezier = "richBlackEase", style = "popin 98%" })
hl.animation({ leaf = "windowsIn", enabled = true, speed = 2.0, bezier = "richBlackEase", style = "popin 98%" })
hl.animation({ leaf = "windowsOut", enabled = true, speed = 1.8, bezier = "richBlackSoft", style = "popin 98%" })
hl.animation({ leaf = "fade", enabled = true, speed = 2.0, bezier = "richBlackSoft" })
hl.animation({ leaf = "fadeIn", enabled = true, speed = 2.0, bezier = "richBlackSoft" })
hl.animation({ leaf = "fadeOut", enabled = true, speed = 1.8, bezier = "richBlackLinear" })

-- Layers cover panels and other shell surfaces; keep them in the same 180-240ms range.
hl.animation({ leaf = "layers", enabled = true, speed = 2.2, bezier = "richBlackEase", style = "fade" })
hl.animation({ leaf = "layersIn", enabled = true, speed = 2.0, bezier = "richBlackEase", style = "fade" })
hl.animation({ leaf = "layersOut", enabled = true, speed = 1.8, bezier = "richBlackSoft", style = "fade" })
hl.animation({ leaf = "fadeLayersIn", enabled = true, speed = 2.0, bezier = "richBlackSoft" })
hl.animation({ leaf = "fadeLayersOut", enabled = true, speed = 1.8, bezier = "richBlackLinear" })

-- Workspace changes stay quiet and fade rather than slide.
hl.animation({ leaf = "workspaces", enabled = true, speed = 2.4, bezier = "richBlackSoft", style = "fade" })
hl.animation({ leaf = "workspacesIn", enabled = true, speed = 2.2, bezier = "richBlackSoft", style = "fade" })
hl.animation({ leaf = "workspacesOut", enabled = true, speed = 2.2, bezier = "richBlackSoft", style = "fade" })
