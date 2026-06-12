--[[--
bridge.lua — HTTP client for piread-bridge server on Mac.

Public API:
  Bridge:ask(params)              → response_text | nil, err
  Bridge:xrayInit(params)         → response_table | nil, err
  Bridge:xrayStatus(job_id)       → response_table | nil, err
  Bridge:xrayProgress(hash, pct)  → ok | nil, err
  Bridge:ping()                   → bool
--]]--

local http       = require("socket.http")
local ltn12      = require("ltn12")
local rapidjson  = require("rapidjson")
local socketutil = require("socketutil")
local logger     = require("logger")

local Async = require("piread_async")

local Bridge = {
    host          = "macbook.local",
    port          = 7731,
    token         = "",
    TIMEOUT_BLOCK = 20,
    TIMEOUT_TOTAL = 25,
    PING_BLOCK    = 3,
    PING_TOTAL    = 5,
}

function Bridge:url(path)
    return string.format("http://%s:%d%s", self.host, self.port, path)
end

-- ── Low-level HTTP ─────────────────────────────────────────────────────────────

function Bridge:_get(path, block_t, total_t)
    local sink = {}
    socketutil:set_timeout(block_t or self.PING_BLOCK, total_t or self.PING_TOTAL)
    local ok, code = http.request({
        url    = self:url(path),
        method = "GET",
        sink   = ltn12.sink.table(sink),
    })
    socketutil:reset_timeout()
    if not ok then
        return nil, "network: " .. (code or "unreachable")
    end
    if code ~= 200 then
        return nil, "HTTP " .. tostring(code)
    end
    local resp, err = rapidjson.decode(table.concat(sink))
    if not resp then return nil, "decode: " .. (err or "?") end
    return resp
end

