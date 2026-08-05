"""Host-Lua routing tests for strictly offline Translate mode."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from translation_text import lookup_key, normalize_source


ROOT = Path(__file__).parent.parent
PLUGIN_DIR = ROOT / "marginalia.koplugin"
LUA = shutil.which("lua")
MISS_TEXT = "No precomputed translation found for this selection."


def lua_literal(value):
    if value is None:
        return "nil"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(value, list):
        return "{" + ",".join(lua_literal(item) for item in value) + "}"
    if isinstance(value, dict):
        return "{" + ",".join(f"[{lua_literal(key)}]={lua_literal(item)}" for key, item in value.items()) + "}"
    raise TypeError(type(value))


def entry(source: str, translation: str = "English") -> tuple[str, dict]:
    return lookup_key(source), {
        "normalized_source": normalize_source(source),
        "original_source": source,
        "source_language": "French",
        "translation": translation,
    }


def run_main_lua(body: str, *args: str) -> subprocess.CompletedProcess[str]:
    assert LUA is not None, "host Lua is required"
    bootstrap = f"""
package.path = {lua_literal(str(PLUGIN_DIR))} .. "/?.lua;" .. package.path
package.preload["libs/libkoreader-lfs"] = function() return {{ attributes = function() return nil end }} end
package.preload["ffi/sha2"] = function() return {{ sha256 = function() return function() return string.rep("0", 64) end end }} end
local shown = {{}}
local function widget(kind)
    return {{ new = function(_, props) props.kind = kind; return props end }}
end
package.preload["ui/widget/buttondialog"] = function() return widget("ButtonDialog") end
package.preload["ui/widget/inputdialog"] = function() return widget("InputDialog") end
package.preload["ui/widget/infomessage"] = function() return widget("InfoMessage") end
package.preload["ui/widget/textviewer"] = function() return widget("TextViewer") end
package.preload["device"] = function()
    return {{ screen = {{ getWidth = function() return 1000 end, getHeight = function() return 800 end }} }}
end
package.preload["dispatcher"] = function() return {{ registerAction = function() end }} end
package.preload["ui/network/manager"] = function()
    return {{ isConnected = function() error("network access forbidden") end }}
end
package.preload["ui/uimanager"] = function()
    return {{
        show = function(_, value) table.insert(shown, value) end,
        close = function() end,
        scheduleIn = function() error("network scheduling forbidden") end,
        unschedule = function() end,
    }}
end
package.preload["ui/event"] = function() return {{ new = function(_, ...) return {{...}} end }} end
package.preload["ui/widget/container/widgetcontainer"] = function()
    return {{ extend = function(_, value) return value end }}
end
package.preload["logger"] = function() return {{ info = function() end, warn = function() end }} end
package.preload["util"] = function() return {{ cleanupSelectedText = function(text) return text end }} end
package.preload["ffi/util"] = function()
    return {{ template = function(text, value) return text:gsub("%%1", tostring(value)) end }}
end
package.preload["gettext"] = function() return function(text) return text end end
package.preload["bridge"] = function()
    return setmetatable({{}}, {{ __index = function(_, name)
        if name == "ask" or name == "askAsync" then
            return function() error("bridge ask forbidden") end
        end
        return function() end
    end }})
end
package.preload["marginalia_cache"] = function() return {{}} end
package.preload["marginalia_context"] = function() return {{}} end
package.preload["marginalia_xray"] = function() return {{}} end
package.preload["marginalia_queue"] = function()
    return setmetatable({{}}, {{ __index = function() return function() error("queue access forbidden") end end }})
end
"""
    result = subprocess.run(
        [LUA, "-", *map(str, args)],
        input=bootstrap + "\n" + body,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode:
        pytest.fail(f"Lua failed ({result.returncode}):\n{result.stderr}\n{result.stdout}")
    return result


def test_translate_mode_uses_in_memory_book_index_without_epub_sidecar():
    index = {
        "version": 1,
        "target_language": "English",
        "translations": dict([entry("Bonjour le monde", "Hello world")]),
    }
    run_main_lua(
        f"""
