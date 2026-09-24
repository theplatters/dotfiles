-- qs-context.lua: opt-in Neovim -> qs-desktop-context publisher.
--
-- Purpose: publish a small atomic private structured record for the
-- application-aware collector (Phase 2), carrying PID + kitty window id so
-- the collector can safely correlate the record to the focused window/pane.
-- Neovim inside kitty takes precedence ONLY as the unique foreground nvim
-- in the matching pane with a fresh record; background/ambiguous nvim is
-- ignored.
--
-- Opt-in (init.lua):
--
--   vim.g.qs_nvim_context_enable = true
--   -- optional override, else $XDG_RUNTIME_DIR/quickshell/nvim-context:
--   -- vim.g.qs_nvim_context_dir = "/run/user/1000/quickshell/nvim-context"
--   require("qs-context").setup()
--
-- Record shape (one file per PID, `<pid>.json`, mode 0600, exclusive
-- creation + atomic rename):
--   {"pid":123,"kitty_window_id":5,"file":"/abs/path.md","cwd":"/abs/cwd","updated_at_ms":1710000000000}
-- `file` is empty-string unless the buffer is a real local file (normal
-- buftype, no URI scheme — unnamed/help/terminal/nofile/fugitive and
-- friends publish no file); `kitty_window_id` is null when not
-- running inside kitty. `updated_at_ms` is wall-clock EPOCH milliseconds
-- (gettimeofday); freshness bound is 30 s (collector drops stale). No file
-- *contents* are ever published, only the path + cwd.
--
-- Security: the directory must be a private owned non-symlink directory
-- (0700, euid-owned); record files are created exclusive 0600 with no
-- symlink following. The collector re-validates owner/type/mode with
-- O_NOFOLLOW and ignores anything else.
--
-- Events: BufEnter, BufWritePost, FocusGained, DirChanged + 10 s heartbeat
-- while idle. VimLeavePre removes the record (best-effort, no failure).

local M = {}

local timer = nil

local function enabled()
  return vim.g.qs_nvim_context_enable == true or vim.g.qs_nvim_context_enable == 1
end

local function context_dir()
  if vim.g.qs_nvim_context_dir and vim.g.qs_nvim_context_dir ~= "" then
    return vim.g.qs_nvim_context_dir
  end
  local r = os.getenv("QS_NVIM_CONTEXT_DIR")
  if r and r ~= "" then
    return r
  end
  local xdg = os.getenv("XDG_RUNTIME_DIR")
  if xdg and xdg ~= "" then
    return xdg .. "/quickshell/nvim-context"
  end
  return nil
end

-- Wall-clock epoch milliseconds. hrtime() is MONOTONIC (wrong clock for
-- freshness); use gettimeofday() with an os.time() fallback.
local function epoch_ms()
  local ok, sec, usec = pcall(function()
    return vim.loop.gettimeofday()
  end)
  if ok and type(sec) == "number" then
    usec = tonumber(usec) or 0
    return math.floor(sec * 1000 + usec / 1000)
  end
  return (os.time() or 0) * 1000
end

local function ensure_private_dir(dir)
  if dir == nil or dir == "" then
    return false
  end
  -- Reject symlink components at the leaf: lstat must not be a link.
  local lst = vim.loop.fs_lstat(dir)
  if lst and lst.type == "link" then
    return false
  end
  if vim.fn.isdirectory(dir) == 0 then
    -- Create parents, then enforce 0700 from the outset.
    vim.fn.mkdir(dir, "p")
    pcall(function()
      vim.loop.fs_chmod(dir, 448) -- 0700
    end)
  end
  if vim.fn.isdirectory(dir) == 0 then
    return false
  end
  -- Re-check after creation: still not a symlink.
  local lst2 = vim.loop.fs_lstat(dir)
  if lst2 and lst2.type == "link" then
    return false
  end
  -- Enforce private mode on the existing leaf (never touches ancestors).
  pcall(function()
    vim.loop.fs_chmod(dir, 448)
  end)
  local st = vim.loop.fs_stat(dir)
  if not st or st.type ~= "directory" then
    return false
  end
  -- Owner/mode check where available: require euid ownership and no
  -- group/other bits. If uid/gid/mode are unavailable, still proceed (the
  -- collector validates authoritatively); never fail open on symlinks.
  if st.uid and st.uid ~= vim.loop.getuid() then
    return false
  end
  if st.mode then
    -- st.mode includes file-type bits; mask permission bits.
    local perm = st.mode % 512 -- 0o777
    if perm % 64 ~= 0 then
      -- group/other bits set: tighten once, then re-stat.
      pcall(function()
        vim.loop.fs_chmod(dir, 448)
      end)
      local st2 = vim.loop.fs_stat(dir)
      if not st2 then
        return false
      end
      if (st2.mode % 512) % 64 ~= 0 then
        return false
      end
    end
  end
  return true
