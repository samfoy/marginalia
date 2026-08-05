"""test_server_routes.py — integration tests against a live ThreadingHTTPServer."""

import json
import hashlib
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

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


def test_cached_book_index_serves_translation_index(live_server, monkeypatch):
    import xray_cache as xc

    cached = {
        "book": {"epub_hash": "abc", "title": "Cached Book"},
        "xray": {"characters": []},
        "mentions": {},
        "generated_at": "2026-08-04T00:00:00Z",
        "translation_index": {
            "version": 1,
            "target_language": "English",
            "generated_at": "2026-08-04T00:00:00Z",
            "source_epub": {
                "filename": "Cached.epub", "size_bytes": 1,
                "sha256": "b" * 64, "koreader_partial_md5": "a" * 32,
            },
            "translations": {"deadbeef": {
                "normalized_source": "bonjour", "original_source": "Bonjour",
                "source_language": "French", "translation": "hello",
            }},
        },
    }
    monkeypatch.setattr(xc, "find_by_title_author", lambda *_args: cached)
    monkeypatch.setattr(xc, "find_all_by_title_author", lambda *_args: [cached])

    code, body = _post(live_server["port"], "/book-index/init", {"book_title": "Cached Book"})

    assert code == 200
    assert body["translation_index"] == cached["translation_index"]


def test_force_reindex_bypasses_the_cache_entirely(live_server, monkeypatch):
    """Reindex must rebuild, not re-serve the record that was already wrong.

    Sam reindexed Lolita repeatedly and kept getting a record with no
    translations: the plugin sent force=true but the bridge ignored it and
    answered from the same cached record every time.
    """
    import xray_cache as xc

    lookups = []
    monkeypatch.setattr(xc, "find_by_title_author",
                        lambda *args: lookups.append("one") or None)
    monkeypatch.setattr(xc, "find_all_by_title_author",
                        lambda *args: lookups.append("all") or [])
    monkeypatch.setattr(server, "find_epub", lambda *args: None)

    code, body = _post(live_server["port"], "/book-index/init",
                       {"book_title": "Lolita", "force": True})

    assert code == 200
    assert body["status"] == "needs_epub"
    assert lookups == [], f"force still consulted the cache: {lookups}"


def test_cached_translation_index_with_no_entries_is_rebuilt(live_server, monkeypatch):
    """An empty index is indistinguishable from a failed build.

    Serving it as complete made the failure permanent, because the bridge kept
    answering from cache and never retried.
    """
    import xray_cache as xc

    cached = {
        "book": {"epub_hash": "empty", "title": "Lolita", "epub_path": "/missing.epub"},
        "xray": {"characters": []},
        "mentions": {},
        "strategy": "epub_text",
        "generated_at": "2026-08-04T00:00:00Z",
        "translation_index": {
            "version": 1,
            "target_language": "English",
            "generated_at": "2026-08-04T00:00:00Z",
            "source_epub": {
                "filename": "Lolita.epub", "size_bytes": 1,
                "sha256": "b" * 64, "koreader_partial_md5": "a" * 32,
            },
            "translations": {},
        },
    }
    monkeypatch.setattr(xc, "find_by_title_author", lambda *_args: cached)
    monkeypatch.setattr(xc, "find_all_by_title_author", lambda *_args: [cached])
    monkeypatch.setattr(xc, "update_reading_pct", lambda *_args: None)
    monkeypatch.setattr(server, "find_epub", lambda *_args: None)

    code, body = _post(live_server["port"], "/book-index/init",
                       {"book_title": "Lolita", "device_partial_md5": "a" * 32})

    assert code == 200
    assert body["status"] != "ready", "empty index served as a complete one"


def test_device_hash_selects_matching_cached_edition(live_server, monkeypatch):
    import xray_cache as xc

    def record(name, partial):
        return {
            "book": {"epub_hash": name, "title": "Same Title"},
            "xray": {"characters": []},
            "mentions": {},
            "generated_at": "2026-08-04T00:00:00Z",
            "translation_index": {
                "version": 1,
                "target_language": "English",
                "generated_at": "2026-08-04T00:00:00Z",
                "source_epub": {
                    "filename": name + ".epub", "size_bytes": 1,
                    "sha256": "a" * 64, "koreader_partial_md5": partial,
                },
                # A non-empty index: this test is about picking the edition that
                # matches the device, and an empty index is deliberately treated
                # as incomplete elsewhere.
                "translations": {
                    "abcd1234": {
                        "normalized_source": "bonjour",
                        "original_source": "Bonjour",
                        "source_language": "French",
                        "translation": "hello",
                    }
                },
            },
        }

    first = record("first", "1" * 32)
    second = record("second", "2" * 32)
    monkeypatch.setattr(xc, "find_all_by_title_author", lambda *_args: [first, second])

    code, body = _post(live_server["port"], "/book-index/init", {
        "book_title": "Same Title",
        "device_partial_md5": "2" * 32,
    })

    assert code == 200
    assert body["book"]["epub_hash"] == "second"


