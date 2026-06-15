--[[--
main.lua — Pi reading assistant plugin for KOReader.

On book open: silently requests X-Ray from piread-bridge (or serves from
local cache instantly). Adds "Ask Pi" to the highlight dialog for
conversational queries, and "Pi X-Ray" to the menu for the entity browser.

Requires piread-bridge running on the same local network as the device.
All features degrade gracefully when offline or bridge is unreachable.
--]]--

local ButtonDialog     = require("ui/widget/buttondialog")
local InputDialog      = require("ui/widget/inputdialog")
local Device           = require("device")
local Dispatcher       = require("dispatcher")
local InfoMessage      = require("ui/widget/infomessage")
local NetworkMgr       = require("ui/network/manager")
local Screen = require("device").screen
local TextViewer       = require("ui/widget/textviewer")
local UIManager        = require("ui/uimanager")
local Event            = require("ui/event")
local WidgetContainer  = require("ui/widget/container/widgetcontainer")
local logger           = require("logger")
local util             = require("util")
local T                = require("ffi/util").template
local _                = require("gettext")

local Bridge    = require("bridge")
local Cache     = require("piread_cache")
local Context   = require("piread_context")
local XRayUI    = require("piread_xray")
local Queue     = require("piread_queue")

local PiRead = WidgetContainer:extend{
    name = "piread",
    -- Populated on init:
    _xray        = nil,    -- current book's X-Ray data (table)
    _book_hash   = nil,    -- epub hash (from bridge)
    _book_meta   = nil,    -- {title, author, series, ...}
    _mentions    = nil,    -- per-entity chapter mention index {name_lower: [{chapter,position_pct,snippet}]}
    _xray_job_id = nil,    -- pending generation job id
    _poll_handle = nil,    -- UIManager scheduled handle for polling
}

local SETTINGS_KEY   = "piread"
local POLL_INTERVAL  = 10    -- seconds between status polls during X-Ray generation
local PROGRESS_EVERY = 5     -- report reading position every N% change

