"""Host-driven contract tests for the device translation sidecar lookup."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from translation_text import lookup_key, normalize_source


ROOT = Path(__file__).parent.parent
PLUGIN_DIR = ROOT / "marginalia.koplugin"
LUA = shutil.which("lua")


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
    normalized = normalize_source(source)
    return lookup_key(source), {
        "normalized_source": normalized,
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


def run_lua(body: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    assert LUA is not None, "host Lua is required"
    script = (
        f'package.path = {lua_literal(str(PLUGIN_DIR))} .. "/?.lua;" .. package.path\n'
        + body
    )
    result = subprocess.run(
        [LUA, "-", *map(str, args)],
        input=script,
        text=True,
        capture_output=True,
    )
    if check and result.returncode:
        pytest.fail(f"Lua failed ({result.returncode}):\n{result.stderr}\n{result.stdout}")
    return result


def test_adjacent_path_replaces_only_final_extension():
    run_lua(
        """
local sidecar = require("marginalia_translation_sidecar")
assert(sidecar.sidecarPath("/books/My.Novel.epub") == "/books/My.Novel.marginalia-translations.json")
assert(sidecar.sidecarPath("/books/Novel") == "/books/Novel.marginalia-translations.json")
assert(sidecar.sidecarPath("") == nil)
assert(sidecar.sidecarPath(nil) == nil)
"""
    )


def test_validate_and_exact_lookup_normalize_selection(tmp_path):
    epub = tmp_path / "Novel.epub"
    epub.write_bytes(b"epub")
    doc = document(epub, [entry("“Bonjour   le monde!”", "Hello world")])
    run_lua(
        f"""
local sidecar = require("marginalia_translation_sidecar")
local doc = {lua_literal(doc)}
local valid, reason = sidecar.validate(doc, arg[1])
assert(valid, reason)
local translation, found = sidecar.lookupDocument(valid, "  bonjour le monde  ")
assert(translation == "Hello world")
assert(found.original_source == "“Bonjour   le monde!”")
""",
        str(epub),
    )


def test_exact_hash_collision_is_rejected_without_fallback(tmp_path):
    epub = tmp_path / "Novel.epub"
    epub.write_bytes(b"epub")
    collision_entry = entry("alpha beta", "Alpha")[1]
    doc = document(epub, [("deadbeef", collision_entry)])
    run_lua(
        f"""
local text = require("marginalia_translation_text")
text.hashNormalized = function(_) return "deadbeef" end
local sidecar = require("marginalia_translation_sidecar")
local valid, reason = sidecar.validate({lua_literal(doc)}, arg[1])
assert(valid, reason)
local translation, miss = sidecar.lookupDocument(valid, "beta")
assert(translation == nil)
assert(miss == "hash collision")
""",
        str(epub),
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "doc.version = 2",
        'doc.target_language = "Spanish"',
        "doc.generated_at = ''",
        "doc.source_epub.sha256 = 'ABC'",
        "doc.source_epub.size_bytes = -1",
        "doc.translations[next(doc.translations)].translation = ''",
        "doc.translations[next(doc.translations)].original_source = 'different'",
        "doc.translations[next(doc.translations)].location = { spine_index = -1 }",
    ],
)
def test_malformed_documents_fail_closed(tmp_path, mutation):
    epub = tmp_path / "Novel.epub"
    epub.write_bytes(b"epub")
    run_lua(
        f"""
local sidecar = require("marginalia_translation_sidecar")
local doc = {lua_literal(document(epub))}
{mutation}
local ok, valid, reason = pcall(sidecar.validate, doc, arg[1])
assert(ok)
assert(valid == nil)
assert(type(reason) == "string")
""",
        str(epub),
    )


def test_mismatched_translation_key_fails_closed(tmp_path):
    epub = tmp_path / "Novel.epub"
    epub.write_bytes(b"epub")
    run_lua(
        f"""
local sidecar = require("marginalia_translation_sidecar")
local doc = {lua_literal(document(epub))}
local old_key, value = next(doc.translations)
doc.translations[old_key] = nil
doc.translations["00000000"] = value
local valid, reason = sidecar.validate(doc, arg[1])
assert(valid == nil and reason == "translation key mismatch")
""",
        str(epub),
    )


def test_load_real_files_with_injected_decoder_and_stale_checks(tmp_path):
    epub = tmp_path / "My.Novel.epub"
    epub.write_bytes(b"real epub bytes")
    sidecar_path = tmp_path / "My.Novel.marginalia-translations.json"
    sidecar_path.write_text("fixture-json", encoding="utf-8")
    doc = document(epub)
    run_lua(
        f"""
local sidecar = require("marginalia_translation_sidecar")
local fixture = {lua_literal(doc)}
local loaded, reason = sidecar.load(arg[1], {{ decode = function(raw)
    assert(raw == "fixture-json")
    return fixture
end }})
assert(loaded, reason)
assert(sidecar.lookupDocument(loaded, "Bonjour le monde") == "Hello world")
fixture.source_epub.size_bytes = fixture.source_epub.size_bytes + 1
local stale, stale_reason = sidecar.load(arg[1], {{ decode = function(_) return fixture end }})
assert(stale == nil and type(stale_reason) == "string")
fixture.source_epub.size_bytes = fixture.source_epub.size_bytes - 1
fixture.source_epub.filename = "Other.epub"
local wrong_name = sidecar.load(arg[1], {{ decode = function(_) return fixture end }})
assert(wrong_name == nil)
""",
        str(epub),
    )


def test_missing_empty_decoder_error_and_oversize_fail_closed(tmp_path):
    missing = tmp_path / "Missing.epub"
    epub = tmp_path / "Book.epub"
    epub.write_bytes(b"x")
    sidecar_path = tmp_path / "Book.marginalia-translations.json"
    run_lua(
        """
