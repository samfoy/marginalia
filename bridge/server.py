#!/usr/bin/env python3
"""
marginalia — KOReader reading intelligence bridge.

Routes:
  GET  /ping                       health check → "pong"
  GET  /index                      Book Index cache index
  GET  /book-index/status/<job_id> poll a background Book Index generation job
  POST /ask                        conversational query (explain/translate/summarize)
  POST /note                       save highlighted passage + context to Obsidian vault
  POST /book-index/init            find book in Calibre, generate Book Index, cache it
  POST /book-index/progress        update reading position for a cached book

  GET  /monitor               live request-monitor dashboard (HTML)
  GET  /monitor/data          monitor snapshot (JSON, polled by the dashboard)

Config via environment variables (all optional):
  MARGINALIA_PORT         TCP port to listen on           (default: 7731)
  MARGINALIA_AWS_PROFILE  AWS credentials profile          (default: "" — required for Bedrock)
  MARGINALIA_AWS_REGION   Bedrock region                   (default: us-west-2)
  MARGINALIA_MODEL_ID     Model for /ask queries           (default: gpt-4o)
  MARGINALIA_TOKEN        Shared secret (empty = no auth)  (default: "")
  MARGINALIA_MAX_TOKENS   Max tokens for /ask responses    (default: 600)
  MARGINALIA_VAULT        Obsidian vault root              (default: ~/Documents)
  MARGINALIA_OPENAI_API_KEY    OpenAI API key for direct OpenAI models   (default: "")
  MARGINALIA_ANTHROPIC_API_KEY Anthropic API key for direct Anthropic    (default: "")
"""

import io
import json
import logging
import os
import re
import signal
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from book_finder import find_epub
from epub_extract import extract_epub
from xray_generator import generate, build_record
import xray_cache
import mentions
import rag
import series
import monitor

# ── Config ────────────────────────────────────────────────────────────────────

PORT       = int(os.environ.get("MARGINALIA_PORT", 7731))
PROFILE    = os.environ.get("MARGINALIA_AWS_PROFILE", "")
# server.py uses Sonnet for /ask and knowledge-only X-Ray — never GPT (no Bedrock invoke_model support)
MODEL_ID   = os.environ.get("MARGINALIA_MODEL_ID", "openai.gpt-5.5")
TOKEN      = os.environ.get("MARGINALIA_TOKEN", "")
MAX_TOKENS = int(os.environ.get("MARGINALIA_MAX_TOKENS", 600))
VAULT_ROOT = os.path.expanduser(os.environ.get("MARGINALIA_VAULT", "~/Documents"))
BOOKS_DIR  = os.path.join(VAULT_ROOT, "Notes", "Books")
# Reasoning effort for interactive companion calls.
COMPANION_EFFORT = os.environ.get("MARGINALIA_COMPANION_EFFORT", "low")

# ── System prompts per mode ───────────────────────────────────────────────────

SYSTEM_PROMPTS: dict[str, str] = {
    "whois": (
        "You are a reading assistant embedded in KOReader. "
        "The user selected a name or term they want identified. "
        "Explain who or what it is within the context of the book shown. "
        "Be concise (2–4 sentences). "
        "Do NOT reveal future plot events. "
        "Plain text only — no markdown."
    ),
    "explain": (
        "You are a reading assistant embedded in KOReader. "
        "The user wants a passage explained. "
        "Clarify difficult vocabulary, literary devices, historical references, "
        "or technical terms as needed. "
        "2–5 sentences. Plain text only — no markdown."
    ),
    "summarize": (
        "You are a reading assistant embedded in KOReader. "
        "The user wants to know the story context at this point in the book. "
        "Based on the passage and book info provided, briefly describe what has "
        "happened in the story up to this moment — who the main characters are "
        "and what situation they are in. "
        "3–6 sentences. Do NOT spoil future events. Plain text only — no markdown."
    ),
    "translate": (
        "You are a reading assistant embedded in KOReader. "
        "Translate the selected text into natural, readable English. "
        "If the text is already in English, note that and offer a plain-language "
        "paraphrase of any difficult sections. "
        "Plain text only — no markdown."
    ),
}

