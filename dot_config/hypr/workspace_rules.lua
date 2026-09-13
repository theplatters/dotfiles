hl.workspace_rule({
	workspace = "special:journal",
	on_created_empty = "kitty --class journal-floating -e fish -c '$HOME/bin/daily_note.fish'",
})
