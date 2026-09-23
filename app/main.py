import asyncio
import hashlib
import json
import logging
import re
import time
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from typing import Annotated

import anyio
import redis
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.db import SessionLocal, begin_write, get_db, init_db, write_session
from app.ingestion import cleanup_document, recover_stale_jobs, safe_storage_path
from app.limiting import limiter
from app.models import (
    Conversation,
    Document,
    DocumentChunk,
    IngestionJob,
    KnowledgeBase,
    LLMCall,
    Message,
    User,
    new_id,
    utcnow,
)
from app.providers import GenerationError, stream_answer
from app.rag import PermanentDocumentError, TransientProviderError, build_citations, retrieve
from app.schemas import (
    ChatIn,
    Credentials,
    DocumentOut,
    JobOut,
    KnowledgeBaseIn,
    KnowledgeBaseOut,
    SearchIn,
    UserOut,
)
from app.security import create_access_token, current_user, hash_password, verify_password
from app.tasks import dispatch_job, shutdown_local_workers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("knowledge.api")
settings = get_settings()


class BodyLimitMiddleware:
    """Enforce size before multipart spooling, including chunked requests."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = get_settings().max_upload_bytes + 64 * 1024  # multipart headers
        headers = dict(scope["headers"])
        try:
            if int(headers.get(b"content-length", b"0")) > limit:
                return await JSONResponse({"detail": "请求体超过大小限制。"}, status_code=413)(
                    scope, receive, send
                )
        except ValueError:
            return await JSONResponse({"detail": "无效的 Content-Length。"}, status_code=400)(
                scope, receive, send
            )
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(413, "请求体超过大小限制。")
            return message

        await self.app(scope, limited_receive, send)


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    if settings.auto_create_schema:
        await run_in_threadpool(init_db)
    application.state.generation_slots = asyncio.Semaphore(settings.max_concurrent_generations)
    if settings.task_mode == "local" and settings.app_env != "test":
        for job_id in await run_in_threadpool(recover_stale_jobs):
            await run_in_threadpool(dispatch_job, job_id)
    yield
    await run_in_threadpool(shutdown_local_workers)


app = FastAPI(
    title="Knowledge Base · Python AI Backend",
    version="1.0.0",
    description=(
        "带权限隔离、文档任务、引用检索与流式回答的后端项目。"
        "默认 hash/demo 为离线检索演示；真实模型需配置 API。"
        "先调用 login，再将 access_token 填入 Authorize。"
    ),
    lifespan=lifespan,
)
app.add_middleware(BodyLimitMiddleware)
api = APIRouter(prefix="/api/v1")
Db = Annotated[Session, Depends(get_db)]
Authenticated = Annotated[User, Depends(current_user)]


@app.middleware("http")
async def request_metadata(request: Request, call_next):
    request_id = new_id()  # never log untrusted caller-supplied IDs
    request.state.request_id = request_id
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Logs exclude query strings, headers, request bodies and generated answers.
    logger.info(
        "http_request request_id=%s method=%s status=%s headers_latency_ms=%.1f",
        request_id,
        request.method,
        response.status_code,
        (time.perf_counter() - started) * 1000,
    )
    return response


@app.exception_handler(RequestValidationError)
async def safe_validation_error(_: Request, exc: RequestValidationError):
    # Default validation errors may echo plaintext passwords/API inputs.
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
                for error in exc.errors()
            ]
        },
    )


@app.exception_handler(Exception)
async def safe_internal_error(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", new_id())
    logger.error("request_failed request_id=%s error_type=%s", request_id, type(exc).__name__)
    return JSONResponse(
        status_code=500,
        content={"detail": "服务内部错误，请根据请求 ID 检查服务日志。"},
        headers={"X-Request-ID": request_id, "X-Content-Type-Options": "nosniff"},
    )


def owned_kb(session: Session, kb_id: str, owner_id: str, lock: bool = False) -> KnowledgeBase:
    query = select(KnowledgeBase).where(
        KnowledgeBase.id == kb_id, KnowledgeBase.owner_id == owner_id, KnowledgeBase.deleted_at.is_(None)
    )
    kb = session.scalar(query.with_for_update() if lock else query)
    if kb is None:
        raise HTTPException(404, "知识库不存在。")
    return kb


def owned_document(session: Session, document_id: str, owner_id: str, lock: bool = False) -> Document:
    doc = session.get(Document, document_id)
    if doc is None or doc.deleted_at is not None:
        raise HTTPException(404, "文档不存在。")
    owned_kb(session, doc.knowledge_base_id, owner_id, lock=lock)
    if lock:
        doc = session.scalar(
            select(Document)
            .where(Document.id == document_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if doc is None or doc.deleted_at is not None:
            raise HTTPException(404, "文档不存在。")
    return doc


def owned_conversation(session: Session, conversation_id: str, owner_id: str) -> Conversation:
    conv = session.scalar(
        select(Conversation).where(Conversation.id == conversation_id, Conversation.owner_id == owner_id)
    )
    if conv is None:
        raise HTTPException(404, "会话不存在。")
    owned_kb(session, conv.knowledge_base_id, owner_id)
    return conv


def schedule_safely(job_id: str):
    try:
        dispatch_job(job_id)
    except Exception as exc:
        # Durable queued row remains recoverable by beat or the CLI.
        logger.warning("queue_publish_deferred job_id=%s error_type=%s", job_id, type(exc).__name__)


def auth_rate_limit(request: Request):
    ip = request.client.host if request.client else "unknown"
    limiter.check(f"auth:{ip}", settings.auth_requests_per_minute)


@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/docs")


@app.get("/health", tags=["system"])
def health():
    return {
        "status": "ok",
        "mode": settings.app_env,
        "task_mode": settings.task_mode,
        "llm_provider": settings.llm_provider,
        "embedding_provider": settings.embedding_provider,
    }


@app.get("/ready", tags=["system"])
def readiness():
    checks = {}
    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1 FROM users LIMIT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "unavailable"
    if settings.task_mode == "celery" or settings.rate_limit_backend == "redis":
        try:
            with redis.Redis.from_url(
                settings.redis_url, socket_timeout=2, socket_connect_timeout=2
            ) as client:
                client.ping()
            checks["redis"] = "ok"
        except redis.RedisError:
            checks["redis"] = "unavailable"
    ok = all(value == "ok" for value in checks.values())
    return JSONResponse(
        {"status": "ready" if ok else "unavailable", "checks": checks}, status_code=200 if ok else 503
    )


@api.post("/auth/register", response_model=UserOut, status_code=201, tags=["auth"])
def register(body: Credentials, request: Request, session: Db):
    auth_rate_limit(request)
    user = User(email=body.email, password_hash=hash_password(body.password))
    begin_write(session)
    session.add(user)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "邮箱已被注册。") from None
    return user


@api.post("/auth/login", tags=["auth"])
def login(body: Credentials, request: Request, session: Db):
    auth_rate_limit(request)
    user = session.scalar(select(User).where(User.email == body.email))
    if not verify_password(body.password, user.password_hash if user else None):
        raise HTTPException(401, "邮箱或密码不正确。")
    return {"access_token": create_access_token(user.id), "token_type": "bearer"}


@api.get("/auth/me", response_model=UserOut, tags=["auth"])
def me(user: Authenticated):
    return user


@api.post("/knowledge-bases", response_model=KnowledgeBaseOut, status_code=201, tags=["knowledge bases"])
def create_kb(body: KnowledgeBaseIn, session: Db, user: Authenticated):
    begin_write(session)
    kb = KnowledgeBase(owner_id=user.id, name=body.name)
    session.add(kb)
    session.commit()
    return kb


@api.get("/knowledge-bases", response_model=list[KnowledgeBaseOut], tags=["knowledge bases"])
def list_kbs(
    session: Db, user: Authenticated, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)
):
    return session.scalars(
        select(KnowledgeBase)
        .where(KnowledgeBase.owner_id == user.id, KnowledgeBase.deleted_at.is_(None))
        .order_by(KnowledgeBase.created_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()


@api.get("/knowledge-bases/{kb_id}", response_model=KnowledgeBaseOut, tags=["knowledge bases"])
def get_kb(kb_id: str, session: Db, user: Authenticated):
    return owned_kb(session, kb_id, user.id)


@api.patch("/knowledge-bases/{kb_id}", response_model=KnowledgeBaseOut, tags=["knowledge bases"])
def rename_kb(kb_id: str, body: KnowledgeBaseIn, session: Db, user: Authenticated):
    begin_write(session)
    kb = owned_kb(session, kb_id, user.id, lock=True)
    kb.name = body.name
    session.commit()
    return kb


def tombstone_document(session: Session, document: Document):
    document.status, document.deleted_at = "deleted", utcnow()
    job = session.scalar(
        select(IngestionJob).where(IngestionJob.document_id == document.id).with_for_update()
    )
    if job:
        job.status, job.lease_token, job.updated_at = "cancelled", None, utcnow()


@api.delete("/knowledge-bases/{kb_id}", status_code=204, tags=["knowledge bases"])
def delete_kb(kb_id: str, session: Db, user: Authenticated):
    begin_write(session)
    kb = owned_kb(session, kb_id, user.id, lock=True)
    documents = session.scalars(
        select(Document)
        .where(Document.knowledge_base_id == kb_id, Document.deleted_at.is_(None))
        .order_by(Document.id)
        .with_for_update()
    ).all()
    ids = [document.id for document in documents]
    for document in documents:
        tombstone_document(session, document)
    kb.deleted_at = utcnow()
    session.commit()
    for document_id in ids:
        cleanup_document(document_id)
    return Response(status_code=204)


@api.post("/knowledge-bases/{kb_id}/documents", status_code=202, tags=["documents"])
def upload_document(kb_id: str, file: UploadFile, session: Db, user: Authenticated):
    begin_write(session)
    owned_kb(session, kb_id, user.id, lock=True)
    filename = Path((file.filename or "document").replace("\\", "/")).name
    filename = re.sub(r"[\x00-\x1f\x7f]", "", filename)[:255]
    suffix = Path(filename).suffix.lower()
    if suffix not in {".pdf", ".txt", ".md"}:
        raise HTTPException(415, "仅支持 PDF、TXT、Markdown 文件。")
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    path = (settings.storage_dir / f"{new_id()}{suffix}").resolve()
    digest, size = hashlib.sha256(), 0
    try:
        with path.open("xb") as destination:
            while block := file.file.read(64 * 1024):
                size += len(block)
                if size > settings.max_upload_bytes:
                    raise HTTPException(413, "文件超过大小限制。")
                digest.update(block)
                destination.write(block)
        if size == 0:
            raise HTTPException(422, "不能上传空文件。")
        sha = digest.hexdigest()
        document = session.scalar(
            select(Document)
            .where(Document.knowledge_base_id == kb_id, Document.sha256 == sha)
            .with_for_update()
        )
        old_path = None
        if document and document.deleted_at is None:
            path.unlink(missing_ok=True)
            job = session.scalar(select(IngestionJob).where(IngestionJob.document_id == document.id))
            return {
                "document_id": document.id,
                "job_id": job.id,
                "status": document.status,
                "deduplicated": True,
            }
        if document:
            old_path = document.storage_path
            document.filename, document.storage_path, document.size_bytes = filename, str(path), size
            document.version += 1
            document.deleted_at, document.status, document.chunk_count = None, "queued", 0
            document.embedding_fingerprint = None
            session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document.id))
            job = session.scalar(
                select(IngestionJob).where(IngestionJob.document_id == document.id).with_for_update()
            )
            job.status, job.attempts, job.error = "queued", 0, None
            job.lease_token, job.started_at, job.updated_at = None, None, utcnow()
        else:
            document = Document(
                knowledge_base_id=kb_id,
                filename=filename,
                storage_path=str(path),
                sha256=sha,
                size_bytes=size,
            )
            session.add(document)
            session.flush()
            job = IngestionJob(document_id=document.id)
            session.add(job)
        session.commit()
    except IntegrityError:
        session.rollback()
        path.unlink(missing_ok=True)
        # Unique DB constraint handles a simultaneous identical SQLite upload.
        document = session.scalar(
            select(Document).where(
                Document.knowledge_base_id == kb_id,
                Document.sha256 == digest.hexdigest(),
                Document.deleted_at.is_(None),
            )
        )
        if document is None:
            raise HTTPException(409, "文档状态发生变化，请重试。") from None
        job = session.scalar(select(IngestionJob).where(IngestionJob.document_id == document.id))
        return {"document_id": document.id, "job_id": job.id, "status": document.status, "deduplicated": True}
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        file.file.close()
    if old_path:
        try:
            safe_storage_path(old_path).unlink(missing_ok=True)
        except OSError:
            logger.warning("old_file_cleanup_deferred document_id=%s", document.id)
    schedule_safely(job.id)
    return {"document_id": document.id, "job_id": job.id, "status": "queued", "deduplicated": False}


@api.get("/knowledge-bases/{kb_id}/documents", response_model=list[DocumentOut], tags=["documents"])
def list_documents(
    kb_id: str,
    session: Db,
    user: Authenticated,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    owned_kb(session, kb_id, user.id)
    return session.scalars(
        select(Document)
        .where(Document.knowledge_base_id == kb_id, Document.deleted_at.is_(None))
        .order_by(Document.created_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()


@api.get("/documents/{document_id}", response_model=DocumentOut, tags=["documents"])
def get_document(document_id: str, session: Db, user: Authenticated):
    return owned_document(session, document_id, user.id)


@api.get("/documents/{document_id}/download", tags=["documents"])
def download_document(document_id: str, session: Db, user: Authenticated):
    document = owned_document(session, document_id, user.id)
    path = safe_storage_path(document.storage_path)
    if not path.is_file():
        raise HTTPException(404, "原始文件已清理或不可用。")
    return FileResponse(path, filename=document.filename, media_type="application/octet-stream")


@api.delete("/documents/{document_id}", status_code=204, tags=["documents"])
def delete_document(document_id: str, session: Db, user: Authenticated):
    begin_write(session)
    document = owned_document(session, document_id, user.id, lock=True)
    tombstone_document(session, document)
    session.commit()
    cleanup_document(document_id)
    return Response(status_code=204)


@api.get("/jobs/{job_id}", response_model=JobOut, tags=["jobs"])
def get_job(job_id: str, session: Db, user: Authenticated):
    job = session.get(IngestionJob, job_id)
    if job is None:
        raise HTTPException(404, "任务不存在。")
    owned_document(session, job.document_id, user.id)
    return job


@api.post("/jobs/{job_id}/retry", response_model=JobOut, status_code=202, tags=["jobs"])
def retry_job(job_id: str, session: Db, user: Authenticated):
    begin_write(session)
    job = session.get(IngestionJob, job_id)
    if job is None:
        raise HTTPException(404, "任务不存在。")
    document = owned_document(session, job.document_id, user.id, lock=True)
    job = session.scalar(
        select(IngestionJob)
        .where(IngestionJob.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if job.status != "failed":
        raise HTTPException(409, "仅失败任务可以手动重试。")
    job.status, job.attempts, job.error = "queued", 0, None
    job.lease_token, job.started_at, job.updated_at = None, None, utcnow()
    document.status = "queued"
    session.commit()
    result = JobOut.model_validate(job)
    schedule_safely(job_id)
    return result


def search_chunks(session: Session, kb_id: str, question: str, top_k: int | None):
    options = settings.model_copy(update={"retrieval_top_k": top_k}) if top_k else settings
    try:
        return retrieve(session, kb_id, question, options)
    except TransientProviderError:
        raise HTTPException(503, "向量服务暂不可用，请稍后重试。") from None
    except PermanentDocumentError:
        raise HTTPException(503, "向量配置或已入库文档不匹配，请检查配置并重新入库。") from None


@api.post("/knowledge-bases/{kb_id}/search", tags=["search and chat"])
def search(kb_id: str, body: SearchIn, session: Db, user: Authenticated):
    owned_kb(session, kb_id, user.id)
    limiter.check(f"query:{user.id}", settings.chat_requests_per_minute)
    return {"citations": build_citations(search_chunks(session, kb_id, body.question, body.top_k))}


def prepare_chat(kb_id: str, body: ChatIn, owner_id: str):
    with write_session() as session:
        owned_kb(session, kb_id, owner_id)
        if body.conversation_id:
            conversation = owned_conversation(session, body.conversation_id, owner_id)
            if conversation.knowledge_base_id != kb_id:
                raise HTTPException(404, "会话不属于该知识库。")
        else:
            conversation = Conversation(owner_id=owner_id, knowledge_base_id=kb_id)
            session.add(conversation)
            session.flush()
        chunks = search_chunks(session, kb_id, body.question, body.top_k)
        session.add(Message(conversation_id=conversation.id, role="user", content=body.question))
        session.commit()
        return conversation.id, chunks


def persist_generation(
    owner_id: str,
    conversation_id: str,
    content: str,
    citations: list,
    status: str,
    started: float,
    first_token_ms: float | None,
    usage: dict,
    error_code: str | None,
) -> str:
    with write_session() as session:
        message = Message(
            conversation_id=conversation_id,
            role="assistant",
            content=content,
            citations=citations,
            status=status,
        )
        session.add(message)
        session.add(
            LLMCall(
                owner_id=owner_id,
                conversation_id=conversation_id,
                provider=settings.llm_provider,
                model=settings.llm_model if settings.llm_provider == "openai" else "extractive-demo",
                status=status,
                latency_ms=(time.perf_counter() - started) * 1000,
                first_token_ms=first_token_ms,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                error_code=error_code,
            )
        )
        session.flush()
        return message.id


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@api.post(
    "/knowledge-bases/{kb_id}/chat",
    tags=["search and chat"],
    responses={200: {"content": {"text/event-stream": {}}, "description": "SSE 事件流"}},
)
async def chat(kb_id: str, body: ChatIn, request: Request, user: Authenticated):
    await run_in_threadpool(limiter.check, f"query:{user.id}", settings.chat_requests_per_minute)
    slots = request.app.state.generation_slots
    try:
        await asyncio.wait_for(slots.acquire(), timeout=0.2)
    except TimeoutError:
        raise HTTPException(503, "生成服务繁忙，请稍后重试。", headers={"Retry-After": "2"}) from None
    try:
        started = time.perf_counter()
        conversation_id, chunks = await run_in_threadpool(prepare_chat, kb_id, body, user.id)
    except BaseException:
        slots.release()
        raise
    citations = build_citations(chunks)

    async def events():
        content, first_token_ms = "", None
        status, error_code = "completed", None
        usage = {"prompt_tokens": None, "completion_tokens": None}
        message_id = None
        try:
            yield sse(
                "meta",
                {
                    "conversation_id": conversation_id,
                    "provider": settings.llm_provider,
                    "model": settings.llm_model if settings.llm_provider == "openai" else "extractive-demo",
                },
            )
            yield sse("sources", {"citations": citations})
            async with aclosing(stream_answer(body.question, chunks, settings)) as answer_stream:
                async for item in answer_stream:
                    if await request.is_disconnected():
                        status, error_code = "cancelled", "client_disconnected"
                        break
                    if item["type"] == "token":
                        token = item["content"]
                        if len(content) + len(token) > 100_000:
                            raise GenerationError("output_limit", "模型输出超过限制。")
                        if first_token_ms is None:
                            first_token_ms = (time.perf_counter() - started) * 1000
                        content += token
                        yield sse("token", {"content": token})
                    elif item["type"] == "usage":
                        usage = {
                            "prompt_tokens": item.get("prompt_tokens"),
                            "completion_tokens": item.get("completion_tokens"),
                        }
        except asyncio.CancelledError:
            status, error_code = "cancelled", "client_disconnected"
            raise
        except GenerationError as exc:
            status, error_code = "failed", exc.code
            yield sse("error", {"code": exc.code, "message": exc.safe_message})
        except Exception as exc:
            status, error_code = "failed", "generation_internal_error"
            logger.warning(
                "generation_failed conversation_id=%s error_type=%s", conversation_id, type(exc).__name__
            )
            yield sse("error", {"code": error_code, "message": "生成失败，请稍后重新提问。"})
        finally:
            try:
                # Shield final accounting from Starlette's disconnect cancellation.
                with anyio.CancelScope(shield=True):
                    message_id = await run_in_threadpool(
                        persist_generation,
                        user.id,
                        conversation_id,
                        content,
                        citations,
                        status,
                        started,
                        first_token_ms,
                        usage,
                        error_code,
                    )
            finally:
                slots.release()
        if status != "cancelled":
            yield sse("done", {"message_id": message_id, "status": status, "usage": usage})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@api.get("/conversations", tags=["history"])
def list_conversations(
    session: Db,
    user: Authenticated,
    knowledge_base_id: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    query = (
        select(Conversation)
        .join(KnowledgeBase, KnowledgeBase.id == Conversation.knowledge_base_id)
        .where(Conversation.owner_id == user.id, KnowledgeBase.deleted_at.is_(None))
    )
    if knowledge_base_id:
        owned_kb(session, knowledge_base_id, user.id)
        query = query.where(Conversation.knowledge_base_id == knowledge_base_id)
    rows = session.scalars(query.order_by(Conversation.created_at.desc()).offset(offset).limit(limit)).all()
    return [
        {"id": row.id, "knowledge_base_id": row.knowledge_base_id, "created_at": row.created_at}
        for row in rows
    ]


@api.get("/conversations/{conversation_id}/messages", tags=["history"])
def list_messages(
    conversation_id: str,
    session: Db,
    user: Authenticated,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    owned_conversation(session, conversation_id, user.id)
    rows = session.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at, Message.id)
        .offset(offset)
        .limit(limit)
    ).all()
    return [
        {
            "id": row.id,
            "role": row.role,
            "content": row.content,
            "citations": row.citations,
            "status": row.status,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@api.get("/usage", tags=["usage"])
def usage_statistics(session: Db, user: Authenticated):
    query = select(LLMCall).where(LLMCall.owner_id == user.id)
    calls = session.scalar(select(func.count()).select_from(query.subquery()))
    successful = session.scalar(
        select(func.count()).select_from(query.where(LLMCall.status == "completed").subquery())
    )
    totals = session.execute(
        select(
            func.coalesce(func.sum(LLMCall.prompt_tokens), 0),
            func.coalesce(func.sum(LLMCall.completion_tokens), 0),
        ).where(LLMCall.owner_id == user.id)
    ).one()
    unknown = session.scalar(
        select(func.count()).select_from(
            query.where(LLMCall.prompt_tokens.is_(None) | LLMCall.completion_tokens.is_(None)).subquery()
        )
    )
    return {
        "calls": calls,
        "successful_calls": successful,
        "failed_calls": calls - successful,
        "prompt_tokens": totals[0],
        "completion_tokens": totals[1],
        "usage_unavailable_calls": unknown,
    }


app.include_router(api)