def test_device_hash_selects_matching_legacy_epub_path(live_server, tmp_path, monkeypatch):
    import xray_cache as xc

    first_epub = tmp_path / "First.epub"
    second_epub = tmp_path / "Second.epub"
    first_epub.write_bytes(b"first edition")
    second_epub.write_bytes(b"second edition")
    records = [
        {"book": {"epub_hash": hashlib.md5(first_epub.read_bytes()).hexdigest(), "title": "Same", "epub_path": str(first_epub)}, "strategy": "test", "xray": {}},
        {"book": {"epub_hash": hashlib.md5(second_epub.read_bytes()).hexdigest(), "title": "Same", "epub_path": str(second_epub)}, "strategy": "test", "xray": {}},
    ]
    monkeypatch.setattr(xc, "find_all_by_title_author", lambda *_args: records)
    with server._jobs_lock:
        server._xray_jobs["matching-edition"] = {
            "kind": "translations", "book_hash": records[1]["book"]["epub_hash"],
            "status": "pending", "record": None, "error": None,
        }

    code, body = _post(live_server["port"], "/book-index/init", {
        "book_title": "Same",
        "device_partial_md5": server._koreader_partial_md5(second_epub),
    })

    assert code == 202
    assert body["job_id"] == "matching-edition"
    with server._jobs_lock:
        server._xray_jobs.pop("matching-edition", None)


def test_invalid_cached_translation_index_is_not_returned_current(live_server, tmp_path, monkeypatch):
    import xray_cache as xc

    epub = tmp_path / "Book.epub"
    epub.write_bytes(b"book")
    cached = {
        "book": {"epub_hash": hashlib.md5(epub.read_bytes()).hexdigest(), "title": "Book", "epub_path": str(epub)},
        "strategy": "test",
        "xray": {"characters": []},
        "generated_at": "2026-08-04T00:00:00Z",
        "translation_index": {"version": 99},
    }
    monkeypatch.setattr(xc, "find_all_by_title_author", lambda *_args: [cached])
    with server._jobs_lock:
        server._xray_jobs["deferred-invalid-index"] = {
            "kind": "translations",
            "book_hash": cached["book"]["epub_hash"],
            "status": "pending",
            "record": None,
            "error": None,
        }

    code, body = _post(live_server["port"], "/book-index/init", {
        "book_title": "Book",
        "device_generated_at": "9999-01-01T00:00:00Z",
    })

    assert code == 202
    assert body["status"] == "generating"
    with server._jobs_lock:
        server._xray_jobs.pop(body["job_id"], None)


def test_legacy_cached_book_backfills_translation_index(live_server, tmp_path, monkeypatch):
    import server as srv
    import xray_cache as xc

    epub = tmp_path / "Legacy.epub"
    epub.write_bytes(b"epub")
    book_hash = hashlib.md5(epub.read_bytes()).hexdigest()
    cached = {
        "book": {"epub_hash": book_hash, "title": "Legacy", "epub_path": str(epub)},
        "xray": {"characters": []},
        "mentions": {},
        "generated_at": "2026-08-04T00:00:00Z",
    }
    saved = []
    monkeypatch.setattr(xc, "find_by_title_author", lambda *_args: cached)
    monkeypatch.setattr(xc, "find_all_by_title_author", lambda *_args: [cached])
    latest = {**cached, "last_reading_pct": 73}
    def merge(book_hash, translation_index):
        record = {**latest, "translation_index": translation_index}
        saved.append((book_hash, record))
        return record
    monkeypatch.setattr(xc, "merge_translation_index", merge)
    monkeypatch.setattr(srv, "build_translation_index", lambda _path: {
        "version": 1,
        "target_language": "English",
        "source_epub": {"koreader_partial_md5": srv._koreader_partial_md5(epub)},
        "translations": {},
    })

    code, body = _post(live_server["port"], "/book-index/init", {"book_title": "Legacy"})
    assert code == 202
    for _ in range(50):
        code, raw_status = _get(live_server["port"], body["poll_url"])
        status = json.loads(raw_status)
        if status["status"] == "ready":
            break
        time.sleep(0.01)
    assert code == 200
    assert status["status"] == "ready"
    assert status["translation_index"]["version"] == 1
    assert status["generated_at"] >= cached["generated_at"]
    assert saved[0][0] == book_hash
    assert saved[0][1]["translation_index"]["version"] == 1
    assert saved[0][1]["last_reading_pct"] == 73


