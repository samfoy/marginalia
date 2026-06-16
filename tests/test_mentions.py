"""test_mentions.py — unit tests for mentions module."""

import pytest

import mentions
from mentions import _search_names, build_mentions, add_mention_counts, MIN_NAME_LEN
from epub_extract import Chapter, EpubContent


# ── helpers ───────────────────────────────────────────────────────────────────

def _chapter(text: str, title: str = "Chapter 1", pct: float = 0.0) -> Chapter:
    return Chapter(title=title, text=text, position_pct=pct)


def _content_with_chapters(chapters: list[Chapter]) -> EpubContent:
    full_text = "\n\n".join(ch.text for ch in chapters)
    return EpubContent(
        full_text=full_text,
        chapters=chapters,
        title="Test Book",
        author="Test Author",
        series=None,
        series_index=None,
        file_hash="abc123",
        total_chars=len(full_text),
        epub_path="/tmp/test.epub",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# _search_names
# ═══════════════════════════════════════════════════════════════════════════════

class TestSearchNames:

    def test_returns_primary_name(self):
        entity = {"name": "Aragorn", "aliases": []}
        result = _search_names(entity)
        assert "Aragorn" in result

    def test_excludes_name_shorter_than_min_len(self):
        # MIN_NAME_LEN is 3; "Li" (2 chars) should be excluded
        short = "Li"
        assert len(short) < MIN_NAME_LEN
        entity = {"name": short, "aliases": []}
        result = _search_names(entity)
        assert short not in result

    def test_includes_aliases(self):
        entity = {"name": "Aragorn", "aliases": ["Strider", "Elessar"]}
        result = _search_names(entity)
        assert "Strider" in result
        assert "Elessar" in result

    def test_excludes_short_aliases(self):
        entity = {"name": "Aragorn", "aliases": ["Ar"]}  # too short
        result = _search_names(entity)
        assert "Ar" not in result

    def test_multi_word_name_adds_first_and_last(self):
        # "Sevro au Fitchner" → also "Sevro" and "Fitchner"
        entity = {"name": "Sevro au Fitchner", "aliases": []}
        result = _search_names(entity)
        assert "Sevro au Fitchner" in result
        assert "Sevro" in result
        assert "Fitchner" in result

    def test_no_duplicates_in_result(self):
        entity = {"name": "Aragorn", "aliases": ["Aragorn"]}
        result = _search_names(entity)
        assert result.count("Aragorn") == 1

    def test_empty_entity_returns_empty(self):
        entity = {"name": "", "aliases": []}
        result = _search_names(entity)
        assert result == []

    def test_none_aliases_handled(self):
        entity = {"name": "Gandalf"}
        # No 'aliases' key at all
        result = _search_names(entity)
        assert "Gandalf" in result


# ═══════════════════════════════════════════════════════════════════════════════
# build_mentions
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildMentions:

    def test_finds_character_in_chapter(self):
        chapter = _chapter("Aragorn strode into the hall with purpose.")
        content = _content_with_chapters([chapter])
        xray = {"characters": [{"name": "Aragorn", "aliases": []}]}
        result = build_mentions(content, xray)
        assert "aragorn" in result
        assert len(result["aragorn"]) > 0

    def test_mention_has_snippet(self):
        chapter = _chapter("Gandalf arrived at the gates of Minas Tirith.")
        content = _content_with_chapters([chapter])
        xray = {"characters": [{"name": "Gandalf", "aliases": []}]}
        result = build_mentions(content, xray)
        mention = result["gandalf"][0]
        assert "snippet" in mention
        assert "Gandalf" in mention["snippet"]

    def test_mention_has_chapter_and_position(self):
        chapter = _chapter("Frodo carried the Ring.", title="The Journey", pct=25.0)
        content = _content_with_chapters([chapter])
        xray = {"characters": [{"name": "Frodo", "aliases": []}]}
        result = build_mentions(content, xray)
        mention = result["frodo"][0]
        assert "chapter" in mention
        assert "position_pct" in mention
        assert mention["chapter"] == "The Journey"

    def test_no_mention_when_name_absent(self):
        chapter = _chapter("Sauron was defeated long ago.")
        content = _content_with_chapters([chapter])
        xray = {"characters": [{"name": "Frodo", "aliases": []}]}
        result = build_mentions(content, xray)
        # Frodo not in text, should not appear
        assert "frodo" not in result

    def test_empty_xray_returns_empty(self):
        chapter = _chapter("Some text with nothing special.")
        content = _content_with_chapters([chapter])
        result = build_mentions(content, {})
        assert result == {}

    def test_multiple_chapters_multiple_mentions(self):
        ch1 = _chapter("Legolas fired an arrow.", title="Ch1", pct=0.0)
        ch2 = _chapter("Legolas leaped over the wall.", title="Ch2", pct=50.0)
        content = _content_with_chapters([ch1, ch2])
        xray = {"characters": [{"name": "Legolas", "aliases": []}]}
        result = build_mentions(content, xray)
        assert "legolas" in result
        assert len(result["legolas"]) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# add_mention_counts
# ═══════════════════════════════════════════════════════════════════════════════

class TestAddMentionCounts:

    def test_adds_chapter_count_to_entity(self):
        xray = {
            "characters": [{"name": "Frodo", "aliases": []}],
        }
        mention_index = {
            "frodo": [{"chapter": "Ch1", "position_pct": 0, "snippet": "Frodo..."}],
        }
        add_mention_counts(xray, mention_index)
        assert xray["characters"][0].get("chapter_count") == 1

    def test_no_count_added_for_unmentioned_entity(self):
        xray = {
            "characters": [{"name": "Bilbo", "aliases": []}],
        }
        mention_index = {}  # no mentions
        add_mention_counts(xray, mention_index)
        assert "chapter_count" not in xray["characters"][0]

    def test_returns_modified_xray(self):
        xray = {"characters": [{"name": "Sam", "aliases": []}]}
        mentions_data = {"sam": [{"chapter": "C", "position_pct": 0, "snippet": "Sam..."}]}
        result = add_mention_counts(xray, mentions_data)
        assert result is xray