local sidecar = require("marginalia_translation_sidecar")
local function assertMiss(path, opts)
    local ok, value, reason = pcall(sidecar.load, path, opts)
    assert(ok and value == nil and type(reason) == "string")
end
assertMiss(arg[1], { decode = function(_) return {} end })
assertMiss(arg[2], { decode = function(_) error("bad json") end })
assertMiss(arg[2], { decode = function(_) return nil end })
assertMiss(arg[2], { decode = function(_) return {} end, max_bytes = 3 })
""",
        str(missing),
        str(epub),
    )
    sidecar_path.write_text("", encoding="utf-8")
    run_lua(
        """
local sidecar = require("marginalia_translation_sidecar")
local loaded, reason = sidecar.load(arg[1], { decode = function(_) return {} end })
assert(loaded == nil and type(reason) == "string")
""",
        str(epub),
    )
    sidecar_path.write_text("four", encoding="utf-8")
    run_lua(
        """
local sidecar = require("marginalia_translation_sidecar")
local loaded, reason = sidecar.load(arg[1], { decode = function(_) return {} end, max_bytes = 3 })
assert(loaded == nil and type(reason) == "string")
""",
        str(epub),
    )


def test_unique_containment_word_boundaries_and_ambiguity(tmp_path):
    epub = tmp_path / "Novel.epub"
    epub.write_bytes(b"x")
    entries = [
        entry("bonjour le monde", "Hello world"),
        entry("une autre phrase", "Another phrase"),
    ]
    doc = document(epub, entries)
    run_lua(
        f"""
local sidecar = require("marginalia_translation_sidecar")
local valid, reason = sidecar.validate({lua_literal(doc)}, arg[1])
assert(valid, reason)
assert(sidecar.lookupDocument(valid, "elle dit bonjour le monde doucement") == "Hello world")
assert(sidecar.lookupDocument(valid, "bonjour le") == "Hello world")
assert(sidecar.lookupDocument(valid, "jour le monde") == nil)
assert(sidecar.lookupDocument(valid, "bonjour le monde") == "Hello world")

local ambiguous = {lua_literal(document(epub, [entry("bonjour le monde", "One"), entry("le monde", "Two")]))}
local valid2, reason2 = sidecar.validate(ambiguous, arg[1])
assert(valid2, reason2)
local hit, miss = sidecar.lookupDocument(valid2, "elle dit bonjour le monde doucement")
assert(hit == nil and miss == "ambiguous containment")
""",
        str(epub),
    )


def test_entry_limit_and_selection_limit_fail_closed(tmp_path):
    epub = tmp_path / "Novel.epub"
    epub.write_bytes(b"x")
    doc = document(epub, [entry("one"), entry("two")])
    run_lua(
        f"""
local sidecar = require("marginalia_translation_sidecar")
local valid = sidecar.validate({lua_literal(doc)}, arg[1], {{ max_entries = 1 }})
assert(valid == nil)
local unsafe_limit = sidecar.validate({lua_literal(doc)}, arg[1], {{ max_entries = 10001 }})
assert(unsafe_limit == nil)
local valid2, reason2 = sidecar.validate({lua_literal(doc)}, arg[1])
assert(valid2, reason2)
local hit, miss = sidecar.lookupDocument(valid2, string.rep("x", 20000))
assert(hit == nil and type(miss) == "string")
""",
        str(epub),
    )


def test_lookup_wraps_load_and_never_throws(tmp_path):
    epub = tmp_path / "Novel.epub"
    epub.write_bytes(b"epub")
    adjacent = tmp_path / "Novel.marginalia-translations.json"
    adjacent.write_text("fixture", encoding="utf-8")
    doc = document(epub)
    run_lua(
        f"""
local sidecar = require("marginalia_translation_sidecar")
local translation, found = sidecar.lookup(arg[1], "Bonjour le monde", {{
    decode = function(_) return {lua_literal(doc)} end,
}})
assert(translation == "Hello world" and found.translation == "Hello world")
local ok, miss, reason = pcall(sidecar.lookup, arg[1], nil, {{
    decode = function(_) return {lua_literal(doc)} end,
}})
assert(ok and miss == nil and type(reason) == "string")
""",
        str(epub),
    )


def test_module_is_local_only_and_main_is_untouched_by_slice():
    module_path = PLUGIN_DIR / "marginalia_translation_sidecar.lua"
    source = module_path.read_text(encoding="utf-8")
    forbidden = ["require(\"bridge\")", "askasync", "http", "socket", "os.execute", "io.popen", "python"]
    assert all(token not in source.lower() for token in forbidden)
    main = (PLUGIN_DIR / "main.lua").read_text(encoding="utf-8")
    assert "marginalia_translation_sidecar" not in main
