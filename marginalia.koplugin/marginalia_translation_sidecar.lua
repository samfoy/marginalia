--[[--
Local validation and lookup for adjacent offline translation sidecars.

The module is deliberately independent of the bridge. Sidecars are bounded,
validated as a whole, and matched through marginalia_translation_text so a
32-bit hash is never treated as proof of source-text identity.
--]]--

local TranslationText = require("marginalia_translation_text")
local util = require("util")

local TranslationSidecar = {}

local DEFAULT_MAX_BYTES = 5 * 1024 * 1024
local DEFAULT_MAX_ENTRIES = 10000
local MAX_SELECTION_BYTES = 16384
local SIDECAR_SUFFIX = ".marginalia-translations.json"

local function nonEmptyString(value)
    return type(value) == "string" and value ~= ""
end

local function nonNegativeInteger(value)
    return type(value) == "number" and value >= 0 and value == math.floor(value)
end

local function basename(path)
    return path:match("([^/\\]+)$") or path
end

local function validLowerHex(value, length)
    return type(value) == "string"
        and #value == length
        and value:match("^[0-9a-f]+$") ~= nil
end

local function fileIdentity(path, expected_size)
    local file = io.open(path, "rb")
    if not file then return nil, "source EPUB unavailable" end
    local size = file:seek("end")
    file:close()
    if type(size) ~= "number" then return nil, "source EPUB seek failed" end
    if size ~= expected_size then
        return nil, "source EPUB size mismatch"
    end
    local ok, digest = pcall(util.partialMD5, path)
    if not ok or not validLowerHex(digest, 32) then return nil, "source EPUB hash failed" end
    return digest
end

local function sizeMatches(path, expected_size)
    local file = io.open(path, "rb")
    if not file then return false end
    local size = file:seek("end")
    file:close()
    return size == expected_size
end

function TranslationSidecar.sidecarPath(epub_path)
    if not nonEmptyString(epub_path) then return nil end
    local directory, name = epub_path:match("^(.*[/\\])([^/\\]+)$")
    directory = directory or ""
    name = name or epub_path
    local stem = name:match("^(.*)%.[^%.]*$") or name
    if stem == "" then stem = name end
    return directory .. stem .. SIDECAR_SUFFIX
end

local function validateLocation(location)
    if location == nil then return true end
    if type(location) ~= "table" then return false end
    if location.spine_path ~= nil and type(location.spine_path) ~= "string" then
        return false
    end
    if location.spine_index ~= nil and not nonNegativeInteger(location.spine_index) then
        return false
    end
    if location.candidate_index ~= nil and not nonNegativeInteger(location.candidate_index) then
        return false
    end
    return true
end

local function validateInternal(document, epub_path, opts)
    opts = type(opts) == "table" and opts or {}
    if type(document) ~= "table" then return nil, "document is not an object" end
    if not nonEmptyString(epub_path) then return nil, "invalid EPUB path" end
    if document.version ~= 1 then return nil, "unsupported version" end
    if document.target_language ~= "English" then return nil, "unsupported target language" end
    if not nonEmptyString(document.generated_at) then return nil, "missing generated_at" end

    local source = document.source_epub
    if type(source) ~= "table" then return nil, "missing source_epub" end
    if not nonEmptyString(source.filename) then return nil, "missing source filename" end
    if not nonNegativeInteger(source.size_bytes) then
        return nil, "invalid source size"
    end
    if not validLowerHex(source.sha256, 64) then
        return nil, "invalid source sha256"
    end
    if not validLowerHex(source.koreader_partial_md5, 32) then
        return nil, "invalid source partial md5"
    end

    local actual_hash, identity_error
    if validLowerHex(opts.actual_partial_md5, 32) then
        if not sizeMatches(epub_path, source.size_bytes) then
            return nil, "source EPUB size mismatch"
        end
        actual_hash = opts.actual_partial_md5
    else
        actual_hash, identity_error = fileIdentity(epub_path, source.size_bytes)
    end
    if not actual_hash then return nil, identity_error end
    if actual_hash ~= source.koreader_partial_md5 then return nil, "source EPUB hash mismatch" end

    if type(document.translations) ~= "table" then
        return nil, "missing translations"
    end
    local max_entries = opts.max_entries or DEFAULT_MAX_ENTRIES
    if not nonNegativeInteger(max_entries)
        or max_entries < 1
        or max_entries > DEFAULT_MAX_ENTRIES then
        return nil, "invalid entry limit"
    end

    local count = 0
    for key, entry in pairs(document.translations) do
        count = count + 1
        if count > max_entries then return nil, "too many translations" end
        if not validLowerHex(key, 8) then return nil, "invalid translation key" end
        if type(entry) ~= "table" then return nil, "invalid translation entry" end
        if not nonEmptyString(entry.normalized_source)
            or not nonEmptyString(entry.original_source)
            or not nonEmptyString(entry.source_language)
            or not nonEmptyString(entry.translation) then
            return nil, "incomplete translation entry"
        end
        if TranslationText.normalize(entry.original_source) ~= entry.normalized_source then
            return nil, "source normalization mismatch"
        end
        if TranslationText.hashNormalized(entry.normalized_source) ~= key then
            return nil, "translation key mismatch"
        end
        if entry.chapter ~= nil and type(entry.chapter) ~= "string" then
            return nil, "invalid chapter"
        end
        if not validateLocation(entry.location) then return nil, "invalid location" end
    end

    return document
