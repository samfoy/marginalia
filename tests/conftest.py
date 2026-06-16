"""conftest.py — shared fixtures for the marginalia test suite."""

import io
import sys
import zipfile
from pathlib import Path

import pytest

# ── sys.path: make bridge/ importable as a flat package ──────────────────────
_BRIDGE = Path(__file__).parent.parent / "bridge"
if str(_BRIDGE) not in sys.path:
    sys.path.insert(0, str(_BRIDGE))

import xray_cache  # noqa: E402 — needs sys.path first


# ── tmp_vault ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_vault(tmp_path):
    """Create a minimal vault tree and return the vault root Path."""
    vault = tmp_path / "vault"
    (vault / "Notes" / "Books").mkdir(parents=True)
    (vault / "Notes" / "Captures").mkdir(parents=True)
    return vault


# ── tmp_cache ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_cache(tmp_path, monkeypatch):
    """Redirect xray_cache to a temp dir so tests never touch ~/.marginalia/."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(xray_cache, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(xray_cache, "INDEX_FILE", cache_dir / "index.json")
    return cache_dir


# ── minimal_epub_bytes ────────────────────────────────────────────────────────

@pytest.fixture()
def minimal_epub_bytes():
    """
    Return bytes of a real minimal EPUB 2 archive that extract_epub() can parse.
    Contains one chapter spine item with text "Hello from chapter one".
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        # mimetype — must be first, stored uncompressed per EPUB spec
        z.writestr(
            zipfile.ZipInfo("mimetype"),
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?>'
            '<container version="1.0"'
            ' xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            "<rootfiles>"
            '<rootfile full-path="OEBPS/content.opf"'
            ' media-type="application/oebps-package+xml"/>'
            "</rootfiles>"
            "</container>",
        )
        z.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf"'
            ' version="2.0" unique-identifier="uid">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"'
            ' xmlns:opf="http://www.idpf.org/2007/opf">'
            "<dc:title>Test Book</dc:title>"
            "<dc:creator>Test Author</dc:creator>"
            "</metadata>"
            "<manifest>"
            '<item id="ch0" href="ch0.xhtml"'
            ' media-type="application/xhtml+xml"/>'
            "</manifest>"
            "<spine>"
            '<itemref idref="ch0"/>'
            "</spine>"
            "</package>",
        )
        z.writestr(
            "OEBPS/ch0.xhtml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml">'
            "<head><title>Chapter 1</title></head>"
            "<body>"
            "<h1>Chapter 1</h1>"
            "<p>Hello from chapter one</p>"
            "</body></html>",
        )
    return buf.getvalue()