local DEFAULT_SETTINGS = {
    host    = "macbook.local",
    port    = 7731,
    token   = "",
    enabled = true,
    spoiler_free = true,    -- hide characters/events past reading position
    auto_capture = true,    -- on a successful Ask Pi lookup, highlight the passage
                            -- (note = Pi's answer) and save it to the Obsidian note
}

-- ── Settings ──────────────────────────────────────────────────────────────────

function PiRead:loadSettings()
    local s = G_reader_settings:readSetting(SETTINGS_KEY) or {}
    for k, v in pairs(DEFAULT_SETTINGS) do
        if s[k] == nil then s[k] = v end
    end
    return s
end

function PiRead:saveSettings(s)
    G_reader_settings:saveSetting(SETTINGS_KEY, s)
end

function PiRead:applySettings()
    local s = self:loadSettings()
    Bridge.host  = s.host
    Bridge.port  = s.port
    Bridge.token = s.token
end

-- ── Lifecycle ─────────────────────────────────────────────────────────────────

function PiRead:init()
    self:applySettings()
    self:onDispatcherRegisterActions()
    if self.document then
        self:hookHighlightDialog()
    end
    self.ui.menu:registerToMainMenu(self)
end

function PiRead:onDispatcherRegisterActions()
    Dispatcher:registerAction("piread_now_reading", {
        category = "none",
        event    = "PiReadNowReading",
        title    = _("Pi: Now Reading dashboard"),
        reader   = true,
    })
    Dispatcher:registerAction("piread_recap", {
        category = "none",
        event    = "PiReadRecap",
        title    = _("Pi: Recap where I left off"),
        reader   = true,
    })
    Dispatcher:registerAction("piread_section", {
        category = "none",
        event    = "PiReadSection",
        title    = _("Pi: Section X-Ray (this chapter)"),
        reader   = true,
    })
    Dispatcher:registerAction("piread_ask", {
        category = "none",
        event    = "PiReadAsk",
        title    = _("Pi: Ask Pi"),
        reader   = true,
    })
    Dispatcher:registerAction("piread_xray", {
        category = "none",
        event    = "PiReadXRay",
        title    = _("Pi: X-Ray browser"),
        reader   = true,
    })
end

function PiRead:onPiReadNowReading()
    local s = self:loadSettings()
    if not s.enabled then return end
    local pct = s.spoiler_free and self:currentReadingPct() or nil
    XRayUI.setContext(self:_xrayContext())
    Context.show(self.ui, self._xray, Bridge, pct, function() self:showChatDialog() end)
end

function PiRead:onPiReadRecap()
    if not self:loadSettings().enabled then return true end
    self:showRecap()
    return true
end

function PiRead:onPiReadSection()
    if not self:loadSettings().enabled then return true end
    self:showSectionXRay()
    return true
end

function PiRead:onPiReadAsk()
    if not self:loadSettings().enabled then return true end
    self:showChatDialog()
    return true
end

function PiRead:onPiReadXRay()
    local s = self:loadSettings()
    if not s.enabled then return true end
    if not self._xray then
        UIManager:show(InfoMessage:new{
            text    = self._xray_job_id
                        and _("X-Ray is being generated. Try again in a minute.")
                        or  _("No X-Ray data. Open a book that's in your Calibre library."),
            timeout = 4,
        })
        return true
    end
    local pct = s.spoiler_free and self:currentReadingPct() or nil
    XRayUI.setContext(self:_xrayContext())
    XRayUI.showMenu(self._xray, pct)
    return true
end

function PiRead:onReaderReady()
    self:hookHighlightDialog()
    self:onDocLoad()
end

function PiRead:onDocLoad()
    local s = self:loadSettings()
    if not s.enabled then return end

    -- Opportunistically flush any notes saved while offline.
    if Queue.count() > 0 then
        UIManager:scheduleIn(5, function()
            self:flushNoteQueue(function(sent, remaining)
                if sent > 0 then
                    UIManager:show(InfoMessage:new{
                        text = T(_("Pi: synced %1 saved note(s) to vault."), sent),
                        timeout = 3,
                    })
                end
            end)
        end)
    end

    local props  = self.ui.doc_props or (self.ui.document and self.ui.document:getProps()) or {}
    local title  = (props.title)   or ""
    local author = (props.authors) or ""
    if title == "" then return end

    -- Load from device cache immediately (instant)
    local record, hash = Cache.findByTitle(title)
    if record and record.xray then
        logger.info("piread: X-Ray loaded from local cache:", title)
        self._xray           = record.xray
        self._book_hash      = hash or record.book and record.book.epub_hash
        self._book_meta      = record.book
        self._mentions       = record.mentions
        self._local_gen_at   = record.generated_at or ""
        -- Background freshness check — silently update if Mac has newer version
        UIManager:scheduleIn(3, function()
            self:checkXRayFreshness(title, author)
        end)
        self:_maybeOfferRecap()
        return
    end

    -- No local cache — try bridge
    local reading_pct = self:currentReadingPct()
    UIManager:scheduleIn(2, function()
        self:requestXRay(title, author, reading_pct)
    end)
end

function PiRead:checkXRayFreshness(title, author)
    if not NetworkMgr:isConnected() then return end
    local gen_at = self._local_gen_at or ""
    -- Async so a slow network never blocks the UI thread on book open.
    Bridge:xrayInitAsync({
        book_title          = title,
        book_author         = author,
        reading_pct         = self:currentReadingPct(),
        device_generated_at = gen_at,
    }, function(resp)
        if not resp then return end
        if resp.status == "current" then
            logger.info("piread: X-Ray is current for", title)
            return
        end
        if resp.status == "ready" and resp.xray then
            logger.info("piread: X-Ray updated from bridge for", title)
            self._xray      = resp.xray
            self._book_meta = resp.book
            self._book_hash = resp.book and resp.book.epub_hash
            self._mentions  = resp.mentions
            self._local_gen_at = resp.generated_at or ""
            local bh = self._book_hash
            if bh then
                Cache.saveXray(bh, {
                    xray         = resp.xray,
                    book         = resp.book,
                    mentions     = resp.mentions,
                    generated_at = resp.generated_at,
                })
                logger.info("piread: Device X-Ray cache refreshed for", title)
            end
        elseif resp.status == "generating" then
            self._xray_job_id = resp.job_id
            self:schedulePoll()
        end
    end, function(err)
        logger.warn("piread: freshness check error:", err)
    end)
end

function PiRead:currentReadingPct()
    if not (self.ui and self.ui.document) then return 0 end
    -- Prefer doc_settings percent_finished (more accurate, matches Hardcover sync)
    if self.ui.doc_settings then
        local pf = self.ui.doc_settings:readSetting("percent_finished")
        if pf and pf > 0 then return math.floor(pf * 100 + 0.5) end
    end
    local cur   = self.ui:getCurrentPage()
    local total = self.ui.document:getPageCount()
    if not cur or not total or total == 0 then return 0 end
    return math.floor(cur / total * 100)
end

-- Extract text of current page(s) to send as reading context.
function PiRead:getCurrentPageText(max_chars)
    max_chars = max_chars or 3000
    if not (self.ui and self.ui.document) then return nil end
    local doc = self.ui.document
    local ok, text = pcall(function()
        if not doc.info.has_pages then
            -- EPUB: use XPointers
            if not doc.getXPointer or not doc.getPageXPointer or not doc.getTextFromXPointers then
                return nil
            end
            local cur_xp = doc:getXPointer()
            if not cur_xp then return nil end
            local cur_page = doc:getPageFromXPointer(cur_xp) or 1
            local start_page = math.max(1, cur_page - 2)
            local end_page   = math.min(doc.info.number_of_pages or cur_page, cur_page + 1)
            local start_xp   = doc:getPageXPointer(start_page)
            local end_xp     = doc:getPageXPointer(end_page)
            if start_xp and end_xp then
                return doc:getTextFromXPointers(start_xp, end_xp)
            end
        else
            -- PDF: extract current page text
            if not doc.getPageText then return nil end
            local page = self.ui.view and self.ui.view.state and self.ui.view.state.page or 1
            local t = doc:getPageText(page)
            if type(t) == "string" then return t end
        end
        return nil
    end)
    if not ok or not text or text == "" then return nil end
    if #text > max_chars then text = text:sub(1, max_chars) end
    return text
end

-- Animated loading indicator. Returns close() function.
function PiRead:showLoadingAnim(label)
    local base   = label or _("Asking Pi")
    local dots   = { ".", "..", "..." }
    local idx    = 1
    local task   = nil
    local closed = false

    -- Single persistent InfoMessage — update text in place
    local dialog = InfoMessage:new{ text = base .. dots[idx] }
    UIManager:show(dialog)

    local function tick()
        if closed then return end
        idx = (idx % #dots) + 1
        -- Update text without closing/reopening (avoids e-ink repaint storm)
        if dialog.text_widget then
            pcall(function() dialog.text_widget:setText(base .. dots[idx]) end)
        end
        task = UIManager:scheduleIn(0.8, tick)
    end
    task = UIManager:scheduleIn(0.8, tick)

    return function()
        if closed then return end
        closed = true
        if task then UIManager:unschedule(task) end
        UIManager:close(dialog)
    end
end

-- ── X-Ray request & polling ───────────────────────────────────────────────────

function PiRead:requestXRay(title, author, reading_pct)
    logger.info("piread: requesting X-Ray for", title)
    Bridge:xrayInitAsync({
        book_title  = title,
        book_author = author,
        reading_pct = reading_pct or 0,
    }, function(resp)
        if not resp then return end
        if resp.status == "ready" then
            logger.info("piread: X-Ray ready (cached=%s)", tostring(resp.cached))
            self:_storeXRay(resp)
        elseif resp.status == "generating" then
            logger.info("piread: X-Ray generating, job_id=%s", tostring(resp.job_id))
            self._xray_job_id = resp.job_id
            self:schedulePoll()
            UIManager:show(InfoMessage:new{
                text    = _("Pi is building your X-Ray…"),
                timeout = 4,
            })
        end
    end, function(err)
        logger.warn("piread: /xray/init error:", err)
    end)
end

function PiRead:schedulePoll()
    if self._poll_handle then
        UIManager:unschedule(self._poll_handle)
    end
    self._poll_handle = function() self:pollXRayStatus() end
    UIManager:scheduleIn(POLL_INTERVAL, self._poll_handle)
end

function PiRead:pollXRayStatus()
    self._poll_handle = nil
    if not self._xray_job_id then return end
    if not NetworkMgr:isConnected() then
        -- No network, retry later
        self:schedulePoll()
        return
    end

    local resp, err = Bridge:xrayStatus(self._xray_job_id)
    if err then
        logger.warn("piread: status poll error:", err)
        self:schedulePoll()  -- retry
        return
    end

    if resp.status == "ready" then
        self._xray_job_id = nil
        self:_storeXRay(resp)
        UIManager:show(InfoMessage:new{
            text    = _("Pi X-Ray ready!"),
            timeout = 3,
        })

    elseif resp.status == "failed" then
        self._xray_job_id = nil
        logger.warn("piread: X-Ray generation failed:", resp.error)

    else
        -- Still generating
        logger.info("piread: still generating (%s)", resp.progress or "…")
        self:schedulePoll()
    end
end

function PiRead:_storeXRay(resp)
    if not resp or not resp.xray then return end
    self._xray      = resp.xray
    self._book_meta = resp.book
    self._mentions  = resp.mentions
    local hash      = resp.book and resp.book.epub_hash
    self._book_hash = hash
    -- Save to local device cache
    if hash then
        Cache.saveXray(hash, {
            xray         = resp.xray,
            book         = resp.book,
            mentions     = resp.mentions,
            generated_at = resp.generated_at,
        })
    end
end

-- ── Companion context + on-demand features ────────────────────────────────────

-- Build the context handed to XRayUI so entity views can offer "Tell me more"
-- (AI Wiki) and "Where appears" (jump-to-mention).
function PiRead:_xrayContext()
    local props = self.ui.doc_props or (self.ui.document and self.ui.document:getProps()) or {}
    return {
        bridge      = Bridge,
        ui          = self.ui,
        book_title  = props.title   or "",
        book_author = props.authors or "",
        reading_pct = self:currentReadingPct(),
        mentions    = self._mentions,
    }
end

local RESUME_GAP = 8 * 3600  -- only auto-offer a recap after an 8h+ gap

-- On book open, offer a spoiler-bounded "where you left off" recap if the reader
-- is mid-book and hasn't opened it recently.
function PiRead:_maybeOfferRecap()
    if not (self._xray and self._book_hash) then return end
    local pct = self:currentReadingPct()
    if pct < 5 or pct > 97 then return end
    local seen = G_reader_settings:readSetting("piread_seen") or {}
    local last = seen[self._book_hash] or 0
    local now  = os.time()
    seen[self._book_hash] = now
    G_reader_settings:saveSetting("piread_seen", seen)
    if (now - last) < RESUME_GAP then return end
    UIManager:scheduleIn(1.5, function()
        local dialog
        dialog = ButtonDialog:new{
            title       = _("Welcome back — want a quick recap of where you left off?"),
            title_align = "center",
            buttons     = {{
                { text = _("Not now"), callback = function() UIManager:close(dialog) end },
                { text = _("Recap"),   callback = function()
                    UIManager:close(dialog)
                    self:showRecap()
                end },
            }},
        }
        UIManager:show(dialog)
    end)
end

-- Spoiler-bounded recap of the story up to the reader's current position.
function PiRead:showRecap()
    local s = self:loadSettings()
    if not s.enabled then return end
    if not NetworkMgr:isConnected() then
        UIManager:show(InfoMessage:new{ text = _("Not connected — recap needs the bridge."), timeout = 4 })
        return
    end
    local props = self.ui.doc_props or (self.ui.document and self.ui.document:getProps()) or {}
    local close_loading = self:showLoadingAnim(_("Pi is recapping"))
    Bridge:recapAsync({
        book_title  = props.title   or "",
        book_author = props.authors or "",
        reading_pct = self:currentReadingPct(),
    }, function(text)
        close_loading()
        UIManager:show(TextViewer:new{
            title  = _("Where you left off"),
            text   = text,
            width  = math.floor(Screen:getWidth()  * 0.92),
            height = math.floor(Screen:getHeight() * 0.78),
        })
    end, function(err)
        close_loading()
        UIManager:show(InfoMessage:new{ text = T(_("Pi: %1"), err), timeout = 6 })
    end)
end

-- Section X-Ray: analyze the current chapter (bounded to reading position).
function PiRead:showSectionXRay()
    if not (self.ui and self.ui.document) then return end
    if not NetworkMgr:isConnected() then
        UIManager:show(InfoMessage:new{ text = _("Not connected — Section X-Ray needs the bridge."), timeout = 4 })
        return
    end
    local props  = self.ui.doc_props or (self.ui.document and self.ui.document:getProps()) or {}
    local cur    = self.ui:getCurrentPage() or 1
    local total  = self.ui.document:getPageCount() or 1
    local read_pct = self:currentReadingPct()

    local chapter_title, start_pct, end_pct
    local ok_toc, toc = pcall(function() return self.ui.document:getToc() end)
    if ok_toc and toc and #toc > 0 then
        for i = #toc, 1, -1 do
            if (toc[i].page or 1) <= cur then
                chapter_title = toc[i].title
                start_pct     = (toc[i].page or 1) / total * 100
                end_pct       = (i < #toc) and ((toc[i+1].page - 1) / total * 100) or 100
                break
            end
        end
    end
    start_pct = start_pct or math.max(0, (cur / total * 100) - 6)
    end_pct   = end_pct   or (cur / total * 100)
    -- Never analyze past where the reader actually is.
    if end_pct > read_pct then end_pct = read_pct end
    if end_pct <= start_pct then end_pct = math.min(100, start_pct + 2) end

    local close_loading = self:showLoadingAnim(_("Analyzing this chapter"))
    Bridge:sectionAsync({
        book_title    = props.title   or "",
        book_author   = props.authors or "",
        chapter_title = chapter_title,
        start_pct     = start_pct,
        end_pct       = end_pct,
    }, function(text)
        close_loading()
        UIManager:show(TextViewer:new{
            title  = chapter_title and T(_("Section: %1"), chapter_title) or _("This section"),
            text   = text,
            width  = math.floor(Screen:getWidth()  * 0.92),
            height = math.floor(Screen:getHeight() * 0.78),
        })
    end, function(err)
        close_loading()
        UIManager:show(InfoMessage:new{ text = T(_("Pi: %1"), err), timeout = 6 })
    end)
end

-- ── Highlight dialog hook ─────────────────────────────────────────────────────

function PiRead:hookHighlightDialog()
    self.ui.highlight:addToHighlightDialog("11_ask_pi", function(this)
        local s = self:loadSettings()
        if not s.enabled then return nil end

        return {
            text = _("Ask Pi"),
            callback = function()
                local sel = this.selected_text
                if not (sel and sel.text and sel.text ~= "") then return end

                local text = util.cleanupSelectedText(sel.text)
                local prev_ctx, next_ctx = this:getSelectedWordContext(40)
                local props = this.ui.doc_props or (this.ui.document and this.ui.document:getProps()) or {}
                local book_title  = props.title   or ""
                local book_author = props.authors or ""

                -- Capture the selection geometry now, before onClose clears it, so a
                -- successful lookup can highlight the passage + attach Pi's answer.
                local captured = {
                    pos0 = sel.pos0, pos1 = sel.pos1,
                    text = text,
                    drawer = sel.drawer, color = sel.color,
                    datetime = sel.datetime,
                    pboxes = sel.pboxes, ext = sel.ext,
                }

                -- First: check if this word is in the local X-Ray cache
                if self._xray then
                    local kind, entity = XRayUI.lookup(self._xray, text)
                    if kind and entity then
                        this:onClose()
                        -- Check spoiler guard
                        local s2 = self:loadSettings()
                        if s2.spoiler_free then
                            local pct = self:currentReadingPct()
                            if (entity.first_appearance_pct or 0) > pct + 5 then
                                UIManager:show(InfoMessage:new{
                                    text    = _("This character/place appears later in the book."),
                                    timeout = 4,
                                })
                                return
                            end
                        end
                        XRayUI.showLookupResult(kind, entity)
                        return
                    end
                end

                -- Not in cache — ask bridge
                this:onClose()
                self:showModeDialog(text, prev_ctx, next_ctx, book_title, book_author, captured)
            end,
        }
    end)

    -- ── Save to note (with optional context) ───────────────────────────────
    self.ui.highlight:addToHighlightDialog("12_pi_save_note", function(this)
        local s = self:loadSettings()
        if not s.enabled then return nil end

        return {
            text = _("Pi: Save Note"),
            callback = function()
                local sel = this.selected_text
                if not (sel and sel.text and sel.text ~= "") then return end

                local highlight_text = util.cleanupSelectedText(sel.text)

                -- Show optional context input dialog
                local dialog
                dialog = InputDialog:new{
                    title       = _("Add context (optional)"),
                    input       = "",
                    input_hint  = _("Your thought, question, or tag…"),
                    input_type  = "text",
                    buttons = {{
                        {
                            text     = _("Cancel"),
                            id       = "close",
                            callback = function() UIManager:close(dialog) end,
                        },
                        {
                            text             = _("Save"),
                            is_enter_default = true,
                            callback         = function()
                                local user_context = dialog:getInputText() or ""
                                UIManager:close(dialog)
                                this:onClose()
                                self:saveHighlightNote(highlight_text, user_context)
                            end,
                        },
                    }},
                }
                UIManager:show(dialog)
            end,
        }
    end)
end

-- ── Mode picker ───────────────────────────────────────────────────────────────

local MODES = {
    { id = "whois",     label = _("Who / What is this?") },
    { id = "explain",   label = _("Explain this passage") },
    { id = "summarize", label = _("Story context so far") },
    { id = "translate", label = _("Translate to English") },
}

function PiRead:showModeDialog(text, prev_ctx, next_ctx, book_title, book_author, captured)
    local buttons = {}
    for _, mode in ipairs(MODES) do
        local mid, mlabel = mode.id, mode.label
        table.insert(buttons, {{ text = mlabel, callback = function()
            UIManager:close(self._mode_dialog)
            self._mode_dialog = nil
            self:askBridge(text, prev_ctx, next_ctx, book_title, book_author, mid, mlabel, captured)
        end }})
    end
    table.insert(buttons, {{ text = _("Cancel"), callback = function()
        UIManager:close(self._mode_dialog)
        self._mode_dialog = nil
    end }})

    self._mode_dialog = ButtonDialog:new{
        title       = _("Ask Pi"),
        title_align = "center",
        buttons     = buttons,
    }
    UIManager:show(self._mode_dialog)
end

-- ── Ask bridge (conversational) ───────────────────────────────────────────────

function PiRead:askBridge(text, prev_ctx, next_ctx, book_title, book_author, mode_id, mode_label, captured)
    local close_loading = self:showLoadingAnim(_("Asking Pi"))

    local ctx = ""
    if prev_ctx and prev_ctx ~= "" then ctx = prev_ctx .. " " end
    if next_ctx and next_ctx ~= "" then ctx = ctx .. next_ctx end

    Bridge:askAsync({
        text        = text,
        context     = ctx ~= "" and ctx or nil,
        book_title  = book_title ~= "" and book_title  or nil,
        book_author = book_author ~= "" and book_author or nil,
        mode        = mode_id,
    }, function(response)
        close_loading()
        UIManager:show(TextViewer:new{
            title  = mode_label,
            text   = response,
            width  = math.floor(Screen:getWidth()  * 0.92),
            height = math.floor(Screen:getHeight() * 0.78),
        })
        -- Highlight the passage + save to the Obsidian note (if auto-capture on).
        self:captureLookup(captured, mode_label, mode_id, text,
                           ctx ~= "" and ctx or nil, response, book_title, book_author)
    end, function(err)
        close_loading()
        logger.warn("piread ask:", err)
        UIManager:show(InfoMessage:new{ text = T(_("Pi: %1"), err), timeout = 6 })
    end)
end

-- ── Capture a lookup: highlight in book + Obsidian note ─────────────────────────

-- Create a saved highlight at the captured selection with Pi's answer as its
-- note. Mirrors ReaderHighlight:saveHighlight but builds the annotation item
-- from the captured geometry (the live selection is already closed). Returns
-- true on success.
function PiRead:createHighlightWithNote(captured, label, answer)
    local rh = self.ui.highlight
    if not (rh and self.ui.annotation and captured and captured.pos0 and captured.pos1) then
        return false
    end
    local note = "\u{1F916} Pi" .. (label and (" · " .. label) or "") .. "\n\n" .. (answer or "")
    local pg_or_xp = self.ui.rolling and captured.pos0 or (captured.pos0 and captured.pos0.page)
    local item = {
        page     = self.ui.paging and (captured.pos0 and captured.pos0.page) or captured.pos0,
        pos0     = captured.pos0,
        pos1     = captured.pos1,
        text     = captured.text,
        drawer   = captured.drawer or (rh.view and rh.view.highlight.saved_drawer),
        color    = captured.color  or (rh.view and rh.view.highlight.saved_color),
        note     = note,
        chapter  = (self.ui.toc and pg_or_xp) and self.ui.toc:getTocTitleByPage(pg_or_xp) or nil,
    }
    if self.ui.paging then
        item.pboxes = captured.pboxes
        item.ext    = captured.ext
        pcall(function() rh:writePdfAnnotation("save", item) end)
    end
    local ok, index = pcall(function() return self.ui.annotation:addItem(item) end)
    if not ok or not index then
        logger.warn("piread: addItem failed:", index)
        return false
    end
    pcall(function() rh.view.footer:maybeUpdateFooter() end)
    self.ui:handleEvent(Event:new("AnnotationsModified",
        { item, nb_highlights_added = 1, index_modified = index }))
    return true
end

-- Highlight the looked-up passage and persist the passage + question + answer
-- to the book's Obsidian note. No-op unless auto_capture is enabled.
function PiRead:captureLookup(captured, mode_label, mode_id, query, context, response, book_title, book_author)
    local s = self:loadSettings()
    if not s.auto_capture then return end
    if not (response and response ~= "") then return end

    pcall(function() self:createHighlightWithNote(captured, mode_label, response) end)

    local note = {
        highlight   = (captured and captured.text) or query,
        context     = (context and context ~= "") and context or nil,
        query       = query,
        response    = response,
        mode        = mode_label,
        source      = "Ask Pi",
        book_title  = (book_title and book_title ~= "") and book_title or nil,
        book_author = (book_author and book_author ~= "") and book_author or nil,
        reading_pct = self:currentReadingPct(),
    }
    Queue.enqueue(note)
    if NetworkMgr:isConnected() then
        self:flushNoteQueue(function() end)
    end
end

-- ── Save highlight + optional context to Obsidian vault via bridge ─────────────

function PiRead:saveHighlightNote(highlight_text, user_context)
    local props  = self.ui.doc_props or (self.ui.document and self.ui.document:getProps()) or {}
    local note = {
        highlight   = highlight_text,
        context     = (user_context and user_context ~= "") and user_context or nil,
        book_title  = (props.title   and props.title   ~= "") and props.title   or nil,
        book_author = (props.authors and props.authors ~= "") and props.authors or nil,
        reading_pct = self:currentReadingPct(),
    }
    -- Durable: write to the local queue first so the note survives an offline
    -- save or a mid-send crash. The flush removes it once the vault confirms.
    Queue.enqueue(note)

    if not NetworkMgr:isConnected() then
        UIManager:show(InfoMessage:new{
            text = _("Saved — will sync to vault when online."), timeout = 3 })
        return
    end

    local close_loading = self:showLoadingAnim(_("Saving to vault…"))
    self:flushNoteQueue(function(sent, remaining)
        close_loading()
        if remaining > 0 then
            UIManager:show(InfoMessage:new{
                text = T(_("Saved — %1 note(s) pending sync."), remaining), timeout = 4 })
        else
            UIManager:show(InfoMessage:new{ text = _("Saved to vault."), timeout = 3 })
        end
    end)
end

-- Flush queued notes to the vault, oldest first. Sends each via the bridge and
-- removes it from the queue on confirmation; stops on the first failure (the
-- rest stay queued). Calls on_complete(sent_count, remaining_count).
function PiRead:flushNoteQueue(on_complete)
    if not NetworkMgr:isConnected() then
        if on_complete then on_complete(0, Queue.count()) end
        return
    end
    local notes = Queue.all()
    if #notes == 0 then
        if on_complete then on_complete(0, 0) end
        return
    end
    local sent = {}
    local function step(i)
        if i > #notes then
            Queue.removeIds(sent)
            if on_complete then on_complete(#sent, Queue.count()) end
            return
        end
        Bridge:noteAsync(notes[i], function(_resp)
            sent[#sent + 1] = notes[i].id
            step(i + 1)
        end, function(err)
            logger.warn("piread: note sync stopped at", i, ":", err)
            Queue.removeIds(sent)
            if on_complete then on_complete(#sent, Queue.count()) end
        end)
    end
    step(1)
end

function PiRead:showChatDialog()
    local s = self:loadSettings()
    if not s.enabled then return end

    local props  = self.ui.doc_props or (self.ui.document and self.ui.document:getProps()) or {}
    local book_title  = props.title   or ""
    local book_author = props.authors or ""
    local reading_pct = self:currentReadingPct()

    local dialog
    dialog = InputDialog:new{
        title           = _("Ask Pi"),
        input           = "",
        input_hint      = _("What's happening? Who is this? What did I miss?"),
        input_type      = "text",
        input_multiline = true,
        allow_newline   = false,
        buttons = {{
            {
                text = _("Cancel"),
                id   = "close",
                callback = function() UIManager:close(dialog) end,
            },
            {
                text             = _("Ask"),
                is_enter_default = true,
                callback         = function()
                    local question = dialog:getInputText()
                    if not question or question:match("^%s*$") then return end
                    UIManager:close(dialog)
                    self:chatBridge(question, book_title, book_author, reading_pct)
                end,
            },
        }},
    }
    UIManager:show(dialog)
end

function PiRead:chatBridge(question, book_title, book_author, reading_pct)
    local close_loading = self:showLoadingAnim(_("Asking Pi"))
    local page_text = self:getCurrentPageText(2500)

    Bridge:chatAsync({
        question    = question,
        book_title  = book_title ~= "" and book_title  or nil,
        book_author = book_author ~= "" and book_author or nil,
        reading_pct = reading_pct,
        page_text   = page_text,
        xray        = self._xray,
    }, function(response)
        close_loading()
        UIManager:show(TextViewer:new{
            title  = _("Pi"),
            text   = response,
            width  = math.floor(Screen:getWidth()  * 0.92),
            height = math.floor(Screen:getHeight() * 0.78),
        })
    end, function(err)
        close_loading()
        logger.warn("piread chat:", err)
        UIManager:show(InfoMessage:new{ text = T(_("Pi: %1"), err), timeout = 6 })
    end)
end


-- ── Menu ──────────────────────────────────────────────────────────────────────

function PiRead:addToMainMenu(menu_items)
    menu_items.piread = {
        text         = _("Pi reading assistant"),
        sorting_hint = "tools",
        sub_item_table = self:buildMenu(),
    }
end

function PiRead:buildMenu()
    local items = {}

    -- Now Reading dashboard (top of menu — primary entry point)
    table.insert(items, {
        text     = _("Now Reading"),
        callback = function()
            local s = self:loadSettings()
            if not s.enabled then
                UIManager:show(InfoMessage:new{ text = _("Pi is disabled."), timeout = 3 })
                return
            end
            local pct = s.spoiler_free and self:currentReadingPct() or nil
            XRayUI.setContext(self:_xrayContext())
            Context.show(self.ui, self._xray, Bridge, pct, function() self:showChatDialog() end)
        end,
    })

    -- Ask Pi (freeform chat)
    table.insert(items, {
        text     = _("Ask Pi"),
        callback = function()
            self:showChatDialog()
        end,
    })

    -- X-Ray browser
    table.insert(items, {
        text_func = function()
            if self._xray then
                local n = #(self._xray.characters or {})
                return string.format(_("X-Ray (%d characters)"), n)
            elseif self._xray_job_id then
                return _("X-Ray (building…)")
            else
                return _("X-Ray (not available)")
            end
        end,
        callback = function()
            if not self._xray then
                UIManager:show(InfoMessage:new{
                    text    = self._xray_job_id
                                and _("X-Ray is being generated. Try again in a minute.")
                                or  _("No X-Ray data. Open a book that's in your Calibre library."),
                    timeout = 5,
                })
                return
            end
            local pct = self:loadSettings().spoiler_free and self:currentReadingPct() or nil
            XRayUI.setContext(self:_xrayContext())
            XRayUI.showMenu(self._xray, pct)
        end,
    })

    -- Recap: where I left off (spoiler-bounded)
    table.insert(items, {
        text         = _("Recap: where I left off"),
        enabled_func = function() return self.ui and self.ui.document ~= nil end,
        callback     = function() self:showRecap() end,
    })

    -- Section X-Ray for the current chapter
    table.insert(items, {
        text         = _("Section X-Ray (this chapter)"),
        enabled_func = function() return self.ui and self.ui.document ~= nil end,
        callback     = function() self:showSectionXRay() end,
    })

    -- Toggle enabled
    table.insert(items, {
        text_func = function()
            return T(_("Enabled: %1"), self:loadSettings().enabled and _("yes") or _("no"))
        end,
        checked_func = function() return self:loadSettings().enabled end,
        callback = function()
            local s = self:loadSettings(); s.enabled = not s.enabled; self:saveSettings(s)
        end,
    })

    -- Spoiler-free toggle
    table.insert(items, {
        text_func = function()
            return T(_("Spoiler-free: %1"), self:loadSettings().spoiler_free and _("on") or _("off"))
        end,
        checked_func = function() return self:loadSettings().spoiler_free end,
        callback = function()
            local s = self:loadSettings(); s.spoiler_free = not s.spoiler_free; self:saveSettings(s)
        end,
    })

    -- Auto-capture lookups (highlight + Obsidian note)
    table.insert(items, {
        text_func = function()
            return T(_("Auto-capture lookups: %1"), self:loadSettings().auto_capture and _("on") or _("off"))
        end,
        checked_func = function() return self:loadSettings().auto_capture end,
        callback = function()
            local s = self:loadSettings(); s.auto_capture = not s.auto_capture; self:saveSettings(s)
        end,
        help_text = _("When on, a successful Ask Pi lookup highlights the passage "
            .. "(note = Pi's answer) and saves the passage + answer to the book's Obsidian note."),
    })

    -- Bridge host
    table.insert(items, {
        text_func = function() return T(_("Host: %1"), self:loadSettings().host) end,
        callback  = function() self:editSetting("host", _("Bridge host"), false) end,
    })

    -- Bridge port
    table.insert(items, {
        text_func = function() return T(_("Port: %1"), tostring(self:loadSettings().port)) end,
        callback  = function() self:editSetting("port", _("Bridge port"), true) end,
    })

    -- Rebuild X-Ray
    table.insert(items, {
        text = _("Rebuild X-Ray for this book"),
        enabled_func = function()
            return self.ui and self.ui.document ~= nil
        end,
        callback = function()
            if not (self.ui and self.ui.document) then return end
            local props = self.ui.doc_props or (self.ui.document and self.ui.document:getProps()) or {}
            local title  = props.title   or ""
            local author = props.authors or ""
            if title == "" then return end
            -- Clear local cache
            if self._book_hash then Cache.deleteXray(self._book_hash) end
            self._xray      = nil
            self._book_hash = nil
            if not NetworkMgr:isConnected() then
                UIManager:show(InfoMessage:new{ text = _("Not connected to network."), timeout = 3 })
                return
            end
            self:requestXRay(title, author, self:currentReadingPct())
        end,
    })

    -- Sync offline-saved notes
    table.insert(items, {
        text_func = function()
            local n = Queue.count()
            return n > 0 and T(_("Sync %1 pending note(s)"), n) or _("Notes: all synced")
        end,
        enabled_func = function() return Queue.count() > 0 end,
        callback = function()
            if not NetworkMgr:isConnected() then
                UIManager:show(InfoMessage:new{ text = _("Not connected to network."), timeout = 3 })
                return
            end
            self:flushNoteQueue(function(sent, remaining)
                UIManager:show(InfoMessage:new{
                    text = remaining > 0
                        and T(_("Synced %1 — %2 still pending."), sent, remaining)
                        or  T(_("Synced %1 note(s) to vault."), sent),
                    timeout = 4,
                })
            end)
        end,
    })

    -- Test connection
    table.insert(items, {
        text = _("Test connection"),
        callback = function()
            self:applySettings()
            local loading = InfoMessage:new{ text = _("Pinging bridge…"), timeout = 6 }
            UIManager:show(loading)
            UIManager:scheduleIn(0.1, function()
                UIManager:close(loading)
                if Bridge:ping() then
                    UIManager:show(InfoMessage:new{
                        text    = T(_("✓ Connected to %1:%2"), Bridge.host, tostring(Bridge.port)),
                        timeout = 4,
                    })
                else
                    UIManager:show(InfoMessage:new{
                        text    = T(_("✗ Cannot reach %1:%2"), Bridge.host, tostring(Bridge.port)),
                        timeout = 5,
                    })
                end
            end)
        end,
    })

    return items
end

-- ── Settings editor ───────────────────────────────────────────────────────────

function PiRead:editSetting(key, title, numeric)
    local InputDialog = require("ui/widget/inputdialog")
    local s = self:loadSettings()
    local dialog
    dialog = InputDialog:new{
        title      = title,
        input      = tostring(s[key] or ""),
        input_type = numeric and "number" or "string",
        buttons = {{
            { text = _("Cancel"), id = "close", callback = function() UIManager:close(dialog) end },
            { text = _("Save"), is_enter_default = true, callback = function()
                local val = dialog:getInputText()
                if numeric then
                    val = tonumber(val)
                    if not val then
                        UIManager:show(InfoMessage:new{ text = _("Enter a valid number"), timeout = 3 })
                        return
                    end
                end
                s[key] = val; self:saveSettings(s); self:applySettings()
                UIManager:close(dialog)
            end },
        }},
    }
    UIManager:show(dialog); dialog:onShowKeyboard()
end

return PiRead
