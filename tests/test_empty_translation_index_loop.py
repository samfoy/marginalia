"""A book with no translatable passages must not loop the device forever.

Second half of the Blood Meridian failure. Once a record was cached with
``translation_index: null``, ``/book-index/init`` kept answering ``needs_epub``:

  device uploads -> build fails/finds nothing -> null index cached
  -> init says needs_epub -> device uploads again -> ...

The plugin caps this at MAX_EMPTY_RETRIES and then shows
"Book Index upload failed", which is what Sam saw. Blood Meridian is an
English novel with a handful of Spanish phrases; a genuinely empty or
near-empty result is a NORMAL outcome and must be recorded as an attempted
build, not retried indefinitely.

Distinction that matters:
  - "we have not tried yet"            -> build (or ask for the EPUB)
  - "we tried and found nothing"       -> serve the record, stop asking
"""

from __future__ import annotations

import pytest

import server as srv


PARTIAL = "a" * 32


def attempted_empty_index(partial: str = PARTIAL) -> dict:
    """What the bridge should record after a build that yielded no entries."""
    return {
        "version": 1,
        "target_language": "English",
        "generated_at": "2026-08-09T02:29:00Z",
        "source_epub": {
            "filename": "Blood Meridian.epub",
            "size_bytes": 391059,
            "sha256": "b" * 64,
            "koreader_partial_md5": partial,
        },
        "translations": {},
        "skipped_candidates": 0,
        "build_attempted": True,
    }


def test_attempted_empty_index_counts_as_complete():
    """A completed build that found nothing must satisfy validation.

    Otherwise the device is told needs_epub forever for any book without
    foreign-language passages.
    """
    assert srv._translation_index_valid(attempted_empty_index(), PARTIAL) is True


def test_partial_build_is_not_treated_as_finished():
    """A build that dropped candidates is genuinely incomplete — retry is right."""
    index = attempted_empty_index()
    index["skipped_candidates"] = 12
    assert srv._translation_index_valid(index, PARTIAL) is False


def test_empty_index_without_attempt_marker_is_still_invalid():
    """Legacy/garbage empty index (no proof a build ran) must still rebuild.

    This preserves the v0.10.3 fix: an empty index that is indistinguishable
    from a failed build must not be accepted.
    """
    index = attempted_empty_index()
    del index["build_attempted"]
    assert srv._translation_index_valid(index, PARTIAL) is False


def test_attempted_empty_index_still_bound_to_the_right_edition():
    """Edition binding is not weakened by the empty-but-attempted case."""
    assert srv._translation_index_valid(attempted_empty_index(), "c" * 32) is False


def test_safe_translation_index_marks_attempted_builds(monkeypatch):
    """_safe_translation_index must stamp a successful build as attempted."""
    monkeypatch.setattr(srv, "build_translation_index", lambda _p: {
        "version": 1,
        "translations": {},
        "skipped_candidates": 0,
    })
    index = srv._safe_translation_index("/books/Blood Meridian.epub")
    assert index is not None
    assert index["build_attempted"] is True


def test_safe_translation_index_does_not_mark_partial_builds(monkeypatch):
    """A partial build must not claim it finished."""
    monkeypatch.setattr(srv, "build_translation_index", lambda _p: {
        "version": 1,
        "translations": {"abcd1234": {
            "normalized_source": "x", "original_source": "x",
            "source_language": "Spanish", "translation": "y",
        }},
        "skipped_candidates": 3,
    })
    index = srv._safe_translation_index("/books/Blood Meridian.epub")
    assert index is not None
    assert index.get("build_attempted") is not True


def test_hard_failure_still_returns_none(monkeypatch):
    """A crash must stay non-fatal and must NOT be stamped as attempted."""
    def boom(_p):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(srv, "build_translation_index", boom)
    assert srv._safe_translation_index("/books/Blood Meridian.epub") is None