DEFAULT_SYSTEM = (
    "You are a helpful reading assistant embedded in KOReader. "
    "Answer the user's question about the selected text concisely. "
    "Plain text only — no markdown. Keep responses under 250 words."
)

# Spoiler-bounded companion prompts (recap / AI Wiki / section) — all grounded
# in retrieved excerpts that come ONLY from before the reader's position.
RECAP_INSTRUCTIONS = (
    "You are a reading companion. The reader is returning to a book after a break. "
    "Using ONLY the provided excerpts and events — all from BEFORE their current "
    "position (which may include earlier books in the same series that the reader "
    "has already finished) — write a brief 'where you left off' recap: the immediate "
    "situation, who is involved, and the most recent significant developments. If the "
    "reader is early in a sequel, briefly bridge from how the previous book ended. "
    "5–8 sentences, plain prose, no markdown. Do not state anything not supported "
    "by the excerpts, and never reference events past the reader's position."
)
WIKI_INSTRUCTIONS = (
    "You are a reading companion writing a spoiler-safe encyclopedia entry about a "
    "specific person, place, term, or reference from a book, bounded to what the "
    "reader has seen so far. Use ONLY the provided excerpts (all from before the "
    "reader's current position, which may span earlier books in the same series "
    "the reader has finished). Cover who/what it is, why it matters, and key "
    "relationships or moments SO FAR, drawing the through-line across books when the "
    "excerpts support it. 5–10 sentences, plain prose, no markdown. Do not reveal or "
    "hint at anything beyond the reader's position. If little is known yet, say so."
)
SECTION_INSTRUCTIONS = (
    "You are a reading companion analyzing one chapter/section the reader has just "
    "finished. Using ONLY the provided excerpts from that section, explain what "
    "matters: the key events, who appears, important reveals, and what to keep in "
    "mind going forward. Be concrete and specific. 5–9 sentences, plain prose, no "
    "markdown. Do not reference anything outside this section or past the reader's "
    "position."
)
CHAT_INSTRUCTIONS = (
    "You are Pi, a reading companion inside KOReader. The reader asks questions "
    "about the book they are currently reading. Answer concisely (3–5 sentences) "
    "using the provided book context and excerpts. Never reveal or hint at events "
    "past the reader's current position. Plain prose, no markdown."
)

# ── Bedrock client ────────────────────────────────────────────────────────────

def ask_claude(text: str, context: str | None, book_title: str | None,
               book_author: str | None, mode: str) -> str:
    from xray_generator import _complete
    system = SYSTEM_PROMPTS.get(mode, DEFAULT_SYSTEM)

    parts: list[str] = []
    if book_title:
        line = f'Book: "{book_title}"'
        if book_author:
            line += f" by {book_author}"
        parts.append(line)
    if context:
        parts.append(f"Surrounding passage:\n{context}")
    parts.append(f"Selected text: {text}")
    user_message = "\n\n".join(parts)

    # Model fallback chain (gpt-5.5 → gpt-5.4 → Sonnet) handles outages/empties.
    raw = _complete(user_message, instructions=system,
                    reasoning_effort=COMPANION_EFFORT)
    return raw.strip()


# ── X-Ray generation job registry ─────────────────────────────────────────────
# ── Obsidian vault note saving ─────────────────────────────────────────────────

