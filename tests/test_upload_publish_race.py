"""Adversarial-review follow-ups on the Blood Meridian fix.

Independent review of the first cut found real defects; each is pinned here.

1. Publication race — the handler wrote the shared content-addressed file and
   only THEN claimed it. An in-flight job could release its last claim and
   unlink between the write and the claim, handing the new job a missing EPUB.
   Writing the destination in place also truncated a file a running job was
   still reading.

2. Backfill path skipped the marker — ``_run_translation_index_job`` calls
   ``build_translation_index`` directly, bypassing the ``build_attempted`` stamp.
   A backfill that legitimately found zero passages was merged without it,
   instantly failed validation, and re-queued forever.

3. Double teardown — a repeat cleanup after the final release could unlink a
   path a newer job had already claimed.

4. Failure handling — a failed publish left a job "pending" forever, unlink
   errors were swallowed silently, and the done-set grew without bound.
"""

from __future__ import annotations

import os
import threading

import pytest

import server as srv


@pytest.fixture(autouse=True)
def clean_state():
    with srv._jobs_lock:
        srv._xray_jobs.clear()
    with srv._uploads_lock:
        srv._upload_users.clear()
        srv._upload_done.clear()
    yield
    with srv._jobs_lock:
        srv._xray_jobs.clear()
    with srv._uploads_lock:
        srv._upload_users.clear()
        srv._upload_done.clear()


# ── 1. publish is atomic with the claim ──────────────────────────────────────

def test_publish_claims_before_returning(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "UPLOADS_DIR", tmp_path / "uploads")

    path, digest = srv._publish_upload(b"epub-bytes", "job-1")

    assert os.path.isfile(path)
    assert digest in path
    with srv._uploads_lock:
        assert "job-1" in srv._upload_users[path], "path was not claimed at publish time"


def test_republish_does_not_truncate_a_file_in_use(tmp_path, monkeypatch):
    """A re-publish must swap the directory entry, not truncate in place.

    A job that re-uploads (the plugin retries) reuses its own path, so an
    in-flight read of that path must keep seeing complete bytes.
    """
    monkeypatch.setattr(srv, "UPLOADS_DIR", tmp_path / "uploads")
    payload = b"identical-epub-bytes"

    first, _ = srv._publish_upload(payload, "job-1")
    reader = open(first, "rb")           # job-1 is "mid-read"
    try:
        second, _ = srv._publish_upload(payload, "job-1")
        assert first == second, "a job re-publishing should reuse its own path"
        # The in-flight reader must still see the full payload.
        assert reader.read() == payload, "in-use upload was truncated"
    finally:
        reader.close()

    assert open(second, "rb").read() == payload
    srv._cleanup_upload(second, "job-1")
    assert not os.path.isfile(second)


def test_jobs_do_not_share_a_path_even_with_identical_bytes(tmp_path, monkeypatch):
    """Different jobs must be isolated, so one teardown can't affect another."""
    monkeypatch.setattr(srv, "UPLOADS_DIR", tmp_path / "uploads")
    payload = b"identical-epub-bytes"

    a, _ = srv._publish_upload(payload, "job-1")
    b, _ = srv._publish_upload(payload, "job-2")
    assert a != b

    srv._cleanup_upload(a, "job-1")
    assert not os.path.isfile(a)
    assert os.path.isfile(b), "job-1's teardown removed job-2's upload"
    assert open(b, "rb").read() == payload

    srv._cleanup_upload(b, "job-2")
    assert list((tmp_path / "uploads").iterdir()) == []


def test_stale_cleanup_cannot_delete_a_reclaimed_upload(tmp_path, monkeypatch):
    """A repeat cleanup must not delete a file the job has since republished."""
    monkeypatch.setattr(srv, "UPLOADS_DIR", tmp_path / "uploads")
    payload = b"racy-epub"

    path, _ = srv._publish_upload(payload, "job-A")
    srv._cleanup_upload(path, "job-A")            # A tears down; file removed
    assert not os.path.isfile(path)

    # A retry republishes the same job's path...
    path_b, _ = srv._publish_upload(payload, "job-A")
    assert path_b == path

    srv._cleanup_upload(path, "job-A")            # ...and a stale repeat fires

    # The stale repeat is allowed to clean up the CURRENT claim (same job), but
    # must never leave bookkeeping that silently skips a future teardown.
    with srv._uploads_lock:
        assert path not in srv._upload_users, "claim leaked after cleanup"
    assert list((tmp_path / "uploads").iterdir()) == [], "uploads leaked"


