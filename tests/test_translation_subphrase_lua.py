"""Sub-phrase selection contract for device translation lookup.

A reader rarely selects exactly the span the bridge indexed. They highlight a
few words inside a longer italicised passage, or drag past it. Both directions
must resolve deterministically instead of reporting no translation.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from translation_sidecar import _koreader_partial_md5
from translation_text import lookup_key, normalize_source


ROOT = Path(__file__).parent.parent
PLUGIN_DIR = ROOT / "marginalia.koplugin"
LUA = shutil.which("lua")

pytestmark = pytest.mark.skipif(LUA is None, reason="host Lua is required")


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
        return '"' + "".join(f"\\{byte:03d}" for byte in value.encode("utf-8")) + '"'
    if isinstance(value, list):
        return "{" + ",".join(lua_literal(item) for item in value) + "}"
    if isinstance(value, dict):
        return "{" + ",".join(f"[{lua_literal(k)}]={lua_literal(v)}"
                              for k, v in value.items()) + "}"
    raise TypeError(type(value))


def entry(source: str, translation: str, language: str = "French"):
    return lookup_key(source), {
        "normalized_source": normalize_source(source),
        "original_source": source,
        "source_language": language,
        "translation": translation,
    }


def document(epub: Path, entries):
    return {
        "version": 1,
        "source_epub": {
            "filename": epub.name,
            "size_bytes": epub.stat().st_size,
            "sha256": hashlib.sha256(epub.read_bytes()).hexdigest(),
            "koreader_partial_md5": _koreader_partial_md5(epub),
        },
        "target_language": "English",
        "generated_at": "2026-08-05T00:00:00Z",
        "translations": dict(entries),
    }


def lookup(epub: Path, entries, selection: str) -> tuple[str | None, str | None]:
    """Run the real device module and return (translation, reason)."""
    doc = document(epub, entries)
    script = f"""
package.path = {lua_literal(str(PLUGIN_DIR))} .. "/?.lua;" .. package.path
local __attrs = {lua_literal({str(epub): {
        "dev": 1, "ino": 2, "size": epub.stat().st_size,
        "modification": 3, "change": 4}})}
local __partial = {lua_literal({str(epub): _koreader_partial_md5(epub)})}
package.preload['libs/libkoreader-lfs'] = function()
  return {{ attributes = function(p) return __attrs[p] end }} end
package.preload['ffi/sha2'] = function()
  return {{ sha256 = function()
    local function partial(c) if c ~= nil then return partial end
      return {lua_literal(hashlib.sha256(epub.read_bytes()).hexdigest())} end
    return partial end }} end
package.preload['util'] = function()
  return {{ partialMD5 = function(p) return __partial[p] end }} end

local sidecar = require("marginalia_translation_sidecar")
local doc, err = sidecar.validate({lua_literal(doc)}, {lua_literal(str(epub))})
if not doc then print("VALIDATE_FAIL\\t" .. tostring(err)) os.exit(0) end
local translation, info = sidecar.lookupDocument(doc, {lua_literal(selection)})
if translation then
  print("HIT\\t" .. translation)
else
  print("MISS\\t" .. tostring(info))
