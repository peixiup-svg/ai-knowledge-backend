"""Leased, idempotent ingestion with a transactional final commit.

Parsing and embedding happen outside a DB transaction. A lease token fences old
workers: only the worker that still owns the token may publish its chunks.
"""

import logging
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, or_, select, update

from app.config import get_settings
from app.db import SessionLocal, write_session
from app.models import Document, DocumentChunk, IngestionJob, new_id, utcnow
from app.rag import (
    PermanentDocumentError,
    TransientProviderError,
    embed_texts,
    embedding_fingerprint,
    parse_and_chunk,
)

logger = logging.getLogger(__name__)


def safe_storage_path(path: str) -> Path:
    root = get_settings().storage_dir.resolve()
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise ValueError("Stored file path is outside STORAGE_DIR")
    return resolved


def cleanup_document(document_id: str) -> None:
    """Idempotent tombstone cleanup. Lock document first, as in final publication."""
    with write_session() as session:
        document = session.scalar(select(Document).where(Document.id == document_id).with_for_update())
        if document is None or document.deleted_at is None:
            return
        session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
        if document.storage_path:
            try:
                safe_storage_path(document.storage_path).unlink(missing_ok=True)
                # Empty path is the cleanup completion marker, so old tombstones
                # do not starve pending cleanup in the bounded recovery scan.
                document.storage_path = ""
            except (OSError, ValueError):
                logger.warning("document_cleanup_deferred document_id=%s", document_id)
        document.chunk_count = 0


def process_document(job_id: str) -> str:
    settings = get_settings()
    token = new_id()
    now = utcnow()
    cutoff = now - timedelta(seconds=settings.job_lease_seconds)
    with write_session() as session:
        document_id = session.scalar(select(IngestionJob.document_id).where(IngestionJob.id == job_id))
        if document_id is None:
            return "ignored"
        # All paths lock document -> job (including delete and recovery).
        document = session.scalar(select(Document).where(Document.id == document_id).with_for_update())
        claimed = session.execute(
            update(IngestionJob)
            .where(
                IngestionJob.id == job_id,
                IngestionJob.attempts < settings.job_max_attempts,
                or_(
                    IngestionJob.status == "queued",
                    (IngestionJob.status == "processing") & (IngestionJob.started_at < cutoff),
                ),
            )
            .values(
                status="processing",
                lease_token=token,
                attempts=IngestionJob.attempts + 1,
                started_at=now,
                updated_at=now,
                error=None,
            )
        ).rowcount
        if not claimed:
            return "ignored"
        job = session.get(IngestionJob, job_id)
        if document is None or document.deleted_at is not None:
            job.status, job.lease_token = "cancelled", None
            return "cancelled"
        document.status = "processing"
        document_id, version = document.id, document.version
        path, filename = document.storage_path, document.filename

    try:
        chunks = parse_and_chunk(safe_storage_path(path), filename, settings)
        if not chunks:
            raise PermanentDocumentError("文档中没有可检索的文字。")
        vectors = embed_texts([chunk.content for chunk in chunks], settings)
        if len(vectors) != len(chunks):
            raise PermanentDocumentError("向量数量与文档分块数量不一致。")
        with write_session() as session:
            # Row locks serialize publishing with deletion/restoring. The token
            # prevents a stale worker from overwriting a newer attempt's output.
            document = session.scalar(select(Document).where(Document.id == document_id).with_for_update())
            job = session.scalar(select(IngestionJob).where(IngestionJob.id == job_id).with_for_update())
            if job is None or job.lease_token != token or job.status != "processing":
                return "ignored"
            if document is None or document.deleted_at is not None or document.version != version:
                job.status, job.lease_token, job.updated_at = "cancelled", None, utcnow()
                return "cancelled"
            session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
            session.add_all(
                [
                    DocumentChunk(
                        document_id=document_id,
                        knowledge_base_id=document.knowledge_base_id,
                        version=version,
                        chunk_index=chunk.chunk_index,
                        page_number=chunk.page_number,
                        content=chunk.content,
                        embedding=vector,
                    )
                    for chunk, vector in zip(chunks, vectors, strict=True)
                ]
            )
            document.status, document.chunk_count = "ready", len(chunks)
            document.embedding_fingerprint = embedding_fingerprint(settings)
            job.status, job.lease_token, job.updated_at = "succeeded", None, utcnow()
        logger.info("ingestion_succeeded job_id=%s chunks=%d", job_id, len(chunks))
        return "succeeded"
    except Exception as exc:
        transient = isinstance(exc, TransientProviderError)
        if transient:
            safe_error = "向量服务暂不可用或超时，系统将按次数上限重试。"
        elif isinstance(exc, PermanentDocumentError):
            safe_error = "文档内容或向量配置无效。请检查 UTF-8 编码、PDF 文字层、长度限制与向量维度。"
        elif isinstance(exc, FileNotFoundError):
            safe_error = "原始文件缺失，请删除文档后重新上传。"
        else:
            safe_error = "文档处理失败，请检查服务配置后重试。"
        # Log type/IDs only; never upstream bodies, keys or document text.
        logger.warning("ingestion_failed job_id=%s error_type=%s", job_id, type(exc).__name__)
        with write_session() as session:
            document = session.scalar(select(Document).where(Document.id == document_id).with_for_update())
            job = session.scalar(select(IngestionJob).where(IngestionJob.id == job_id).with_for_update())
            if job is None or job.lease_token != token or job.status != "processing":
                return "ignored"
            if document is None or document.deleted_at is not None or document.version != version:
                job.status, job.lease_token, job.updated_at = "cancelled", None, utcnow()
                return "cancelled"
            status = "queued" if transient and job.attempts < settings.job_max_attempts else "failed"
            job.status, job.error, job.lease_token, job.updated_at = status, safe_error, None, utcnow()
            document.status = status
            return status


