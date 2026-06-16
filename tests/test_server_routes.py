"""test_server_routes.py — integration tests against a live ThreadingHTTPServer."""

import json
import os
import threading
import urllib.error
import urllib.request

import pytest

import server
import xray_generator
from server import Handler, ThreadingHTTPServer


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def live_server(tmp_vault, monkeypatch):
    """
    Spin up a real ThreadingHTTPServer on a random OS-assigned port,
    with vault dirs redirected to tmp_vault and LLM calls mocked.
    """
    books_dir = str(tmp_vault / "Notes" / "Books")
    captures_dir = str(tmp_vault / "Notes" / "Captures")

    monkeypatch.setattr(server, "BOOKS_DIR", books_dir)
    monkeypatch.setattr(server, "CAPTURES_DIR", captures_dir)
    monkeypatch.setattr(server, "VAULT_ROOT", str(tmp_vault))
    monkeypatch.setattr(server, "TOKEN", "")  # no auth required

    # Mock the LLM — both ask_claude and _gpt_companion route through _complete
    monkeypatch.setattr(xray_generator, "_complete",
                        lambda *args, **kwargs: "mocked AI response")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    yield {"port": port, "vault": tmp_vault,
           "books_dir": books_dir, "captures_dir": captures_dir}

    srv.shutdown()


# ── helpers ───────────────────────────────────────────────────────────────────

def _get(port: int, path: str):
    url = f"http://127.0.0.1:{port}{path}"
    try:
        resp = urllib.request.urlopen(url, timeout=5)
        return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post(port: int, path: str, payload: dict):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


# ═══════════════════════════════════════════════════════════════════════════════
# GET /ping
# ═══════════════════════════════════════════════════════════════════════════════

def test_ping(live_server):
    code, body = _get(live_server["port"], "/ping")
    assert code == 200
    assert body == b"pong"


# ═══════════════════════════════════════════════════════════════════════════════
# POST /note
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoteRoute:

    def test_valid_payload_creates_file(self, live_server):
        code, body = _post(live_server["port"], "/note", {
            "highlight": "A great line",
            "book_title": "Dune",
            "book_author": "Frank Herbert",
            "reading_pct": 42,
        })
        assert code == 200
        assert body.get("ok") is True
        # File created in books_dir
        path = body.get("path", "")
        assert os.path.exists(path)
        assert path.startswith(live_server["books_dir"])

    def test_missing_book_title_returns_400(self, live_server):
        code, _ = _post(live_server["port"], "/note", {
            "highlight": "Some text",
        })
        assert code == 400

    def test_missing_highlight_and_response_returns_400(self, live_server):
        code, _ = _post(live_server["port"], "/note", {
            "book_title": "Dune",
        })
        assert code == 400

    def test_note_with_query_and_response(self, live_server):
        code, body = _post(live_server["port"], "/note", {
            "book_title": "Dune",
            "book_author": "Frank Herbert",
            "query": "What is spice?",
            "response": "Spice is the most valuable substance.",
            "reading_pct": 55,
        })
        assert code == 200
        assert body.get("ok") is True
        content = open(body["path"]).read()
        assert "What is spice?" in content
        assert "Spice is the most valuable substance." in content


# ═══════════════════════════════════════════════════════════════════════════════
# POST /note-new
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoteNewRoute:

    def test_valid_payload_creates_file(self, live_server):
        code, body = _post(live_server["port"], "/note-new", {
            "title": "Emergency kit",
            "body": "Gloves, suction bulb, cord clamp.",
            "book_title": "The Expectant Father",
            "book_author": "Jennifer Ash Rudick",
            "reading_pct": 52,
        })
        assert code == 200
        assert body.get("ok") is True
        path = body.get("path", "")
        assert os.path.exists(path)
        assert path.startswith(live_server["captures_dir"])

    def test_missing_title_returns_400(self, live_server):
        code, _ = _post(live_server["port"], "/note-new", {
            "body": "Some content",
        })
        assert code == 400

    def test_missing_body_returns_400(self, live_server):
        code, _ = _post(live_server["port"], "/note-new", {
            "title": "My note",
        })
        assert code == 400

    def test_note_content_written(self, live_server):
        code, body = _post(live_server["port"], "/note-new", {
            "title": "Ice-nine symbolism",
            "body": "Ice-nine is a metaphor for human hubris.",
        })
        assert code == 200
        content = open(body["path"]).read()
        assert "Ice-nine is a metaphor for human hubris." in content


# ═══════════════════════════════════════════════════════════════════════════════
# POST /ask
# ═══════════════════════════════════════════════════════════════════════════════

