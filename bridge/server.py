#!/usr/bin/env python3
"""
marginalia — KOReader reading intelligence bridge.

Routes:
  GET  /ping                       health check → "pong"
  GET  /index                      Book Index cache index
  GET  /book-index/status/<job_id> poll a background Book Index generation job
  POST /ask                        conversational query (explain/translate/summarize)
  POST /chat                       free Q&A grounded in reading position (RAG)
  POST /recap                      "where you left off" summary
  POST /wiki                       AI Wiki deep-dive on one entity
  POST /section                    chapter-by-chapter analysis
  POST /note                       save highlighted passage + context to Obsidian vault
  POST /note-new                    create a standalone Obsidian note from a chat response
  POST /book-index/upload-epub      receive EPUB from device, generate Book Index
  POST /book-index/init            find book in Calibre, generate Book Index, cache it
  POST /book-index/progress        update reading position for a cached book

  GET  /v1/models                  OpenAI-compat model list (for KO Assistant)
  POST /v1/chat/completions        OpenAI-compat proxy → Bedrock (for KO Assistant);
                                   Bedrock-only, does NOT use the provider fallback chain

  GET  /monitor               live request-monitor dashboard (HTML)
  GET  /monitor/data          monitor snapshot (JSON, polled by the dashboard)

Config via environment variables (all optional):
  MARGINALIA_PORT         TCP port to listen on           (default: 7731)
  MARGINALIA_AWS_PROFILE  AWS credentials profile          (default: "" — required for Bedrock)
  MARGINALIA_AWS_REGION   Bedrock region                   (default: us-west-2)
  MARGINALIA_MODEL_ID     Primary model                    (default: openai:gpt-4o)
  MARGINALIA_TOKEN        Shared secret (empty = no auth)  (default: "")
  MARGINALIA_MAX_TOKENS   Max tokens for /ask responses    (default: 600)
  MARGINALIA_LOG_FILE      Log file path (default: ~/Library/Logs/marginalia.log on macOS,
                           ~/.local/share/marginalia/marginalia.log on Linux)
  MARGINALIA_CALIBRE_DB   Path to Calibre library dir      (default: ~/Calibre Library)
  MARGINALIA_BOOKS_DIR   Vault subdirectory for book notes  (default: Notes/Books relative to vault)
  MARGINALIA_CAPTURES_DIR Vault subdirectory for standalone notes (default: Notes/Captures relative to vault)
  MARGINALIA_VAULT        Obsidian vault root              (default: ~/Documents)
  MARGINALIA_OPENAI_API_KEY    OpenAI API key for direct OpenAI models   (default: "")
  MARGINALIA_ANTHROPIC_API_KEY Anthropic API key for direct Anthropic    (default: "")
"""

import collections
import hashlib
import io
import json
import logging
import os
import re
import signal
import shutil
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

try:
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:
    Config = None  # type: ignore
    BotoCoreError = Exception  # type: ignore
    ClientError = Exception  # type: ignore

from book_finder import find_epub
from epub_extract import extract_epub
from translation_sidecar import build_translation_index, _koreader_partial_md5
from xray_generator import generate, build_record
import xray_cache
import mentions
import rag
import series
import monitor

# ── Config ────────────────────────────────────────────────────────────────────

PORT       = int(os.environ.get("MARGINALIA_PORT", 7731))
PROFILE    = os.environ.get("MARGINALIA_AWS_PROFILE", "")
# Companion calls use the configured model chain (openai: → direct OpenAI, anthropic: → direct Anthropic, else → Bedrock).
MODEL_ID   = os.environ.get("MARGINALIA_MODEL_ID", "openai:gpt-4o")
TOKEN      = os.environ.get("MARGINALIA_TOKEN", "")
MAX_TOKENS = int(os.environ.get("MARGINALIA_MAX_TOKENS", 600))
VAULT_ROOT = os.path.expanduser(os.environ.get("MARGINALIA_VAULT", "~/Documents"))
_default_books = os.path.join(VAULT_ROOT, "Notes", "Books")
_books_raw = os.path.expanduser(os.environ.get("MARGINALIA_BOOKS_DIR", _default_books))
# Resolve relative paths against VAULT_ROOT so Notes/Books works as documented
BOOKS_DIR  = _books_raw if os.path.isabs(_books_raw) else os.path.join(VAULT_ROOT, _books_raw)
_default_captures = os.path.join(VAULT_ROOT, "Notes", "Captures")
_captures_raw = os.path.expanduser(os.environ.get("MARGINALIA_CAPTURES_DIR", _default_captures))
CAPTURES_DIR = _captures_raw if os.path.isabs(_captures_raw) else os.path.join(VAULT_ROOT, _captures_raw)
UPLOADS_DIR  = Path.home() / ".marginalia" / "uploads"
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

    # Model fallback chain handles provider outages automatically.
    raw = _complete(user_message, instructions=system,
                    reasoning_effort=COMPANION_EFFORT)
    return raw.strip()


