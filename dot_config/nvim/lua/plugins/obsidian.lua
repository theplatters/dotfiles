return {
  "epwalsh/obsidian.nvim",
  version = "*", -- Recommended, use latest release instead of latest commit
  lazy = true,
  ft = "markdown",
  dependencies = {
    "nvim-lua/plenary.nvim",
  },
  opts = {

    disable_frontmatter = true,
    workspaces = {
      {
        name = "logseq",
        path = "~/Nextcloud/Documents/Notes/", -- Point this to your Logseq root folder
      },
    },
    -- This maps standard wiki links correctly
    wiki_link_func = "use_alias_only",
    daily_notes = {
      -- Optional, if you keep daily notes in a separate directory.
      folder = "journals",
      -- Optional, if you want to change the date format for the ID of daily notes.
      date_format = "%Y-%m-%d",
    },
  },
  -- Inside your ~/.config/nvim/lua/plugins/obsidian.lua
  mappings = {
    -- Your custom mapping
    ["<leader>cd"] = {
      action = function()
        return require("obsidian").util.gf_passthrough()
      end,
      opts = { noremap = false, expr = true, buffer = true, desc = "Follow Logseq Link" },
    },

    -- The plugin's recommended "Default" Mappings:
    -- 1. Overrides 'gf' to follow links natively under cursor
    ["gf"] = {
      action = function()
        return require("obsidian").util.gf_passthrough()
      end,
      opts = { noremap = false, expr = true, buffer = true, desc = "Follow Link (gf)" },
    },
    -- 2. Toggle a checkbox state ([ ] -> [x])
    ["<leader>ch"] = {
      action = function()
        return require("obsidian").util.toggle_checkbox()
      end,
      opts = { buffer = true, desc = "Toggle Checkbox" },
    },
    -- 3. Smart Enter (follows link, folds heading, or toggles checkbox depending on cursor)
    ["<cr>"] = {
      action = function()
        return require("obsidian").util.smart_action()
      end,
      opts = { buffer = true, expr = true, desc = "Obsidian Smart Action" },
    },
  },
}
