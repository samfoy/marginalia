--[[--
bridge.lua — HTTP client for the marginalia bridge server.

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

local Async = require("marginalia_async")

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
        logger.warn("marginalia bridge:", code)
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
    return self:_post("/book-index/init", params)
end

--- Async X-Ray init — non-blocking. Calls on_done(full_response_table) or on_error(err).
-- Used on book open so a slow/flaky network never freezes the UI thread.
function Bridge:xrayInitAsync(params, on_done, on_error)
    return Async.post(self:url("/book-index/init"), params, on_done, on_error, { raw = true })
end

--- Stream the device's open EPUB to the bridge for Book Index generation.
-- Used when /book-index/init returns {status="needs_epub"}.
-- params: {epub_path, book_title, book_author, reading_pct}
-- on_done(resp_table {status, job_id, poll_url}) / on_error(err)
function Bridge:xrayUploadAsync(params, on_done, on_error)
    local headers = {
        ["X-Book-Title"]  = params.book_title  or "",
        ["X-Book-Author"] = params.book_author or "",
        ["X-Reading-Pct"] = tostring(params.reading_pct or 0),
    }
    if self.token and self.token ~= "" then
        headers["X-Marginalia-Token"] = self.token
    end
    return Async.postFile(
        self:url("/book-index/upload-epub"),
        params.epub_path,
        headers,
        on_done,
        on_error
    )
end

--- Poll an in-progress X-Ray generation job.
-- Tight timeouts (3s) keep this synchronous GET well under the 5s ANR
-- threshold on flaky networks; pollXRayStatus retries on timeout.
-- Returns response table or (nil, err).
function Bridge:xrayStatus(job_id)
    return self:_get("/book-index/status/" .. tostring(job_id), 2, 3)
end

--- Report reading progress to keep the bridge cache current.
function Bridge:xrayProgress(book_hash, reading_pct)
    return self:_post("/book-index/progress", {
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

--- Save-to-vault — POST /note. Calls on_done(resp_table) or on_error(err).
-- Uses a SHORT BLOCKING request, not the fork (Async): the note flush often runs
-- concurrently with the /book-index/init subprocess on book-open, and forked siblings
-- cross-talk through inherited pipe FDs — the note latches onto the X-Ray
-- subprocess's 200 JSON (no error field → false success), clearing the queue while
-- the real POST never goes out. Notes are tiny, so a brief blocking call is safe.
function Bridge:noteAsync(params, on_done, on_error)
    local body = {
        highlight   = params.highlight,
        context     = params.context,
        book_title  = params.book_title,
        book_author = params.book_author,
        reading_pct = params.reading_pct,
        query       = params.query,
        response    = params.response,
        mode        = params.mode,
        source      = params.source,
    }
    if self.token and self.token ~= "" then body.token = self.token end
    local body_json, enc_err = rapidjson.encode(body)
    if not body_json then
        if on_error then on_error("encode: " .. (enc_err or "?")) end
        return
    end
    logger.warn("marginalia DBG noteAsync: POST " .. self:url("/note") .. " bytes=" .. tostring(#body_json))
    local sink = {}
    socketutil:set_timeout(4, 7)
    local ok, code = http.request({
        url     = self:url("/note"),
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
        if on_error then on_error("network: " .. tostring(code)) end
        return
    end
    if code ~= 200 then
        if on_error then on_error("HTTP " .. tostring(code)) end
        return
    end
    local resp, derr = rapidjson.decode(table.concat(sink))
    if not resp then
        if on_error then on_error("decode: " .. (derr or "?")) end
    elseif type(resp.error) == "string" and resp.error ~= "" then
        if on_error then on_error(resp.error) end
    else
        logger.info("marginalia: note synced to vault", resp.path or "")
        if on_done then on_done(resp) end
    end
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

--- Create a standalone Obsidian note from a chat response. Blocking (same
-- reason as noteAsync: avoids fork/FD cross-talk with concurrent subprocesses).
-- params: {title, body, book_title, book_author, reading_pct}
-- on_done(resp_table) or on_error(err)
function Bridge:noteNewAsync(params, on_done, on_error)
    local body = {
        title       = params.title,
        body        = params.body,
        book_title  = params.book_title,
        book_author = params.book_author,
        reading_pct = params.reading_pct,
    }
    if self.token and self.token ~= "" then body.token = self.token end
    local body_json, enc_err = rapidjson.encode(body)
    if not body_json then
        if on_error then on_error("encode: " .. (enc_err or "?")) end
        return
    end
    local sink = {}
    socketutil:set_timeout(4, 7)
    local ok, code = http.request({
        url     = self:url("/note-new"),
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
        if on_error then on_error("network: " .. tostring(code)) end
        return
    end
    if code ~= 200 then
        if on_error then on_error("HTTP " .. tostring(code)) end
        return
    end
    local resp, derr = rapidjson.decode(table.concat(sink))
    if not resp then
        if on_error then on_error("decode: " .. (derr or "?")) end
    elseif type(resp.error) == "string" and resp.error ~= "" then
        if on_error then on_error(resp.error) end
    else
        logger.info("marginalia: standalone note saved", resp.path or "")
        if on_done then on_done(resp) end
    end
end

return Bridge
