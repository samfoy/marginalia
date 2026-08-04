"""Tests for explicit offline translation-sidecar generation."""

from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

import translation_sidecar
from translation_text import hash_normalized


def _write_epub(path):
    with zipfile.ZipFile(path, "w") as epub:
        epub.writestr("mimetype", "application/epub+zip")
        epub.writestr(
            "META-INF/container.xml",
            """<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>""",
        )
        epub.writestr(
            "OEBPS/content.opf",
            """<package xmlns="http://www.idpf.org/2007/opf"><manifest>
<item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>
<item id="two" href="two.xhtml" media-type="application/xhtml+xml"/>
<item id="toc" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
</manifest><spine toc="toc"><itemref idref="one"/><itemref idref="two"/></spine></package>""",
        )
        epub.writestr(
            "OEBPS/one.xhtml",
            '<html><body><p lang="fr"><i>Bonjour, mon ami!</i></p>'
            '<p><em>Hello friend</em></p></body></html>',
        )
        epub.writestr(
            "OEBPS/two.xhtml",
            '<html><body><p lang="es">Buenas noches</p></body></html>',
        )
        epub.writestr(
            "OEBPS/toc.ncx",
            """<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>
<navPoint><navLabel><text>First Chapter</text></navLabel><content src="one.xhtml"/></navPoint>
<navPoint><navLabel><text>Second Chapter</text></navLabel><content src="two.xhtml"/></navPoint>
</navMap></ncx>""",
        )


def _response_for_prompt(prompt, instructions=None):
    request = json.loads(prompt.split("INPUT_JSON:\n", 1)[1])
    rows = []
    for item in request["candidates"]:
        english = item["source"] == "Hello friend"
        translations = {
            "Bonjour, mon ami!": ("French", "Hello, my friend!"),
            "Buenas noches": ("Spanish", "Good night"),
        }
        language, translated = translations.get(item["source"], ("English", ""))
        rows.append(
            {
                "id": item["id"],
                "source_language": language,
                "is_english": english,
                "translation": translated,
            }
        )
    return json.dumps({"translations": rows})


def test_sidecar_path_replaces_only_final_extension(tmp_path):
    epub = tmp_path / "A.Book.v2.epub"
    assert translation_sidecar.sidecar_path(epub) == (
        tmp_path / "A.Book.v2.marginalia-translations.json"
    )


def test_generate_real_epub_batches_and_writes_deterministic_v1_sidecar(tmp_path, monkeypatch):
    epub = tmp_path / "A.Book.epub"
    _write_epub(epub)
    prompts = []

    def fake_complete(prompt, instructions=None):
        prompts.append((prompt, instructions))
        return _response_for_prompt(prompt)

    monkeypatch.setattr(
        translation_sidecar.xray_generator,
        "_complete",
        lambda *args, **kwargs: pytest.fail("default completer was called"),
    )
    fixed = datetime(2026, 8, 4, 12, 30, tzinfo=timezone.utc)
    output = translation_sidecar.generate_translation_sidecar(
        epub, completer=fake_complete, batch_size=2, generated_at=fixed
    )
    first_bytes = output.read_bytes()
    document = json.loads(first_bytes)

    assert output == tmp_path / "A.Book.marginalia-translations.json"
    assert len(prompts) == 2
    assert [len(json.loads(p.split("INPUT_JSON:\n", 1)[1])["candidates"]) for p, _ in prompts] == [2, 1]
    assert all("classify" in instructions.lower() and "translate" in instructions.lower() for _, instructions in prompts)
    assert list(document) == [
        "version", "source_epub", "target_language", "generated_at", "translations"
    ]
    assert document["version"] == 1
    assert document["target_language"] == "English"
    assert document["generated_at"] == "2026-08-04T12:30:00Z"
    assert document["source_epub"] == {
        "filename": "A.Book.epub",
        "size_bytes": epub.stat().st_size,
        "sha256": hashlib.sha256(epub.read_bytes()).hexdigest(),
    }
    assert list(document["translations"]) == [
        hash_normalized("bonjour, mon ami"), hash_normalized("buenas noches")
    ]
    french = document["translations"][hash_normalized("bonjour, mon ami")]
    assert french == {
        "normalized_source": "bonjour, mon ami",
        "original_source": "Bonjour, mon ami!",
        "source_language": "French",
        "translation": "Hello, my friend!",
        "chapter": "First Chapter",
        "location": {
            "spine_path": "OEBPS/one.xhtml",
            "spine_index": 0,
            "candidate_index": 0,
        },
    }
    assert b"\n" not in first_bytes
    assert "hello friend" not in [row["normalized_source"] for row in document["translations"].values()]

    output.unlink()
    translation_sidecar.generate_translation_sidecar(
        epub, completer=fake_complete, batch_size=2, generated_at=fixed
    )
    assert output.read_bytes() == first_bytes


