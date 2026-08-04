"""Portable text normalization and hashing for translation sidecar lookups.

The contract intentionally uses only transformations that can be reproduced by
stdlib Python and Lua 5.1. It does not perform Unicode normalization or general
Unicode case folding: ASCII A-Z is lowered and all other UTF-8 bytes are kept.

``lookup_key`` is a compact index, not proof of identity. Consumers must compare
the stored normalized source after every hash hit to reject 32-bit collisions.
"""

from __future__ import annotations

import re


_CHARACTER_MAP = str.maketrans(
    {
        "\u00a0": " ",
        "\u202f": " ",
        "\u2018": "'",
        "\u2019": "'",
        "\u02bc": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u00ab": '"',
        "\u00bb": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
    }
)
_ASCII_WHITESPACE = re.compile(r"[ \t\r\n\f\v]+")
_SURROUNDING_NOISE = frozenset("'\".,!?;:-()[]{}")


def _collapse_whitespace(text: str) -> str:
    return _ASCII_WHITESPACE.sub(" ", text).strip(" ")


def normalize_source(source: str) -> str:
    """Return the deterministic source form shared with KOReader Lua."""
    text = source.translate(_CHARACTER_MAP)
    text = _collapse_whitespace(text)
    text = re.sub(r"[A-Z]", lambda match: match.group(0).lower(), text)

    while text and (text[0] in _SURROUNDING_NOISE or text[-1] in _SURROUNDING_NOISE):
        if text and text[0] in _SURROUNDING_NOISE:
            text = text[1:]
        if text and text[-1] in _SURROUNDING_NOISE:
            text = text[:-1]
        text = _collapse_whitespace(text)
    return text


def hash_normalized(normalized_source: str) -> str:
    """Hash normalized UTF-8 bytes with djb2-32 as eight lowercase hex digits."""
    value = 5381
    for byte in normalized_source.encode("utf-8"):
        value = (value * 33 + byte) % (2**32)
    return f"{value:08x}"


def lookup_key(source: str) -> str:
    """Normalize source text and return its portable sidecar lookup key."""
    return hash_normalized(normalize_source(source))
