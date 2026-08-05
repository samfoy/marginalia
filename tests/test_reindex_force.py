"""Reindex must actually rebuild, not re-serve the same cached record.

Sam tapped Reindex on Lolita and kept getting a record with no translations.
The plugin sends force=true and clears its own device cache, but the bridge
never read the flag, so it answered from the same cached record every time --
there was no way to recover a book whose translation index was missing, empty,
or partial.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import server as srv


@pytest.fixture(autouse=True)
def clean_jobs():
    with srv._jobs_lock:
        srv._xray_jobs.clear()
    yield
    with srv._jobs_lock:
        srv._xray_jobs.clear()


PARTIAL = "a" * 32


def index(translations: dict | None = None, partial: str = PARTIAL) -> dict:
    return {
        "version": 1,
        "target_language": "English",
        "generated_at": "2026-08-05T00:00:00Z",
        "source_epub": {
            "filename": "Lolita.epub",
            "size_bytes": 1234,
            "sha256": "b" * 64,
            "koreader_partial_md5": partial,
        },
        "translations": translations if translations is not None else {
            "abcd1234": {
                "normalized_source": "mon cher petit papa",
                "original_source": "mon cher petit papa",
                "source_language": "French",
                "translation": "my dear little papa",
            }
        },
    }


# ── an empty index must not count as a complete one ─────────────────────────

def test_empty_translation_index_is_not_treated_as_valid():
    """A book with zero translations is indistinguishable from a failed build.

    Treating it as valid made the state permanent: the bridge served it from
    cache forever and never attempted a rebuild.
    """
    assert srv._translation_index_valid(index(translations={}), PARTIAL) is False


def test_non_empty_translation_index_is_valid():
    assert srv._translation_index_valid(index(), PARTIAL) is True


def test_missing_translation_index_is_not_valid():
    assert srv._translation_index_valid(None, PARTIAL) is False


# ── force must bypass the cache ─────────────────────────────────────────────

def test_force_request_is_parsed_as_a_rebuild_request():
    """The plugin's Reindex sends force=true; the bridge must honour it."""
    assert srv._wants_rebuild({"force": True}) is True
    assert srv._wants_rebuild({"force": "true"}) is True
    assert srv._wants_rebuild({"force": 1}) is True


def test_absent_or_false_force_keeps_using_the_cache():
    assert srv._wants_rebuild({}) is False
    assert srv._wants_rebuild({"force": False}) is False
    assert srv._wants_rebuild({"force": "false"}) is False
    assert srv._wants_rebuild({"force": 0}) is False
    assert srv._wants_rebuild({"force": None}) is False