def test_legacy_cache_rejects_wrong_server_epub(live_server, tmp_path, monkeypatch):
    import xray_cache as xc

    wrong_epub = tmp_path / "Wrong.edition.epub"
    wrong_epub.write_bytes(b"different edition")
    cached = {
        "book": {"epub_hash": hashlib.md5(b"device edition").hexdigest(), "title": "Same Title", "epub_path": str(wrong_epub)},
        "xray": {"characters": []},
        "generated_at": "2026-08-04T00:00:00Z",
    }
    monkeypatch.setattr(xc, "find_by_title_author", lambda *_args: cached)
    monkeypatch.setattr(xc, "find_all_by_title_author", lambda *_args: [cached])
    monkeypatch.setattr(server, "find_epub", lambda *_args: {"epub_path": str(wrong_epub)})

    code, body = _post(live_server["port"], "/book-index/init", {
        "book_title": "Same Title",
        "device_partial_md5": hashlib.md5(b"device edition").hexdigest(),
    })

    assert code == 200
    assert body["status"] == "needs_epub"
    assert "translation_index" not in cached


def test_cache_miss_rejects_different_calibre_edition(live_server, tmp_path, monkeypatch):
    import xray_cache as xc

    calibre_epub = tmp_path / "Calibre.epub"
    calibre_epub.write_bytes(b"calibre edition")
    monkeypatch.setattr(xc, "find_all_by_title_author", lambda *_args: [])
    monkeypatch.setattr(server, "find_epub", lambda *_args: {"epub_path": str(calibre_epub)})

    code, body = _post(live_server["port"], "/book-index/init", {
        "book_title": "Same Title",
        "device_partial_md5": hashlib.md5(b"device edition").hexdigest(),
    })

    assert code == 200
    assert body["status"] == "needs_epub"


def test_translation_job_claim_is_atomic():
    import server as srv

    book_hash = "atomic-translation-job"
    with srv._jobs_lock:
        srv._xray_jobs.clear()
    with ThreadPoolExecutor(max_workers=12) as executor:
        claims = list(executor.map(lambda _: srv._claim_translation_job(book_hash), range(24)))

    assert sum(created for _job_id, created in claims) == 1
    assert len({job_id for job_id, _created in claims}) == 1
    with srv._jobs_lock:
        srv._xray_jobs.clear()


def test_epub_job_survives_translation_generation_failure(tmp_path, monkeypatch):
    """Translations are an enhancement, not a precondition.

    Previously a failing translation build aborted the whole job, so a book
    whose translation generation hit one bad batch got no Book Index at all and
    the device reported no translations. The Book Index must now still be
    produced and cached, with translation_index left as None.
    """
    import server as srv

    epub = tmp_path / "Book.epub"
    epub.write_bytes(b"epub")
    job_id = "translation-failure"
    with srv._jobs_lock:
        srv._xray_jobs[job_id] = {"status": "pending", "record": None, "error": None}

    content = SimpleNamespace(
        title="Book", author="Author", series=None, series_index=None,
        total_chars=4, chapters=[], file_hash="hash", epub_path=str(epub),
    )
    monkeypatch.setattr(srv, "extract_epub", lambda _path: content)
    monkeypatch.setattr(srv, "generate", lambda _content: ({"characters": []}, "test"))
    monkeypatch.setattr(srv.series, "resolve", lambda **_kwargs: None)
    monkeypatch.setattr(srv.mentions, "build_mentions", lambda *_args: {})
    monkeypatch.setattr(srv, "build_record", lambda *_args: {"book": {"epub_hash": "hash"}, "xray": {}})
    monkeypatch.setattr(srv, "build_translation_index", lambda _path: (_ for _ in ()).throw(RuntimeError("translation failed")))
    saved = []
    removed = []
    monkeypatch.setattr(srv.xray_cache, "save", lambda *_args: saved.append(True))
    monkeypatch.setattr(srv.xray_cache, "remove_knowledge_by_title", lambda *args: removed.append(args))

    srv._run_xray_job_from_epub(job_id, str(epub), "Book", "Author", 0)

    with srv._jobs_lock:
        job = srv._xray_jobs.pop(job_id)
    assert job["status"] == "ready"
    assert job["error"] is None
    assert job["record"]["translation_index"] is None
    assert saved == [True]


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
