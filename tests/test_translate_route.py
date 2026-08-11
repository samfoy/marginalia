"""test_translate_route.py — POST /translate (live on-demand translation).

Distinct from the KOReader plugin's offline "Translate to English", which reads
only the precomputed translation_index and must never call the network. This
endpoint serves clients with no Book Index pipeline (the CrossPoint firmware on
the Xteink X4 Pro), which hold only the selected text.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

import server
import xray_generator
from server import Handler, ThreadingHTTPServer


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def translate_server(monkeypatch):
    """Live server with _complete captured so prompt construction is assertable."""
    monkeypatch.setattr(server, "TOKEN", "")  # no auth in-process

    calls = []

    def fake_complete(prompt, instructions=None, reasoning_effort=None, primary=None):
        calls.append({"prompt": prompt, "instructions": instructions,
                      "reasoning_effort": reasoning_effort, "primary": primary})
        return "  The horse was very tired.  "  # deliberately padded: handler must strip

    monkeypatch.setattr(xray_generator, "_complete", fake_complete)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"port": port, "calls": calls}
    srv.shutdown()


def _post(port, path, payload):
    """POST returning (code, parsed_body) — parses the body on error too, unlike
    the helper in test_server_routes, because these tests assert on error text."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.getcode(), json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"_raw": raw.decode(errors="replace")}


# ── happy path ────────────────────────────────────────────────────────────────

def test_translate_returns_stripped_translation(translate_server):
    code, body = _post(translate_server["port"], "/translate",
                       {"text": "El caballo estaba muy cansado."})
    assert code == 200
    assert body["translation"] == "The horse was very tired."  # stripped
    assert body["error"] is None
    assert body["target_lang"] == "English"


def test_defaults_to_english_and_pins_sonnet5(translate_server):
    _post(translate_server["port"], "/translate", {"text": "Mon Dieu"})
    call = translate_server["calls"][0]
    assert call["primary"] == server.TRANSLATE_MODEL_ID
    assert "sonnet-5" in server.TRANSLATE_MODEL_ID
    assert "Target language: English." in call["prompt"]
    assert call["instructions"] == server.TRANSLATE_INSTRUCTIONS


def test_target_language_is_honoured(translate_server):
    code, body = _post(translate_server["port"], "/translate",
                       {"text": "the horse", "target_lang": "Spanish"})
    assert code == 200
    assert body["target_lang"] == "Spanish"
    assert "Target language: Spanish." in translate_server["calls"][0]["prompt"]


def test_source_language_hint_included_when_given(translate_server):
    _post(translate_server["port"], "/translate",
          {"text": "das Pferd", "source_lang": "German"})
    assert "Source language: German." in translate_server["calls"][0]["prompt"]


def test_source_language_autodetect_when_absent(translate_server):
    _post(translate_server["port"], "/translate", {"text": "das Pferd"})
    assert "Detect the source language." in translate_server["calls"][0]["prompt"]


# ── context handling (the single-word disambiguation path) ─────────────────────

def test_context_is_sent_but_marked_do_not_translate(translate_server):
    _post(translate_server["port"], "/translate",
          {"text": "banco", "context": "Se sentó en el banco del parque."})
    prompt = translate_server["calls"][0]["prompt"]
    assert "Se sentó en el banco del parque." in prompt
    assert "do NOT translate this" in prompt


def test_context_identical_to_text_is_omitted(translate_server):
    """No point spending tokens repeating the selection as its own context."""
    same = "El caballo"
    _post(translate_server["port"], "/translate", {"text": same, "context": same})
    prompt = translate_server["calls"][0]["prompt"]
    assert prompt.count(same) == 1
    assert "do NOT translate this" not in prompt


def test_book_title_and_author_included(translate_server):
    _post(translate_server["port"], "/translate",
          {"text": "hombre", "book_title": "Blood Meridian", "book_author": "Cormac McCarthy"})
    prompt = translate_server["calls"][0]["prompt"]
    assert "Blood Meridian" in prompt
    assert "Cormac McCarthy" in prompt


# ── refusals: the endpoint must fail loudly, never fabricate ──────────────────

def test_missing_text_is_rejected(translate_server):
    code, body = _post(translate_server["port"], "/translate", {})
    assert code == 400
    assert body["translation"] is None
    assert "Missing text" in body["error"]
    assert translate_server["calls"] == []  # no model call for an invalid request


def test_whitespace_only_text_is_rejected(translate_server):
    code, body = _post(translate_server["port"], "/translate", {"text": "   \n  "})
    assert code == 400
    assert body["translation"] is None
    assert translate_server["calls"] == []


def test_overlong_selection_is_rejected_before_the_model(translate_server):
    """A device sends a word or a phrase; a chapter-sized body is refused."""
    code, body = _post(translate_server["port"], "/translate",
                       {"text": "x" * (server.TRANSLATE_MAX_CHARS + 1)})
    assert code == 413
    assert body["translation"] is None
    assert "too long" in body["error"]
    assert translate_server["calls"] == []  # cost is bounded: no model call


def test_selection_at_the_exact_limit_is_accepted(translate_server):
    code, body = _post(translate_server["port"], "/translate",
                       {"text": "x" * server.TRANSLATE_MAX_CHARS})
    assert code == 200
    assert body["translation"] == "The horse was very tired."


def test_invalid_json_is_rejected(translate_server):
    req = urllib.request.Request(
        f"http://127.0.0.1:{translate_server['port']}/translate",
        data=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    try:
        code = urllib.request.urlopen(req, timeout=10).getcode()
    except urllib.error.HTTPError as e:
        code = e.code
    assert code == 400
    assert translate_server["calls"] == []


# ── model failures must not surface as a blank pane ───────────────────────────

def test_empty_completion_is_an_error_not_a_translation(monkeypatch):
    """A blank model reply must 502, not render as an empty definition pane."""
    monkeypatch.setattr(server, "TOKEN", "")
    monkeypatch.setattr(xray_generator, "_complete",
                        lambda *a, **k: "   \n  ")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, body = _post(port, "/translate", {"text": "hola"})
    finally:
        srv.shutdown()
    assert code == 502
    assert body["translation"] is None
    assert "Empty translation" in body["error"]


def test_model_exception_returns_500_with_reason(monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "")

    def boom(*a, **k):
        raise RuntimeError("bedrock unavailable")

    monkeypatch.setattr(xray_generator, "_complete", boom)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, body = _post(port, "/translate", {"text": "hola"})
    finally:
        srv.shutdown()
    assert code == 500
    assert body["translation"] is None
    assert "bedrock unavailable" in body["error"]


# ── the offline plugin path must stay offline ─────────────────────────────────

def test_plugin_translate_mode_still_has_no_bridge_fallback():
    """AGENTS.md: the plugin's 'Translate to English' never calls the network.

    Adding a bridge /translate endpoint must not tempt main.lua into calling it —
    the offline contract is deliberate. Guard it as a test so a future edit that
    wires the plugin to this endpoint fails here first.
    """
    from pathlib import Path
    main_lua = (Path(__file__).resolve().parents[1]
                / "marginalia.koplugin" / "main.lua").read_text()
    start = main_lua.index('mode_id == "translate"')
    branch = main_lua[start:start + 1200]
    assert "No precomputed translation found" in branch
    for forbidden in ("/translate", "bridge.translate", "Bridge.translate"):
        assert forbidden not in branch, f"plugin translate branch must stay offline ({forbidden})"
