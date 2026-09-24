-- every spec file under the "plugins" directory will be loaded automatically by lazy.nvim
--
-- In your plugin files, you can:
-- * add extra plugins
-- * disable/enabled LazyVim plugins
-- * override the configuration of LazyVim plugins
return {
  {
    "Olical/conjure",
    -- Load the plugin when needed
    ft = { "clojure", "fennel", "janet", "racket", "scheme", "julia", "lisp", "python" }, -- adjust based on which languages you use
    -- Configure global variables before the plugin loads
    init = function()
      vim.g["conjure#log#diagnostics"] = false
      vim.g["conjure#client#julia#stdio#command"] = "julia --project --banner=no --color=no -i"
      vim.g["conjure#client#python#stdio#command"] = "uv run python3 -iq"
      -- Or rebind it from K to <prefix>gk
      -- vim.g["conjure#mapping#doc_word"] = "gk"

      -- Or reset to default unprefixed K
      -- vim.g["conjure#mapping#doc_word"] = {"K"}

      -- Add any other Conjure configurations here
      -- vim.g["conjure#log#botright"] = true
      -- vim.g["conjure#client#clojure#nrepl#eval#auto_require"] = false
    end,
    -- Other lazy.nvim options as needed
    config = function()
      -- Any additional setup that should run AFTER the plugin loads
    end,
  },
}
