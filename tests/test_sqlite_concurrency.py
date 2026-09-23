"""Deterministic SQLite regressions for operations that require write serialization."""

import io
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import event

from app import ingestion, main
from app.db import SessionLocal, engine
from app.models import Document, KnowledgeBase, User


def test_tombstone_cleanup_cannot_erase_a_restored_document(client, knowledge_base, ready_document):
    """Pause cleanup after its read; a concurrent restore must wait for its commit.

    SQLite ignores SELECT FOR UPDATE. Without an early write transaction, the
    restore commits a new path and cleanup subsequently overwrites it with an
    empty string using the previously loaded tombstone.
    """
    assert engine.dialect.name == "sqlite"
    document_id = ready_document["document_id"]
    job_id = ready_document["job_id"]
    with SessionLocal.begin() as session:
        document = session.get(Document, document_id)
        kb = session.get(KnowledgeBase, knowledge_base["id"])
        user = session.get(User, kb.owner_id)
        session.expunge(user)
        original_path = Path(document.storage_path)
        content = original_path.read_bytes()
        original_version = document.version
        main.tombstone_document(session, document)

    cleanup_paused = threading.Event()
    release_cleanup = threading.Event()
    restore_started = threading.Event()
    restore_finished = threading.Event()
    cleanup_thread_id = None

    def pause_cleanup_before_chunk_delete(connection, cursor, statement, parameters, context, executemany):
        if (
            threading.get_ident() == cleanup_thread_id
            and statement.startswith("DELETE FROM document_chunks")
            and not cleanup_paused.is_set()
        ):
            cleanup_paused.set()
            assert release_cleanup.wait(10), "Test failed to release cleanup"

    def cleanup():
        nonlocal cleanup_thread_id
        cleanup_thread_id = threading.get_ident()
        ingestion.cleanup_document(document_id)

    def restore():
        restore_started.set()
        try:
            with SessionLocal() as session:
                return main.upload_document(
                    knowledge_base["id"],
                    UploadFile(filename="policy.txt", file=io.BytesIO(content)),
                    session,
                    user,
                )
        finally:
            restore_finished.set()

    event.listen(engine, "before_cursor_execute", pause_cleanup_before_chunk_delete)
    try:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sqlite-race") as pool:
            cleanup_future = pool.submit(cleanup)
            try:
                assert cleanup_paused.wait(10), "Cleanup did not reach the controlled pause"
                restore_future = pool.submit(restore)
                assert restore_started.wait(10)
                assert not restore_finished.wait(0.2), "Restore bypassed cleanup's write lock"
            finally:
                # Always release the worker, including when the regression recurs.
                release_cleanup.set()
            cleanup_future.result(timeout=10)
            restored = restore_future.result(timeout=10)
    finally:
        event.remove(engine, "before_cursor_execute", pause_cleanup_before_chunk_delete)

    assert restored["document_id"] == document_id
    assert restored["deduplicated"] is False
    with SessionLocal() as session:
        document = session.get(Document, document_id)
        assert document.version == original_version + 1
        assert document.deleted_at is None
        assert document.status == "queued"
        assert document.storage_path
        assert Path(document.storage_path).read_bytes() == content
    assert not original_path.exists()
    assert ingestion.process_document(job_id) == "succeeded"
    with SessionLocal() as session:
        document = session.get(Document, document_id)
        assert document.status == "ready"
        assert document.chunk_count > 0
