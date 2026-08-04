"""Cross-language contract tests for offline translation lookup keys."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from translation_text import lookup_key, normalize_source


VECTORS = [
    ("  Hello\tWORLD \r\n", "hello world", "3551c8c1"),
    ("\u00a0Hello\u202fworld\u00a0", "hello world", "3551c8c1"),
    (
        "“Mais qui est cette petite fille?”",
        "mais qui est cette petite fille",
        "e4477ef6",
    ),
    ("—Mon cher, l’étoile—", "mon cher, l'étoile", "750f8059"),
    ("«ÉCOLE Über»", "École Über", "75efc34c"),
    ("...  L’amour-propre?! ", "l'amour-propre", "3ff9d121"),
    (" \t—“?!”—\n", "", "00001505"),
]


@pytest.mark.parametrize(("source", "normalized", "key"), VECTORS)
def test_normalization_and_hash_known_vectors(source, normalized, key):
    assert normalize_source(source) == normalized
    assert lookup_key(source) == key


def test_maps_all_supported_quote_and_dash_variants():
    assert normalize_source("‘a’ ʼb “c” «d»") == "a' 'b \"c\" \"d"
    assert normalize_source("‐a‑b‒c–d—e−f") == "a-b-c-d-e-f"


def test_preserves_internal_punctuation_and_accented_bytes():
    assert normalize_source("('L’ÉTÉ—bleu',)") == "l'ÉtÉ-bleu"


def test_lookup_key_is_exact_lowercase_hex():
    key = lookup_key("Hello world")
    assert len(key) == 8
    assert set(key) <= set("0123456789abcdef")


def test_python_and_lua_known_vectors_match(tmp_path):
    lua = shutil.which("lua")
    assert lua is not None, "host Lua is required for the cross-language contract test"

    plugin_dir = Path(__file__).parent.parent / "marginalia.koplugin"
    lua_script = r'''
package.path = arg[1] .. "/?.lua;" .. package.path
local text = require("marginalia_translation_text")
local function fromhex(hex)
    return (hex:gsub("..", function(pair)
        return string.char(tonumber(pair, 16))
    end))
end
for line in io.lines() do
    local source = fromhex(line)
    io.write(text.normalize(source), "\t", text.lookupKey(source), "\n")
end
'''
    encoded = "\n".join(source.encode("utf-8").hex() for source, _, _ in VECTORS) + "\n"
    runner = tmp_path / "translation_vectors.lua"
    runner.write_text(lua_script, encoding="utf-8")
    result = subprocess.run(
        [lua, str(runner), str(plugin_dir)],
        input=encoded,
        text=True,
        capture_output=True,
        check=True,
    )
    actual = [tuple(line.split("\t", 1)) for line in result.stdout.splitlines()]
    expected = [(normalized, key) for _, normalized, key in VECTORS]
    assert actual == expected
