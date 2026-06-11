--[[--
piread_async.lua — Non-blocking HTTP using ffiutil.runInSubProcess.

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
local MAX_POLLS     = 40    -- 60s max

local ERR_MARKER = "PIREAD_ERR:"

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
    logger.dbg("piread_async: " .. msg)
end

function Async.post(url, body_table, on_done, on_error)
    local body_str = rapidjson.encode(body_table)

    -- ── child function — runs in forked subprocess ──────────────────────────
    -- Must not call require() for new modules — only use parent-captured upvalues.
    local function child_fn(pid, child_write_fd)
        local ok, err = pcall(function()
            -- Set socket timeout (defensive pcall — safe after fork)
            local su_ok, socketutil = pcall(require, "socketutil")
            if su_ok and socketutil then
                socketutil:set_timeout(25, 30)
            end

            local pipe_sink = wrap_fd(child_write_fd)
            local ok2, code = http.request({
                url     = url,
                method  = "POST",
                headers = {
                    ["Content-Type"]   = "application/json",
                    ["Content-Length"] = tostring(#body_str),
                },
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
        logger.warn("piread async: fork unavailable, falling back to blocking")
        local sink = {}
        local ok, code = http.request({
            url     = url,
            method  = "POST",
            headers = {
                ["Content-Type"]   = "application/json",
                ["Content-Length"] = tostring(#body_str),
            },
            source = ltn12.source.string(body_str),
            sink   = ltn12.sink.table(sink),
        })
        if not ok then
            on_error("network: " .. tostring(code))
        else
            local resp, ferr = rapidjson.decode(table.concat(sink))
            if not resp then on_error("decode: " .. (ferr or "?"))
            elseif type(resp.error) == "string" and resp.error ~= "" then on_error(resp.error)
            else on_done(resp.response or "") end
        end
        return function() end
    end

    logger.dbg("piread async: subprocess pid=" .. tostring(pid))
    dlog("fork ok pid=" .. tostring(pid))

    -- ── poll loop ───────────────────────────────────────────────────────────
    local poll_count = 0
    local cancelled  = false
    local completed  = false

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
        dlog("poll #" .. poll_count)

        local size = ffiutil.getNonBlockingReadSize(parent_read_fd)
        local done = ffiutil.isSubProcessDone(pid)
        dlog("size=" .. tostring(size) .. " done=" .. tostring(done))

        if size > 0 or done then
            local raw = ffiutil.readAllFromFD(parent_read_fd) or ""

            if raw:sub(1, #ERR_MARKER) == ERR_MARKER then
                finish(nil, raw:sub(#ERR_MARKER + 1))
                return
            end

            if raw == "" then
                if done then
                    finish(nil, "empty response")
                else
                    -- No data yet and not done — keep polling
                    if poll_count < MAX_POLLS then
                        UIManager:scheduleIn(POLL_INTERVAL, poll)
                    else
                        finish(nil, "timeout")
                    end
                end
                return
            end

            -- Parse JSON response
            local resp, decode_err = rapidjson.decode(raw)
            if not resp then
                finish(nil, "decode error: " .. (decode_err or raw:sub(1, 80)))
            elseif type(resp.error) == "string" and resp.error ~= "" then
                finish(nil, resp.error)
            else
                finish(resp.response or "", nil)
            end
            return
        end

        -- Still waiting
        if poll_count >= MAX_POLLS then
            finish(nil, "timeout")
            return
        end
        UIManager:scheduleIn(POLL_INTERVAL, poll)
    end

    UIManager:scheduleIn(POLL_INTERVAL, poll)

    return function()
        cancelled = true
        if pid then pcall(ffiutil.terminateSubProcess, pid) end
    end
end

return Async
