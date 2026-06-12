"""
rag.py — Position-bounded retrieval over book text for piread.

At X-Ray generation time we chunk the EPUB, embed every chunk with Cohere
(via Bedrock), and store the vectors + chunk metadata in a sidecar alongside
the X-Ray cache:

    ~/.piread/cache/<hash>.rag.npy    — float16 matrix (n_chunks, dim), L2-normalized
    ~/.piread/cache/<hash>.rag.json   — {"model", "dim", "count", "chunks":[{chapter, position_pct, text}]}

At query time we embed the question, filter chunks to those at or before the
reader's current position (spoiler-safe BY CONSTRUCTION — future chunks are
never even considered), and return the top-k by cosine similarity.

This grounds /chat, /recap, /wiki, and /section answers in the actual prose the
reader has already seen, instead of relying only on the lossy X-Ray timeline.

Embeddings go through Bedrock (Cohere embed-english-v3) — no heavy local model
dependency, works on Python 3.14, uses the same AWS profile as everything else.
"""

import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from epub_extract import EpubContent, Chapter

logger = logging.getLogger("piread.rag")

# ── Config ──────────────────────────────────────────────────────────────────

EMBED_MODEL   = os.environ.get("PIREAD_EMBED_MODEL", "cohere.embed-english-v3")
CHUNK_CHARS   = int(os.environ.get("PIREAD_RAG_CHUNK_CHARS", "1600"))   # ~400 tokens
CHUNK_OVERLAP = int(os.environ.get("PIREAD_RAG_OVERLAP", "200"))
EMBED_BATCH   = int(os.environ.get("PIREAD_RAG_BATCH", "96"))           # Cohere max texts/call
EMBED_WORKERS = int(os.environ.get("PIREAD_RAG_WORKERS", "4"))
DEFAULT_TOP_K = int(os.environ.get("PIREAD_RAG_TOP_K", "8"))

CACHE_DIR = Path.home() / ".piread" / "cache"

_PARA_SPLIT = re.compile(r"\n\s*\n")


# ── Bedrock embedding client (reuse xray_generator's cached client) ──────────

def _embed_client():
    # Reuse the single cached bedrock-runtime client from xray_generator so we
    # don't open a second boto session.
    from xray_generator import _client
    return _client()


def _embed_batch(texts: list[str], input_type: str) -> list[list[float]]:
    """Embed a single batch (≤ EMBED_BATCH texts) via Cohere on Bedrock."""
    body = json.dumps({"texts": texts, "input_type": input_type})
    resp = _embed_client().invoke_model(
        modelId=EMBED_MODEL,
        body=body,
        contentType="application/json",
        accept="application/json",
    )
    data = json.loads(resp["body"].read())
    embs = data.get("embeddings")
    # Cohere v3 returns a plain list of vectors; v4/embedding_types returns {"float": [...]}
    if isinstance(embs, dict):
        embs = embs.get("float") or next(iter(embs.values()))
    return embs


def _embed_texts(texts: list[str], input_type: str) -> np.ndarray:
    """Embed many texts (batched + parallel). Returns L2-normalized float32 matrix."""
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)

    batches = [texts[i:i + EMBED_BATCH] for i in range(0, len(texts), EMBED_BATCH)]
    results: list[list[list[float]]] = [None] * len(batches)  # type: ignore

    def run(i: int, batch: list[str]):
        results[i] = _embed_batch(batch, input_type)

    with ThreadPoolExecutor(max_workers=EMBED_WORKERS) as ex:
        futs = [ex.submit(run, i, b) for i, b in enumerate(batches)]
        for f in futs:
            f.result()

    vecs = [v for batch in results for v in batch]
    mat = np.asarray(vecs, dtype=np.float32)
    # L2-normalize so dot product == cosine similarity
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


# ── Chunking ─────────────────────────────────────────────────────────────────

def _chapter_span(chapters: list[Chapter], i: int) -> tuple[float, float]:
    """(start_pct, end_pct) for chapter i, using the next chapter's start as end."""
    start = float(chapters[i].position_pct)
    end = float(chapters[i + 1].position_pct) if i + 1 < len(chapters) else 100.0
    if end < start:
        end = start
    return start, end


