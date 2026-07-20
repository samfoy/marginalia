"""test_vault_notes.py — unit tests for _save_vault_note and _create_standalone_note."""

import os
import re

import pytest

import server


# ── helpers ───────────────────────────────────────────────────────────────────

def _books_dir(tmp_vault):
    return str(tmp_vault / "Notes" / "Books")


def _captures_dir(tmp_vault):
    return str(tmp_vault / "Notes" / "Captures")


# ═══════════════════════════════════════════════════════════════════════════════
# _save_vault_note
# ═══════════════════════════════════════════════════════════════════════════════

class TestSaveVaultNote:

    def test_creates_file_with_frontmatter(self, tmp_vault, monkeypatch):
        monkeypatch.setattr(server, "BOOKS_DIR", _books_dir(tmp_vault))
        path = server._save_vault_note(
            highlight="A great passage",
            context="",
            book_title="Dune",
            book_author="Frank Herbert",
            reading_pct=42.0,
        )
        content = open(path).read()
        assert '---' in content
        assert 'title: "Dune"' in content
        assert 'author: "Frank Herbert"' in content
        assert "tags:" in content
        assert "  - book" in content
        assert "# Dune" in content
        assert "## Notes" in content

    def test_appends_under_notes_on_second_call(self, tmp_vault, monkeypatch):
        monkeypatch.setattr(server, "BOOKS_DIR", _books_dir(tmp_vault))
        kwargs = dict(
            highlight="First note",
            context="",
            book_title="Dune",
            book_author="Frank Herbert",
            reading_pct=10.0,
        )
        server._save_vault_note(**kwargs)
        server._save_vault_note(highlight="Second note", context="",
                                book_title="Dune", book_author="Frank Herbert",
                                reading_pct=20.0)
        path = os.path.join(_books_dir(tmp_vault), "Frank Herbert - Dune.md")
        content = open(path).read()
        # Frontmatter should appear exactly once
        assert content.count("---\ntitle:") == 1
        assert "First note" in content
        assert "Second note" in content

    def test_chat_note_includes_asked_and_ai(self, tmp_vault, monkeypatch):
        monkeypatch.setattr(server, "BOOKS_DIR", _books_dir(tmp_vault))
        path = server._save_vault_note(
            highlight="",
            context="",
            book_title="Dune",
            book_author="Frank Herbert",
            reading_pct=55.0,
            query="What is the Bene Gesserit?",
            response="A sisterhood that manipulates genetics.",
            source="Chat",
        )
        content = open(path).read()
        assert "**Asked:**" in content
        assert "What is the Bene Gesserit?" in content
        assert "**AI:**" in content
        assert "A sisterhood that manipulates genetics." in content

    def test_query_not_echoed_when_same_as_highlight(self, tmp_vault, monkeypatch):
        monkeypatch.setattr(server, "BOOKS_DIR", _books_dir(tmp_vault))
        text = "Fear is the mind-killer"
        path = server._save_vault_note(
            highlight=text,
            context="",
            book_title="Dune",
            book_author="Frank Herbert",
            reading_pct=30.0,
            query=text,   # identical to highlight — should NOT be echoed
            response="A famous litany.",
        )
        content = open(path).read()
        # The text appears once (in highlight), not again under **Asked:**
        assert "**Asked:**" not in content
        assert text in content

    def test_no_author_filename(self, tmp_vault, monkeypatch):
        monkeypatch.setattr(server, "BOOKS_DIR", _books_dir(tmp_vault))
        path = server._save_vault_note(
            highlight="test", context="",
            book_title="Orphan Book", book_author="",
            reading_pct=0,
        )
        assert os.path.basename(path) == "Orphan Book.md"

    def test_special_chars_stripped_from_filename(self, tmp_vault, monkeypatch):
        monkeypatch.setattr(server, "BOOKS_DIR", _books_dir(tmp_vault))
        path = server._save_vault_note(
            highlight="hi", context="",
            book_title='A/B: "Test" Book?',
            book_author="Author*Name|Here",
            reading_pct=0,
        )
        fname = os.path.basename(path)
        assert "/" not in fname
        assert "?" not in fname
        assert '"' not in fname
        assert "*" not in fname
        assert "|" not in fname
        assert fname.endswith(".md")

    def test_reading_pct_in_bullet(self, tmp_vault, monkeypatch):
        monkeypatch.setattr(server, "BOOKS_DIR", _books_dir(tmp_vault))
        path = server._save_vault_note(
            highlight="test", context="",
            book_title="Book", book_author="Author",
            reading_pct=73.9,
        )
        content = open(path).read()
        assert "(73%)" in content

    def test_zero_pct_omitted_from_bullet(self, tmp_vault, monkeypatch):
        monkeypatch.setattr(server, "BOOKS_DIR", _books_dir(tmp_vault))
        path = server._save_vault_note(
            highlight="test", context="",
            book_title="Book", book_author="Author",
            reading_pct=0,
        )
        content = open(path).read()
        # pct_tag is empty when reading_pct is falsy
        assert "(0%)" not in content

    def test_returns_absolute_path(self, tmp_vault, monkeypatch):
        monkeypatch.setattr(server, "BOOKS_DIR", _books_dir(tmp_vault))
        path = server._save_vault_note(
            highlight="x", context="",
            book_title="B", book_author="A",
            reading_pct=0,
        )
        assert os.path.isabs(path)
        assert os.path.exists(path)

    def test_reuses_existing_note_despite_subtitle_and_author_variation(
        self, tmp_vault, monkeypatch
    ):
        """KOReader sends full EPUB metadata (long subtitle, multi-author). If an import
        already made a short-named note for the same book, the save must land there, not
        create a duplicate file."""
        books = _books_dir(tmp_vault)
        os.makedirs(books, exist_ok=True)
        monkeypatch.setattr(server, "BOOKS_DIR", books)
        # Pre-existing import-style note under a clean short name.
        existing = os.path.join(books, "Culadasa - The Mind Illuminated.md")
        with open(existing, "w", encoding="utf-8") as f:
            f.write(
                '---\ntitle: "The Mind Illuminated: A Complete Meditation Guide"\n'
                'author: "Culadasa (John Yates) & Matthew Immergut"\n'
                "tags:\n  - book\n---\n\n# The Mind Illuminated\n\n## Notes\n\n"
            )
        # KOReader save with the messy full metadata.
        path = server._save_vault_note(
            highlight="A passage on mind-wandering",
            context="",
            book_title="The Mind Illuminated: A Complete Meditation Guide Integrating "
                       "Buddhist Wisdom and Brain Science for Greater Mindfulness",
            book_author="CuladasaMatthew Immergut, Phd",
            reading_pct=18.0,
        )
        # Must reuse the existing file, not mint a second one.
        assert path == existing
        md_files = [f for f in os.listdir(books) if f.endswith(".md")]
        assert len(md_files) == 1, f"expected 1 note, got {md_files}"
        assert "A passage on mind-wandering" in open(existing).read()

    def test_creates_note_when_no_existing_match(self, tmp_vault, monkeypatch):
        books = _books_dir(tmp_vault)
        monkeypatch.setattr(server, "BOOKS_DIR", books)
        path = server._save_vault_note(
            highlight="hi", context="",
            book_title="A Totally New Book", book_author="Jane Doe",
            reading_pct=5,
        )
        assert os.path.basename(path) == "Jane Doe - A Totally New Book.md"
        assert os.path.exists(path)


