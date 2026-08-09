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
    """The EPUB must survive until the LAST concurrent job is done.

    This is the exact Blood Meridian failure: two jobs share one upload path,
    the first finishes and unlinks, and the second can no longer read the file
    it was given.
    """
    monkeypatch.setattr(srv, "UPLOADS_DIR", tmp_path / "uploads")

    first, second = "aaaaaaaa", "bbbbbbbb"
    _job(first)
    _job(second)

    # Both uploads carry identical bytes, so both resolve to the same path —
    # exactly what the plugin's retry loop produces.
    path, _ = srv._publish_upload(minimal_epub_bytes, first)
    path2, _ = srv._publish_upload(minimal_epub_bytes, second)
    assert path == path2, "identical bytes must share one content-addressed path"

    monkeypatch.setattr(srv, "extract_epub", lambda p: (_ for _ in ()).throw(
        RuntimeError("stop early — file existence is what matters here")))

    # First job runs to completion (and would previously unlink).
    srv._run_xray_job_from_epub(first, path, "Blood Meridian", "Cormac McCarthy", 0)

    assert os.path.isfile(path), (
        "first job deleted the shared upload while a second job was still using it"
    )

    srv._run_xray_job_from_epub(second, path, "Blood Meridian", "Cormac McCarthy", 0)

    # Once the last user is done the file is cleaned up — no disk leak.
    assert not os.path.isfile(path), "upload leaked after the last job finished"


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