end
"""
    result = subprocess.run([LUA, "-"], input=script, text=True,
                            capture_output=True, timeout=30)
    if result.returncode:
        pytest.fail(f"Lua failed: {result.stderr}\n{result.stdout}")
    kind, _, detail = result.stdout.strip().partition("\t")
    assert kind != "VALIDATE_FAIL", detail
    return (detail, None) if kind == "HIT" else (None, detail)


@pytest.fixture()
def epub(tmp_path):
    path = tmp_path / "Novel.epub"
    path.write_bytes(b"epub-bytes-for-sub-phrase-tests")
    return path


PHRASE = "mon cher petit papa"
PAPA = [entry(PHRASE, "my dear little papa")]


# ── punctuation must act as a word boundary ──────────────────────────────────
# normalize_source keeps interior punctuation, so a comma directly after the
# selected span used to fail the space-only boundary check.

COMMA = [entry("bonjour le monde, mon ami", "hello world, my friend")]


@pytest.mark.parametrize("selection", [
    "bonjour le monde",   # span ends right before the comma
    "monde",              # single word ending at the comma
    "mon ami",            # span after the comma
    "le monde, mon",      # span crossing the comma
])
def test_punctuation_counts_as_a_word_boundary(epub, selection):
    translation, reason = lookup(epub, COMMA, selection)
    assert translation == "hello world, my friend", reason


def test_apostrophe_is_a_boundary(epub):
    entries = [entry("la vita e breve, l'arte e lunga", "life is short, art is long")]
    translation, reason = lookup(epub, entries, "arte")
    assert translation == "life is short, art is long", reason


# ── sub-phrase selection inside one indexed passage ──────────────────────────

@pytest.mark.parametrize("selection", [
    "cher petit papa",
    "mon cher petit",
    "cher petit",
    "papa",
    "mon",
])
def test_sub_phrase_of_single_entry_resolves(epub, selection):
    translation, reason = lookup(epub, PAPA, selection)
    assert translation == "my dear little papa", reason


def test_partial_word_does_not_match(epub):
    """'jour' inside 'bonjour' is not a word — it must not resolve."""
    entries = [entry("bonjour le monde", "hello world")]
    translation, _ = lookup(epub, entries, "jour le monde")
    assert translation is None


# ── overlapping entries must disambiguate, not give up ──────────────────────

OVERLAP = [
    entry("bonjour le monde", "hello world"),
    entry("le monde", "the world"),
]


def test_selection_inside_two_entries_prefers_the_most_specific(epub):
    """'le monde' is itself indexed: the exact entry must win."""
    translation, reason = lookup(epub, OVERLAP, "le monde")
    assert translation == "the world", reason


def test_sub_phrase_inside_two_entries_prefers_the_tightest_containing_entry(epub):
    """'monde' sits in both; the shorter passage is the more precise answer."""
    translation, reason = lookup(epub, OVERLAP, "monde")
    assert translation == "the world", reason


def test_wide_selection_containing_two_entries_prefers_the_longest_covered(epub):
    """Dragging past both entries should return the fuller passage."""
    translation, reason = lookup(epub, OVERLAP, "elle dit bonjour le monde doucement")
    assert translation == "hello world", reason


def test_genuinely_ambiguous_equal_length_alternatives_still_report_ambiguity(epub):
    """Two distinct same-length candidates cannot be ranked; stay honest."""
    entries = [
        entry("bonjour madame", "good day madam"),
        entry("bonsoir madame", "good evening madam"),
    ]
    translation, reason = lookup(
        epub, entries, "elle dit bonjour madame puis bonsoir madame")
    assert translation is None
    assert reason == "ambiguous containment"


def test_covering_passage_beats_an_incidental_short_word_inside_the_selection(epub):
    """Regression: real Lolita ranking bug.

    Selecting most of a long passage also contains short indexed words such as
    "nothing" or "should". Ranking those above the passage that covers the whole
    selection returned a useless one-word gloss. The covering passage must win.
    """
    passage = "for the benefit of old-fashioned readers who wish to follow"
    entries = [
        entry(passage, "the real passage translation"),
        entry("should", "ought to"),
        entry("nothing", "not a thing"),
        entry("who wish", "the ones wanting"),
    ]
    selection = "the benefit of old-fashioned readers who wish to follow"
    translation, reason = lookup(epub, entries, selection)
    assert translation == "the real passage translation", reason


def test_unrelated_selection_still_misses(epub):
    translation, reason = lookup(epub, PAPA, "the quick brown fox")
    assert translation is None
    assert reason == "translation not found"


# ── real-world Lolita shape: short words inside many passages ───────────────

def test_common_short_word_inside_many_entries_resolves_to_tightest(epub):
    """Lolita indexes 'ça' inside several longer passages."""
    entries = [
        entry("ça", "that"),
        entry("ça alors", "well then"),
        entry("mais ça ne fait rien", "but it does not matter"),
    ]
    translation, reason = lookup(epub, entries, "ça")
    assert translation == "that", reason
