"""
series.py — Cross-book series context for X-Ray.

When opening book N in a series, pull characters/locations/terms from
books 1..N-1 whose X-Rays are already cached. Annotates inherited
entities with source_book so the UI can show "From Red Rising #1".

Called from xray_generator.build_record() when series info is present.
"""

import logging
import os
import re
import sqlite3
from copy import deepcopy

import xray_cache

logger = logging.getLogger(__name__)

_CALIBRE_DB = os.path.expanduser("~/CalibreLibrary/metadata.db")


# ── Authoritative series resolution from Calibre's metadata.db ──────────────────
# The EPUB-embedded OPF and per-book metadata.opf are frequently stale or
# missing series tags; metadata.db is the source of truth.

def resolve(calibre_id: int | None = None, title: str = "",
            author: str = "") -> dict | None:
    """Return {'series': name, 'series_index': int} from metadata.db, or None."""
    if not os.path.exists(_CALIBRE_DB):
        return None
    try:
        con = sqlite3.connect(f"file:{_CALIBRE_DB}?mode=ro", uri=True)
        try:
            base = (
                "SELECT s.name, b.series_index FROM books b "
                "LEFT JOIN books_series_link l ON l.book = b.id "
                "LEFT JOIN series s ON s.id = l.series "
            )
            row = None
            if calibre_id:
                row = con.execute(base + "WHERE b.id = ?", (calibre_id,)).fetchone()
            if (not row or not row[0]) and title:
                row = con.execute(base + "WHERE b.title = ? COLLATE NOCASE",
                                  (title,)).fetchone()
            if (not row or not row[0]) and title:
                # Fuzzy: strip subtitle after colon/dash, match prefix
                stem = re.split(r"[:\u2014-]", title, 1)[0].strip()
                if stem and stem.lower() != title.lower():
                    row = con.execute(base + "WHERE b.title LIKE ? COLLATE NOCASE",
                                      (stem + "%",)).fetchone()
            if row and row[0]:
                return {"series": row[0], "series_index": int(float(row[1] or 0))}
        finally:
            con.close()
    except Exception:
        logger.exception("series.resolve failed (calibre_id=%s title=%r)", calibre_id, title)
    return None


def build_scope(record: dict, reading_pct: float) -> list[dict]:
    """Spoiler-bounded reading scope across a series.

    Returns an ordered list of {hash, title, series_index, max_pct, current}:
      - the current book, bounded to reading_pct
      - every PRIOR book in the series that has a retrieval index, bounded to
        its last_reading_pct (default 100% — assumed finished)
    Future books (index > current) are excluded entirely. This is the spoiler
    boundary for all series-aware retrieval.
    """
    book = record.get("book", {})
    cur_hash = book.get("epub_hash")
    cur_idx = book.get("series_index")
    series = book.get("series")
    scope = [{
        "hash": cur_hash,
        "title": book.get("title", ""),
        "series_index": cur_idx,
        "max_pct": float(reading_pct) if reading_pct else 100.0,
        "current": True,
    }]
    if not (series and cur_idx):
        return scope
    for r in xray_cache.get_series(series):
        b = r.get("book", {})
        idx = b.get("series_index") or 0
        h = b.get("epub_hash")
        if 0 < idx < cur_idx and h and h != cur_hash:
            # Prior books in the series are assumed finished — if you're reading
            # book N you've read 1..N-1 in full. last_reading_pct only reflects
            # where the cache last saw you, so it's not a reliable bound here.
            scope.append({
                "hash": h,
                "title": b.get("title", ""),
                "series_index": idx,
                "max_pct": 100.0,
                "current": False,
            })
    scope.sort(key=lambda s: s.get("series_index") or 0)
    return scope


def _norm(name: str) -> str:
    return name.lower().strip() if name else ""


def _entity_key(entity: dict, name_key: str = "name") -> str:
    return _norm(entity.get(name_key, ""))


def _merge_from_prior(
    current: list[dict],
    prior: list[dict],
    desc_key: str,
    source_label: str,
) -> list[dict]:
    """
    Merge prior-book entities into current, tagging them source_book=source_label.
    Existing entities are NOT overwritten — prior knowledge is supplemental.
    Characters/locations already in current are enriched with prior description
    only if they have none.
    """
    current_keys = {_entity_key(e) for e in current}
    added = 0
    enriched = 0

    for prior_entity in prior:
        key = _entity_key(prior_entity)
        if not key:
            continue

        if key in current_keys:
            # Already known — optionally enrich if current has no description
            for e in current:
                if _entity_key(e) == key:
                    if not e.get(desc_key) and prior_entity.get(desc_key):
                        e[desc_key] = f"[From {source_label}] {prior_entity[desc_key]}"
                        enriched += 1
                    break
        else:
            # Not yet known — import with source tag. These come from books the
            # reader has finished, so they are KNOWN: force first_appearance_pct
            # to 0 so the current-book spoiler filter never hides them.
            copy = deepcopy(prior_entity)
            copy["source_book"]  = source_label
            copy["source_label"] = f"From {source_label}"
            copy["first_appearance_pct"] = 0
            current.append(copy)
            current_keys.add(key)
            added += 1

    if added or enriched:
        logger.info("series: %s → added %d, enriched %d", source_label, added, enriched)
    return current


def inject_series_context(xray: dict, series: str, series_index: int) -> dict:
    """
    Pull entities from prior books in the same series that are already cached.

    Mutates xray in place (adds/enriches characters, locations, terms from
    earlier books). Returns the modified xray.
    """
    if not series or not series_index or series_index <= 1:
        return xray

    prior_records = xray_cache.get_series(series)
    # Filter to books with a lower index than current
    prior_books = [
        r for r in prior_records
        if r.get("book", {}).get("series_index", 0) < series_index
    ]

    if not prior_books:
        logger.info("series: no prior books cached for '%s'", series)
        return xray

    logger.info(
        "series: injecting context from %d prior book(s) of '%s'",
        len(prior_books), series
    )

    for record in sorted(prior_books, key=lambda r: r.get("book", {}).get("series_index", 0)):
        book_meta = record.get("book", {})
        idx   = book_meta.get("series_index", "?")
        title = book_meta.get("title", f"Book {idx}")
        label = f"{series} #{idx} ({title})"
        prior = record.get("xray", {})

        xray["characters"] = _merge_from_prior(
            xray.get("characters", []),
            prior.get("characters", []),
            "description", label,
        )
        xray["locations"] = _merge_from_prior(
            xray.get("locations", []),
            prior.get("locations", []),
            "description", label,
        )
        xray["terms"] = _merge_from_prior(
            xray.get("terms", []),
            prior.get("terms", []),
            "definition", label,
        )
        # References carry over (mythological/literary context persists across series)
        xray["references"] = _merge_from_prior(
            xray.get("references", []),
            prior.get("references", []),
            "description", label,
        )

    return xray
