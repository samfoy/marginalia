"""
xray_cache.py — Per-book Book Index cache in ~/.marginalia/cache/.

Structure:
  ~/.marginalia/cache/
    index.json          — quick-lookup table (title/author/hash/metadata)
    <md5_hash>.json     — full Book Index data for one book
"""

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_DIR  = Path.home() / ".marginalia" / "cache"
INDEX_FILE = CACHE_DIR / "index.json"
_lock      = threading.Lock()


def _ensure() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: Path, data: dict) -> None:
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(data, temporary, ensure_ascii=False, indent=2)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


# ── Full X-Ray data ────────────────────────────────────────────────────────────

def load(book_hash: str) -> dict | None:
    """Load full X-Ray record by hash. Returns None on miss."""
    path = CACHE_DIR / f"{book_hash}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Cache read error (%s): %s", path, e)
        return None


def save(book_hash: str, record: dict) -> None:
    """
    Persist a full X-Ray record and update the index.
    `record` should match the schema produced by xray_generator.build_record().
    """
    _ensure()
    path = CACHE_DIR / f"{book_hash}.json"
    with _lock:
        _atomic_write_json(path, record)
        _update_index(book_hash, record)
    logger.info("Cache saved: %s (%s)", record.get("book", {}).get("title", "?"), book_hash)


def merge_translation_index(book_hash: str, translation_index: dict) -> dict:
    """Atomically merge translations into the latest cache record."""
    _ensure()
    path = CACHE_DIR / f"{book_hash}.json"
    with _lock:
        if not path.exists():
            raise FileNotFoundError(f"Book Index cache missing for {book_hash}")
        record = json.loads(path.read_text(encoding="utf-8"))
        previous_bytes = path.read_bytes()
        previous_index_bytes = INDEX_FILE.read_bytes() if INDEX_FILE.exists() else None
        record["translation_index"] = translation_index
        record["generated_at"] = _now()
        try:
            _atomic_write_json(path, record)
            _update_index(book_hash, record)
        except Exception:
            path.write_bytes(previous_bytes)
            if previous_index_bytes is None:
                try:
                    INDEX_FILE.unlink()
                except FileNotFoundError:
                    pass
            else:
                INDEX_FILE.write_bytes(previous_index_bytes)
            raise
    return record


def _update_index(book_hash: str, record: dict) -> None:
    index = _load_index()
    book  = record.get("book", {})
    xray  = record.get("xray", {})
    index["books"][book_hash] = {
        "hash":            book_hash,
        "title":           book.get("title", ""),
        "author":          book.get("author", ""),
        "series":          book.get("series"),
        "series_index":    book.get("series_index"),
        "calibre_id":      book.get("calibre_id"),
        "epub_path":       book.get("epub_path", ""),
        "generated_at":    record.get("generated_at", ""),
        "strategy":        record.get("strategy", ""),
        "character_count":  len(xray.get("characters", [])),
        "location_count":   len(xray.get("locations", [])),
        "term_count":       len(xray.get("terms", [])),
        "reference_count":  len(xray.get("references", [])),
        "timeline_count":   len(xray.get("timeline", [])),
        "last_reading_pct": record.get("last_reading_pct"),
    }
    index["updated"] = _now()
    _atomic_write_json(INDEX_FILE, index)


def update_reading_pct(book_hash: str, pct: float) -> None:
    """Update last-known reading position without regenerating."""
    _ensure()
    with _lock:
        index = _load_index()
        entry = index.get("books", {}).get(book_hash)
        if entry is not None:
            entry["last_reading_pct"] = round(pct, 1)
            index["updated"] = _now()
            _atomic_write_json(INDEX_FILE, index)
        # Also update the full record if it exists
        full = load(book_hash)
        if full:
            full["last_reading_pct"] = round(pct, 1)
            path = CACHE_DIR / f"{book_hash}.json"
            _atomic_write_json(path, full)


def remove_knowledge_by_title(title: str, author: str = "") -> list[str]:
    """Remove synthetic knowledge-only records superseded by a real EPUB."""
    tl = title.lower().strip()
    al = author.lower().strip() if author else ""
    removed: list[str] = []
    with _lock:
        index = _load_index()
        staged: list[tuple[str, dict, Path | None]] = []
        for book_hash, meta in list(index.get("books", {}).items()):
            if meta.get("strategy") != "knowledge_only":
                continue
            if meta.get("title", "").lower().strip() != tl:
                continue
            meta_author = meta.get("author", "").lower().strip()
            if (al and al not in meta_author) or (not al and meta_author):
                continue
            path = CACHE_DIR / f"{book_hash}.json"
            tombstone = CACHE_DIR / f"{book_hash}.json.deleting"
            try:
                os.replace(path, tombstone)
                staged_path: Path | None = tombstone
            except FileNotFoundError:
                staged_path = None
            except OSError as error:
                logger.warning("Could not stage superseded cache %s: %s", book_hash, error)
                continue
            staged.append((book_hash, meta, staged_path))
            index["books"].pop(book_hash, None)
        if staged:
            index["updated"] = _now()
            try:
                _atomic_write_json(INDEX_FILE, index)
            except Exception:
                for book_hash, meta, staged_path in staged:
                    index["books"][book_hash] = meta
                    if staged_path is not None:
                        os.replace(staged_path, CACHE_DIR / f"{book_hash}.json")
                raise
            for book_hash, _meta, staged_path in staged:
                removed.append(book_hash)
                if staged_path is None:
                    continue
                try:
                    staged_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as error:
                    logger.warning("Could not remove superseded cache tombstone %s: %s", book_hash, error)
    return removed


# ── Index queries (used by pi chat) ───────────────────────────────────────────

def load_index() -> dict:
    _ensure()
    return _load_index()


def find_all_by_title_author(title: str, author: str = "") -> list[dict]:
    """Return all cached editions matching title/author."""
    tl = title.lower().strip()
    al = author.lower().strip() if author else ""
    records = []
    for book_hash, meta in _load_index().get("books", {}).items():
        if meta.get("title", "").lower().strip() != tl:
            continue
        if al and al not in meta.get("author", "").lower():
            continue
        record = load(book_hash)
        if record:
            records.append(record)
    return records


def find_by_title_author(title: str, author: str = "") -> dict | None:
    """
    Find a cached X-Ray by title (exact, case-insensitive).
    Returns the full record or None.
    """
    records = find_all_by_title_author(title, author)
    records.sort(key=lambda record: record.get("strategy") == "knowledge_only")
    return records[0] if records else None


def list_cached() -> list[dict]:
    """Return all index entries — for pi chat browsing / 'what books do I have X-Ray for'."""
    return list(_load_index().get("books", {}).values())


def get_series(series_name: str) -> list[dict]:
    """Return full X-Ray records for every cached book in a series, sorted by index."""
    sn = series_name.lower().strip()
    results = []
    for book_hash, meta in _load_index().get("books", {}).items():
        if (meta.get("series") or "").lower().strip() == sn:
            rec = load(book_hash)
            if rec:
                results.append(rec)
    return sorted(results, key=lambda r: r.get("book", {}).get("series_index") or 0)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_index() -> dict:
    if INDEX_FILE.exists():
        try:
            return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"version": 1, "updated": "", "books": {}}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
