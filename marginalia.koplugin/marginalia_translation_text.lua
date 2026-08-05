--[[--
Portable normalization and djb2-32 hashing for translation sidecar lookups.

This mirrors bridge/translation_text.py without native Unicode/hash modules.
It maps selected UTF-8 punctuation, collapses ASCII whitespace, and lowercases
ASCII A-Z only. Non-ASCII bytes are preserved; NFC/NFD normalization is not
attempted. A lookup key is only an index: callers must compare the normalized
source stored in the sidecar to reject 32-bit hash collisions.
--]]--

local TranslationText = {}

local replacements = {
    ["\194\160"] = " ", -- U+00A0 NO-BREAK SPACE
    ["\226\128\175"] = " ", -- U+202F NARROW NO-BREAK SPACE
    ["\226\128\152"] = "'", -- U+2018 LEFT SINGLE QUOTATION MARK
    ["\226\128\153"] = "'", -- U+2019 RIGHT SINGLE QUOTATION MARK
    ["\202\188"] = "'", -- U+02BC MODIFIER LETTER APOSTROPHE
    ["\226\128\156"] = '"', -- U+201C LEFT DOUBLE QUOTATION MARK
    ["\226\128\157"] = '"', -- U+201D RIGHT DOUBLE QUOTATION MARK
    ["\194\171"] = '"', -- U+00AB LEFT-POINTING DOUBLE ANGLE QUOTATION MARK
    ["\194\187"] = '"', -- U+00BB RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK
    ["\226\128\144"] = "-", -- U+2010 HYPHEN
    ["\226\128\145"] = "-", -- U+2011 NON-BREAKING HYPHEN
    ["\226\128\146"] = "-", -- U+2012 FIGURE DASH
    ["\226\128\147"] = "-", -- U+2013 EN DASH
    ["\226\128\148"] = "-", -- U+2014 EM DASH
    ["\226\136\146"] = "-", -- U+2212 MINUS SIGN
}

local surrounding_noise = {}
for i = 1, #"'\".,!?;:-()[]{}" do
    surrounding_noise[("'\".,!?;:-()[]{}"):sub(i, i)] = true
end

local ascii_whitespace = "[ \t\r\n\f" .. string.char(11) .. "]+"

local function collapseWhitespace(text)
    return (text:gsub(ascii_whitespace, " "):gsub("^ +", ""):gsub(" +$", ""))
end

function TranslationText.normalize(source)
    local text = source
    for original, replacement in pairs(replacements) do
        text = text:gsub(original, replacement)
    end
    text = collapseWhitespace(text)
    text = text:gsub("[A-Z]", function(char)
        return string.char(string.byte(char) + 32)
    end)

    while #text > 0 do
        local changed = false
        if surrounding_noise[text:sub(1, 1)] then
            text = text:sub(2)
            changed = true
        end
        if #text > 0 and surrounding_noise[text:sub(-1)] then
            text = text:sub(1, -2)
            changed = true
        end
        if not changed then break end
        text = collapseWhitespace(text)
    end
    return text
end

function TranslationText.hashNormalized(normalized_source)
    local value = 5381
    for i = 1, #normalized_source do
        value = (value * 33 + normalized_source:byte(i)) % 4294967296
    end
    return string.format("%08x", value)
end

function TranslationText.lookupKey(source)
    return TranslationText.hashNormalized(TranslationText.normalize(source))
end

return TranslationText