end

function TranslationSidecar.validate(document, epub_path, opts)
    local ok, value, reason = pcall(validateInternal, document, epub_path, opts)
    if not ok then return nil, "validation error" end
    return value, reason
end

local function defaultDecode(raw)
    local rapidjson = require("rapidjson")
    return rapidjson.decode(raw)
end

local function loadInternal(epub_path, opts)
    opts = type(opts) == "table" and opts or {}
    local path = TranslationSidecar.sidecarPath(epub_path)
    if not path then return nil, "invalid EPUB path" end

    local file = io.open(path, "rb")
    if not file then return nil, "sidecar unavailable" end
    local size = file:seek("end")
    local max_bytes = opts.max_bytes or DEFAULT_MAX_BYTES
    if not nonNegativeInteger(max_bytes)
        or max_bytes < 1
        or max_bytes > DEFAULT_MAX_BYTES then
        file:close()
        return nil, "invalid sidecar size limit"
    end
    if type(size) ~= "number" or size < 1 or size > max_bytes then
        file:close()
        return nil, "invalid sidecar size"
    end
    if not file:seek("set", 0) then
        file:close()
        return nil, "sidecar seek failed"
    end
    local raw = file:read(size)
    file:close()
    if type(raw) ~= "string" or #raw ~= size then return nil, "sidecar read failed" end

    local decoder = opts.decode or defaultDecode
    if type(decoder) ~= "function" then return nil, "invalid decoder" end
    local decoded_ok, document = pcall(decoder, raw)
    if not decoded_ok or type(document) ~= "table" then return nil, "sidecar decode failed" end
    return TranslationSidecar.validate(document, epub_path, {
        max_entries = opts.max_entries or DEFAULT_MAX_ENTRIES,
    })
end

function TranslationSidecar.load(epub_path, opts)
    local ok, value, reason = pcall(loadInternal, epub_path, opts)
    if not ok then return nil, "sidecar load error" end
    return value, reason
end

local function phraseContains(longer, shorter)
    if #longer <= #shorter then return false end
    local start = 1
    while true do
        local first, last = longer:find(shorter, start, true)
        if not first then return false end
        local left_ok = first == 1 or longer:sub(first - 1, first - 1) == " "
        local right_ok = last == #longer or longer:sub(last + 1, last + 1) == " "
        if left_ok and right_ok then return true end
        start = first + 1
    end
end

local function lookupDocumentInternal(document, selection)
    if type(document) ~= "table" or type(document.translations) ~= "table" then
        return nil, "invalid document"
    end
    if type(selection) ~= "string" then return nil, "invalid selection" end
    local normalized = TranslationText.normalize(selection)
    if normalized == "" or #normalized > MAX_SELECTION_BYTES then
        return nil, "invalid selection"
    end

    local key = TranslationText.hashNormalized(normalized)
    local exact = document.translations[key]
    if exact ~= nil then
        if type(exact) ~= "table" or exact.normalized_source ~= normalized then
            return nil, "hash collision"
        end
        return exact.translation, exact
    end

    local match
    local matched_source
    local count = 0
    for _, entry in pairs(document.translations) do
        count = count + 1
        if count > DEFAULT_MAX_ENTRIES then return nil, "too many translations" end
        if type(entry) ~= "table" or type(entry.normalized_source) ~= "string" then
            return nil, "invalid document"
        end
        local source = entry.normalized_source
        if source ~= normalized
            and (phraseContains(normalized, source) or phraseContains(source, normalized)) then
            if matched_source ~= nil and matched_source ~= source then
                return nil, "ambiguous containment"
            end
            match = entry
            matched_source = source
        end
    end
    if match then return match.translation, match end
    return nil, "translation not found"
end

function TranslationSidecar.lookupDocument(document, selection)
    local ok, value, entry_or_reason = pcall(lookupDocumentInternal, document, selection)
    if not ok then return nil, "lookup error" end
    return value, entry_or_reason
end

function TranslationSidecar.lookup(epub_path, selection, opts)
    local document, reason = TranslationSidecar.load(epub_path, opts)
    if not document then return nil, reason end
    return TranslationSidecar.lookupDocument(document, selection)
end

return TranslationSidecar