# ── X-Ray generation job registry ─────────────────────────────────────────────
# ── Obsidian vault note saving ─────────────────────────────────────────────────

def _norm_title(t: str) -> str:
    """Normalize a title for cross-source matching: drop subtitle after ':', lowercase,
    strip punctuation. 'Atomic Habits: An Easy...' and 'Atomic Habits' collapse to same."""
    if not t:
        return ""
    t = t.lower().split(":")[0]
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_AUTHOR_STOPWORDS = {"phd", "md", "jr", "sr", "dr", "the", "and"}


def _author_tokens(name: str) -> set[str]:
    """Set of lowercased name tokens across ALL listed authors, minus honorifics.

    Used for tolerant author matching: KOReader may send authors in a different order
    or format than an import ('Culadasa (John Yates) & Matthew Immergut' vs a garbled
    'CuladasaMatthew Immergut, Phd'). Requiring only a non-empty token intersection
    still binds the two to the same book without demanding identical author strings.
    """
    if not name:
        return set()
    name = re.sub(r"\(.*?\)", " ", name)
    toks = {t.lower() for t in re.findall(r"[A-Za-z]{2,}", name)}
    return toks - _AUTHOR_STOPWORDS


def _find_existing_book_note(book_title: str, book_author: str) -> str | None:
    """Return the path of an existing note in BOOKS_DIR for the same book, or None.

    Matches on (normalized title, author surname), reading each note's YAML frontmatter
    title/author (falling back to the filename stem). This lets KOReader-driven saves land
    in a note an import already created under a shorter name, preventing duplicate files.
    """
    if not os.path.isdir(BOOKS_DIR):
        return None
    want_title = _norm_title(book_title)
    if not want_title:
        return None
    want_authors = _author_tokens(book_author)
    for entry in os.listdir(BOOKS_DIR):
        if not entry.endswith(".md") or entry.startswith("Series - "):
            continue
        if entry in ("Books.md", "Reading Dashboard.md"):
            continue
        path = os.path.join(BOOKS_DIR, entry)
        try:
            with open(path, encoding="utf-8") as f:
                head = f.read(1500)
        except OSError:
            continue
        ft, fa = None, None
        m = re.match(r"---\n(.*?)\n---", head, re.S)
        if m:
            for line in m.group(1).splitlines():
                mm = re.match(r"^(title|author):\s*(.*)$", line)
                if mm:
                    val = mm.group(2).strip().strip("'\"")
                    if mm.group(1) == "title":
                        ft = val
                    else:
                        fa = val
        # Fall back to filename stem "<Author> - <Title>" when frontmatter is absent.
        if ft is None:
            stem = entry[:-3]
            ft = stem.split(" - ", 1)[1] if " - " in stem else stem
            if fa is None and " - " in stem:
                fa = stem.split(" - ", 1)[0]
        if _norm_title(ft) != want_title:
            continue
        # Title matches. Confirm the author too — but tolerantly: a non-empty token
        # overlap is enough. If either side lists no author, title match alone binds.
        note_authors = _author_tokens(fa or "")
        if not want_authors or not note_authors or (want_authors & note_authors):
            return path
    return None


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

    # Reuse an existing note for the same book if one already exists (e.g. created by a
    # StoryGraph/Hardcover import under a cleaner short name), instead of minting a new
    # "<Full Author> - <Full Title: Subtitle>.md" that duplicates it. KOReader sends full
    # EPUB metadata (long subtitles, multi-author strings) while imports use short names,
    # so a naive filename would collide-by-content. Match on normalized title + author
    # surname — the same key the BookOrbit sync uses.
    existing = _find_existing_book_note(book_title, book_author)
    if existing:
        filepath = existing
    else:
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


# ── Standalone vault note creation ───────────────────────────────────────────

