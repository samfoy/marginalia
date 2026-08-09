"""Device-side contract for a book with nothing to translate.

The bridge fix (build_attempted on a complete, zero-entry build) is only useful
if the DEVICE also accepts that document. If the plugin's validator rejected it,
the record would be dropped on device and Blood Meridian would keep asking the
bridge to rebuild — the same loop, one layer down.

Reuses the Lua harness from test_translation_sidecar_lua.py so this runs against
the real plugin code, not a reimplementation.
"""

from __future__ import annotations

import pytest

from .test_translation_sidecar_lua import (
    LUA,
    document,
    entry,
    lua_literal,
    run_lua,
)

pytestmark = pytest.mark.skipif(
    LUA is None, reason="host Lua interpreter is required for device contract tests"
)


def test_empty_but_attempted_index_is_accepted_on_device(tmp_path):
    """Zero translations must validate — nothing to translate is a real outcome."""
    epub = tmp_path / "Blood Meridian.epub"
    epub.write_bytes(b"PK\x03\x04 blood meridian payload")

    doc = document(epub, entries=[])
    doc["skipped_candidates"] = 0
    doc["build_attempted"] = True

    result = run_lua(
        f"""
local sidecar = require("marginalia_translation_sidecar")
local doc = {lua_literal(doc)}
local valid, err = sidecar.validate(doc, {lua_literal(str(epub))})
assert(valid, "empty-but-attempted index was rejected: " .. tostring(err))
-- A lookup simply misses; it must not error.
local translation, reason = sidecar.lookupDocument(valid, "cualquier cosa")
assert(translation == nil, "unexpected hit in an empty index")
print("OK")
""",
        str(epub),
    )
    assert "OK" in result.stdout


def test_unknown_bridge_fields_do_not_break_validation(tmp_path):
    """Forward compatibility: new bridge fields must not fail the device.

    build_attempted is added by a newer bridge than some installed plugins.
    An older/stricter validator that rejected unknown keys would break the
    whole Book Index, so pin the tolerant behaviour with a test.
    """
    epub = tmp_path / "Lolita.epub"
    epub.write_bytes(b"PK\x03\x04 lolita payload")

    doc = document(epub, entries=[entry("Bonjour le monde", "Hello world")])
    doc["build_attempted"] = True
    doc["some_future_field"] = {"nested": [1, 2, 3]}

    result = run_lua(
        f"""
local sidecar = require("marginalia_translation_sidecar")
local doc = {lua_literal(doc)}
local valid, err = sidecar.validate(doc, {lua_literal(str(epub))})
assert(valid, "unknown fields broke validation: " .. tostring(err))
local translation = sidecar.lookupDocument(valid, "Bonjour le monde")
assert(translation == "Hello world", "wrong translation: " .. tostring(translation))
print("OK")
""",
        str(epub),
    )
    assert "OK" in result.stdout
