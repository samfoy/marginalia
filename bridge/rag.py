"""
rag.py — Position-bounded retrieval over book text for marginalia.

At Book Index generation time we chunk the EPUB, embed every chunk, and store
the vectors + chunk metadata in a sidecar alongside the Book Index cache:

    ~/.marginalia/cache/<hash>.rag.npy    — float16 matrix (n_chunks, dim), L2-normalized
    ~/.marginalia/cache/<hash>.rag.json   — {"model", "dim", "count", "chunks":[{chapter, position_pct, text}]}

At query time we embed the question, filter chunks to those at or before the
reader's current position (spoiler-safe BY CONSTRUCTION — future chunks are
never even considered), and return the top-k by cosine similarity.

This grounds /chat, /recap, /wiki, and /section answers in the actual prose the
reader has already seen, instead of relying only on the lossy Book Index timeline.

Three embedding backends are supported:
  - local: sentence-transformers (default, ~80MB, no API key, works offline)
  - openai: text-embedding-3-small (if MARGINALIA_OPENAI_API_KEY is set)
  - bedrock: Cohere embed-english-v3 via AWS Bedrock (original behaviour)

Auto-detection order: OpenAI key present → openai, sentence-transformers
installed → local, else bedrock.
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

logger = logging.getLogger("marginalia.rag")

# ── Config ──────────────────────────────────────────────────────────────────

EMBED_MODEL   = os.environ.get("MARGINALIA_EMBED_MODEL", "cohere.embed-english-v3")
CHUNK_CHARS   = int(os.environ.get("MARGINALIA_RAG_CHUNK_CHARS", "1600"))   # ~400 tokens
CHUNK_OVERLAP = int(os.environ.get("MARGINALIA_RAG_OVERLAP", "200"))
EMBED_BATCH   = int(os.environ.get("MARGINALIA_RAG_BATCH", "96"))           # Cohere max texts/call
EMBED_MAXLEN  = int(os.environ.get("MARGINALIA_RAG_MAXLEN", "2048"))        # Cohere hard per-text char limit
EMBED_WORKERS = int(os.environ.get("MARGINALIA_RAG_WORKERS", "4"))
DEFAULT_TOP_K = int(os.environ.get("MARGINALIA_RAG_TOP_K", "8"))

EMBED_BACKEND = os.environ.get("MARGINALIA_EMBED_BACKEND", "auto")
# "auto"    = prefer OpenAI if key available, else local sentence-transformers, else bedrock
# "local"   = always use sentence-transformers (no API key needed)
# "openai"  = always use openai text-embedding-3-small
# "bedrock" = always use Cohere via AWS Bedrock (original behaviour)

LOCAL_EMBED_MODEL = os.environ.get("MARGINALIA_LOCAL_EMBED_MODEL", "all-MiniLM-L6-v2")
# sentence-transformers model name. all-MiniLM-L6-v2 is ~80MB, fast, good quality.
# Alternative: "all-mpnet-base-v2" (420MB, higher quality)

CACHE_DIR = Path.home() / ".marginalia" / "cache"

_PARA_SPLIT = re.compile(r"\n\s*\n")


# ── Backend helpers ──────────────────────────────────────────────────────────

def _get_embed_backend() -> str:
    """Resolve 'auto' to a concrete backend name."""
    if EMBED_BACKEND != "auto":
        return EMBED_BACKEND
    # Auto-detect: OpenAI key → openai, else try local, else bedrock
    if os.environ.get("MARGINALIA_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        return "openai"
    try:
        import sentence_transformers  # noqa: F401
        return "local"
    except ImportError:
        return "bedrock"


def _embed_local(texts: list[str]) -> "np.ndarray":
    """Embed using local sentence-transformers (no API key needed)."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        raise RuntimeError(
            "sentence-transformers not installed — pip install sentence-transformers"
        )
    # Lazy-load and cache the model across calls
    if not hasattr(_embed_local, "_model") or _embed_local._model_name != LOCAL_EMBED_MODEL:
        logger.info("rag: loading local embed model %s", LOCAL_EMBED_MODEL)
        _embed_local._model = SentenceTransformer(LOCAL_EMBED_MODEL)
        _embed_local._model_name = LOCAL_EMBED_MODEL
    vecs = _embed_local._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return np.array(vecs, dtype=np.float16)