end

local function record_path(dir)
  -- Single numeric segment: "<pid>.json" only, no interpolation.
  local pid = vim.fn.getpid()
  if type(pid) ~= "number" then
    return nil
  end
  return dir .. "/" .. tostring(pid) .. ".json"
end

-- Only real local files publish a path: normal buffers (empty buftype)
-- whose expanded name carries no URI scheme. Unnamed, help, terminal,
-- nofile, fugitive://, term:// and friends publish no file (cwd is
-- still retained); without this gate `expand("%:p")` would invent
-- cwd-joined paths for buffers that are not files at all.
local function current_file()
  if vim.bo.buftype ~= "" then
    return ""
  end
  local name = vim.fn.expand("%:p")
  if type(name) ~= "string" or name == "" then
    return ""
  end
  if name:match("^%w[%w+.-]*://") then
    return ""
  end
  if #name > 4096 then
    name = name:sub(1, 4096)
  end
  return name
end

local function current_record()
  local pid = vim.fn.getpid()
  if type(pid) ~= "number" then
    return nil
  end
  local kitty_raw = os.getenv("KITTY_WINDOW_ID")
  local kitty_id = nil
  if kitty_raw and kitty_raw:match("^%d+$") and #kitty_raw <= 10 then
    kitty_id = tonumber(kitty_raw)
  end
  local file = current_file()
  local cwd = vim.fn.getcwd()
  if type(cwd) ~= "string" then
    cwd = ""
  end
  return {
    pid = pid,
    kitty_window_id = kitty_id,
    file = file,
    cwd = cwd,
    updated_at_ms = epoch_ms(),
  }
end

local function write_atomic(path, data)
  if not path then
    return
  end
  local ok, encoded = pcall(vim.json.encode, data)
  if not ok or type(encoded) ~= "string" then
    return
  end
  if #encoded > 16 * 1024 then
    return
  end
  -- Exclusive 0600 creation (no symlink following, no truncation of a
  -- victim file): libuv O_WRONLY|O_CREAT|O_EXCL via numeric flags
  -- (1|64|128). libuv returns nil+err on failure (no throw), so check
  -- returns directly.
  local tmp = path .. ".tmp." .. tostring(vim.fn.getpid())
  vim.loop.fs_unlink(tmp)
  local O_EXCL_CREATE_WRONLY = 1 + 64 + 128
  local fd = vim.loop.fs_open(tmp, O_EXCL_CREATE_WRONLY, 384) -- 0600 exclusive
  if not fd then
    return
  end
  local wlen = vim.loop.fs_write(fd, encoded, 0)
  pcall(function()
    vim.loop.fs_fsync(fd)
  end)
  vim.loop.fs_close(fd)
  if wlen ~= #encoded then
    vim.loop.fs_unlink(tmp)
    return
  end
  pcall(function()
    vim.loop.fs_chmod(tmp, 384)
  end)
  os.rename(tmp, path)
  pcall(function()
    vim.loop.fs_chmod(path, 384)
  end)
end

function M.publish()
  if not enabled() then
    return
  end
  local dir = context_dir()
  if not ensure_private_dir(dir) then
    return
  end
  local ok, rec = pcall(current_record)
  if not ok or not rec then
    return
  end
  pcall(write_atomic, record_path(dir), rec)
end

function M.cleanup()
  if not enabled() then
    return
  end
  local dir = context_dir()
  if dir == nil then
    return
  end
  pcall(os.remove, record_path(dir))
end

function M.setup()
  if not enabled() then
    return
  end
  local group = vim.api.nvim_create_augroup("QsDesktopContext", { clear = true })
  vim.api.nvim_create_autocmd(
    { "BufEnter", "BufWritePost", "FocusGained", "DirChanged", "VimEnter" },
    { group = group, callback = function()
      M.publish()
    end }
  )
  vim.api.nvim_create_autocmd("VimLeavePre", {
    group = group,
    callback = function()
      M.cleanup()
    end,
  })
  if timer then
    pcall(function()
      timer:stop()
    end)
    timer = nil
  end
  timer = vim.loop.new_timer()
  if timer then
    timer:start(
      10 * 1000,
      10 * 1000,
      vim.schedule_wrap(function()
        M.publish()
      end)
    )
  end
  M.publish()
end

return M
