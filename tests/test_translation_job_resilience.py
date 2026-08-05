"""The Book Index must survive translation generation failure."""

from __future__ import annotations

import logging

import pytest

import server


def test_translation_failure_returns_none_and_does_not_raise(monkeypatch, caplog):
    """A hard failure must degrade to None, never kill the Book Index job."""
    def boom(_path):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(server, "build_translation_index", boom)

    with caplog.at_level(logging.ERROR):
        assert server._safe_translation_index("/books/Lolita.epub") is None
    assert "non-fatal" in caplog.text


def test_partial_translation_index_is_returned_and_logged(monkeypatch, caplog):
    """A partial index is kept — some translations beat none."""
    monkeypatch.setattr(server, "build_translation_index", lambda _path: {
        "version": 1,
        "translations": {"abcd1234": {"translation": "hello"}},
        "skipped_candidates": 40,
    })

    with caplog.at_level(logging.WARNING):
        index = server._safe_translation_index("/books/Lolita.epub")

    assert index is not None
    assert len(index["translations"]) == 1
    assert index["skipped_candidates"] == 40
    assert "partial" in caplog.text


def test_complete_translation_index_passes_through(monkeypatch):
    payload = {
        "version": 1,
        "translations": {"abcd1234": {"translation": "hello"}},
        "skipped_candidates": 0,
    }
    monkeypatch.setattr(server, "build_translation_index", lambda _path: payload)
    assert server._safe_translation_index("/books/Lolita.epub") == payload


def test_empty_index_is_still_returned(monkeypatch):
    """Zero translatable passages is a valid outcome, not a failure."""
    payload = {"version": 1, "translations": {}, "skipped_candidates": 0}
    monkeypatch.setattr(server, "build_translation_index", lambda _path: payload)
    assert server._safe_translation_index("/books/English.epub") == payload
