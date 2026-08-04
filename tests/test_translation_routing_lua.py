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


def document(epub: Path, entries: list[tuple[str, dict]] | None = None) -> dict:
    return {
        "version": 1,
        "source_epub": {
            "filename": epub.name,
            "size_bytes": epub.stat().st_size,
            "sha256": "a" * 64,
        },
        "target_language": "English",
        "generated_at": "2026-08-04T00:00:00Z",
        "translations": dict(entries or [entry("Bonjour le monde", "Hello world")]),
    }


def run_main_lua(body: str, *args: str) -> subprocess.CompletedProcess[str]:
    assert LUA is not None, "host Lua is required"
    bootstrap = f"""
package.path = {lua_literal(str(PLUGIN_DIR))} .. "/?.lua;" .. package.path
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
    )
    if result.returncode:
        pytest.fail(f"Lua failed ({result.returncode}):\n{result.stderr}\n{result.stdout}")
    return result


def write_fixture(epub: Path, raw: str = "fixture-json") -> Path:
    epub.write_bytes(b"real epub bytes")
    adjacent = epub.with_suffix(".marginalia-translations.json")
    adjacent.write_text(raw, encoding="utf-8")
    return adjacent


def test_translate_mode_callback_uses_real_sidecar_and_shows_viewer(tmp_path):
    epub = tmp_path / "Novel.epub"
    write_fixture(epub)
    doc = document(epub, [entry("Bonjour le monde", "Hello world")])
    run_main_lua(
        f"""
package.preload["rapidjson"] = function()
    return {{ decode = function(raw) assert(raw == "fixture-json"); return {lua_literal(doc)} end }}
end
local PiRead = require("main")
local plugin = {{ ui = {{ document = {{ file = arg[1] }} }} }}
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
""",
        str(epub),
    )


@pytest.mark.parametrize(
    ("case", "path_kind", "sidecar_raw", "doc_mutation"),
    [
        ("missing", "epub", None, ""),
        ("malformed", "epub", "bad-json", "decode_error"),
        ("stale", "epub", "fixture-json", "stale"),
        ("ambiguous", "epub", "fixture-json", "ambiguous"),
        ("missing-path", "missing", None, ""),
        ("non-epub", "text", None, ""),
    ],
)
def test_translate_misses_are_identical_and_never_fall_through(
    tmp_path, case, path_kind, sidecar_raw, doc_mutation
):
    epub = tmp_path / "Novel.epub"
    epub.write_bytes(b"real epub bytes")
    if sidecar_raw is not None:
        epub.with_suffix(".marginalia-translations.json").write_text(sidecar_raw, encoding="utf-8")
    doc = document(epub)
    if doc_mutation == "stale":
        doc["source_epub"]["size_bytes"] += 1
    elif doc_mutation == "ambiguous":
        doc["translations"] = dict(
            [entry("selected words"), entry("more selected text")]
        )
    if path_kind == "missing":
        lua_path = "nil"
    elif path_kind == "text":
        lua_path = lua_literal(str(tmp_path / "Novel.txt"))
    else:
        lua_path = "arg[1]"
    decoder = (
        'error("decoder failed")'
        if doc_mutation == "decode_error"
        else f"return {lua_literal(doc)}"
    )
    run_main_lua(
        f"""
package.preload["rapidjson"] = function()
    return {{ decode = function(_) {decoder} end }}
end
local PiRead = require("main")
local plugin = {{ ui = {{ document = {{ file = {lua_path} }} }} }}
setmetatable(plugin, {{ __index = PiRead }})
plugin.askBridge = function() error("askBridge forbidden for Translate") end
plugin.showLoadingAnim = function() error("loading forbidden for Translate") end
plugin.captureLookup = function() error("capture forbidden for Translate") end
plugin:handleModeSelection("selected", "before", "after", "Novel", "Author", "translate", "Translate to English", {{}})
assert(#shown == 1)
assert(shown[1].kind == "InfoMessage")
assert(shown[1].text == {lua_literal(MISS_TEXT)})
""",
        str(epub),
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
    assert "TranslationSidecar.lookup" in translate_part
    assert "self:askBridge" in bridge_part
    ask_start = source.index("function PiRead:askBridge")
    ask_end = source.index("\nend", ask_start)
    assert "Bridge:askAsync" in source[ask_start:ask_end]
