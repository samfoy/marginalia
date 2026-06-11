#!/usr/bin/env python3
"""
piread-bridge — KOReader → Claude Bedrock bridge.

Routes:
  GET  /ping                  health check → "pong"
  GET  /index                 X-Ray cache index (for pi chat queries)
  GET  /xray/status/<job_id>  poll a background X-Ray generation job
  POST /ask                   conversational query (explain/translate/summarize)
  POST /note                  save highlighted passage + context to Obsidian vault
  POST /xray/init             find book in Calibre, generate X-Ray, cache it
  POST /xray/progress         update reading position for a cached book

Config via environment variables (all optional):
  PIREAD_PORT         TCP port to listen on           (default: 7731)
  PIREAD_AWS_PROFILE  AWS credentials profile          (default: openclaw-bedrock)
  PIREAD_AWS_REGION   Bedrock region                   (default: us-west-2)
  PIREAD_MODEL_ID     Model for /ask queries           (default: us.anthropic.claude-sonnet-4-6)
  PIREAD_TOKEN        Shared secret (empty = no auth)  (default: "")
  PIREAD_MAX_TOKENS   Max tokens for /ask responses    (default: 600)
  PIREAD_VAULT        Obsidian vault root              (default: ~/Documents/Sam)
"""

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

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from book_finder import find_epub
from epub_extract import extract_epub
from xray_generator import generate, build_record
import xray_cache
import pi_session

# ── Config ────────────────────────────────────────────────────────────────────

