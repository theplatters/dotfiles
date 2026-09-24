return {
  {
    "mfussenegger/nvim-dap",
    opts = function()
      local dap = require("dap")

      dap.adapters.julia = {
        type = "executable",
        command = "julia",
        args = {
          "--startup-file=no",
          "--history-file=no",
          "-e",
          [[
            using DebugAdapter
            DebugAdapter.run_main()
          ]],
        },
      }

      dap.configurations.julia = {
        {
          type = "julia",
          request = "launch",
          name = "Launch Julia Debugger",
          program = "${file}",
        },
      }
    end,
  },
}
