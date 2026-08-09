"""A job must always read the exact bytes it was handed.

Adversarial review flagged that uploads are content-addressed with MD5, so two
distinct EPUBs can be forced onto one path. Probing it showed a real gap: a job
opens its upload TWICE (extract_epub, then the translation build), so a
colliding upload landing between those reads made the second read return a
DIFFERENT book's bytes — a silent content mix, not just a hash-theory concern.

Two independent hardenings, both pinned here:

1. Identity uses SHA-256 (collision-resistant) rather than MD5 for the upload
   path, so an attacker can't steer two payloads onto one name.
2. More importantly, correctness no longer DEPENDS on the digest: each job gets
   its own private file, so even a forced collision cannot cross-contaminate.
   Defence in depth — (2) holds even if (1) were ever weakened.

Note the repo still uses MD5 as the *cache key* (`epub_extract.file_hash`,
`_epub_matches_cache`) and KOReader's partial-MD5 for edition binding. Those are
deliberate compatibility contracts with existing caches and the device, and are
not what these tests cover.
"""

from __future__ import annotations

import hashlib
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


def test_each_job_gets_a_private_upload(tmp_path, monkeypatch):
    """Two jobs uploading identical bytes must NOT share one file.

    Sharing is what allowed one job's teardown (or a colliding write) to affect
    another job's read.
    """
    monkeypatch.setattr(srv, "UPLOADS_DIR", tmp_path / "uploads")
    payload = b"identical-epub-bytes"

    path_a, _ = srv._publish_upload(payload, "job-A")
    path_b, _ = srv._publish_upload(payload, "job-B")

    assert path_a != path_b, "jobs shared one upload path"
    assert open(path_a, "rb").read() == payload
    assert open(path_b, "rb").read() == payload

    # Each job's teardown affects only its own file.
    srv._cleanup_upload(path_a, "job-A")
    assert not os.path.isfile(path_a)
    assert os.path.isfile(path_b), "cleaning up job A destroyed job B's upload"

    srv._cleanup_upload(path_b, "job-B")
    assert not os.path.isfile(path_b)


def test_colliding_digest_cannot_swap_a_job_s_content(tmp_path, monkeypatch):
    """Even a FORCED digest collision must not cross-contaminate two jobs.

    Reproduces the probe that failed before this change: job A opened its path
    after a colliding upload landed and read job B's bytes.
    """
    monkeypatch.setattr(srv, "UPLOADS_DIR", tmp_path / "uploads")
    a, b = b"AAAA-book-one", b"BBBB-book-two-longer"

    class Fixed:
        def __init__(self, *_a, **_k):
            pass

        def hexdigest(self):
            return "f" * 64

        def update(self, *_a, **_k):
            pass

    # Force every payload to the same digest.
    monkeypatch.setattr(srv.hashlib, "sha256", lambda *_a, **_k: Fixed())
    monkeypatch.setattr(srv.hashlib, "md5", lambda *_a, **_k: Fixed())

    path_a, _ = srv._publish_upload(a, "job-A")
    path_b, _ = srv._publish_upload(b, "job-B")

    # A late read by job A (the exposure the probe found) must still see A.
    assert open(path_a, "rb").read() == a, "job A read another job's bytes"
    assert open(path_b, "rb").read() == b, "job B read another job's bytes"


def test_upload_path_uses_a_collision_resistant_digest(tmp_path, monkeypatch):
    """The filename must not be derived from MD5."""
    monkeypatch.setattr(srv, "UPLOADS_DIR", tmp_path / "uploads")
    payload = b"some-epub-bytes"

    path, digest = srv._publish_upload(payload, "job-1")

    assert digest == hashlib.sha256(payload).hexdigest(), "digest is not SHA-256"
    assert hashlib.md5(payload).hexdigest() not in path, "path still derived from MD5"


def test_repeated_upload_by_same_job_is_stable(tmp_path, monkeypatch):
    """A job re-publishing must not accumulate stray files."""
    monkeypatch.setattr(srv, "UPLOADS_DIR", tmp_path / "uploads")
    payload = b"retry-epub"

    first, _ = srv._publish_upload(payload, "job-1")
    second, _ = srv._publish_upload(payload, "job-1")

    assert first == second, "same job should reuse its own private path"
    assert open(second, "rb").read() == payload
    srv._cleanup_upload(second, "job-1")
    assert list((tmp_path / "uploads").iterdir()) == [], "stray files left behind"


# ── job ids must be unique, since they key BOTH job state and the upload ─────

def test_job_ids_do_not_collide_even_when_uuid_repeats(monkeypatch):
    """A truncated uuid4 is only 32 bits — a clash must be retried, not accepted.

    job_id keys `_xray_jobs` AND names the per-job upload, so a duplicate would
    cross-wire two jobs' status and let one job's cleanup delete the other's
    EPUB, recreating the original Blood Meridian race.
    """
    # First two draws collide, third is fresh.
    draws = iter(["dupdup00-x", "dupdup00-x", "unique01-y"])
    monkeypatch.setattr(srv.uuid, "uuid4", lambda: next(draws))

    first = srv._new_job_id()
    second = srv._new_job_id()

    assert first == "dupdup00", first
    assert second == "unique01", "collision was accepted instead of redrawn"
    with srv._jobs_lock:
        assert set(srv._xray_jobs) == {"dupdup00", "unique01"}


def test_new_job_id_registers_the_job(monkeypatch):
    """The id must be claimed atomically, or two callers could draw the same one."""
    job_id = srv._new_job_id()
    with srv._jobs_lock:
        assert job_id in srv._xray_jobs
        assert srv._xray_jobs[job_id]["status"] == "pending"


def test_concurrent_job_id_allocation_is_unique():
    """Hammer the allocator from many threads: no duplicates, none lost."""
    ids: list[str] = []
    lock = threading.Lock()

    def worker():
        jid = srv._new_job_id()
        with lock:
            ids.append(jid)

    threads = [threading.Thread(target=worker) for _ in range(60)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(ids) == 60
    assert len(set(ids)) == 60, "duplicate job ids handed out"


def test_colliding_job_ids_cannot_share_an_upload(tmp_path, monkeypatch):
    """Belt and braces: even if two jobs DID share an id, prove the risk is real.

    Documents why uniqueness is enforced upstream — a shared id means a shared
    upload path, and one job's cleanup destroys the other's EPUB.
    """
    monkeypatch.setattr(srv, "UPLOADS_DIR", tmp_path / "uploads")
    payload = b"same-bytes"

    a, _ = srv._publish_upload(payload, "samejob")
    b, _ = srv._publish_upload(payload, "samejob")
    assert a == b, "same id + same bytes collapses to one path (hence _new_job_id)"

    srv._cleanup_upload(a, "samejob")
    assert not os.path.isfile(b)
