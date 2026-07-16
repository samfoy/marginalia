"""
bookorbit_source.py — Fetch EPUBs from a running BookOrbit server.

BookOrbit (https://github.com/bookorbit/bookorbit) is a self-hosted library
server. Unlike the local Calibre / flat-file layouts in book_finder.py, this
source talks to the BookOrbit HTTP API: it searches by title, resolves the
book's primary EPUB file, downloads it to a local cache, and returns a
book_finder-shaped dict so the rest of the pipeline (extract_epub, RAG, series)
works unchanged.

API flow (verified against BookOrbit ghcr.io/bookorbit/bookorbit, 2026-07):
  1. POST /api/v1/auth/login           {username, password} -> {accessToken}
  2. GET  /api/v1/books/search?q=TITLE -> [{id, title, seriesName, authors,
                                            seriesIndex?, formats}]
  3. GET  /api/v1/books/{id}           -> {..., files:[{id, format, role,
                                            filename, sizeBytes}]}
  4. GET  /api/v1/books/files/{fileId}/download  -> raw EPUB bytes

Enable by setting (see .env.example):
  MARGINALIA_BOOKORBIT_URL=http://bookorbit-app:3000   (base, no trailing /api)
  MARGINALIA_BOOKORBIT_USER=sam
  MARGINALIA_BOOKORBIT_PASSWORD=...

All calls use stdlib urllib (no extra deps). Every public function returns a
safe empty/None value on any error and logs at debug/info — a BookOrbit outage
must never break the knowledge-only fallback in server.py.
"""

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────
# Base URL WITHOUT the /api/v1 suffix (we append it). Empty = disabled.
BOOKORBIT_URL = os.environ.get("MARGINALIA_BOOKORBIT_URL", "").rstrip("/")
BOOKORBIT_USER = os.environ.get("MARGINALIA_BOOKORBIT_USER", "")
BOOKORBIT_PASSWORD = os.environ.get("MARGINALIA_BOOKORBIT_PASSWORD", "")
# Where downloaded EPUBs are cached (keyed by BookOrbit file id).
CACHE_DIR = Path(os.path.expanduser(
    os.environ.get("MARGINALIA_BOOKORBIT_CACHE", "~/.marginalia/bookorbit-cache")
))
_HTTP_TIMEOUT = int(os.environ.get("MARGINALIA_BOOKORBIT_TIMEOUT", "60"))

# Reuse book_finder's normalisation + scoring so match behaviour is identical
# across sources. Imported lazily inside functions to avoid an import cycle at
# module load (book_finder does not import this module, but keep it defensive).


def is_enabled() -> bool:
    """True when BookOrbit is configured. Cheap; call before any network work."""
    return bool(BOOKORBIT_URL and BOOKORBIT_USER and BOOKORBIT_PASSWORD)


def _author_name(a) -> str:
    """BookOrbit returns authors as plain strings (search) or objects with a
    'name'/'sortName' (book detail). Normalise either shape to a display string."""
    if isinstance(a, dict):
        return a.get("name") or a.get("sortName") or ""
    return str(a or "")


# ── Token cache ─────────────────────────────────────────────────────────────
_token: str | None = None
_token_ts: float = 0.0
# JWT lifetime is generous; cache aggressively. BookOrbit throttles /auth/login
# on a ~60s rolling window, so re-logging in per-book during a batch trips a 429
# and blocks new tokens. A long TTL means one login serves an entire warm run.
_TOKEN_TTL = 6 * 60 * 60  # 6h


def _api(path: str) -> str:
    return f"{BOOKORBIT_URL}/api/v1{path}"


def _request(path: str, *, method: str = "GET", token: str | None = None,
             body: dict | None = None, raw: bool = False):
    """Single HTTP call. Returns parsed JSON (raw=False) or bytes (raw=True).
    Raises urllib errors — callers wrap in try/except."""
    url = _api(path)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        payload = resp.read()
    return payload if raw else json.loads(payload)