PORT       = int(os.environ.get("PIREAD_PORT", 7731))
PROFILE    = os.environ.get("PIREAD_AWS_PROFILE", "openclaw-bedrock")
REGION     = os.environ.get("PIREAD_AWS_REGION", "us-west-2")
MODEL_ID   = os.environ.get("PIREAD_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
TOKEN      = os.environ.get("PIREAD_TOKEN", "")
MAX_TOKENS = int(os.environ.get("PIREAD_MAX_TOKENS", 600))
VAULT_ROOT = os.path.expanduser(os.environ.get("PIREAD_VAULT", "~/Documents/Sam"))
BOOKS_DIR  = os.path.join(VAULT_ROOT, "Notes", "Books")

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

# ── Bedrock client ────────────────────────────────────────────────────────────

def _bedrock_client():
    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    return session.client("bedrock-runtime")


def ask_claude(text: str, context: str | None, book_title: str | None,
               book_author: str | None, mode: str) -> str:
    system = SYSTEM_PROMPTS.get(mode, DEFAULT_SYSTEM)

    # Build the user message
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

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user_message}],
    }

    client = _bedrock_client()
    resp = client.invoke_model(
        modelId=MODEL_ID,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    result = json.loads(resp["body"].read())
    return result["content"][0]["text"].strip()


# ── X-Ray generation job registry ─────────────────────────────────────────────
# ── Obsidian vault note saving ─────────────────────────────────────────────────

def _save_vault_note(
    highlight: str, context: str,
    book_title: str, book_author: str, reading_pct: float,
) -> str:
    """
    Append a highlight + optional context to the book's Obsidian vault note.
    File: BOOKS_DIR/<Author> - <Title>.md
    Creates the file with frontmatter if it doesn't exist.
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

    # Build bullet
    date_str  = datetime.now().strftime("%Y-%m-%d")
    pct_tag   = f" ({int(reading_pct)}%)" if reading_pct else ""
    lines     = [f"- {date_str}{pct_tag}:", f"  > {highlight}"]
    if context:
        lines += ["", f"  {context}"]
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
            # Fallback: generate from Claude's knowledge (no EPUB needed)
            logging.info("Book not in Calibre, using knowledge-only mode: %s", title)
            update("generating", progress=f"Generating X-Ray from knowledge (no EPUB): {title}")
            _run_knowledge_xray_job(job_id, title, author)
            return

        update("extracting", progress="Extracting EPUB text")
        content = extract_epub(book_meta["epub_path"])

        chars = content.total_chars
        if chars <= 560_000:
            strat = "single_shot"
        elif chars <= 1_600_000:
            strat = "two_pass"
        else:
            strat = "chunked"
        update("generating",
               progress=f"Generating X-Ray via {strat} ({chars:,} chars)")

        xray, strategy = generate(content)
        record = build_record(content, book_meta, xray, strategy)
        if reading_pct:
            record["last_reading_pct"] = reading_pct
        xray_cache.save(content.file_hash, record)
        update("ready", record=record, error=None)
        logging.info("X-Ray job %s complete: %s", job_id, title)

    except Exception as exc:
        logging.exception("X-Ray job %s failed", job_id)
        update("failed", error=str(exc))


def _run_knowledge_xray_job(job_id: str, title: str, author: str) -> None:
    """
    Background thread: generate X-Ray from Claude's training knowledge alone.
    Used when the EPUB is not in Calibre (e.g. audiobook listeners).
    """
    def update(status: str, **kw):
        with _jobs_lock:
            _xray_jobs[job_id].update({"status": status, **kw})

    try:
        update("generating", progress=f"Generating X-Ray from knowledge: {title}")

        # Build a knowledge-only prompt
        system = (
            "You are a literary analyst. Generate a structured X-Ray for the book "
            "from your training knowledge. Return ONLY valid JSON. "
            "Your entire response must be one JSON object starting with '{' and ending with '}'."
        )
        prompt = f"""Generate a complete X-Ray for \"{title}\" by {author} from your knowledge of the book.

Return JSON matching exactly this structure:
{{
  \"book_type\": \"fiction\",
  \"characters\": [
    {{\"name\": str, \"role\": str, \"description\": str, \"aliases\": [str], \"first_appearance_pct\": 0}}
  ],
  \"locations\": [{{\"name\": str, \"description\": str, \"importance\": str}}],
  \"terms\": [{{\"name\": str, \"definition\": str, \"aliases\": [str]}}],
  \"historical_figures\": [{{\"name\": str, \"biography\": str, \"context_in_book\": str}}],
  \"references\": [
    {{\"name\": str, \"type\": \"literary|historical|mythological|cultural\", \"description\": str, \"context_in_book\": str, \"first_appearance_pct\": 0}}
  ],
  \"timeline\": [{{\"chapter\": str, \"event\": str, \"position_pct\": 0}}],
  \"author_info\": {{\"name\": str, \"bio\": str, \"born\": str, \"died\": null}}
}}

Generate 15-25 characters, 10-15 locations, 15-25 terms, 10-20 references, 25-40 timeline events.
For position_pct use your best estimate of where in the book each entity/event appears (0-100).
"""

        import json as _json
        import boto3
        from botocore.config import Config as _BotocoreConfig
        _cfg = _BotocoreConfig(read_timeout=600, connect_timeout=30)
        session = boto3.Session(profile_name=PROFILE, region_name=REGION)
        client = session.client("bedrock-runtime", config=_cfg)
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 16000,  # knowledge-only needs more room
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        resp = client.invoke_model(
            modelId=MODEL_ID,
            body=_json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )
        raw = _json.loads(resp["body"].read())["content"][0]["text"].strip()

        # Parse JSON (with repair fallback)
        try:
            data = _json.loads(raw)
        except _json.JSONDecodeError:
            start = raw.find("{")
            if start >= 0:
                raw = raw[start:]
            # Balance braces
            depth, in_str, esc = 0, False, False
            end = -1
            for i, c in enumerate(raw):
                if esc: esc = False; continue
                if c == "\\" and in_str: esc = True; continue
                if c == '"': in_str = not in_str; continue
                if not in_str:
                    if c == "{": depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0: end = i; break
            data = _json.loads(raw[:end+1] if end >= 0 else raw)

        from xray_generator import build_record
        from xray_cache import save
        import hashlib
        from epub_extract import EpubContent
        from datetime import datetime, timezone

        # Build a minimal EpubContent stand-in
        book_hash = hashlib.md5(f"{title}|{author}|knowledge".encode()).hexdigest()
        record = {
            "version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "strategy": "knowledge_only",
            "book": {
                "title": title, "author": author,
                "series": None, "series_index": None,
                "calibre_id": None, "epub_path": None,
                "epub_hash": book_hash,
                "total_chars": 0, "chapter_count": 0,
            },
            "xray": data,
            "mentions": {},
        }
        save(book_hash, record)
        update("ready", record=record, error=None)
        logging.info("Knowledge X-Ray complete: %s", title)

    except Exception as exc:
        logging.exception("Knowledge X-Ray job %s failed", job_id)
        update("failed", error=str(exc))


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # redirect to Python logging
        logging.info("HTTP %s", fmt % args)

    # ── GET /ping ──────────────────────────────────────────────────────────────
    def do_GET(self):
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
        elif self.path.startswith("/xray/status/"):
            job_id = self.path.split("/xray/status/", 1)[-1]
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
                resp["xray"] = job["record"]["xray"]
                resp["book"] = job["record"]["book"]
            self._send_json(200, resp)
        else:
            self.send_error(404)

    # ── POST dispatch ─────────────────────────────────────────────────────────
    def do_POST(self):
        if self.path == "/xray/init":
            self._handle_xray_init()
            return
        if self.path == "/xray/progress":
            self._handle_xray_progress()
            return
        if self.path == "/chat":
            self._handle_chat()
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
        book_context = "\n".join(ctx_parts)

        try:
            session = pi_session.get_session()
            response_text = session.ask(question, book_context)
            self._send_json(200, {"response": response_text, "error": None})
        except Exception as exc:
            logging.exception("pi_session /chat error")
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
            logging.info("X-Ray cache HIT: %s", title)
            if reading_pct:
                xray_cache.update_reading_pct(cached["book"]["epub_hash"], reading_pct)
            mac_generated_at = cached.get("generated_at", "")
            # If device already has this version, just confirm it's current
            if device_generated_at and device_generated_at >= mac_generated_at:
                self._send_json(200, {"status": "current"})
                return
            self._send_json(200, {"status": "ready", "cached": True,
                                   "xray": cached["xray"], "book": cached["book"],
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
        logging.info("X-Ray job %s started for '%s'", job_id, title)
        self._send_json(202, {"status": "generating", "job_id": job_id,
                               "poll_url": f"/xray/status/{job_id}"})

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

        if not highlight:
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
    """HTTPServer with per-request threads — /ask never blocks X-Ray generation."""
    daemon_threads = True


def main():
    log_file = os.path.expanduser("~/Library/Logs/piread-bridge.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    logging.info("piread-bridge listening on :%d  model=%s  profile=%s", PORT, MODEL_ID, PROFILE)

    # Warm up the pi session in a background thread so the first /chat is fast
    threading.Thread(target=pi_session.get_session, daemon=True).start()

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
