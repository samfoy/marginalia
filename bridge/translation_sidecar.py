"""Generate compact offline translation sidecars for EPUB files."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import xray_generator
from epub_extract import TranslationCandidate, extract_translation_candidates
from translation_text import hash_normalized

VERSION = 1
DEFAULT_BATCH_SIZE = 20
MAX_BATCH_SIZE = 50
MAX_ATTEMPTS = 3

Completer = Callable[..., str]

_INSTRUCTIONS = """Classify every candidate's source language and translate every non-English
candidate to English. Return only one JSON object with a translations array. Each item must
contain the supplied integer id, a non-empty source_language, a boolean is_english, and a
non-empty translation when is_english is false. Return exactly one item per supplied id."""


def sidecar_path(epub_path: str | os.PathLike[str]) -> Path:
    """Return the adjacent v1 translation-sidecar path for an EPUB."""
    return Path(epub_path).with_suffix(".marginalia-translations.json")


def _timestamp(value: datetime | str | None) -> str:
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _source_identity(epub_path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    with epub_path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "filename": epub_path.name,
        "size_bytes": epub_path.stat().st_size,
        "sha256": digest.hexdigest(),
        "koreader_partial_md5": _koreader_partial_md5(epub_path),
    }


def _koreader_partial_md5(epub_path: Path) -> str:
    digest = hashlib.md5()
    offsets = [0] + [1024 << (2 * index) for index in range(11)]
    with epub_path.open("rb") as epub:
        for offset in offsets:
            epub.seek(offset)
            chunk = epub.read(1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _request_prompt(batch: list[tuple[int, TranslationCandidate]], previous_error: str = "") -> str:
    request = {
        "candidates": [
            {
                "id": candidate_id,
                "source": candidate.original_source,
                "language_hint": candidate.language_hint,
            }
            for candidate_id, candidate in batch
        ]
    }
    prefix = ""
    if previous_error:
        prefix = (
            "The previous response was invalid: " + previous_error +
            ". Return a corrected complete response for the same input.\n"
        )
    return prefix + "INPUT_JSON:\n" + json.dumps(
        request, ensure_ascii=False, separators=(",", ":")
    )


def _object_fragment(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if "```" in raw:
            raw = raw.split("```", 1)[0]

    start = raw.find("{")
    if start < 0:
        raise ValueError("response does not contain a JSON object")

    depth = 0
    in_string = False
    escaped = False
    for index, character in enumerate(raw[start:], start=start):
        if escaped:
            escaped = False
        elif character == "\\" and in_string:
            escaped = True
        elif character == '"':
            in_string = not in_string
        elif not in_string:
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    return raw[start:index + 1]
    return raw[start:]


def _parse_response(raw: str) -> object:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        fragment = _object_fragment(raw)
    try:
        return json.loads(fragment)
    except json.JSONDecodeError:
        return json.loads(xray_generator._repair_json_text(fragment))


def _validate_response(payload: object, expected_ids: list[int]) -> dict[int, dict[str, object]]:
    if not isinstance(payload, dict) or set(payload) != {"translations"}:
        raise ValueError("response must be an object containing only translations")
    rows = payload["translations"]
    if not isinstance(rows, list):
        raise ValueError("translations must be an array")

    expected = set(expected_ids)
    validated: dict[int, dict[str, object]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each translation result must be an object")
        candidate_id = row.get("id")
        if type(candidate_id) is not int or candidate_id not in expected:
            raise ValueError("response contains an unknown candidate id")
        if candidate_id in validated:
            raise ValueError("response contains a duplicate candidate id")
        is_english = row.get("is_english")
        if type(is_english) is not bool:
            raise ValueError("is_english must be a boolean")
        source_language = row.get("source_language")
        if not isinstance(source_language, str) or not source_language.strip():
            raise ValueError("source_language must be a non-empty string")
        translation = row.get("translation", "")
        if not is_english and (not isinstance(translation, str) or not translation.strip()):
            raise ValueError("non-English results require a translation")
        if is_english and not isinstance(translation, str):
            raise ValueError("translation must be a string when present")
        validated[candidate_id] = {
            "source_language": source_language.strip(),
            "is_english": is_english,
            "translation": translation.strip(),
        }

    if set(validated) != expected:
        raise ValueError("response is missing candidate ids")
    return validated


def _complete_batch(
    batch: list[tuple[int, TranslationCandidate]], completer: Completer
) -> dict[int, dict[str, object]]:
    error = ""
    for _attempt in range(MAX_ATTEMPTS):
        prompt = _request_prompt(batch, error)
        try:
            payload = _parse_response(completer(prompt, instructions=_INSTRUCTIONS))
            return _validate_response(payload, [candidate_id for candidate_id, _ in batch])
        except (ValueError, json.JSONDecodeError, TypeError) as exc:
            error = str(exc)
    raise ValueError(f"translation batch validation failed after {MAX_ATTEMPTS} attempts: {error}")


def _entry(candidate: TranslationCandidate, result: dict[str, object]) -> dict[str, object]:
    entry: dict[str, object] = {
        "normalized_source": candidate.normalized_source,
        "original_source": candidate.original_source,
        "source_language": result["source_language"],
        "translation": result["translation"],
    }
    if candidate.chapter:
        entry["chapter"] = candidate.chapter
    entry["location"] = {
        "spine_path": candidate.spine_path,
        "spine_index": candidate.spine_index,
        "candidate_index": candidate.candidate_index,
    }
    return entry


def _atomic_write_json(path: Path, document: dict[str, object]) -> None:
    encoded = json.dumps(
        document, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def build_translation_index(
    epub_path: str | os.PathLike[str], *, completer: Completer | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE, generated_at: datetime | str | None = None,
) -> dict[str, object]:
    """Build the translation document embedded in a Book Index record."""
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")

    epub = Path(epub_path)
    candidates = extract_translation_candidates(str(epub))
    complete = completer if completer is not None else xray_generator._complete
    results: dict[int, dict[str, object]] = {}
    indexed = list(enumerate(candidates))
    for start in range(0, len(indexed), batch_size):
        results.update(_complete_batch(indexed[start:start + batch_size], complete))

    translations: dict[str, dict[str, object]] = {}
    for candidate_id, candidate in indexed:
        result = results[candidate_id]
        if result["is_english"]:
            continue
        key = hash_normalized(candidate.normalized_source)
        existing = translations.get(key)
        if existing is not None:
            if existing["normalized_source"] != candidate.normalized_source:
                raise ValueError(f"translation hash collision for {key}")
            continue
        translations[key] = _entry(candidate, result)

    return {
        "version": VERSION,
        "source_epub": _source_identity(epub),
        "target_language": "English",
        "generated_at": _timestamp(generated_at),
        "translations": translations,
    }


def generate_translation_sidecar(
    epub_path: str | os.PathLike[str], *, completer: Completer | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE, generated_at: datetime | str | None = None,
) -> Path:
    """Generate and atomically refresh an EPUB's adjacent export sidecar."""
    epub = Path(epub_path)
    document = build_translation_index(
        epub,
        completer=completer,
        batch_size=batch_size,
        generated_at=generated_at,
    )
    output = sidecar_path(epub)
    _atomic_write_json(output, document)
    return output
