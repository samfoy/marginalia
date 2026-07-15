--[[--
marginalia_async.lua — Non-blocking HTTP using ffiutil.runInSubProcess.

Mirrors the pattern used by koassistant.koplugin (which works on Android 15):
  - Child function takes (pid, child_write_fd) arguments
  - Child writes raw response body to the pipe fd via ffiutil.writeToFD
  - Parent polls with UIManager:scheduleIn, reads via ffiutil.readAllFromFD

Public API:
  Async.post(url, body_table, on_done, on_error)
    Starts an async POST. on_done(text) or on_error(err). Returns cancel().
--]]--

-- All requires happen at module level (parent process — safe)
local ffiutil    = require("ffi/util")
local http       = require("socket.http")
local ltn12      = require("ltn12")
local rapidjson  = require("rapidjson")
local UIManager  = require("ui/uimanager")
local logger     = require("logger")
local ffi        = require("ffi")

local Async = {}

local POLL_INTERVAL = 1.5

local ERR_MARKER = "MARGINALIA_ERR:"

-- ── pipe write helper (same as koassistant's wrap_fd) ─────────────────────────

local function wrap_fd(fd)
    return {
        write = function(self, chunk)
            ffiutil.writeToFD(fd, chunk)
            return self
        end,
        close = function() return true end,
    }
end

-- ── Public API ────────────────────────────────────────────────────────────────

local function dlog(msg)
    logger.dbg("marginalia_async: " .. msg)
end

function Async.post(url, body_table, on_done, on_error, opts)
    local body_str = rapidjson.encode(body_table)
    local want_raw = opts and opts.raw
    local extra_headers = (opts and opts.headers) or {}

    -- ── child function — runs in forked subprocess ──────────────────────────
    -- Must not call require() for new modules — only use parent-captured upvalues.
    local function child_fn(pid, child_write_fd)
        local ok, err = pcall(function()
            -- Set socket timeout (defensive pcall — safe after fork)
            local su_ok, socketutil = pcall(require, "socketutil")
            if su_ok and socketutil then
                socketutil:set_timeout(25, 30)
            end

            local req_headers = {
                ["Content-Type"]   = "application/json",
                ["Content-Length"] = tostring(#body_str),
            }
            for k, v in pairs(extra_headers) do req_headers[k] = v end

            local pipe_sink = wrap_fd(child_write_fd)
            local ok2, code = http.request({
                url     = url,
                method  = "POST",
                headers = req_headers,
                source = ltn12.source.string(body_str),
                sink   = ltn12.sink.file(pipe_sink),
            })

            if not ok2 then
                ffiutil.writeToFD(child_write_fd,
                    ERR_MARKER .. "network: " .. tostring(code))
            elseif code ~= 200 then
                ffiutil.writeToFD(child_write_fd,
                    ERR_MARKER .. "HTTP " .. tostring(code))
            end
            -- On 200, the body was already written to the pipe by ltn12.sink.file
        end)

        if not ok then
            ffiutil.writeToFD(child_write_fd,
                ERR_MARKER .. tostring(err))
        end
    end

    -- ── spawn subprocess ────────────────────────────────────────────────────
    local pid, parent_read_fd = ffiutil.runInSubProcess(child_fn, true)

    if not pid then
        -- Fork unavailable — fall back to blocking (better than crash)
        logger.warn("marginalia async: fork unavailable, falling back to blocking")
        local sink = {}
        local fb_headers = {
            ["Content-Type"]   = "application/json",
            ["Content-Length"] = tostring(#body_str),
        }
        for k, v in pairs(extra_headers) do fb_headers[k] = v end
        local ok, code = http.request({
            url     = url,
            method  = "POST",
            headers = fb_headers,
            source = ltn12.source.string(body_str),
            sink   = ltn12.sink.table(sink),
        })
        if not ok then
            on_error("network: " .. tostring(code))
        else
            local resp, ferr = rapidjson.decode(table.concat(sink))
            if not resp then on_error("decode: " .. (ferr or "?"))
            elseif type(resp.error) == "string" and resp.error ~= "" then on_error(resp.error)
            else on_done(want_raw and resp or (resp.response or "")) end
        end
        return function() end
    end

    logger.dbg("marginalia async: subprocess pid=" .. tostring(pid))
    dlog("fork ok pid=" .. tostring(pid))

    -- ── poll loop ───────────────────────────────────────────────────────────
    local poll_count = 0
    local cancelled  = false
    local completed  = false
    local chunks     = {}                 -- accumulated response body across polls
    local deadline   = os.time() + 130    -- wall-clock timeout (GPT calls can be slow)

    local function cleanup()
        if pid then
            -- Drain pipe and reap child
            UIManager:scheduleIn(1, function()
                if ffiutil.isSubProcessDone(pid) then
                    if parent_read_fd then
                        pcall(ffiutil.readAllFromFD, parent_read_fd)
                    end
                else
                    UIManager:scheduleIn(3, function()
                        if parent_read_fd then
                            pcall(ffiutil.readAllFromFD, parent_read_fd)
                        end
                        if pid then pcall(ffiutil.terminateSubProcess, pid) end
                    end)
                end
            end)
        end
    end

    local function finish(text, err)
        if completed then return end
        completed = true
        cleanup()
        dlog("finish err=" .. tostring(err) .. " text_len=" .. tostring(text and #text or 0))
        if err then on_error(err)
        else on_done(text) end
    end

    local function poll()
        if cancelled then cleanup(); return end
        poll_count = poll_count + 1

        -- Drain everything currently available. readAllFromFD returns available
        -- bytes (not blocking to EOF), so large bodies (e.g. a 275 KB X-Ray) arrive
        -- across several reads and MUST be accumulated, not decoded piecemeal.
        local got = false
        while true do
            local size = ffiutil.getNonBlockingReadSize(parent_read_fd)
            if not size or size <= 0 then break end
            local part = ffiutil.readAllFromFD(parent_read_fd)
            if part and #part > 0 then
                chunks[#chunks + 1] = part
                got = true
            else
                break
            end
        end

        if ffiutil.isSubProcessDone(pid) then
            -- Final sweep, then decode the full accumulated body.
            while true do
                local size = ffiutil.getNonBlockingReadSize(parent_read_fd)
                if not size or size <= 0 then break end
                local part = ffiutil.readAllFromFD(parent_read_fd)
                if part and #part > 0 then chunks[#chunks + 1] = part else break end
            end

            local raw = table.concat(chunks)
            dlog("done; total_len=" .. #raw)

            local mpos = raw:find(ERR_MARKER, 1, true)
            if mpos then
                finish(nil, raw:sub(mpos + #ERR_MARKER))
                return
            end
            if raw == "" then
                finish(nil, "empty response")
                return
            end
            local resp, decode_err = rapidjson.decode(raw)
            if not resp then
                finish(nil, "decode error: " .. (decode_err or raw:sub(1, 80)))
            elseif type(resp.error) == "string" and resp.error ~= "" then
                finish(nil, resp.error)
            else
                finish(want_raw and resp or (resp.response or ""), nil)
            end
            return
        end

        if os.time() > deadline then
            finish(nil, "timeout")
            return
        end
        -- Pull remaining data fast while it's flowing; wait longer when idle
        -- (e.g. while the bridge/GPT is still thinking).
        UIManager:scheduleIn(got and 0.1 or POLL_INTERVAL, poll)
    end

    UIManager:scheduleIn(POLL_INTERVAL, poll)

    return function()
        cancelled = true
        if pid then pcall(ffiutil.terminateSubProcess, pid) end
    end
end

--- Upload a local file as raw binary (for EPUB upload to the bridge).
-- headers_table: extra HTTP headers (e.g. X-Book-Title, Content-Type).
-- on_done(resp_table) — parsed JSON response.
-- on_error(err_string).
-- Returns cancel().
function Async.postFile(url, file_path, headers_table, on_done, on_error)
    local function child_fn(pid, child_write_fd)
        local ok, err = pcall(function()
            local su_ok, socketutil = pcall(require, "socketutil")
            if su_ok and socketutil then
                socketutil:set_timeout(60, 300)
            end

            local f = io.open(file_path, "rb")
            if not f then
                ffiutil.writeToFD(child_write_fd, ERR_MARKER .. "cannot open file: " .. file_path)
                return
            end
            f:seek("end")
            local size = f:seek()
            f:seek("set", 0)

            local hdrs = {
                ["Content-Length"] = tostring(size),
                ["Content-Type"]   = "application/epub+zip",
            }
            for k, v in pairs(headers_table) do hdrs[k] = v end

            local pipe_sink = wrap_fd(child_write_fd)
            local ok2, code = http.request({
                url     = url,
                method  = "POST",
                headers = hdrs,
                source  = ltn12.source.file(f),
                sink    = ltn12.sink.file(pipe_sink),
            })

            if not ok2 then
                ffiutil.writeToFD(child_write_fd, ERR_MARKER .. "network: " .. tostring(code))
            elseif code ~= 200 and code ~= 202 then
                ffiutil.writeToFD(child_write_fd, ERR_MARKER .. "HTTP " .. tostring(code))
            end
        end)
        if not ok then
            ffiutil.writeToFD(child_write_fd, ERR_MARKER .. tostring(err))
        end
    end

    local pid, parent_read_fd = ffiutil.runInSubProcess(child_fn, true)
    if not pid then
        on_error("fork unavailable for file upload")
        return function() end
    end

    local cancelled = false
    local completed = false
    local chunks    = {}
    local deadline  = os.time() + 310   -- 5 min for large EPUBs on slow links

    local function cleanup()
        if pid then
            UIManager:scheduleIn(1, function()
                if ffiutil.isSubProcessDone(pid) then
                    if parent_read_fd then pcall(ffiutil.readAllFromFD, parent_read_fd) end
                else
                    UIManager:scheduleIn(3, function()
                        if parent_read_fd then pcall(ffiutil.readAllFromFD, parent_read_fd) end
                        if pid then pcall(ffiutil.terminateSubProcess, pid) end
                    end)
                end
            end)
        end
    end

    local function finish(resp, err)
        if completed then return end
        completed = true
        cleanup()
        if err then on_error(err)
        else on_done(resp) end
    end

    local function poll()
        if cancelled then cleanup(); return end
        local got = false
        while true do
            local size = ffiutil.getNonBlockingReadSize(parent_read_fd)
            if not size or size <= 0 then break end
            local part = ffiutil.readAllFromFD(parent_read_fd)
            if part and #part > 0 then chunks[#chunks + 1] = part; got = true
            else break end
        end

        if ffiutil.isSubProcessDone(pid) then
            while true do
                local size = ffiutil.getNonBlockingReadSize(parent_read_fd)
                if not size or size <= 0 then break end
                local part = ffiutil.readAllFromFD(parent_read_fd)
                if part and #part > 0 then chunks[#chunks + 1] = part else break end
            end
            local raw = table.concat(chunks)
            local mpos = raw:find(ERR_MARKER, 1, true)
            if mpos then finish(nil, raw:sub(mpos + #ERR_MARKER)); return end
            if raw == "" then finish(nil, "empty response"); return end
            local resp, derr = rapidjson.decode(raw)
            if not resp then finish(nil, "decode: " .. (derr or raw:sub(1, 80)))
            elseif type(resp.error) == "string" and resp.error ~= "" then finish(nil, resp.error)
            else finish(resp, nil) end
            return
        end

        if os.time() > deadline then finish(nil, "upload timeout"); return end
        UIManager:scheduleIn(got and 0.5 or POLL_INTERVAL, poll)
    end

    UIManager:scheduleIn(POLL_INTERVAL, poll)
    return function() cancelled = true; if pid then pcall(ffiutil.terminateSubProcess, pid) end end
end

return Async