def _create_standalone_note(
    title: str, body: str,
    book_title: str = "", book_author: str = "",
    reading_pct: float = 0,
) -> str:
    """
    Create a standalone Obsidian note from a chat response.
    File: CAPTURES_DIR/<sanitised title>.md

    On first write: creates the file with frontmatter + a wikilink back to the
    book note.  On subsequent writes to the same title: appends a dated section
    rather than overwriting, so the user can build up notes across sessions.
    Returns the absolute path written.
    """
    from datetime import datetime

    os.makedirs(CAPTURES_DIR, exist_ok=True)

    def safe(s: str) -> str:
        return re.sub(r'[\\/:*?"<>|]', '', s).strip()

    filename = safe(title)[:100] + ".md"
    filepath = os.path.join(CAPTURES_DIR, filename)

    date_str = datetime.now().strftime("%Y-%m-%d")
    pct_str  = f" ({int(reading_pct)}%)" if reading_pct else ""

    # Wikilink back to the book note (relative to BOOKS_DIR)
    backlink = ""
    if book_title:
        link_base = (f"{safe(book_author)} - {safe(book_title)}"
                     if book_author else safe(book_title))
        backlink = f"[[{link_base}]]"

    if os.path.exists(filepath):
        # Append a dated section so repeated saves accumulate rather than overwrite.
        with open(filepath, encoding="utf-8") as f:
            content = f.read()
        addition = f"\n\n---\n\n*{date_str}{pct_str}*\n\n{body.strip()}\n"
        content = content.rstrip() + addition
        logging.info("vault note-new: appended to %s", filepath)
    else:
        source_line = (f"\n> *{backlink}{pct_str}*\n" if backlink else "")
        content = (
            f"---\n"
            f'title: "{title}"\n'
            f"date: {date_str}\n"
            + (f'source: "{book_title}"\n' if book_title else "")
            + (f'author: "{book_author}"\n' if book_author else "")
            + "tags:\n  - reading-capture\n---\n\n"
            f"# {title}\n"
            + source_line + "\n"
            + body.strip() + "\n"
        )
        logging.info("vault note-new: created %s", filepath)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    return filepath


# job_id → {status, progress, record, error}
_xray_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


# Uploaded EPUBs used to live at a CONTENT-ADDRESSED path ({md5}.epub), so every
# repeat upload of one book — and the plugin retries on its own — resolved to the
# same file. Each job then unlinked that path unconditionally in its finally
# block, deleting the EPUB out from under the jobs still running: their
# translation build died with FileNotFoundError, the record was cached with a
# null translation_index, init answered needs_epub, and the device re-uploaded
# until it gave up with "Book Index upload failed" (Blood Meridian, 2026-08-09).
#
# Uploads are now PER JOB ({sha256}-{job_id}.epub): a job owns its file outright,
# so no other job can delete or overwrite the bytes it is working on. That also
# closes a content-integrity hole a shared path had — a job opens its upload
# twice (extract_epub, then the translation build), so a colliding upload landing
# between those reads could feed it a different book. SHA-256 replaces MD5 so the
# name isn't attacker-steerable either, but correctness no longer depends on the
# digest being collision-free.
#
# The refcount is kept: it makes cleanup idempotent and safe if a path is ever
# shared again, and lets a bypassed registration still be cleaned up.
_upload_users: dict[str, set[str]] = {}
# Paths whose teardown already completed, so a repeat cleanup can't unlink twice.
# Bounded LRU: this server is long-lived and every distinct upload would
# otherwise add an entry forever.
_upload_done: collections.OrderedDict[str, None] = collections.OrderedDict()
_UPLOAD_DONE_MAX = 512
_uploads_lock = threading.Lock()


def _note_upload_done(epub_path: str) -> None:
    """Record a completed teardown, evicting the oldest entries past the cap."""
    _upload_done[epub_path] = None
    _upload_done.move_to_end(epub_path)
    while len(_upload_done) > _UPLOAD_DONE_MAX:
        _upload_done.popitem(last=False)