def _save_vault_note(
    highlight: str, context: str,
    book_title: str, book_author: str, reading_pct: float,
    query: str | None = None, response: str | None = None,
    mode: str | None = None, source: str | None = None,
) -> str:
    """
    Append a highlight + optional context to the book's Obsidian vault note.
    File: BOOKS_DIR/<Author> - <Title>.md
    Creates the file with frontmatter if it doesn't exist.

    When `response` is provided (a captured Pi lookup), the entry also records
    what was asked and Pi's answer, labelled by source/mode.
    Returns the absolute path written.
    """
    from datetime import datetime

    os.makedirs(BOOKS_DIR, exist_ok=True)

    # Sanitise filename
    def safe(s: str) -> str:
        return re.sub(r'[\\/:*?"<>|]', '', s).strip()

    filename = (f"{safe(book_author)} - {safe(book_title)}.md"
                if book_author else f"{safe(book_title)}.md")
    filepath = os.path.join(BOOKS_DIR, filename)

    # Build bullet. A multi-line value (Pi's prose answer) is indented so it
    # stays part of the Markdown list item.
    def _block(prefix: str, text: str) -> list[str]:
        parts = text.strip().split("\n")
        out = [f"  {prefix}{parts[0]}"]
        out += [f"  {ln}" if ln.strip() else "" for ln in parts[1:]]
        return out

    date_str  = datetime.now().strftime("%Y-%m-%d")
    pct_tag   = f" ({int(reading_pct)}%)" if reading_pct else ""
    label     = ""
    label_bits = [b for b in (source, mode) if b]
    if label_bits:
        label = " — " + " · ".join(label_bits)
    lines     = [f"- {date_str}{pct_tag}{label}:"]
    if highlight:
        lines.append(f"  > {highlight}")
    if context:
        lines += [""] + _block("", context)
    # Only echo the query if it differs from the highlighted text (avoids dupes).
    if query and query.strip() and query.strip() != (highlight or "").strip():
        lines += [""] + _block("**Asked:** ", query)
    if response and response.strip():
        lines += [""] + _block("**AI:** ", response)
    bullet = "\n".join(lines)

    # Read or create
    if os.path.exists(filepath):
        with open(filepath, encoding="utf-8") as f:
            content = f.read()
    else:
        content = (
            f"---\ntitle: \"{book_title}\"\nauthor: \"{book_author}\"\n"
            f"tags:\n  - book\n---\n\n# {book_title}\n\n"
            f"**Author:** {book_author}\n\n## Notes\n\n"
        )
        logging.info("vault note: created %s", filepath)

    # Append under ## Notes (before next ## if any, else EOF)
    if "## Notes" in content:
        notes_idx = content.find("## Notes") + 8
        m = re.search(r"\n## ", content[notes_idx:])
        at = (notes_idx + m.start()) if m else len(content)
        content = content[:at].rstrip() + "\n\n" + bullet + "\n" + content[at:]
    else:
        content = content.rstrip() + "\n\n## Notes\n\n" + bullet + "\n"

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    logging.info("vault note: saved to %s", filepath)
    return filepath


# job_id → {status, progress, record, error}
_xray_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _run_xray_job(job_id: str, title: str, author: str, reading_pct: float) -> None:
    """Background thread: find book, extract, generate, cache."""
    def update(status: str, **kw):
        with _jobs_lock:
            _xray_jobs[job_id].update({"status": status, **kw})

    try:
        update("finding", progress="Looking up book in Calibre")
        book_meta = find_epub(title, author)
        if not book_meta:
            # Author mismatch is common (EPUB metadata vs Calibre). Retry title-only.
            book_meta = find_epub(title, "")
        if not book_meta:
            # Fallback: generate from Claude's knowledge (no EPUB needed)
            logging.info("Book not in Calibre, using knowledge-only mode: %s", title)
            update("generating", progress=f"Generating Book Index from knowledge (no EPUB): {title}")
            _run_knowledge_xray_job(job_id, title, author)
            return

        update("extracting", progress="Extracting EPUB text")
        content = extract_epub(book_meta["epub_path"])

        update("generating",
               progress=f"Generating Book Index ({content.total_chars:,} chars)")

        # Authoritative series from Calibre metadata.db (EPUB tags are often stale).
        sv = series.resolve(calibre_id=book_meta.get("calibre_id"),
                            title=content.title, author=content.author)
        if sv:
            content.series = sv["series"]
            content.series_index = sv["series_index"]
            logging.info("series: resolved '%s' #%s for %s",
                         sv["series"], sv["series_index"], content.title)

        xray, strategy = generate(content)

        # Build the per-entity mention index (chapter distribution + jump-to).
        # Pure regex over chapter text — fast, no network.
        try:
            mention_idx = mentions.build_mentions(content, xray)
            mentions.add_mention_counts(xray, mention_idx)
        except Exception:
            logging.exception("mentions build failed (non-fatal)")
            mention_idx = {}

        record = build_record(content, book_meta, xray, strategy)
        record["mentions"] = mention_idx
        if reading_pct:
            record["last_reading_pct"] = reading_pct
        xray_cache.save(content.file_hash, record)

        # Build the retrieval index (embeddings sidecar) so /chat, /recap,
        # /wiki, /section can ground answers in the actual prose. Non-fatal.
        try:
            rag.build_index(content, content.file_hash)
        except Exception:
            logging.exception("rag index build failed (non-fatal)")

        update("ready", record=record, error=None)
        logging.info("Book Index job %s complete: %s", job_id, title)

    except Exception as exc:
        logging.exception("Book Index job %s failed", job_id)
        update("failed", error=str(exc))


