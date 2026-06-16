--[[--
piread_queue.lua — Durable offline queue for "Save Note" highlights.

Notes are written to a local queue file first, then flushed to the Obsidian
vault (via the bridge /note endpoint) whenever the bridge is reachable. This
guarantees a highlight note is never lost to a flaky/absent connection.

Queue file: <settings>/piread/note_queue.json
  { "notes": [ {id, highlight, context, book_title, book_author, reading_pct, ts} ] }
--]]--

local DataStorage = require("datastorage")
local rapidjson   = require("rapidjson")
local logger      = require("logger")

local ok_lfs, lfs = pcall(require, "libs/libkoreader-lfs")
if not ok_lfs or type(lfs) ~= "table" then
    ok_lfs, lfs = pcall(require, "lfs")
end

local Queue = {}

local function dir()  return DataStorage:getSettingsDir() .. "/piread" end
local function path() return dir() .. "/note_queue.json" end

local function ensureDir()
    if not (ok_lfs and lfs) then return end
    if lfs.attributes(dir(), "mode") ~= "directory" then lfs.mkdir(dir()) end
end

--- Return the list of queued notes (oldest first). Always a table.
function Queue.all()
    local f = io.open(path(), "r")
    if not f then return {} end
    local raw = f:read("*a"); f:close()
    local ok, d = pcall(rapidjson.decode, raw)
    if ok and type(d) == "table" and type(d.notes) == "table" then
        return d.notes
    end
    return {}
end

local function write(notes)
    ensureDir()
    -- Rebuild as a fresh integer-keyed array so rapidjson always encodes as []
    -- not {}. A table decoded from JSON {} carries an object flag; adding integer
    -- keys doesn't clear it, so re-encoding silently drops the elements back to {}.
    local arr = {}
    for i = 1, #notes do arr[i] = notes[i] end
    local ok, enc = pcall(rapidjson.encode, { notes = arr })
    if not ok then
        logger.warn("piread_queue: encode error:", enc)
        return false
    end
    local f = io.open(path(), "w")
    if not f then
        logger.warn("piread_queue: cannot write", path())
        return false
    end
    f:write(enc); f:close()
    return true
end

--- Append a note. Assigns an id if absent. Returns the id.
function Queue.enqueue(note)
    local notes = Queue.all()
    note.id = note.id or string.format("%d-%04d", os.time(), math.random(0, 9999))
    note.ts = note.ts or os.time()
    notes[#notes + 1] = note
    local okw = write(notes)
    logger.info("piread_queue: enqueued note", note.id, "queue size", #notes, "written", tostring(okw))
    return note.id
end

--- Number of pending notes.
function Queue.count()
    return #Queue.all()
end

--- Remove notes by id (after a successful sync).
function Queue.removeIds(ids)
    if not ids or #ids == 0 then return end
    local rm = {}
    for _, id in ipairs(ids) do rm[id] = true end
    local kept = {}
    for _, n in ipairs(Queue.all()) do
        if not rm[n.id] then kept[#kept + 1] = n end
    end
    write(kept)
end

return Queue