function Bridge:_post(path, params)
    if self.token and self.token ~= "" then
        params.token = self.token
    end
    local body_json, enc_err = rapidjson.encode(params)
    if not body_json then
        return nil, "encode: " .. (enc_err or "?")
    end
    local sink = {}
    socketutil:set_timeout(self.TIMEOUT_BLOCK, self.TIMEOUT_TOTAL)
    local ok, code = http.request({
        url     = self:url(path),
        method  = "POST",
        source  = ltn12.source.string(body_json),
        sink    = ltn12.sink.table(sink),
        headers = {
            ["Content-Type"]   = "application/json",
            ["Content-Length"] = tostring(#body_json),
        },
    })
    socketutil:reset_timeout()
    if not ok then
        logger.warn("piread bridge:", code)
        return nil, "Bridge unreachable (" .. (code or "no route") .. ")"
    end
    -- Accept both 200 (ready) and 202 (generating)
    if code ~= 200 and code ~= 202 then
        return nil, "Server error (HTTP " .. tostring(code) .. ")"
    end
    local resp, err = rapidjson.decode(table.concat(sink))
    if not resp then return nil, "Bad response: " .. (err or "?") end
    return resp
end

-- ── Public API ─────────────────────────────────────────────────────────────────

--- Quick reachability check.
function Bridge:ping()
    socketutil:set_timeout(self.PING_BLOCK, self.PING_TOTAL)
    local ok, code = http.request(self:url("/ping"))
    socketutil:reset_timeout()
    return ok ~= nil and code == 200
end

--- Conversational query (explain / translate / summarize).
-- params: {text, context, book_title, book_author, mode}
-- Returns (response_text, nil) or (nil, err)
function Bridge:ask(params)
    local resp, err = self:_post("/ask", params)
    if err then return nil, err end
    if resp.error then return nil, resp.error end
    return resp.response
end

--- Freeform chat with book context.
-- params: {question, book_title, book_author, reading_pct, xray}
function Bridge:chat(params)
    -- Build a compact xray summary to send (characters + recent events)
    local xray_summary = nil
    if params.xray then
        local parts = {}
        local chars = params.xray.characters or {}
        if #chars > 0 then
            local names = {}
            for i = 1, math.min(#chars, 20) do
                table.insert(names, chars[i].name)
            end
            table.insert(parts, "Characters: " .. table.concat(names, ", "))
        end
        local events = params.xray.timeline or {}
        local pct = params.reading_pct or 0
        local recent = {}
        for _, ev in ipairs(events) do
            if (ev.chapter_pct or 0) <= pct then
                table.insert(recent, ev)
            end
        end
        -- Last 8 events the reader has reached
        local start = math.max(1, #recent - 7)
        local event_lines = {}
        for i = start, #recent do
            table.insert(event_lines, recent[i].summary or recent[i].event or "")
        end
        if #event_lines > 0 then
            table.insert(parts, "Recent story events: " .. table.concat(event_lines, "; "))
        end
        if #parts > 0 then
            xray_summary = table.concat(parts, "\n")
        end
    end

    local payload = {
        question    = params.question,
        book_title  = params.book_title,
        book_author = params.book_author,
        reading_pct = params.reading_pct,
        xray_summary = xray_summary,
        mode        = "chat",
    }
    local resp, err = self:_post("/chat", payload, self.TIMEOUT_BLOCK, self.TIMEOUT_TOTAL)
    if err then return nil, err end
    if resp.error then return nil, resp.error end
    return resp.response
end

--- Initialise X-Ray for a book.
-- params: {book_title, book_author, reading_pct}
-- Returns:
--   {status="ready",      xray={...}, book={...}}  ← cache hit
--   {status="generating", job_id="...", poll_url}  ← background job
-- or (nil, err)
function Bridge:xrayInit(params)
    return self:_post("/xray/init", params)
end

--- Poll an in-progress X-Ray generation job.
-- Returns response table or (nil, err).
function Bridge:xrayStatus(job_id)
    return self:_get("/xray/status/" .. tostring(job_id))
end

--- Report reading progress to keep the bridge cache current.
function Bridge:xrayProgress(book_hash, reading_pct)
    return self:_post("/xray/progress", {
        book_hash   = book_hash,
        reading_pct = reading_pct,
    })
end

--- Async ask — returns cancel(). Calls on_done(text) or on_error(err).
function Bridge:askAsync(params, on_done, on_error)
    return Async.post(self:url("/ask"), params, on_done, on_error)
end

--- Async chat — returns cancel(). Calls on_done(text) or on_error(err).
function Bridge:chatAsync(params, on_done, on_error)
    -- Build compact xray summary inline (same logic as chat(), but for the payload)
    local xray_summary = nil
    if params.xray then
        local parts = {}
        local chars = params.xray.characters or {}
        if #chars > 0 then
            local names = {}
            for i = 1, math.min(#chars, 20) do
                table.insert(names, chars[i].name)
            end
            table.insert(parts, "Characters: " .. table.concat(names, ", "))
        end
        local events = params.xray.timeline or {}
        local pct = params.reading_pct or 0
        local recent = {}
        for _, ev in ipairs(events) do
            if (ev.chapter_pct or 0) <= pct then
                table.insert(recent, ev)
            end
        end
        local start = math.max(1, #recent - 7)
        local event_lines = {}
        for i = start, #recent do
            table.insert(event_lines, recent[i].summary or recent[i].event or "")
        end
        if #event_lines > 0 then
            table.insert(parts, "Recent events: " .. table.concat(event_lines, "; "))
        end
        if #parts > 0 then xray_summary = table.concat(parts, "\n") end
    end

    local payload = {
        question     = params.question,
        book_title   = params.book_title,
        book_author  = params.book_author,
        reading_pct  = params.reading_pct,
        xray_summary = xray_summary,
        page_text    = params.page_text,
    }
    return Async.post(self:url("/chat"), payload, on_done, on_error)
end

--- Async save-to-vault — POST /note. Calls on_done(resp_table) or on_error(err).
function Bridge:noteAsync(params, on_done, on_error)
    return Async.post(self:url("/note"), {
        highlight   = params.highlight,
        context     = params.context,
        book_title  = params.book_title,
        book_author = params.book_author,
        reading_pct = params.reading_pct,
    }, on_done, on_error)
end

--- Async recap — "where you left off" (spoiler-bounded). on_done(text)/on_error(err).
function Bridge:recapAsync(params, on_done, on_error)
    return Async.post(self:url("/recap"), {
        book_title  = params.book_title,
        book_author = params.book_author,
        reading_pct = params.reading_pct,
    }, on_done, on_error)
end

--- Async AI Wiki deep-dive on one entity (spoiler-bounded). on_done(text)/on_error(err).
function Bridge:wikiAsync(params, on_done, on_error)
    return Async.post(self:url("/wiki"), {
        book_title  = params.book_title,
        book_author = params.book_author,
        entity_name = params.entity_name,
        entity_kind = params.entity_kind,
        known       = params.known,
        reading_pct = params.reading_pct,
    }, on_done, on_error)
end

--- Async Section X-Ray for one chapter/part. on_done(text)/on_error(err).
function Bridge:sectionAsync(params, on_done, on_error)
    return Async.post(self:url("/section"), {
        book_title    = params.book_title,
        book_author   = params.book_author,
        chapter_title = params.chapter_title,
        start_pct     = params.start_pct,
        end_pct       = params.end_pct,
    }, on_done, on_error)
end

return Bridge