def _publish_upload(data: bytes, job_id: str) -> tuple[str, str]:
    """Write an uploaded EPUB to a path this job owns, and claim it.

    Returns (path, sha256). The filename includes job_id, so two jobs uploading
    identical bytes get separate files and can never disturb each other's reads
    or teardown. The bytes are written to a temp file and os.replace()d into
    place, so even a re-publish by the same job swaps the directory entry
    instead of truncating a file it may already be reading.
    """
    epub_hash = hashlib.sha256(data).hexdigest()
    epub_path = str(UPLOADS_DIR / f"{epub_hash}-{job_id}.epub")

    # Write OUTSIDE the lock: this can be up to 100 MB plus an fsync, and
    # blocking every other upload/cleanup for that long is needless.
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(UPLOADS_DIR), suffix=".part")
    published = False
    try:
        with os.fdopen(tmp_fd, "wb") as tmp:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())

        with _uploads_lock:
            # Claim before publishing so no cleanup can race the rename.
            _upload_users.setdefault(epub_path, set()).add(job_id)
            _upload_done.pop(epub_path, None)
            try:
                os.replace(tmp_path, epub_path)
                published = True
            except BaseException:
                users = _upload_users.get(epub_path)
                if users is not None:
                    users.discard(job_id)
                    if not users:
                        del _upload_users[epub_path]
                raise
    finally:
        # Covers every failure path including KeyboardInterrupt/SystemExit while
        # acquiring the lock. Once replace() succeeds the temp name is gone, so
        # only an unpublished temp file is removed here.
        if not published:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return epub_path, epub_hash


def _cleanup_upload(epub_path: str, job_id: str) -> None:
    """Release a job's claim and delete the upload if it was the last user.

    The decision and the unlink happen under one lock, so a republish of the same
    path (a job retrying) cannot have its freshly claimed file deleted by an
    earlier, now-stale cleanup. Paths are per-job, so in practice the "last user"
    is the owning job — the refcount keeps this correct and idempotent anyway.
    """
    with _uploads_lock:
        users = _upload_users.get(epub_path)
        if users is not None:
            users.discard(job_id)
            if users:
                return          # someone else is still using it
            del _upload_users[epub_path]
        elif epub_path in _upload_done:
            return              # already torn down; don't unlink twice
        try:
            os.unlink(epub_path)
        except FileNotFoundError:
            pass                # already gone — normal
        except OSError:
            # Permission/filesystem failures must not be silent, and must NOT be
            # recorded as a completed teardown: the file is still on disk, so a
            # later cleanup has to be allowed to try again instead of returning
            # early and leaking it forever.
            logging.exception("failed to remove upload %s", epub_path)
            return
        _note_upload_done(epub_path)


def _mark_translation_build_complete(index: dict) -> dict:
    """Stamp a COMPLETE translation build so an empty result counts as finished.

    A book can legitimately have nothing to translate. Without this marker an
    empty index is indistinguishable from a failed build, so it fails
    validation, init answers needs_epub, and the device re-uploads forever.

    Only a build that verifiably skipped nothing may be stamped: the count must
    be a real integer 0. A missing field, None, or any other malformed value is
    NOT proof of completeness, and treating it as such would let a broken
    dependency mark a degraded index permanently valid.
    """
    skipped = index.get("skipped_candidates")
    if type(skipped) is int and skipped == 0:
        index["build_attempted"] = True
    return index


def _new_job_id(initial: dict | None = None) -> str:
    """Allocate a job id that is unique among live jobs, and register it.

    job_id is both the `_xray_jobs` key AND part of the per-job upload filename,
    so a duplicate would cross-wire two jobs' status *and* let one job's cleanup
    delete the other's EPUB. `uuid4()[:8]` is only 32 bits, so don't assume
    uniqueness — claim the id under the lock and retry on the (rare) clash.
    """
    with _jobs_lock:
        while True:
            job_id = str(uuid.uuid4())[:8]
            if job_id in _xray_jobs:
                continue          # already live — draw again
            _xray_jobs[job_id] = dict(initial) if initial else {
                "status": "pending", "progress": "Starting",
                "record": None, "error": None,
            }
            return job_id


def _claim_translation_job(book_hash: str) -> tuple[str, bool]:
    """Atomically reuse or reserve one translation backfill job per book."""
    with _jobs_lock:
        for job_id, job in _xray_jobs.items():
            if (job.get("kind") == "translations"
                    and job.get("book_hash") == book_hash
                    and job.get("status") not in ("ready", "failed")):
                return job_id, False
        # Already inside _jobs_lock, so allocate inline rather than calling
        # _new_job_id() (which takes the same non-reentrant lock).
        while True:
            job_id = str(uuid.uuid4())[:8]
            if job_id not in _xray_jobs:
                break
        _xray_jobs[job_id] = {
            "kind": "translations",
            "book_hash": book_hash,
            "status": "pending",
            "progress": "Starting translation index",
            "record": None,
            "error": None,
        }
        return job_id, True


