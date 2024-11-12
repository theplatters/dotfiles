-- Options are automatically loaded before lazy.nvim startup
-- Default options that are always set: https://github.com/LazyVim/LazyVim/blob/main/lua/lazyvim/config/options.lua
-- Add any additional options here

return {
  {
    "Olical/conjure",
    ft = function()
      return { "clojure", "julia" }
    end,
  },

  {
    "nvim-treesitter/nvim-treesitter",
    opts = {
      ensure_installed = {
        "julia",
      },
    },
  },
  {
    "neovim/nvim-lspconfig",
    opts = {
      servers = {
        clojure_lsp = {},
      },
    },
  },
}