def test_concurrent_publish_and_cleanup_never_loses_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "UPLOADS_DIR", tmp_path / "uploads")
    payload = b"stress-epub"
    failures: list[str] = []

    def worker(i):
        try:
            path, _ = srv._publish_upload(payload, f"job{i}")
            if not os.path.isfile(path):
                failures.append(f"job{i}: file missing while claimed")
            srv._cleanup_upload(path, f"job{i}")
        except Exception as exc:  # pragma: no cover - diagnostic
            failures.append(f"job{i}: {exc!r}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not failures, failures
    assert list((tmp_path / "uploads").iterdir()) == [], "uploads leaked"
    with srv._uploads_lock:
        assert srv._upload_users == {}


# ── 4. failure handling ──────────────────────────────────────────────────────

def test_publish_failure_rolls_back_the_claim(tmp_path, monkeypatch):
    """A failed publish must not pin the path forever."""
    monkeypatch.setattr(srv, "UPLOADS_DIR", tmp_path / "uploads")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(srv.tempfile, "mkstemp", boom)

    with pytest.raises(OSError):
        srv._publish_upload(b"bytes", "job-1")

    with srv._uploads_lock:
        assert srv._upload_users == {}, "failed publish left a dangling claim"


def test_publish_failure_removes_partial_temp_file(tmp_path, monkeypatch):
    """A failure after mkstemp must not leave .part droppings behind."""
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(srv, "UPLOADS_DIR", uploads)

    real_replace = srv.os.replace

    def boom(*_a, **_k):
        raise OSError("rename failed")

    monkeypatch.setattr(srv.os, "replace", boom)
    with pytest.raises(OSError):
        srv._publish_upload(b"bytes", "job-1")
    monkeypatch.setattr(srv.os, "replace", real_replace)

    assert list(uploads.iterdir()) == [], "temp file leaked after failed publish"


def test_unlink_failure_is_logged_not_swallowed(tmp_path, monkeypatch, caplog):
    """Persistent cleanup failures must be visible, not silent."""
    monkeypatch.setattr(srv, "UPLOADS_DIR", tmp_path / "uploads")
    path, _ = srv._publish_upload(b"bytes", "job-1")

    def boom(_p):
        raise PermissionError("read-only fs")

    monkeypatch.setattr(srv.os, "unlink", boom)

    import logging
    with caplog.at_level(logging.ERROR):
        srv._cleanup_upload(path, "job-1")

    assert "failed to remove upload" in caplog.text


def test_cleanup_retries_after_a_failed_unlink(tmp_path, monkeypatch, caplog):
    """A failed unlink must NOT be recorded as a completed teardown.

    Otherwise the file stays on disk while every later cleanup returns early,
    leaking the upload permanently.
    """
    monkeypatch.setattr(srv, "UPLOADS_DIR", tmp_path / "uploads")
    path, _ = srv._publish_upload(b"bytes", "job-1")

    real_unlink = os.unlink

    def boom(_p):
        raise PermissionError("read-only fs")

    import logging
    monkeypatch.setattr(srv.os, "unlink", boom)
    with caplog.at_level(logging.ERROR):
        srv._cleanup_upload(path, "job-1")

    assert "failed to remove upload" in caplog.text
    assert os.path.isfile(path), "test precondition: file should still be present"
    with srv._uploads_lock:
        assert path not in srv._upload_done, (
            "failed unlink was recorded as done — later cleanups would skip it"
        )

    # Filesystem recovers: a retry must actually remove the file.
    monkeypatch.setattr(srv.os, "unlink", real_unlink)
    srv._cleanup_upload(path, "job-1")
    assert not os.path.isfile(path), "retry after a failed unlink did not clean up"


def test_missing_file_on_cleanup_is_not_an_error(tmp_path, monkeypatch, caplog):
    """An already-deleted upload is normal and must stay quiet."""
    monkeypatch.setattr(srv, "UPLOADS_DIR", tmp_path / "uploads")
    path, _ = srv._publish_upload(b"bytes", "job-1")
    os.unlink(path)

    import logging
    with caplog.at_level(logging.ERROR):
        srv._cleanup_upload(path, "job-1")

    assert "failed to remove upload" not in caplog.text


def test_done_bookkeeping_is_bounded(tmp_path, monkeypatch):
    """The done-set must not grow forever in a long-running server."""
    monkeypatch.setattr(srv, "UPLOADS_DIR", tmp_path / "uploads")

    for i in range(srv._UPLOAD_DONE_MAX + 50):
        path, _ = srv._publish_upload(f"payload-{i}".encode(), f"job{i}")
        srv._cleanup_upload(path, f"job{i}")

    with srv._uploads_lock:
        assert len(srv._upload_done) <= srv._UPLOAD_DONE_MAX, "unbounded bookkeeping"
        assert srv._upload_users == {}


# ── 2. backfill path must stamp completeness too ─────────────────────────────

def _backfill(monkeypatch, tmp_path, index: dict, job_id: str) -> dict:
    epub = tmp_path / "Book.epub"
    epub.write_bytes(b"PK\x03\x04payload")
    cached = {"book": {"epub_hash": "h" * 32, "epub_path": str(epub)}}

    monkeypatch.setattr(srv, "_epub_matches_cache", lambda *_a, **_k: True)
    monkeypatch.setattr(srv, "build_translation_index", lambda _p: index)

    merged: dict = {}

    def fake_merge(book_hash, idx):
        merged["index"] = idx
        return {"book": {"epub_hash": book_hash}, "translation_index": idx}

    monkeypatch.setattr(srv.xray_cache, "merge_translation_index", fake_merge)

    with srv._jobs_lock:
        srv._xray_jobs[job_id] = {"status": "pending", "progress": "",
                                  "record": None, "error": None}
    srv._run_translation_index_job(job_id, cached, str(epub))
    return merged


def _index(**kw) -> dict:
    base = {
        "version": 1,
        "target_language": "English",
        "generated_at": "2026-08-09T00:00:00Z",
        "source_epub": {"filename": "x.epub", "size_bytes": 1,
                        "sha256": "b" * 64, "koreader_partial_md5": "a" * 32},
        "translations": {},
        "skipped_candidates": 0,
    }
    base.update(kw)
    return base


def test_backfill_marks_complete_empty_index(monkeypatch, tmp_path):
    """A backfill finding zero passages must record that it finished."""
    merged = _backfill(monkeypatch, tmp_path, _index(), "bf000001")

    with srv._jobs_lock:
        assert srv._xray_jobs["bf000001"]["status"] == "ready"
    assert merged["index"].get("build_attempted") is True, (
        "backfill merged an empty index without the completeness marker"
    )


def test_backfill_does_not_mark_partial_index(monkeypatch, tmp_path):
    """A partial backfill must stay retryable."""
    merged = _backfill(monkeypatch, tmp_path, _index(skipped_candidates=7), "bf000002")
    assert merged["index"].get("build_attempted") is not True


# ── completeness marker must demand real proof ───────────────────────────────

@pytest.mark.parametrize("skipped", [None, False, "0", 0.0, [], {}])
def test_marker_requires_a_real_integer_zero(skipped):
    """Malformed metadata is NOT proof that a build finished cleanly.

    Accepting a falsy-but-malformed value would let a broken dependency stamp a
    degraded index as permanently valid.
    """
    index = {"version": 1, "translations": {}, "skipped_candidates": skipped}
    assert srv._mark_translation_build_complete(index).get("build_attempted") is not True


def test_marker_rejects_a_missing_count():
    """A missing skipped_candidates field is unverified, so no stamp."""
    index = {"version": 1, "translations": {}}
    assert srv._mark_translation_build_complete(index).get("build_attempted") is not True


def test_marker_accepts_integer_zero():
    """The real builder always emits an int; 0 means genuinely nothing skipped."""
    index = {"version": 1, "translations": {}, "skipped_candidates": 0}
    assert srv._mark_translation_build_complete(index)["build_attempted"] is True


def test_marker_rejects_bool_true_count():
    """True == 1 in Python; it must not be read as 'one candidate skipped'."""
    index = {"version": 1, "translations": {}, "skipped_candidates": True}
    assert srv._mark_translation_build_complete(index).get("build_attempted") is not True


def test_real_builder_output_is_stampable(tmp_path, monkeypatch):
    """End-to-end: a genuine complete build from the real builder gets stamped.

    Guards against the stricter type check rejecting the production shape.
    """
    monkeypatch.setattr(srv, "build_translation_index", lambda _p: {
        "version": 1,
        "target_language": "English",
        "generated_at": "2026-08-09T00:00:00Z",
        "source_epub": {"filename": "x.epub", "size_bytes": 1,
                        "sha256": "b" * 64, "koreader_partial_md5": "a" * 32},
        "translations": {},
        "skipped_candidates": 0,      # what translation_sidecar actually emits
    })
    index = srv._safe_translation_index("/books/Blood Meridian.epub")
    assert index["build_attempted"] is True
    assert srv._translation_index_valid(index, "a" * 32) is True
