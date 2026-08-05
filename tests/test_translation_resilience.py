"""Resilience contract: a partial translation index beats no index at all.

These tests encode the Lolita failure: 388 candidates across 20 batches, where
one persistently invalid batch used to discard the entire book's translations.
"""

from __future__ import annotations

import json

import pytest

from epub_extract import TranslationCandidate
import translation_sidecar as ts


def candidate(source: str, index: int) -> TranslationCandidate:
    return TranslationCandidate(
        original_source=source,
        normalized_source=ts.normalize_source(source),
        language_hint="",
        chapter="Chapter One",
        spine_path="OEBPS/ch1.xhtml",
        spine_index=0,
        candidate_index=index,
    )


def responder(*, fail_ids: set[int] = frozenset(), translate: dict[str, str] | None = None):
    """Model stub: answers every id except fail_ids, which it always omits."""
    translate = translate or {}
    calls = {"n": 0}

    def completer(prompt: str, **kwargs) -> str:
        calls["n"] += 1
        body = prompt[prompt.index("INPUT_JSON:"):]
        payload = json.loads(body[body.index("{"):body.rindex("}") + 1])
        rows = []
        for item in payload["candidates"]:
            if item["id"] in fail_ids:
                continue
            source = item["source"]
            translation = translate.get(source)
            rows.append({
                "id": item["id"],
                "source_language": "French" if translation else "English",
                "is_english": translation is None,
                "translation": translation or "",
            })
        return json.dumps({"translations": rows})

    completer.calls = calls
    return completer


# ── partial results survive a failing batch ──────────────────────────────────

def test_failing_batch_does_not_discard_successful_batches(monkeypatch, tmp_path):
    """One unanswerable candidate must not cost the whole book."""
    sources = [f"phrase {index}" for index in range(6)]
    candidates = [candidate(source, index) for index, source in enumerate(sources)]
    monkeypatch.setattr(ts, "extract_translation_candidates", lambda _path: candidates)

    epub = tmp_path / "Book.epub"
    epub.write_bytes(b"epub-bytes")

    # id 3 lands in the second batch and is never answered.
    completer = responder(
        fail_ids={3},
        translate={source: f"english {index}" for index, source in enumerate(sources)},
    )

    index = ts.build_translation_index(epub, completer=completer, batch_size=2)

    translations = index["translations"]
    # Batches 1 and 3 succeed; the batch holding id 3 is dropped entirely.
    assert len(translations) == 4
    kept = {entry["original_source"] for entry in translations.values()}
    assert kept == {"phrase 0", "phrase 1", "phrase 4", "phrase 5"}


def test_index_reports_skipped_candidate_count(monkeypatch, tmp_path):
    """A partial index must be observable, not silently lossy."""
    candidates = [candidate(f"phrase {index}", index) for index in range(4)]
    monkeypatch.setattr(ts, "extract_translation_candidates", lambda _path: candidates)
    epub = tmp_path / "Book.epub"
    epub.write_bytes(b"epub-bytes")

    completer = responder(fail_ids={2}, translate={"phrase 0": "english 0"})
    index = ts.build_translation_index(epub, completer=completer, batch_size=2)

    assert index["skipped_candidates"] == 2  # the whole failed batch
    assert index["version"] == ts.VERSION


def test_all_batches_failing_yields_empty_but_valid_index(monkeypatch, tmp_path):
    """Total model failure degrades to an empty index, never an exception."""
    candidates = [candidate(f"phrase {index}", index) for index in range(4)]
    monkeypatch.setattr(ts, "extract_translation_candidates", lambda _path: candidates)
    epub = tmp_path / "Book.epub"
    epub.write_bytes(b"epub-bytes")

    def always_invalid(prompt: str, **kwargs) -> str:
        return "not json at all"

    index = ts.build_translation_index(epub, completer=always_invalid, batch_size=2)

    assert index["translations"] == {}
    assert index["skipped_candidates"] == 4
    assert index["source_epub"]["koreader_partial_md5"]


def test_successful_build_reports_zero_skipped(monkeypatch, tmp_path):
    candidates = [candidate("mon cher petit papa", 0)]
    monkeypatch.setattr(ts, "extract_translation_candidates", lambda _path: candidates)
    epub = tmp_path / "Book.epub"
    epub.write_bytes(b"epub-bytes")

    completer = responder(translate={"mon cher petit papa": "my dear little papa"})
    index = ts.build_translation_index(epub, completer=completer, batch_size=20)

    assert index["skipped_candidates"] == 0
    assert len(index["translations"]) == 1


# ── junk candidate filtering ─────────────────────────────────────────────────

@pytest.mark.parametrize("source", [
    "of", "if", "no", "you", "had", "the", "a", "I",
])
def test_short_ascii_emphasis_is_not_sent_to_the_model(source):
    """Lolita italicizes English words for emphasis; they are not translatable."""
    assert ts.is_probable_english_emphasis(source) is True


@pytest.mark.parametrize("source", [
    "mon cher petit papa",
    "chocolat glacé",
    "lycée",
    "manqué",
    "Les Misérables",
    "La Beauté Humaine",
    # Unaccented foreign phrases must survive: no length heuristic can tell
    # these from English emphasis, so only single function words are dropped.
    "Au revoir",
    "Mon Dieu",
    "hasta luego",
    "Buenas noches",
])
def test_real_foreign_phrases_are_kept(source):
    assert ts.is_probable_english_emphasis(source) is False


def test_multi_word_english_is_kept_rather_than_risk_losing_a_translation():
    """A false positive silently loses a translation, so phrases are always kept."""
    assert ts.is_probable_english_emphasis("Hello friend") is False


def test_uncommon_single_english_word_is_kept():
    """Only common function/stress words are filtered, not every ASCII word."""
    assert ts.is_probable_english_emphasis("really") is False


def test_filtering_reduces_candidates_but_keeps_foreign(monkeypatch, tmp_path):
    noise = ["of", "if", "no", "you", "had"]
    real = ["mon cher petit papa", "chocolat glacé"]
    candidates = [candidate(source, index)
                  for index, source in enumerate(noise + real)]
    monkeypatch.setattr(ts, "extract_translation_candidates", lambda _path: candidates)
    epub = tmp_path / "Book.epub"
    epub.write_bytes(b"epub-bytes")

    completer = responder(translate={
        "mon cher petit papa": "my dear little papa",
        "chocolat glacé": "iced chocolate",
    })
    index = ts.build_translation_index(epub, completer=completer, batch_size=20)

    kept = {entry["original_source"] for entry in index["translations"].values()}
    assert kept == set(real)
    # The five English emphasis words never reached the model.
    payload_sizes = completer.calls["n"]
    assert payload_sizes == 1
