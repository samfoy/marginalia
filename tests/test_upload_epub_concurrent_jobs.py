"""Concurrent uploads of the same book must not delete each other's EPUB.

Sam opened Blood Meridian and got "Book Index upload failed". The bridge had
logged four successful uploads (202) and four successful extractions, so the
transport was never the problem. The real chain was:

1. Uploads are stored at a CONTENT-ADDRESSED path (``{md5}.epub``), so every
   retry of the same book resolves to the SAME file on disk.
2. Each job ended with ``finally: os.unlink(epub_path)``. The first job to
   finish deleted the file out from under the jobs still running, so their
   translation build raised FileNotFoundError and the record was cached with
   ``translation_index: null``.
3. A null index fails ``_translation_index_valid``, so ``/book-index/init``
   answered ``needs_epub`` again, the device re-uploaded, and the race repeated
   until the plugin surfaced "Book Index upload failed".

The upload path is shared state. A job may only delete the EPUB when it is the
last user of it.
"""

from __future__ import annotations

import os

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


def _job(job_id: str) -> None:
    with srv._jobs_lock:
        srv._xray_jobs[job_id] = {"status": "pending", "progress": "Starting",
                                  "record": None, "error": None}


def test_second_job_still_sees_epub_after_first_finishes(tmp_path, monkeypatch,
                                                        minimal_epub_bytes, tmp_cache):
    """A finishing job must never remove the EPUB another job is working on.

    This is the exact Blood Meridian failure. Originally both jobs shared one
    content-addressed file and the first to finish unlinked it. Uploads are now
    per-job, so isolation is structural — assert the OUTCOME (each job keeps a
    readable EPUB for its whole run) rather than the old shared-path mechanism.
    """
    monkeypatch.setattr(srv, "UPLOADS_DIR", tmp_path / "uploads")

    first, second = "aaaaaaaa", "bbbbbbbb"
    _job(first)
    _job(second)

    # Identical bytes — exactly what the plugin's retry loop produces.
    path, _ = srv._publish_upload(minimal_epub_bytes, first)
    path2, _ = srv._publish_upload(minimal_epub_bytes, second)
    assert path != path2, "jobs must not share one upload file"

    monkeypatch.setattr(srv, "extract_epub", lambda p: (_ for _ in ()).throw(
        RuntimeError("stop early — file existence is what matters here")))

    # First job runs to completion (this is what used to nuke the shared file).
    srv._run_xray_job_from_epub(first, path, "Blood Meridian", "Cormac McCarthy", 0)

    assert os.path.isfile(path2), (
        "first job's teardown removed the EPUB the second job was still using"
    )
    assert open(path2, "rb").read() == minimal_epub_bytes, "second job's bytes changed"

    srv._run_xray_job_from_epub(second, path2, "Blood Meridian", "Cormac McCarthy", 0)

    # Both jobs cleaned up after themselves — no disk leak.
    assert not os.path.isfile(path)
    assert not os.path.isfile(path2)
    assert list((tmp_path / "uploads").iterdir()) == [], "uploads leaked"


def test_single_job_still_cleans_up(tmp_path, monkeypatch, minimal_epub_bytes, tmp_cache):
    """The ordinary one-job case must still delete the upload."""
    monkeypatch.setattr(srv, "UPLOADS_DIR", tmp_path / "uploads")

    job_id = "cccccccc"
    _job(job_id)
    path, _ = srv._publish_upload(minimal_epub_bytes, job_id)

    monkeypatch.setattr(srv, "extract_epub", lambda p: (_ for _ in ()).throw(
        RuntimeError("boom")))

    srv._run_xray_job_from_epub(job_id, path, "Book", "Author", 0)

    assert not os.path.isfile(path), "single-job upload was not cleaned up"


def test_untracked_path_is_still_cleaned_up(tmp_path, monkeypatch,
                                            minimal_epub_bytes, tmp_cache):
    """A job whose path was never claimed must not leak the file.

    Defensive: a caller that bypasses _publish_upload must not turn the uploads
    dir into a disk leak.
    """
    epub = tmp_path / "untracked.epub"
    epub.write_bytes(minimal_epub_bytes)
    path = str(epub)

    job_id = "dddddddd"
    _job(job_id)  # deliberately never published/claimed

    monkeypatch.setattr(srv, "extract_epub", lambda p: (_ for _ in ()).throw(
        RuntimeError("boom")))

    srv._run_xray_job_from_epub(job_id, path, "Book", "Author", 0)

    assert not os.path.isfile(path), "untracked upload leaked"