def _login() -> str | None:
    """Return a valid JWT, using a long-lived cache. Retries on 429 (BookOrbit
    throttles /auth/login on a ~60s window). None only on hard failure."""
    global _token, _token_ts
    if _token and (time.time() - _token_ts) < _TOKEN_TTL:
        return _token
    for attempt in range(4):
        try:
            resp = _request("/auth/login", method="POST",
                            body={"username": BOOKORBIT_USER, "password": BOOKORBIT_PASSWORD})
            tok = resp.get("accessToken") or resp.get("access_token")
            if not tok:
                logger.warning("bookorbit: login response had no accessToken")
                return None
            _token, _token_ts = tok, time.time()
            logger.info("bookorbit: authenticated as %s", BOOKORBIT_USER)
            return tok
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                # Honor Retry-After if present, else wait out the throttle window.
                wait = 0
                try:
                    wait = int(e.headers.get("Retry-After", "0"))
                except (TypeError, ValueError):
                    wait = 0
                wait = wait or 20 * (attempt + 1)
                logger.warning("bookorbit: login rate-limited (429), waiting %ss", wait)
                time.sleep(wait)
                continue
            logger.warning("bookorbit: login failed: HTTP %s", e.code)
            return None
        except Exception as e:
            if attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            logger.warning("bookorbit: login failed: %s", e)
            return None
    return None


# ── Search + download ───────────────────────────────────────────────────────

def _search(title: str, token: str) -> list[dict]:
    """Return raw search hits for a title query. Retries on 429 / transient
    errors (BookOrbit's /books/search is rate-throttled under batch load and
    would otherwise return [] → a spurious 'no match')."""
    q = urllib.parse.quote(title)
    for attempt in range(4):
        try:
            return _request(f"/books/search?q={q}", token=token) or []
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                wait = 0
                try:
                    wait = int(e.headers.get("Retry-After", "0"))
                except (TypeError, ValueError):
                    wait = 0
                wait = wait or 15 * (attempt + 1)
                logger.warning("bookorbit: search rate-limited (429) for %r, waiting %ss", title, wait)
                time.sleep(wait)
                continue
            logger.debug("bookorbit: search HTTP %s for %r", e.code, title)
            return []
        except Exception as e:
            if attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            logger.debug("bookorbit: search failed for %r: %s", title, e)
            return []
    return []


def _best_match(hits: list[dict], title: str, author: str) -> tuple[dict, float] | None:
    """Score search hits with book_finder's helpers; return (hit, score) or None."""
    from book_finder import _norm_title, _norm_author, _title_score, _author_score

    norm_t = _norm_title(title)
    norm_a = _norm_author(author) if author else ""
    best: tuple[dict, float] | None = None
    for h in hits:
        # BookOrbit only serves EPUB text; skip audio/comic-only entries.
        formats = [str(f).lower() for f in (h.get("formats") or [])]
        if "epub" not in formats:
            continue
        ts = _title_score(norm_t, _norm_title(h.get("title", "")))
        if ts < 0.5:
            continue
        authors = h.get("authors") or []
        h_author = _author_name(authors[0]) if authors else ""
        author_weight = 0.35 if norm_a else 0.0
        as_ = _author_score(norm_a, _norm_author(h_author)) if norm_a else 0.0
        score = ts * (1.0 - author_weight) + as_ * author_weight
        if score < 0.55:
            continue
        if best is None or score > best[1]:
            best = (h, score)
    if best and best[1] >= 0.60:
        return best
    return None


def _primary_epub_file(book_id: int, token: str) -> dict | None:
    """Fetch book detail and return the primary EPUB file dict {id, filename, ...}."""
    try:
        detail = _request(f"/books/{book_id}", token=token)
    except Exception as e:
        logger.debug("bookorbit: detail fetch failed for book %s: %s", book_id, e)
        return None
    files = detail.get("files") or []
    epubs = [f for f in files if str(f.get("format", "")).lower() == "epub"]
    if not epubs:
        return None
    # Prefer the file flagged role=primary, else the first EPUB.
    epubs.sort(key=lambda f: 0 if f.get("role") == "primary" else 1)
    return {"file": epubs[0], "detail": detail}