local PiRead = require("main")
local plugin = {{ _translation_index = {lua_literal(index)}, ui = {{ document = {{ file = "/books/Novel.epub" }} }} }}
setmetatable(plugin, {{ __index = PiRead }})
plugin.askBridge = function() error("askBridge forbidden for Translate") end
plugin:handleModeSelection("Bonjour le monde", "", "", "Novel", "Author", "translate", "Translate to English", nil)
assert(#shown == 1 and shown[1].kind == "TextViewer")
assert(shown[1].text == "Hello world")
"""
    )


def test_translate_mode_callback_uses_cached_index_and_shows_viewer():
    index = {
        "version": 1,
        "target_language": "English",
        "translations": dict([entry("Bonjour le monde", "Hello world")]),
    }
    run_main_lua(
        f"""
local PiRead = require("main")
local plugin = {{ _translation_index = {lua_literal(index)}, ui = {{ document = {{ file = "/books/Novel.epub" }} }} }}
setmetatable(plugin, {{ __index = PiRead }})
plugin.askBridge = function() error("askBridge forbidden for Translate") end
plugin.showLoadingAnim = function() error("loading forbidden for Translate") end
plugin.captureLookup = function() error("capture forbidden for Translate") end
plugin:showModeDialog("Bonjour le monde", "before", "after", "Novel", "Author", {{ text = "captured" }})
assert(shown[#shown].kind == "ButtonDialog")
local dialog = shown[#shown]
dialog.buttons[4][1].callback()
assert(#shown == 2)
assert(shown[2].kind == "TextViewer")
assert(shown[2].title == "Translate to English")
assert(shown[2].text == "Hello world")
assert(shown[2].width == 920 and shown[2].height == 624)
"""
    )


@pytest.mark.parametrize(
    "index",
    [
        None,
        {"version": 1, "target_language": "English", "translations": {}},
        {"version": 1, "target_language": "English", "translations": "invalid"},
        {
            "version": 1,
            "target_language": "English",
            # Deliberately unrelated to the "selected" selection below. A
            # sub-phrase of an indexed passage now resolves by design, so a
            # miss case must share no words with any entry.
            "translations": dict([entry("bonjour le monde"), entry("chocolat glace")]),
        },
    ],
)
def test_translate_cache_misses_are_identical_and_never_fall_through(index):
    run_main_lua(
        f"""
local PiRead = require("main")
local plugin = {{ _translation_index = {lua_literal(index)}, ui = {{ document = {{ file = "/books/Novel.epub" }} }} }}
setmetatable(plugin, {{ __index = PiRead }})
plugin.askBridge = function() error("askBridge forbidden for Translate") end
plugin.showLoadingAnim = function() error("loading forbidden for Translate") end
plugin.captureLookup = function() error("capture forbidden for Translate") end
plugin:handleModeSelection("selected", "before", "after", "Novel", "Author", "translate", "Translate to English", {{}})
assert(#shown == 1)
assert(shown[1].kind == "InfoMessage")
assert(shown[1].text == {lua_literal(MISS_TEXT)})
"""
    )


@pytest.mark.parametrize("mode_id", ["whois", "explain", "summarize"])
def test_non_translate_modes_preserve_bridge_arguments(mode_id):
    run_main_lua(
        f"""
package.preload["rapidjson"] = function() return {{ decode = function() error("local lookup forbidden") end }} end
local PiRead = require("main")
local plugin = {{ ui = {{ document = {{ file = "/books/Novel.epub" }} }} }}
setmetatable(plugin, {{ __index = PiRead }})
local captured = {{ marker = true }}
local calls = 0
plugin.askBridge = function(_, text, prev, next_, title, author, mode, label, selected)
    calls = calls + 1
    assert(text == "text" and prev == "prev" and next_ == "next")
    assert(title == "title" and author == "author")
    assert(mode == {lua_literal(mode_id)} and label == "label")
    assert(selected == captured)
end
plugin:handleModeSelection("text", "prev", "next", "title", "author", {lua_literal(mode_id)}, "label", captured)
assert(calls == 1)
assert(#shown == 0)
"""
    )


def test_mode_callback_delegates_once_through_routing_seam():
    run_main_lua(
        """
package.preload["rapidjson"] = function() return { decode = function() return {} end } end
local PiRead = require("main")
local plugin = { ui = { document = { file = "/books/Novel.epub" } } }
setmetatable(plugin, { __index = PiRead })
local routed = {}
plugin.handleModeSelection = function(_, ...) table.insert(routed, {...}) end
plugin:showModeDialog("text", "prev", "next", "title", "author", { marker = true })
local dialog = shown[#shown]
dialog.buttons[4][1].callback()
assert(#routed == 1 and routed[1][6] == "translate")
plugin:showModeDialog("text", "prev", "next", "title", "author", { marker = true })
dialog = shown[#shown]
dialog.buttons[2][1].callback()
assert(#routed == 2 and routed[2][6] == "explain")
"""
    )


def test_translate_branch_is_local_and_other_modes_keep_async_bridge():
    source = (PLUGIN_DIR / "main.lua").read_text(encoding="utf-8")
    start = source.index("function PiRead:handleModeSelection")
    end = source.index("\nend", start)
    branch = source[start:end]
    translate_part, bridge_part = branch.split("if mode_id == \"translate\"", 1)[1].split("return", 1)
    forbidden = ["askBridge", "Bridge:", "askAsync", "showLoadingAnim", "captureLookup", "Queue", "NetworkMgr"]
    assert all(token not in translate_part for token in forbidden)
    assert "TranslationSidecar.lookupDocument" in translate_part
    assert "TranslationSidecar.lookup," not in translate_part
    assert "self._translation_index" in translate_part
    assert "self:askBridge" in bridge_part
    ask_start = source.index("function PiRead:askBridge")
    ask_end = source.index("\nend", ask_start)
    assert "Bridge:askAsync" in source[ask_start:ask_end]


def test_translation_index_is_loaded_and_persisted_with_book_cache():
    source = (PLUGIN_DIR / "main.lua").read_text(encoding="utf-8")
    doc_load = source[source.index("function PiRead:onDocLoad"):source.index("function PiRead:checkXRayFreshness")]
    store = source[source.index("function PiRead:_storeXRay"):source.index("function PiRead:_xrayContext")]
    freshness = source[source.index("function PiRead:checkXRayFreshness"):source.index("function PiRead:currentReadingPct")]

    assert "self:_validatedTranslationIndex(record.translation_index)" in doc_load
    assert "self:_validatedTranslationIndex(resp.translation_index)" in store
    assert "translation_index = resp.translation_index" in store
    assert "self:_validatedTranslationIndex(resp.translation_index)" in freshness
    assert "translation_index = resp.translation_index" in freshness
    validator = source[source.index("function PiRead:_validatedTranslationIndex"):source.index("function PiRead:onDocLoad")]
    assert "TranslationSidecar.validate" in validator
    assert 'match("%.epub$")' in validator
    assert "self.ui.document.file" in validator
    assert "actual_partial_md5 = self._device_partial_md5" in validator
    assert 'resp.status == "needs_epub"' in freshness
    assert "self:requestXRay(title, author, self:currentReadingPct())" in freshness
    assert "device_partial_md5  = self._device_partial_md5" in freshness
    request = source[source.index("function PiRead:requestXRay"):source.index("function PiRead:schedulePoll")]
    assert "device_partial_md5 = self._device_partial_md5" in request


def test_document_load_clears_previous_translation_state():
    source = (PLUGIN_DIR / "main.lua").read_text(encoding="utf-8")
    doc_load = source[source.index("function PiRead:onDocLoad"):source.index("function PiRead:checkXRayFreshness")]
    reset_index = doc_load.index("self._translation_index = nil")
    reset_hash = doc_load.index("self._device_partial_md5 = nil")
    cache_lookup = doc_load.index("Cache.findByTitle")
    assert reset_index < cache_lookup
    assert reset_hash < cache_lookup


def test_poll_rejects_job_started_for_previous_document():
    source = (PLUGIN_DIR / "main.lua").read_text(encoding="utf-8")
    poll = source[source.index("function PiRead:pollXRayStatus"):source.index("function PiRead:_storeXRay")]
    store = source[source.index("function PiRead:_storeXRay"):source.index("function PiRead:_xrayContext")]
    doc_load = source[source.index("function PiRead:onDocLoad"):source.index("function PiRead:checkXRayFreshness")]

    assert "self._xray_job_generation ~= self._document_generation" in poll
    assert "self:_storeXRay(resp, generation)" in poll
    assert "generation ~= self._document_generation" in store
    assert "self._xray_job_id = nil" in doc_load
    assert "UIManager:unschedule(self._poll_handle)" in doc_load
    assert "if generation ~= self._document_generation then return end" in doc_load