class TestAskRoute:

    def test_returns_mocked_response(self, live_server):
        code, body = _post(live_server["port"], "/ask", {
            "text": "Who is Paul Atreides?",
            "book_title": "Dune",
            "mode": "whois",
        })
        assert code == 200
        assert "response" in body
        assert body["response"] == "mocked AI response"
        assert body.get("error") is None

    def test_missing_text_returns_400(self, live_server):
        code, _ = _post(live_server["port"], "/ask", {
            "book_title": "Dune",
        })
        assert code == 400


# ═══════════════════════════════════════════════════════════════════════════════
# POST /chat
# ═══════════════════════════════════════════════════════════════════════════════

class TestChatRoute:

    def test_returns_response(self, live_server):
        code, body = _post(live_server["port"], "/chat", {
            "question": "What is happening in this chapter?",
            "book_title": "Dune",
            "book_author": "Frank Herbert",
            "reading_pct": 30,
        })
        assert code == 200
        assert "response" in body
        assert body["response"]  # non-empty

    def test_missing_question_returns_400(self, live_server):
        code, _ = _post(live_server["port"], "/chat", {
            "book_title": "Dune",
        })
        assert code == 400


# ═══════════════════════════════════════════════════════════════════════════════
# 404 on unknown route
# ═══════════════════════════════════════════════════════════════════════════════

def test_unknown_route_returns_404(live_server):
    code, _ = _get(live_server["port"], "/no-such-endpoint")
    assert code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# POST /book-index/init  — needs_epub + upload
# ═══════════════════════════════════════════════════════════════════════════════

class TestXRayEpubUpload:

    def test_init_returns_needs_epub_when_calibre_misses(self, live_server, monkeypatch):
        """When Calibre has no match, /book-index/init returns needs_epub."""
        import book_finder
        monkeypatch.setattr(book_finder, "find_epub", lambda *a, **kw: None)
        # Also patch xray_cache so there's no stale cached hit
        import xray_cache as xc
        monkeypatch.setattr(xc, "find_by_title_author", lambda *a, **kw: None)

        code, body = _post(live_server["port"], "/book-index/init", {
            "book_title": "A Book Not In Calibre XYZ123",
            "book_author": "Nobody",
            "reading_pct": 50,
        })
        assert code == 200
        assert body.get("status") == "needs_epub"

    def test_upload_epub_starts_job(self, live_server, tmp_path, minimal_epub_bytes, monkeypatch):
        """Uploading a valid EPUB returns 202 with a job_id."""
        import server as srv

        started = []
        real_thread_start = __import__("threading").Thread.start

        def fake_run(job_id, epub_path, title, author, reading_pct):
            started.append(job_id)
            # Don't actually generate — just mark ready immediately
            with srv._jobs_lock:
                srv._xray_jobs[job_id]["status"] = "ready"
            try:
                import os; os.unlink(epub_path)
            except OSError:
                pass

        monkeypatch.setattr(srv, "_run_xray_job_from_epub", fake_run)

        url = f"http://127.0.0.1:{live_server['port']}/book-index/upload-epub"
        req = urllib.request.Request(
            url,
            data=minimal_epub_bytes,
            headers={
                "Content-Type":   "application/epub+zip",
                "Content-Length": str(len(minimal_epub_bytes)),
                "X-Book-Title":   "Test Book",
                "X-Book-Author":  "Test Author",
                "X-Reading-Pct":  "42",
            },
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            code = resp.getcode()
            body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            code = e.code
            body = {}

        assert code == 202
        assert "job_id" in body
        assert body.get("status") == "generating"
        assert body.get("poll_url", "").startswith("/book-index/status/")

    def test_upload_epub_missing_title_returns_400(self, live_server, minimal_epub_bytes):
        """Upload without X-Book-Title header → 400."""
        url = f"http://127.0.0.1:{live_server['port']}/book-index/upload-epub"
        req = urllib.request.Request(
            url,
            data=minimal_epub_bytes,
            headers={
                "Content-Type":   "application/epub+zip",
                "Content-Length": str(len(minimal_epub_bytes)),
                # deliberately no X-Book-Title
            },
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400

    def test_upload_epub_empty_body_returns_400(self, live_server):
        """Upload with Content-Length: 0 → 400."""
        url = f"http://127.0.0.1:{live_server['port']}/book-index/upload-epub"
        req = urllib.request.Request(
            url,
            data=b"",
            headers={
                "Content-Type":    "application/epub+zip",
                "Content-Length":  "0",
                "X-Book-Title":    "Some Book",
            },
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