def _embed_openai(texts: list[str]) -> "np.ndarray":
    """Embed using OpenAI text-embedding-3-small."""
    try:
        import openai
    except ImportError:
        raise RuntimeError("openai package not installed — pip install openai")
    api_key = os.environ.get("MARGINALIA_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("MARGINALIA_OPENAI_API_KEY not set")
    client = openai.OpenAI(api_key=api_key)
    # If EMBED_MODEL was set to a Cohere model ID, fall back to the OpenAI default
    model = EMBED_MODEL if not EMBED_MODEL.startswith("cohere.") else "text-embedding-3-small"
    resp = client.embeddings.create(input=texts, model=model)
    vecs = np.array([e.embedding for e in resp.data], dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    vecs = (vecs / np.maximum(norms, 1e-8)).astype(np.float16)
    return vecs


# ── Bedrock embedding client (reuse xray_generator's cached client) ──────────

def _embed_client():
    # Reuse the single cached bedrock-runtime client from xray_generator so we
    # don't open a second boto session.
    from xray_generator import _client
    return _client()


def _embed_bedrock(texts: list[str], input_type: str = "search_document") -> "np.ndarray":
    """Embed a single batch (≤ EMBED_BATCH texts) via Cohere on Bedrock."""
    # Cohere enforces a hard 2048-char limit per text. Truncate defensively;
    # the lost tail is covered by the next chunk's overlap.
    texts = [t[:EMBED_MAXLEN] for t in texts]
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
    return np.array(embs, dtype=np.float16)


def _embed_batch(texts: list[str], input_type: str = "search_document") -> "np.ndarray":
    """Route to the configured embedding backend.

    `input_type` is forwarded to the Bedrock/Cohere backend (search_document
    vs search_query); local and OpenAI backends ignore it.
    """
    backend = _get_embed_backend()
    logger.debug("rag: embedding %d texts via %s", len(texts), backend)
    if backend == "local":
        return _embed_local(texts)
    elif backend == "openai":
        return _embed_openai(texts)
    else:
        return _embed_bedrock(texts, input_type)


def _embed_texts(texts: list[str], input_type: str = "search_document") -> np.ndarray:
    """Embed many texts (batched + parallel). Returns L2-normalized float32 matrix."""
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)

    batches = [texts[i:i + EMBED_BATCH] for i in range(0, len(texts), EMBED_BATCH)]
    results: list = [None] * len(batches)

    def run(i: int, batch: list[str]):
        arr = _embed_batch(batch, input_type)
        # Normalise each backend's output to float32 for consistent scoring
        results[i] = arr.astype(np.float32)

    with ThreadPoolExecutor(max_workers=EMBED_WORKERS) as ex:
        futs = [ex.submit(run, i, b) for i, b in enumerate(batches)]
        for f in futs:
            f.result()

    mat = np.vstack(results)
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

    backend = _get_embed_backend()
    model_tag = LOCAL_EMBED_MODEL if backend == "local" else (
        "text-embedding-3-small" if backend == "openai" else EMBED_MODEL
    )
    logger.info("rag: embedding %d chunks for %s via %s (%s)",
                len(chunks), book_hash, backend, model_tag)
    mat = _embed_texts([c["text"] for c in chunks], "search_document")
    npy, meta = _paths(book_hash)

    np.save(npy, mat.astype(np.float16))
    meta.write_text(json.dumps({
        "model":   model_tag,
        "backend": backend,
        "dim":     int(mat.shape[1]) if mat.size else 0,
        "count":   len(chunks),
        "chunks":  chunks,
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

def _embed_query(query: str):
    """Embed a query once. Returns a normalized vector or None."""
    qv = _embed_texts([query], "search_query")
    return qv[0] if qv.size else None


def _retrieve_vec(book_hash: str, qv, reading_pct: float | None,
                  k: int) -> list[dict]:
    """Score one book's chunks against a pre-embedded query vector."""
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


def retrieve(book_hash: str, query: str, reading_pct: float | None,
             k: int = DEFAULT_TOP_K) -> list[dict]:
    """Return up to k chunks most relevant to `query`, bounded to reading_pct.

    Chunks whose position_pct is past the reader are never considered — spoiler
    safety is structural, not prompt-based. Each result: {chapter, position_pct,
    text, score}.
    """
    qv = _embed_query(query)
    if qv is None:
        return []
    return _retrieve_vec(book_hash, qv, reading_pct, k)


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


def retrieve_series(scope: list[dict], query: str,
                    k: int = DEFAULT_TOP_K) -> list[dict]:
    """Retrieve across a series reading scope, merging by score.

    `scope` is a list of {hash, title, series_index, max_pct} (see
    series.build_scope). Each book is queried bounded to its own max_pct, so
    spoiler safety holds per-book. Results are tagged with the source book.
    """
    qv = _embed_query(query)
    if qv is None:
        return []
    all_hits: list[dict] = []
    for s in scope:
        h = s.get("hash")
        if not h or not has_index(h):
            continue
        hits = _retrieve_vec(h, qv, s.get("max_pct"), k)
        for hit in hits:
            hit["book"] = s.get("title", "")
            hit["series_index"] = s.get("series_index")
            all_hits.append(hit)
    all_hits.sort(key=lambda x: -x.get("score", 0))
    return all_hits[:k]


def context_block(results: list[dict], max_chars: int = 6000) -> str:
    """Format retrieved chunks into a labeled context block for an LLM prompt."""
    parts: list[str] = []
    total = 0
    for r in results:
        book = r.get("book")
        book_tag = (book + " · ") if book else ""
        label = f'[{book_tag}{r.get("chapter") or "?"} · {int(r.get("position_pct", 0))}%]'
        snippet = r.get("text", "").strip()
        piece = f"{label}\n{snippet}"
        if total + len(piece) > max_chars:
            break
        parts.append(piece)
        total += len(piece)
    return "\n\n".join(parts)
