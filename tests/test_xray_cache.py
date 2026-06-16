"""test_xray_cache.py — unit tests for xray_cache module."""

import json

import pytest

import xray_cache


# ── Sample record helpers ─────────────────────────────────────────────────────

def _make_record(title="Test Book", author="Test Author",
                 series=None, series_index=None, book_hash="abc123"):
    return {
        "version": 1,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "strategy": "single_shot",
        "last_reading_pct": 0,
        "book": {
            "title": title,
            "author": author,
            "series": series,
            "series_index": series_index,
            "calibre_id": None,
            "epub_path": None,
            "epub_hash": book_hash,
            "total_chars": 100,
            "chapter_count": 1,
        },
        "xray": {
            "characters": [],
            "locations": [],
            "terms": [],
            "references": [],
            "timeline": [],
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# save / load roundtrip
# ═══════════════════════════════════════════════════════════════════════════════

class TestSaveLoad:

    def test_roundtrip(self, tmp_cache):
        rec = _make_record()
        xray_cache.save("abc123", rec)
        loaded = xray_cache.load("abc123")
        assert loaded is not None
        assert loaded["book"]["title"] == "Test Book"

    def test_load_miss_returns_none(self, tmp_cache):
        result = xray_cache.load("nonexistent_hash_xyz")
        assert result is None

    def test_save_updates_index(self, tmp_cache):
        rec = _make_record(title="Index Book", book_hash="idx001")
        xray_cache.save("idx001", rec)
        index = xray_cache.load_index()
        assert "idx001" in index["books"]
        assert index["books"]["idx001"]["title"] == "Index Book"


# ═══════════════════════════════════════════════════════════════════════════════
# find_by_title_author
# ═══════════════════════════════════════════════════════════════════════════════

class TestFindByTitleAuthor:

    def test_exact_match(self, tmp_cache):
        rec = _make_record(title="Dune", author="Frank Herbert", book_hash="h1")
        xray_cache.save("h1", rec)
        result = xray_cache.find_by_title_author("Dune", "Frank Herbert")
        assert result is not None
        assert result["book"]["title"] == "Dune"

    def test_title_only_match(self, tmp_cache):
        rec = _make_record(title="Dune", author="Frank Herbert", book_hash="h2")
        xray_cache.save("h2", rec)
        result = xray_cache.find_by_title_author("Dune", "")
        assert result is not None

    def test_case_insensitive_title(self, tmp_cache):
        rec = _make_record(title="Dune", author="Frank Herbert", book_hash="h3")
        xray_cache.save("h3", rec)
        result = xray_cache.find_by_title_author("dune", "frank herbert")
        assert result is not None

    def test_miss_returns_none(self, tmp_cache):
        result = xray_cache.find_by_title_author("Nonexistent Title")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# update_reading_pct
# ═══════════════════════════════════════════════════════════════════════════════

class TestUpdateReadingPct:

    def test_updates_index_and_full_record(self, tmp_cache):
        rec = _make_record(book_hash="upd01")
        xray_cache.save("upd01", rec)
        xray_cache.update_reading_pct("upd01", 67.5)

        # Index entry updated
        index = xray_cache.load_index()
        assert index["books"]["upd01"]["last_reading_pct"] == 67.5

        # Full record updated
        full = xray_cache.load("upd01")
        assert full["last_reading_pct"] == 67.5

    def test_unknown_hash_does_not_raise(self, tmp_cache):
        # Should silently do nothing
        xray_cache.update_reading_pct("no_such_hash", 50.0)


# ═══════════════════════════════════════════════════════════════════════════════
# load_index edge cases
# ═══════════════════════════════════════════════════════════════════════════════

class TestLoadIndex:

    def test_empty_when_file_missing(self, tmp_cache):
        index = xray_cache.load_index()
        assert "books" in index
        assert isinstance(index["books"], dict)

    def test_empty_when_file_corrupt(self, tmp_cache):
        xray_cache.INDEX_FILE.write_text("NOT VALID JSON }{", encoding="utf-8")
        index = xray_cache.load_index()
        assert "books" in index


# ═══════════════════════════════════════════════════════════════════════════════
# list_cached / get_series
# ═══════════════════════════════════════════════════════════════════════════════

class TestListAndSeries:

    def test_list_cached_returns_entries(self, tmp_cache):
        xray_cache.save("lc1", _make_record(title="A", book_hash="lc1"))
        xray_cache.save("lc2", _make_record(title="B", book_hash="lc2"))
        entries = xray_cache.list_cached()
        titles = [e["title"] for e in entries]
        assert "A" in titles
        assert "B" in titles

    def test_get_series_returns_sorted_records(self, tmp_cache):
        rec1 = _make_record(title="Series #1", series="MySeries",
                            series_index=1, book_hash="s1")
        rec2 = _make_record(title="Series #2", series="MySeries",
                            series_index=2, book_hash="s2")
        xray_cache.save("s1", rec1)
        xray_cache.save("s2", rec2)
        results = xray_cache.get_series("MySeries")
        assert len(results) == 2
        # Sorted by series_index
        assert results[0]["book"]["series_index"] == 1
        assert results[1]["book"]["series_index"] == 2

    def test_get_series_miss_returns_empty(self, tmp_cache):
        assert xray_cache.get_series("NoSuchSeries") == []

    def test_get_series_case_insensitive(self, tmp_cache):
        rec = _make_record(title="T", series="MyCase", series_index=1, book_hash="cs1")
        xray_cache.save("cs1", rec)
        results = xray_cache.get_series("mycase")
        assert len(results) == 1