@pytest.mark.parametrize("batch_size", [0, -1, 51])
def test_batch_size_is_positive_and_bounded(tmp_path, batch_size):
    epub = tmp_path / "book.epub"
    _write_epub(epub)
    with pytest.raises(ValueError, match="batch_size"):
        translation_sidecar.generate_translation_sidecar(
            epub, completer=lambda *_args, **_kwargs: "", batch_size=batch_size
        )


def test_default_generation_routes_through_complete(tmp_path, monkeypatch):
    epub = tmp_path / "book.epub"
    _write_epub(epub)
    calls = []

    def fake_default(prompt, instructions=None):
        calls.append((prompt, instructions))
        return _response_for_prompt(prompt)

    monkeypatch.setattr(translation_sidecar.xray_generator, "_complete", fake_default)
    translation_sidecar.generate_translation_sidecar(epub, generated_at="2026-08-04T12:30:00Z")
    assert calls


def test_fenced_trailing_and_repaired_json_are_accepted(tmp_path):
    epub = tmp_path / "book.epub"
    _write_epub(epub)
    call_number = 0

    def fake_complete(prompt, instructions=None):
        nonlocal call_number
        call_number += 1
        valid = _response_for_prompt(prompt)
        if call_number == 1:
            return "```json\n" + valid + "\n``` trailing prose"
        return valid.replace("}]}", "},]}")

    translation_sidecar.generate_translation_sidecar(
        epub, completer=fake_complete, batch_size=2, generated_at="2026-08-04T12:30:00Z"
    )


@pytest.mark.parametrize(
    "bad_response",
    [
        "not json",
        "[]",
        '{"translations": []}',
        '{"translations":[{"id":0,"source_language":"French","is_english":false,"translation":"Hi"},{"id":0,"source_language":"French","is_english":false,"translation":"Hi"},{"id":1,"source_language":"English","is_english":true}]}',
        '{"translations":[{"id":0,"source_language":"French","is_english":false,"translation":"Hi"},{"id":99,"source_language":"English","is_english":true}]}',
        '{"translations":[{"id":0,"source_language":"French","is_english":"false","translation":"Hi"},{"id":1,"source_language":"English","is_english":true}]}',
        '{"translations":[{"id":0,"source_language":"","is_english":false,"translation":"Hi"},{"id":1,"source_language":"English","is_english":true}]}',
        '{"translations":[{"id":0,"source_language":"French","is_english":false,"translation":""},{"id":1,"source_language":"English","is_english":true}]}',
    ],
)
def test_invalid_batches_retry_then_preserve_existing_sidecar(tmp_path, bad_response):
    epub = tmp_path / "book.epub"
    _write_epub(epub)
    output = translation_sidecar.sidecar_path(epub)
    output.write_bytes(b"existing-valid-sidecar")
    calls = []

    def fake_complete(prompt, instructions=None):
        calls.append(prompt)
        return bad_response

    with pytest.raises(ValueError, match="after 3 attempts"):
        translation_sidecar.generate_translation_sidecar(epub, completer=fake_complete, batch_size=2)
    assert len(calls) == 3
    assert "previous response" in calls[1].lower()
    assert output.read_bytes() == b"existing-valid-sidecar"
    assert not list(tmp_path.glob("*.tmp"))


def test_hash_collision_is_rejected_without_replacing_output(tmp_path, monkeypatch):
    epub = tmp_path / "book.epub"
    _write_epub(epub)
    output = translation_sidecar.sidecar_path(epub)
    output.write_bytes(b"old")
    monkeypatch.setattr(translation_sidecar, "hash_normalized", lambda _source: "deadbeef")

    with pytest.raises(ValueError, match="collision"):
        translation_sidecar.generate_translation_sidecar(
            epub, completer=_response_for_prompt, generated_at="2026-08-04T12:30:00Z"
        )
    assert output.read_bytes() == b"old"


def test_successful_refresh_atomically_replaces_existing_sidecar(tmp_path):
    epub = tmp_path / "book.epub"
    _write_epub(epub)
    output = translation_sidecar.sidecar_path(epub)
    output.write_bytes(b"old")
    translation_sidecar.generate_translation_sidecar(
        epub, completer=_response_for_prompt, generated_at="2026-08-04T12:30:00Z"
    )
    assert json.loads(output.read_bytes())["version"] == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_generation_is_reachable_only_from_explicit_cli():
    root = Path(__file__).resolve().parents[1]
    call_sites = []
    product_sources = [
        *root.joinpath("bridge").glob("*.py"),
        *root.joinpath("marginalia.koplugin").glob("*.lua"),
    ]
    for path in product_sources:
        if path.name == "translation_sidecar.py":
            continue
        if "generate_translation_sidecar" in path.read_text(encoding="utf-8"):
            call_sites.append(path.relative_to(root).as_posix())

    assert call_sites == ["bridge/cli.py"]