def _run_knowledge_xray_job(job_id: str, title: str, author: str) -> None:
    """
    Background thread: generate Book Index from model knowledge (no EPUB).
    Uses xray_generator._call() so GPT-5.5 (primary) or Sonnet (fallback)
    handle it the same as EPUB-based generation.
    """
    import hashlib
    from datetime import datetime, timezone
    from xray_generator import _call, _parse, _normalize, _SCHEMA, _REFERENCE_RULES, _TIMELINE_RULES

    def update(status: str, **kw):
        with _jobs_lock:
            _xray_jobs[job_id].update({"status": status, **kw})

    try:
        update("generating", progress=f"Generating Book Index from knowledge: {title}")

        author_clause = f" by {author}" if author else ""
        header = f'Book: "{title}"{author_clause}'
        prompt = (
            header + "\n\n"
            "Generate a complete Book Index for this book from your training knowledge.\n"
            "Use your best estimates for first_appearance_pct and position_pct (0-100).\n\n"
            + _SCHEMA + "\n\n"
            + _REFERENCE_RULES + "\n\n"
            + _TIMELINE_RULES
        )

        raw  = _call(prompt)
        xray = _normalize(_parse(raw))

        book_hash = hashlib.md5(f"{title}|{author}|knowledge".encode()).hexdigest()
        record = {
            "version":      1,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "strategy":     "knowledge_only",
            "book": {
                "title": title, "author": author,
                "series": None, "series_index": None,
                "calibre_id": None, "epub_path": None,
                "epub_hash": book_hash,
                "total_chars": 0, "chapter_count": 0,
            },
            "xray": xray,
        }
        xray_cache.save(book_hash, record)
        update("ready", record=record, error=None)
        logging.info("Knowledge Book Index complete: %s (%d chars | %d themes | %d timeline)",
                     title, len(xray.get("characters", [])),
                     len(xray.get("themes", [])), len(xray.get("timeline", [])))

    except Exception as exc:
        logging.exception("Knowledge Book Index job %s failed", job_id)
        update("failed", error=str(exc))


# ── HTTP handler ──────────────────────────────────────────────────────────────

