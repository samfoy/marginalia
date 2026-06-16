"""test_epub_extract.py — unit tests for epub_extract module."""

import hashlib
import os

import pytest

import epub_extract
from epub_extract import _TextExtractor, EpubContent, Chapter, extract_epub


# ═══════════════════════════════════════════════════════════════════════════════
# _TextExtractor
# ═══════════════════════════════════════════════════════════════════════════════

class TestTextExtractor:

    def _extract(self, html: str) -> str:
        p = _TextExtractor()
        p.feed(html)
        return p.result()

    def test_strips_html_tags(self):
        result = self._extract("<b>hello</b> <em>world</em>")
        assert result == "hello world"

    def test_skips_script_content(self):
        result = self._extract("<p>before</p><script>alert('x')</script><p>after</p>")
        assert "alert" not in result
        assert "before" in result
        assert "after" in result

    def test_skips_style_content(self):
        result = self._extract("<p>text</p><style>.foo{color:red}</style><p>more</p>")
        assert "color" not in result
        assert "text" in result
        assert "more" in result

    def test_block_elements_emit_newlines(self):
        result = self._extract("<p>first</p><p>second</p>")
        assert "first" in result
        assert "second" in result
        assert "\n" in result

    def test_h1_emits_newline(self):
        result = self._extract("<h1>Title</h1><p>Body</p>")
        assert "Title" in result
        assert "Body" in result
        assert "\n" in result

    def test_div_emits_newline(self):
        result = self._extract("<div>A</div><div>B</div>")
        assert "\n" in result

    def test_collapses_multiple_spaces(self):
        result = self._extract("<p>hello   world</p>")
        assert "hello world" in result
        assert "   " not in result

    def test_empty_input(self):
        result = self._extract("")
        assert result == ""


# ═══════════════════════════════════════════════════════════════════════════════
# extract_epub
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtractEpub:

    def test_returns_epub_content(self, tmp_path, minimal_epub_bytes):
        epub_path = tmp_path / "test.epub"
        epub_path.write_bytes(minimal_epub_bytes)
        result = extract_epub(str(epub_path))
        assert isinstance(result, EpubContent)

    def test_title_and_author_parsed(self, tmp_path, minimal_epub_bytes):
        epub_path = tmp_path / "test.epub"
        epub_path.write_bytes(minimal_epub_bytes)
        result = extract_epub(str(epub_path))
        assert result.title == "Test Book"
        assert result.author == "Test Author"

    def test_full_text_contains_chapter_text(self, tmp_path, minimal_epub_bytes):
        epub_path = tmp_path / "test.epub"
        epub_path.write_bytes(minimal_epub_bytes)
        result = extract_epub(str(epub_path))
        assert "Hello from chapter one" in result.full_text

    def test_has_at_least_one_chapter(self, tmp_path, minimal_epub_bytes):
        epub_path = tmp_path / "test.epub"
        epub_path.write_bytes(minimal_epub_bytes)
        result = extract_epub(str(epub_path))
        assert len(result.chapters) >= 1

    def test_chapters_have_text(self, tmp_path, minimal_epub_bytes):
        epub_path = tmp_path / "test.epub"
        epub_path.write_bytes(minimal_epub_bytes)
        result = extract_epub(str(epub_path))
        # At least one chapter has non-empty text
        assert any(ch.text.strip() for ch in result.chapters)

    def test_file_hash_is_deterministic(self, tmp_path, minimal_epub_bytes):
        epub_path = tmp_path / "test.epub"
        epub_path.write_bytes(minimal_epub_bytes)
        result1 = extract_epub(str(epub_path))
        result2 = extract_epub(str(epub_path))
        assert result1.file_hash == result2.file_hash

    def test_file_hash_is_md5_hex(self, tmp_path, minimal_epub_bytes):
        epub_path = tmp_path / "test.epub"
        epub_path.write_bytes(minimal_epub_bytes)
        result = extract_epub(str(epub_path))
        # MD5 hex is exactly 32 hex chars
        assert len(result.file_hash) == 32
        assert all(c in "0123456789abcdef" for c in result.file_hash)

    def test_hash_matches_file_bytes(self, tmp_path, minimal_epub_bytes):
        epub_path = tmp_path / "test.epub"
        epub_path.write_bytes(minimal_epub_bytes)
        result = extract_epub(str(epub_path))
        expected = hashlib.md5(minimal_epub_bytes).hexdigest()
        assert result.file_hash == expected

    def test_nonexistent_path_raises(self, tmp_path):
        with pytest.raises(Exception):
            extract_epub(str(tmp_path / "no_such_file.epub"))

    def test_total_chars_positive(self, tmp_path, minimal_epub_bytes):
        epub_path = tmp_path / "test.epub"
        epub_path.write_bytes(minimal_epub_bytes)
        result = extract_epub(str(epub_path))
        assert result.total_chars > 0

    def test_epub_path_stored(self, tmp_path, minimal_epub_bytes):
        epub_path = tmp_path / "test.epub"
        epub_path.write_bytes(minimal_epub_bytes)
        result = extract_epub(str(epub_path))
        assert result.epub_path == str(epub_path)
