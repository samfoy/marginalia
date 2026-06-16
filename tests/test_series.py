"""test_series.py — unit tests for series module."""

import pytest

import xray_cache
import series
from series import build_scope, inject_series_context, _merge_from_prior


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_record(title, author="Author", series_name=None,
                 series_index=None, book_hash=None):
    book_hash = book_hash or title.lower().replace(" ", "_")
    return {
        "version": 1,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "strategy": "single_shot",
        "book": {
            "title": title,
            "author": author,
            "series": series_name,
            "series_index": series_index,
            "calibre_id": None,
            "epub_path": None,
            "epub_hash": book_hash,
            "total_chars": 500,
            "chapter_count": 2,
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
# build_scope
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildScope:

    def test_no_series_returns_current_only(self, tmp_cache):
        rec = _make_record("Standalone", series_name=None, series_index=None,
                           book_hash="standalone01")
        scope = build_scope(rec, reading_pct=60.0)
        assert len(scope) == 1
        assert scope[0]["current"] is True

    def test_current_entry_has_correct_max_pct(self, tmp_cache):
        rec = _make_record("Book Two", series_name="MySeries", series_index=2,
                           book_hash="b2")
        scope = build_scope(rec, reading_pct=42.0)
        current = next(s for s in scope if s.get("current"))
        assert current["max_pct"] == 42.0

    def test_includes_prior_book_from_cache(self, tmp_cache):
        rec1 = _make_record("Book One", series_name="MySeries", series_index=1,
                             book_hash="b1")
        rec2 = _make_record("Book Two", series_name="MySeries", series_index=2,
                             book_hash="b2")
        xray_cache.save("b1", rec1)
        xray_cache.save("b2", rec2)

        scope = build_scope(rec2, reading_pct=50.0)
        hashes = {s["hash"] for s in scope}
        assert "b1" in hashes   # prior book included
        assert "b2" in hashes   # current book included

    def test_does_not_include_future_books(self, tmp_cache):
        rec1 = _make_record("Book One", series_name="MySeries", series_index=1,
                             book_hash="f1")
        rec2 = _make_record("Book Two", series_name="MySeries", series_index=2,
                             book_hash="f2")
        rec3 = _make_record("Book Three", series_name="MySeries", series_index=3,
                             book_hash="f3")
        xray_cache.save("f1", rec1)
        xray_cache.save("f2", rec2)
        xray_cache.save("f3", rec3)

        # Reading book 2 — book 3 should be excluded
        scope = build_scope(rec2, reading_pct=50.0)
        hashes = {s["hash"] for s in scope}
        assert "f3" not in hashes

    def test_current_entry_has_current_flag(self, tmp_cache):
        rec = _make_record("B", series_name="S", series_index=1, book_hash="bb")
        scope = build_scope(rec, reading_pct=10.0)
        current_entries = [s for s in scope if s.get("current")]
        assert len(current_entries) == 1

    def test_prior_entries_have_max_pct_100(self, tmp_cache):
        rec1 = _make_record("Book One", series_name="S", series_index=1,
                             book_hash="p1")
        rec2 = _make_record("Book Two", series_name="S", series_index=2,
                             book_hash="p2")
        xray_cache.save("p1", rec1)
        xray_cache.save("p2", rec2)

        scope = build_scope(rec2, reading_pct=50.0)
        prior = [s for s in scope if not s.get("current")]
        for entry in prior:
            assert entry["max_pct"] == 100.0


# ═══════════════════════════════════════════════════════════════════════════════
# inject_series_context
# ═══════════════════════════════════════════════════════════════════════════════

class TestInjectSeriesContext:

    def test_copies_characters_from_prior_book(self, tmp_cache):
        prior_rec = _make_record("Book One", series_name="S", series_index=1,
                                  book_hash="ic1")
        prior_rec["xray"]["characters"] = [
            {"name": "Leia", "description": "A rebel princess", "aliases": []}
        ]
        xray_cache.save("ic1", prior_rec)

        current_xray = {
            "characters": [],
            "locations": [],
            "terms": [],
            "references": [],
        }
        inject_series_context(current_xray, "S", series_index=2)
        names = [c["name"] for c in current_xray["characters"]]
        assert "Leia" in names

    def test_injected_entities_tagged_source_book(self, tmp_cache):
        prior_rec = _make_record("Book One", series_name="T", series_index=1,
                                  book_hash="it1")
        prior_rec["xray"]["characters"] = [
            {"name": "Han", "description": "A smuggler", "aliases": []}
        ]
        xray_cache.save("it1", prior_rec)

        current_xray = {"characters": [], "locations": [], "terms": [], "references": []}
        inject_series_context(current_xray, "T", series_index=2)

        injected = next(c for c in current_xray["characters"] if c["name"] == "Han")
        assert "source_book" in injected

    def test_does_not_overwrite_existing_entity(self, tmp_cache):
        prior_rec = _make_record("Book One", series_name="U", series_index=1,
                                  book_hash="iu1")
        prior_rec["xray"]["characters"] = [
            {"name": "Luke", "description": "From prior", "aliases": []}
        ]
        xray_cache.save("iu1", prior_rec)

        current_xray = {
            "characters": [{"name": "Luke", "description": "Current desc", "aliases": []}],
            "locations": [], "terms": [], "references": [],
        }
        inject_series_context(current_xray, "U", series_index=2)

        lukes = [c for c in current_xray["characters"] if c["name"] == "Luke"]
        assert len(lukes) == 1
        assert lukes[0]["description"] == "Current desc"

    def test_no_op_for_series_index_one(self, tmp_cache):
        prior_rec = _make_record("Book Zero", series_name="V", series_index=0,
                                  book_hash="iv0")
        xray_cache.save("iv0", prior_rec)
        xray = {"characters": [{"name": "Existing"}], "locations": [], "terms": [], "references": []}
        original_count = len(xray["characters"])
        inject_series_context(xray, "V", series_index=1)
        assert len(xray["characters"]) == original_count

    def test_no_op_for_series_index_zero(self, tmp_cache):
        xray = {"characters": [], "locations": [], "terms": [], "references": []}
        result = inject_series_context(xray, "V", series_index=0)
        assert result is xray

    def test_returns_modified_xray(self, tmp_cache):
        xray = {"characters": [], "locations": [], "terms": [], "references": []}
        result = inject_series_context(xray, "NoSeries", series_index=1)
        assert result is xray


# ═══════════════════════════════════════════════════════════════════════════════
# _merge_from_prior (internal helper)
# ═══════════════════════════════════════════════════════════════════════════════

class TestMergeFromPrior:

    def test_adds_new_entity(self):
        current = []
        prior = [{"name": "NewChar", "description": "A new one"}]
        result = _merge_from_prior(current, prior, "description", "Book #1")
        assert any(e["name"] == "NewChar" for e in result)

    def test_does_not_duplicate_existing(self):
        current = [{"name": "OldChar", "description": "existing"}]
        prior = [{"name": "OldChar", "description": "from prior"}]
        result = _merge_from_prior(current, prior, "description", "Book #1")
        matches = [e for e in result if e["name"] == "OldChar"]
        assert len(matches) == 1

    def test_injected_entity_has_zero_first_appearance(self):
        current = []
        prior = [{"name": "Ghost", "description": "desc", "first_appearance_pct": 80}]
        result = _merge_from_prior(current, prior, "description", "Book #1")
        ghost = next(e for e in result if e["name"] == "Ghost")
        assert ghost["first_appearance_pct"] == 0

    def test_enriches_existing_entity_without_description(self):
        current = [{"name": "Sparse", "description": ""}]
        prior = [{"name": "Sparse", "description": "enriched from prior"}]
        result = _merge_from_prior(current, prior, "description", "Book #1")
        sparse = next(e for e in result if e["name"] == "Sparse")
        assert "enriched from prior" in sparse["description"]