# ═══════════════════════════════════════════════════════════════════════════════
# _create_standalone_note
# ═══════════════════════════════════════════════════════════════════════════════

class TestCreateStandaloneNote:

    def test_creates_file_with_frontmatter_and_body(self, tmp_vault, monkeypatch):
        monkeypatch.setattr(server, "CAPTURES_DIR", _captures_dir(tmp_vault))
        path = server._create_standalone_note(
            title="Emergency kit",
            body="Gloves, suction bulb, cord clamp.",
            book_title="The Expectant Father",
            book_author="Jennifer Ash Rudick",
            reading_pct=52.0,
        )
        content = open(path).read()
        assert '---' in content
        assert 'title: "Emergency kit"' in content
        assert 'source: "The Expectant Father"' in content
        assert 'author: "Jennifer Ash Rudick"' in content
        assert "reading-capture" in content
        assert "# Emergency kit" in content
        assert "Gloves, suction bulb, cord clamp." in content

    def test_includes_wikilink_when_book_info_given(self, tmp_vault, monkeypatch):
        monkeypatch.setattr(server, "CAPTURES_DIR", _captures_dir(tmp_vault))
        path = server._create_standalone_note(
            title="My note",
            body="Some content",
            book_title="Dune",
            book_author="Frank Herbert",
            reading_pct=20.0,
        )
        content = open(path).read()
        assert "[[Frank Herbert - Dune]]" in content

    def test_wikilink_with_title_only(self, tmp_vault, monkeypatch):
        monkeypatch.setattr(server, "CAPTURES_DIR", _captures_dir(tmp_vault))
        path = server._create_standalone_note(
            title="My note",
            body="Some content",
            book_title="Dune",
            book_author="",
            reading_pct=0,
        )
        content = open(path).read()
        assert "[[Dune]]" in content

    def test_no_wikilink_without_book_info(self, tmp_vault, monkeypatch):
        monkeypatch.setattr(server, "CAPTURES_DIR", _captures_dir(tmp_vault))
        path = server._create_standalone_note(
            title="Standalone",
            body="Body text",
            book_title="",
            book_author="",
            reading_pct=0,
        )
        content = open(path).read()
        assert "[[" not in content
        assert 'source:' not in content
        assert 'author:' not in content

    def test_second_call_appends_not_overwrites(self, tmp_vault, monkeypatch):
        monkeypatch.setattr(server, "CAPTURES_DIR", _captures_dir(tmp_vault))
        server._create_standalone_note(
            title="My note", body="First body",
            book_title="B", book_author="A", reading_pct=0,
        )
        server._create_standalone_note(
            title="My note", body="Second body",
            book_title="B", book_author="A", reading_pct=0,
        )
        fname = "My note.md"
        content = open(os.path.join(_captures_dir(tmp_vault), fname)).read()
        # Frontmatter appears exactly once
        assert content.count('title: "My note"') == 1
        assert "First body" in content
        assert "Second body" in content
        # Appended section uses ---
        assert "\n---\n" in content

    def test_filename_sanitised(self, tmp_vault, monkeypatch):
        monkeypatch.setattr(server, "CAPTURES_DIR", _captures_dir(tmp_vault))
        path = server._create_standalone_note(
            title='Bad/chars: "test"?',
            body="body",
        )
        fname = os.path.basename(path)
        assert "/" not in fname
        assert "?" not in fname
        assert '"' not in fname
        assert fname.endswith(".md")

    def test_filename_truncated_at_100_chars(self, tmp_vault, monkeypatch):
        monkeypatch.setattr(server, "CAPTURES_DIR", _captures_dir(tmp_vault))
        long_title = "A" * 120
        path = server._create_standalone_note(title=long_title, body="x")
        fname = os.path.basename(path)
        # safe(title)[:100] + ".md" → 104 chars max
        assert len(fname) <= 104

    def test_returns_absolute_path(self, tmp_vault, monkeypatch):
        monkeypatch.setattr(server, "CAPTURES_DIR", _captures_dir(tmp_vault))
        path = server._create_standalone_note(title="t", body="b")
        assert os.path.isabs(path)
        assert os.path.exists(path)
