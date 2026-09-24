-- Keymaps are automatically loaded on the VeryLazy event
-- Default keymaps that are always set: https://github.com/LazyVim/LazyVim/blob/main/lua/lazyvim/config/keymaps.lua
-- Add any additional keymaps here
vim.keymap.set("n", "<leader>cp", function()
  -- Get the current line and cursor position
  local line = vim.api.nvim_get_current_line()
  local col = vim.fn.col(".")

  -- Search backwards for '@' and forwards for end of citekey
  local start_pos = line:sub(1, col):find("@[%w_-]*$")
  if not start_pos then
    print("No @citekey under cursor")
    return
  end

  local citekey = line:match("@([%w_-]+)", start_pos)
  if not citekey then
    print("No @citekey found")
    return
  end

  -- Replace in line
  local new_line = line:gsub("@" .. citekey, "#cite(<" .. citekey .. '>, form: "prose")', 1)
  vim.api.nvim_set_current_line(new_line)
end, { desc = "Convert @citekey → #cite<citekey> (prose)" })
