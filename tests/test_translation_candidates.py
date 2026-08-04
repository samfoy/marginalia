"""Contract tests for deterministic EPUB translation-candidate extraction."""

import zipfile

from epub_extract import extract_translation_candidates


def _write_candidate_epub(path):
    """Write an EPUB whose archive order deliberately differs from spine order."""
    with zipfile.ZipFile(path, "w") as epub:
        epub.writestr("mimetype", "application/epub+zip")
        epub.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles>
</container>""",
        )
        epub.writestr(
            "OEBPS/content.opf",
            """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf">
  <manifest>
    <item id="one" href="chapter-one.xhtml" media-type="application/xhtml+xml"/>
    <item id="two" href="chapter-two.xhtml" media-type="application/xhtml+xml"/>
    <item id="toc" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  </manifest>
  <spine toc="toc"><itemref idref="two"/><itemref idref="one"/></spine>
</package>""",
        )
        epub.writestr(
            "OEBPS/chapter-one.xhtml",
            """<html xmlns="http://www.w3.org/1999/xhtml"><head><title>One</title></head>
<body lang="en"><p><em>English <b>marked</b> text</em></p>
<p lang="es">Buenas tardes</p><p><i>Repeated phrase</i></p></body></html>""",
        )
        epub.writestr(
            "OEBPS/chapter-two.xhtml",
            """<html xmlns="http://www.w3.org/1999/xhtml" lang="en">
<head><title><em>hidden head</em></title><style>.x { content: '<i>hidden style</i>'; }</style></head>
<body><script><em>hidden script</em></script>
<p><i>Repeated phrase</i></p>
<p lang="fr">«Mon&nbsp;cher&nbsp;ami!»</p>
<div xml:lang="fr">“C’est&nbsp;moi—Humbert.”</div>
<em>Avant <i>mon amour</i> après</em>
<div lang="fr">salut <em lang="DE">Guten Tag</em></div>
<div lang="fr"><i>Ça va</i></div>
<em>“—”</em>
</body></html>""",
        )
        epub.writestr(
            "OEBPS/toc.ncx",
            """<?xml version="1.0"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>
<navPoint><navLabel><text>Chapter One</text></navLabel><content src="chapter-one.xhtml"/></navPoint>
<navPoint><navLabel><text>Chapter Two</text></navLabel><content src="chapter-two.xhtml"/></navPoint>
</navMap></ncx>""",
        )


def test_candidates_follow_spine_and_source_start_order(tmp_path):
    epub_path = tmp_path / "candidates.epub"
    _write_candidate_epub(epub_path)

    candidates = extract_translation_candidates(epub_path)

    assert [candidate.original_source for candidate in candidates] == [
        "Repeated phrase",
        "«Mon cher ami!»",
        "“C’est moi—Humbert.”",
        "Avant mon amour après",
        "mon amour",
        "salut Guten Tag",
        "Guten Tag",
        "Ça va",
        "English marked text",
        "Buenas tardes",
    ]
    assert [candidate.candidate_index for candidate in candidates] == list(range(10))
    assert [candidate.spine_index for candidate in candidates] == [0] * 8 + [1] * 2


def test_candidates_retain_location_language_and_shared_normalization(tmp_path):
    epub_path = tmp_path / "candidates.epub"
    _write_candidate_epub(epub_path)

    candidates = extract_translation_candidates(epub_path)
    by_source = {candidate.original_source: candidate for candidate in candidates}

    french = by_source["«Mon cher ami!»"]
    assert french.normalized_source == "mon cher ami"
    assert french.language_hint == "fr"
    assert french.chapter == "Chapter Two"
    assert french.spine_path == "OEBPS/chapter-two.xhtml"
    assert french.spine_index == 0

    assert by_source["Avant mon amour après"].language_hint == "en"
    assert by_source["mon amour"].language_hint == "en"
    assert by_source["salut Guten Tag"].language_hint == "fr"
    assert by_source["Guten Tag"].language_hint == "de"
    assert by_source["English marked text"].chapter == "Chapter One"


def test_candidates_decode_entities_skip_non_content_and_drop_noise(tmp_path):
    epub_path = tmp_path / "candidates.epub"
    _write_candidate_epub(epub_path)

    candidates = extract_translation_candidates(epub_path)
    sources = [candidate.original_source for candidate in candidates]

    assert "C’est moi—Humbert." in sources[2]
    assert all("hidden" not in source for source in sources)
    assert "“—”" not in sources
    assert not any("<" in source or ">" in source for source in sources)


def test_candidates_deduplicate_globally_and_keep_first_location(tmp_path):
    epub_path = tmp_path / "candidates.epub"
    _write_candidate_epub(epub_path)

    candidates = extract_translation_candidates(epub_path)
    repeated = [candidate for candidate in candidates if candidate.normalized_source == "repeated phrase"]
    nested_identical = [candidate for candidate in candidates if candidate.normalized_source == "Ça va"]

    assert len(repeated) == 1
    assert repeated[0].chapter == "Chapter Two"
    assert repeated[0].spine_index == 0
    assert len(nested_identical) == 1


def test_candidates_are_deterministic_without_filtering_english(tmp_path):
    epub_path = tmp_path / "candidates.epub"
    _write_candidate_epub(epub_path)

    first = extract_translation_candidates(epub_path)
    second = extract_translation_candidates(epub_path)

    assert first == second
    assert any(candidate.normalized_source == "english marked text" for candidate in first)
    assert any(candidate.normalized_source == "buenas tardes" for candidate in first)