def _cache_path(file_id: int, filename: str) -> Path:
    safe = re.sub(r"[^\w.\- ]", "_", filename or f"{file_id}.epub")
    return CACHE_DIR / f"{file_id}__{safe}"


def _download_epub(file_id: int, dest: Path, token: str) -> bool:
    """Download a BookOrbit EPUB file to dest. Returns True on success."""
    try:
        data = _request(f"/books/files/{file_id}/download", token=token, raw=True)
    except Exception as e:
        logger.warning("bookorbit: download failed for file %s: %s", file_id, e)
        return False
    # Sanity check: EPUB is a ZIP (magic PK\x03\x04).
    if not data[:2] == b"PK":
        logger.warning("bookorbit: file %s is not a ZIP/EPUB (magic=%r)", file_id, data[:4])
        return False
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    logger.info("bookorbit: cached %d bytes -> %s", len(data), dest)
    return True


# ── Public API (book_finder-shaped) ──────────────────────────────────────────

def find_epub(title: str, author: str = "") -> dict | None:
    """
    Search BookOrbit, download the best-matching EPUB, and return a dict shaped
    like book_finder.find_epub(): {epub_path, calibre_id=None, title, author,
    series, series_index, score, source="bookorbit"}. None if BookOrbit is
    disabled, unreachable, or no confident match.
    """
    if not is_enabled():
        return None
    token = _login()
    if not token:
        return None

    hits = _search(title, token)
    if not hits and author:
        # Some titles index better with the author appended.
        hits = _search(f"{title} {author}", token)
    match = _best_match(hits, title, author)
    if not match:
        logger.info("bookorbit: no confident match for %r by %r", title, author)
        return None
    hit, score = match

    pf = _primary_epub_file(hit["id"], token)
    if not pf:
        logger.info("bookorbit: matched '%s' but it has no EPUB file", hit.get("title"))
        return None
    fmeta, detail = pf["file"], pf["detail"]
    file_id = fmeta["id"]

    dest = _cache_path(file_id, fmeta.get("filename", ""))
    if not dest.exists() or dest.stat().st_size == 0:
        if not _download_epub(file_id, dest, token):
            return None

    authors = detail.get("authors") or hit.get("authors") or []
    author_name = _author_name(authors[0]) if authors else author
    result = {
        "epub_path":    str(dest),
        "calibre_id":   None,
        "title":        detail.get("title") or hit.get("title", title),
        "author":       author_name,
        "series":       detail.get("seriesName") or hit.get("seriesName"),
        "series_index": detail.get("seriesIndex") or hit.get("seriesIndex"),
        "score":        round(score, 3),
        "source":       "bookorbit",
    }
    logger.info("bookorbit: matched '%s' by '%s' (score=%.2f, file_id=%d)",
                result["title"], result["author"], score, file_id)
    return result


def list_all() -> list[dict]:
    """
    Return every EPUB book known to BookOrbit (for index building / chat browse).
    Empty list if disabled or unreachable. Does NOT download files — only
    metadata (epub_path is omitted; callers that need text go through find_epub).
    """
    if not is_enabled():
        return []
    token = _login()
    if not token:
        return []
    # BookOrbit search with an empty-ish query returns nothing; page libraries.
    try:
        libs = _request("/libraries", token=token) or []
    except Exception as e:
        logger.debug("bookorbit: libraries fetch failed: %s", e)
        return []
    results: list[dict] = []
    # There is no documented bulk-list endpoint; use search with common wildcards
    # is unreliable. Instead rely on the caller having titles. For index building
    # we expose what search gives for a broad query per library name is not
    # meaningful, so we keep list_all conservative: return [] and let find_epub
    # handle on-demand lookups. (Bulk enumeration can be added if BookOrbit
    # exposes a paginated /books listing.)
    return results
