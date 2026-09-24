-- bootstrap lazy.nvim, LazyVim and your plugins
require("config.lazy")

vim.g.qs_nvim_context_enable = true
-- optional override, else $XDG_RUNTIME_DIR/quickshell/nvim-context:
-- vim.g.qs_nvim_context_dir = "/run/user/1000/quickshell/nvim-context"
require("qs-context").setup()
