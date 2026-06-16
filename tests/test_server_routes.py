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