def _serve_xray(record: dict) -> dict:
    """Return a book's Book Index for serving, with prior-series-book entities merged
    in (so the Book Index browser shows characters carried over from earlier books).
    Injected entities are tagged source_label and never spoiler-gated."""
    import copy as _copy
    xray = record.get("xray", {})
    book = record.get("book", {})
    s, si = book.get("series"), book.get("series_index")
    if not (s and si and si > 1):
        return xray
    try:
        return series.inject_series_context(_copy.deepcopy(xray), s, si)
    except Exception:
        logging.exception("series inject failed (non-fatal)")
        return xray


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # redirect to Python logging
        logging.info("HTTP %s", fmt % args)

    def send_response(self, code, message=None):  # capture status for the monitor
        self._status = code
        super().send_response(code, message)

    # ── GET dispatch (with request monitoring) ──────────────────────────────────
    def do_GET(self):
        # Monitor pages are served directly and never self-tracked.
        if self.path == "/monitor":
            self._send(200, monitor.render_html().encode(), "text/html; charset=utf-8")
            return
        if self.path == "/monitor/data":
            data = monitor.snapshot()
            data["model"] = MODEL_ID
            data["effort"] = COMPANION_EFFORT
            data["books_cached"] = len(xray_cache.load_index().get("books", {}))
            self._send_json(200, data)
            return

        rec = monitor.begin("GET", self.path, monitor.detail_for_get(self.path)) \
            if monitor.should_track(self.path) else None
        try:
            self._dispatch_get()
        finally:
            if rec:
                monitor.end(rec, getattr(self, "_status", 200))

    def _dispatch_get(self):
        if self.path == "/ping":
            self._send(200, b"pong", "text/plain")
        elif self.path == "/index":
            # Pi chat uses this to browse the X-Ray cache
            index = xray_cache.load_index()
            self._send_json(200, index)
        elif self.path == "/v1/models":
            # KO Assistant probes this to verify the provider
            self._send_json(200, {
                "object": "list",
                "data": [{"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "bedrock"}]
            })
        elif self.path.startswith("/book-index/status/"):
            job_id = self.path.split("/book-index/status/", 1)[-1]
            with _jobs_lock:
                job = _xray_jobs.get(job_id)
            if not job:
                self.send_error(404, "Unknown job")
                return
            # Don't send the full record in the status poll — just metadata
            resp = {"status": job["status"],
                    "progress": job.get("progress", ""),
                    "error": job.get("error")}
            if job["status"] == "ready" and job.get("record"):
                resp["xray"] = _serve_xray(job["record"])
                resp["book"] = job["record"]["book"]
                resp["mentions"] = job["record"].get("mentions", {})
            self._send_json(200, resp)
        else:
            self.send_error(404)

    # ── POST dispatch (with request monitoring) ─────────────────────────────────
    def do_POST(self):
        # Read the body once so the monitor can label the request (which book /
        # entity / %); replay it to the handlers via an in-memory buffer.
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        original_rfile = self.rfile
        self.rfile = io.BytesIO(raw)

        rec = monitor.begin("POST", self.path, monitor.detail_for_post(self.path, raw)) \
            if monitor.should_track(self.path) else None
        try:
            self._dispatch_post()
        finally:
            self.rfile = original_rfile
            if rec:
                monitor.end(rec, getattr(self, "_status", 200))

    def _dispatch_post(self):
        if self.path == "/book-index/init":
            self._handle_xray_init()
            return
        if self.path == "/book-index/progress":
            self._handle_xray_progress()
            return
        if self.path == "/chat":
            self._handle_chat()
            return
        if self.path == "/recap":
            self._handle_recap()
            return
        if self.path == "/wiki":
            self._handle_wiki()
            return
        if self.path == "/section":
            self._handle_section()
            return
        if self.path == "/note":
            self._handle_note()
            return
        if self.path == "/v1/chat/completions":
            self._handle_openai_compat()
            return
        if self.path != "/ask":
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        try:
            req = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.send_error(400, f"Invalid JSON: {exc}")
            return

        # Token check (optional)
        if TOKEN and req.get("token") != TOKEN:
            self.send_error(403, "Forbidden")
            return

        text = (req.get("text") or "").strip()
        if not text:
            self.send_error(400, "Missing 'text'")
            return

        try:
            response_text = ask_claude(
                text=text,
                context=req.get("context"),
                book_title=req.get("book_title"),
                book_author=req.get("book_author"),
                mode=req.get("mode", "explain"),
            )
            payload = {"response": response_text, "error": None}
            self._send_json(200, payload)

        except (BotoCoreError, ClientError) as exc:
            logging.error("Bedrock error: %s", exc)
            self._send_json(500, {"response": None, "error": f"Bedrock: {exc}"})
        except Exception as exc:
            logging.exception("Unexpected error")
            self._send_json(500, {"response": None, "error": str(exc)})

    # ── helpers ────────────────────────────────────────────────────────────────
    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── /chat ─────────────────────────────────────────────────────────────────
    def _handle_chat(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON"); return

        question     = (req.get("question") or "").strip()
        book_title   = (req.get("book_title") or "").strip()
        book_author  = (req.get("book_author") or "").strip()
        reading_pct  = req.get("reading_pct") or 0
        xray_summary = (req.get("xray_summary") or "").strip()
        page_text    = (req.get("page_text") or "").strip()

        if not question:
            self.send_error(400, "Missing question"); return

        # Build book context block
        ctx_parts = []
        if book_title:
            line = f'Book: "{book_title}"'
            if book_author:
                line += f" by {book_author}"
            if reading_pct:
                line += f" ({reading_pct:.0f}% read)"
            ctx_parts.append(line)
        if xray_summary:
            ctx_parts.append(xray_summary)
        if page_text:
            ctx_parts.append(f"Current page text:\n{page_text}")

        # Ground the answer in actual prose the reader has already seen.
        rag_ctx = self._rag_context(book_title, book_author, question, reading_pct, k=6)
        if rag_ctx:
            ctx_parts.append(
                "Relevant passages from earlier in the book (already read):\n" + rag_ctx
            )
        book_context = "\n\n".join(ctx_parts)
        message = (book_context + "\n\nQuestion: " + question) if book_context else question

        try:
            response_text = self._gpt_companion(CHAT_INSTRUCTIONS, message)
            self._send_json(200, {"response": response_text, "error": None})
        except Exception as exc:
            logging.exception("/chat error")
            self._send_json(500, {"response": None, "error": str(exc)})

    # ── RAG helpers (position-bounded retrieval) ──────────────────────────────
    def _book_hash(self, title: str, author: str) -> str | None:
        rec = xray_cache.find_by_title_author(title, author)
        if not rec and author:
            rec = xray_cache.find_by_title_author(title, "")
        if rec:
            return rec.get("book", {}).get("epub_hash")
        return None

    def _rag_context(self, title: str, author: str, query: str,
                     reading_pct, k: int = 8, max_chars: int = 7000) -> str:
        """Series-aware position-bounded retrieval context.

        Pulls from the current book (≤ reading_pct) and every prior book in the
        series the reader has finished — never from future books or ahead in the
        current one.
        """
        try:
            rec = xray_cache.find_by_title_author(title, author)
            if not rec and author:
                rec = xray_cache.find_by_title_author(title, "")
            if not rec:
                return ""
            scope = series.build_scope(rec, float(reading_pct or 0))
            hits = rag.retrieve_series(scope, query, k=k)
            return rag.context_block(hits, max_chars=max_chars)
        except Exception:
            logging.exception("rag context lookup failed (non-fatal)")
            return ""

    def _gpt_companion(self, instructions: str, user_message: str) -> str:
        from xray_generator import _complete
        return _complete(user_message, instructions=instructions,
                         reasoning_effort=COMPANION_EFFORT).strip()

    # ── /recap — spoiler-bounded "where you left off" ─────────────────────────
    def _handle_recap(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON"); return

        title       = (req.get("book_title") or "").strip()
        author      = (req.get("book_author") or "").strip()
        reading_pct = float(req.get("reading_pct") or 0)
        if not title:
            self.send_error(400, "Missing book_title"); return

        parts = [f'Book: "{title}"' + (f" by {author}" if author else "")
                 + f" — reader is at {reading_pct:.0f}%."]

        # Timeline events the reader has reached (from cached X-Ray)
        rec = xray_cache.find_by_title_author(title, author) or \
              (xray_cache.find_by_title_author(title, "") if author else None)
        if rec:
            events = [e for e in rec.get("xray", {}).get("timeline", [])
                      if (e.get("position_pct") or 0) <= reading_pct]
            if events:
                recent = events[-10:]
                lines = [f"- {e.get('chapter','?')}: {e.get('event','')}" for e in recent]
                parts.append("Recent plot events (chronological):\n" + "\n".join(lines))

        rag_ctx = self._rag_context(
            title, author,
            "the most recent events, the current situation, and where the "
            "protagonist is right now",
            reading_pct, k=8, max_chars=7000)
        if rag_ctx:
            parts.append("Excerpts from the pages just read:\n" + rag_ctx)

        try:
            text = self._gpt_companion(RECAP_INSTRUCTIONS, "\n\n".join(parts))
            self._send_json(200, {"response": text, "error": None})
        except Exception as exc:
            logging.exception("/recap error")
            self._send_json(500, {"response": None, "error": str(exc)})

    # ── /wiki — AI Wiki deep-dive on one entity, bounded to position ──────────
    def _handle_wiki(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON"); return

        title       = (req.get("book_title") or "").strip()
        author      = (req.get("book_author") or "").strip()
        entity      = (req.get("entity_name") or "").strip()
        kind        = (req.get("entity_kind") or "").strip() or "subject"
        known       = (req.get("known") or "").strip()
        reading_pct = float(req.get("reading_pct") or 0)
        if not title or not entity:
            self.send_error(400, "Missing book_title or entity_name"); return

        parts = [f'Book: "{title}"' + (f" by {author}" if author else "")
                 + f" — reader is at {reading_pct:.0f}%.",
                 f"Write the entry about this {kind}: {entity}"]
        if known:
            parts.append(f"What the Book Index already notes: {known}")

        rag_ctx = self._rag_context(
            title, author,
            f"{entity} — who/what they are, their role, significance, and relationships",
            reading_pct, k=8, max_chars=7000)
        if rag_ctx:
            parts.append(f"Excerpts mentioning {entity} (already read):\n" + rag_ctx)

        try:
            text = self._gpt_companion(WIKI_INSTRUCTIONS, "\n\n".join(parts))
            self._send_json(200, {"response": text, "error": None})
        except Exception as exc:
            logging.exception("/wiki error")
            self._send_json(500, {"response": None, "error": str(exc)})

    # ── /section — Section X-Ray for one chapter/part ─────────────────────────
    def _handle_section(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON"); return

        title       = (req.get("book_title") or "").strip()
        author      = (req.get("book_author") or "").strip()
        chapter     = (req.get("chapter_title") or "").strip()
        start_pct   = float(req.get("start_pct") or 0)
        end_pct     = float(req.get("end_pct") or 100)
        if not title:
            self.send_error(400, "Missing book_title"); return

        h = self._book_hash(title, author)
        if not h or not rag.has_index(h):
            self._send_json(200, {"response": None,
                                  "error": "Section analysis needs the retrieval "
                                           "index — rebuild Book Index for this book."})
            return

        chunks = rag.section_chunks(h, start_pct, end_pct, max_chars=7000)
        if not chunks:
            self._send_json(200, {"response": None,
                                  "error": "No text found for this section."})
            return

        label = chapter or f"{start_pct:.0f}%–{end_pct:.0f}%"
        body = (f'Book: "{title}"' + (f" by {author}" if author else "")
                + f"\nSection: {label} ({start_pct:.0f}%–{end_pct:.0f}%)\n\n"
                + "Section text:\n" + rag.context_block(chunks, max_chars=7000))
        try:
            text = self._gpt_companion(SECTION_INSTRUCTIONS, body)
            self._send_json(200, {"response": text, "error": None})
        except Exception as exc:
            logging.exception("/section error")
            self._send_json(500, {"response": None, "error": str(exc)})

    # ── /xray/init ────────────────────────────────────────────────────────────
    def _handle_xray_init(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON"); return

        title  = (req.get("book_title") or "").strip()
        author = (req.get("book_author") or "").strip()
        reading_pct = float(req.get("reading_pct") or 0)
        device_generated_at = (req.get("device_generated_at") or "").strip()

        if not title:
            self.send_error(400, "Missing book_title"); return

        # ── Check cache first ──────────────────────────────────────────────────
        cached = xray_cache.find_by_title_author(title, author)
        if cached:
            logging.info("Book Index cache HIT: %s", title)
            if reading_pct:
                xray_cache.update_reading_pct(cached["book"]["epub_hash"], reading_pct)
            mac_generated_at = cached.get("generated_at", "")
            # If device already has this version, just confirm it's current
            if device_generated_at and device_generated_at >= mac_generated_at:
                self._send_json(200, {"status": "current"})
                return
            self._send_json(200, {"status": "ready", "cached": True,
                                   "xray": _serve_xray(cached), "book": cached["book"],
                                   "mentions": cached.get("mentions", {}),
                                   "generated_at": mac_generated_at})
            return

        # ── Start background generation job ─────────────────────────────────
        job_id = str(uuid.uuid4())[:8]
        with _jobs_lock:
            _xray_jobs[job_id] = {"status": "pending", "progress": "Starting",
                                   "record": None, "error": None}
        t = threading.Thread(
            target=_run_xray_job,
            args=(job_id, title, author, reading_pct),
            daemon=True,
        )
        t.start()
        logging.info("Book Index job %s started for '%s'", job_id, title)
        self._send_json(202, {"status": "generating", "job_id": job_id,
                               "poll_url": f"/book-index/status/{job_id}"})

    # ── /v1/chat/completions (OpenAI-compatible proxy for KO Assistant) ───────
    def _handle_openai_compat(self):
        """OpenAI-compatible endpoint so KO Assistant can use Bedrock via our bridge."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON"); return

        messages  = req.get("messages", [])
        model     = req.get("model") or MODEL_ID
        max_tok   = int(req.get("max_tokens") or MAX_TOKENS)

        # Split out system message (Bedrock takes it separately)
        system_parts = []
        bedrock_msgs = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if role == "system":
                system_parts.append(content if isinstance(content, str) else str(content))
            else:
                bedrock_msgs.append({"role": role, "content": content})

        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tok,
            "messages": bedrock_msgs,
        }
        if system_parts:
            body["system"] = " ".join(system_parts)

        try:
            from xray_generator import _client as bedrock_client
            resp   = bedrock_client().invoke_model(
                modelId=model,
                body=json.dumps(body),
                contentType="application/json",
                accept="application/json",
            )
            result = json.loads(resp["body"].read())
            text   = result["content"][0]["text"].strip()
            usage  = result.get("usage", {})
            openai_resp = {
                "id":      f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object":  "chat.completion",
                "created": int(time.time()),
                "model":   model,
                "choices": [{
                    "index":         0,
                    "message":       {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens":     usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens":      usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                },
            }
            self._send_json(200, openai_resp)
        except Exception as exc:
            logging.exception("OpenAI-compat error")
            self._send_json(500, {"error": {"message": str(exc), "type": "server_error"}})

    # ── /note — save highlight + context to Obsidian vault ────────────────
    def _handle_note(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON"); return

        highlight  = (req.get("highlight") or "").strip()
        context    = (req.get("context") or "").strip()
        book_title = (req.get("book_title") or "").strip()
        book_author = (req.get("book_author") or "").strip()
        reading_pct = req.get("reading_pct") or 0
        query      = (req.get("query") or "").strip() or None
        response   = (req.get("response") or "").strip() or None
        mode       = (req.get("mode") or "").strip() or None
        source     = (req.get("source") or "").strip() or None

        # A captured lookup may have no highlight text of its own; require at
        # least a highlight OR a response to anchor the note.
        if not highlight and not response:
            self.send_error(400, "Missing highlight"); return
        if not book_title:
            self.send_error(400, "Missing book_title"); return

        try:
            path = _save_vault_note(
                highlight=highlight,
                context=context,
                book_title=book_title,
                book_author=book_author,
                reading_pct=reading_pct,
                query=query,
                response=response,
                mode=mode,
                source=source,
            )
            self._send_json(200, {"ok": True, "path": path})
        except Exception as exc:
            logging.exception("Note save error")
            self._send_json(500, {"ok": False, "error": str(exc)})

    # ── /xray/progress ────────────────────────────────────────────────────────
    def _handle_xray_progress(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON"); return
        book_hash   = req.get("book_hash", "")
        reading_pct = float(req.get("reading_pct") or 0)
        if book_hash and reading_pct:
            xray_cache.update_reading_pct(book_hash, reading_pct)
        self._send(200, b"ok", "text/plain")

    def _send_json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self._send(code, body, "application/json")


# ── Entry point ───────────────────────────────────────────────────────────────

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTPServer with per-request threads — /ask never blocks Book Index generation."""
    daemon_threads = True


def main():
    log_file = os.path.expanduser("~/Library/Logs/marginalia.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    logging.info("marginalia listening on :%d  model=%s  profile=%s", PORT, MODEL_ID, PROFILE)

    def _shutdown(sig, _frame):
        logging.info("Shutting down (signal %d)", sig)
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
