"""Exercise task redelivery, retry limits, and commit-time lease ownership."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

from sqlalchemy import func, select

from app import ingestion
from app.config import get_settings
from app.db import SessionLocal
from app.models import Document, DocumentChunk, IngestionJob, new_id, utcnow
from app.rag import TransientProviderError


def chunk_count(document_id):
    with SessionLocal() as session:
        return session.scalar(
            select(func.count()).select_from(DocumentChunk).where(DocumentChunk.document_id == document_id)
        )


def test_queued_to_processing_to_ready_is_visible(uploaded_document, monkeypatch):
    real_embed = ingestion.embed_texts
    observed_states = []

    def observe_processing(texts, settings):
        with SessionLocal() as session:
            job = session.get(IngestionJob, uploaded_document["job_id"])
            document = session.get(Document, uploaded_document["document_id"])
            observed_states.append((job.status, document.status, bool(job.lease_token)))
        return real_embed(texts, settings)

    with SessionLocal() as session:
        assert session.get(IngestionJob, uploaded_document["job_id"]).status == "queued"
        assert session.get(Document, uploaded_document["document_id"]).status == "queued"
    monkeypatch.setattr(ingestion, "embed_texts", observe_processing)
    ingestion.process_document(uploaded_document["job_id"])
    assert observed_states == [("processing", "processing", True)]
    with SessionLocal() as session:
        assert session.get(IngestionJob, uploaded_document["job_id"]).status == "succeeded"
        document = session.get(Document, uploaded_document["document_id"])
        assert document.status == "ready"
        assert document.chunk_count > 0


def test_successful_job_redelivery_does_not_duplicate_chunks(ready_document):
    document_id, job_id = ready_document["document_id"], ready_document["job_id"]
    with SessionLocal() as session:
        original_ids = list(
            session.scalars(select(DocumentChunk.id).where(DocumentChunk.document_id == document_id))
        )
        original_attempts = session.get(IngestionJob, job_id).attempts
    ingestion.process_document(job_id)
    ingestion.process_document(job_id)
    with SessionLocal() as session:
        assert (
            list(session.scalars(select(DocumentChunk.id).where(DocumentChunk.document_id == document_id)))
            == original_ids
        )
        assert session.get(IngestionJob, job_id).attempts == original_attempts


def test_active_lease_blocks_second_worker(uploaded_document, monkeypatch):
    lease = new_id()
    with SessionLocal() as session:
        job = session.get(IngestionJob, uploaded_document["job_id"])
        job.status, job.lease_token, job.attempts = "processing", lease, 1
        job.started_at = job.updated_at = utcnow()
        session.commit()

    def should_not_embed(*args, **kwargs):
        raise AssertionError("Second worker must not process an active lease")

    monkeypatch.setattr(ingestion, "embed_texts", should_not_embed)
    ingestion.process_document(uploaded_document["job_id"])
    assert chunk_count(uploaded_document["document_id"]) == 0
    with SessionLocal() as session:
        job = session.get(IngestionJob, uploaded_document["job_id"])
        assert job.lease_token == lease
        assert job.attempts == 1


def test_worker_cannot_commit_after_losing_lease(uploaded_document, monkeypatch):
    real_embed = ingestion.embed_texts
    replacement_lease = new_id()

    def lose_lease(texts, settings):
        with SessionLocal() as session:
            job = session.get(IngestionJob, uploaded_document["job_id"])
            job.lease_token = replacement_lease
            session.commit()
        return real_embed(texts, settings)

    monkeypatch.setattr(ingestion, "embed_texts", lose_lease)
    ingestion.process_document(uploaded_document["job_id"])
    assert chunk_count(uploaded_document["document_id"]) == 0
    with SessionLocal() as session:
        assert session.get(Document, uploaded_document["document_id"]).status != "ready"
        assert session.get(IngestionJob, uploaded_document["job_id"]).lease_token == replacement_lease


def test_document_deleted_during_embedding_cannot_reappear(client, headers, uploaded_document, monkeypatch):
    real_embed = ingestion.embed_texts

    def delete_before_commit(texts, settings):
        response = client.delete(f"/api/v1/documents/{uploaded_document['document_id']}", headers=headers)
        assert response.status_code == 204
        return real_embed(texts, settings)

    monkeypatch.setattr(ingestion, "embed_texts", delete_before_commit)
    ingestion.process_document(uploaded_document["job_id"])
    assert chunk_count(uploaded_document["document_id"]) == 0
    with SessionLocal() as session:
        document = session.get(Document, uploaded_document["document_id"])
        assert document.deleted_at is not None
        assert document.status != "ready"


def test_transient_failure_retries_are_bounded_and_sanitized(uploaded_document, monkeypatch):
    def upstream_failure(*args, **kwargs):
        raise TransientProviderError("secret-provider-body api_key=DO-NOT-EXPOSE")

    monkeypatch.setattr(ingestion, "embed_texts", upstream_failure)
    for attempt in range(1, get_settings().job_max_attempts + 1):
        ingestion.process_document(uploaded_document["job_id"])
        with SessionLocal() as session:
            job = session.get(IngestionJob, uploaded_document["job_id"])
            assert job.attempts == attempt
            assert job.status == ("failed" if attempt == get_settings().job_max_attempts else "queued")
            assert "DO-NOT-EXPOSE" not in (job.error or "")
    assert chunk_count(uploaded_document["document_id"]) == 0
    ingestion.process_document(uploaded_document["job_id"])
    with SessionLocal() as session:
        assert (
            session.get(IngestionJob, uploaded_document["job_id"]).attempts == get_settings().job_max_attempts
        )


def test_expired_job_recovery_invalidates_old_lease(uploaded_document):
    old_lease = new_id()
    with SessionLocal() as session:
        job = session.get(IngestionJob, uploaded_document["job_id"])
        job.status, job.lease_token, job.attempts = "processing", old_lease, 1
        job.started_at = job.updated_at = utcnow() - timedelta(seconds=get_settings().job_lease_seconds + 1)
        session.commit()
    recovered = ingestion.recover_stale_jobs()
    assert uploaded_document["job_id"] in recovered
    with SessionLocal() as session:
        job = session.get(IngestionJob, uploaded_document["job_id"])
        assert job.status == "queued"
        assert job.lease_token != old_lease
    ingestion.process_document(uploaded_document["job_id"])
    with SessionLocal() as session:
        assert session.get(IngestionJob, uploaded_document["job_id"]).status == "succeeded"


def test_expired_job_at_attempt_limit_is_failed(uploaded_document):
    with SessionLocal() as session:
        job = session.get(IngestionJob, uploaded_document["job_id"])
        job.status, job.lease_token, job.attempts = "processing", new_id(), get_settings().job_max_attempts
        job.started_at = job.updated_at = utcnow() - timedelta(seconds=get_settings().job_lease_seconds + 1)
        session.commit()
    assert uploaded_document["job_id"] not in ingestion.recover_stale_jobs()
    with SessionLocal() as session:
        assert session.get(IngestionJob, uploaded_document["job_id"]).status == "failed"


def test_concurrent_delivery_only_one_worker_embeds(uploaded_document, monkeypatch):
    """Hold the first worker mid-call and redeliver the same job concurrently."""
    entered, release = Event(), Event()
    real_embed = ingestion.embed_texts
    calls = []

    def paused_embed(texts, settings):
        calls.append(len(texts))
        entered.set()
        if not release.wait(timeout=10):
            raise RuntimeError("Test did not release embedding worker")
        return real_embed(texts, settings)

    monkeypatch.setattr(ingestion, "embed_texts", paused_embed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(ingestion.process_document, uploaded_document["job_id"])
        try:
            assert entered.wait(timeout=5), "First worker never reached embedding"
            duplicate = pool.submit(ingestion.process_document, uploaded_document["job_id"])
            assert duplicate.result(timeout=5) == "ignored"
        finally:
            release.set()
        assert first.result(timeout=5) == "succeeded"
    assert len(calls) == 1
    assert chunk_count(uploaded_document["document_id"]) == 1
    with SessionLocal() as session:
        assert session.get(IngestionJob, uploaded_document["job_id"]).attempts == 1


def test_replaced_document_version_rejects_old_worker(uploaded_document, monkeypatch):
    real_embed = ingestion.embed_texts

    def replace_version(texts, settings):
        with SessionLocal.begin() as session:
            document = session.get(Document, uploaded_document["document_id"])
            document.version += 1
        return real_embed(texts, settings)

    monkeypatch.setattr(ingestion, "embed_texts", replace_version)
    assert ingestion.process_document(uploaded_document["job_id"]) == "cancelled"
    assert chunk_count(uploaded_document["document_id"]) == 0
    with SessionLocal() as session:
        assert session.get(Document, uploaded_document["document_id"]).status != "ready"


def test_storage_path_cannot_escape_upload_root(uploaded_document, tmp_path):
    unrelated = tmp_path / "private.txt"
    unrelated.write_text("Private information that must not be parsed", encoding="utf-8")
    with SessionLocal.begin() as session:
        session.get(Document, uploaded_document["document_id"]).storage_path = str(unrelated)
    assert ingestion.process_document(uploaded_document["job_id"]) == "failed"
    assert chunk_count(uploaded_document["document_id"]) == 0
    assert unrelated.exists()
    with SessionLocal() as session:
        error = session.get(IngestionJob, uploaded_document["job_id"]).error
        assert str(tmp_path) not in error
        assert "Private information" not in error


def test_deleted_knowledge_base_during_embedding_cannot_publish(
    client, headers, knowledge_base, uploaded_document, monkeypatch
):
    real_embed = ingestion.embed_texts

    def delete_kb_before_commit(texts, settings):
        assert (
            client.delete(f"/api/v1/knowledge-bases/{knowledge_base['id']}", headers=headers).status_code
            == 204
        )
        return real_embed(texts, settings)

    monkeypatch.setattr(ingestion, "embed_texts", delete_kb_before_commit)
    assert ingestion.process_document(uploaded_document["job_id"]) in {"ignored", "cancelled"}
    assert chunk_count(uploaded_document["document_id"]) == 0
    assert (
        client.get(f"/api/v1/documents/{uploaded_document['document_id']}", headers=headers).status_code
        == 404
    )