def _split_text(text: str, size: int, overlap: int) -> list[tuple[int, str]]:
    """Split text into ~size-char windows on paragraph boundaries with overlap.

    Returns (char_offset, chunk_text) pairs. char_offset is the start offset of
    the chunk within `text` (used to interpolate position_pct within a chapter).
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [(0, text)]

    paras = _PARA_SPLIT.split(text)
    chunks: list[tuple[int, str]] = []
    buf: list[str] = []
    buf_len = 0
    buf_start = 0
    cursor = 0  # running char offset into `text`

    def flush(start_off: int):
        if buf:
            chunks.append((start_off, "\n\n".join(buf).strip()))

    for para in paras:
        plen = len(para) + 2  # +2 for the paragraph separator
        if buf and buf_len + plen > size:
            flush(buf_start)
            # Start next buffer with a tail-overlap of the previous text
            joined = "\n\n".join(buf)
            tail = joined[-overlap:] if overlap > 0 else ""
            buf = [tail, para] if tail else [para]
            buf_len = len(tail) + plen
            buf_start = max(0, cursor - len(tail))
        else:
            if not buf:
                buf_start = cursor
            buf.append(para)
            buf_len += plen
        cursor += plen

    flush(buf_start)

    # A single paragraph longer than `size` — hard-split it.
    final: list[tuple[int, str]] = []
    for off, c in chunks:
        if len(c) <= size * 1.5:
            final.append((off, c))
        else:
            step = size - overlap
            for s in range(0, len(c), step):
                final.append((off + s, c[s:s + size]))
    return final


def chunk_content(content: EpubContent) -> list[dict]:
    """Chunk a book into position-tagged windows for retrieval."""
    chunks: list[dict] = []
    chapters = content.chapters or []
    for i, ch in enumerate(chapters):
        start_pct, end_pct = _chapter_span(chapters, i)
        span = max(1.0, end_pct - start_pct)
        ch_len = max(1, len(ch.text))
        for off, ctext in _split_text(ch.text, CHUNK_CHARS, CHUNK_OVERLAP):
            if not ctext.strip():
                continue
            pos = start_pct + (off / ch_len) * span
            chunks.append({
                "chapter":      ch.title,
                "position_pct": round(min(max(pos, 0.0), 100.0), 2),
                "text":         ctext,
            })
    return chunks


# ── Sidecar persistence ──────────────────────────────────────────────────────

def _paths(book_hash: str) -> tuple[Path, Path]:
    return (CACHE_DIR / f"{book_hash}.rag.npy",
            CACHE_DIR / f"{book_hash}.rag.json")


def has_index(book_hash: str) -> bool:
    npy, meta = _paths(book_hash)
    return npy.exists() and meta.exists()


def build_index(content: EpubContent, book_hash: str) -> int:
    """Chunk → embed → persist sidecar. Returns chunk count. Idempotent overwrite."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    chunks = chunk_content(content)
    if not chunks:
        logger.warning("rag: no chunks for %s", book_hash)
        return 0

    logger.info("rag: embedding %d chunks for %s (%s)", len(chunks), book_hash, EMBED_MODEL)
    mat = _embed_texts([c["text"] for c in chunks], "search_document")
    npy, meta = _paths(book_hash)

    np.save(npy, mat.astype(np.float16))
    meta.write_text(json.dumps({
        "model": EMBED_MODEL,
        "dim":   int(mat.shape[1]) if mat.size else 0,
        "count": len(chunks),
        "chunks": chunks,
    }, ensure_ascii=False), encoding="utf-8")
    logger.info("rag: index built for %s — %d chunks, dim=%d",
                book_hash, len(chunks), mat.shape[1] if mat.size else 0)
    return len(chunks)


def _load_index(book_hash: str):
    npy, meta = _paths(book_hash)
    if not (npy.exists() and meta.exists()):
        return None, None
    try:
        mat = np.load(npy).astype(np.float32)
        info = json.loads(meta.read_text(encoding="utf-8"))
        return mat, info
    except Exception as e:
        logger.warning("rag: failed to load index %s: %s", book_hash, e)
        return None, None


# ── Retrieval ────────────────────────────────────────────────────────────────

def retrieve(book_hash: str, query: str, reading_pct: float | None,
             k: int = DEFAULT_TOP_K) -> list[dict]:
    """Return up to k chunks most relevant to `query`, bounded to reading_pct.

    Chunks whose position_pct is past the reader are never considered — spoiler
    safety is structural, not prompt-based. Each result: {chapter, position_pct,
    text, score}.
    """
    mat, info = _load_index(book_hash)
    if mat is None or info is None or mat.size == 0:
        return []

    chunks = info["chunks"]
    bound = 100.0 if (reading_pct is None or reading_pct <= 0) else float(reading_pct)
    eligible = [i for i, c in enumerate(chunks) if c.get("position_pct", 0) <= bound]
    if not eligible:
        # Reader is right at the very start — allow the opening chunks.
        eligible = [i for i, c in enumerate(chunks) if c.get("position_pct", 0) <= 5]
    if not eligible:
        return []

    qv = _embed_texts([query], "search_query")
    if qv.size == 0:
        return []
    qv = qv[0]

    sub = mat[eligible]
    scores = sub @ qv
    order = np.argsort(-scores)[:k]

    out: list[dict] = []
    for j in order:
        idx = eligible[int(j)]
        c = chunks[idx]
        out.append({
            "chapter":      c.get("chapter", ""),
            "position_pct": c.get("position_pct", 0),
            "text":         c.get("text", ""),
            "score":        float(scores[int(j)]),
        })
    return out


def section_chunks(book_hash: str, start_pct: float, end_pct: float,
                   max_chars: int = 7000) -> list[dict]:
    """Return chunks whose position falls in [start_pct, end_pct], in reading order.

    Used by Section X-Ray to analyze one chapter/part. No scoring — a section is
    small enough to pass through whole (capped at max_chars).
    """
    _, info = _load_index(book_hash)
    if info is None:
        return []
    out: list[dict] = []
    total = 0
    for c in info["chunks"]:
        pos = c.get("position_pct", 0)
        if start_pct <= pos <= end_pct:
            t = c.get("text", "")
            if total + len(t) > max_chars:
                break
            out.append({"chapter": c.get("chapter", ""), "position_pct": pos, "text": t})
            total += len(t)
    return out


def context_block(results: list[dict], max_chars: int = 6000) -> str:
    """Format retrieved chunks into a labeled context block for an LLM prompt."""
    parts: list[str] = []
    total = 0
    for r in results:
        label = f'[{r.get("chapter") or "?"} · {int(r.get("position_pct", 0))}%]'
        snippet = r.get("text", "").strip()
        piece = f"{label}\n{snippet}"
        if total + len(piece) > max_chars:
            break
        parts.append(piece)
        total += len(piece)
    return "\n\n".join(parts)