def _epub_matches_cache(epub_path: str, book_hash: str) -> bool:
    try:
        digest = hashlib.md5()
        with open(epub_path, "rb") as epub:
            for chunk in iter(lambda: epub.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == book_hash
    except OSError:
        return False


def _wants_rebuild(req: dict) -> bool:
    """True when the client asked to rebuild instead of reusing the cache.

    The plugin's Reindex action clears its own device cache and sends
    force=true. The bridge previously ignored the flag entirely, so a reindex
    just re-served the same cached record -- there was no way to recover a book
    whose translation index was missing, empty, or partial.
    """
    value = req.get("force")
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return False


def _translation_index_valid(index: object, device_partial_md5: str = "") -> bool:
    if not isinstance(index, dict) or index.get("version") != 1:
        return False
    if index.get("target_language") != "English" or not isinstance(index.get("translations"), dict):
        return False
    # An index with no entries is indistinguishable from a failed or skipped
    # build, so it must not count as complete on its own. Treating it as valid
    # made the failure permanent: the bridge kept serving the empty index from
    # cache and never retried, so the device reported no translations forever.
    #
    # But a book genuinely CAN have nothing to translate (Blood Meridian is
    # English with a few Spanish phrases). Without a way to record "we ran a
    # full build and found nothing", init answered needs_epub forever and the
    # device re-uploaded until it surfaced "Book Index upload failed".
    # _safe_translation_index stamps build_attempted on a COMPLETE build only,
    # which is what separates "found nothing" from "never finished".
    if not index["translations"]:
        if index.get("build_attempted") is not True:
            return False
        if index.get("skipped_candidates"):
            return False
    if not isinstance(index.get("generated_at"), str) or not index["generated_at"]:
        return False
    source = index.get("source_epub")
    if not isinstance(source, dict):
        return False
    if not isinstance(source.get("filename"), str) or not source["filename"]:
        return False
    if not isinstance(source.get("size_bytes"), int) or source["size_bytes"] < 0:
        return False
    if not isinstance(source.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"]):
        return False
    partial = source.get("koreader_partial_md5")
    if not isinstance(partial, str) or not re.fullmatch(r"[0-9a-f]{32}", partial):
        return False
    if device_partial_md5 and partial != device_partial_md5:
        return False
    for key, entry in index["translations"].items():
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{8}", key):
            return False
        if not isinstance(entry, dict):
            return False
        for field in ("normalized_source", "original_source", "source_language", "translation"):
            if not isinstance(entry.get(field), str) or not entry[field]:
                return False
    return True


def _record_matches_device(record: dict, device_partial_md5: str) -> bool:
    epub_path = record.get("book", {}).get("epub_path")
    if not epub_path or not os.path.isfile(epub_path):
        return False
    try:
        return _koreader_partial_md5(Path(epub_path)) == device_partial_md5
    except OSError:
        return False


def _run_translation_index_job(job_id: str, cached: dict, epub_path: str) -> None:
    """Backfill translations for a legacy cached Book Index."""
    def update(status: str, **kw):
        with _jobs_lock:
            _xray_jobs[job_id].update({"status": status, **kw})

    try:
        update("generating", progress="Precomputing foreign-language translations")
        book_hash = cached["book"]["epub_hash"]
        snapshot_path = None
        try:
            with open(epub_path, "rb") as source, tempfile.NamedTemporaryFile(
                    suffix=".epub", delete=False) as snapshot:
                snapshot_path = snapshot.name
                shutil.copyfileobj(source, snapshot)
                snapshot.flush()
                os.fsync(snapshot.fileno())
            if not _epub_matches_cache(snapshot_path, book_hash):
                raise ValueError("EPUB changed before translation backfill")
            translation_index = build_translation_index(snapshot_path)
            translation_index["source_epub"]["filename"] = Path(epub_path).name
            # Same completeness marker the inline build applies — otherwise a
            # backfill that legitimately finds zero passages is merged without
            # it, instantly fails validation, and re-queues forever.
            _mark_translation_build_complete(translation_index)
        finally:
            if snapshot_path:
                try:
                    os.unlink(snapshot_path)
                except FileNotFoundError:
                    pass
        # Generation can take long enough for reading progress or other cache
        # fields to change. Merge into the latest record instead of writing the
        # stale snapshot captured by /book-index/init.
        record = xray_cache.merge_translation_index(book_hash, translation_index)
        update("ready", record=record, error=None)
    except Exception as exc:
        logging.exception("Translation index job %s failed", job_id)
        update("failed", error=str(exc))


def _safe_translation_index(epub_path: str) -> dict | None:
    """Build a translation index without ever failing the Book Index job.

    Translations are an enhancement, not a precondition: a book must still get
    its Book Index (and RAG, mentions, recap) even when translation generation
    fails outright. Mirrors the non-fatal treatment of mentions and rag.
    """
    try:
        index = build_translation_index(epub_path)
    except Exception:
        logging.exception("translation index build failed (non-fatal): %s", epub_path)
        return None
    skipped = index.get("skipped_candidates") or 0
    if skipped:
        logging.warning("Translation index for %s is partial: %d candidate(s) skipped",
                        epub_path, skipped)
    else:
        # Record that a COMPLETE build ran. This is what lets a book with
        # nothing to translate (an English novel with a few foreign phrases)
        # count as finished instead of looping the device through needs_epub →
        # upload → empty index → needs_epub until it reports upload failure.
        _mark_translation_build_complete(index)
    logging.info("Translation index built for %s: %d entr(ies), %d skipped",
                 epub_path, len(index.get("translations") or {}), skipped)
    return index


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
        update("generating", progress="Precomputing foreign-language translations")
        record["translation_index"] = _safe_translation_index(book_meta["epub_path"])
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


def _run_xray_job_from_epub(job_id: str, epub_path: str, title: str, author: str, reading_pct: float) -> None:
    """Background thread: generate Book Index from a device-uploaded EPUB.
    Skips the Calibre lookup — epub_path is already local. Cleans up the
    uploaded file on completion (success or failure).
    """
    def update(status: str, **kw):
        with _jobs_lock:
            _xray_jobs[job_id].update({"status": status, **kw})

    try:
        update("extracting", progress="Extracting EPUB text")
        content = extract_epub(epub_path)

        # Use provided title/author as fallback if EPUB metadata is empty
        if not content.title:
            content.title = title
        if not content.author:
            content.author = author

        update("generating", progress=f"Generating Book Index ({content.total_chars:,} chars)")

        # Try series info from Calibre metadata.db by title (no calibre_id available)
        sv = series.resolve(title=content.title, author=content.author)
        if sv:
            content.series = sv["series"]
            content.series_index = sv["series_index"]
            logging.info("series: resolved '%s' #%s for %s",
                         sv["series"], sv["series_index"], content.title)

        xray, strategy = generate(content)

        try:
            mention_idx = mentions.build_mentions(content, xray)
            mentions.add_mention_counts(xray, mention_idx)
        except Exception:
            logging.exception("mentions build failed (non-fatal)")
            mention_idx = {}

        book_meta = {"epub_path": epub_path, "calibre_id": None}
        record = build_record(content, book_meta, xray, strategy)
        record["mentions"] = mention_idx
        update("generating", progress="Precomputing foreign-language translations")
        record["translation_index"] = _safe_translation_index(epub_path)
        if reading_pct:
            record["last_reading_pct"] = reading_pct
        xray_cache.save(content.file_hash, record)
        xray_cache.remove_knowledge_by_title(title, author)

        try:
            rag.build_index(content, content.file_hash)
        except Exception:
            logging.exception("rag index build failed (non-fatal)")

        update("ready", record=record, error=None)
        logging.info("Book Index job %s complete (device epub): %s", job_id, title)

    except Exception as exc:
        logging.exception("Book Index job %s failed (device epub)", job_id)
        update("failed", error=str(exc))
    finally:
        # Only the last job using this shared upload may delete it, and the
        # decision + unlink happen atomically so a concurrently republished
        # upload isn't deleted out from under its new owner.
        _cleanup_upload(epub_path, job_id)


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

    # ── Header-based auth (defense-in-depth) ────────────────────────────────────
    # Primary enforcement is at the reverse proxy (Caddy) on the public mount, but
    # we also honor the X-Marginalia-Token header here so a direct hit to the
    # container is rejected when a token is configured. Returns True if the request
    # may proceed, else sends 403 and returns False. /ping stays open for health
    # checks. The legacy JSON-body token check on /ask remains for old plugins.
    def _header_auth_ok(self) -> bool:
        if not TOKEN:
            return True
        supplied = self.headers.get("X-Marginalia-Token", "")
        if supplied == TOKEN:
            return True
        self.send_error(403, "Forbidden")
        return False

    # ── GET dispatch (with request monitoring) ──────────────────────────────────
    def do_GET(self):
        # Monitor pages are served directly and never self-tracked.
        if self.path == "/monitor":
            if not self._header_auth_ok():
                return
            self._send(200, monitor.render_html().encode(), "text/html; charset=utf-8")
            return
        if self.path == "/monitor/data":
            if not self._header_auth_ok():
                return
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
            return
        # All other GET endpoints require the token when one is configured.
        if not self._header_auth_ok():
            return
        if self.path == "/index":
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
                resp["translation_index"] = job["record"].get("translation_index")
                resp["generated_at"] = job["record"].get("generated_at")
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
        # Token gate (defense-in-depth; primary enforcement at the reverse proxy).
        if not self._header_auth_ok():
            return
        if self.path == "/book-index/init":
            self._handle_xray_init()
            return
        if self.path == "/book-index/upload-epub":
            self._handle_epub_upload()
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
        if self.path == "/note-new":
            self._handle_note_new()
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

        # Token check (optional). Accept EITHER the X-Marginalia-Token header
        # (checked already in _dispatch_post) OR the legacy JSON body token, so
        # older plugin builds that only send the body token keep working.
        if TOKEN and req.get("token") != TOKEN \
                and self.headers.get("X-Marginalia-Token", "") != TOKEN:
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
        raw_device_partial_md5 = req.get("device_partial_md5")
        if raw_device_partial_md5 is not None and not isinstance(raw_device_partial_md5, str):
            self.send_error(400, "Invalid device_partial_md5"); return
        device_partial_md5 = (raw_device_partial_md5 or "").strip().lower()
        if device_partial_md5 and not re.fullmatch(r"[0-9a-f]{32}", device_partial_md5):
            self.send_error(400, "Invalid device_partial_md5"); return

        if not title:
            self.send_error(400, "Missing book_title"); return

        force_rebuild = _wants_rebuild(req)

        # ── Check cache first ──────────────────────────────────────────────────
        cached_records = [] if force_rebuild else xray_cache.find_all_by_title_author(title, author)
        if force_rebuild:
            logging.info("Book Index rebuild requested (force), bypassing cache: %s", title)
        if device_partial_md5:
            cached = next((record for record in cached_records
                           if _translation_index_valid(record.get("translation_index"), device_partial_md5)), None)
            if cached is None:
                cached = next((record for record in cached_records
                               if _record_matches_device(record, device_partial_md5)), None)
            if cached is None:
                cached = next((record for record in cached_records
                               if record.get("strategy") != "knowledge_only"), None)
        else:
            cached = None if force_rebuild else xray_cache.find_by_title_author(title, author)
        if cached:
            logging.info("Book Index cache HIT: %s", title)
            if reading_pct:
                xray_cache.update_reading_pct(cached["book"]["epub_hash"], reading_pct)
            if not _translation_index_valid(cached.get("translation_index"), device_partial_md5):
                book_hash = cached["book"]["epub_hash"]
                epub_path = cached.get("book", {}).get("epub_path")
                if device_partial_md5:
                    try:
                        server_partial_md5 = _koreader_partial_md5(Path(epub_path)) if epub_path and os.path.isfile(epub_path) else ""
                    except OSError:
                        server_partial_md5 = ""
                    if server_partial_md5 != device_partial_md5:
                        self._send_json(200, {"status": "needs_epub"})
                        return
                elif not epub_path or not _epub_matches_cache(epub_path, book_hash):
                    book_meta = find_epub(title, author) or (find_epub(title, "") if author else None)
                    epub_path = book_meta and book_meta.get("epub_path")
                if not device_partial_md5 and (not epub_path or not _epub_matches_cache(epub_path, book_hash)):
                    logging.info("Translation index: no server EPUB for %r, requesting device epub", title)
                    self._send_json(200, {"status": "needs_epub"})
                    return
                job_id, created = _claim_translation_job(book_hash)
                if created:
                    threading.Thread(
                        target=_run_translation_index_job,
                        args=(job_id, cached, epub_path),
                        daemon=True,
                    ).start()
                self._send_json(202, {
                    "status": "generating",
                    "job_id": job_id,
                    "poll_url": f"/book-index/status/{job_id}",
                })
                return
            mac_generated_at = cached.get("generated_at", "")
            # If device already has this version, just confirm it's current
            if device_generated_at and device_generated_at >= mac_generated_at:
                self._send_json(200, {"status": "current"})
                return
            self._send_json(200, {"status": "ready", "cached": True,
                                   "xray": _serve_xray(cached), "book": cached["book"],
                                   "mentions": cached.get("mentions", {}),
                                   "translation_index": cached.get("translation_index"),
                                   "generated_at": mac_generated_at})
            return

        # ── Not cached — check Calibre synchronously before deciding what to do ──
        book_meta = find_epub(title, author)
        if not book_meta and author:
            book_meta = find_epub(title, "")
        if not book_meta:
            # No EPUB in Calibre — ask the device to send it
            logging.info("Book Index: no Calibre match for %r, requesting device epub", title)
            self._send_json(200, {"status": "needs_epub"})
            return
        if device_partial_md5:
            try:
                calibre_partial_md5 = _koreader_partial_md5(Path(book_meta["epub_path"]))
            except OSError:
                calibre_partial_md5 = ""
            if calibre_partial_md5 != device_partial_md5:
                logging.info("Book Index: Calibre edition differs from open device EPUB for %r", title)
                self._send_json(200, {"status": "needs_epub"})
                return

        # Calibre has it — spawn normal background job
        job_id = _new_job_id()
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

    # ── /note-new — create a standalone Obsidian note ───────────────────────────
    def _handle_note_new(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON"); return

        title       = (req.get("title") or "").strip()
        body        = (req.get("body") or "").strip()
        book_title  = (req.get("book_title") or "").strip()
        book_author = (req.get("book_author") or "").strip()
        reading_pct = float(req.get("reading_pct") or 0)

        if not title:
            self.send_error(400, "Missing title"); return
        if not body:
            self.send_error(400, "Missing body"); return

        try:
            path = _create_standalone_note(
                title=title, body=body,
                book_title=book_title, book_author=book_author,
                reading_pct=reading_pct,
            )
            self._send_json(200, {"ok": True, "path": path})
        except Exception as exc:
            logging.exception("/note-new error")
            self._send_json(500, {"ok": False, "error": str(exc)})


    # ── /book-index/upload-epub — receive EPUB from device, generate Book Index ──
    def _handle_epub_upload(self):
        title       = self.headers.get("X-Book-Title", "").strip()
        author      = self.headers.get("X-Book-Author", "").strip()
        reading_pct = float(self.headers.get("X-Reading-Pct") or 0)
        length      = int(self.headers.get("Content-Length") or 0)

        if not title:
            self.send_error(400, "Missing X-Book-Title header"); return
        if length <= 0:
            self.send_error(400, "Empty body"); return
        if length > 100 * 1024 * 1024:
            self.send_error(413, "EPUB too large (100 MB limit)"); return

        data = self.rfile.read(length)

        job_id = _new_job_id()
        # Publish + claim under one lock. The digest is discarded here because it
        # is already in epub_path (and therefore in the log line below).
        try:
            epub_path, _ = _publish_upload(data, job_id)
        except Exception:
            # Disk full, permissions, an unavailable hash primitive, etc. Don't
            # leave a job stuck "pending" forever — drop it and answer honestly.
            logging.exception("epub upload could not be stored (title=%r)", title)
            with _jobs_lock:
                _xray_jobs.pop(job_id, None)
            self.send_error(500, "Could not store uploaded EPUB")
            return
        logging.info("epub upload: %s bytes → %s (title=%r)", length, epub_path, title)

        t = threading.Thread(
            target=_run_xray_job_from_epub,
            args=(job_id, epub_path, title, author, reading_pct),
            daemon=True,
        )
        try:
            t.start()
        except BaseException:
            # Thread never ran, so its finally-block cleanup never will either.
            logging.exception("could not start Book Index job for %r", title)
            _cleanup_upload(epub_path, job_id)
            with _jobs_lock:
                _xray_jobs.pop(job_id, None)
            self.send_error(500, "Could not start Book Index job")
            return
        logging.info("Book Index job %s started (device epub) for %r", job_id, title)
        self._send_json(202, {"status": "generating", "job_id": job_id,
                              "poll_url": f"/book-index/status/{job_id}"})

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
    import platform
    _default_log = os.path.expanduser(
        "~/Library/Logs/marginalia.log" if platform.system() == "Darwin"
        else os.path.join(os.path.expanduser("~/.local/share/marginalia"), "marginalia.log")
    )
    log_file = os.environ.get("MARGINALIA_LOG_FILE", _default_log)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
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