def recover_stale_jobs() -> list[str]:
    """Requeue abandoned leases, cap attempts, and retry tombstone cleanup."""
    settings = get_settings()
    cutoff = utcnow() - timedelta(seconds=settings.job_lease_seconds)
    ready: list[str] = []
    recoverable = or_(
        IngestionJob.status == "queued",
        (IngestionJob.status == "processing") & (IngestionJob.started_at < cutoff),
    )
    with SessionLocal() as session:
        candidates = session.execute(
            select(IngestionJob.id, IngestionJob.document_id)
            .where(
                or_(
                    IngestionJob.status == "queued",
                    (IngestionJob.status == "processing") & (IngestionJob.started_at < cutoff),
                )
            )
            .order_by(IngestionJob.updated_at, IngestionJob.id)
            .limit(1000)
        ).all()
    for job_id, document_id in candidates:
        with write_session() as session:
            document = session.scalar(select(Document).where(Document.id == document_id).with_for_update())
            job = session.scalar(
                select(IngestionJob).where(IngestionJob.id == job_id, recoverable).with_for_update()
            )
            if job is None:
                continue
            if document is None or document.deleted_at is not None:
                job.status, job.lease_token, job.updated_at = "cancelled", None, utcnow()
                continue
            if job.attempts >= settings.job_max_attempts:
                job.status, job.lease_token, job.updated_at = "failed", None, utcnow()
                job.error = "处理次数已达上限，请检查后手动重试。"
                document.status = "failed"
                continue
            # Rotate scan order even for pending messages if publishing keeps
            # failing, so a large backlog is not permanently starved.
            job.status, job.lease_token, job.updated_at = "queued", None, utcnow()
            document.status = "queued"
            ready.append(job.id)
    with SessionLocal() as session:
        deleted = session.scalars(
            select(Document.id)
            .where(
                Document.deleted_at.is_not(None), (Document.storage_path != "") | (Document.chunk_count > 0)
            )
            .order_by(Document.deleted_at, Document.id)
            .limit(1000)
        ).all()
    for document_id in deleted:
        cleanup_document(document_id)
    return ready
