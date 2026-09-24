return {
  {
    "neovim/nvim-lspconfig",
    ft = "julia",
    opts = {
      servers = {
        jetls = {
          filetypes = { "julia" },
          cmd = {
            "jetls",
            "serve",
          },
          settings = {
            julia = {
              completionmode = "qualify",
              lint = { missingrefs = "none" },
            },
          },
        },
      },
    },
  },
}
